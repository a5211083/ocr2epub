#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
renumber_footnotes.py — OCR 文本整理工具：去页眉 + 页末注释整理为章末注释

处理流程（与页面分页符配合工作，默认 '\f'）：
  1. _parse_pages          切分页面，去除每页首尾空行
  2. _detect_headers       页首高频行聚类（相似度匹配）→ 识别页眉；连续整数序列 → 识别页码；一并移除
  3. _detect_chapters      正则匹配「第X章/Chapter X/序章/附录…」→ 标记章节边界
  4. _detect_footnotes     从页底向上扫描，按注释标记（[1] ① 1. 等）比例校验分离注释区
  5. _parse_footnotes      解析为 Footnote 对象，处理续行
  6. _group_by_chapters    按章节归集正文与注释
  7. _generate_output      章末统一输出注释，正文内标记按章重新编号

用法：
  python renumber_footnotes.py input.txt -o output.txt            # 交互确认页眉
  python renumber_footnotes.py input.txt -o output.txt --yes      # 全自动
  python renumber_footnotes.py input.txt --renumber none          # 保留原编号
  python renumber_footnotes.py input.txt --chapter-pattern "^\\s*§"  # 自定义章节正则
  python renumber_footnotes.py input.txt --ratio 0.3              # 注释区标记比例阈值
"""

import argparse
import bisect
import functools
import os
import difflib
import re
import sys
import time as _time
from dataclasses import dataclass, field

# Windows GBK 控制台防护：不可编码字符按替换符输出，不抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 标记定义

CIRCLED = {}
for _i, _cp in enumerate(range(0x2460, 0x2474), 1):   CIRCLED[chr(_cp)] = _i   # ①-⑳
for _i, _cp in enumerate(range(0x3251, 0x3260), 21):  CIRCLED[chr(_cp)] = _i   # ㉑-㉟
for _i, _cp in enumerate(range(0x32B1, 0x32C0), 36):  CIRCLED[chr(_cp)] = _i   # ㊱-㊿
for _i, _cp in enumerate(range(0x2776, 0x2780), 1):   CIRCLED[chr(_cp)] = _i   # ❶-❿
for _i, _cp in enumerate(range(0x24EB, 0x24F5), 11):  CIRCLED[chr(_cp)] = _i   # ⓫-⓴
CIRCLED_REV = {}
for _ch, _n in CIRCLED.items():
    CIRCLED_REV.setdefault(_n, _ch)  # ①-⑳ 先插入，优先于 ❶-❿

# v0.9n15 圈码归一：圈码是全流水线唯一的"多字形同义家族"（①-⑳/㉑-㉟/㊱-㉟/
# ❶-❿/⓫-⓴ 五段码位，OCR ㉚→①、㊾→④ 识错实证），对任何正则都防不胜防。
# 入口一处把圈码字符归一为 [N]，下游 match_marker/重编号只面对 bracket 单一形态。
CIRCLED_ALL_RE = re.compile("[" + re.escape("".join(CIRCLED)) + "]")

def norm_circled(text):
    """圈码字符 → [N]。纯字符映射、无判定语义；幂等（[N] 不含圈码字符）。"""
    return CIRCLED_ALL_RE.sub(lambda m: "[%d]" % CIRCLED[m.group(0)], text)

PAGENUM_RE = re.compile(r"^\s*[\[\]【】()\-–—_·.]*\s*(\d{1,4})\s*[\[\]【】()\-–—_·.]*\s*$")
SEP_LINE_RE = re.compile(r"^\s*[-—–_=*~·.]+\s*$")
_FW2HW = str.maketrans("０１２３４５６７８９", "0123456789")   # v0.9d: 全角数字归一半角
# v0.9b 分隔线残片与强注释标记粘连（"_① …" "__[1] …" "——① …"）：剥残片后再认标记。
# 只对 circled/bracket 强家族生效，弱家族（§ 1. (1)）不剥，防误捕。
GLUE_SEP_RE = re.compile(
    r"^[\-—–_=*~·.]+\s*(?=[" + re.escape("".join(CIRCLED)) + r"\[【])")

CHAPTER_MARKER_RE = re.compile(r"^\s*<<<CHAPTER>>>\s*(.*?)\s*$")

CHAPTER_RES = [
    re.compile(r"^\s*第\s*[0-9０-９一二三四五六七八九十百千零两]+\s*[章回卷部篇]"
               r"(?=$|[\s，,、.．:：;；!！?？—-])"),
    re.compile(r"^\s*(?:chapter|ch\.?)\s+[0-9ivxlc]+", re.I),
    re.compile(r"^\s*(序章|序言|前言|引言|导言|尾声|后记|跋|附录)\s*($|[，,、.．:：\s])"),
]

FAMILY_RES = {
    "bracket": re.compile(r"\[\d{1,3}\]|【\d{1,3}】"),
    "paren":   re.compile(r"[（(]\d{1,3}[)）]"),
    "paren2":  re.compile(r"(?<![\d(（])\d{1,3}[)）]"),
    "dot":     re.compile(r"(?<![\d.])(\d{1,3})[.、．]"),
    "numsp":   re.compile(r"(?<![\d.(\[【])(\d{1,3})[ 　](?=\S)"),
    "circled": re.compile("[" + re.escape("".join(CIRCLED)) + "]"),
}


_LOG_LEVEL = "INFO"          # 默认只留结果与异常；--log-level DEBUG 开全量逐调用追踪
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}



# ---------------------------------------------------------------- 语言档案
# 注释块标题等文案按语言取自 toc_languages.json（--lang-file 可换路径；
# 语言代码来源：--lang > <input>.lang sidecar > 内置中文默认）。
LANG_FILE_DEFAULT = "toc_languages.json"
LANG_DEFAULTS = {
    "label": "中文",
    "toc_headings": ["目录", "目次", "目錄"],
    "notes_title": "注释",
    "sent_end": "。！？；：…”』」】)",
    "reflow_min_len": 25,
    "heading_max_len": 25,
    "heading_punct": "。，、；：",
}


def _load_lang_profile(lang_code, lang_file=None):
    """v0.10：默认值唯一真相源 = toc_languages.json 的 zh 块；LANG_DEFAULTS
    仅在档案缺失/损坏时兜底。各语言块可只写覆盖键。JSON 路径下缺必需键即
    报错退出——不要沉默的错误默认值。"""
    import json as _json
    code = lang_code or "zh"
    path = lang_file or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     LANG_FILE_DEFAULT)
    try:
        raw = _json.load(open(path, encoding="utf-8"))
        zh_base = raw.get("zh") if isinstance(raw.get("zh"), dict) else None
    except Exception as _e:
        _log("WARN", "LANG", f"语言档案读取失败（{_e}），回退内置默认")
        zh_base = None
    if zh_base is None:
        return dict(LANG_DEFAULTS)
    prof = dict(zh_base)
    if code != "zh":
        blk = raw.get(code)
        if isinstance(blk, dict):
            prof.update(blk)
        else:
            _log("WARN", "LANG", f"语言档案中无 [{code}] 块，按 zh 档案执行")
    _required = ("toc_headings", "notes_title", "sent_end",
                 "reflow_min_len", "heading_max_len", "heading_punct")
    _missing = [k for k in _required if k not in prof]
    if _missing:
        _log("ERROR", "LANG",
             f"语言档案 [{code}] 缺键 {_missing}，请到 {path} 补全（参考 zh 块）")
        raise SystemExit(2)
    return prof


def _log(level, tag, msg):
    """带时间戳+级别的统一输出；低于 _LOG_LEVEL 的丢弃（默认 INFO，DEBUG 是逐调用全量）。"""
    if _LEVELS.get(level, 20) < _LEVELS.get(_LOG_LEVEL, 20):
        return
    print(f"[{_time.strftime('%H:%M:%S')}][{level:<5}][{tag:<4}] {msg}")



_FN_DEPTH = [0]
_FN_COUNT = {}


def _traced(f=None, *, hot=False):
    """函数级日志：进/出均署名函数名。hot=True 采样（第1次+每2000次），退出报总调用数。"""
    def deco(func):
        nm = func.__name__
        @functools.wraps(func)
        def w(*a, **k):
            _FN_COUNT[nm] = _FN_COUNT.get(nm, 0) + 1
            c = _FN_COUNT[nm]
            ind = "  " * _FN_DEPTH[0]
            show = (_LOG_LEVEL == "DEBUG") and ((not hot) or c == 1 or c % 2000 == 0)
            if show:
                _log("INFO", "FN  ", f"{ind}->{nm}#{c} args={repr(a)[:60]}")
            _FN_DEPTH[0] += 1
            try:
                r = func(*a, **k)
                _FN_DEPTH[0] -= 1
                if show:
                    _log("INFO", "FN  ", f"{ind}<-{nm}#{c} ret={repr(r)[:60]}")
                return r
            except Exception as e:
                _FN_DEPTH[0] -= 1
                _log("ERROR", "FN  ", f"{ind}!!{nm}#{c} raise {type(e).__name__}: {e}")
                raise
        w.__name__ = nm
        return w
    return deco(f) if f else deco


@_traced(hot=True)
def fmt_marker(style, n):
    if style == "bracket":
        return f"[{n}]"
    if style == "numsp":
        return f"[{n}]"          # v0.9n17 "N "注释归一显示与 bracket 一致
    if style == "paren":
        return f"({n})"
    if style == "paren2":
        return f"{n})"
    if style == "dot":
        return f"{n}."
    if style == "circled":
        return CIRCLED_REV.get(n, f"[{n}]")
    return None  # symbol 不支持重编号


@_traced(hot=True)
def match_marker(s):
    """行首注释标记匹配，返回 (style, num, raw, end) 或 None。"""
    # v0.9n14: 括号须同族配对（[1】 混配不再互认）；全角［］+全角数字照旧
    m = re.match(r"^\s*(?:\[([0-9０-９]{1,3})\]|【([0-9０-９]{1,3})】|［([0-9０-９]{1,3})］)", s)
    if m:
        _g = m.group(1) or m.group(2) or m.group(3)
        return ("bracket", int(_g.translate(_FW2HW)), m.group(0).strip(), m.end())
    m = re.match(r"^\s*[（(]([0-9０-９]{1,3})[)）]", s)
    if m:
        return ("paren", int(m.group(1).translate(_FW2HW)), m.group(0).strip(), m.end())
    stripped = s.lstrip()
    if stripped and stripped[0] in CIRCLED:
        return ("circled", CIRCLED[stripped[0]], stripped[0], len(s) - len(stripped) + 1)
    m = re.match(r"^\s*([*†‡§¶])\s*", s)
    if m:
        return ("symbol", None, m.group(1), m.end())
    m = re.match(r"^\s*(\d{1,3})[.、．]", s)
    if m:
        return ("dot", int(m.group(1)), m.group(0).strip(), m.end())
    # v0.9n17 "N 文本"注释行（数字+空格）：《导读拉康》章末注释原生版式。
    # 页码行"3"无尾随空格不匹配；正文"20 世纪"由重编号门控挡（见 _renumber_chapter）
    m = re.match(r"^\s*(\d{1,3})[ 　]", s)
    if m:
        return ("numsp", int(m.group(1)), m.group(0).strip(), m.end())
    m = re.match(r"^\s*(\d{1,3})[)）]", s)
    if m:
        return ("paren2", int(m.group(1)), m.group(0).strip(), m.end())
    return None


@_traced(hot=True)
def normalize(line):
    s = re.sub(r"\s+", "", line)
    s = re.sub(r"[-_·•.…]+", "", s)
    return s.lower()


@_traced(hot=True)
def _cert_hit_forms(t, forms):
    """已归一化行形态与证书集合比对：本体 + 前后各剥 1-2 字符的变体全等命中，
    吸收 OCR 残字（"S Title"前挂、"Title 2S"后挂、"页 S"尾字母）。
    只剥 1-2 个字符，剥后不足 2 字不算，防止吞掉正常拉丁词尾造成误删。"""
    for c in (t, t[1:], t[2:], t[:-1], t[:-2]):
        if len(c) >= 2 and c in forms:
            return True
    return False


def normalize_header(line):
    """页眉归一化：在 normalize 基础上剥掉行首/行尾数字，使「书名    3」、
    「003 德国古典美学」这类页眉页码同行的行能跨页聚类。"""
    s = re.sub(r"[0-9０-９]+$", "", normalize(line))
    return re.sub(r"^[0-9０-９]+", "", s)


# ---------------------------------------------------------------- 数据模型

@dataclass
class Footnote:
    style: str
    num: int | None
    raw: str
    text: str
    page_idx: int
    new_num: int | None = None


@dataclass
class Page:
    idx: int
    lines: list
    body_lines: list = field(default_factory=list)
    footnotes: list = field(default_factory=list)


@dataclass
class Chapter:
    start_page: int
    title: str | None
    body: str = ""
    footnotes: list = field(default_factory=list)
    toc_chunks: list = field(default_factory=list)  # v0.10.3：目录页在本章装配序列中的块序号，reflow 跳过  # v0.10：目录页逐行原文；挂章首输出，reflow/重编号均不触碰


def _load_folio_edge_forms():
    """形态字典外置加载（folio_edge_forms.json，与本脚本同目录）。
    文件缺失/损坏/无条目时回退内置默认并 WARN——字典是维护点，不是单点故障源。"""
    import json as _json
    _default = [
        {"name": "midnum", "strip_line": True, "re": r"^\s*(\d{1,3})[ 　]+(\S.*\S)[ 　]+(\d{1,4})\s*$", "v": 3, "rest": (1, 2)},
        {"name": "suffix_glued", "strip_line": True, "re": r"^(.*\D)[ 　]*(\d{1,4})\s*$", "v": 2, "rest": (1,)},
        {"name": "prefix", "strip_line": True, "re": r"^\s*(\d{1,4})[ 　]+(\S.*)$", "v": 1, "rest": (2,)},
        {"name": "suffix", "strip_line": True, "re": r"^(.*\S)[ 　]+(\d{1,4})\s*$", "v": 2, "rest": (1,)},
    ]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "folio_edge_forms.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = _json.load(fh)
        forms = []
        for e in data.get("forms", []):
            forms.append({"name": e["name"], "re": re.compile(e["pattern"]),
                          "v": int(e["v"]), "rest": tuple(e["rest"]),
                          "strip_line": bool(e.get("strip_line", False))})
        if forms:
            return forms
        _log("WARN", "PAG ", f"形态字典 {path} 无条目，回退内置默认")
    except FileNotFoundError:
        pass
    except Exception as _e:
        _log("WARN", "PAG ", f"形态字典加载失败（{type(_e).__name__}: {_e}），回退内置默认")
    return [{"name": d["name"], "re": re.compile(d["re"]), "v": d["v"], "rest": d["rest"]}
            for d in _default]


FOLIO_EDGE_FORMS = _load_folio_edge_forms()


# ---------------------------------------------------------------- 1. 切页

@_traced
def _parse_pages(text, separator):
    pages = text.split("\f") if separator is None else re.compile(separator, re.M).split(text)
    out = []
    for i, p in enumerate(pages):
        lines = p.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        out.append(Page(idx=i, lines=lines))
    return out


# ---------------------------------------------------------------- 2. 页眉/页码

@_traced(hot=True)
def _top_region_lines(page, n):
    """页首+页尾各 n 个非空行（页眉候选；v0.9g 起含页底页眉块，
    如 '西方文论经典·第六卷 / 后现代与文化研究 / 008' 三行式页脚）"""
    ne = [(i, l) for i, l in enumerate(page.lines) if l.strip()]
    picked = {}
    for i, l in ne[:n] + ne[-n:]:
        picked[i] = l
    return sorted(picked.items())


@_traced
def _scan_pagenum_sequence(pages, top_n):
    """与 _detect_headers 原内联块完全相同的偏移扫描，抽出共用。
    返回 (最佳覆盖率, 偏移, 命中页索引列表)。"""
    n_pages = len(pages)
    per_page = []
    for pg in pages:
        vals = []
        for _, line in _top_region_lines(pg, top_n):
            m = PAGENUM_RE.match(line)
            if m:
                vals.append(int(m.group(1)))
        per_page.append(vals)
    best = (0.0, 0, [])
    for offset in range(-30, 11):
        hits = [i for i, vals in enumerate(per_page) if (i + 1 + offset) in vals]
        cov = len(hits) / n_pages
        if cov > best[0]:
            best = (cov, offset, hits)
    return best


@_traced
def _detect_pagenum_seqorder(pages, top_n=3, min_chain=8):
    """文档顺序兜底（独立数字流，v0.9n5 位置分类版）：整行纯数字按页内位置分类——
    页首带（前 top_n 个非空行，归页首流链，禁止混池）、页脚带（后 top_n 个非空行）、
    页中。页脚流与页中页边码流数值相互错开（目录 20 页即差 ±20），混池单链会被
    流间值回落每页打碎；必须各流独立 LIS（类内最长严格递增链，len >= min_chain
    即有效），命中行由 _remove_headers 按 line_hits 逐行精确删除。"""
    v_max = max(1000, 2 * len(pages))
    classes = {"foot": [], "mid": []}
    for pg in pages:
        ne = [i for i, l in enumerate(pg.lines) if l.strip()]
        # 独立数字只按页脚带/页中带分类；页首带不排除——附着式页眉数字
        # （"10 导读拉康"）永不进本池，排除只会误杀全注释短页的页脚数字
        foot_band = set(ne[-top_n:]) if len(ne) > top_n else set(ne)
        for i, line in enumerate(pg.lines):
            s = line.strip()
            if not re.match(r"^\d{1,4}$", s):
                continue
            v = int(s)
            if not (1 <= v <= v_max):
                continue
            classes["foot" if i in foot_band else "mid"].append((pg.idx, i, v))
    def _lis(cands):
        tails, pos_of, prev = [], [], [-1] * len(cands)
        for j, (_, _, v) in enumerate(cands):
            t = bisect.bisect_left(tails, v)
            if t == len(tails):
                tails.append(v)
                pos_of.append(j)
            else:
                tails[t] = v
                pos_of[t] = j
            prev[j] = pos_of[t - 1] if t else -1
        chain = []
        j = pos_of[-1] if pos_of else -1
        while j >= 0:
            chain.append(cands[j])
            j = prev[j]
        chain.reverse()
        return chain

    hits = []
    for cname, cands in classes.items():
        if len(cands) < min_chain:
            continue
        chain = _lis(cands)
        if len(chain) >= min_chain:
            hits.extend(chain)
        else:
            _log("WARN", "PAG ", f"独立数字流[{cname}]拒判：链 {len(chain)}/{len(cands)}")
    if len(hits) < min_chain:
        # v0.9n17b 跨流并池：短流（页内非空行 ≤ top_n 时整页归入 foot，眉下
        # 页码被撕进 foot 池）把长流打碎成 < min_chain 两截，逐流判永拒
        # （导读拉康重跑漂移实证：mid 7 + foot 1 = 8 眉码全漏、k1 残留纯页码
        # 页、k2 才删净）。并池只在合成链为无间断连续整数时采纳——真页码流
        # 必连续；章题"3"+页脚流 5..12 这类跨流拼接留有空档，仍拒。
        _pool = sorted(classes["foot"] + classes["mid"])
        _chain = _lis(_pool)
        if len(_chain) >= min_chain \
                and all(b[2] == a[2] + 1 for a, b in zip(_chain, _chain[1:])):
            hits = _chain
            _log("WARN", "PAG ", f"独立数字流跨流并池采纳：连续链 {len(_chain)} 条")
        else:
            _log("WARN", "PAG ", "独立数字流拒判：foot/mid 两流均无有效链")
            return None
    total = sum(len(c) for c in classes.values())
    return {"coverage": len(hits) / max(1, total), "offset": None, "pages": set(),
            "line_hits": {(pp, li) for pp, li, _ in hits}, "chain": hits}


@_traced
def _detect_pagenum_relaxed(pages, top_n, min_cov=0.30, min_run=5):
    """严格序列未检出的兜底：放宽覆盖率，但要求最长连续命中 >= min_run 页。
    格言序号按章重启，凑不出连续 N 页"数值 == 页索引"的段，误删面被压掉。"""
    cov, offset, hits = _scan_pagenum_sequence(pages, top_n)
    if not hits or cov < min_cov:
        return None
    run = longest = 1
    for a, b in zip(hits, hits[1:]):
        run = run + 1 if b == a + 1 else 1
        longest = max(longest, run)
    if longest < min_run:
        return None
    return {"coverage": cov, "offset": offset, "pages": set(hits)}


@_traced
def _detect_headers(pages, top_n, threshold):
    """返回 (headers候选列表, pagenum候选 or None)。

    v0.9n8：奇偶双模板按 parity 分别计覆盖率。左页"009 大标题"与右页"小标题
    010"归一化形态不同，各覆盖约 50%，整体验收双双低于阈值会漏检（奇偶双模
    板书页眉完全删不掉的实装案例）。现单模板在奇页或偶页任一侧覆盖率达标
    且绝对命中 >=4 即确认（小样本防误捕：目录区同词条目重复两次偶数页，
    2/3 页的虚高覆盖率不得放行），删除按形态匹配、与奇偶无关。"""
    n_pages = len(pages)
    raw = []
    for pg in pages:
        for _, line in _top_region_lines(pg, top_n):
            # v0.9n13: 含注释标记的行不进入页眉聚类——跨页正文条文
            # （"正文第N页…①"）只差行内一个数字，相似度轻松过 0.88 阈值，
            # 会被当页眉整行删除（评审实测：3 页正文连同圈码被删光）。
            # 真页眉永远不会带注释标记，排除零代价。
            if (any(ch in CIRCLED for ch in line)
                    or FAMILY_RES["bracket"].search(line)
                    or FAMILY_RES["paren"].search(line)
                    or FAMILY_RES["paren2"].search(line)):
                continue
            form = normalize_header(line)
            if len(form) >= 2:  # 纯页码行剥光后过短，交给页码序列逻辑处理
                raw.append((form, line, pg.idx))

    clusters = []
    for form, line, pidx in raw:
        for c in clusters:
            _hit = form in c["forms"]
            if not _hit:   # v0.9b quick_ratio 上界预筛，防 O(n²) 卡死
                _sm = difflib.SequenceMatcher(None, form, c["repr"])
                _hit = _sm.quick_ratio() >= 0.88 and _sm.ratio() >= 0.88
            if _hit:
                c["forms"].add(form)
                c["count"] += 1
                c["lines"].append(line)
                c["pidxs"].append(pidx)
                break
        else:
            clusters.append({"repr": form, "forms": {form}, "count": 1,
                             "lines": [line], "pidxs": [pidx]})

    n_odd = sum(1 for pg in pages if pg.idx % 2 == 1)
    n_even = n_pages - n_odd
    headers = []
    for c in clusters:
        _pidxs = set(c["pidxs"])      # v0.9n14: 同一页页首带+页脚带各贡献一次，按页去重
        cov = len(_pidxs) / n_pages
        odd_hits = sum(1 for i in _pidxs if i % 2 == 1)
        cov_odd = odd_hits / max(1, n_odd)
        cov_even = (c["count"] - odd_hits) / max(1, n_even)
        # v0.9n14：整体验收 或 奇/偶单侧验收，统一要求绝对命中 >=4——
        # 防小样本误捕（目录同词条目重复 2-3 偶数页、3 页书同条文被当页眉
        # 删光，评审实测）。真实书页眉重复几十次，>=4 不影响正常场景。
        if max(cov, cov_odd, cov_even) >= threshold and c["count"] >= 4:
            headers.append({
                "text": max(set(c["lines"]), key=c["lines"].count).strip(),
                "coverage": cov, "count": c["count"], "forms": c["forms"],
                "cov_odd": cov_odd, "cov_even": cov_even,
            })
    headers.sort(key=lambda h: -max(h["coverage"], h["cov_odd"], h["cov_even"]))
    # 连续页码：页索引 + 固定偏移的整数序列
    best = _scan_pagenum_sequence(pages, top_n)
    pagenum = None
    if best[0] >= threshold:
        pagenum = {"coverage": best[0], "offset": best[1], "pages": set(best[2])}
    return headers, pagenum


@_traced
def _remove_headers(pages, headers, pagenum, top_n):
    forms = set()
    for h in headers:
        forms |= h["forms"]
    removed = 0
    for pg in pages:
        ne_idx = [i for i, l in enumerate(pg.body_lines) if l.strip()]
        # v0.9g：页首前 top_n 与页尾后 top_n 的非空行都算页眉区，页底页眉块在此删除
        edge = set(ne_idx[:top_n]) | set(ne_idx[-top_n:])
        keep = []
        for i, line in enumerate(pg.body_lines):
            if i in edge:
                _nh = normalize_header(line)
                # v0.9n9：OCR 残字容忍——"2 象征界 S"剥数字后是"象征界s"、
                # "S Geschehen als Problem"前挂残字，短剥变体比对簇形态
                if _cert_hit_forms(_nh, forms):
                    removed += 1
                    continue
                if pagenum and pg.idx in pagenum["pages"]:
                    m = PAGENUM_RE.match(line)
                    if m and int(m.group(1)) == pg.idx + 1 + pagenum["offset"]:
                        removed += 1
                        continue
                if pagenum and "line_hits" in pagenum and (pg.idx, i) in pagenum["line_hits"]:
                    removed += 1
                    continue
            keep.append(line)
        pg.lines = keep
        pg.body_lines = keep   # v0.9c 审计#3: 输出读的是 body_lines，必须同步
    return removed

@_traced
def _detect_topstream_chain(pages, top_n, min_chain=8, min_cov=0.55):
    """附着式页码-眉行流链（v0.9n8 奇偶 × 页首/页脚四流版）。

    两个已知的广泛版式，v0.9n6 奇偶二分单链接不住：
      1) 奇偶页页眉两套模板（左页"009 大标题"、右页"小标题 010"）——奇偶
         分流各验各链解决；
      2) 双重页码且不同步（页眉标正文序 5、页脚标全书序 25）——每页两枚
         附着数字，单链 LIS 被两流交替值打碎、覆盖率不足整体拒判，页眉
         页脚全留。本版按 页奇偶 × 行位置（页首带/页脚带）分四条独立链各
         验各的 LIS，页脚附着码不再污染页首链。返回 {(奇偶, 位置带): chain}。"""
    v_max = max(1000, 2 * len(pages))
    classes = {}
    for pg in pages:
        ne = [i for i, l in enumerate(pg.lines) if l.strip()]
        foot_band = set(ne[-top_n:]) if len(ne) > top_n else set(ne)
        for i, line in _top_region_lines(pg, top_n):
            s = line.strip()
            if "/" in s:          # 目录条目（"1 想象界/25"），永不可能是页眉行
                continue
            band = "foot" if i in foot_band else "top"
            for f in FOLIO_EDGE_FORMS:
                m = re.match(f["re"], s)
                if not m:
                    continue
                v = int(m.group(f["v"]))
                if 1 <= v <= v_max:
                    _rest = " ".join(m.group(g) for g in f["rest"]).strip()
                    _rf = re.sub(r"[\s？?！!。；，、：:．.]+$", "", normalize_header(_rest))
                    classes.setdefault((pg.idx % 2, band), []).append((pg.idx, i, v, _rf))
                break
    out = {}
    stream_forms = {}   # v0.9n11: 链成员剩余形态证书（含被拒流，覆盖率不再一票否决）
    for (parity, band), cands in classes.items():
        if len(cands) < min_chain:
            continue
        tails, pos_of, prev = [], [], [-1] * len(cands)
        for j, (_, _, v, _rf0) in enumerate(cands):
            t = bisect.bisect_left(tails, v)
            if t == len(tails):
                tails.append(v)
                pos_of.append(j)
            else:
                tails[t] = v
                pos_of[t] = j
            prev[j] = pos_of[t - 1] if t else -1
        chain = []
        j = pos_of[-1]
        while j >= 0:
            chain.append(cands[j])
            j = prev[j]
        chain.reverse()
        if len(chain) >= min_chain:   # v0.9n11: 链≥8 即出形态证书，覆盖率低只影响闸2不放行
            for _, _, _, _rf in chain:
                if _rf:
                    stream_forms[_rf] = stream_forms.get(_rf, 0) + 1
        chain_pages = {pp for pp, _, _, _ in chain}
        cand_pages = {pp for pp, _, _, _ in cands}
        if len(chain) >= min_chain and len(chain_pages) >= min_cov * max(1, len(cand_pages)):
            out[(parity, band)] = chain
        else:
            _log("WARN", "PAG ", f"附着页码流[{'奇' if parity else '偶'}-"
                              f"{'页脚' if band == 'foot' else '页首'}]拒判："
                              f"链 {len(chain)}，覆盖页 {len(chain_pages)}/{len(cand_pages)}")
    return (out, stream_forms)


@_traced
def _strip_folio_tokens(pages, chains_list, top_n, min_form=4, stream_forms=None):
    """附着式页码-眉行处理（三闸 + strip_line，v0.9n2；v0.9n8 大改）：
      闸1 位置：行位于 \f 页的边缘区（页首/页尾各 top_n 个非空行）；
      闸2 链成员：被剥数字值属于任一流链（逐链判定成员或同链前后值夹逼，
          页眉流与页脚流互不挤占——双重页码下两流各认各链）；
      闸3 剩余形态：剥掉数字后剩余串的归一化形态在全书边缘行出现 >= min_form
          次，且不含 "/"。v0.9n8 修复：形态表键此前用含数字的整行归一化做键，
          与剥数字后的剩余形态永远比不中（每页数字不同→键各不同→频次永远
          为 1），闸3 从未放行过；现形态表键与 normalize_header 同规则剥数字。
    三闸全过后按字典条目的 strip_line 处理：
      strip_line=true 且剩余串无句读尾（页眉形态）→ 整行删除，并连带删除同页
      页首区内、位于该行上方、无数字、归一化 <=30 字、页首高频（>= min_form）
      的伴随行（多行页眉块的另一行，如"西方文论经典·第五卷"）；
      否则 → 剥数字保留剩余文本。
    页内序号（闸1）、目录（闸2/"/"守卫）、章标题（无链上数字）、注释行（闸3）
    全部被挡。v0.9k 全 body 无闸版之死见规范变更日志第 5 案。"""
    by_page = {}
    for chain in chains_list:
        for pp, _, v, _rf in chain:
            by_page.setdefault(pp, []).append(v)
    page_vals = sorted(by_page)
    chain_maps = []
    for chain in chains_list:
        d = {}
        for pp, _, v, _rf in chain:
            d.setdefault(pp, []).append(v)
        chain_maps.append((sorted(d), d))

    def _bracket(pidx, v):
        # v 是某页某链成员 → 放行；否则被同一条链的前后页值夹逼才放行。
        # 逐链独立判定：双重页码两流共存时，页眉小值不会被页脚大值的
        # 邻页夹逼挤死（v0.9n8 前 by_page 全流混合取 max/min 的洞）。
        for cvals, cd in chain_maps:
            if v in cd.get(pidx, []):
                return True
            prevs = [pv for pv in cvals if pv < pidx]
            nexts = [pv for pv in cvals if pv > pidx]
            if not prevs or not nexts:
                continue
            if max(max(cd[pv]) for pv in prevs) < v < min(min(cd[pv]) for pv in nexts):
                return True
        return False

    form_counts, top_form_counts = {}, {}
    for pg in pages:
        for _, line in _top_region_lines(pg, top_n):
            # v0.9n8：键必须与闸3 的剩余形态同规则（normalize_header 剥数字），
            # 否则含页码的页眉行每页键不同、频次永远攒不够 min_form
            f = re.sub(r"[\s？?！!。；，、：:．.]+$", "", normalize_header(line))
            if len(f) >= 2:
                form_counts[f] = form_counts.get(f, 0) + 1
            t = normalize(line)
            if len(t) >= 2:
                top_form_counts[t] = top_form_counts.get(t, 0) + 1

    def _head_form(s):
        return re.sub(r"[\s？?！!。；，、：:．.]+$", "", normalize(s))

    rem_forms = set()
    stripped, deleted, samples = 0, 0, []
    _SENT_TAIL = tuple("。！？；…”—』」)）")
    for pg in pages:
        edge = dict(_top_region_lines(pg, top_n))
        del_idx = set()
        for i in sorted(edge):
            s = pg.body_lines[i].strip()
            cands = []
            for f in FOLIO_EDGE_FORMS:
                m = re.match(f["re"], s)
                if not m:
                    continue
                rest = " ".join(m.group(g) for g in f["rest"]).strip()
                cands.append((f, int(m.group(f["v"])), rest))
                break
            for f, v, rest in cands:
                if not rest or "/" in rest:
                    continue
                if not _bracket(pg.idx, v):
                    continue
                # v0.9n12: rf 与形态表/清扫同规则（normalize_header 剥数字）——
                # "1 想象界 27"三段式剩余形态为"1 想象界"，旧版 normalize 剥不掉
                # 行首章号，与形态表键"想象界"比不中，闸3 整体拒收（lacan 实案）
                rf = re.sub(r"[\s？?！!。；，、：:．.]+$", "", normalize_header(rest))
                if len(rf) < 2 or form_counts.get(rf, 0) < min_form:
                    continue
                rem_forms.add(rf)                 # 剩余形态并入证书（裸眉行凭此清扫）
                # 句读尾守卫已撤：闸3 形态频次已担保页眉属性，"？"-结尾的眉
                # （"为什么是拉康？"全角变体）此前被误判为句尾走保留分支留壳
                if f.get("strip_line"):
                    del_idx.add(i)
                    deleted += 1
                # v0.9n29d 引文守卫：页码后黏章题引文、其后仍有实质正文
                # （rest >= 8 字）时，不得连带剥引文（法西斯主义书实证：
                # "52七、“民主的危机”之前提到的没有…"被剥成"之前…"）。
                # 只剥行首数字、保留全文。
                    for j in sorted(edge):
                        if j >= i:
                            break
                        s2 = pg.body_lines[j].strip()
                        if re.search(r"\d", s2):
                            continue
                        t2 = normalize(s2)
                        if len(t2) <= 30 and len(t2) >= 2 \
                                and top_form_counts.get(t2, 0) >= min_form:
                            del_idx.add(j)
                            rem_forms.add(_head_form(s2))   # 伴随行形态并入证书
                    if len(samples) < 60:   # v0.9n13: 样例上限，防日志/内存膨胀
                        samples.append((pg.idx + 1, s, "〔整行删除〕"))
                elif len(rest.strip()) >= 8:
                    pg.body_lines[i] = re.sub(r"^\s*\d{1,3}\s*", "", s).strip()
                    stripped += 1
                    if len(samples) < 60:
                        samples.append((pg.idx + 1, s, "〔引文守卫：仅剥数字〕"))
                else:
                    pg.body_lines[i] = rest.strip()
                    stripped += 1
                    if len(samples) < 60:
                        samples.append((pg.idx + 1, s, rest.strip()))
                break
        if del_idx:
            pg.body_lines = [l for k, l in enumerate(pg.body_lines) if k not in del_idx]
            pg.lines = pg.body_lines
    # 二段：眉行清扫（v0.9n9 扩到含数字行）。两类残留都凭"剩余形态证书"删除：
    #  a) 无数字裸眉行（OCR 丢页码："为什么是拉康？"）；
    #  b) 含数字但三闸没放行的页眉行——同文本页眉在别的页已通过闸（证书即来自
    #     这些页的剩余形态），本页却因 OCR 数字损坏进不了候选、或链断档夹逼
    #     失败而漏网（"Geschehen als Problem 5"、"1 想象界 27"实证）。
    # 比对用 normalize_header（剥首尾数字），证书外含数字行一律不动；
    # 章起点标题行被删无害：章节标题由 _detect_chapters 捕获并经
    # <<<CHAPTER>>> 单独输出。
    # v0.9n11: 流链形态证书并入——链≥8 成员的去数字形态直接获证书，
    # 损坏页/被拒流覆盖的页眉行凭证书在清扫阶段删除（奇数页残留的根治）
    if stream_forms:
        for _f, _c in stream_forms.items():
            if _c >= min_form:
                rem_forms.add(_f)
    if by_page:
        first_pg = min(by_page) - 4
        last_pg = max(by_page) + 4
    else:
        first_pg, last_pg = 0, len(pages) - 1
    swept = 0
    for pg in pages:
        if not (first_pg <= pg.idx <= last_pg):
            continue
        idxs = [i for i, _ in _top_region_lines(pg, top_n)]
        delset = set()
        for i in idxs:
            line = pg.body_lines[i]
            if not re.search(r"\d", line):
                if _cert_hit_forms(_head_form(line), rem_forms):
                    delset.add(i)
            else:
                _nh = re.sub(r"[\s？?！!。；，、：:．.]+$", "", normalize_header(line))
                if _nh and _cert_hit_forms(_nh, rem_forms):
                    delset.add(i)
        if delset:
            pg.body_lines = [l for k, l in enumerate(pg.body_lines) if k not in delset]
            pg.lines = pg.body_lines
            swept += len(delset)
    return stripped, deleted + swept, samples


# ---------------------------------------------------------------- 3. 章节

def _detect_chapters(pages, extra_pattern=None):
    """识别章节起点：优先 toc_mark_chapters.py 注入的 <<<CHAPTER>>> 标记，
    其次内置正则（第X章/Chapter X/序章/附录…）与 --chapter-pattern。"""
    res = list(CHAPTER_RES)
    if extra_pattern:
        res.append(re.compile(extra_pattern))
    toc_leader = re.compile(r"[.·•‥․…⋯]{2,}")  # 目录点线：含 …… 的行不是章节标题
    starts = []
    # 整页扫描注入标记（不只前 5 行）：章标题可能位于页首多行之后，
    # 漏识别会导致 <<<CHAPTER>>> 原文漏进 final.txt。机器注入的标记
    # 不会天然出现在书中，整页扫描安全。
    for pg in pages:
        for _li, line in enumerate(pg.lines):
            m = CHAPTER_MARKER_RE.match(line.strip())
            if m:
                starts.append((pg.idx, _li, m.group(1) or "（章节）"))
    if starts:
        starts.sort(key=lambda x: (x[0], x[1]))
        starts = [(pp, tt) for pp, _, tt in starts]
        _log("INFO", "CHAP", f"注入标记 {len(starts)} 个（同页多标记已全收）")
        return starts  # 有注入标记时只信标记；同页篇名+文章题不再互吞
    for pg in pages:
        for line in pg.lines[:5]:
            s = line.strip()
            m = CHAPTER_MARKER_RE.match(s)
            if m:
                starts.append((pg.idx, m.group(1) or "（章节）"))
                break
            if len(s) > 40:
                continue
            if any(r.match(s) for r in res):
                starts.append((pg.idx, s))
                break
    return starts


# ---------------------------------------------------------------- 4/5. 注释区检测与解析

# ---------------------------------------------------------------- v0.10 目录页豁免
# 目录页每条 "1. Geschehen…3" 都命中 dot 家族，页底注释区扫描会整页吞掉；
# 被吞条目随后走章末归组+重编号，目录区错号与 notes_title 混入皆源于此。
# 判据 = 点线密度（结构），不含任何语言词形；页首词命中档案 toc_headings 仅作
# 辅助信号。sidecar（toc_mark_chapters 落盘）优先，本检测只做兜底。
_TOC_ENTRY_LINE_RE = re.compile(
    r"^\s*(?:[IVXLC]{1,8}|\d{1,3})?[.、．]?\s*\S.{2,}?[.·•‥․…⋯]{2,}\s*"
    r"(?:\d{1,4}|[IVXLC]{1,6})\s*$")

def _detect_toc_pages(pages, lang_profile=None, first_chapter_page=None):
    """目录页 idx 集合（密度兜底；sidecar 存在时由 _load_toc_pages 优先使用）。
    first_chapter_page：第一个章界页 idx；目录在结构上只可能位于其前的篇首区，
    正文页一律不豁免（防参考文献/年代表等行尾多数字的页被误判）。"""
    import unicodedata as _ud
    toc = set()
    heads = set()
    if lang_profile:
        for _h in lang_profile.get("toc_headings", []):
            _hh = _ud.normalize("NFD", _h)
            heads.add("".join(c for c in _hh if _ud.category(c) != "Mn").lower())
    for pg in pages:
        if first_chapter_page is not None and pg.idx >= first_chapter_page:
            break
        _ls = [l.strip() for l in pg.lines if l.strip()]
        if not _ls:
            continue
        _ent = sum(1 for l in _ls if _TOC_ENTRY_LINE_RE.match(l))
        if _ent >= 5 or _ent / len(_ls) >= 0.3:
            toc.add(pg.idx)
            continue
        _f = _ud.normalize("NFD", _ls[0])
        _f = "".join(c for c in _f if _ud.category(c) != "Mn").lower()
        if heads and any(_h in _f for _h in heads):
            toc.add(pg.idx)
    if toc:
        _log("INFO", "TOC ", f"目录页豁免(密度检测) {len(toc)} 页："
                            f"{sorted(toc)[:15]}{'…' if len(toc) > 15 else ''}")
    return toc

def _load_toc_pages(args, pages, lang_profile=None, first_chapter_page=None):
    """目录页集合：sidecar 优先，密度检测兜底。"""
    _stem = os.path.splitext(args.input)[0]
    _cands = [_stem + ".tocpages.json",                                   # 同名（无中间步骤时）
              _stem.replace(".prepped", ".marked") + ".tocpages.json",    # batch: marked→prepped 改名
              re.sub(r"\.(prepped|marked)$", "", _stem) + ".tocpages.json"]
    for _side in dict.fromkeys(_cands):
        if os.path.exists(_side):
            try:
                import json as _json
                _toc = set(_json.load(open(_side, encoding="utf-8")))
                _log("INFO", "TOC ", f"目录页豁免(sidecar) {len(_toc)} 页 ← {_side}")
                return _toc
            except Exception as _e:
                _log("WARN", "TOC ", f"sidecar 读取失败({_e})，改密度检测")
                break
    return _detect_toc_pages(pages, lang_profile, first_chapter_page)


@_traced
def _detect_footnote_zone(lines, ratio, style_lock=None, skip_pred=None):
    """从页底向上定位注释区，返回起始行号 k（lines[k:] 为注释区）。

    style_lock：锁定标记样式（如 "circled"）。锁定后只有该样式的行
    才被当作注释标记，其它样式（含 §）一律按正文处理。

    v0.9n26 skip_pred：页底扫描遇非标记行时的豁免判定。仅当该行
      (a) 位于页底最后两个非空行之内、(b) 命中已知章/部题标题、
      (c) 其下再无其它非空行即下方直接是标记锚点（中间只隔空行/页码）
      时豁免并续扫；提取后从页内删除（既不属正文也不属注释，stage4
      页眉删除本也会删它）。
      实证：《德国观念论的终结》注释页页底 OCR 粘连部题行
      "第二部分 …|"（带残字），整页注释 [5]–[9] 因此漏提；
      无界豁免在 Saintsbury 目录页上会逐行越过目录行、把整页吞进
      注释区，故收紧为"仅页底拖尾"。不满足条件的页与 v0.9n24 行为
      完全一致（Saintsbury/lacan/Ekonomia 回归字节一致）。

    自底向上三阶段：
      1. 定位最底部标记行（空行/页码/分隔线跳过；其下的纯文本行=续行）；
      2. 向上扩展：标记行直接计入；遇分隔线即停（硬边界）；纯文本行仅在
         标记占比 >= ratio 时计入；
      3. 剔除区顶误入的正文行（续行只可能跟在标记行之下，区顶纯行必是正文）。
    防护：区內已有 ①/bracket/symbol 类标记时，dot/paren 样式的"标记行"
    按正文处理，防止 '4.对于善……' 这类 § 编号行混入注释区被重编号。
    """
    STRONG = ("bracket", "circled")   # [1] ① 互认（同一页可能混排）
    # 弱家族：§ † ‡ ¶ 与 1. (1) 只有在页底锚点也是同类时才认作注释标记，
    # 否则一律按正文处理 —— 否则 '§16.…' 章节标题行会被当成注释标记，
    # 把其下正文整段吞进注释区（康德《美的分析论》OCR 实证踩坑）。

    def _family_ok(style, prefer):
        if style == prefer:
            return True
        return style in STRONG and prefer in STRONG

    n = len(lines)
    i = n - 1
    _skip_below = []             # v0.9n26 页底豁免行（锚点之下），提取后删除
    _skip_trail = 0              # v0.9n26 仅豁免页底最后两个非空行内的标题行
    while i >= 0:
        s = lines[i].strip()
        if not s or PAGENUM_RE.match(s) or SEP_LINE_RE.match(s):
            i -= 1
            continue
        mm = match_marker(s)
        if mm and (style_lock is None or mm[0] == style_lock):
            break
        if mm:
            return n             # 页底是其它样式的标记（锁定模式下不算注释）
        if skip_pred is not None and skip_pred(s) and _skip_trail < 2:
            _skip_trail += 1
            _skip_below.append(i)
            i -= 1
            continue             # v0.9n26 页底拖尾的部题/章题粘连行：豁免并续扫
        return n                 # 页底是正文 → 本页无注释区
    if i < 0:
        return n

    zone_top = i
    prefer = match_marker(lines[i])[0]
    marker_count, nonblank = 1, 1
    for t in lines[i + 1:]:
        st = t.strip()
        if st and not PAGENUM_RE.match(st) and not SEP_LINE_RE.match(st):
            nonblank += 1        # 底部标记行之下 = 该注释的续行

    j = i - 1
    while j >= 0:
        s = lines[j].strip()
        if not s or PAGENUM_RE.match(s):
            j -= 1
            continue
        if SEP_LINE_RE.match(s):
            break                # 分隔线是注释区上边界
        mm = match_marker(s)
        if mm and (style_lock is None or mm[0] == style_lock) \
                and _family_ok(mm[0], prefer):
            marker_count += 1
            nonblank += 1
            zone_top = j
            j -= 1
            continue
        nonblank += 1
        if marker_count / nonblank >= ratio:
            zone_top = j
            j -= 1
            continue
        break

    while zone_top < n:          # 剔除区顶误入的正文
        s = lines[zone_top].strip()
        if not s or PAGENUM_RE.match(s) or SEP_LINE_RE.match(s):
            zone_top += 1
            continue
        mm = match_marker(s)
        if mm and (style_lock is None or mm[0] == style_lock) \
                and _family_ok(mm[0], prefer):
            break
        zone_top += 1
    # v0.9e 弱锚点防护：dot/paren 区顶上方必须有硬边界（分隔线/页码行之上即页首），
    # 否则页底恰好是编号正文段的页会被整段吞成注释区（迪尼亚诺夫案：正文 1/2/5 段被吞）
    if prefer in ("dot", "paren", "paren2", "numsp") and zone_top > 0:
        _j3 = zone_top - 1
        while _j3 >= 0 and (not lines[_j3].strip() or PAGENUM_RE.match(lines[_j3].strip())):
            _j3 -= 1
        if _j3 < 0 or not SEP_LINE_RE.match(lines[_j3].strip()):
            return n
    # v0.9n26 页底豁免行删除：索引均在锚点之下，k=zone_top 不受影响
    for _di in sorted(_skip_below, reverse=True):
        del lines[_di]
    return zone_top

@_traced
def _find_orphan_notes(lines, style_lock=None):
    """救援页中注释：书页边界在 OCR/切页中丢失时，注释行落在页中间、
    页底是正文，主检测会认为"本页无注释"。确认信号（满足其一）：
      1. 上一行是分界线（原书页底的分隔线还在，如 '_' + '①指莱布尼茨派'）；
      2. 该标记的数字在本行上方的正文里出现过（行内引用，如正文
         '（conditio sine qua non①）' 之下紧跟 '① 拉丁文…'）。
    v0.9b：分隔线残片与标记粘连（'_① …' '__[1] …'）先剥残片再识别。
    弱家族符号（§ 1. (1)）不参与，防误捕。返回 [(start, end)] 行区间。
    """
    out = []
    n = len(lines)
    for i, l in enumerate(lines):
        s = l.strip()
        mm = match_marker(s)
        if not mm:
            g = GLUE_SEP_RE.match(s)          # 粘连残片：__① 原文…
            if g:
                mm = match_marker(s[g.end():])
        if not mm or mm[0] in ("dot", "paren", "paren2", "numsp", "symbol"):
            continue
        if style_lock and mm[0] != style_lock:
            continue
        ok = False
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0 and SEP_LINE_RE.match(lines[j].strip()):
            ok = True                      # 信号1：上一行是分界线
        if not ok:
            above = "\n".join(lines[:i])
            res = FAMILY_RES.get(mm[0])
            if res:
                for m in res.finditer(above):
                    if _extract_num(mm[0], m.group(0)) == mm[1]:
                        ok = True          # 信号2：上方正文行内引用同号标记
                        break
        if ok:
            e2 = i + 1                     # 含续行
            while e2 < n and lines[e2].strip() and not match_marker(lines[e2].strip()):
                e2 += 1
            out.append((i, e2))
    return out

@_traced
def _join_lines(parts):
    out = ""
    for p in parts:
        if out and out[-1].isascii() and out[-1].isalnum() and p[:1].isascii() and p[:1].isalnum():
            out += " " + p
        else:
            out += p
    return out


@_traced
def _parse_footnotes(zone_lines, page_idx):
    """把注释区行解析为 Footnote 列表。首行须有标记（经 match_marker），
    其下无标记行并入当前注释的 text（跨页断行/续行）。无标记区首行
    容错为无编号注释（不丢文本）。
    v0.9b：行首分隔线残片与标记粘连（"_① …"）时剥残片再认标记。"""
    notes = []
    cur = None
    for line in zone_lines:
        s = line.strip()
        if not s or SEP_LINE_RE.match(s) or PAGENUM_RE.match(s) \
                or re.match(r"^\s*第\s*[0-9０-９]{1,4}\s*页\s*$", s):   # v0.9d: "第128页"式页码也不并入
            continue
        mm = match_marker(s)
        if not mm:
            g = GLUE_SEP_RE.match(s)          # 粘连残片：__① 原文… / ——[1] 原文…
            if g:
                mm = match_marker(s[g.end():])
                if mm:
                    s = s[g.end():]
        if mm:
            style, num, raw, end = mm
            cur = Footnote(style=style, num=num, raw=raw,
                           text=_join_lines([s[end:].strip()]), page_idx=page_idx)
            notes.append(cur)
        elif cur is not None:
            cur.text = _join_lines([cur.text, s])
        else:
            cur = Footnote(style=None, num=None, raw="", text=s, page_idx=page_idx)
            notes.append(cur)
    return notes

@_traced
def _reject_run_boundary(pages, first_body_pg, run_pages, chapter_starts=None):
    """v0.9n17b 中章注释 run 判别：注释页 run 结束后的正文里，若出现
    "回看"run 内注释的标记（同号注释只存在于 run 之内、本章剩余部分没有），
    说明注释被 run 拦腰截断、本章仍在继续——在该处切章界会把本章注释倾倒
    在中章任意位置（《导读拉康》实证：注释页夹在章中间，斯宾诺莎[7]等在
    run 前引用，达利[1]/偏执狂[2]/布勒东[5]/鲁文斯坦[6]在 run 后引用，
    "关键的邂/逅"一句被截断）。返回 True = 拒绝切界。
    本章剩余 = first_body_pg 起至（含）下一注释页或下一章起点的页区间——
    注释必在章界前落地，区间内出现的同号注释即"前视"证据；超过 6 页窗的
    远距离注释不再漏判（re-run 漂移实证：6 页窗漏掉第 7 页的注释页，
    章界丢失、注释连续编号）。正常章尾 run 的后续正文引用下一章自己的
    注释，同号注释在区间内存在 → 不拒绝（[21] 修复场景保持有效）。
    """
    run_nums = {nt.num for pg in run_pages for nt in pg.footnotes
                if nt.num is not None}
    if not run_nums:
        return False
    n_pages = len(pages)
    _starts = chapter_starts or set()
    limit = n_pages - 1
    for r in range(first_body_pg.idx, n_pages):
        if pages[r].footnotes or pages[r].idx in _starts:
            limit = r
            break
    hits = set()
    for q in range(first_body_pg.idx, limit + 1):
        body = "\n".join(pages[q].lines)
        for st, res in FAMILY_RES.items():
            if st in ("dot", "numsp"):
                continue               # 弱形态走号集门控，下方按号过滤
            for m in res.finditer(body):
                hits.add(_extract_num(st, m.group(0)))
        for st in ("dot", "numsp"):
            for m in FAMILY_RES[st].finditer(body):
                n_ = _extract_num(st, m.group(0))
                if n_ in run_nums:     # 本章真注释号才算（"20 世纪"等不误报）
                    hits.add(n_)
    hits &= run_nums
    if not hits:
        return False
    later = {nt.num for pg in pages[first_body_pg.idx:limit + 1]
             for nt in pg.footnotes if nt.num is not None}
    return bool(hits - later)          # 有标记回看 run 内注释 → 中章 run


def _group_by_chapters(pages, starts):
    if not starts:
        chapters = [Chapter(start_page=0, title=None)]
        boundary = [0]
    else:
        chapters = []
        boundary = []
        if starts[0][0] > 0:
            chapters.append(Chapter(start_page=0, title=None))
            boundary.append(0)
        for pg_idx, title in starts:
            chapters.append(Chapter(start_page=pg_idx, title=title))
            boundary.append(pg_idx)
    for pg in pages:
        ci = bisect.bisect_right(boundary, pg.idx) - 1
        chapters[ci].footnotes.extend(pg.footnotes)
    return chapters


# ---------------------------------------------------------------- 7. 正文标记重编号 + 输出

@_traced(hot=True)
def _extract_num(style, token):
    if style == "circled":
        return CIRCLED.get(token)
    m = re.search(r"\d+", token)
    return int(m.group(0)) if m else None


@_traced
def _renumber_chapter(pages_data, style_lock=None, quiet=False, toc_pages=()):
    """章内注释重编号（同页同号多注释修复版 + v0.9b 前视跨页修复）。

    pages_data: [(page_idx, 正文, [Footnote])]，按页序。
    书页边界丢失会产生合并页，同一页可能含多条同号注释 —— 旧版按
    (样式, 页码, 编号) 当唯一键，冲突时一条被静默跳过、两条共享一号。
    本版：每页注释按行序入队，正文标记按出现顺序消费（标记与注释
    的出现顺序天然一一对应）；消费完的重复标记复用已分配编号（同一
    注释被多次引用）；本页从未有过该号先向前回溯（见前注，告警）；
    v0.9b：回溯不到则前视下一页（书页底标记与其注释被切页符拆开，
    注释落在下一 marked 页，告警）；仍无则保留原号并告警。消费后
    剩余的注释为未引用，排在章内序列末尾（告警）。
    """
    if style_lock == "symbol":          # v0.9c 审计#2: symbol 不在 FAMILY_RES，锁定它不扫描不编号
        families = {}
    else:
        families = ({style_lock: FAMILY_RES[style_lock]} if style_lock else FAMILY_RES)
    counters = {}

    def next_num(st):
        counters[st] = counters.get(st, 0) + 1
        return counters[st]

    page_groups = []
    for pi, body, notes in pages_data:
        groups = {}
        for nt in notes:
            if nt.num is not None and nt.style not in ("symbol", None):
                groups.setdefault((nt.style, nt.num), []).append(nt)
        page_groups.append(groups)

    # v0.9n17 章级同号表：注释常被排版/切页聚到章尾或邻页，距正文标记可
    # 达数页（《导读拉康》实证：标记在第 4 页，6 条注释全在第 7 页，超出
    # 前视 2 页窗口）。值为 (页序, 注释) 元组，消费时须同步从 page_groups
    # 摘除，否则章尾"未被消费"分支会对已消费注释二次编号
    chapter_groups = {}
    for _qi, (_pi, _b, _nts) in enumerate(pages_data):
        for _nt in _nts:
            if _nt.num is not None and _nt.style not in ("symbol", None):
                chapter_groups.setdefault((_nt.style, _nt.num), []).append((_qi, _nt))

    styles_present = set()
    for _, _, _nts in pages_data:
        for _nt in _nts:
            if _nt.style:
                styles_present.add(_nt.style)

    claims_chapter = {}          # (st, n) -> 前页前视为本页注释预定的新号
    spans_by_page = {}           # 逐页 spans 延迟回写（配合序数配对）
    pend = []                    # 未解析标记 (页idx,起,止,族,号,原文)
    # v0.9n17 numsp 正文门控集：本章注释出现过的原书编号。正文"N "形态只有
    # 命中本章真实注释号才算标记——"20 世纪 70 年代"这类数字+空格正文由此
    # 挡在标记之外，绝不偷注释（门控前 numsp 正文标记是 PDF 转写书的最大误报源）
    chapter_nums = set()
    for _, _, _nts in pages_data:
        for _nt in _nts:
            if _nt.num is not None and _nt.style not in ("symbol", None):
                chapter_nums.add(_nt.num)
    for idx, (pi, body, notes) in enumerate(pages_data):
        if pi in toc_pages:
            # v0.9n24: 目录页条目行命中 dot/paren 族标记形态（"1. Kolektivizimi…37"），
            # 参与正文标记扫描会挤占配对表并刷假警告（阿尔巴尼亚书末目录实证）；
            # 目录页不含真实引用，整页豁免。回写循环另读 pages_data，不受影响。
            body = ""
        groups = page_groups[idx]
        had_key = set(groups)
        consumed_num = {}            # (st, n) -> 该注释已分配的新号（重复引用复用）
        # 跨页前视预定表：前页标记经前视消费了本页注释时，本页自己的同号标记
        # 不能再 pop（注释已被 pop 走），按预定号复用，否则 consumed_num[key] KeyError
        claims = claims_chapter
        spans = []
        unresolved = 0
        unres_list = []
        # v0.9n17 跨族标记按正文出现顺序统一消解：原实现按族循环，dot 整族
        # 先于 numsp 拿到编号，两族交错时编号与注释错位（《导读拉康》实证：
        # "…(1984a)5 以及…(1989)6、"中 dot 先扫完，注释 5/6 互换新号）。改为
        # 先收集全部候选（各族专属守卫保留），按位置排序、重叠去重后逐一消解。
        _cands = []
        for st, res in families.items():
            if st not in styles_present and st != "symbol" \
                    and st not in ("dot", "numsp"):
                continue   # v0.9a style-lock：本章无此族注释，正文不扫该族（§/段落号防误报）。
                           # v0.9n17 例外：dot/numsp 正文标记与注释常跨族（"1、"正文 ↔
                           # "[1]"注释，《导读拉康》实证），两族有 chapter_nums 门控兜底，
                           # 不受 styles_present 限制——否则跨族桥接永远无米下锅
            for m in res.finditer(body):
                # v0.9e 行首强标记 = 页顶注释残留文本，不是真引用（姚斯案）
                if st in ("circled", "bracket"):
                    _ls0 = body.rfind("\n", 0, m.start()) + 1
                    if not body[_ls0:m.start()].strip():
                        continue
                if st in ("dot", "paren", "paren2"):
                    _ls0 = body.rfind("\n", 0, m.start()) + 1
                    if m.start() - _ls0 <= 1:
                        continue       # 行首（含§后）结构性编号
                if st == "dot" and m.end() < len(body) and body[m.end()].isdigit():
                    continue           # 小数（72.00 的 72.）
                _cands.append((m.start(), st, m))
        _cands.sort(key=lambda x: x[0])
        _last_end = -1
        for _pos, st, m in _cands:
            if _pos < _last_end:
                continue   # 与已采纳候选重叠（"（5）"兼中 paren/paren2 之类），先者优先
            _last_end = m.end()
            old_n = _extract_num(st, m.group(0))
            if st in ("dot", "numsp") and old_n not in chapter_nums:
                continue           # v0.9n17 门控：本章无同号注释 → 非标记
                                   # （"20 世纪"/"1. 引言"等数字形态不误伤）
            key = (st, old_n)
            dq = groups.get(key)
            rep_style = st
            if not dq and st in ("dot", "numsp", "bracket"):
                # v0.9n17 跨族同号桥接：正文"1、"/"5 "与注释"[1]"/"1 "分属
                # dot/numsp/bracket 族但同号同义（《导读拉康》实证：正文圈码
                # 被 OCR 成"1、"，注释行是"1 劳拉·莫微"），按号直配，
                # 显示随注释族统一 [N]
                for _st2 in ("bracket", "numsp", "dot", "paren", "paren2"):
                    if _st2 == st:
                        continue
                    _dq2 = groups.get((_st2, old_n))
                    if _dq2:
                        dq = _dq2
                        rep_style = _st2
                        break
            rep = None
            if dq:
                nt = dq.pop(0)
                if nt.new_num is None:
                    nt.new_num = next_num(nt.style)
                consumed_num[key] = nt.new_num
                consumed_num.setdefault((nt.style, nt.num), nt.new_num)
                rep = fmt_marker(rep_style, nt.new_num)
            elif key in consumed_num:
                rep = fmt_marker(st, consumed_num[key])   # 同一注释被多次引用
            elif key in claims:
                rep = fmt_marker(st, claims[key])         # 前页前视已消费本页该号注释
            else:
                # v0.9n17 章内跨页桥接（回溯/前视之前；位于 consumed_num/
                # claims 之后，重复引用仍优先复用已配编号）：注释聚于章尾/
                # 邻页、距标记页超出前视 2 页窗口（《导读拉康》实证：标记在
                # 第 4 页，6 条注释全在第 7 页），或 dot/numsp/bracket 跨族
                # 同号时，页级表查无，按章全局同号表消费
                if st in ("dot", "numsp", "bracket"):
                    for _st2 in (st,) + tuple(x for x in ("bracket", "numsp", "dot", "paren", "paren2")
                                              if x != st):
                        _dq3 = chapter_groups.get((_st2, old_n))
                        while _dq3 and _dq3[0][1].new_num is not None:
                            _dq3.pop(0)     # 页级/前视已消费的过期条目
                        if not _dq3:
                            continue
                        if _dq3[0][0] <= idx + 2:
                            continue    # v0.9n19 门控：前视窗口（2 页）内的同号
                                        # 注释留给前视按序消费，章级表只管超窗远距
                        _qi, _nt = _dq3.pop(0)
                        if _nt.new_num is None:
                            _nt.new_num = next_num(_nt.style)
                        _pg = page_groups[_qi].get((_st2, old_n))
                        if _pg:
                            try:
                                _pg.remove(_nt)      # 同步摘除，防章尾二次编号
                            except ValueError:
                                pass
                        consumed_num[key] = _nt.new_num
                        consumed_num.setdefault((_nt.style, _nt.num), _nt.new_num)
                        rep = fmt_marker(_st2, _nt.new_num)
                        break
                if rep is None:
                    for q in range(idx - 1, -1, -1):
                        qd = page_groups[q].get(key)
                        if qd and qd[0].new_num is not None:
                            rep = fmt_marker(st, qd[0].new_num)
                            print(f"  [提示] 第{pi + 1}页标记 {m.group(0)} 回溯改号为"
                                  f"第{pages_data[q][0] + 1}页注释（若并非'见前注'类引用，"
                                  f"请检查该页注释提取）", file=sys.stderr)
                            break
                if rep is None:
                    # v0.9c-B 前视：页底标记与其注释被切页符拆开（注释在
                    # 后续 marked 页的注释区/页首）时按序跨页消费，最多前探 2 页。
                    for q in range(idx + 1, min(idx + 3, len(pages_data))):
                        qd = page_groups[q].get(key)
                        if qd:
                            nt = qd.pop(0)
                            if nt.new_num is None:
                                nt.new_num = next_num(st)
                            claims_chapter[key] = nt.new_num
                            rep = fmt_marker(st, nt.new_num)
                            print(f"  [提示] 第{pi + 1}页标记 {m.group(0)} 与第"
                                  f"{pages_data[q][0] + 1}页注释跨页相接，已按序改号",
                                  file=sys.stderr)
                            break
                if rep is None:
                    unresolved += 1
                    _ls = body.rfind("\n", 0, m.start()) + 1
                    _le = body.find("\n", m.end())
                    _snip = body[_ls:_le if _le > 0 else len(body)].strip()[:50]
                    unres_list.append((m.group(0), _snip))
                    _rs, _re = m.start(), m.end()
                    if st in ("dot", "numsp"):
                        _re = m.end(1)     # 与上方替换区间一致，保留标点
                    pend.append((idx, _rs, _re, st, old_n, m.group(0)))
                    continue
            _rs, _re = m.start(), m.end()
            if st in ("dot", "numsp"):
                _re = m.end(1)    # v0.9n17 只换数字、保留尾随标点/空格：
                                  # "1、"→"[1]、"，"5 "→"[5] "（圈码本无标点，
                                  # bracket/circled 仍整段替换，行为不变）
            spans.append((_rs, _re, rep))
        if unresolved:
            _det = "；".join(f"{r} @《{s}…》" for r, s in unres_list[:3])
            if not quiet:
                print(f"  [提示] 第{pi + 1}页 {unresolved} 个标记找不到注释 [{_det}]",
                      file=sys.stderr)
        for sp in spans:
            spans_by_page.setdefault(idx, []).append(sp)

    # ---- 序数配对救援 --------------------------------------------------
    # 连续编号（㉑-㊿）的书 OCR 几乎必然把正文圈码识错（㊾→④、㉚→①），
    # 精确等号找不到注释、注释成批"未引用"。一章之内正文标记与页底注释
    # 天然同序：未解析标记按出现顺序与未引用注释两两配对；
    # 同号重复引用复用已配编号，不消耗新注释。
    if pend:
        _pool = []
        for _qi, (_pi, _b, _nts) in enumerate(pages_data):
            for _nt in _nts:
                if _nt.new_num is None and _nt.style in families and _nt.style != "symbol":
                    _pool.append((_qi, _nt))
        _used, _paired, _rescued = set(), {}, 0
        for (_idx, _s2, _e2, _st, _old, _raw) in pend:
            _key = (_st, _old)
            if _key in _paired:
                spans_by_page.setdefault(_idx, []).append(
                    (_s2, _e2, fmt_marker(_st, _paired[_key])))
                continue
            for _k, (_qi, _nt) in enumerate(_pool):
                if id(_nt) in _used or _qi < _idx or _nt.style != _st:
                    continue
                _nt.new_num = next_num(_nt.style)
                _used.add(id(_nt))
                _qd = page_groups[_qi].get((_nt.style, _nt.num))
                if _qd:
                    try:
                        _qd.remove(_nt)
                    except ValueError:
                        pass
                _paired[_key] = _nt.new_num
                _rescued += 1
                # v0.9n17 显示随注释族：[N] 统一（跨族配对后正文"1."与注释"[1]"并存的乱象）
                spans_by_page.setdefault(_idx, []).append(
                    (_s2, _e2, fmt_marker(_nt.style, _nt.new_num)))
                break
        if _rescued:
            print(f"  [提示] 序数配对救援：{_rescued} 个编号失配标记已按出现顺序"
                  f"配对本章注释（原书连续编号 ㉑-㊿ 的圈码被 OCR 识错所致）",
                  file=sys.stderr)
    for _idx, _spans in spans_by_page.items():
        _pi, _body, _nts = pages_data[_idx]
        for _s2, _e2, _rep in sorted(_spans, key=lambda x: -x[0]):
            _body = _body[:_s2] + _rep + _body[_e2:]
        pages_data[_idx] = (_pi, _body, _nts)

    for (pi, body, notes), groups in zip(pages_data, page_groups):
        for key, dq in groups.items():
            for nt in dq:            # 未被消费的注释
                nt.new_num = next_num(nt.style)
                if not quiet:
                    print(f"  [提示] 第{pi + 1}页注释 {nt.raw} 未引用→第 {nt.new_num} 号 "
                          f"《{nt.text[:40]}…》", file=sys.stderr)

@_traced
def _is_heading_line(s, prof=None):
    """标题/独立行：§ 开头、【开头、（选自…，或短行且无任何句读。
    长度窗口与句读集来自语言档案（zh 默认与 v0.9n7 硬编码一致）。"""
    if prof is None:
        prof = LANG_DEFAULTS
    if _HEAD_START_RE.match(s):
        return True
    return len(s) <= prof["heading_max_len"] \
        and not any(p in s for p in prof["heading_punct"])


_SENT_END = tuple("。！？；：…”』」】)")
_HEAD_START_RE = re.compile(r"^(§|【|（选自|第[一二三四五六七八九十百]+[卷部篇]|<<<)")

@_traced
def _reflow(body, prof=None):
    """断行重排：丢弃残留注释分隔线；上一行未以句读收尾、且两侧都不像
    标题、并非双短行时，拼回一句（CJK 直连；拉丁字母词间补空格）。
    句尾判定/最小行长来自语言档案（zh 默认与 v0.9n7 硬编码一致）。"""
    if prof is None:
        prof = LANG_DEFAULTS
    _sent_end = tuple(prof["sent_end"])
    _min_len = prof["reflow_min_len"]
    paras = []
    # v0.9n16: \f 是硬边界——逐页各自重排再拼回。跨页拼句本就违规（页界是
    # OCR 断行最不可靠处），且 \f 必须在成品中存活到写出，重跑才拿得到页结构。
    if "\f" in body:
        # 空 chunk（页本体被抽空/纯空行 padding）不产生输出——注释在 footnotes
        # 里，正文空块无信息；保留只会让 \f 逐遍叠加（空白漂移实证）
        return "\f".join(c for c in (_reflow(chunk, prof)
                                     for chunk in body.split("\f")) if c)
    for line in body.split("\n"):
        s = line.strip()
        if not s or SEP_LINE_RE.match(s):
            continue
        if paras:
            prev = paras[-1]
            # v0.9k2: 不拼接入数字起排行（注释/编号项形态）——页底断行+页内注释
            # 会被误拼（lacan "并认为精神1 巴勃罗·毕加索…"实证）。保守防粘连，
            # 只阻断拼接，绝不删除。
            _digit_item = re.match(r"^\d{1,2}[ 　]+\S", s)
            if (not _digit_item
                    and not prev.endswith(_sent_end)
                    and not _is_heading_line(prev, prof)
                    and not _is_heading_line(s, prof)
                    and len(prev) > _min_len and len(s) > _min_len):
                joiner = (" " if (prev[-1].isascii() and prev[-1].isalnum()
                                  and s[0].isascii() and s[0].isalnum()) else "")
                paras[-1] = prev + joiner + s
                continue
        paras.append(s)
    return "\n\n".join(paras)



@_traced
def _generate_output(chapters, renumber, notes_title, page_join, reflow=True,
                     inline=False, prof=None):
    """inline=True 时注释就地回填：跟在首次引用它的正文段之后；
    未被引用的注释仍归章末，绝不静默丢弃。inline=False 维持 0.1 章末归集。"""
    sep = "\n\n" if reflow else "\n"
    blocks = []
    for ch in chapters:
        parts = []
        if ch.title:
            parts.append(ch.title)   # 章节标题（<<<CHAPTER>>> 正文行已被剔除）
        if reflow and ch.body:
            _tp = set(getattr(ch, "toc_chunks", None) or ())
            if _tp:
                body = page_join.join(
                    t if i in _tp else _reflow(t, prof)
                    for i, t in enumerate(ch.body.split(page_join)))
            else:
                body = _reflow(ch.body, prof)
        else:
            body = ch.body or ""
        # v0.9n16: 章题去重下沉到段落级——\f 保留后章题常与正文同处一个 \f 块
        # （块内段间 \n\n），main 里旧的"整块等题"判据永不命中，重跑逐遍叠加章题
        # （front bucket 实证：run2 起每遍多一个重复标题）。首个 \f 块内所有与
        # 章题等值的 leading 段一律剔除。
        if ch.title:
            _tb = body.lstrip().split("\f")
            if _tb:
                _tp = _tb[0].split("\n\n")
                while _tp and _tp[0].strip() == str(ch.title).strip():
                    _tp.pop(0)
                _tb[0] = "\n\n".join(_tp)
                if not _tb[0]:
                    _tb = _tb[1:]      # 首个 \f 块被掏空则整块移除，不留悬挂 \f
                body = "\f".join(_tb)
        tail = []
        if ch.footnotes:
            if inline:
                note_map = {}
                for nt in ch.footnotes:
                    if nt.style != "symbol":
                        _kn = nt.new_num if nt.new_num is not None else nt.num   # v0.9d: none 模式回退原号（v0.9c 被前置条件挡死）
                        if _kn is not None:
                            _key = (nt.style, _kn)
                            # v0.9f 审计#2: 键冲突禁止覆盖——被覆盖的注释不进 placed
                            # 也不进 rest/tail，会静默蒸发。保留先到者（页序在前），
                            # 后来者转章末并告警。原则：任何注释要么落位要么进章末。
                            if _key in note_map:
                                _log("WARN", "GEN ", f"章{str(ch.title)[:12]!r} 注释编号冲突 "
                                     f"{fmt_marker(*_key)}: 保留《{note_map[_key].text[:20]}…》，"
                                     f"《{nt.text[:20]}…》转章末")
                                tail.append(nt)
                                continue
                            note_map[_key] = nt
                            continue
                    tail.append(nt)
                out, placed = [], set()
                # v0.9n16: inline 成品维持扁平形态（无 \f），与 v0.9n16 前行为一致；
                # 默认章末归集模式的幂等性由 \f 保留担保
                for p in body.replace("\f", sep).split(sep):
                    out.append(p)
                    hits = []
                    for key in note_map:
                        if key in placed:
                            continue
                        marker = fmt_marker(key[0], key[1])
                        if not marker:
                            continue
                        start = 0
                        while True:
                            i = p.find(marker, start)
                            if i < 0:
                                break
                            start = i + len(marker)
                            # v0.9b 边界检查："15." 里的 "5."、"3.14" 里的 "3." 不算
                            if i > 0 and p[i - 1].isdigit():
                                continue
                            if i + len(marker) < len(p) and p[i + len(marker)].isdigit():
                                continue
                            # dot/paren 族段首命中 = 结构性段落号，不是引用
                            if key[0] in ("dot", "paren", "paren2") and i <= 1:
                                continue
                            hits.append((i, key))
                            break               # 每注释每段取首个有效命中
                    for _, key in sorted(hits):   # 按标记在段内出现位置排序
                        nt = note_map[key]
                        out.append(f"{fmt_marker(key[0], key[1])} {nt.text}".rstrip())
                        placed.add(key)
                body = sep.join(out)
                rest = [nt for key, nt in note_map.items() if key not in placed]
                # v0.9e 安全网：被消费但标记落位失败（含主流程差异路径）的注释不得丢失
                for key, nt in list(note_map.items()):
                    if key in placed and nt.text[:30] not in body:
                        print(f"  [提示] 注释落位失败，转章末: {nt.text[:30]}…", file=sys.stderr)
                        placed.discard(key)   # v0.9n13: 防同注既内联又章末双现
                        rest.append(nt)
                tail = rest + tail
                _log("INFO", "GEN ", f"章{ch.title and str(ch.title)[:12]!r} 内联{len(placed)} 尾部{len(tail)}")
            if not inline:
                lines = []
                for nt in ch.footnotes:
                    if renumber == "seq" and nt.new_num is not None and nt.style != "symbol":
                        head = fmt_marker(nt.style, nt.new_num) or nt.raw
                    else:
                        head = nt.raw if nt.raw else "(注释)"
                    lines.append(f"{head} {nt.text}".rstrip())
                parts.append(notes_title + "\n" + "\n".join(lines))
            elif tail:
                lines = []
                for nt in tail:
                    if renumber == "seq" and nt.new_num is not None and nt.style != "symbol":
                        head = fmt_marker(nt.style, nt.new_num) or nt.raw
                    else:
                        head = nt.raw if nt.raw else "(注释)"
                    lines.append(f"{head} {nt.text}".rstrip())
                parts.append(notes_title + "\n" + "\n".join(lines))
        if body:
            parts.insert(1 if ch.title else 0, body)   # 保持 标题→正文→注释 顺序
        # v0.9n17: 章内 parts 用 \f 衔接（页结构不变量）：标题/正文/注释块各成
        # 页，重跑时全注释页 run 边界与章界稳定复现。用 \n\n 会把标题页与正文
        # 页粘成一页、注释块粘进本章末页，重跑章节坍缩、注释连续编号
        # （导读拉康 k1→k2 漂移实证：注释块末页化后 run 边界消失）
        blocks.append("\f".join(b for b in parts if b))
    # v0.9n16: 章间用 \f 衔接——章末注释块必须是最后一个 \f 块的块底锚点，
    # 重跑时 zone 自底向上才锚得到（\n\n 衔接会把下一章正文粘进同块，页底变
    # 正文行 → 注释整片漏提，漂移实证）。make_epub 分块前把 \f 归一为空行。
    return "\f".join(b for b in blocks if b)





COPY_KEYWORDS = ("图书在版编目", "CIP", "核字", "版权所有", "ISBN", "在版编目", "定价：")


@_traced

def _detect_stat_headers(pages, top_n, toc_pages, first_content_idx):
    """v0.9n22 统计页眉行检测器：与页眉聚类/独立数字流/附着流三检测器独立运行，
    互不设门。处理三检测器的已知盲区——OCR 只保留部分页页眉时聚类覆盖率不足
    拒判、而裸眉行（无附着页码）又不匹配任何页码形态（《德国观念论的终结》
    198/816 页案）。

    判据（两道，对应规范「形态圈候选、结构验证才是判据」）：
      形态（圈候选）：全书归一化行频 >=3、归一化长度 >=4、非纯数字、
                      非 <<<CHAPTER>>> 标记行；
      结构（放行）：  该形态的全部命中行都位于页首带/页尾带（top_n 非空行内）。
    豁免：前置部分（首个章标记页之前，封面/题名页同形行由此保命）与目录豁免
          页内的命中行一律不删。
    拒判留痕：候选形态数、各形态命中数与位置违约数。
    返回 accepted = [(form, [(pg_idx, line_idx), ...]), ...]。
    """
    from collections import Counter
    freq, hits = Counter(), {}
    for pi, pg in enumerate(pages):
        for li, l in enumerate(pg.lines):
            # 归一化对齐 toc 的 normalize_header：剥首尾页码与点
            # （"RENAISSANCE CRITICISM. 4" 若不去数字，每个页码一个形态，
            #  频次全碎在 3 以下，统计候选根本形不成——Saintsbury 实证）
            n = re.sub(r"^\s*[\d.．]+\s*|\s*[\d.．]+\s*$", "", l)
            n = re.sub(r"\s+", "", n)
            if len(n) >= 4 and not n.isdigit() and not l.lstrip().startswith("<<<"):
                freq[n] += 1
                hits.setdefault(n, []).append((pi, li))
    edge = {}
    for pi, pg in enumerate(pages):
        idx = [i for i, l in enumerate(pg.lines) if l.strip()]
        edge[pi] = set(idx[:top_n]) | set(idx[-top_n:] if len(idx) > top_n else idx)
    accepted = []
    for form, cnt in freq.most_common():
        if cnt < 3:
            break
        hs = hits[form]
        off_edge = [(pi, li) for pi, li in hs if li not in edge.get(pi, set())]
        if not off_edge:
            safe = [(pi, li) for pi, li in hs
                    if pi >= first_content_idx and pi not in toc_pages]
            if safe:
                accepted.append((form, safe))
                _log("INFO", "SHDR", f"统计页眉行确认：'{form[:24]}' 命中 {len(hs)} "
                     f"（删 {len(safe)}，豁免 {len(hs)-len(safe)}）")
            else:
                _log("WARN", "SHDR", f"统计页眉行拒判：'{form[:24]}' 命中 {len(hs)} "
                     f"但全部落在前置部分/目录页，不删")
        else:
            _log("WARN", "SHDR", f"统计页眉行拒判：'{form[:24]}' 命中 {len(hs)}，"
                 f"位置违约 {len(off_edge)}（正文行同形，形态盲删风险）")
    return accepted

def _split_pages_at_markers(pages):
    """v0.9n18: 页内含 <<<CHAPTER>>> 且不在首行时按标记行切页并重建页索引。"""
    out = []
    for pg in pages:
        cuts = [li for li, l in enumerate(pg.lines)
                if li > 0 and CHAPTER_MARKER_RE.match(l.strip())]
        if not cuts:
            out.append(pg)
            continue
        prev = 0
        for c in cuts + [len(pg.lines)]:
            seg = pg.lines[prev:c]
            if seg:
                out.append(Page(idx=len(out), lines=seg))
            prev = c
    for n, pg in enumerate(out):
        pg.idx = n
    return out


def _scan_copy_pages(pages):
    """版权页定位：逐页扫描 CIP/版权关键词，WARN 级输出页码+命中行。
    v0.7a: renumber 的 pages 是 Page 对象列表，行在 pg.lines（Page 不可迭代）。"""
    hits = []
    for pg in pages:
        for ln in pg.lines:
            if any(k in ln for k in COPY_KEYWORDS):
                _log("WARN", "COPY", f"p{pg.idx+1}: {ln.strip()[:60]!r}")
                hits.append((pg.idx, ln.strip()))
    if hits:
        _log("WARN", "COPY", f"版权页命中 {len(hits)} 行, 页 {sorted(set(pi for pi, _ in hits))}（前置 matter，应被豁免）")
    else:
        _log("INFO", "COPY", "版权页扫描: 0 命中")
    return hits

@_traced
def _dbg_diff_deleted(before, pages):
    """对比 remove_headers 前后行集合，报告被删行中含圈码的行。
    若 >0，即实锤"注释行被页眉删除误杀"（v0.3-B8 要消灭的现象）。"""
    from collections import Counter
    circled = re.compile(r'[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
                         r'㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟]')
    suspicious = 0
    for pg in pages:
        b = before.get(pg.idx)
        if b is None or len(pg.lines) >= len(b):
            continue
        diff = Counter(b) - Counter(pg.lines)
        for ln, c in diff.items():
            if circled.search(ln):
                suspicious += c
                if suspicious <= 20:
                    print(f"[debug] 被删行含圈码 第{pg.idx+1}页: {ln.strip()[:60]!r}")
    if suspicious:
        _log("WARN", "DIFF", f"remove_headers 删行含圈码 {suspicious} 行——注释被误删！")
    else:
        _log("INFO", "DIFF", "remove_headers 删行含圈码 0 行")


__VERSION__ = "v0.9n28-statguard1118"
def main():
    ap = argparse.ArgumentParser(
        description="按标号处理：去页眉/页码 + 注释归集章末 + 跨页引用修复。\n"
                   "章节来源：<<<CHAPTER>>> 注入标记（见 toc_mark_chapters.py）＞"
                   "内置正则＞--chapter-pattern",
        epilog="两步流水线：\n"
               "  python toc_mark_chapters.py book.txt --toc-pages 2 -o marked.txt\n"
               "  python renumber_footnotes.py marked.txt -o final.txt --yes",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("input", metavar="文件路径",
                help="输入文本文件路径（必填；文本需含 \\f 分页符或配合 --separator）")
    ap.add_argument("-o", "--output", help="输出文件路径，默认在输入文件名后加 .clean.txt")
    ap.add_argument("--separator", metavar="正则",
                    help="页分隔方式：默认按换页符 \\f 切页；若文本用某行分页"
                         "（如 '----- 12 -----'），传该行的正则，\n"
                         "如 \"^\\s*-+\\s*\\d+\\s*-+\\s*$\"")
    ap.add_argument("--pages", type=int, default=3,
                    help="页眉判定区域：每页顶部前 N 个非空行（默认 3）。\n"
                         "页眉位置不固定、或页眉和页码之间隔了空行时可调大；页底页眉块与页首同等处理")
    ap.add_argument("--threshold", type=float, default=0.6,
                    help="页眉/页码覆盖率阈值 0~1（默认 0.6）：\n"
                         "候选行须出现在 >= 该比例的页面顶部才被识别。漏检调低，误检调高")
    ap.add_argument("--ratio", type=float, default=0.4,
                    help="注释区标记行比例阈值 0~1（默认 0.4）：\n"
                         "从页底向上扫描，标记行占注释区非空行的比例 >= 该值才认定是注释。\n"
                         "注释漏提取（长续行多）调低；正文被误当注释调高")
    ap.add_argument("--note-style", choices=["auto", "circled", "bracket", "dot", "paren", "symbol"],
                    default="auto",
                    help="锁定全书注释标记样式（默认 auto 每页自动识别）：\n"
                         "circled=①② bracket=[1] dot=1. paren=(1) symbol=§†‡¶。\n"
                         "锁定后其它样式一律按正文处理，彻底避免 '§16.' 这类行被误判为注释；\n"
                         "建议先用 --verbose 确认全书样式后再锁定")
    ap.add_argument("--chapter-pattern", metavar="正则",
                    help="追加自定义章节标题正则（如西文书 \"^\\s*Section\\s\"），\n"
                         "与内置的「第X章/Chapter X/序章/附录」等规则同时生效")
    ap.add_argument("--renumber", choices=["seq", "none"], default="seq",
                    help="seq（默认）：注释按章重新连续编号，正文内标记同步改写\n"
                         "（只改本页注释对应的标记，章内顺序重编（含跨页回溯/前视改号））；\n"
                         "none：保留原标记，适合原书注释本就全文连续编号的情况")
    ap.add_argument("--notes-title", default=None,
                    help="章末注释区块的标题文字（默认取语言档案 notes_title；显式传入则覆盖档案）")
    ap.add_argument("--lang", metavar="代码", default=None,
                    help="强制语言代码（如 de/en），跳过 <input>.lang sidecar 探测")
    ap.add_argument("--lang-file", metavar="路径", default=None,
                    help="语言档案 JSON，默认取脚本同目录 toc_languages.json")
    ap.add_argument("--no-reflow", action="store_true",
                    help="关闭正文断行重排（默认开启：残留分隔线丢弃、句中换行拼回、"
                         "标题行保持独立）")
    ap.add_argument("--yes", action="store_true",
                    help="跳过交互确认，自动删除检测到的全部页眉/页码")
    ap.add_argument("--verbose", action="store_true",
                    help="打印每页提取到几条注释及各自标记，用于排查漏提/多提")
    ap.add_argument("--inline", action="store_true",
                    help="注释就地回填到首次引用它的正文段之后（默认维持 0.1 章末归集）")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARN", "ERROR"],
                    help="日志级别：WARN=只看异常, INFO=结果+异常(默认), DEBUG=逐调用全量")
    args = ap.parse_args()
    globals()["_LOG_LEVEL"] = args.log_level

    # tee 必须先于一切输出，保证 [VER]/时间戳/崩溃现场都进 .debug.log
    try:
        import sys as _sys
        import traceback as _tb
        _dbg_path = (args.output or (args.input + ".clean.txt")) + ".debug.log"
        _dbg_f = open(_dbg_path, "w", encoding="utf-8")
        class _TeeOut:
            def __init__(self, *streams): self._streams = streams
            def write(self, s):
                for _s in self._streams:
                    _s.write(s); _s.flush()
                return len(s)
            def flush(self):
                for _s in self._streams: _s.flush()
        _sys.stdout = _TeeOut(_sys.stdout, _dbg_f)
        _sys.stderr = _TeeOut(_sys.stderr, _dbg_f)

        def _crash_hook(exc_type, exc, tb, _p=_dbg_path):
            try:
                with open(_p, "a", encoding="utf-8") as _cf:
                    _cf.write("\n[%s][CRASH] 未捕获异常，完整现场：\n" % _time.strftime("%H:%M:%S"))
                    _tb.print_exception(exc_type, exc, tb, file=_cf)
            finally:
                _sys.__excepthook__(exc_type, exc, tb)
        _sys.excepthook = _crash_hook
        _log("INFO", "DBG", f"日志+崩溃钩子就绪: {_dbg_path}")
        import hashlib as _hl
        _h = _hl.sha256(open(__file__, "rb").read()).hexdigest()[:12]
        _dbg_f.write("BANNER v=%s hash=%s time=%s argv=%s\n" % (
            __VERSION__, _h, _time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(_sys.argv)))
        _dbg_f.flush()
        _t0 = _time.time()
    except Exception as _e:
        print(f"日志启用失败（不影响处理）: {_e}")
        _h = ""
    _log("INFO", "VER", f"{__VERSION__} hash={_h} file={__file__}")
    _log("INFO", "IN ", f"输入={args.input} 输出={args.output} renumber={args.renumber} inline={args.inline}")

    # 语言档案：注释块标题/断行句读按语言取；代码来源 --lang > <input>.lang
    # > 同名 .marked.txt.lang（流水线中间步改名后不传 sidecar 的回退）> zh
    _lang_code = args.lang
    if not _lang_code:
        for _cand in (args.input + ".lang",
                      args.input.replace(".prepped.txt", ".marked.txt") + ".lang"):
            if os.path.exists(_cand):
                _lang_code = open(_cand, encoding="utf-8").read().strip() or None
                if _lang_code:
                    _log("INFO", "LANG", f"语言代码取自 {_cand}")
                    break
    _lang_prof = _load_lang_profile(_lang_code, args.lang_file)
    if _lang_prof is None:
        print(f"语言代码「{_lang_code}」不在档案中：检查 toc_languages.json，"
              f"或改用 --lang 指定", file=sys.stderr)
        sys.exit(1)
    if not _lang_code:
        _lang_code = "zh"
    if args.notes_title is None:
        args.notes_title = _lang_prof["notes_title"]
    _log("INFO", "LANG", f"语言={_lang_code}({_lang_prof['label']}) "
                         f"注释块标题={args.notes_title!r}")

    try:

        with open(args.input, encoding="utf-8") as f:
            text = f.read()

        # v0.9n15 圈码归一：无论上游是否跑过 sup_note_prep，进流水线先统一为 [N]
        _n_circ = len(CIRCLED_ALL_RE.findall(text))
        text = norm_circled(text)
        if _n_circ:
            print(f"圈码归一: {_n_circ} 处 ①…㉟ → [N]", file=sys.stderr)
        # v0.9n17 脚注标记逗号残形：闭括号/闭引号后"数字+全角逗号"必是脚注
        # 标记（顿号被 OCR 识错，"(1989)6，这样的著作"实证），归一"6，"→"6、"
        # 走 dot 族。行首年份"1980年，"不匹配（数字后紧跟"年"）。
        _n_comma = len(re.findall(r"(?<=[)）”』])\d{1,2}(?=，)", text))
        text = re.sub(r"(?<=[)）”』])(\d{1,2})，", r"\1、", text)
        if _n_comma:
            print(f"逗号残形归一: {_n_comma} 处 (1989)6， → (1989)6、", file=sys.stderr)

        # 1
        _log("INFO", "STG ", "1/6 _parse_pages")
        pages = _parse_pages(text, args.separator)
        copy_hits = _scan_copy_pages(pages)
        # v0.9n18 标记行级切页：部题/题名页无运行页眉，自动切页不为其分页，
        # 前一章正文尾会与题名页共享一个 \f 块；页级章界把前章尾段连页底注释
        # 整页划给下一章（关键思想/进阶阅读书目实证）。在标记行处切页，
        # 前半留前章，标记与题名起新章；\f 结构由后续输出原样保留。
        _pre_split = len(pages)
        pages = _split_pages_at_markers(pages)
        if len(pages) != _pre_split:
            _log("INFO", "SPLT", f"标记行级切页: {_pre_split}页 -> {len(pages)}页")
        _f_lines = sum(len(pg.lines) for pg in pages)
        _log("INFO", "P1  ", f"切页: {len(pages)}页 共{_f_lines}行 首页行数={len(pages[0].lines) if pages else 0}")
        print(f"共切分出 {len(pages)} 页")

        # 3
        _log("INFO", "STG ", "2/6 _detect_chapters")
        starts = _detect_chapters(pages, args.chapter_pattern)
        _log("INFO", "P3  ", f"章节起点: {len(starts)}个 首页={starts[0] if starts else None} 末页={starts[-1] if starts else None}")
        if starts:
            print(f"检测到 {len(starts)} 个章节起点：")
            for pg_idx, title in starts:
                print(f"  第{pg_idx + 1}页: {title}")
        else:
            print("未检测到章节标题，全部注释将置于全文末尾（可用 --chapter-pattern 指定）")

        _log("INFO", "STG ", "3/6 _detect_footnotes")
        # 4/5
        total_notes = 0
        _zone_pages = 0
        _n_nhead = 0
        lock = None if args.note_style == "auto" else args.note_style
        _fcp = min((pp for pp, _ in starts), default=None)   # v0.10 结构护栏
        toc_pages = _load_toc_pages(args, pages, _lang_prof, _fcp)   # v0.10
        # v0.9n26 部题/章题粘连豁免：页底扫描仅豁免"页底拖尾"的已知
        # 章/部题标题行（《德国观念论的终结》实证：部题行 OCR 粘连在
        # 注释页页底致整页注释漏提；比对前规范化：去全部空白、剥尾部
        # |/｜ 残字）。Saintsbury 目录页实证：豁免必须限制在页底最后
        # 两个非空行内，否则目录行会被逐行越过、整页被吞进注释区。
        _title_keys = set()
        for _, _t in starts:
            _tk = re.sub(r"\s+", "", _t).rstrip("|｜")
            if _tk:
                _title_keys.add(_tk)
        _ntk = re.sub(r"\s+", "", args.notes_title).rstrip("|｜")
        if _ntk:
            _title_keys.add(_ntk)
        def _title_line_pred(s):
            return re.sub(r"\s+", "", s).rstrip("|｜") in _title_keys
        # v0.9n27 页脚注体制直通模式：段首行为 numsp 样式标记（裸数字+
        # 空格，OCR 切页错位使页脚注行落在次段首部）且全书 >=5 页即判定。
        # 注释区提取与章级重编号整体跳过：注行保持 OCR 原位阅读顺序与原号，
        # 不做绑定（Saintsbury 实证：章级编号模型对页级重启体制张冠李戴，
        # 消费复用分支把次页 [8] 当首页 [8] 注的重复引用）。页眉页码清理、
        # 章结构、make_epub 切章等其余 stage 照常。
        _per_page_mode = 0
        for _pg0 in pages:
            if not _pg0.lines:
                continue
            _mm0 = match_marker(_pg0.lines[0].strip())
            if _mm0 and _mm0[0] == "numsp" and len(_pg0.lines[0].strip()) < 200:
                _per_page_mode += 1
        if _per_page_mode >= 5:
            _log("INFO", "STG ", "页脚注体制直通模式：段首 numsp 注行 %d 页，跳过提取与重编号" % _per_page_mode)
        for pg in pages:
            if _per_page_mode >= 5:
                pg.body_lines = list(pg.lines)
                pg.footnotes = []
                pg.lines = list(pg.body_lines)
                continue
            if pg.idx in toc_pages:            # v0.10 目录页整页豁免注释区检测
                pg.body_lines = list(pg.lines)
                pg.footnotes = []
                continue
            k = _detect_footnote_zone(pg.lines, args.ratio, lock,
                                      skip_pred=_title_line_pred)
            # v0.9n16 注释区标题行剔除：原书注释页版式"注释\n① …"的标题行不属
            # 注释（zone 自标记行起），残留正文会与生成标题叠加，重跑逐遍累积
            # （输出漂移审计实证：final2 比 final 多一层"注释"）。只剔紧邻
            # zone 上方、与注释块标题精确等值的独立行，无形态猜测。
            if k > 0 and pg.lines[k - 1].strip() == args.notes_title:
                del pg.lines[k - 1]
                k -= 1
                _n_nhead += 1
            # v0.9f 审计#3: 整页被吞成注释区时告警。注释溢出的"全注释页"属正常
            # （行数很少）；行数多却整页被吞，多半是把正文吃掉了，需人工核对。
            if k == 0:
                _nb = sum(1 for l in pg.lines if l.strip())
                if _nb >= 6:
                    _log("WARN", "ZONE", f"第{pg.idx + 1}页整页（{_nb} 个非空行）被吞为注释区，"
                                         "若该页含正文请人工核对（可调高 --ratio）")
            pg.body_lines = pg.lines[:k]
            page_notes = [(k + t, nt) for t, nt in
                          enumerate(_parse_footnotes(pg.lines[k:], pg.idx))]
            for s2, e2 in reversed(_find_orphan_notes(pg.body_lines, lock)):
                st2 = s2                       # 信号1 的分隔线一并带走
                j2 = s2 - 1
                while j2 >= 0 and not pg.body_lines[j2].strip():
                    j2 -= 1
                if j2 >= 0 and SEP_LINE_RE.match(pg.body_lines[j2].strip()):
                    st2 = j2
                zone2 = pg.body_lines[st2:e2]
                del pg.body_lines[st2:e2]
                page_notes.extend((st2 + t, nt) for t, nt in
                                  enumerate(_parse_footnotes(zone2, pg.idx)))
            page_notes.sort(key=lambda x: x[0])
            pg.footnotes = [nt for _, nt in page_notes]
            if pg.footnotes:
                _zone_pages += 1
                if _zone_pages <= 8 or _zone_pages % 50 == 0:
                    _log("DEBUG", "P4  ", f"zone p{pg.idx+1} k={k} 注{len(pg.footnotes)}: {pg.footnotes[0].text[:24]!r}")
            pg.lines = list(pg.body_lines)  # v0.3-B8: 注释已抽入 pg.footnotes，页眉删除只碰正文
            total_notes += len(pg.footnotes)
            if args.verbose and pg.footnotes:
                marks = ", ".join(nt.raw or "?" for nt in pg.footnotes)
                warns = []
                above = pg.body_lines[-1].strip() if pg.body_lines else ""   # v0.9d: 与边界 k 脱钩，孤儿救援删行后不再越界
                if above and not SEP_LINE_RE.match(above):
                    warns.append("区顶无分隔线")
                    mm = match_marker(above)
                    if mm:
                        warns.append(f"区顶上方紧邻标记行 {above[:20]!r}")
                if len(pg.footnotes) == 1:
                    warns.append("单条注释")
                flag = ("  ⚠ " + "; ".join(warns)) if warns else ""
                print(f"  第{pg.idx + 1}页: 提取注释 {len(pg.footnotes)} 条 [{marks}]{flag}")
        _log("INFO", "P4  ", f"zone统计: 有注页={_zone_pages} 总注={total_notes} "
                          f"注释区标题行剔除={_n_nhead}")
        if _n_nhead:
            print(f"剔除紧邻注释区的标题行「{args.notes_title}」 {_n_nhead} 行")
        print(f"共提取注释 {total_notes} 条")

        # 2
        _log("INFO", "STG ", "4/6 _detect_headers")
        headers, pagenum = _detect_headers(pages, args.pages, args.threshold)
        # v0.9n22 统计页眉行检测器（独立，与三检测器互不设门）
        _first_content = starts[0][0] if starts else 10**9
        _stat = _detect_stat_headers(pages, args.pages, toc_pages, _first_content)

        for _form, _dels in _stat:
            for _pi, _li in sorted(_dels, reverse=True):
                # v0.9n22-fix：章体正文来自 STG3 快照的 body_lines，
                # 必须从 body_lines 删（pg.lines 删了不进输出）
                _pg = pages[_pi]
                # v0.9n28 内容校验闸：索引在 lines（含注行）上统计、在
                # body_lines（注行已抽走）上删除，两列表错位时会删到正文
                # 行（法西斯主义书实证：段内双孤立页码行使统计态偏移，
                # 正文整行被误删）。删除前必须命中统计形态本身，否则跳过
                # 并留痕。
                # v0.9n28 内容锚定删除：_li 在统计侧与执行侧列表可能错位
                # （法西斯主义书实证：误删正文整行且全程静默）。以统计时
                # 捕获的行内容为准，在执行侧 body_lines 里找同源行删除；
                # 找不到即拦下并 WARN（删除可见性纪律：凡删必留痕）。
                if _li < len(_pg.body_lines):
                    _bl = _pg.body_lines[_li].strip()
                    _nhv = normalize_header(_bl)
                    if _cert_hit_forms(_nhv, {_form}) or _bl == _form \
                            or _nhv in {_form, _form.rstrip('.')}:
                        del _pg.body_lines[_li]
                        if _li < len(_pg.lines):
                            del _pg.lines[_li]      # v0.9n28 master 同步，防下游索引级联
                        _log("INFO", "STG4",
                             f"统计页眉删除：第{_pi+1}页行{_li} {_bl[:24]!r}")
                    else:
                        _log("WARN", "STG4",
                             f"统计页眉删除被闸拦下：第{_pi+1}页行{_li} 形态不匹配"
                             f"（{_bl[:24]!r} vs {_form[:24]!r}）")
        if pagenum is None:
            pagenum = _detect_pagenum_relaxed(pages, args.pages)
            if pagenum:
                _log("WARN", "PAG ", f"严格页码序列未检出（阈值 {args.threshold}），"
                     f"启用放宽判定：offset={pagenum['offset']:+d} "
                     f"覆盖率={pagenum['coverage']:.2f}（>=0.30 且最长连续命中 >=5 页）")
        if pagenum is None:
            pagenum = _detect_pagenum_seqorder(pages, args.pages)
            if pagenum:
                _log("WARN", "PAG ", f"页码序列与页索引错位（切页点不等于印刷页），"
                     f"启用文档顺序判定（LIS）：单调链覆盖 {pagenum['coverage']:.2f}，"
                     f"独立行 {len(pagenum['line_hits'])} 条")
        if not headers and not pagenum:
            _log("WARN", "HDR ", f"未检出任何页眉/页码候选（覆盖率阈值 {args.threshold}），"
                 "删除步骤跳过，正文中残留的页眉/页码行将原样保留。")
        if headers or pagenum or args.yes:
            print("检测到页首候选：")
            for i, h in enumerate(headers, 1):
                print(f"  {i}. 「{h['text']}」 覆盖 {h['count']}/{len(pages)} 页 "
                      f"({h['coverage']:.0%}；奇 {h.get('cov_odd', 0):.0%} / 偶 {h.get('cov_even', 0):.0%})")
            pn_idx = None
            if pagenum:
                pn_idx = len(headers) + 1
                if "line_hits" in pagenum:   # seqorder 结果：无 offset/pages
                    print(f"  {pn_idx}. [连续页码·文档顺序] 命中 {len(pagenum['line_hits'])} 行"
                          f" (拟合 {pagenum['coverage']:.0%})")
                else:
                    print(f"  {pn_idx}. [连续页码] 偏移 {pagenum['offset']:+d}，"
                          f"覆盖 {len(pagenum['pages'])}/{len(pages)} 页 ({pagenum['coverage']:.0%})")
            if args.yes:
                sel_headers, sel_pn = headers, pagenum
            else:
                try:
                    ans = input("输入要删除的编号（空格分隔，all=全部，回车=不删）: ").strip()
                except EOFError:                       # v0.9n13: 管道/重定向非交互环境
                    ans = ""
                    _log("WARN", "HDR ", "非交互环境（EOF），按回车=不删处理")
                sel_headers, sel_pn = [], None
                if ans:
                    try:
                        picks = set(range(1, len(headers) + 1)) \
                            if ans.lower() == "all" else {int(x) for x in ans.split()}
                    except ValueError:
                        picks = set()
                        _log("WARN", "HDR ", f"无法解析的选择「{ans}」，按不删处理")
                    sel_headers = [h for i, h in enumerate(headers, 1) if i in picks]
                    if pagenum and pn_idx in picks:
                        sel_pn = pagenum
            dbg_before = {pg.idx: list(pg.lines) for pg in pages}
            r = _remove_headers(pages, sel_headers, sel_pn, args.pages)
            print(f"已移除 {r} 行页眉/页码")
            top_chains = _detect_topstream_chain(pages, args.pages)
            if top_chains and (top_chains[0] or top_chains[1]):
                _streams, _stream_forms = top_chains
                _log("WARN", "PAG ", f"附着页码流链确认：{len(_streams)} 条流 "
                     f"共 {sum(len(c) for c in _streams.values())} 枚"
                     f"（奇偶页 × 页首/页脚带各验各链；链≥8 形态即获证书，覆盖率低不否决）")
                r2, r2d, samples = _strip_folio_tokens(pages, list(_streams.values()),
                                                       args.pages, stream_forms=_stream_forms)
                if r2 or r2d:
                    print(f"边缘页码-眉行：剥数字 {r2} 处、整行删除 {r2d} 行（三闸判定）")
                    for pi, before, after in samples:
                        print(f"  第{pi}页 「{before[:30]}」 → {after}")
            _dbg_diff_deleted(dbg_before, pages)

        # v0.9n17 注释页run边界：章末注释独立成页的书，注释页连续run（正文抽干、
        # 只剩注释的页）结束即新章起点。电子书默认规范：每章注释从 1 起编。
        # 章题"为什么是拉康？"这类不匹配任何章节正则、toc 标记又缺失的书，
        # 这是注释不重号的最后防线（《导读拉康》实证：全书合并成一章，
        # 第 4 章注释被排成 [21] 起）。页下注书无全注释页，行为不变；
        # 书末全目录注释run在文本末尾，无后续页，不产边界。
        # v0.9n17b 中章run拒绝：注释页也可能夹在章中间（原书分节排注释、
        # OCR 跨页断句实证："关键的邂/逅"被注释页截断）。run 后正文标记若
        # 回看 run 内注释（同号注释只存在于 run 内、身后窗口没有），说明
        # 本章仍在继续，切边界会把注释倾倒在中章无标记处——拒绝。
        _note_run_ends = []
        _in_run = False
        _run_pages = []
        for _pg in pages:
            _full = (not any(l.strip() for l in _pg.lines)) and bool(_pg.footnotes)
            if _full:
                _in_run = True
                _run_pages.append(_pg)
            elif _in_run:
                if not _reject_run_boundary(pages, _pg, _run_pages,
                                            chapter_starts={p for p, _ in starts}):
                    _note_run_ends.append(_pg.idx)
                _in_run = False
                _run_pages = []
        if _note_run_ends:
            # v0.10.3：有注入标记的书禁用 run 边界——它是"无标记书"的最后防线
            #（本块注释自述），有标记时纯属越权（拉康 p24 误切实证：注释倒在
            # 中章无题边界，"邂/逅"被注释块截断）。
            _has_injected = any(CHAPTER_MARKER_RE.match(l.strip())
                                for _pg in pages for l in _pg.lines[:5])
            if _has_injected:
                print("注释页run边界: 已禁用（检测到注入章标记，run 边界仅服务无标记书）")
                _note_run_ends = []
        if _note_run_ends:
            _have = {p for p, _ in starts}
            _adds = [(p, None) for p in _note_run_ends if p not in _have]
            if _adds:
                starts = sorted(starts + _adds)
                print(f"注释页run边界: {len(_adds)} 处（每章注释从 1 起编）")

        # 6
        _log("INFO", "STG ", "5/6 _group_by_chapters")
        chapters = _group_by_chapters(pages, starts)
        for _ci, _ch in enumerate(chapters):
            _log("INFO", "GRP ", f"章{_ci} 第{_ch.start_page+1}页 {str(_ch.title)[:16]!r} 注释={len(_ch.footnotes)}")
        # v0.9n16 BREAKING: 输出固定保留 \f 页界（原默认空行衔接）。页结构是全部
        # 判定（注释区/页眉/页码/章节）的前提，丢 \f 的成品重跑会整章坍缩成一页、
        # 注释全量重排（漂移实证）。--keep-page-breaks 选项随默认行为一并移除。
        page_join = "\f"
        boundary_list = [c.start_page for c in chapters]
        page_chapter = {pg.idx: bisect.bisect_right(boundary_list, pg.idx) - 1 for pg in pages}

        # 7
        for ci, ch in enumerate(chapters):
            pages_data = []
            _body_pages = []          # v0.10.3：输出装配序列，目录页原位保留
            for pg in pages:
                if page_chapter[pg.idx] != ci:
                    continue
                body_lines = [l for l in pg.body_lines if not CHAPTER_MARKER_RE.match(l.strip())]
                # v0.9n16: 剥页尾空行 padding。注释已抽入 footnotes，页尾空行只剩
                # 版面意义；\f 衔接下它会变成下游 chunk 的尾随空串，逐遍重跑逐遍
                # 累积空行（空白漂移实证）。页本体仍保留（审计#1 前视依赖）。
                while body_lines and not body_lines[-1].strip():
                    body_lines.pop()
                # v0.9f 审计#1: 正文为空的页（整页被吞成注释区的"全注释页"、
                # 只剩 <<<CHAPTER>>> 标记的章题页）也必须进 pages_data——
                # 其注释要进重编号队列，供前页标记跨页前视消费；否则注释拿不到
                # new_num，在 _generate_output 里以原始页内号混入 note_map，
                # 与已重编号键冲突被静默覆盖（姚斯案：皮康/斯托罗斯特/威勒克 5 条蒸发）。
                if pg.idx in toc_pages:        # v0.10.3：不进重编号队列，原位保留在装配序列
                    ch.toc_chunks.append(len(_body_pages))
                    _body_pages.append(page_join.join(body_lines))  # 一行=一块，与 make_epub 切块约定对齐
                    continue
                _body_pages.append(page_join.join(body_lines))
                pages_data.append((pg.idx, page_join.join(body_lines), pg.footnotes))
            if args.renumber == "seq" and pages_data and _per_page_mode < 5:
                _renumber_chapter(pages_data, lock, quiet=(ch.title is None), toc_pages=toc_pages)
                # v0.9n19: _renumber_chapter 已把重编号后的正文回写 pages_data，
                # 章体必须用回写版，否则正文标记保留原号、与注释新号错位。
                # v0.9n20: toc 豁免块（ch.toc_chunks 记录其在 _body_pages 的位号）
                # 不在 pages_data 里，重建时必须原位保留，否则目录页整体蒸发。
                _tp = set(getattr(ch, "toc_chunks", None) or ())
                _pit = iter(pages_data)
                _new_pages = []
                for _i, _b in enumerate(_body_pages):
                    if _i in _tp:
                        _new_pages.append(_b)
                    else:
                        _new_pages.append(next(_pit)[1])
                _body_pages = _new_pages
            ch.body = page_join.join(_body_pages)
            # v0.9c 审计#6 的章题去重已下沉 _generate_output 段落级（v0.9n16）

        # v0.9n29 段级纯页码页删除：\x0c 切页产生的孤立页码段（无伴生
        # 页眉、无正文）此前漏出到成品（"人14绝对"型残留同族）。仅删
        # "全部非空行都匹配 PAGENUM_RE 且非空行数 <=2" 的页，凡删留痕。
        _kept = []
        for _pg in pages:
            _ne = [l for l in _pg.lines if l.strip()]
            if len(_ne) <= 2 and all(PAGENUM_RE.match(l.strip()) for l in _ne) and _ne:
                _log("INFO", "STG6",
                     f"段级纯页码页删除：第{_pg.idx+1}页 ({'/'.join(l.strip() for l in _ne)})")
                continue
            _kept.append(_pg)
        pages[:] = _kept
        _log("INFO", "STG ", "6/6 _generate_output")
        out_text = _generate_output(chapters, args.renumber, args.notes_title,
                                    page_join, reflow=not args.no_reflow,
                                    inline=args.inline, prof=_lang_prof)
        output = args.output or (args.input + ".clean.txt")
        with open(output, "w", encoding="utf-8") as f:
            f.write(out_text)
        print(f"输出：{output}")
        # 语言代码 sidecar 沿流水线下传（make_epub 等下游读取）
        try:
            with open(output + ".lang", "w", encoding="utf-8") as _lf:
                _lf.write(_lang_code + "\n")
        except Exception as _e:
            _log("WARN", "LANG", f".lang sidecar 写出失败: {_e}")
        # v0.9n22 章节清单转写为 make_epub 原生消费的 <输出>.toc：
        # 词干匹配（marked/prepped/clean）找到权威清单（toc_mark 落盘）
        # 即透传，make_epub 读 sidecar 优先于自提（判定单点归 toc_mark）
        # 词干变换照搬 _load_toc_pages（输入词干替换流水线中缀）
        _stem_in = os.path.splitext(args.input)[0]
        _stem_out = os.path.splitext(output)[0]
        _cands = [_stem_in.replace(".prepped", ".marked") + ".toc",
                  _stem_in.replace(".clean", ".marked") + ".toc",
                  re.sub(r"\.(prepped|marked)$", "", _stem_in) + ".marked.toc",
                  _stem_out + ".toc"]
        for _cand in dict.fromkeys(_cands):
            if os.path.exists(_cand):
                import shutil as _sh
                if _per_page_mode >= 5:   # v0.9n29c 体制标记发射（契约见规范 2026-11-19）
                    _src = open(_cand, encoding="utf-8").read()
                    open(_stem_out + ".toc", "w", encoding="utf-8").write(
                        "#mode: per-page-notes\n" + _src)
                else:
                    _sh.copyfile(_cand, _stem_out + ".toc")
                print(f"[INFO] 章节清单转写：{_cand} → {_stem_out}.toc"
                      f"{'（体制 per-page-notes）' if _per_page_mode >= 5 else ''}")
                break
        # ---- [VERIFY] 结果级校验：注释真动了没有，看文件不看过程 ----
        try:
            from collections import Counter as _Counter
            _out = open(output, encoding="utf-8").read()
            # v0.9f 审计#4: 多重集比对。旧版用子串包含判断，多条文本互为前缀的
            # 注释（如姚斯章三条"同上，第352、289、356、274页。"）丢一条也判"在"，
            # 造成假阴性（实丢 5 条只报 4 条）。现按文本出现次数核对。
            _need = _Counter()
            _meta = {}
            _tot = 0
            for _ch in chapters:
                for _nt in getattr(_ch, "footnotes", []):
                    if _nt.text:
                        _tot += 1
                        _need[_nt.text[:30]] += 1
                        _meta.setdefault(_nt.text[:30], (str(_ch.title)[:12], _nt.raw))
            _miss = []
            for _t, _n in _need.items():
                _got = _out.count(_t)
                if _got < _n:
                    _c, _r = _meta[_t]
                    _miss.append((_c, _r, _t, _n - _got))
            if _miss:
                _log("ERROR", "VRFY", f"{sum(m[3] for m in _miss)}/{_tot} 条注释文本不在输出文件中——移动失败！")
                for _c, _r, _t, _d in _miss[:10]:
                    _log("ERROR", "VRFY", f"  章[{_c}] {_r} 《{_t}…》×{_d}")
            else:
                _log("INFO", "VRFY", f"全部 {_tot} 条注释文本均在输出文件中（位置由 [GEN] 内联数背书）")
        except Exception as _e:
            _log("WARN", "VRFY", f"复读校验跳过: {_e}")
        _log("INFO", "END ", f"renumber 完成 用时={_time.time()-_t0:.1f}s 函数调用={dict(_FN_COUNT)}")

    except Exception:
        import traceback as _tb
        _log("ERROR", "CRASH", "管线异常，未写出成品。traceback:\n" + _tb.format_exc())
        raise


if __name__ == "__main__":
    main()
