"""把所有界面文字（含后端发到界面的提示）抽成模板：${...} / {...} → {0}{1}…，HTML 标签处切开。"""
import json, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
CJK = re.compile(r'[㐀-鿿＀-￯　-〿]')
out = set()

def add(seg):
    seg = seg.strip()
    if not CJK.search(seg):
        return
    # 编号占位符
    n = [0]
    def rep(m):
        r = '{%d}' % n[0]; n[0] += 1; return r
    seg = re.sub(r'\x00', rep, seg)
    seg = re.sub(r'\s+', ' ', seg).strip()
    out.add(seg)

def split_text(s):
    # 去掉 HTML 标签（标签里的属性 title/placeholder 另外处理）
    for m in re.finditer(r'(?:title|placeholder|alt)="([^"]*)"', s):
        for part in m.group(1).split('\x00\x00'):
            add(part)
    s = re.sub(r'<[^>]*>', '\x01', s)
    for part in s.split('\x01'):
        add(part)

def replace_interp(s, open_='${'):
    """把 ${...}（可嵌套）替换成 \x00。"""
    res, i = [], 0
    while i < len(s):
        if s.startswith(open_, i):
            depth, j = 1, i + len(open_)
            while j < len(s) and depth:
                if s[j] == '{': depth += 1
                elif s[j] == '}': depth -= 1
                j += 1
            res.append('\x00'); i = j
        else:
            res.append(s[i]); i += 1
    return ''.join(res)

# HTML
for f in ['static/index.html', 'static/login.html', 'static/player.html']:
    t = (ROOT / f).read_text('utf-8')
    t = re.sub(r'<script.*?</script>', '', t, flags=re.S)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S)
    t = t.replace('__NAME__', '\x00').replace('__SRC__', '\x00')
    split_text(t)

# JS：字符串和模板字符串
js = (ROOT / 'static/app.js').read_text('utf-8')
for m in re.finditer(r'`((?:[^`\\]|\\.)*)`|\'((?:[^\'\\\n]|\\.)*)\'|"((?:[^"\\\n]|\\.)*)"', js):
    s = m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3))
    if not CJK.search(s): continue
    s = replace_interp(s)
    # 模板里嵌套的模板字符串已被 ${} 吞掉；剩下按标签切
    split_text(s)

# Python：所有含中文的字符串（f-string 的 {...} 当占位符）
for f in ['koi/engine.py', 'koi/server.py', 'koi/httpdl.py', 'koi/rss.py', 'koi/updater.py', 'koi/winsys.py', 'main.py']:
    src = (ROOT / f).read_text('utf-8')
    for m in re.finditer(r'(f?)(?:"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\')', src):
        s = m.group(2) if m.group(2) is not None else m.group(3)
        if not s or not CJK.search(s): continue
        line_start = src.rfind('\n', 0, m.start()) + 1
        if src[line_start:m.start()].lstrip().startswith('#'): continue
        if src[max(0, m.start()-2):m.start()] == '""' or src[m.end():m.end()+2] == '""': continue   # 文档字符串
        if '|' in s and 'xunlei' in s: continue
        if m.group(1) == 'f':
            s = replace_interp(s, '{')
        split_text(s)

lst = sorted(out, key=lambda x: (len(x), x))
json.dump(lst, open(ROOT / 'tools/i18n_keys.json', 'w'), ensure_ascii=False, indent=0)
print(len(lst))
