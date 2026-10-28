#!/usr/bin/env python3
# 人民币价格源自建构建脚本：从阿里云百炼、DeepSeek 官方定价页抓取人民币价格，
# 生成 newapi 上游价格同步兼容的 ratio_config JSON（type1 格式）。
#
# 用法:
#   python3 build.py                  # 构建全部供应商
#   python3 build.py deepseek         # 只构建 DeepSeek
#   python3 build.py aliyun           # 只构建百炼
#   python3 build.py --rate 7.3      # 指定锁定汇率（默认 7.3，须与 newapi 站点汇率一致）
#
# 输出: dist/<provider>/ratio_config.json
# 接入: 在 newapi 系统设置 → 模型设置 → 上游价格同步中添加自定义源，
#       BaseURL 指向托管该 JSON 的地址，Endpoint 填 /ratio_config.json。
#
# 计价说明:
#   系数 = 官方人民币单价(元/百万tokens) / 汇率，即美元/百万tokens 计价的
#   newapi 表达式系数。站点展示货币为 CNY 且汇率与 --rate 一致时，
#   展示价格精确闭环回官方人民币整数价。
#
# 已知近似（与官方口径的差异）:
#   - 百炼缓存命中按通用规则"输入单价 20%"计（隐式缓存口径；各模型实际
#     折扣率见百炼上下文缓存文档，如需精确可手动微调）。
#   - DeepSeek 分时计价的表达式不支持中国法定节假日，节假日的周一至周五
#     会按高峰价计费（与 basellm 等通用做法一致）。
#   - 百炼 Batch 半价、限时折扣不参与计算，一律取原价。

import argparse
import json
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

RATE = 7.3
UA = "Mozilla/5.0 (X11; Linux x86_64) price-source-builder/1.0"


# ----------------------------------------------------------------------------
# 通用 HTML DOM 工具（零第三方依赖）
# ----------------------------------------------------------------------------

class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []
        self.parent = parent
        self.text = ""


class Dom(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", [], None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.children.append(n)
        if tag not in ("br", "img", "meta", "link", "input", "hr"):
            self.cur = n

    def handle_endtag(self, tag):
        p = self.cur
        while p is not None and p.tag != tag:
            p = p.parent
        if p is not None and p.parent is not None:
            self.cur = p.parent

    def handle_data(self, data):
        if self.cur is not None:
            self.cur.text += data


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def text_of(node):
    parts = []
    stack = [node]
    while stack:
        n = stack.pop(0)
        if n.text:
            parts.append(n.text)
        stack.extend(n.children)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def direct_rows(table):
    rows = []

    def rec(node):
        for child in node.children:
            if child.tag == "tr":
                rows.append(child)
            elif child.tag == "table":
                continue
            elif child.tag in ("thead", "tbody", "tfoot"):
                rec(child)
            else:
                rec(child)

    rec(table)
    return rows


def direct_cells(tr):
    cells = []

    def rec(node):
        for child in node.children:
            if child.tag in ("td", "th"):
                cells.append(child)
            elif child.tag == "table":
                continue
            else:
                rec(child)

    rec(tr)
    return cells


def top_level_tables(dom):
    tables = []
    for n in walk(dom.root):
        if n.tag != "table":
            continue
        p = n.parent
        nested = False
        while p is not None:
            if p.tag == "table":
                nested = True
                break
            p = p.parent
        if not nested:
            tables.append(n)
    return tables


def fetch(url, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


# ----------------------------------------------------------------------------
# 价格与区间解析
# ----------------------------------------------------------------------------

def parse_price(text, loose=False):
    if "免费" in text:
        return 0.0
    m = re.search(r"原价\s*([\d.]+)\s*元", text)
    if m:
        return float(m.group(1))
    m = re.search(r"([\d.]+)\s*元", text)
    if m:
        return float(m.group(1))
    if loose:
        # 智谱 pricing.md 的表格价格是纯数字（无"元"字）
        m = re.match(r"^\s*([\d.]+)\s*$", text)
        if m:
            return float(m.group(1))
    return None


def parse_range_upper(text):
    if "无阶梯" in text:
        return None
    m = re.search(r"≤\s*([\d.]+)\s*([KMkm])", text)
    if m:
        n = float(m.group(1))
        return int(n * 1000 if m.group(2).upper() == "K" else n * 1000000)
    return None


def is_range_cell(text):
    if "无阶梯" in text:
        return True
    if re.search(r"(Token\s*数|Token\s*范围)", text):
        return False
    # 形如 "256K<Token≤1M"、"0<Token≤0.2K"（数字可带 K/M 单位）
    return bool(re.search(r"\d+(?:\.\d+)?[KMkm]?\s*<\s*Token", text))


def extract_model_id(text):
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._\-/]*)", text)
    return m.group(1) if m else None


def coeff(cny):
    """表达式系数直接使用官方人民币原值（元/百万 tokens），不做汇率换算。

    newapi 侧按模型币种在扣费时折算，展示时原样输出，因此源里存的就是官方数字。
    """
    return round(cny, 10)


def fmt(n):
    return str(round(n, 10))


# ----------------------------------------------------------------------------
# 表达式生成
# ----------------------------------------------------------------------------

def tier_expr(tier_name, input_, cache, output, include_cache=True):
    terms = [f"p * {fmt(coeff(input_))}"]
    if include_cache:
        terms.append(f"cr * {fmt(coeff(cache))}")
        terms.append("cc * 0")
    terms.append(f"c * {fmt(coeff(output))}")
    return f'tier("{tier_name}", ' + " + ".join(terms) + ")"


def build_tiered_expr(tiers, thinking_toggle=False):
    """tiers: [{range: int|None, input, output, output_thinking?}] 按 range 升序。"""
    cache_ratio = 0.2  # 百炼隐式缓存命中按输入单价 20% 计

    def cache_of(t):
        # 官方缓存命中单价优先；否则按通用规则取输入单价 20%
        if t.get("cache") is not None:
            return t["cache"]
        return t["input"] * cache_ratio

    def standard_tier(t, name):
        return tier_expr(name, t["input"], cache_of(t), t["output"])

    def thinking_tier(t, name):
        out = t.get("output_thinking", t["output"])
        return tier_expr(name, t["input"], cache_of(t), out)

    def tier_chain(tier_fn):
        if len(tiers) == 1:
            return tier_fn(tiers[0], "standard")
        # 从末档往前拼：末档（range=None）兜底，其余按"len <= 上界"判断
        expr = None
        for i in range(len(tiers) - 1, -1, -1):
            t = tiers[i]
            upper = t["range"]
            label = "plus" if upper is None else str(upper)
            body = tier_fn(t, f"tier_{i}_{label}")
            if expr is None:
                expr = body
            else:
                expr = f"len <= {upper} ? {body} : {expr}"
        return expr

    if not thinking_toggle:
        return tier_chain(standard_tier)

    std = tier_chain(standard_tier)
    think = tier_chain(thinking_tier)
    return f'param("enable_thinking") == true ? {think} : {std}'


DEEPSEEK_PEAK_COND = (
    'weekday("UTC") >= 1 && weekday("UTC") <= 5 && '
    '((hour("UTC") >= 1 && hour("UTC") < 4) || '
    '(hour("UTC") >= 6 && hour("UTC") < 10))'
)


# ----------------------------------------------------------------------------
# DeepSeek
# ----------------------------------------------------------------------------

DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"


def build_deepseek():
    html = fetch(DEEPSEEK_PRICING_URL)
    dom = Dom()
    dom.feed(html)
    tables = top_level_tables(dom)

    models = {}

    def grab(pattern, text):
        m = re.search(pattern, text)
        return float(m.group(1)) if m else None

    for table in tables:
        rows = direct_rows(table)
        texts = [text_of(c) for r in rows for c in direct_cells(r)]
        joined = "|".join(texts)
        if "缓存命中" not in joined or "百万tokens输出" not in joined:
            continue

        model_names = re.findall(r"\|(deepseek-[a-z0-9.\-]+)", joined)
        if not model_names:
            continue

        # 价格顺序: 空闲[flash, pro] 高峰[flash, pro]
        # 表格文本按列拼接，直接按模型数分组提取
        per = len(model_names)
        nums = re.findall(r"([\d.]+)元", joined)
        if len(nums) < per * 6:
            print(f"warning: deepseek price cells incomplete: {nums}", file=sys.stderr)
            continue

        # 依次: 缓存命中空闲per个、缓存命中高峰per个、输入空闲per个、
        #       输入高峰per个、输出空闲per个、输出高峰per个
        blocks = [nums[i : i + per] for i in range(0, per * 6, per)]
        for idx, name in enumerate(model_names):
            models[name] = {
                "cache": (float(blocks[0][idx]), float(blocks[1][idx])),
                "input": (float(blocks[2][idx]), float(blocks[3][idx])),
                "output": (float(blocks[4][idx]), float(blocks[5][idx])),
            }

    if not models:
        raise RuntimeError("no deepseek models parsed")

    exprs = {}
    for name, p in models.items():
        inp_off, inp_peak = p["input"]
        out_off, out_peak = p["output"]
        cache_off, cache_peak = p["cache"]
        peak_expr = tier_expr("peak", inp_peak, cache_peak, out_peak)
        off_expr = tier_expr("off_peak", inp_off, cache_off, out_off)
        exprs[name] = f"{DEEPSEEK_PEAK_COND} ? {peak_expr} : {off_expr}"

    return {"billing_expr": exprs}


# ----------------------------------------------------------------------------
# 阿里云百炼
# ----------------------------------------------------------------------------

ALIYUN_BILLING_URL = (
    "https://help.aliyun.com/zh/model-studio/billing-for-model-studio"
)


def is_snapshot(name):
    return bool(
        re.search(r"-\d{4}-\d{2}-\d{2}$", name) or re.search(r"-\d{4}$", name)
    )


def strip_snapshot(name):
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", re.sub(r"-\d{4}$", "", name))


def build_aliyun(keep_snapshots=False):
    html = fetch(ALIYUN_BILLING_URL)
    dom = Dom()
    dom.feed(html)

    models = {}  # model_id -> tiers list
    cur_h4 = None
    current = None  # 当前模型（用于续行）

    for n in walk(dom.root):
        if n.tag == "h4":
            cur_h4 = text_of(n)
        if n.tag != "table" or cur_h4 != "华北2（北京）":
            continue
        p = n.parent
        nested = False
        while p is not None:
            if p.tag == "table":
                nested = True
                break
            p = p.parent
        if nested:
            continue

        # 表头判断是否有独立双输出列（非思考/思考）。B 类型表头是双行的：
        # 第二表头行是 ["非思考模式", "思考模式（思维链+回答）"]，故合并前两行检测。
        rows = direct_rows(n)
        if not rows:
            continue
        header_text = " ".join(
            text_of(c) for r in rows[:2] for c in direct_cells(r)
        )
        # 仅处理按 token 计价的文本模型；图像/视频表为"元/张"、"元/秒"。
        if "每百万" not in header_text:
            continue
        # omni/realtime/livetranslate 类的表头按"文本/音频/图片"分列计价，
        # 且输出价随输入模态变化（newapi 表达式无法表达），第一版排除，
        # 这些模型请在定价页手动配置。
        if "音频" in header_text:
            continue
        # 简化四价列表格: 模型ID | 服务部署范围 | 输入 | 缓存命中 | 输出
        # （缓存命中为官方单价，优先于 20% 通用规则）
        has_cache_col = "缓存命中输入单价" in header_text
        dual_output = "非思考模式" in header_text

        for r in rows:
            cells = [text_of(c) for c in direct_cells(r)]
            if not cells:
                continue
            first = cells[0]

            # 跳过表头行
            if "模型 ID" in first or first == "非思考模式":
                continue

            model_id = extract_model_id(first)

            if is_range_cell(first) or first == "无阶梯计价":
                # 续行（阶梯档）
                if current is None:
                    continue
                upper = parse_range_upper(first)
                if dual_output and len(cells) >= 4:
                    inp = parse_price(cells[1])
                    out_std = parse_price(cells[2])
                    out_think = parse_price(cells[3])
                else:
                    inp = parse_price(cells[1]) if len(cells) >= 3 else None
                    out = parse_price(cells[2]) if len(cells) >= 3 else None
                    out_std, out_think = out, out
                if inp is None or out_std is None:
                    continue
                current["tiers"].append(
                    {
                        "range": upper,
                        "input": inp,
                        "output": out_std,
                        "output_thinking": out_think,
                    }
                )
                continue

            if not model_id or len(cells) < 4:
                continue

            if has_cache_col:
                # 模型ID | 部署范围 | 输入 | 缓存命中 | 输出
                if len(cells) < 5 or "中国内地" not in cells[1]:
                    current = None
                    continue
                inp = parse_price(cells[2])
                cache = parse_price(cells[3])
                out_std = parse_price(cells[4])
                out_think = out_std
                range_col = "无阶梯计价"
                upper = None
            elif dual_output and len(cells) >= 5:
                # 模型ID | 区间 | 输入 | 输出非思考 | 输出思考 | 免费额度
                range_col = cells[1]
                inp = parse_price(cells[2])
                out_std = parse_price(cells[3])
                out_think = parse_price(cells[4])
            else:
                # 模型ID | (模式) | 区间 | 输入 | 输出 | 免费额度
                # 或: 模型ID | 区间 | 输入 | 输出 | 免费额度
                if "模式" in " ".join(cells[:2]) and len(cells) >= 5:
                    range_col = cells[2]
                    inp = parse_price(cells[3])
                    out_std = parse_price(cells[4])
                else:
                    range_col = cells[1]
                    inp = parse_price(cells[2])
                    out_std = parse_price(cells[3])
                out_think = out_std

            if inp is None or out_std is None:
                continue
            has_dual = (
                out_think is not None
                and out_std is not None
                and out_think != out_std
            )
            upper = (
                None if has_cache_col else parse_range_upper(range_col)
            )
            first_tier = {
                "range": upper,
                "input": inp,
                "output": out_std,
                "output_thinking": out_think if has_dual else out_std,
            }
            if has_cache_col and cache is not None:
                first_tier["cache"] = cache
            entry = {"tiers": [first_tier], "dual": has_dual}
            # 同名模型（不同快照系列出现重复）取首次
            if model_id not in models:
                models[model_id] = entry
            current = models[model_id] if model_id in models else entry

    if not models:
        raise RuntimeError("no aliyun models parsed")

    if not keep_snapshots:
        # 别名（无快照后缀）已覆盖当前价格时，去掉历史快照版本，减少同步噪音。
        all_names = set(models)
        models = {
            m: e
            for m, e in models.items()
            if not (is_snapshot(m) and strip_snapshot(m) in all_names)
        }

    exprs = {}
    for model_id, entry in models.items():
        exprs[model_id] = build_tiered_expr(entry["tiers"], entry["dual"])

    return {"billing_expr": exprs}


# ----------------------------------------------------------------------------
# 智谱 AI（官方 pricing.md，Markdown 表格）
# ----------------------------------------------------------------------------

ZHIPU_PRICING_URL = "https://docs.bigmodel.cn/cn/guide/start/pricing.md"


def zhipu_range_upper(text):
    """返回该行阶梯的输入上界；单档返回 None，末档（输入 ≥X）返回 -1。"""
    if not text:
        return None
    # 只解析"输入"维度：如"输入 [0, 32K)，输出 ≥0.2K"的输出条件不影响输入档
    input_part = text.split("输出")[0]
    if "≥" in input_part:
        return -1
    m = re.search(r"\[\s*\d+\s*[KkMm]?\s*,\s*([\d.]+)\s*([KkMm])", input_part)
    if m:
        n = float(m.group(1))
        return int(n * 1000 if m.group(2).lower() == "k" else n * 1000000)
    return None


def build_zhipu():
    md = fetch(ZHIPU_PRICING_URL)
    col = None
    entries = {}  # model -> {upper: {"input","output","cache"}}
    single = {}  # model -> {"input","output","cache"}（非阶梯）

    for raw in md.splitlines():
        s = raw.strip()
        if s.startswith(">"):
            s = s.lstrip(">").strip()
        if not s.startswith("|") or not s.endswith("|"):
            continue
        cells = [
            c.strip().replace("\\[", "[").replace("\\]", "]")
            for c in s.strip("|").split("|")
        ]
        if not any(cells):
            continue

        # 表头行：含模型列且价格单位是"元/百万 Tokens"
        if "模型名称" in cells[0] or "模型 ID" in cells[0]:
            if not any("元/百万" in c for c in cells):
                col = None
                continue
            col = {}
            for i, h in enumerate(cells):
                if "输入单价" in h:
                    col["input"] = i
                elif "输出单价" in h:
                    col["output"] = i
                elif "缓存命中" in h:
                    col["cache"] = i
            continue
        if col is None or set(cells) <= {"-", ""}:
            continue
        if "input" not in col or "output" not in col:
            continue

        model = cells[0].strip()
        if not model or model.startswith("-"):
            continue
        # 智谱定价表用大写展示名（GLM-5.3），API 模型名为小写
        model = model.lower()
        try:
            inp = parse_price(cells[col["input"]], loose=True)
            out = parse_price(cells[col["output"]], loose=True)
            cache = (
                parse_price(cells[col["cache"]], loose=True)
                if "cache" in col and col["cache"] < len(cells)
                else None
            )
        except IndexError:
            continue
        if inp is None or out is None:
            continue
        cache = cache if cache is not None else inp * 0.2

        range_text = cells[1] if len(cells) > 1 else ""
        upper = zhipu_range_upper(range_text)
        if upper is None:
            # 上下文列（如 "1M"）或未标注区间 => 单档
            if model not in single and model not in entries:
                single[model] = {"input": inp, "output": out, "cache": cache}
            continue
        tier = entries.setdefault(model, {})
        prev = tier.get(upper)
        # 同一输入档出现多行（输出长度阶梯）时取输出高价，保守计费
        if prev is None:
            tier[upper] = {"input": inp, "output": out, "cache": cache}
        else:
            prev["output"] = max(prev["output"], out)
            prev["cache"] = max(prev["cache"], cache)

    exprs = {}
    for model in set(entries) | set(single):
        if model in single:
            t = single[model]
            exprs[model] = build_tiered_expr(
                [
                    {
                        "range": None,
                        "input": t["input"],
                        "output": t["output"],
                        "cache": t["cache"],
                    }
                ]
            )
            continue
        tiers = []
        for upper in sorted(entries[model], key=lambda u: (u == -1, u)):
            t = entries[model][upper]
            tiers.append(
                {
                    "range": None if upper == -1 else upper,
                    "input": t["input"],
                    "output": t["output"],
                    "cache": t["cache"],
                }
            )
        exprs[model] = build_tiered_expr(tiers)

    if not exprs:
        raise RuntimeError("no zhipu models parsed")
    return {"billing_expr": exprs}


# ----------------------------------------------------------------------------
# Kimi（官方定价页：表格数据在流式 JSON 里）
# ----------------------------------------------------------------------------

KIMI_PRICING_URL = "https://platform.kimi.com/docs/pricing/chat"


def build_kimi():
    html = fetch(KIMI_PRICING_URL)
    # 页面把表格 data 塞进 self.__next_f.push([1,"..."]) 的流式片段
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    if not chunks:
        raise RuntimeError("kimi: no __next_f chunks found")
    blob = "".join(chunks).replace('\\"', '"').replace("\\n", "\n")

    exprs = {}
    # 每个表格: rows:[[_jsx(...children:`model`) ,`¥x / MTok`,`¥y / MTok`,`¥z / MTok`,`¥w / MTok`,`¥v / MTok`], ...]
    for block in re.finditer(r"rows:\[\[(.*?)\]\]\}", blob, re.S):
        body = block.group(1)
        for row_text in re.split(r"\],\s*\[", body):
            row_text = row_text.strip("[]")
            name_m = re.search(r"children:`([A-Za-z0-9][A-Za-z0-9._\-]*)`", row_text)
            if not name_m:
                continue
            name = name_m.group(1)
            if not name.startswith("kimi-"):
                continue
            prices = [float(x) for x in re.findall(r"¥([\d.]+) / MTok", row_text)]
            # 列顺序：输入(未命中) 输出 缓存命中 缓存写入5min 缓存写入1h
            if len(prices) < 3:
                continue
            p_in, p_out, p_cache = prices[0], prices[1], prices[2]
            cc5 = prices[3] if len(prices) > 3 else 0.0
            cc1h = prices[4] if len(prices) > 4 else 0.0
            terms = [
                f"p * {fmt(p_in)}",
                f"cr * {fmt(p_cache)}",
                f"cc * {fmt(cc5)}",
            ]
            if cc1h:
                terms.append(f"cc1h * {fmt(cc1h)}")
            terms.append(f"c * {fmt(p_out)}")
            exprs[name] = 'tier("standard", ' + " + ".join(terms) + ")"

    if not exprs:
        raise RuntimeError("no kimi models parsed")
    return {"billing_expr": exprs}


# ----------------------------------------------------------------------------
# MiniMax（官方定价页 Markdown 表格）
# ----------------------------------------------------------------------------

MINIMAX_PRICING_URL = "https://platform.minimax.cn/docs/pricing/overview.md"


def build_minimax():
    md = fetch(MINIMAX_PRICING_URL)
    exprs = {}
    in_language_section = False
    skip_block = False  # "优先"档按标准价 1.5 倍，表达式无法区分，跳过

    for raw in md.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_language_section = "语言模型" in line
            continue
        if not line.startswith("|"):
            # 档位标题：**标准** / **优先**
            if "优先" in line:
                skip_block = True
            elif "标准" in line:
                skip_block = False
            continue
        if not in_language_section or skip_block:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        # 跳表头/分隔行
        if "模型" in cells[0] or set(cells[0]) <= {"-", ":", " "}:
            continue

        # 模型名去掉 ** 和档位后缀；价格列取最终价（删除线后那个）
        raw_name = re.sub(r"\*+", "", cells[0]).strip()
        name_m = re.match(r"(MiniMax-[A-Za-z0-9.\-]+)", raw_name)
        if not name_m:
            continue
        name = name_m.group(1)

        def final_price(cell):
            # "~~4.20~~ 2.10" -> 2.10
            cleaned = re.sub(r"~~[^~]*~~", "", cell).strip()
            m = re.search(r"([\d.]+)", cleaned)
            return float(m.group(1)) if m else None

        p_in = final_price(cells[1])
        p_out = final_price(cells[2])
        p_cache = final_price(cells[3]) if len(cells) > 3 else None
        if p_in is None or p_out is None:
            continue

        upper = None
        is_last_tier = False
        if "512k" in raw_name:
            if "≤" in raw_name:
                upper = 512000
            else:
                is_last_tier = True
        cache = p_cache if p_cache is not None else p_in * 0.2

        tier = {"range": upper, "input": p_in, "output": p_out, "cache": cache}
        entry = exprs.get(name)
        if not is_last_tier and upper is None:
            if entry is None:
                exprs[name] = {"tiers": [tier]}
        else:
            if entry is None:
                exprs[name] = {"tiers": []}
                entry = exprs[name]
            entry["tiers"].append(tier)

    result = {}
    for name, entry in exprs.items():
        tiers = sorted(entry["tiers"], key=lambda t: (t["range"] is None, t["range"] or 0))
        if not tiers:
            continue
        result[name] = build_tiered_expr(tiers)

    if not result:
        raise RuntimeError("no minimax models parsed")
    return {"billing_expr": result}


# ----------------------------------------------------------------------------
# 阶跃（官方定价页 Markdown 表格）
# ----------------------------------------------------------------------------

STEPFUN_PRICING_URL = "https://platform.stepfun.com/docs/zh/guides/pricing/details.md"


def build_stepfun():
    md = fetch(STEPFUN_PRICING_URL)
    exprs = {}
    in_text_section = False

    for raw in md.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_text_section = "文本与多模态模型" in line
            continue
        if not in_text_section or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        name_m = re.match(r"`?([A-Za-z0-9][A-Za-z0-9._\-]*)`?", cells[0])
        if not name_m:
            continue
        name = name_m.group(1)
        if name.lower() in ("model id",) or set(cells[0]) <= {"-", ":", " "}:
            continue
        try:
            p_in = float(cells[1])
            p_cache = float(cells[2])
            p_out = float(cells[3])
        except ValueError:
            continue
        exprs[name] = (
            'tier("standard", '
            + f"p * {fmt(p_in)} + cr * {fmt(p_cache)} + cc * 0 + c * {fmt(p_out)})"
        )

    if not exprs:
        raise RuntimeError("no stepfun models parsed")
    return {"billing_expr": exprs}


# ----------------------------------------------------------------------------
# 统一源：美元价取内置源（官方国际站价）+ 人民币价取自建（官方中国站价）
# ----------------------------------------------------------------------------

# 内置源里需要收录的 provider（全部为官方美元价）
# 国模的国际站 provider 与国内站 provider 用的是不同模型名，因此美元段与人民币段
# 各自独立存放，互不覆盖。
BASELLM_USD_PROVIDERS = [
    # 国模的国际站（官方美元价）
    "zai",          # 智谱国际
    "alibaba",      # 阿里国际
    "moonshotai",   # Kimi 国际
    "minimax",      # MiniMax 国际
    "volcengine",   # 火山（暂无人民币自建源，仅有美元段）
    "stepfun",      # 阶跃
    "deepseek",     # DeepSeek
    # 外模
    "openai",
    "anthropic",
    "google",
    "xai",
    "mistral",
    "cohere",
    "meta",
]

# 人民币自建源（官方中国站人民币原价）。
# 顺序即优先级：厂商自己的官方站优先，转售平台（百炼）靠后，
# 否则百炼转售的 GLM 缓存价会覆盖智谱官方价。
LOCAL_CNY_PROVIDERS = ["zhipu", "kimi", "minimax", "stepfun", "deepseek", "aliyun"]

BASELLM_PROVIDER_URL = (
    "https://basellm.github.io/llm-metadata/api/newapi/providers"
    "/{provider}/ratio_config-v1-base.json"
)


def fetch_basellm(provider):
    url = BASELLM_PROVIDER_URL.format(provider=provider)
    payload = json.loads(fetch(url))
    if not payload.get("success"):
        raise RuntimeError(f"basellm {provider}: success=false")
    data = payload.get("data") or {}
    exprs = data.get("billing_expr") or {}
    if not exprs:
        raise RuntimeError(f"basellm {provider}: empty billing_expr")
    return exprs


def build_unified():
    """生成单一合并源：

    - usd_billing_expr:   各厂商官方美元价（内置源，含国模国际站与外模）
    - cny_billing_expr:   国模官方人民币价（自建，从官方中国站定价页抓取）
    - currency:           每个模型的原始币种（决定模型广场显示哪种货币）
    """
    manifest = {
        "built_at_utc": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
        "rate": RATE,
        "sources": {},
        "usd_models": 0,
        "cny_models": 0,
        "currency_map": {},
    }

    usd_exprs = {}
    for provider in BASELLM_USD_PROVIDERS:
        try:
            exprs = fetch_basellm(provider)
        except Exception as exc:  # noqa: BLE001
            manifest["sources"][f"usd:{provider}"] = {"error": str(exc)}
            continue
        kept = 0
        for m, e in exprs.items():
            if m not in usd_exprs:
                usd_exprs[m] = e
                kept += 1
        manifest["sources"][f"usd:{provider}"] = {"fetched": len(exprs), "kept": kept}

    cny_exprs = {}
    for name in LOCAL_CNY_PROVIDERS:
        builder = PROVIDERS.get(name)
        if builder is None:
            continue
        data = builder()
        exprs = data["billing_expr"]
        overridden = 0
        for m, e in exprs.items():
            if m in cny_exprs:
                overridden += 1
                continue
            cny_exprs[m] = e
        manifest["sources"][f"cny:{name}"] = {
            "built": len(exprs),
            "skipped_dup": overridden,
        }

    manifest["usd_models"] = len(usd_exprs)
    manifest["cny_models"] = len(cny_exprs)

    # 原始币种：有人民币官方价的模型视为国模（显示人民币），其余为美元
    currency = {}
    for m in usd_exprs:
        currency[m] = "CNY" if m in cny_exprs else "USD"
    for m in cny_exprs:
        currency.setdefault(m, "CNY")
    manifest["currency_map"] = {
        "CNY": sum(1 for v in currency.values() if v == "CNY"),
        "USD": sum(1 for v in currency.values() if v == "USD"),
    }

    return usd_exprs, cny_exprs, currency, manifest


# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------

PROVIDERS = {
    "deepseek": build_deepseek,
    "aliyun": build_aliyun,
    "zhipu": build_zhipu,
    "minimax": build_minimax,
    "kimi": build_kimi,
    "stepfun": build_stepfun,
}

# 页面改版导致静默残缺时的下限保护
MIN_MODELS = {"deepseek": 2, "aliyun": 25}


def verify_exprs(exprs):
    """审计：系数直接是官方原价，只校验非负与可解析。"""
    suspicious = []
    for model, expr in exprs.items():
        for mm in re.finditer(r"(?<![a-z])(p|cr|cc1h|cc|c) \* ([\d.]+)", expr):
            val = float(mm.group(2))
            if val < 0:
                suspicious.append((model, mm.group(1), val))
    return suspicious


def write_ratio_config(path, exprs, modes, data_extra=None):
    data = {
        "billing_expr": exprs,
        "billing_mode": modes,
    }
    if data_extra:
        data.update(data_extra)
    payload = {
        "success": True,
        "message": "",
        "data": data,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def main():
    global RATE
    ap = argparse.ArgumentParser(description="build CNY-anchored ratio_config")
    ap.add_argument("providers", nargs="*", default=[])
    ap.add_argument("--rate", type=float, default=7.3)
    ap.add_argument(
        "--snapshots", action="store_true", help="keep dated snapshot model IDs"
    )
    ap.add_argument(
        "--unified",
        action="store_true",
        help="build local providers and merge with basellm closed-loop "
        "providers into dist/ratio_config.json",
    )
    args = ap.parse_args()
    RATE = args.rate

    out_dir = Path(__file__).parent / "dist"

    if args.unified:
        usd_exprs, cny_exprs, currency, manifest = build_unified()
        write_ratio_config(
            out_dir / "ratio_config.json",
            usd_exprs,
            {m: "tiered_expr" for m in usd_exprs},
            data_extra={
                "cny_billing_expr": cny_exprs,
                "currency": currency,
            },
        )
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        print(
            f"unified: {len(usd_exprs)} USD models, {len(cny_exprs)} CNY models "
            f"-> {out_dir / 'ratio_config.json'}"
        )
        bad_cny = verify_exprs(cny_exprs)
        if bad_cny:
            print(f"WARNING: {len(bad_cny)} CNY coefficients invalid", file=sys.stderr)
        else:
            print(f"  CNY price check: all {len(cny_exprs)} models OK")
        return

    names = list(PROVIDERS) if not args.providers or "all" in args.providers else args.providers
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        ap.error(f"unknown providers: {unknown} (choose from {list(PROVIDERS)})")
    for name in names:
        data = (
            PROVIDERS[name](keep_snapshots=args.snapshots)
            if name == "aliyun"
            else PROVIDERS[name]()
        )
        write_ratio_config(
            out_dir / name / "ratio_config.json",
            data["billing_expr"],
            {m: "tiered_expr" for m in data["billing_expr"]},
        )
        print(f"{name}: {len(data['billing_expr'])} models -> {out_dir / name / 'ratio_config.json'}")

        count = len(data["billing_expr"])
        if count < MIN_MODELS.get(name, 1):
            print(
                f"WARNING: {name} only parsed {count} models "
                f"(expected >= {MIN_MODELS.get(name)}); "
                "the upstream page structure may have changed.",
                file=sys.stderr,
            )
        suspicious = verify_exprs(data["billing_expr"])
        if suspicious:
            print(
                f"WARNING: {name} has {len(suspicious)} price coefficients that "
                "do not round-trip to the official CNY amount:",
                file=sys.stderr,
            )
            for model, var, cny in suspicious[:10]:
                print(f"  {model}: {var} -> ¥{cny:.6f}", file=sys.stderr)
        else:
            print(f"  closed-loop check: all {count} models OK")


if __name__ == "__main__":
    main()
