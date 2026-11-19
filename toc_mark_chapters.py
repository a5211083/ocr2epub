#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
toc_mark_chapters.py — 目录识别章节并注入标记（方案B：预处理）

适用场景：正文章节标题没有「第X章」等文本特征，只能从目录识别。
工具定位「目录」区、自动提取章节标题（或读取你手抄的标题列表），
在正文中模糊回查每个标题的首次出现位置，在其上方插入标记行：

    <<<CHAPTER>>>第一章 存在与时间

之后用 renumber_footnotes.py 处理（它能识别该标记），注释即按章归集。

用法（全书零参数自动模式）：
  python toc_mark_chapters.py book.txt -o marked.txt

  手动指定（OCR 质量差、自动识别失败时用）：
  python toc_mark_chapters.py book.txt --toc-file titles.txt -o marked.txt
  python toc_mark_chapters.py book.txt --toc-pages 2 -o marked.txt

  两步流水线
  python toc_mark_chapters.py book.txt -o marked.txt
  python renumber_footnotes.py marked.txt -o final.txt --yes

说明：
  - 无 \f 时按页眉高频行 + 页码序列自动切页；
  - 自动模式：定位「目录」行 → 提取「标题/页码」「标题……页码」及
    无页码的篇名 → 只在目录之后回查，避免封面/序言误命中；
  - 找不到的标题只警告不中断，可用 --match 调阈值。
  - 多语言：目录标题词与提取规则由脚本同目录 toc_languages.json 提供
    （可用 --lang-file 换档案、--lang 代码 强制语言），维护说明见 JSON 内 _readme。
"""

import argparse
import difflib
import json
import os
import re
import sys

# Windows GBK 控制台防护：不可编码字符按替换符输出，不抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass





_LOG_LEVEL = "INFO"          # 默认只留结果与异常；--log-level DEBUG 开全量逐调用追踪
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


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
def normalize(s):
    # NFD 剥离组合音符：土耳其 İ、德文 Ü、法文 é 等变音符不影响匹配
    import unicodedata as _ud
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[-_·•.…]+", "", s)
    s = _ud.normalize("NFD", s)
    s = "".join(c for c in s if _ud.category(c) != "Mn")
    return s.lower()


PAGENUM_RE = re.compile(r"^\s*[\[\]【】()\-–—_·.]*\s*(\d{1,4})\s*[\[\]【】()\-–—_·.]*\s*$")
TOC_ENTRY_RE = re.compile(r"^(.*?)[/／]\s*(?:\d{1,4}|[ivxlcdmⅠ-Ⅻ]{1,6}|书末插页)\s*$", re.I)
# 点线字符含 ⋯(U+22EF)；点线内允许空白（"… ………"）；页码后允许粘连句点/短横
# ("……… 6."、"⋯24-"——页码引用带句点是阿尔巴尼亚语目录的常规形态）
_DOTS = "…‥․·.•⋯"
TOC_DOTS_RE = re.compile(
    r"^(.*?)[{0}][\s{0}]*\s*(?:\d{{1,4}}|[ivxlcdmⅠ-Ⅻ]{{1,6}}|书末插页)[.]?[-–—]?\s*$".format(_DOTS), re.I)
ROMAN_RE = re.compile(r"^\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+\s*$")
SENT_PUNCT = "。，、！？；：.,;:!?"
# -------------------------------------------------- 多语言目录档案（外部 JSON）
LANG_FILE_DEFAULT = "toc_languages.json"

DEFAULT_PROFILE = {
    "label": "中文",
    "toc_headings": ["目录", "目次", "目錄"],
    "min_len": 2,
    "max_len": 16,
    "sentence_punct": SENT_PUNCT,
    "reject_digit_start": True,
    "reason_reject_regex": r"[著译编【\[\]（()]",
    "extract_reject_regex": r"[【\[\]（()]",
    "author_suffix_regex": r"(著|译|编)\s*$",
    "strip_leading_regex": "",
    "strip_trailing_roman": False,
    "reject_end_punct": "",
    "pagenum_line_breaks": True,
    "skip_roman_line_regex": "",
    "colon_split_alias": True,
}


def _norm_headings(words):
    return {normalize(w) for w in words if normalize(w)}


@_traced
def load_profiles(path):
    """读语言档案 JSON → {语言代码: 档案dict}。
    v0.10：默认值唯一真相源 = JSON 的 zh 块；DEFAULT_PROFILE 仅在档案缺失/损坏时
    兜底。各语言块可只写覆盖键，合并基线来自 zh 块。"""
    builtin = {"zh": dict(DEFAULT_PROFILE,
                          _headings=_norm_headings(DEFAULT_PROFILE["toc_headings"]))}
    if not path or not os.path.exists(path):
        _log("WARN", "LANG", f"未找到语言档案 {path or LANG_FILE_DEFAULT}，回退内置中文档案")
        return builtin
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        _log("ERROR", "LANG", f"语言档案解析失败({e})，回退内置中文档案")
        return builtin
    zh_base = raw.get("zh") if isinstance(raw.get("zh"), dict) else DEFAULT_PROFILE
    profiles = {}
    for k, v in raw.items():
        if k.startswith("_") or k == "fallback" or not isinstance(v, dict):
            continue
        prof = dict(zh_base)
        if k != "zh":
            prof.update(v)
        prof["_headings"] = _norm_headings(prof.get("toc_headings") or [])
        if prof["_headings"]:
            profiles[k] = prof
    if not profiles:
        _log("WARN", "LANG", f"{path} 中没有可用语言块，回退内置中文档案")
        return builtin
    return profiles


@_traced(hot=True)
def _heading_hit(n, headings):
    """目录标题词命中：normalize 后先精确，再 0.85 模糊兜底。"""
    if n in headings:
        return True
    if not (2 <= len(n) <= 40):
        return False
    return any(difflib.SequenceMatcher(None, n, h).ratio() >= 0.85 for h in headings)




@_traced(hot=True)
def normalize_header(line):
    """页眉归一化：剥前导/行尾数字与罗马页码，使 '003 德国古典美学'、
    '从德国古典美学到自然主义 004'、'iv 导读拉康'、'1 想象界 27' 这类
    页码与页眉同行的行能跨页聚类（罗马码仅在紧邻 CJK/非 ASCII 时剥，
    避免误伤外文词；截断误判只影响模板命名，不影响正文行）。"""
    s = re.sub(r"[0-9０-９]+$", "", normalize(line))
    s = re.sub(r"^[0-9０-９]+", "", s)
    s = re.sub(r"^[ivxlcdm]{1,4}(?=[\u4e00-\u9fff])", "", s, flags=re.I)
    s = re.sub(r"(?<=[^\x00-\x7f])[ivxlcdm]{1,4}$", "", s, flags=re.I)
    return s


@_traced
def auto_split_lines(text):
    """无 \f 时按页眉/页码行自动切页。

    页界 = 全局出现 >=3 次的行（页眉模板，容忍页码同行/前导）+ 构成
    单调递增序列的独立页码行（容缺页、容小幅跳号）。页码在页顶还是
    页底无需判断：统一从该行之前断开，页眉/页码行保留在页首（删除归 renumber_footnotes，toc 只切不删）。
    返回 (page_line_lists, header_forms, num_values)；无法识别返回 None。
    """
    from collections import Counter
    lines = text.split("\n")
    cnt = Counter()
    for l in lines:
        n = normalize(l)
        f = normalize_header(l)
        if len(n) >= 4 and len(f) >= 2:
            cnt[f] += 1
    header_forms = {f for f, c in cnt.items() if c >= 3 and not re.search(r"[\[【].{0,15}[\]】].{0,30}(著|译)", f)}   # v0.9d: 作者行不当页眉

    seq_idx, num_vals, last = set(), set(), None
    for i, l in enumerate(lines):
        m = PAGENUM_RE.match(l)
        if not m:
            continue
        v = int(m.group(1))
        if v > 2000:            # 排除年份类
            continue
        if last is None or (v > last and v - last <= 5):
            seq_idx.add(i)
            num_vals.add(v)
            last = v
    if len(seq_idx) < 3:        # 独立页码不足 → 只信页眉行
        seq_idx, num_vals = set(), set()
    if not header_forms and not seq_idx:
        return None

    pages, cur = [], []
    for i, l in enumerate(lines):
        if cur and (normalize_header(l) in header_forms or i in seq_idx):
            pages.append(cur)
            cur = []
        cur.append(l)
    if cur:
        pages.append(cur)
    return pages, header_forms, num_vals


# ---------------------------------------------------------------- 标题匹配

@_traced(hot=True)
def title_match_score(line_norm, title_norm):
    if not line_norm or not title_norm:
        return 0.0
    if title_norm in line_norm:
        return 1.0
    return difflib.SequenceMatcher(None, line_norm, title_norm).ratio()


@_traced(hot=True)
def _toc_like_hits(lines):
    """一页里像目录条目的行数（标题/页码 或 标题……页码）。"""
    c = 0
    for l in lines:
        s = l.strip()
        if TOC_ENTRY_RE.match(s) or TOC_DOTS_RE.match(s):
            c += 1
    return c




import time as _time





def _toc_page_reason(nxt, prof):
    """判定一页是否像目录内容，返回 (bool, 原因码)。判定标准全部来自语言档案 prof。"""
    # v0.10b 折行归并：条目被印刷折行切断（标题段 + 孤立页码行）时先拼回再判定，
    # 与 extract_titles_auto 同一归并函数，不新增判定实现
    nxt = _merge_wrapped_toc_lines([l for l in nxt], prof)
    body = [l.strip() for l in nxt if l.strip()]
    if not body:
        return True, "空页"
    for l in body:
        if TOC_ENTRY_RE.match(l) or TOC_DOTS_RE.match(l):
            continue
        if ROMAN_RE.match(l):
            continue
        if prof["skip_roman_line_regex"] and re.match(prof["skip_roman_line_regex"], l):
            continue
        # 档案键 extra_accept_regex：行匹配即判目录行，先于长度/句读门槛。
        # 两类目录形态靠它放行——行尾页码引用（"… 6."、"⋯24-"、"51"、
        # "278. - … 280."）与点线粘连超长行（换行切断、行尾无引用）。
        # Albanian 等目录的条目页码带句点/短横，会被 reject_end_punct 误杀
        # 导致目录区腰斩。行尾引用是目录结构特征而非正文句尾；扩展多吞一页
        # 的代价远小于目录区中断——搜索起点由目录区位置另行裁定。
        if prof.get("extra_accept_regex") and re.search(prof["extra_accept_regex"], l):
            continue
        l2 = re.sub(r"^\s*\d{1,4}\s*", "", l)
        if not l2.strip():
            continue    # v0.10b 纯页码行放行：目录区夹入的页眉-页码短页不应截断目录区
        s2 = re.sub(r'[、""''「」『』]', "", l2)
        _s2t = re.sub(r"([\s．.]*[0-9０-９]{1,4})+\s*$", "", s2).strip()
        n = normalize(_s2t if _s2t else s2)
        why = []
        # v0.10d：编号小节条目（"2. Sistemi..."）折行可达 2*max_len，单行超"原预算"
        # 即拒会让整页退出目录区、后续章题全丢（书末多栏目录实证）
        _budget = (prof["max_len"] * 2 if re.match(r"^\s*\d{1,2}[.)]", s2)
                   else prof["max_len"])
        if not (prof["min_len"] <= len(n) <= _budget):
            why.append("长度%d" % len(n))
        bad = [pp for pp in prof["sentence_punct"] if pp in s2]
        if bad and not re.search(r"[0-9０-９]{1,4}\s*$", l):
            why.append("句读%s" % "".join(bad))
        if prof["reason_reject_regex"] and re.search(prof["reason_reject_regex"], l2):
            why.append("档案拒收正则")
        if prof["reject_digit_start"] and l2[:1].isdigit():
            why.append("数字开头")
        if (prof["reject_end_punct"] and s2.rstrip()[-1:] in prof["reject_end_punct"]
                and not (s2.isupper() or len(s2.split()) <= 4)):
            # v0.10c 英文运行页眉全大写且常以句点收尾（"RENAISSANCE
            # CRITICISM."），短标题行尾点是排版不是散文句尾
            why.append("行尾句读")
        if why:
            return False, "行%r: %s" % (l[:30], "+".join(why))
    return True, "%d行全通过" % len(body)


@_traced
def _toc_page_like(nxt, prof):
    return _toc_page_reason(nxt, prof)[0]


@_traced
def find_toc_region(pages, profiles, lang=None):
    """扫描语言档案里的目录标题词 → 定位目录页并确定语言 → 向后逐页扩展至正文页。
    返回 (起点, 终点, 语言)；找不到返回 None。"""
    toc_start, lang_found = None, None
    for i, lines in enumerate(pages):
        for l in lines:
            n = normalize(l).rstrip(".,;:!?")
            if not n:
                continue
            # v0.10c 先精确后模糊：CONTENTS. 曾因尾点失配 en、被 de 的 0.85
            # 模糊先捞走（Saintsbury 实证）；多形态命中时精确等值优先。
            _exact = [lk for lk, prof in profiles.items()
                      if (not lang or lk == lang) and n in prof["_headings"]]
            _pool = _exact or [lk for lk, prof in profiles.items()
                               if (not lang or lk == lang)
                               and _heading_hit(n, prof["_headings"])]
            if _pool:
                toc_start, lang_found = i, _pool[0]
                break
        if toc_start is not None:
            break
    if toc_start is None:
        return None
    toc_end = toc_start
    _rescues = 0
    while toc_end + 1 < len(pages):
        _ok = _toc_page_like(pages[toc_end + 1], profiles[lang_found])
        print(f"[REG] p{toc_end+2} {'续' if _ok else '停'}: {[l.strip()[:20] for l in pages[toc_end+1][:4]]}")
        if not _ok:
            # v0.10b 尾页救援：目录末页无页眉时与正文首页合并成一个 \f 块，
            # 整页判定被正文行否决，但页内仍剩目录条目（《德国观念论的终结》
            # 尾页含 第三节海德格尔/第二章/第三章/译后记 四案）。页内仍有
            # >=2 条目录条目形态行时吸收该页。v0.10d 改连续（上限 3 页）：
            # 书末多栏目录的栏间长行会把连续若干页都打成混合页，救一页
            # 即停丢的是后续章题（阿尔巴尼亚书末目录实证）。
            _nx = _merge_wrapped_toc_lines(pages[toc_end + 1], profiles[lang_found])
            _entries = sum(1 for _l in _nx
                           if re.search(r"[0-9０-９]{1,4}\s*$", _l.strip())
                           and len(normalize(_l)) >= profiles[lang_found]["min_len"])
            # v0.10d 双栏指纹：多栏目录页的页码列会让条目文本行与页码行分家，
            # 条目形态行数不足——此时页内 >=2 纯页码行 且 >=2 短文本行即是目录页
            # （阿尔巴尼亚书末目录 KREU VI 页实证：条目数字被栏切走后只剩文本行）
            _pagenums = sum(1 for _l in _nx if re.fullmatch(r"[0-9０-９]{1,4}", _l.strip()))
            _shorts = sum(1 for _l in _nx if 0 < len(_l.strip()) <= 45)
            if (_entries >= 2 or (_pagenums >= 2 and _shorts >= 2)) and _rescues < 8:
                print(f"[REG] 尾页救援: 吸收含 {_entries} 条目形态行的混合页")
                toc_end += 1
                _rescues += 1
                continue
            break
        toc_end += 1
    return (toc_start, toc_end, lang_found)


@_traced

def _merge_wrapped_toc_lines(lines, prof):
    """v0.10b 目录折行拼接：条目标题被印刷折行切断时，段尾行无页码、
    页码孤立在下行（"…观念论的" / "主要观念" / "122"三行一案）。仅当
    本行无尾页码、非条目/点线形态且像标题段时拼接；纯页码行或
    （短续行+纯页码行）结算。归并后走既有单点判定，不新增判定实现。"""
    out = []
    i = 0
    L = len(lines)
    digit_tail = re.compile(r"[0-9０-９]{1,4}\s*$")
    pure_num = re.compile(r"\s*[0-9０-９]{1,4}\s*$")
    while i < L:
        l = lines[i]
        if (not TOC_ENTRY_RE.match(l) and not TOC_DOTS_RE.match(l)
                and not digit_tail.search(l)
                and not ROMAN_RE.match(l)
                and not _heading_hit(normalize(l), prof["_headings"])
                and len(normalize(l)) >= prof["min_len"]):
            if i + 1 < L and pure_num.fullmatch(lines[i + 1]):
                out.append(l + lines[i + 1].strip())
                i += 2
                continue
            if (i + 1 < L and digit_tail.search(lines[i + 1])
                    and not re.match(r"^\s*第[0-9０-９一二三四五六七八九十]{1,3}[章节部分]",
                                     lines[i + 1])):
                out.append(l + re.sub(r"([\s．.]*[0-9０-９]{1,4})+\s*$",
                                      "", lines[i + 1]).strip())
                i += 2
                continue
            if (i + 2 < L
                    and len(normalize(lines[i + 1])) <= 12
                    and not digit_tail.search(lines[i + 1])
                    and not TOC_ENTRY_RE.match(lines[i + 1])
                    and pure_num.fullmatch(lines[i + 2])):
                out.append(l + lines[i + 1].strip() + lines[i + 2].strip())
                i += 3
                continue
        out.append(l)
        i += 1
    return out

def extract_titles_auto(pages, toc_start, toc_end, prof):
    """从目录区提取章标题：条目行取「条目」部分；无页码的干净短行是篇名。
    判定标准全部来自语言档案 prof（zh 默认与旧硬编码逐条等价）。"""
    titles, seen = [], set()
    def add(t):
        n = normalize(t)
        if n and n not in seen:
            seen.add(n)
            titles.append(t)
            print(f"[TIT-{len(titles):02d}] {t[:30]!r}")

    # 档案键 extract_reject_digit_start（默认 False=维持旧行为）：编号开头
    # （"1. …"）的目录条目是否拒收为标题。sq= true——本书章=KREU 块+全大写
    # 标题行，编号条目全是章内小节，提出去会把每个小节误标成章节。
    _rej_digit = prof.get("extract_reject_digit_start", False)
    started = False
    for pi in range(toc_start, toc_end + 1):
        nonmatch = 0          # v0.10：熔断按页内计，跨页累计会把多页目录中途掐断
        # v0.10b 页眉-页码短页防呆：页眉常引用当前章题（与合法标题同文），
        # 行频过滤会误杀标题；短页（非空行<=2）只在目录区作桥接出现，
        # 其内容行不作篇名来源
        _ne = [(_l.strip()) for _l in pages[pi] if _l.strip()]
        # v0.10d 精化：短页须同时满足"含纯页码行"与"内容行皆短"（<=16 字归一化）。
        # 章题页可能只有 [页码, 空, 章题] 三行（阿尔巴尼亚 KREU VI 实证），
        # 旧条件把章题页误判为页眉短页、篇名被吞。
        _sparse = (len(_ne) <= 2
                   and any(re.fullmatch(r"[0-9０-９]{1,4}", _l) for _l in _ne)
                   and all(len(normalize(_l)) <= 16 for _l in _ne
                           if not re.fullmatch(r"[0-9０-９]{1,4}", _l)))
        for l in _merge_wrapped_toc_lines(pages[pi], prof):
            s = l.strip()
            if not s:
                continue
            if not started:
                if _heading_hit(normalize(s), prof["_headings"]):
                    started = True
                continue
            if prof["skip_roman_line_regex"] and re.match(prof["skip_roman_line_regex"], s):
                continue
            if ROMAN_RE.match(s):
                break
            pm = PAGENUM_RE.match(s)
            if pm and pm.group(1) and int(pm.group(1)) > 0 \
                    and not TOC_ENTRY_RE.match(s):
                # v0.10b 跳页码行不熔断：目录末页与正文首页共享 \f 块时
                # （《德国观念论的终结》混合页实证），页首页码行直接 break
                # 会让同页剩余目录条目全丢；continue 对纯目录页无行为差异。
                continue
            m = TOC_ENTRY_RE.match(s) or TOC_DOTS_RE.match(s)
            if m:
                if _rej_digit and re.match(r"^\s*\d", s):
                    continue          # 编号条目=章内小节，非章（档案键放行才拒收）
                t = m.group(1).strip(" \t-—–:：、，。/／")
                if "/" in t or "／" in t:
                    t = re.split(r"[/／]", t)[0].strip(" \t-—–:：、，。/／")
                if prof["strip_leading_regex"]:
                    _tl = re.sub(prof["strip_leading_regex"], "", t).strip(" \t-—–:：、，。/／")
                    if _tl:
                        t = _tl
                if t:
                    if prof["colon_split_alias"] and (("：" in t) or (":" in t)):
                        parts = re.split(r"[：:]", t)
                        suf = parts[-1].strip(" \t-—–:：、，。/／")
                        if suf and suf != t and len(normalize(parts[0])) <= 14:
                            add(suf)
                        else:
                            add(t)
                    else:
                        add(t)
                    nonmatch = 0
                continue
            # 档案键 extra_accept_regex 命中的行 = 目录条目形态，不是篇名：
            # 既不提出当标题，也不计 nonmatch（sq 的 reject_end_punct 会把
            # "… 6." 判成正文句尾，三条目录行就腰斩提取）。
            if prof.get("extra_accept_regex") and re.search(prof["extra_accept_regex"], s):
                # v0.10e 守卫：extra_accept 无差别吞行会蒸发章题——被归并函数拼上
                # 尾随页码的全大写章题行（"PRONA SHOQERORE NE SOCIALIZEM92"）命中
                # "行尾数字" 模式后整行跳过（阿尔巴尼亚实证）。核心 >=15 字符且
                # 字母七成以上大写（章题全大写版式特征，条目句不满足）则不吞，落篇名。
                _core = re.sub(r"([\s．.]*[0-9０-９]{1,4})+\s*$", "", s).strip()
                _lets = [c for c in _core if c.isalpha()]
                if len(_core) >= 15 and _lets and sum(c.isupper() for c in _lets) / len(_lets) >= 0.7:
                    pass
                else:
                    continue
            # 档案键 entry_skip_regex：条目折行续段含行中页码引用（" 200. -"、
            # "310.—"）但无行尾引用，会漏进篇名分支被误提为章标题。
            if prof.get("entry_skip_regex") and re.search(prof["entry_skip_regex"], s):
                continue
            # 编号开头的行：档案显式给了 extract_reject_digit_start 时视为
            # 目录内容（章内小节），跳过且不计 nonmatch；未给该键的语言
            # 保持旧行为（落入 nonmatch 统计）。
            if _rej_digit and "extract_reject_digit_start" in prof and s[0].isdigit():
                continue
            # v0.10：目录页自己的页眉行（"xii Inhaltsverzeichnis"）是版式
            # 噪声——篇名门本就拒收它，但会落进 else 累计熔断，故显式跳过
            if _heading_hit(normalize(s), prof["_headings"]):
                continue
            s2 = re.sub(r'[、""''「」『』]', "", s)
            _t9p = re.sub(r"([\s．.]*[0-9０-９]{1,4})+\s*$", "", s).strip()
            n = normalize(_t9p if _t9p else s2)
            if (prof["min_len"] <= len(n) <= prof["max_len"]
                    and (not any(p in s2 for p in prof["sentence_punct"])
                         or re.search(r"[0-9０-９]{1,4}\s*$", s))
                    and not (prof["reject_digit_start"] and s[0].isdigit())
                    and not (prof["author_suffix_regex"]
                             and re.search(prof["author_suffix_regex"], s))
                    and not (prof["extract_reject_regex"]
                             and re.search(prof["extract_reject_regex"], s))
                    and not (prof["reject_end_punct"]
                             and s.rstrip()[-1:] in prof["reject_end_punct"]
                             and not (s.isupper() or len(s.split()) <= 4))
                    and not _heading_hit(normalize(s), prof["_headings"])):
                _t9 = re.sub(r"[\s．.]*[0-9０-９]{1,4}\s*$", "", s).strip()
                if prof["strip_trailing_roman"]:
                    # v0.10d 守卫：仅剥离"分离的"罗马数字（前临非字母），词尾普通
                    # 字母不再被当罗马数字吃掉——flags=re.I 时 VOGEL→VOGE、
                    # NDËRTIMI→NDËRTIM 一类误伤（阿尔巴尼亚书实证）
                    _t9 = re.sub(r"(?<![A-Za-zÀ-ž\u0100-\u017f])[\s．.]*"
                                 r"[IVXLCDM]{1,6}\s*$", "", _t9,
                                 flags=re.I).strip()
                if prof["strip_leading_regex"]:
                    _t9 = re.sub(prof["strip_leading_regex"], "", _t9).strip(" \t-—–:：、，。/／")
                if _t9 and not _sparse:
                    add(_t9)
                nonmatch = 0
            else:
                # v0.10d 栏头词跳过：多栏目录页页码栏的栏头（Faqe/Fage=page）
                # 不作篇名、不计熔断；词表在语言档案 toc_col_headers（sq 实证）
                if normalize(s).lower() in set(prof.get("toc_col_headers") or []):
                    continue
                nonmatch += 1
                if nonmatch >= 3:
                    break
    # v0.10i 目录条目清污：目录折行归并会把部题与章题黏成一题、把点线
    # 与作者名残留在题尾（《作为群众运动的法西斯主义》实证："小资产阶级
    # 的阶级组织一、法西斯主义独裁 ⋯3""总序 …新领域 ⋯衣俊卿"——部题黏连
    # 使 make_epub 回查正文失配、3 章漏切折入邻章）。点线起全部截掉；
    # 部题后黏章题（前缀无顿号、以 数字+、 起）拆开双入册。对所有书生效：
    # 真实章题不含点线，章号黏连形态他书目录同构。
    _clean = []
    for _t in titles:
        _t = re.sub(r"[⋯…]{1,}.*$", "", _t).strip()
        if not _t:
            continue
        _m = re.match(r"^(.{2,}?)([一二三四五六七八九十]+、.+)$", _t)
        if _m and "、" not in _m.group(1):
            _clean.append(_m.group(1).strip())
            _clean.append(_m.group(2).strip())
        else:
            _clean.append(_t)
    titles = _clean
    # v0.10f 章题形态锚：语言档案可声明章题必须匹配的形态锚（Saintsbury
    # 多级目录实证：小节裸行 "On Satire"/"Scaligerana" 被"无页码干净短行
    # 是篇名"规则整批提为章题 → make_epub 按小节行切章、全书章界错乱）。
    # 过滤后不足 2 条则整书回退不过滤（防形态锚配错把章题清零）。
    _anchor = (prof.get("chapter_title_regex") or "").strip()
    if _anchor:
        try:
            _rx = re.compile(_anchor)
            _kept = [t for t in titles if _rx.match(t)]
            _drop = [t for t in titles if not _rx.match(t)]
            if len(_kept) >= 2:
                if _drop:
                    _log("INFO", "TOC ", "章题形态锚 %r：%d→%d 条，拒收 %d 条小节行（如 %r）"
                         % (_anchor, len(titles), len(_kept), len(_drop), _drop[0][:30]))
            else:
                _log("WARN", "TOC ", "章题形态锚过滤后仅剩 %d 条（<2），回退不过滤"
                     % len(_kept))
                _kept = list(titles)
            # v0.10f 多节粘连条目截断：命中形态锚且含分节标记（§ I.…§ II.…）
            # 的超长条目截到首节，防止整行回查正文失配被 make_epub 跳章
            _final = []
            _cut = []
            for _t in _kept:
                if len(_t) > 60 and '§' in _t:
                    _head = re.split(r'\s+§\s*', _t)[0].strip()
                    if 0 < len(_head) < len(_t):
                        _cut.append((_t, _head))
                        _t = _head
                _final.append(_t)
            titles = _final
            if _cut:
                _log("INFO", "TOC ", "多节粘连条目截断 %d 条（如 %r → %r）"
                     % (len(_cut), _cut[0][0][:40], _cut[0][1][:40]))
            # v0.10g 正文章题补扫：目录区之外的正文页上，命中形态锚的独立
            # 短行视为正文自带头题的章，补入清单（Saintsbury 实证：目录区
            # 只覆盖部分卷册且混入小节，正文 BOOK I–III 的章题行不在目录
            # 里，make_epub 按残缺清单切章只剩 12 章）。去重：规范化后与
            # 已入册标题相同、或互为前后缀（裸题 vs 带说明题）则跳过。
            _norm = lambda s: re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", s).lower()
            _have = {_norm(t) for t in titles}
            _added = []
            for _pi in range(len(pages)):
                if toc_start <= _pi <= toc_end:
                    continue
                for _l in pages[_pi]:
                    _s = _l.strip()
                    if not (2 < len(_s) <= 60):
                        continue
                    if not _rx.match(_s):
                        continue
                    _ns = _norm(_s)
                    if not _ns:
                        continue
                    if any(_ns == _h or (len(_ns) >= 4 and _h.startswith(_ns))
                           or (len(_h) >= 4 and _ns.startswith(_h)) for _h in _have):
                        continue
                    _have.add(_ns)
                    _added.append(_s)
            if _added:
                _log("INFO", "TOC ", "正文章题补扫 +%d 条：%s"
                     % (len(_added), [t[:24] for t in _added[:5]]))
                titles.extend(_added)
        except re.error as _e:
            _log("WARN", "TOC ", "章题形态锚 %r 编译失败（%s），不过滤" % (_anchor, _e))
    return titles


def toc_extract_titles(pages, n_toc_pages):
    """--toc-pages 模式：从目录页提取标题：去点线、去行尾页码、去重。
    v0.9d: 排除全书出现 >=3 次的行（自动切页会把页眉/封面残片切进前几页）；
    "目次"与自动模式统一排除。"""
    from collections import Counter
    _cnt = Counter()
    for _lines in pages:
        for _line in _lines:
            _cnt[normalize(_line)] += 1
    titles, seen = [], set()
    for lines in pages[:n_toc_pages]:
        for line in lines:
            s = re.sub(r"[.·•‥․…⋯]{2,}.*$", "", line.strip())
            s = re.sub(r"[\s．.]*\d{1,4}\s*$", "", s).strip(" -—–:：、，,")
            n = normalize(s)
            if 2 <= len(n) <= 30 and not n.isdigit() and n not in seen \
                    and n not in ("目录", "目次", "目錄") and _cnt[n] < 3:
                seen.add(n)
                titles.append(s)
    return titles

def _title_hit(line, tn, threshold):
    """短标题（normalize 后 <=4 字）：整行相等（剥页码）优先；
    容差兜底：标题与作者名粘在同一行时，允许包含匹配且行宽 <= 2*len+2。
    长标题维持 0.1 的 包含/相似度 + 行宽上限。"""
    hn = normalize_header(line)
    if len(tn) <= 4:
        if hn == tn:
            return True
        ln = normalize(line)
        return tn in ln and len(ln) <= len(tn) * 2 + 2
    ln = normalize(line)
    return bool(ln) and title_match_score(ln, tn) >= threshold \
        and len(ln) <= len(tn) * 1.6 + 4



AUTHOR_RE = re.compile(r'^\s*[\[［【][^\]］】]{0,12}[\]］】][^\n]{0,30}(著|译)')



@_traced
def _short_title_with_author(pages, tn, search_from=0):
    """短标题专用：找「标题行 + 紧邻下一非空行是作者行」的位置。
    本书文章标题页版式固定为「标题 / [国]作者著 译者译」，
    可区分正文里的散词（第366页"艺术"）与真标题页（第640页"艺术"）。"""
    for pi in range(search_from, len(pages)):
        lines = pages[pi]
        for li, l in enumerate(lines):
            if normalize_header(l) != tn:
                continue
            nxt = None
            for j in range(li + 1, min(li + 4, len(lines))):
                if lines[j].strip():
                    nxt = lines[j]
                    break
            if nxt and AUTHOR_RE.match(nxt):
                _log("DEBUG", "ANCH", f"{tn!r} 锚定 p{pi+1} 作者行={nxt.strip()[:24]!r}")
                return (pi, li)
    _log("WARN", "ANCH", f"{tn!r} 无作者行锚定，退回首匹配")
    return None


@_traced
def find_chapters(pages, titles, threshold=0.8, search_from=0, exclude_pages=None,
                  multi_hit=False):
    """逐标题找首次出现页。返回 (found, toc_pages)：
    found = {页idx: [(行idx, 标题)]}，同页可多章。
    短标题优先用作者行锚定；锚定失败退回普通首匹配。
    exclude_pages：显式排除页集合（如已定位的目录区）——密度启发（toc_pages）
    要求单页命中 >= max(3, 标题数/4)，目录分散在多页时每页命中数不足，
    目录页拦不住，必须靠显式排除。"""
    found, toc_pages = {}, set()
    _hit_n = 0
    titles_norms = [(t, normalize(t)) for t in titles]
    toc_min = max(3, len(titles_norms) // 4)
    _excl = set(exclude_pages or ())
    for pi, lines in enumerate(pages):
        c = 0
        for _, tn in titles_norms:
            for l in lines:
                if _title_hit(l, tn, threshold):
                    c += 1
                    break
        if c >= toc_min:
            toc_pages.add(pi)
    _excl |= toc_pages
    # 顺序锚定：目录顺序=正文顺序。逐标题回查从上一命中页起（而非全书），
    # 封面/扉页重复的书名、丛书记事行不会把后段章节误锚到书首
    # （本书实证：书名行"EKONOMIA POLITIKE (Socializmi)"与章题
    # "EKONOMIA DHE POLITIKA NE SOCIALIZE"模糊度 0.83，越过第 1~7 章
    # 直接锚到第 1 页）。
    anchor = search_from
    for t, tn in titles_norms:
        if not tn:
            continue
        # B7-fix：目录章题带章号（"2 象征界"）而正文题名页无章号（"_2_ / 象征界 33"）
        # 时，章号形态会越过题名页命中次页运行页眉（"2 象征界 47"），章界错位一页。
        # 章号剥离形态作为并行候选，两种形态都试，取最早命中页。
        cands = [tn]
        alias = re.sub(r'^\d+', '', tn)
        if alias and alias != tn:
            cands.append(alias)
        hit = None
        for cand in cands:
            h = None
            if len(cand) <= 4:
                h = _short_title_with_author(pages, cand, anchor)
            if h is None:
                for pi, lines in enumerate(pages):
                    if pi < anchor or pi in _excl:
                        continue
                    for li, l in enumerate(lines):
                        if _title_hit(l, cand, threshold):
                            h = (pi, li)
                            break
                    if h:
                        break
            if h and (hit is None or (h[0], h[1]) < (hit[0], hit[1])):
                hit = h
        if hit:
            anchor = hit[0]
        if hit:
            _log("DEBUG", "HIT ", f"{t[:26]!r} -> 第{hit[0]+1}页 行{hit[1]}")
            _hit_n += 1
            found.setdefault(hit[0], []).append((hit[1], t))
            # v0.10h 卷内重号章全收（仅形态锚档案）：同一章题在正文后段的
            # 再次出现是下一卷的同名章（Saintsbury 卷内编号实证），补入全部
            # 命中，与 make_epub 的切章保持一致
            if multi_hit:
                _pi2, _ln2 = hit[0], hit[1]
                while True:
                    _extra = None
                    for _p2 in range(_pi2, len(pages)):
                        if exclude_pages and _p2 in exclude_pages:
                            continue
                        for _l2, _line2 in enumerate(pages[_p2]):
                            if _p2 == _pi2 and _l2 <= _ln2:
                                continue
                            if _title_hit(_line2, tn, threshold):
                                _extra = (_p2, _l2)
                                break
                        if _extra:
                            break
                    if not _extra:
                        break
                    found.setdefault(_extra[0], []).append((_extra[1], t))
                    _log("DEBUG", "HIT+", f"{t[:26]!r} -> 第{_extra[0]+1}页 行{_extra[1]}")
                    _pi2, _ln2 = _extra
                    anchor = _pi2
        else:
            _log("WARN", "MISS", f"{t[:30]!r} (tn={tn!r})")
    _log("INFO", "HIT ", f"标题命中 {_hit_n}/{len(titles_norms)}")
    return found, toc_pages



__VERSION__ = "v0.10i 1118-tocpurge"


def main():
    ap = argparse.ArgumentParser(
        description="目录识别章节 → 在正文章标题上方注入 <<<CHAPTER>>> 标记\n"
                    "默认零参数自动识别目录；识别失败再用 --toc-file / --toc-pages",
        epilog="示例：python toc_mark_chapters.py book.txt -o marked.txt",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("input", metavar="文件路径")
    ap.add_argument("-o", "--output", help="输出文件，默认 <input>.marked.txt")
    ap.add_argument("--separator", metavar="正则",
                    help="页分隔方式：默认 \\f；若用分隔行切页传该行的正则")
    ap.add_argument("--toc-file", metavar="路径",
                    help="章节标题列表文件（一行一个，按目录顺序）")
    ap.add_argument("--toc-pages", type=int, metavar="N", help="前 N 页是目录页，自动提取标题")
    ap.add_argument("--match", type=float, default=0.8,
                    help="标题模糊匹配阈值 0~1（默认 0.8）。找不到→调低；找错→调高")
    ap.add_argument("--lang-file", metavar="路径",
                    help="语言档案 JSON，默认取脚本同目录 toc_languages.json")
    ap.add_argument("--lang", metavar="代码",
                    help="强制使用档案中某种语言（如 de/en），跳过语言自动判定")
    ap.add_argument("--marker", default="<<<CHAPTER>>>",
                    help="注入的标记前缀（默认 <<<CHAPTER>>>）")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARN", "ERROR"],
                    help="日志级别：WARN=只看异常, INFO=结果+异常(默认), DEBUG=逐调用全量")
    args = ap.parse_args()
    globals()["_LOG_LEVEL"] = args.log_level

    # ===== 日志先行：tee 装好后一切输出（含版本）才进 .debug.log =====
    import sys as _sys
    import hashlib as _hl
    _dbg_path = (args.output + ".debug.log") if args.output else None
    _dbg_f = None
    if _dbg_path:
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
        import traceback as _tb
        def _crash_hook(exc_type, exc, tb, _p=_dbg_path):
            try:
                with open(_p, "a", encoding="utf-8") as _cf:
                    _cf.write("\n[%s][CRASH] 未捕获异常:\n" % _time.strftime("%H:%M:%S"))
                    _tb.print_exception(exc_type, exc, tb, file=_cf)
            finally:
                _sys.__excepthook__(exc_type, exc, tb)
        _sys.excepthook = _crash_hook
        # BANNER 直写文件头：版本+源码哈希+时间+命令行，不经 stdout，杜绝"哪份代码跑的"争议
        _h = _hl.sha256(open(__file__, "rb").read()).hexdigest()[:12]
        _dbg_f.write("BANNER v=%s hash=%s time=%s argv=%s\n" % (
            __VERSION__, _h, _time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(_sys.argv)))
        _dbg_f.flush()
    else:
        _h = "nofile"
    _log("INFO", "VER ", f"{__VERSION__} hash={_h} file={__file__}")
    _log("INFO", "IN  ", f"输入={args.input} 输出={args.output} match={args.match}")
    _t0 = _time.time()

    with open(args.input, encoding="utf-8") as f:
        text = f.read()

    header_forms, num_vals = set(), set()
    if args.separator is None and "\f" not in text:
        auto = auto_split_lines(text)
        if auto is None:
            print("未检测到 \\f，且无法从页眉/页码行推断分页，请用 --separator 指定分页行",
                  file=sys.stderr)
            sys.exit(1)
        pages, header_forms, num_vals = auto
        print(f"未检测到 \\f，按页眉/页码行自动切分为 {len(pages)} 页"
              f"（页眉模板 {len(header_forms)} 种，页码 {len(num_vals)} 个）")
    else:
        raw_pages = text.split("\f") if args.separator is None else re.compile(args.separator, re.M).split(text)
        pages = [p.split("\n") for p in raw_pages]
        print(f"共切分出 {len(pages)} 页")

    # 语言档案：默认取脚本同目录 toc_languages.json，可用 --lang-file 覆盖
    _lang_path = args.lang_file or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                LANG_FILE_DEFAULT)
    profiles = load_profiles(_lang_path)

    search_from = 0
    region = None
    if args.toc_file:
        titles = [l.strip() for l in open(args.toc_file, encoding="utf-8") if l.strip()]
    elif args.toc_pages:
        titles = toc_extract_titles(pages, args.toc_pages)
        search_from = args.toc_pages
    else:
        region = find_toc_region(pages, profiles, args.lang)
        if region is None:
            known = "；".join(f"{lk}={prof['label']}({', '.join(prof['toc_headings'][:2])})"
                              for lk, prof in profiles.items())
            print(f"未找到目录标题行。已搜索档案：{known}\n"
                  f"该书语言不在档案中时：编辑 {_lang_path} 照已有块格式新增语言；\n"
                  f"或临时用 --lang 代码 强制语言 / --toc-file 标题列表 / --toc-pages N。",
                  file=sys.stderr)
            sys.exit(1)
        titles = extract_titles_auto(pages, region[0], region[1], profiles[region[2]])
        # v0.10c 权威标题列表落盘：make_epub 不再自提（判定单点），
        # 经 renumber 转写到其输出旁后优先消费
        try:
            _tp = os.path.splitext(args.output)[0] + ".toc"
            with open(_tp, "w", encoding="utf-8") as _tf:
                # v0.10j 落盘规范化：章题剥尾点（"BOOK VI."→"BOOK VI"，判定单点内修
                # 渲染层回查失配的正路；CJK 不受影响）。登记见规范 2026-11-19。
                _tf.write("\n".join(t.rstrip(".") for t in titles) + "\n")
            print(f"章节清单 sidecar：{len(titles)} 条 → {_tp}")
        except Exception as _e:
            print(f"[WARN] titles sidecar 写入失败: {_e}", file=sys.stderr)
        search_from = region[1] + 1
        if search_from >= len(pages) * 0.5:
            # 目录在书末（扫描件常见：正文页在前、目录页压尾）：目录之后无正文，
            # 沿用 search_from 会跳过全书。改为全书回查，目录区页显式排除。
            _log("WARN", "TOC ", f"目录区位于全书后段（第{region[0]+1}~{region[1]+1}/"
                                 f"{len(pages)}页），目录之后无正文，回查范围改为全书")
            search_from = 0
        args._lang_code = region[2]
        print(f"目录区：第 {region[0] + 1}~{region[1] + 1} 页"
              f"，语言 {profiles[region[2]]['label']}({region[2]})，提取标题 {len(titles)} 条")
    if len(titles) < 3:
        print(f"标题仅 {len(titles)} 条，自动识别可能失败：请用 --toc-file 手动指定",
              file=sys.stderr)
        sys.exit(1)

    print(f"标题列表 {len(titles)} 条：")
    for t in titles:
        print(f"  · {t}")

    found, toc_pages = find_chapters(pages, titles, args.match, search_from,
                                     exclude_pages=range(region[0], region[1] + 1)
                                     if region else None,
                                     multi_hit=bool(profiles[args._lang_code].get(
                                         "chapter_title_regex")) if region else False)
    if toc_pages:
        print(f"目录页：{sorted(p + 1 for p in toc_pages)}（已排除）")

    n_marked = 0
    for pi, hits in found.items():
        for li, t in sorted(hits, key=lambda x: -x[0]):  # 倒序插入避免行号位移
            pages[pi].insert(li, args.marker + t)
            n_marked += 1
    print(f"已注入 {n_marked} 个章节标记")
    _log("INFO", "END ", f"toc 完成 用时={_time.time()-_t0:.1f}s 函数调用={dict(_FN_COUNT)}")

    # v0.9f: 页首页眉/页码行一律保留——删除归 renumber_footnotes 单点判定
    # （toc 预删会让下游三层页码判定与页眉聚类集体失锚，lacan 案实证）。

    if args.separator is not None:
        print("注意：输出统一用 \\f 分页（原分隔行不保留）")
    output = args.output or (args.input + ".marked.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\f\n".join("\n".join(l for l in pg) for pg in pages))
    print(f"输出：{output}")

    # v0.10：目录页 sidecar——renumber 据此整页豁免注释区检测，不靠密度二次猜测
    _toc_pg = None
    if region:
        _toc_pg = list(range(region[0], region[1] + 1))
    elif getattr(args, "toc_pages", None):
        _toc_pg = list(range(args.toc_pages))
    if _toc_pg is not None:
        try:
            with open(os.path.splitext(output)[0] + ".tocpages.json", "w", encoding="utf-8") as _tf:
                json.dump(_toc_pg, _tf)
            print(f"目录页 sidecar：{len(_toc_pg)} 页 → {os.path.splitext(output)[0]}.tocpages.json")
        except Exception as _e:
            _log("WARN", "TOC ", f"sidecar 写出失败: {_e}")

    _lang_out = getattr(args, "_lang_code", None) or args.lang
    if _lang_out:
        try:
            with open(output + ".lang", "w", encoding="utf-8") as _lf:
                _lf.write(_lang_out + "\n")
            print(f"语言代码 {_lang_out} → {output}.lang（供 renumber/make_epub 读取）")
        except Exception as _e:
            _log("WARN", "LANG", f".lang sidecar 写出失败: {_e}")
    print("下一步：python renumber_footnotes.py " + output + " -o final.txt --yes")


if __name__ == "__main__":
    main()
