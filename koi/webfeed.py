"""把没有 RSS 的普通网页变成订阅源。

两跳：列表页拿到一堆帖子链接 → 进每个帖子页把磁力链抠出来。

列表页那跳不用 CSS 选择器，用「URL 长相」分组：同一个列表里的帖子，
地址结构必然一样（/read.php?tid=123、/htm_data/2211/7/xxx.html），
把数字换成 # 就能聚成一类。这比 CSS 选择器稳 —— 站方改版面不改路由。

详情页那跳连规则都不用：磁力链和种子链接格式是死的，整页正则扫一遍就行。
"""
import asyncio
import base64
import hashlib
import html.parser
import re
import urllib.parse

import aiohttp

UA = "Mozilla/5.0 (compatible; KoiDown/1.0; +https://github.com/koi-apps/koi-bt)"
MAX_BYTES = 2 * 1024 * 1024
DETAIL_PER_ROUND = 15      # 一轮最多进几个帖子页，免得被当成刷站
CATCHUP_PER_ROUND = 40     # 「补下已有的」是用户主动点的，一次多走几个
DETAIL_GAP = 0.5           # 两次详情请求之间歇多久
MIN_TITLE = 2              # 标题短于这个的当导航扔掉；中文两个字就能成词，别设大了
MAX_LINKS_PER_PAGE = 300   # 一个帖子里最多认这么多条链接（真正加几条由订阅规则那边的上限决定）
HOP_PER_PAGE = 3           # 帖子里没有能直接下的东西时，最多跟几个外站链接去找种子
HOP_MAX_BYTES = 4 * 1024 * 1024   # 种子文件不会有这么大
HOP_TIMEOUT = 12                  # 跟一跳是「顺手试试」，不值得为它卡住整轮抓取
_TORRENT_CACHE_MAX = 300

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{32,40}[^\s\"'<>\\]*", re.I)
# 种子链接经常是相对路径（/attachment/9.torrent、down/9.torrent），所以从
# href/src 属性里取，拿到后再用 urljoin 补成绝对地址
TORRENT_RE = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"'\s<>]+?\.torrent(?:\?[^"'\s<>]*)?)["']""", re.I)
THUNDER_RE = re.compile(r"thunder://[A-Za-z0-9+/=]{8,}", re.I)
# 有的论坛只贴一串光秃秃的 info hash，不给完整磁力链。hash 就是种子的身份证，
# 自己补上 magnet: 前缀照样能下。
BTIH_HEX = re.compile(r"(?<![0-9a-zA-Z])([0-9a-fA-F]{40})(?![0-9a-zA-Z])")
# 但 40 位十六进制也可能是代码版本号、文件校验和，所以只在页面确实在讲
# 下载这件事的时候才采信，不然会给用户塞一个下不动的任务。
_DL_HINT = re.compile(r"磁力|磁鏈|磁链|種子|种子|torrent|btih|info\s*hash|哈希|雜湊|download|下載|下载", re.I)
_SCRIPTY = re.compile(r"(?is)<(script|style)\b.*?</\1>")
# 标题只剩数字和日期符号的，是页码或时间列，不是正文标题
_NUMISH = re.compile(r"^[\d\s\-:/.,年月日时分秒]+$")
_SKIP_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".zip", ".rar")


class _LinkGrab(html.parser.HTMLParser):
    """把页面里所有 <a href> 和它的文字抓出来。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        d = dict(attrs)
        href = (d.get("href") or "").strip()
        if self._href is not None:          # 碰上嵌套 <a>，先把上一个收了
            self._close()
        if href:
            self._href = href
            self._buf = []
            if d.get("title"):
                self._buf.append(d["title"])

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._close()

    def _close(self):
        if self._href is None:
            return
        self.links.append((self._href, " ".join("".join(self._buf).split())[:300]))
        self._href, self._buf = None, []

    def error(self, message):               # Python 3.9 之前的 HTMLParser 要这个
        pass


def is_download(url):
    """这个链接本身就能喂给下载器？那就不用再进详情页了。"""
    return url.startswith("magnet:") or url.lower().split("?")[0].endswith(".torrent")


def shape_of(url):
    """URL 的「长相」：数字换成 #，只留 query 的键名。同一个列表里的帖子长相相同。"""
    if is_download(url):
        # 本身就能下的，不管文件名长什么样都算同一组 —— 它们之间没有「列表」关系要认
        return "magnet:" if url.startswith("magnet:") else "直接的种子链接"
    p = urllib.parse.urlsplit(url)
    path = re.sub(r"\d+", "#", p.path)
    keys = ",".join(sorted({k for k, _ in urllib.parse.parse_qsl(p.query)}))
    return p.netloc + path + ("?" + keys if keys else "")


def links_of(body, base):
    """解析出 [(绝对地址, 链接文字)]，只留同站的网页链接。"""
    pr = _LinkGrab()
    try:
        pr.feed(body.decode("utf-8", "replace"))
        pr.close()
    except Exception:
        pass
    host = urllib.parse.urlsplit(base).netloc
    # 同一个地址常常出现两次：一个只包着图标（没文字），一个才是真标题。
    # 所以去重时不能先到先得，要留文字最长的那个。
    order, best = [], {}

    def keep(u, text):
        if u in best:
            if len(text) > len(best[u]):
                best[u] = text
        else:
            best[u] = text
            order.append(u)

    for href, text in pr.links:
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        if href.startswith("magnet:"):          # 列表页直接挂磁力链的，照单收下
            dn = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(href).query)).get("dn", "")
            keep(href, text if len(text) >= MIN_TITLE else dn[:300])
            continue
        u = urllib.parse.urldefrag(urllib.parse.urljoin(base, href))[0]
        sp = urllib.parse.urlsplit(u)
        if sp.scheme not in ("http", "https") or sp.netloc != host:
            continue
        if sp.path.lower().endswith(_SKIP_EXT):
            continue
        # 文件直链没有链接文字时，用文件名当标题，总好过空着
        if not text and is_download(u):
            text = urllib.parse.unquote(sp.path.rsplit("/", 1)[-1])
        keep(u, text)
    return [(u, best[u]) for u in order]


def _navish(titles):
    """这一组的标题是不是全是页码／日期？是的话多半是分页条或时间列。"""
    if not titles:
        return False
    return sum(1 for t in titles if _NUMISH.match(t)) >= len(titles) * 0.7


def guess_groups(links, base):
    """按长相把链接分组，猜哪一组才是「帖子列表」。"""
    self_shape = shape_of(base)
    g = {}
    for u, t in links:
        s = shape_of(u)
        if s == self_shape:              # 翻页链接，不是内容
            continue
        g.setdefault(s, []).append((u, t))
    out = []
    for s, items in g.items():
        titled = [(u, t) for u, t in items if len(t) >= MIN_TITLE]
        direct = s in ("magnet:", "直接的种子链接")
        # 普通链接要成组才像列表；直接能下的一条也算数，不用凑
        if len(titled) < (1 if direct else 3):
            continue
        avg = sum(len(t) for _, t in titled) / len(titled)
        # 光比条数会让分页（1 2 3 … 20）和表头排序链接压过正文，所以要乘标题长度
        score = len(titled) * min(avg, 40)
        nav = _navish([t for _, t in titled])
        if nav:
            score *= 0.05            # 页码／日期当标题的，沉到最底下
        if direct:
            score *= 3
        out.append({
            "pattern": s,
            "count": len(titled),
            "sample": [t[:70] for _, t in titled[:3]],
            "sample_url": titled[0][0],
            "nav": nav,
            "_score": score,
        })
    out.sort(key=lambda x: x["_score"], reverse=True)
    for x in out:
        x.pop("_score", None)
    return out[:6]


async def _read_capped(resp, limit):
    """读到上限为止。

    aiohttp 的 content.read(n) 只会把「当前缓冲区里有的」给你，不保证读满 n 字节 ——
    页面一大就会被悄悄截断，页尾的磁力链跟着一起丢。所以要自己按块读。
    """
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        buf += chunk
        if len(buf) >= limit:
            break
    return bytes(buf[:limit])


async def get(session, url):
    try:
        async with session.get(url, headers={"User-Agent": UA}, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status >= 400:
                return None, r.status
            return await _read_capped(r, MAX_BYTES), r.status
    except Exception:
        return None, 0


def _unthunder(u):
    """迅雷链接就是把真地址首尾加 AA/ZZ 再 base64，解出来多半是普通 http 地址。"""
    try:
        raw = base64.b64decode(u[len("thunder://"):] + "===").decode("utf-8", "replace")
    except Exception:
        return ""
    if raw[:2].upper() == "AA" and raw[-2:].upper() == "ZZ":
        raw = raw[2:-2]
    return raw if raw.startswith(("http://", "https://", "magnet:")) else ""


def bare_hashes(text):
    """页面里光秃秃的 info hash，补成磁力链。

    只认「这一页确实在讲下载」的情况：没有种子/磁力/下载这类字眼就不猜，
    宁可漏掉也别把版本号当种子塞给用户。script/style 里的随机串先剔掉。
    """
    body = _SCRIPTY.sub(" ", text)
    if not _DL_HINT.search(body):
        return []
    out, got = [], set()
    for m in BTIH_HEX.finditer(body):
        h = m.group(1).lower()
        if h in got or len(set(h)) <= 2:      # 重复的，以及全 0 全 f 这种占位
            continue
        got.add(h)
        out.append("magnet:?xt=urn:btih:" + h)
    return out


def _ih_of(link):
    """链接指向的那个种子的身份：同一个 hash 不管写成磁力链还是裸 hash 都算一条。"""
    m = re.search(r"btih:([0-9a-zA-Z]{32,40})", link or "")
    return m.group(1).lower() if m else (link or "")


def downloads_all(body, base, cap=0):
    """整页扫能下的东西，全部返回。

    一个帖子里挂几十条磁力链是常事（「30 部合集」这种）。以前这里只取第一条，
    于是一个合集帖永远只下第一部 —— 现在整页都要。
    顺序：磁力链、种子文件、迅雷链、光秃秃的 hash；同一个种子只留一条。
    """
    text = body.decode("utf-8", "replace")
    out, seen = [], set()

    def push(link):
        k = _ih_of(link)
        if k and k not in seen:
            seen.add(k)
            out.append(link)

    for m in MAGNET_RE.finditer(text):
        push(m.group(0))
    for m in TORRENT_RE.finditer(text):
        push(urllib.parse.urljoin(base, m.group(1)))
    for m in THUNDER_RE.finditer(text):
        got = _unthunder(m.group(0))
        if got:
            push(got)
    for mag in bare_hashes(text):
        push(mag)
    return out[:cap] if cap > 0 else out


def downloads_in(body, base):
    """只要第一条。预览、以及「一帖一链」的场景用。"""
    got = downloads_all(body, base, cap=1)
    return got[0] if got else ""


# ---------- 跟一跳取种子 ----------
# 有的帖子里一条磁力链都没有，给的是第三方种子寄存站的地址，点进去还要再提交一次表单
# 才拿得到 .torrent。这里不认任何站点专用的写法，只用两条通用规则：
#   ① 外站链接逐个访问一次，谁返回的是种子文件就要谁；
#   ② 返回的要是网页，就按 HTML 标准把页面里的表单原样提交一次，再看返回的是不是种子。
_HOP_SKIP_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp",
                 ".mp4", ".webm", ".mp3", ".woff", ".woff2", ".ttf", ".zip", ".rar", ".7z", ".pdf")
_HASHY = re.compile(r"[0-9a-fA-F]{32,64}")
# 比 40 位长的 hex 串：真正的 info hash 可能是它的头 40 位或尾 40 位
_HEXRUN = re.compile(r"(?<![0-9a-zA-Z])([0-9a-fA-F]{41,64})(?![0-9a-zA-Z])")
HASH_GUESS_MAX = 4         # 一个帖子最多拿几个猜测去种子缓存核对
TORRENT_CACHES = ["https://itorrents.org/torrent/{HASH}.torrent"]   # 按 info hash 取原种子
_TORRENT_CACHE = {}        # 磁力链 -> 种子文件原文，加任务时直接用，省得再去网上找元数据


class _FormGrab(html.parser.HTMLParser):
    """把页面里的表单扒出来：action、method、以及所有带 name 的字段。纯标准 HTML。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._cur = None

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "form":
            self._cur = {"action": (d.get("action") or "").strip(),
                         "method": (d.get("method") or "get").strip().lower(),
                         "fields": {}}
            self.forms.append(self._cur)
        elif self._cur is not None and tag in ("input", "select", "textarea"):
            name = (d.get("name") or "").strip()
            # 浏览器会把被点的那个提交按钮一起发出去，所以 submit 留着；
            # 普通按钮和文件框发不了，扔掉
            if name and (d.get("type") or "").lower() not in ("button", "reset", "file"):
                self._cur["fields"][name] = d.get("value") or ""

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None

    def error(self, message):
        pass


def _looks_torrent(data, ctype=""):
    """是不是一个种子文件。bencode 的字典以 d 开头，顶层必有 info，多数还带 announce。"""
    if not data or data[:1] != b"d":
        return False
    if "bittorrent" in (ctype or "").lower():
        return True
    head = data[:8192]
    return b"4:info" in head or b"8:announce" in head


def _magnet_of(data):
    """种子文件 → 磁力链（带名字和 tracker）。原文件留在内存里，加任务时直接喂给引擎。"""
    try:
        import libtorrent as lt
        ti = lt.torrent_info(lt.bdecode(data))
        ih = ti.info_hashes()
        if not ih.has_v1():
            return ""
        q = [("dn", ti.name())] + [("tr", tr.url) for tr in list(ti.trackers())[:8]]
    except Exception:
        return ""
    mag = f"magnet:?xt=urn:btih:{ih.v1}&" + urllib.parse.urlencode(q)
    if len(_TORRENT_CACHE) >= _TORRENT_CACHE_MAX:
        _TORRENT_CACHE.pop(next(iter(_TORRENT_CACHE)), None)
    _TORRENT_CACHE[mag] = data
    return mag


def take_torrent(link):
    """加任务时取回原种子文件（取一次就扔）。没有就返回 None，照常当磁力链下。"""
    return _TORRENT_CACHE.pop(link, None)


def hop_candidates(body, base):
    """挑出值得跟一跳的外站链接。

    排掉图片脚本样式这些不可能是种子的；地址或链接文字里带长 hash、或者写着「下载/种子」
    的排前面 —— 寄存站的地址长什么样我们不关心，只看这两个通用信号。
    """
    pr = _LinkGrab()
    try:
        pr.feed(body.decode("utf-8", "replace"))
        pr.close()
    except Exception:
        pass
    host = urllib.parse.urlsplit(base).netloc.lower()
    seen, scored = set(), []
    for href, text in pr.links:
        if href.startswith(("#", "javascript:", "mailto:", "magnet:")):
            continue
        u = urllib.parse.urldefrag(urllib.parse.urljoin(base, href))[0]
        sp = urllib.parse.urlsplit(u)
        if sp.scheme not in ("http", "https") or not sp.netloc:
            continue
        if sp.netloc.lower() == host:          # 同站的那跳前面已经扫过了
            continue
        if sp.path.lower().endswith(_HOP_SKIP_EXT):
            continue
        if u in seen:
            continue
        seen.add(u)
        score = 0
        if _HASHY.search(sp.query):
            score += 2
        if _DL_HINT.search(text or "") or _DL_HINT.search(u):
            score += 1
        scored.append((-score, len(scored), u))
    scored.sort()
    return [u for _, _, u in scored]


async def _hop_get(session, url, method="get", data=None, referer="", ua=UA):
    """跟一跳专用：拿原始字节 + Content-Type（种子是二进制，不能当网页解码）。"""
    headers = {"User-Agent": ua}
    if referer:
        headers["Referer"] = referer
    try:
        if method == "post":
            cm = session.post(url, headers=headers, data=data, allow_redirects=True,
                              timeout=aiohttp.ClientTimeout(total=HOP_TIMEOUT))
        else:
            cm = session.get(url, headers=headers, params=data or None, allow_redirects=True,
                             timeout=aiohttp.ClientTimeout(total=HOP_TIMEOUT))
        async with cm as r:
            if r.status >= 400:
                return b"", "", str(r.url)
            return await _read_capped(r, HOP_MAX_BYTES), (r.headers.get("Content-Type") or ""), str(r.url)
    except Exception:
        return b"", "", url


def _runs_in(s):
    """一段文字里所有「比 40 位长的 hex 串」的头 40 位和尾 40 位。"""
    out = []
    for m in _HEXRUN.finditer(s or ""):
        run = m.group(1).lower()
        out += [run[:40], run[-40:]]
    return out


def long_hash_guesses(text):
    """页面里比 40 位长的 hex 串，头 40 位和尾 40 位各算一个候选。

    种子寄存站常把 info hash 混在自家的参数里（前后多几个字符），光靠「正好 40 位」
    的规则就漏掉了。注意这只是猜 —— 必须去种子缓存把真种子取回来、sha1 对得上才算数，
    所以猜错不会往下载列表里塞垃圾。

    先扔掉 <script>/<style>（里面的 sha256 校验值全是干扰），再让地址里的排在正文前面
    —— 种子的 id 基本都长在链接上。
    """
    body = _SCRIPTY.sub(" ", text or "")
    in_url = []
    for m in re.finditer(r"""(?:href|src|action)\s*=\s*["']([^"'\s<>]+)["']""", body, re.I):
        in_url += _runs_in(m.group(1))
    out = []
    for c in in_url + _runs_in(body):
        if c not in out:
            out.append(c)
    return out


async def cache_torrent(session, ih):
    """拿 info hash 去公共种子缓存取原种子。sha1 对不上就不要。"""
    try:
        import libtorrent as lt
    except Exception:
        return b""
    for tpl in TORRENT_CACHES:
        # 种子缓存站对非浏览器 UA 会发一个占位的假种子（实测 4KB vs 真的 57KB），
        # 所以这里用浏览器 UA；就算拿错了下面 sha1 也会把它拦掉
        data, ctype, _ = await _hop_get(session, tpl.format(HASH=ih.upper(), hash=ih),
                                        ua="Mozilla/5.0")
        if not _looks_torrent(data, ctype):
            continue
        try:
            d = lt.bdecode(data)
            if hashlib.sha1(lt.bencode(d[b"info"])).hexdigest() != ih:
                continue
        except Exception:
            continue
        return data
    return b""


async def hop_downloads(session, body, base, cap=HOP_PER_PAGE):
    """帖子里没有能直接下的东西时，跟一跳去把种子找回来。返回磁力链列表。"""
    out = []
    # ① 先看页面里有没有藏着的 info hash（寄存站地址的参数里常常就是），
    #    拿去公共种子缓存核对，对得上就直接拿到原种子，一次请求搞定，也不用去打扰寄存站
    text = body.decode("utf-8", "replace")
    for ih in long_hash_guesses(text)[:HASH_GUESS_MAX]:
        data = await cache_torrent(session, ih)
        if data:
            mag = _magnet_of(data)
            if mag:
                out.append(mag)
                break
    # ② 还不行，再去跟外站链接
    tried = 0
    for u in ([] if out else hop_candidates(body, base)[:cap]):
        if tried:
            await asyncio.sleep(DETAIL_GAP)     # 跟一跳也要节流，别把人家站点刷崩
        tried += 1
        data, ctype, final = await _hop_get(session, u, referer=base)
        if not data:
            continue
        if _looks_torrent(data, ctype):
            mag = _magnet_of(data)
            if mag:
                out.append(mag)
            continue
        if "html" not in ctype.lower() and ctype:
            continue
        # 是网页：页面里可能还藏着能直接下的东西，先照常扫一遍
        got = downloads_all(data, final, cap=MAX_LINKS_PER_PAGE)
        if got:
            out.extend(got)
            continue
        # 再不行就按 HTML 标准把表单提交一次
        fp = _FormGrab()
        try:
            fp.feed(data.decode("utf-8", "replace"))
            fp.close()
        except Exception:
            pass
        for form in fp.forms[:2]:
            if not form["fields"]:
                continue
            action = urllib.parse.urljoin(final, form["action"] or final)
            if form["method"] != "post":
                # HTML 规范：GET 表单提交时，表单数据是「替换」掉 action 上原有的 query，
                # 不是追加。照着浏览器来，不然寄存站会把我们当成没填表单
                action = urllib.parse.urlunsplit(urllib.parse.urlsplit(action)._replace(query=""))
            await asyncio.sleep(DETAIL_GAP)
            d2, c2, f2 = await _hop_get(session, action, form["method"], form["fields"], referer=final)
            if _looks_torrent(d2, c2):
                mag = _magnet_of(d2)
                if mag:
                    out.append(mag)
                break
            if d2 and ("html" in c2.lower() or not c2):
                got = downloads_all(d2, f2, cap=MAX_LINKS_PER_PAGE)
                if got:
                    out.extend(got)
                    break
        if out:
            break
    # 同一个种子只留一条
    uniq, seen = [], set()
    for link in out:
        k = _ih_of(link)
        if k and k not in seen:
            seen.add(k)
            uniq.append(link)
    return uniq


async def fetch(session, url, pattern, known, first_time=False, budget=None):
    """抓一轮。known 是已经处理过的地址，这些不再进详情页。

    budget 是这一轮最多进几个帖子页，不给就用默认的 DETAIL_PER_ROUND；
    「补下已有的」要一次过一遍历史条目，会给一个大一点的数。
    返回 (items, error)；items 结构跟 RSS 那边的完全一样，好让规则引擎直接复用。
    """
    body, st = await get(session, url)
    if body is None:
        return [], (f"打不开列表页（HTTP {st}）" if st else "打不开列表页，可能被挡住了或要登录")

    links = links_of(body, url)
    if not links:
        return [], "这个页面里一个链接都没解析出来，可能内容是 JS 动态加载的"

    picked = [(u, t) for u, t in links if shape_of(u) == pattern and len(t) >= MIN_TITLE]
    if not picked:
        return [], "按保存的地址模式一条都没匹配上，多半是站点改版了，重新选一次模式"

    items, budget = [], (DETAIL_PER_ROUND if budget is None else int(budget))
    spent = 0
    for u, t in picked:
        if u in known:
            continue
        # 第一次订阅只记录、不进详情页：反正旧条目本来就不下载
        if is_download(u):                    # 列表里直接给了磁力链/种子，不用进详情页
            items.append({"title": t, "link": u, "links": [u], "guid": u, "date": "", "size": 0,
                          "page": u, "tried": True})
            continue
        if first_time or budget <= 0:
            # 第一次订阅：当旧条目记下就完了。额度用完：这轮先不进去，下轮接着抓
            items.append({"title": t, "link": "", "links": [], "guid": u, "date": "", "size": 0,
                          "page": u, "tried": first_time})
            continue
        budget -= 1
        if spent:
            await asyncio.sleep(DETAIL_GAP)
        spent += 1
        d, _ = await get(session, u)
        got = downloads_all(d, u, cap=MAX_LINKS_PER_PAGE) if d else []
        if not got and d:
            # 帖子里一条都没有：可能种子挂在第三方寄存站上，跟一跳试试
            got = await hop_downloads(session, d, u)
        items.append({"title": t, "link": got[0] if got else "", "links": got,
                      "guid": u, "date": "", "size": 0, "page": u, "tried": True})
    return items, ""


def _spread(seq, k):
    """均匀取 k 条来试。列表开头常年是置顶公告、版规这类没下载链接的东西，
    只看前几条会把好好的一组误判成『抓不到』。"""
    if len(seq) <= k:
        return list(seq)
    step = len(seq) / k
    return [seq[min(int(i * step), len(seq) - 1)] for i in range(k)]


async def preview(session, url, pattern=""):
    """界面上「预览」用：没给模式就返回候选分组，给了就试抓前几条看看能不能拿到磁力链。"""
    body, st = await get(session, url)
    if body is None:
        return {"ok": False, "error": f"打不开这个地址（HTTP {st}）" if st else "打不开这个地址，可能被挡住了或要登录"}

    links = links_of(body, url)
    if not pattern:
        groups = guess_groups(links, url)
        if not groups:
            return {"ok": False, "error": "没在这个页面里找到成组的链接。如果内容是滚动加载出来的，这种页面抓不了"}
        return {"ok": True, "groups": groups}

    matched = [(u, t) for u, t in links if shape_of(u) == pattern and len(t) >= MIN_TITLE]
    if not matched:
        return {"ok": False, "error": "这个模式在当前页面上一条都没匹配上"}
    picked = _spread(matched, 4)
    tried = []
    for i, (u, t) in enumerate(picked):
        if is_download(u):
            tried.append({"title": t[:70], "page": u, "link": u, "why": ""})
            continue
        if i:
            await asyncio.sleep(DETAIL_GAP)
        d, dst = await get(session, u)
        link = downloads_in(d, u) if d else ""
        tried.append({"title": t[:70], "page": u, "link": link,
                      "why": "" if link else (f"帖子页打不开（HTTP {dst}）" if d is None else "帖子页里没有磁力链或种子")})
    return {"ok": True, "tried": tried, "hit": sum(1 for x in tried if x["link"])}
