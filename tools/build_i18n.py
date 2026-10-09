"""重新生成繁体词典：所有界面文字 + 英文词典里的 key → OpenCC s2twp（台湾用词）。改了界面文字后跑一下。"""
import json, os, subprocess, sys
from pathlib import Path
import opencc
ROOT = Path(__file__).resolve().parent.parent
subprocess.run([sys.executable, str(ROOT / 'tools/extract_i18n.py')], check=True, stdout=subprocess.DEVNULL)
cc = opencc.OpenCC('s2twp')
keys = set(json.load(open(ROOT / 'tools/i18n_keys.json'))) | set(json.load(open(ROOT / 'static/i18n/en.json')))
tw = {k: cc.convert(k) for k in keys if cc.convert(k) != k}
chars = {}
for line in open(os.path.join(os.path.dirname(opencc.__file__), 'dictionary', 'STCharacters.txt'), encoding='utf-8'):
    a, b = line.rstrip('\n').split('\t')
    if b.split(' ')[0] != a:
        chars[a] = b.split(' ')[0]
json.dump({"phrases": tw, "chars": chars}, open(ROOT / 'static/i18n/zh-TW.json', 'w'), ensure_ascii=False, separators=(',', ':'))
print('zh-TW phrases', len(tw))
