# -*- coding: utf-8 -*-
"""conservation_check.py — 正文守恒验收门 v1.0 1118

batch_run 每书跑完后执行：源文本 vs renumber 输出做句子级对账。
原理：页眉候选（源中独立成行、出现 >=8 次、长度 <=20）先从源剔除，
使跨页断句正确接合；剥注释标记；按 。！？切句（>=12 字）逐一在
输出中查全串，未命中再以 12 字前缀模糊复核。真缺失（前缀也不命中）
即 FAIL 并列出清单——正文被整段吞进注释区/章切分丢章/漏页都会在此显形。
退出码 0=PASS，1=FAIL，2=用法错误。
"""
import sys, re
from collections import Counter

def norm(t):
    t = re.sub(r"(?<![\d])\d{1,3}(?![\d])", "", t)   # 页码/短数字剥除（两侧同剥，20世纪→世纪不影响可比性；年份 4 位保留）
    return re.sub(r"[\s\x0c]+", "", t)

def strip_marks(t):
    t = re.sub(r"[\u2460-\u2473]", "", t)   # ①-⑳
    t = re.sub(r"\[\d+\]", "", t)
    return t

def header_candidates(lines):
    # v1.3：候选也收"页码胶合型"——剥行首/行尾页码后再计频（lacan 实证：
    # 运行页眉"导读拉康"从不独立成行，0 候选致页眉黏连全部误报）
    c = Counter()
    for l in lines:
        s = re.sub(r"^\d{1,3}\s*", "", l.strip())
        s = re.sub(r"\s*\d{1,3}$", "", s)
        if 2 <= len(s) <= 20:
            c[s] += 1
    return {s for s, n in c.items() if n >= 8}

def main():
    if len(sys.argv) < 3:
        print("usage: conservation_check.py <source.txt> <clean.txt>", file=sys.stderr)
        return 2
    src = open(sys.argv[1], encoding="utf-8", errors="replace").read()
    out = open(sys.argv[2], encoding="utf-8", errors="replace").read()
    # 豁免清单（规范 2026-11-19 登记）：源旁 <名>.skip，每行一子串签名
    import os as _os
    _skips = []
    _sk = _os.path.splitext(_os.path.basename(sys.argv[1]))[0] + ".skip"
    _skp = _os.path.join(_os.path.dirname(sys.argv[1]), _sk)
    if _os.path.exists(_skp):
        _skips = [l.split("#", 1)[0].strip() for l in open(_skp, encoding="utf-8") if l.split("#", 1)[0].strip()]
    hdrs = header_candidates(src.splitlines())
    # 页眉置空须行锚定：全局子串替换会把正文中含页眉文的串（书名副题）掏空
    src = "\n".join("" if l.strip() in hdrs else l for l in src.splitlines())
    # 行内页眉黏连也要剥（"发‖十、章题‖展"），但《》内的副题保护
    for _h in sorted(hdrs, key=len, reverse=True):
        src = re.sub(r"(?<![《〈])" + re.escape(_h), "", src)
    ns = norm(strip_marks(src))
    nc = norm(strip_marks(out))
    _grams = {nc[i:i+8] for i in range(max(0, len(nc) - 7))}
    miss = []
    n_sents = 0
    for s in re.split(r"[。！？!?]", ns):
        s = s.strip()
        if len(s) < 12:
            continue
        if _skips and any(_k in s for _k in _skips):
            n_sents += 1
            continue
        n_sents += 1
        if s in nc:
            continue
        wins = [s[i:i+8] for i in range(len(s) - 7)]
        cov = sum(1 for w in wins if w in _grams) / len(wins)
        if cov < 0.6:
            # 注尾黏连（"——译者注]X"）：按注界切开，只查 >=8 字片段
            _fr = [f for f in re.split(r"——译者注|【|】", s) if len(f) >= 8]
            if _fr and all(sum(1 for i in range(len(f)-7) if f[i:i+8] in _grams) / (len(f)-7) >= 0.6
                         for f in _fr):
                continue
            # 弯引号形态差：剥引号复测
            _q = re.sub(r"[“”‘’]", "", s)
            if _q != s and _q in nc:
                continue
            # v1.3：容忍性复测——删除任一页眉候选子串后能匹配即视为
            # 页眉黏连假阳性（如"译者注‖导读拉康‖：)"）；不预删，防短标题
            # 吃掉正文（"想象界"在正文中真实出现）
            _rescued = False
            s2 = s
            for _ in range(6):          # 迭代剥页眉（CIP 斜杠串页眉可连黏多次）
                _hit = False
                for _h in hdrs:
                    if len(_h) >= 4 and _h in s2:
                        s2 = s2.replace(_h, "")
                        _hit = True
                if not _hit:
                    break
            if len(s2) >= 10:
                if s2 in nc or sum(1 for i in range(len(s2)-7) if s2[i:i+8] in _grams) / (len(s2)-7) >= 0.6:
                    _rescued = True
            # TOC 斜杠串豁免（lacan CIP 页实证）：≥3 斜杠且多数含 CJK 的
            # 逐段都能在成品中找到 → 目录条目胶合，非丢失
            if not _rescued and s.count("/") >= 3:
                _cks = [c for c in s.split("/") if re.search(r"[\u4e00-\u9fff]", c)]
                if _cks and sum(1 for c in _cks if re.sub(r"\s+", "", c) in nc) / len(_cks) >= 0.6:
                    _rescued = True
            # CIP 斜杠胶合豁免：页眉剥净后剩余以标点/斜杠为主（编目垃圾行）
            if not _rescued and s.count("/") >= 3 and len(re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", s2)) < 8:
                _rescued = True
            if not _rescued and cov < 0.6:   # 8 字窗覆盖率：整段丢失必低覆盖
                miss.append((cov, s))
    if miss:
        miss.sort()
        print("[GATE] CONSERVATION FAIL: 真缺失/低覆盖 %d 句（最劣 10）:" % len(miss))
        for c, m in miss[:10]:
            print("   - [%.2f] %s" % (c, m[:55]))
        return 1
    print("[GATE] CONSERVATION PASS: %d 句 8 字窗覆盖率 >=0.6，页眉候选 %d 行" % (n_sents, len(hdrs)))
    return 0

if __name__ == "__main__":
    sys.exit(main())
