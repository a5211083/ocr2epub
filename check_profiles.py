#!/usr/bin/env python3
"""v0.10：档案完整性校验——代码新读的键，JSON 必须已补。
扫两个脚本里所有 prof["..."] / _lang_prof[...] 字面键，
先剔除有 .get() 前置守卫的键，再断言每个键存在于 zh 块。"""
import re, json, sys, os

ROOT = os.path.dirname(os.path.abspath(__file__))
GUARDED = set()
keys = set()
for fn in ("toc_mark_chapters.py", "renumber_footnotes.py"):
    src = open(os.path.join(ROOT, fn), encoding="utf-8").read()
    for m in re.finditer(r'\bprof\.get\(\s*[\'"]([\w]+)[\'"]', src):
        GUARDED.add(m.group(1))
    for m in re.finditer(r'\bprof\[\s*[\'"]([\w]+)[\'"]\s*\]', src):
        keys.add(m.group(1))
    for m in re.finditer(r'\b_lang_prof\[\s*[\'"]([\w]+)[\'"]\s*\]', src):
        keys.add(m.group(1))
keys = {k for k in keys if k not in GUARDED and not k.startswith("_")}

raw = json.load(open(os.path.join(ROOT, "toc_languages.json"), encoding="utf-8"))
zh = raw.get("zh", {})
missing_zh = sorted(k for k in keys if k not in zh)
fail = False
if missing_zh:
    print(f"[FAIL] zh 块缺键 {missing_zh} —— 补全后才允许提交")
    fail = True
for lang in sorted(raw):
    if lang.startswith("_") or not isinstance(raw[lang], dict):
        continue
    miss = sorted(k for k in keys if k not in raw[lang] and k not in zh)
    if miss:
        print(f"[WARN] {lang} 块缺键（将继承 zh）: {miss}")
print(f"共核对 {len(keys)} 个必档案键")
sys.exit(1 if fail else 0)
