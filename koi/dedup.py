"""查重：别把已经有的东西再下一遍。

名字的写法千奇百怪 —— 同一部片在不同站点上可能叫 `kss55.cc@APKH118`、
`APKH-118.mp4`、`APKH118_4K60FPS`。所以比的不是名字本身，是从名字里榨出来的「钥匙」：

1. 编号（番号 / 货号）：名字里 `字母+数字` 那一段，去掉分隔符就是钥匙。
   这是最硬的一把 —— 分辨率、字幕组、站点前缀怎么加都不影响它。
2. 没有编号的（电影、合辑、剧集）：退回「名字去掉噪声后整串比对」。

查重目录用的是「文件系统上现在有什么」，不是下载历史 —— 用户手动删掉的片子
就该能重新下，而历史记录会一直拦着他。
"""
import os
import re
import time

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".rmvb", ".mpg", ".mpeg", ".webm", ".iso")

# 站点前缀：kss55.cc@xxx、[nyaa.si] xxx、【字幕组】xxx
_SITE = re.compile(r"(?:^|\s)[0-9a-z\-]+\.(?:cc|com|net|org|xyz|me|tv|co|io|info|top|club|site|online|vip)@?", re.I)
_BRACKET = re.compile(r"[\[\(【（][^\]\)】）]{0,40}[\]\)】）]")
# 清晰度、编码、片源这类说明词：它们不参与「是不是同一部」的判断
_NOISE = {
    "1080p", "1080", "720p", "720", "480p", "2160p", "4k", "8k", "uhd", "hd", "fhd", "sd", "hq",
    "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "dts", "flac", "mp3", "opus",
    "mp4", "mkv", "avi", "wmv", "mov", "m4v", "ts", "flv", "rmvb", "webm", "iso",
    "web", "webrip", "webdl", "bluray", "bdrip", "brrip", "hdrip", "dvdrip", "remux", "rip",
    "hdr", "dv", "sdr", "repack", "proper", "uncut", "full", "complete",
    "中文字幕", "无码", "無碼", "有码", "有碼", "破解", "流出", "合集", "精选", "精選",
}
_NOISEY = re.compile(r"^(?:\d+fps|\d+k\d*fps?|\d+bit|\d{3,4}p|v\d|cd\d|disc\d|part\d|\d{1,2}of\d{1,2})$")
_CODEISH = re.compile(r"^(?:[0-9a-z]*?)([a-z]+)(\d{2,8})$")
# 编号的字母头最多就这么长（实测 400 多部片库：1~5 个字母全覆盖）。
# 放宽到更长会把「演员名+数字」这种结尾当编号用，同一个人的不同片子会互相判重。
MAX_CODE_LETTERS = 6
# 这些字母开头的「编号」是通用修饰词，两部完全不同的片都可能有 ep02、vol3，不能当身份
_CODE_STOP = {"ep", "episode", "vol", "volume", "cd", "disc", "disk", "part", "pt", "season",
              "s", "e", "no", "ch", "chapter", "sp", "extra", "ver", "v", "track", "page", "p"}


def _tokens(name):
    """切成小写词，顺手把站点前缀、方括号注释、清晰度这类噪声扔掉。"""
    s = name.lower()
    root, ext = os.path.splitext(s)         # 去扩展名（只看尾部，别把片名里的点吃了）
    if ext in VIDEO_EXT or re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        s = root
    s = _SITE.sub(" ", s)
    s = _BRACKET.sub(" ", s)
    toks = [t for t in re.split(r"[^0-9a-z\u4e00-\u9fff]+", s) if t]
    return [t for t in toks if t not in _NOISE and not _NOISEY.match(t)]


def code_of(name):
    """名字里的编号。`APKH-118` / `apkh118` / `FC2PPV-4792509` 都能归到同一把钥匙。

    只在词内部和「字母词 + 紧跟的数字词」之间找，不跨词硬拼 ——
    否则 `The.Office.S01E01` 会被拼成编号，第 1 集和第 2 集就成了同一部。
    一个名字里可能有好几段（`4k60fps`、`ep02`），取数字位数最多的那段。
    """
    toks = _tokens(name)
    two = lambda j: 0 <= j < len(toks) and re.fullmatch(r"\d{2}", toks[j])
    cands = []
    for i, t in enumerate(toks):
        m = _CODEISH.match(t)
        if m and len(m.group(1)) <= MAX_CODE_LETTERS and not (len(m.group(2)) == 2 and two(i + 1)):
            cands.append((m.group(1), m.group(2)))
        # 分隔符把编号切开了：apkh-118 → ["apkh", "118"]，fc2ppv-4792509 → ["fc2ppv", "4792509"]
        if i + 1 < len(toks) and re.fullmatch(r"\d{2,8}", toks[i + 1]):
            tail = re.search(r"([a-z]+)$", t)
            if tail and len(tail.group(1)) > MAX_CODE_LETTERS:
                tail = None
            # 片商名 + 日期：erito.25.01.03、FapHouse.26.07.17 —— 这是日期不是编号，
            # 当编号用会把同一个片商的几十部片全判成重复
            if tail and not (len(toks[i + 1]) == 2 and two(i + 2)):
                cands.append((tail.group(1), toks[i + 1]))
    best = ("", "")
    for letters, digits in cands:
        if letters in _CODE_STOP:
            continue
        if len(letters) == 1 and len(digits) < 3:   # n0523 算，合集 A + 11 不算
            continue
        if len(digits) > len(best[1]):
            best = (letters, digits)
    return best[0] + best[1]


def keys_of(name):
    """这个名字的所有钥匙。两个名字只要有一把钥匙对上，就当是同一个东西。"""
    toks = _tokens(name)
    if not toks:
        return set()
    keys = {"n:" + "".join(toks)}
    c = code_of(name)
    if c:
        keys.add("c:" + c)
        # 有的站写 ABP00904，有的写 ABP904 —— 前导零不算区别
        head = re.match(r"([a-z]+)0*(\d+)$", c)
        if head:
            keys.add("c:" + head.group(1) + head.group(2))
    return keys


class MediaIndex:
    """查重目录里现有的东西，按钥匙建索引。dirs_fn 每次刷新时重新问一遍目录列表。"""

    def __init__(self, dirs_fn, ttl=300, max_entries=200000):
        self.dirs_fn = dirs_fn
        self.ttl = ttl
        self.max_entries = max_entries
        self.index = {}        # key -> 第一个撞上的路径（扫目录来的）
        self.extra = {}        # 运行中手动记进来的，扫目录不会把它冲掉
        self.built = 0
        self.scanned = 0

    def refresh(self, force=False):
        if not force and self.built and time.time() - self.built < self.ttl:
            return
        idx, n = {}, 0
        for d in self.dirs_fn() or []:
            if not d or not os.path.isdir(d):
                continue
            for root, subs, files in os.walk(d):
                subs[:] = [x for x in subs if not x.startswith(("@", "."))]   # 群晖的 @eaDir 之类
                for nm in list(subs) + files:
                    if nm.startswith("."):
                        continue
                    if nm in files and os.path.splitext(nm)[1].lower() not in VIDEO_EXT:
                        continue
                    n += 1
                    for k in keys_of(nm):
                        idx.setdefault(k, os.path.join(root, nm))
                if n > self.max_entries:
                    break
        self.index, self.built, self.scanned = idx, time.time(), n

    def find(self, name):
        """已经有了就返回那个路径，没有返回 None。"""
        self.refresh()
        for k in keys_of(name):
            hit = self.index.get(k) or self.extra.get(k)
            if hit:
                return hit
        return None

    def add(self, name, path=""):
        """记一笔「这个已经算有了」。同一轮里加进来的重复条目靠它互相挡住。"""
        for k in keys_of(name):
            self.extra.setdefault(k, path or name)
