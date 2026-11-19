#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
make_epub.py — 把整理好的 final.txt 打包成 EPUB（仅标准库，无需 pip 安装）

章节切分不靠猜：final.txt 里保留着原书目录区，脚本从目录提取章节标题
（'标题/页码' 与无页码的篇名），再到正文中按标题精确切分——不会把
'第一契机'、'（杨平）'这类行误判为章。

正文中的注释标记（[1][2]…，兼容旧成品的圈码 ①②…）与章末注释自动
建立双向跳转链接。v1.1 起成品标记统一 [N] 形态（renumber v0.9n15 入口归一）。

用法：
  python make_epub.py final.txt -o book.epub --title "西方文论经典·第三卷" --author "高建平 丁国旗"
"""

import argparse
import html
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

__VERSION__ = "v1.7 1117-multihit"

# Windows GBK 控制台防护：不可编码字符按替换符输出，不抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

TOC_ENTRY_RE = re.compile(
    r"^(.*?)(?:[/／]|\s+)\s*(?:\d{1,4}|[ivxlcdmⅠ-Ⅻ]{1,6}|书末插页)\s*$", re.I)
ROMAN_RE = re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+$")
CJK_RE = re.compile(r"[一-鿿]")
SENT_PUNCT = "。，、；：！？"

# ---------------------------------------------------------------- 语言档案
# 目录标题词 / 注释块标题 / 卷首名 / dc:language 按语言取自 toc_languages.json
# （--lang-file 可换路径；语言代码来源：--lang > <input>.lang sidecar > 内置中文默认）。
# 标题词比较走 _norm（去空白、去 -_·•.…、NFD 剥离组合音符、转小写），
# 大小写与变音符不敏感——"CONTENTS"、"İÇİNDEKİLER" 都能命中。zh 默认与旧硬编码一致。
LANG_FILE_DEFAULT = "toc_languages.json"
LANG_DEFAULTS = {
    "label": "中文",
    "toc_headings": ["目录", "目次", "目錄"],
    "notes_title": "注释",
    "epub_toc_title": "目录",
    "epub_lang": "zh-CN",
    "front_title": "卷首",
    "heading_max_len": 20,
    "heading_punct": "。，、；：！？",
    "heading_needs_cjk": True,
    "heading_starts": ["§", "【"],
    "toc_entry_needs_cjk": True,
}


def _norm(s):
    import unicodedata as _ud
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[-_·•.…]+", "", s)
    s = _ud.normalize("NFD", s)
    s = "".join(c for c in s if _ud.category(c) != "Mn")
    return s.lower()


def _load_lang_profile(lang_code, lang_file=None):
    import json as _json
    code = lang_code or "zh"
    if code == "zh":
        return dict(LANG_DEFAULTS)
    path = lang_file or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     LANG_FILE_DEFAULT)
    try:
        raw = _json.load(open(path, encoding="utf-8"))
        blk = raw.get(code)
        if isinstance(blk, dict):
            prof = dict(LANG_DEFAULTS)
            prof.update(blk)
            return prof
        print(f"语言档案中无 [{code}] 块", file=sys.stderr)
    except Exception as _e:
        print(f"语言档案读取失败（{_e}）", file=sys.stderr)
    return None


def _resolve_lang_code(args):
    if getattr(args, "lang", None):
        return args.lang
    side = args.input + ".lang"
    if os.path.exists(side):
        code = open(side, encoding="utf-8").read().strip()
        if code:
            return code
    return None


_CIRCLED = {}
for _i, _cp in enumerate(range(0x2460, 0x2474), 1):
    _CIRCLED[chr(_cp)] = _i          # ①-⑳ = 1..20
for _i, _cp in enumerate(range(0x3251, 0x3260), 21):
    _CIRCLED[chr(_cp)] = _i          # ㉑-㉟ = 21..35
for _i, _cp in enumerate(range(0x32B1, 0x32C0), 36):
    _CIRCLED[chr(_cp)] = _i          # ㊱-㊿ = 36..50
REV = {}
for _ch, _n in _CIRCLED.items():
    REV.setdefault(_n, _ch)          # v1.1 起显示统一 [N]（marker_char），表保留兼容
# 正文标记：圈码字符 或 [N]。v1.1 起成品以 [N] 为主（上游归一），圈码兼容旧 final.txt
# 组布局与 INLINE_NOTE_RE 一致：group(1)=圈码字符，group(2)=[N] 数字
# v1.6: 增 paren2 形态 (N) / N)——sq 语注释块形态（正文经上游归一仍走 [N]，
# 注释块原生 N) 形态此前解析不出注释对象、无锚（阿尔巴尼亚实证）
MARKER_RE = re.compile("([" + re.escape("".join(_CIRCLED)) + r"])|\[(\d{1,3})\]|(\d{1,3})(?:\s*[)\）])")
INLINE_NOTE_RE = re.compile(
    r"^(?:([\u2460-\u2473\u3251-\u325f\u32b1-\u32bf])|\[(\d{1,3})\]|(\d{1,3})(?:[)\）]))[ 　]")


def _marker_num(m):
    """MARKER_RE/INLINE_NOTE_RE 命中 → 注释编号。group(1)=圈码字符，group(2)=[N] 数字。"""
    if m.group(2) is not None:
        return int(m.group(2))
    if m.group(3) is not None:
        return int(m.group(3))
    return _CIRCLED[m.group(1)]


def esc(s):
    return html.escape(s, quote=False)


def marker_char(n):
    """注释回链标记：统一 [N]，与正文标记形态一致（旧圈码成品改显 [N]）。"""
    return "[%d]" % n


def is_heading(text, prof):
    """章内小节标题判定。zh 默认与旧硬编码逐条一致；西书档案关 CJK 要求、
    放大长度窗口、句读集含英文句点（标题可以含逗号）。"""
    s = text.strip()
    if prof["heading_starts"] and s.startswith(tuple(prof["heading_starts"])):
        return True
    if (len(s) <= prof["heading_max_len"]
            and (not prof["heading_needs_cjk"] or CJK_RE.search(s))
            and not any(p in s for p in prof["heading_punct"])
            and not s.startswith(("（", "("))):
        return True
    return False


def _norm_title(t):
    """比较用规范化：半角括号→全角、去空白。"""
    return t.strip().replace("(", "（").replace(")", "）").replace(" ", "")


def _bare_title(t):
    """目录条目常为「作者：标题」，正文页通常只有标题部分。"""
    if "：" in t:
        return t.split("：", 1)[1].strip()
    return t.strip()


def _match_block(block, t, bare, prof, nxt=""):
    """块是否为本章标题页：整块等标题、首行等标题、或首行/整块等裸标题。
    裸标题要求首行无句读，防止正文短句误命中。
    v1.3：标题等值命中不得来自目录条目行——目录条目剥尾码后与章题等值
    （"我们今天为什么需要导读书？/iii"），会把章起点锚到目录页、卷首目录
    整块丢失（lacan 实证）。真章题行永不以页码收尾。"""
    s = block.strip().lstrip(">")          # v1.8：注入标记残留（">总序"）剥除
    _lines = block.split("\n")
    first = _lines[0].strip().lstrip(">")
    if len(_lines) > 1:
        _lines[1] = _lines[1].lstrip(">")
    if len(_lines) > 2:
        _lines[2] = _lines[2].lstrip(">")
    # v1.4：章题页首行常是章号（"KREU III"，行频不足未成切页点时与副题同块），
    # 副题在第二行——第二行等标题同样认（阿尔巴尼亚实证： 仅 III 因此漏配）
    second = _lines[1].strip() if len(_lines) > 1 else ""
    third = _lines[2].strip() if len(_lines) > 2 else ""
    ns, nf = _norm_title(s), _norm_title(first)
    n2 = _norm_title(second)
    n3 = _norm_title(third)
    _tail_page = bool(re.search(r"(?:[/／]\s*[ivxIVX0-9０-９]{1,6}|[0-9０-９]{1,4})\s*$",
                                first))
    # v1.3-fix2：折行目录条目的标题行独立成块、页码被甩到下一块
    # （"致谢" 块 + "xxi" 块），单块守卫被骗过、章起点锚进目录页、
    # 卷首目录整块丢失（lacan 实证）。后继块若以页码收尾，同样判目录条目。
    if not _tail_page and nxt:
        _tail_page = bool(re.search(
            r"^\s*[/／]?\s*[ivxIVX0-9０-９]{1,4}\s*$", nxt.strip()))
    # v1.9 比对归一化：两侧同剥尾点（sidecar 落盘剥点/正文保留原样，
    # 比对 hygiene，非判定；CHAPTER I. 正文对 CHAPTER I 题）。
    _nt_dot = _norm_title(t).rstrip(".")
    if (ns == _nt_dot or ns.rstrip(".") == _nt_dot
            or nf == _nt_dot or nf.rstrip(".") == _nt_dot
            or n2 == _nt_dot or n2.rstrip(".") == _nt_dot
            or n3 == _nt_dot or n3.rstrip(".") == _nt_dot) and not _tail_page:
        return True


def extract_titles(blocks, prof):
    """从目录区提取章节标题（顺序）与目录区结束位置。
    兼容两种条目：'标题/页码' 与 '作者 标题 页码'（空格分隔，无斜杠）；
    无页码的篇名行（可含顿号，如「意象派、表现主义」）也收；
    目录跨页处的罗马页码行（Ⅱ/Ⅲ）跳过不收、也不终止扫描。"""
    headings = {_norm(w) for w in prof["toc_headings"]}
    start = None
    for i, b in enumerate(blocks):
        if _norm(b) in headings:
            start = i
            break
    if start is None:
        return None, 0
    titles = []
    seen_bare = set()
    region_end = start + 1
    for i in range(start + 1, len(blocks)):
        b = blocks[i]
        s = b.strip()
        if not s:
            continue
        m = TOC_ENTRY_RE.match(s)
        if m and "\n" not in b:
            t = m.group(1).strip()
            if "/" in t or "／" in t:      # "标题/作者 /页码"式条目，只留标题段
                t = re.split(r"[/／]", t)[0].strip()
            if t and (not prof["toc_entry_needs_cjk"] or CJK_RE.search(t)):
                titles.append(t)
                seen_bare.add(_bare_title(t))
                region_end = i         # 只有带页码的条目才推进搜索起点
            continue
        if (len(s) <= 22
                and (not prof["toc_entry_needs_cjk"] or CJK_RE.search(s))
                and not any(p in s for p in "。，；：！?/")
                and not s.startswith(("（", "(", "【", "["))
                and "[" not in s and "【" not in s):
            if _bare_title(s) not in seen_bare:   # 目录区之后的同名短行是正文首页，跳过
                titles.append(s)      # 无页码短行：篇名 / 德国古典美学…（不推进起点）
            continue
        if ROMAN_RE.match(s) or s.isdigit():
            region_end = i             # 罗马页码行：目录跨页，跳过但继续
            continue
        break                          # 正文样式段落：目录结束（终止块不算目录区）
    return titles, region_end



def split_chapters(blocks, titles, notes_head, search_from, pre_scan=True, prof=None,
                   pp_mode=False):
    """在目录区之后按标题精确切分。返回 (front_blocks, [chapter])，
    chapter = (title, [正文段落], [(num, text)])；正文段落不含注释块。"""
    chapters = []
    pos = search_from
    # v1.3-fix3：目录标题之后连续短块（<=45 字）为目录区，区内禁止标题匹配。
    # 无页码部题（"BOOK IV."）在目录列表与正文各有一处，先命中目录会把
    # 卷首截断、条目全丢（Saintsbury 实证）。真章题页后面必跟长正文段，
    # 连续短块必然止于目录列表尾，结构判据不会误伤。
    toc_zone_end = 0
    toc_zone_start = 10**9
    _hw = {_norm(w) for w in prof["toc_headings"]}
    _tset = {_norm(t) for t in titles}
    for _i in range(len(blocks) - 1, -1, -1):
        if _norm(blocks[_i]) in _hw:
            toc_zone_start = _i
            _j = _i + 1
            def _head_sig(_k):
                # 真章题签名：短块属标题集 且 （后随长正文段
                # 或 后随另一标题短块再后随长正文段）。
                # 单行章题（lacan/schulz）走第一档；双行章题
                # （"CHAPTER I." + "INTRODUCTORY—ERASMUS."，Saintsbury）走第二档。
                # 目录条目链全是短块且不在长正文段前，两档都不触发。
                if _norm(blocks[_k].strip()) not in _tset:
                    return False
                _n1 = blocks[_k + 1].strip() if _k + 1 < len(blocks) else ''
                if len(_n1) > 45:
                    return True
                if _norm(_n1) in _tset:
                    _n2 = blocks[_k + 2].strip() if _k + 2 < len(blocks) else ''
                    return len(_n2) > 45
                return False
            while _j < len(blocks):
                if len(blocks[_j].strip()) > 45 or _head_sig(_j):
                    break
                _j += 1
            toc_zone_end = _j
            break
    else:
        toc_zone_start = 10**9
    # 篇名页：目录与第一个目录标题正文起点之间的独立短行（目录未列篇名，
    # 如「德国古典美学」单独占一页的书，篇名不出现在目录条目里）。
    # 上限 15 个：找不到目录标题时防止把全书短行当篇名。
    _pre = 0
    for i in range(search_from if pre_scan else 0, len(blocks)):
        if not pre_scan or _pre >= 15:
            break
        s = blocks[i].strip()
        if not s:
            continue
        if any(s == t for t in titles):
            break
        if (len(s) <= 20
                and (not prof["toc_entry_needs_cjk"] or CJK_RE.search(s))
                and not any(p in s for p in SENT_PUNCT)
                and not s.startswith(("（", "(", "【", "["))
                and "[" not in s and "【" not in s):
            chapters.append((s, i))
            pos = i + 1
            _pre += 1
    # v1.7 形态锚档案：全量收集各章题的全部命中块，按位置排序后统一切章。
    # 卷内重号实证（Saintsbury）：CHAPTER I. 在 BOOK IV/V/VI 各出现一次；
    # "逐题取首次 + pos 顺序推进"会被前一题的后次命中吞掉中间题的首次
    # 命中（BOOK IV. 的后次命中把 pos 推过 CHAPTER I–V 的全部首次命中）。
    _cta = prof.get("chapter_title_regex") if prof else None
    _crx = None
    if _cta:
        try:
            _crx = re.compile(_cta)
        except re.error:
            _crx = None
    if _crx:
        _cands = []
        for _t in titles:
            _bare = _bare_title(_t)
            for _i in range(toc_zone_end, len(blocks)):
                if _match_block(blocks[_i], _t, _bare, prof,
                                blocks[_i + 1] if _i + 1 < len(blocks) else ""):
                    _cands.append((_i, _t))
        _seen_blk = set()
        for _i, _t in sorted(_cands, key=lambda x: x[0]):
            if _i in _seen_blk:
                continue
            _seen_blk.add(_i)
            chapters.append((_t, _i))
        _hit_t = {_t for _t, _ in chapters}
        # v1.7 降噪：规范化后同题的另一写法已被命中切走时不再误报（块去重所致）
        _hit_norm = {re.sub(r"\s+", "", t).rstrip(".") for t in _hit_t}
        for _t in titles:
            if _t not in _hit_t \
                    and re.sub(r"\s+", "", _t).rstrip(".") not in _hit_norm:
                print(f"  [提示] 章「{_t}」在正文中未找到（不在本文本范围内？），跳过",
                      file=sys.stderr)
    else:
        for t in titles:
            bare = _bare_title(t)
            # 强匹配（整块/首行=完整标题）与裸标题匹配一次扫描，取 pos 之后第一个命中。
            # 必须一遍完成：完整标题若只在书末重复目录出现，两遍式会先命中它把 pos 跳到书尾。
            found = None
            for i in range(pos, len(blocks)):
                # v1.5 语义修正：跳过的是目录区 [start, end) 内部，不是 end 之前的全部——
                # 书末目录时 end 贴近全书尾，旧守卫把正文块也全跳了（阿尔巴尼亚 8 章漏配实证）
                if toc_zone_start <= i < toc_zone_end:
                    continue
                if _match_block(blocks[i], t, bare, prof,
                                blocks[i + 1] if i + 1 < len(blocks) else ""):
                    found = i
                    break
            if found is None:
                print(f"  [提示] 章「{t}」在正文中未找到（不在本文本范围内？），跳过",
                      file=sys.stderr)
                continue
            chapters.append((t, found))
            pos = found + 1
    if not chapters:
        return blocks, []
    front = blocks[:chapters[0][1]]
    out = []
    for idx, (t, s) in enumerate(chapters):
        e = chapters[idx + 1][1] if idx + 1 < len(chapters) else len(blocks)
        seg = [b for b in blocks[s + 1:e] if b.strip()]
        body, notes = [], []
        k = None
        for i, b in enumerate(seg):
            first = b.split("\n", 1)[0].strip()
            if first == notes_head:
                k = i                  # 取最后一个注释块（章末注释）
        if k is not None:
            tail = seg[k + 1:]
            if tail and tail[0].strip() in ("目录", "目 录", "目　录"):
                tail = []              # 书末全目录不进正文
            lines = seg[k].split("\n")[1:]
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                mm = MARKER_RE.match(ln)
                if mm and mm.end() < len(ln) and ln[mm.end()] in " 　":
                    num, text = _marker_num(mm), ln[mm.end():].strip()
                elif mm and mm.end() == len(ln):
                    num, text = _marker_num(mm), ""
                else:
                    num, text = None, ln
                notes.append((num, text))
            body = seg[:k] + tail
        else:
            body = seg
        # 行内注释块（--inline 成品：注释紧跟引用段、以"① "开头）归入章末，
        # 与章末"注释"块同样建立双向跳转链接
        body2, inline = [], []
        for b in body:
            m = INLINE_NOTE_RE.match(b.strip())
            if m and b.strip() != m.group(0):
                inline.append((_marker_num(m),
                               b.strip()[m.end():].strip()))
            else:
                body2.append(b)
        if inline and not pp_mode:
            inline.sort(key=lambda x: x[0] if x[0] is not None else 999)
            notes = notes + inline
            body = body2
        out.append((t, body, notes))
    return front, out


# ---------------------------------------------------------------- XHTML 生成

class Linker:
    """正文标记 → 注释的链接器（每章一个）。valid 之外的编号原样保留，不产生悬空链接。"""

    def __init__(self, valid=None):
        self.first_ref = {}
        self.counts = {}
        self.valid = valid

    def link(self, escaped_text):
        def rep(m):
            n = _marker_num(m)
            if self.valid is not None and n not in self.valid:
                return m.group(0)
            k = self.counts.get(n, 0)
            self.counts[n] = k + 1
            rid = "fr%d_%d" % (n, k)
            if n not in self.first_ref:
                self.first_ref[n] = rid
            return '<a href="#fn%d" id="%s" class="noteref">%s</a>' % (n, rid, m.group(0))
        return MARKER_RE.sub(rep, escaped_text)


def chapter_xhtml(title, body, notes, notes_head, prof):
    # v1.9 续段合并（格式化职责，登记见规范 2026-11-19）：前块不以句读收尾、
    # 后块非章题/独立页码段/行内注形态 → 并入同段。不删字不改序。
    _SENT9 = tuple("。！？!?；;：:…—–")
    _mg = []
    for _b in body:
        if _mg and not _mg[-1].rstrip().endswith(_SENT9)                 and not is_heading(_b, prof)                 and not re.fullmatch(r"\s*\d{1,3}\s*", _b or "")                 and not INLINE_NOTE_RE.match(_b.strip() or ""):
            _sep = " " if re.search(r"[A-Za-z]$", _mg[-1].rstrip()) else ""
            _mg[-1] = _mg[-1] + _sep + _b
        else:
            _mg.append(_b)
    body = _mg
    lk = Linker(set(n for n, _ in notes if n is not None))
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<!DOCTYPE html>',
             '<html xmlns="http://www.w3.org/1999/xhtml" '
             'xmlns:epub="http://www.idpf.org/2007/ops" lang="%s">' % prof["epub_lang"],
             '<head><meta charset="utf-8"/><title>%s</title>'
             '<link rel="stylesheet" type="text/css" href="style.css"/></head>'
             '<body>' % esc(title),
             "<h1>%s</h1>" % esc(title)]
    for p in body:
        if is_heading(p, prof):
            parts.append("<h2>%s</h2>" % lk.link(esc(p)))
        else:
            parts.append("<p>%s</p>" % lk.link(esc(p)))
    if notes:
        parts.append('<section class="notes" epub:type="endnotes"><h2>%s</h2>'
                     % esc(notes_head))
        for num, text in notes:
            if num is None:
                parts.append('<p class="note">%s</p>' % esc(text))
                continue
            back = lk.first_ref.get(num)
            mk = ('<a href="#%s" class="noteref">%s</a>' % (back, marker_char(num))
                  if back else marker_char(num))
            parts.append('<p class="note" id="fn%d">%s %s</p>'
                         % (num, mk, lk.link(esc(text)) if False else esc(text)))
        parts.append("</section>")
    parts.append("</body></html>")
    return "\n".join(parts)


CSS = """body { font-family: serif; line-height: 1.75; margin: 0 6%; }
p { text-indent: 2em; margin: 0.4em 0; }
h1 { font-size: 1.45em; text-align: center; margin: 1em 0; }
h2 { font-size: 1.12em; margin: 1.4em 0 0.6em; }
.notes { margin-top: 2.5em; border-top: 1px solid #999; padding-top: 0.6em;
         font-size: 0.92em; }
.notes p { text-indent: 0; }
.notes h2 { border: none; margin-top: 0; }
a.noteref { color: #444; text-decoration: none; font-size: 0.85em;
            vertical-align: super; line-height: 0; }
"""

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def build_epub(path, book_title, author, front_title, notes_head,
               front, chapters, prof):
    uid = "urn:uuid:" + str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    docs = []                       # (href, xhtml, toc_label)
    if front:
        body = [p for p in front if p.strip()]
        x = chapter_xhtml(front_title, body, [], notes_head, prof)
        docs.append(("front.xhtml", x, front_title))
    for i, (t, body, notes) in enumerate(chapters, 1):
        x = chapter_xhtml(t, body, notes, notes_head, prof)
        docs.append(("chap_%03d.xhtml" % i, x, t))

    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    spine = []
    ncx_points = []
    nav_items = []
    for i, (href, _, label) in enumerate(docs):
        rid = "doc%d" % i
        manifest.append('<item id="%s" href="%s" media-type="application/xhtml+xml"/>'
                        % (rid, href))
        spine.append('<itemref idref="%s"/>' % rid)
        ncx_points.append('<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text>'
                          '</navLabel><content src="%s"/></navPoint>'
                          % (i, i + 1, esc(label), href))
        nav_items.append('<li><a href="%s">%s</a></li>' % (href, esc(label)))

    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title>%s</dc:title>
    <dc:language>%s</dc:language>%s
    <meta property="dcterms:modified">%s</meta>
  </metadata>
  <manifest>
    %s
  </manifest>
  <spine toc="ncx">
    %s
  </spine>
</package>
""" % (uid, esc(book_title), prof["epub_lang"],
       "\n    <dc:creator>%s</dc:creator>" % esc(author) if author else "",
       modified, "\n    ".join(manifest), "\n    ".join(spine))

    ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="%s"/></head>
  <docTitle><text>%s</text></docTitle>
  <navMap>
    %s
  </navMap>
</ncx>
""" % (uid, esc(book_title), "\n    ".join(ncx_points))

    nav = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="%s">
<head><meta charset="utf-8"/><title>%s</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<nav epub:type="toc"><h1>%s</h1>
<ol>
    %s
</ol>
</nav>
</body></html>
""" % (prof["epub_lang"], esc(prof["epub_toc_title"]),
       esc(prof["epub_toc_title"]), "\n    ".join(nav_items))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)   # 必须第一且不解压
        zf.writestr("META-INF/container.xml", CONTAINER)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/nav.xhtml", nav)
        zf.writestr("OEBPS/style.css", CSS)
        for href, x, _ in docs:
            zf.writestr("OEBPS/" + href, x)
    return len(docs)


def validate(path):
    """结构自检：mimetype 首位且存储、全部 XML 可解析。"""
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        assert infos[0].filename == "mimetype", "mimetype 必须第一个写入"
        assert infos[0].compress_type == zipfile.ZIP_STORED, "mimetype 必须不压缩"
        assert zf.read("mimetype") == b"application/epub+zip"
        for name in zf.namelist():
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(zf.read(name))
        bad = zf.testzip()
        assert bad is None, "损坏成员: %s" % bad
        return len(infos)


def main():
    ap = argparse.ArgumentParser(description="把 final.txt 打包成 EPUB（仅标准库）")
    ap.add_argument("input", help="renumber_footnotes 输出的 final.txt")
    ap.add_argument("-o", "--output", help="输出 .epub 路径，默认 <input>.epub")
    ap.add_argument("--title", default=None, help="书名（默认取文件名）")
    ap.add_argument("--author", default="", help="作者")
    ap.add_argument("--front-title", default=None,
                    help="目录前内容的章节名（默认取语言档案 front_title；档案为空则用书名）")
    ap.add_argument("--notes-title", default=None,
                    help="章末注释块标题（默认取语言档案 notes_title；显式传入则覆盖档案）")
    ap.add_argument("--lang", metavar="代码", default=None,
                    help="强制语言代码（如 de/en），跳过 <input>.lang sidecar 探测")
    ap.add_argument("--lang-file", metavar="路径", default=None,
                    help="语言档案 JSON，默认取脚本同目录 toc_languages.json")
    ap.add_argument("--toc-file", default=None,
                    help="章节标题清单（一行一个）。默认自动读取 <input>.toc"
                         "（renumber_footnotes_v9 生成的 sidecar），没有再回退到目录解析")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        text = f.read()
    # v1.2: renumber v0.9n16 起成品保留 \f 页界（重跑幂等的前提），分块前归一为空行
    blocks = text.replace("\f", "\n\n").split("\n\n")

    # 语言档案：代码来源 --lang > <input>.lang sidecar > 内置中文默认
    _lang_code = _resolve_lang_code(args)
    prof = _load_lang_profile(_lang_code, args.lang_file)
    if prof is None:
        print(f"语言代码「{_lang_code}」不在档案中：检查 toc_languages.json，"
              f"或改用 --lang 指定", file=sys.stderr)
        sys.exit(1)
    if not _lang_code:
        _lang_code = "zh"
    print(f"语言 {_lang_code}（{prof['label']}）：注释块={prof['notes_title']!r} "
          f"目录页={prof['epub_toc_title']!r} dc:language={prof['epub_lang']}")

    # 书末重复的全目录不参与切分：目录标题之后全是短行才认作目录（正文段落很长）
    _headings = {_norm(w) for w in prof["toc_headings"]}
    toc_zone_end = 0
    for i in range(len(blocks) - 1, -1, -1):
        if _norm(blocks[i]) in _headings:
            if all(len(blocks[j].strip()) <= 30 for j in range(i + 1, len(blocks))):
                blocks = blocks[:i]
            break

    # v1.3: sidecar 路径兼容——<input>.toc 与 <input 去扩展名>.toc 都认
    # （renumber v0.9n22 转写落 stem 式，toc_mark 直出带 .txt 全名时两种都会出现）
    toc_side = args.toc_file
    if not toc_side:
        for _c in (args.input + ".toc",
                   os.path.splitext(args.input)[0] + ".toc"):
            if os.path.exists(_c):
                toc_side = _c
                break
    from_sidecar, search_from = False, 0
    if not toc_side:
        toc_side = None
    if toc_side:
        with open(toc_side, encoding="utf-8") as f:
            titles = [l.strip() for l in f if l.strip()]
        from_sidecar = True
        if titles and titles[0].startswith("#mode:"):   # 契约见规范 2026-11-19
            _pp_mode = titles.pop(0).split(":", 1)[1].strip() == "per-page-notes"
            print(f"读取章节清单 {toc_side}：{len(titles)} 条（体制 per-page-notes）")
        else:
            _pp_mode = False
            print(f"读取章节清单 {toc_side}：{len(titles)} 条")
    else:
        titles, region_end = extract_titles(blocks, prof)
        if not titles:
            print("未找到「目录」且无章节清单，无法切分章节", file=sys.stderr)
            sys.exit(1)
        search_from = region_end + 1
        print(f"目录提取章节标题 {len(titles)} 条")

    notes_head = args.notes_title or prof["notes_title"]
    front, chapters = split_chapters(blocks, titles, notes_head,
                                     search_from, pre_scan=not from_sidecar,
                                     prof=prof, pp_mode=_pp_mode)
    if not chapters:
        print("正文中未定位到任何章节", file=sys.stderr)
        sys.exit(1)
    print(f"切出章节 {len(chapters)} 个" + (f"，卷首 {len(front)} 段" if front else ""))

    book_title = args.title or args.input.rsplit(".", 1)[0]
    front_title = args.front_title or prof["front_title"] or book_title
    out = args.output or (args.input.rsplit(".", 1)[0] + ".epub")
    n = build_epub(out, book_title, args.author, front_title,
                   notes_head, front, chapters, prof)
    files = validate(out)
    print(f"输出：{out}（{files} 个成员，结构自检通过，含 {n} 个文档）")


if __name__ == "__main__":
    main()
