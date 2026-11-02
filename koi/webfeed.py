"""把没有 RSS 的普通网页变成订阅源。

两跳：列表页拿到一堆帖子链接 → 进每个帖子页把磁力链抠出来。

列表页那跳不用 CSS 选择器，用「URL 长相」分组：同一个列表里的帖子，
地址结构必然一样（/read.php?tid=123、/htm_data/2211/7/xxx.html），
把数字换成 # 就能聚成一类。这比 CSS 选择器稳 —— 站方改版面不改路由。

详情页那跳连规则都不用：磁力链和种子链接格式是死的，整页正则扫一遍就行。
"""
import asyncio
import html.parser
import re
import urllib.parse

import aiohttp

UA = "Mozilla/5.0 (compatible; KoiDown/1.0; +https://github.com/koi-apps/koi-bt)"
MAX_BYTES = 2 * 1024 * 1024
DETAIL_PER_ROUND = 15      # 一轮最多进几个帖子页，免得被当成刷站
DETAIL_GAP = 0.5           # 两次详情请求之间歇多久
MIN_TITLE = 2              # 标题短于这个的当导航扔掉；中文两个字就能成词，别设大了

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{32,40}[^\s\"'<>\\]*", re.I)
TORRENT_RE = re.compile(r"""["'](https?://[^"'\s]+?\.torrent(?:\?[^"'\s]*)?)["']""", re.I)
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
        out.append({
            "pattern": s,
            "count": len(titled),
            "sample": [t[:70] for _, t in titled[:3]],
            "sample_url": titled[0][0],
            "_score": score * 3 if direct else score,
        })
    out.sort(key=lambda x: x["_score"], reverse=True)
    for x in out:
        x.pop("_score", None)
    return out[:6]


async def get(session, url):
    try:
        async with session.get(url, headers={"User-Agent": UA}, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status >= 400:
                return None, r.status
            return await r.content.read(MAX_BYTES), r.status
    except Exception:
        return None, 0


def downloads_in(body, base):
    """整页扫磁力链和 .torrent，磁力链优先。"""
    text = body.decode("utf-8", "replace")
    for m in MAGNET_RE.finditer(text):
        return m.group(0)
    for m in TORRENT_RE.finditer(text):
        return urllib.parse.urljoin(base, m.group(1))
    return ""


async def fetch(session, url, pattern, known, first_time=False):
    """抓一轮。known 是已经处理过的地址，这些不再进详情页。

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

    items, budget = [], DETAIL_PER_ROUND
    for u, t in picked:
        if u in known:
            continue
        # 第一次订阅只记录、不进详情页：反正旧条目本来就不下载
        if is_download(u):                    # 列表里直接给了磁力链/种子，不用进详情页
            items.append({"title": t, "link": u, "guid": u, "date": "", "size": 0,
                          "page": u, "tried": True})
            continue
        if first_time or budget <= 0:
            # 第一次订阅：当旧条目记下就完了。额度用完：这轮先不进去，下轮接着抓
            items.append({"title": t, "link": "", "guid": u, "date": "", "size": 0,
                          "page": u, "tried": first_time})
            continue
        budget -= 1
        if budget < DETAIL_PER_ROUND - 1:
            await asyncio.sleep(DETAIL_GAP)
        d, _ = await get(session, u)
        items.append({"title": t, "link": downloads_in(d, u) if d else "",
                      "guid": u, "date": "", "size": 0, "page": u, "tried": True})
    return items, ""


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

    picked = [(u, t) for u, t in links if shape_of(u) == pattern and len(t) >= MIN_TITLE][:3]
    if not picked:
        return {"ok": False, "error": "这个模式在当前页面上一条都没匹配上"}
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
