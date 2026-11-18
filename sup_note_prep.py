#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sup_note_prep.py — 上标数字注释预处理（Optional，针对文中¹²³⁴+页底"N 注释"形态的书）
用法: python sup_note_prep.py in.txt -o out.txt
规则：
1. 文中上标 ¹²³⁴⁵⁶⁷⁸⁹⁰ → [1]-[10]（上标在正文里几乎无歧义）；
2. 页首（\f 之后前 2 个非空行内）的 "N 注释文本" 行 → "[N] 注释文本"，
   仅当 N≤2 位数字、该行不是页眉残留（ toc_mark_chapters 已删页眉的前提下，
   页首数字行几乎只可能是注释首行）。跑完用 renumber --verbose 核对提取数。

v0.9g：输出目标格式由圈码改为 [N]。圈码是多字形家族（①-⑳/㉑-㉟/㊱-㉟
…五段码位），OCR 识错率随码位飙升；[N] 单一形态全流水线通用
（renumber v0.9n15 起入口归一，下游只认 bracket）。
"""
import argparse, re

SUP = {"¹": "[1]", "²": "[2]", "³": "[3]", "⁴": "[4]", "⁵": "[5]",
       "⁶": "[6]", "⁷": "[7]", "⁸": "[8]", "⁹": "[9]", "⁰": "[10]"}
# LaTeX 圈码残渣（OCR 把圈码识别成数学标记）：^{\enclose{circle}{1}} → [1]
ENCLOSE_RE = re.compile(r"\^\{\\enclose\{circle\}\{(\d{1,2})\}\}")

ap = argparse.ArgumentParser()
ap.add_argument("input"); ap.add_argument("-o", "--output", required=True)
__VERSION__ = "v0.9g-supbrk 1006"
a = ap.parse_args()
text = open(a.input, encoding="utf-8").read()
for k, v in SUP.items():
    text = text.replace(k, v)

def _enc_rep(m):
    n = int(m.group(1))
    return f"[{n}]" if 1 <= n <= 10 else m.group(0)
text = ENCLOSE_RE.sub(_enc_rep, text)

n_conv = 0
NOTE_LINE = re.compile(r"^(\d{1,2})[ 　]+(\S.*)$")
# 粘连形态：OCR 常把序号直接贴在注释文本上（1"project"一词…、2让-保罗·萨特…），
# 无空格；仅当行尾为署名注时认，防 "20世纪…" 这类正文行误伤
NOTE_GLUE = re.compile(r'^(\d{1,2})(["“‘『(\u4e00-\u9fff].*)$')
# 行尾署名注：页中夹排的"N 注释文本——译者注/编者注/原注"行（本书大量注释
# 块落在页中，页首尾窗口扫不到），行尾特征几乎无误伤
NOTE_TAIL = ("译者注", "编者注", "原注")

def convert(lines, idxs, need_tail=False):
    global n_conv
    for i in idxs:
        s = lines[i].strip()
        m = NOTE_LINE.match(s)
        if not m and s.rstrip("。．. 　)）").endswith(NOTE_TAIL):
            m = NOTE_GLUE.match(s)
        if m and (not need_tail or s.rstrip("。．. 　)）").endswith(NOTE_TAIL)) \
                and 1 <= int(m.group(1)) <= 10:
            lines[i] = f"[{int(m.group(1))}] " + m.group(2)
            n_conv += 1


pages = text.split("\f")
out = []
for pg in pages:
    lines = pg.split("\n")
    ne = [i for i, ln in enumerate(lines) if ln.strip()]
    if ne:
        # v0.9f-suptail: 页首尾数字化必须带署名注尾（译者注/编者注/原注）。
        # lacan 双页码书实证："10 导读拉康"页首页码行被误转为"⑩ 导读拉康"，
        # 页码混同尾注。页码/序号判别归 renumber（\f+链验证），sup 不认无尾数字行。
        convert(lines, ne[:2], need_tail=True)   # 页首前 2 个非空行
        convert(lines, ne[-2:], need_tail=True)  # 页尾后 2 个非空行
        # 页中夹排注释块：行首"N 文本"（含无空格粘连）且行尾为署名注
        convert(lines, [i for i in ne[2:-2]], need_tail=True)
    out.append("\n".join(lines))
open(a.output, "w", encoding="utf-8").write("\f".join(out))
print(f"上标转[N]完成；页首数字注释行转[N] {n_conv} 行 → {a.output}")
