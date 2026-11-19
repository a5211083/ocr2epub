#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_run.py — 批量跑 ocr2epub 流水线
用法: python batch_run.py 输入目录 输出目录

每本 book.txt 的处理链：
  toc_mark_chapters → [sup_note_prep] → renumber_footnotes → make_epub
- book.txt 可配 book.titles.txt（章节标题，一行一个）走 --toc-file；
- sup_note_prep 仅当书中出现上标数字标记（¹²³⁴ 等）时自动插入；
- 单本失败打印原因并继续，结尾汇总成败清单（不再整批中断）；
- 输出 <书名>.marked.txt、<书名>.clean.txt、成品 <书名>.epub。
"""
import glob, locale, os, subprocess, sys

# Windows GBK 控制台打印 UTF-8 输出会乱码，对齐到系统首选编码
try:
    sys.stdout.reconfigure(encoding=locale.getpreferredencoding(False),
                           errors="replace")
except Exception:
    pass

src, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
here = os.path.dirname(os.path.abspath(__file__))
py = sys.executable
SUP_CHARS = "¹²³⁴⁵⁶⁷⁸⁹⁰"

import re as _re
for _s in ["toc_mark_chapters.py", "sup_note_prep.py",
           "renumber_footnotes.py", "make_epub.py"]:
    try:
        _src = open(os.path.join(here, _s), encoding="utf-8").read()
        _m = _re.search(r'__VERSION__ = "([^"]+)"', _src)
        print(f"[VER] {_s}: {_m.group(1) if _m else '（无版本戳）'}")
    except FileNotFoundError:
        print(f"[VER] {_s}: 缺失！")

failed, ok = [], []
for f in sorted(glob.glob(os.path.join(src, "*.txt"))):
    name = os.path.basename(f)
    if name.endswith((".titles.txt", ".marked.txt", ".prepped.txt", ".clean.txt")):
        continue
    stem = name[:-4]
    # v0.9n30-b 元数据旁车：<stem>.meta 每行 key=value（key ∈ title/creator/
    # contributor，# 起行为注释）→ 透传 make_epub，缺省书名仍取文件茎
    _meta = {}
    _mp = os.path.join(src, stem + ".meta")
    if os.path.exists(_mp):
        for _l in open(_mp, encoding="utf-8"):
            _l = _l.strip()
            if "=" in _l and not _l.startswith("#"):
                _k, _v = _l.split("=", 1)
                _meta[_k.strip()] = _v.strip()
    marked = os.path.join(dst, name + ".marked.txt")
    prepped = os.path.join(dst, name + ".prepped.txt")
    clean = os.path.join(dst, stem + ".clean.txt")
    epub = os.path.join(dst, stem + ".epub")
    titles = os.path.join(src, stem + ".titles.txt")
    print(f"===== {name} =====", flush=True)
    try:
        steps = []
        cmd1 = [os.path.join(here, "toc_mark_chapters.py"), f, "-o", marked]
        if os.path.exists(titles):
            cmd1 += ["--toc-file", titles]
        steps.append(("toc", cmd1))
        cur = marked
        _head = open(f, encoding="utf-8").read()
        if any(c in _head for c in SUP_CHARS) or "\\enclose{circle}" in _head:
            steps.append(("prep", [os.path.join(here, "sup_note_prep.py"), marked, "-o", prepped]))
            cur = prepped
        steps.append(("renumber", [os.path.join(here, "renumber_footnotes.py"),
                                   cur, "-o", clean, "--yes"]))
        _margs = []
        if _meta:
            _margs = ["--title", _meta.get("title", stem)]
            if _meta.get("creator"):
                _margs += ["--author", _meta["creator"]]
            if _meta.get("contributor"):
                _margs += ["--contributor", _meta["contributor"]]
        else:
            _margs = ["--title", stem]
        steps.append(("epub", [os.path.join(here, "make_epub.py"), clean,
                               "-o", epub] + _margs))
        for tag, cmd in steps:
            r = subprocess.run([py] + cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            last = " | ".join((r.stdout + r.stderr).strip().splitlines()[-2:])
            print(f"  [{tag}] rc={r.returncode} {last[:160]}", flush=True)
            if r.returncode != 0:
                raise RuntimeError(f"{tag} 失败 rc={r.returncode}")
        g = subprocess.run([py, os.path.join(here, "conservation_check.py"), f, clean],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        _gl = (g.stdout or g.stderr).strip().splitlines()
        print(f"  [gate] {_gl[0] if _gl else '(无输出)'}", flush=True)
        if g.returncode != 0:
            for _line in _gl[1:8]:
                print(f"         {_line}", flush=True)
            raise RuntimeError("守恒门 FAIL：正文缺失，禁止交付")
        ok.append(name)
    except Exception as e:
        print(f"  [跳过] {e}", flush=True)
        failed.append((name, str(e)))

print(f"\n===== 汇总：成功 {len(ok)} 本，失败 {len(failed)} 本 =====")
for n, w in failed:
    print(f"  失败: {n} — {w}")
for n in ok:
    print(f"  成功: {n}")
