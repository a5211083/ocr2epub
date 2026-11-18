#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 用法: python diag2.py marked.txt
import re, importlib.util, sys
spec = importlib.util.spec_from_file_location("rn", "renumber_footnotes.py")
rn = importlib.util.module_from_spec(spec); spec.loader.exec_module(rn)

# [检查0] A-pass 是否真的装在你这份文件里
src = open("renumber_footnotes.py", encoding="utf-8").read()
print("== 检查0: 'v0.9c-A' 标记在文件中:", "v0.9c-A" in src, "==")

text = open(sys.argv[1], encoding="utf-8").read()
pages = []
for i, p in enumerate(text.split("\f")):
    pg = rn.Page(i, p); pg.lines = p.split("\n")
    k = rn._detect_footnote_zone(pg.lines, 0.35, None)
    pg.body_lines = pg.lines[:k]
    zone = [l for l in pg.lines[k:] if l.strip() and not rn.SEP_LINE_RE.match(l.strip())]
    for st, e in rn._find_orphan_notes(pg.body_lines):
        zone += [l for l in pg.body_lines[st:e] if l.strip()]
    pg.footnotes = rn._parse_footnotes(zone, pg.idx)
    pages.append(pg)
chapters = rn._detect_chapters(pages, None)

def chapter_pages(keyword):
    for idx, (sp, t) in enumerate(chapters):
        if t and keyword in t:
            end = chapters[idx+1][0] if idx+1 < len(chapters) else len(pages)
            return [(pg.idx, "\n".join(pg.body_lines), list(pg.footnotes))
                    for pg in pages if sp <= pg.idx < end]
    return None

# [迪尼亚诺夫] 逐条注释对象 + A-pass 模拟
pdata = chapter_pages("演变")
rn._renumber_chapter(pdata)
print("== 迪尼亚诺夫章注释对象 ==")
dots = []
for pi, body, notes in pdata:
    for n in notes:
        print(f"  p{pi+1} style={n.style!r} num={n.num!r} new_num={n.new_num!r} raw={n.raw!r} text={n.text[:16]!r}")
        if n.style == "dot" and n.new_num in (1, 2, 3):
            dots.append(n)
body_all = rn._reflow("\n\n".join(b for _, b, _ in pdata))
paras = body_all.split("\n\n")
print("== A-pass 模拟（对 reflow 后段落跑原号正则）==")
for n in dots:
    pat = re.compile(rf"^\s*{n.num}\.(\D|$)")
    hits = [q[:18] for q in paras if pat.match(q)]
    print(f"  原号{n.num}: 命中 {len(hits)} 段 -> {hits[:2]}")

# [姚斯] 805-816 页注释与悬空标记
print("== 姚斯章 p805-816 ==")
pdata2 = chapter_pages("文学史作为文学科学的挑战")
for pi, body, notes in pdata2:
    if pi+1 in range(805, 817):
        ms = [(m.group(0), body[max(0,m.start()-25):m.start()+5].replace(chr(10),""))
              for m in re.finditer(r"[②③④⑤]", body)]
        print(f"p{pi+1} 注释:", [(n.style, n.num, n.text[:18]) for n in notes])
        if ms: print(f"   圈码标记: {ms[:4]}")