"""RSS 订阅 + 规则自动下载（追剧）。"""
import json
import re
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

import aiohttp

from . import config as C


def _text(el, *names):
    for n in names:
        x = el.find(n)
        if x is not None:
            if x.text and x.text.strip():
                return x.text.strip()
            if x.get("href"):
                return x.get("href")
    return ""


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    # 去掉命名空间，RSS / Atom 一起处理
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    items = []
    for it in list(root.iter("item")) + list(root.iter("entry")):
        title = _text(it, "title")
        link = ""
        enc = it.find("enclosure")
        if enc is not None and enc.get("url"):
            link = enc.get("url")
        for l in it.findall("link"):
            href = l.get("href") or (l.text or "").strip()
            if href.startswith("magnet:") or href.endswith(".torrent") or (l.get("type") or "").endswith("bittorrent"):
                link = href
            elif not link:
                link = href
        for tag in ("magnetURI", "magnetUri", "infohash"):
            v = _text(it, tag)
            if v:
                link = v if v.startswith("magnet:") else f"magnet:?xt=urn:btih:{v}"
        guid = _text(it, "guid", "id") or link or title
        size = 0
        if enc is not None and (enc.get("length") or "").isdigit():
            size = int(enc.get("length"))
        items.append({"title": title, "link": link, "guid": guid, "date": _text(it, "pubDate", "updated", "published"), "size": size})
    return items


class RssManager:
    def __init__(self, add_fn):
        self.add_fn = add_fn   # add_fn(link, save_path, category) -> 返回描述
        self.data = {"feeds": [], "rules": [], "seen": {}}
        try:
            self.data.update(json.loads(C.RSS_FILE.read_text("utf-8")))
        except (OSError, ValueError):
            pass
        self.items = {}        # feed_id -> 最近的条目
        self.log = []

    def save(self):
        C.RSS_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")

    # ---------- 订阅 ----------
    def add_feed(self, url, name="", interval_min=15):
        f = {"id": uuid.uuid4().hex[:8], "url": url.strip(), "name": name or url.strip(), "interval": int(interval_min),
             "last": 0, "error": ""}
        self.data["feeds"].append(f)
        self.save()
        return f

    def remove_feed(self, fid):
        self.data["feeds"] = [f for f in self.data["feeds"] if f["id"] != fid]
        self.items.pop(fid, None)
        self.save()

    def set_rule(self, rule):
        rule.setdefault("id", uuid.uuid4().hex[:8])
        rule.setdefault("enabled", True)
        self.data["rules"] = [r for r in self.data["rules"] if r["id"] != rule["id"]] + [rule]
        self.save()
        return rule

    def remove_rule(self, rid):
        self.data["rules"] = [r for r in self.data["rules"] if r["id"] != rid]
        self.save()

    @staticmethod
    def match(rule, title):
        """包含：空格分隔的关键词全部要出现（或者写正则 /.../）；排除：任意一个出现就跳过。"""
        t = title.lower()
        inc = rule.get("include", "").strip()
        exc = rule.get("exclude", "").strip()
        if inc:
            if inc.startswith("/") and inc.endswith("/") and len(inc) > 2:
                if not re.search(inc[1:-1], title, re.I):
                    return False
            elif not all(k.lower() in t for k in inc.split()):
                return False
        if exc:
            if exc.startswith("/") and exc.endswith("/") and len(exc) > 2:
                if re.search(exc[1:-1], title, re.I):
                    return False
            elif any(k.lower() in t for k in exc.split()):
                return False
        return True

    # ---------- 抓取 ----------
    async def refresh(self, session, force=False):
        now = time.time()
        for f in self.data["feeds"]:
            if not force and now - f["last"] < f["interval"] * 60:
                continue
            f["last"] = now
            try:
                async with session.get(f["url"], headers={"User-Agent": "KoiDown RSS"}, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    body = await r.read()
                items = parse_feed(body)
                self.items[f["id"]] = items[:200]
                f["error"] = ""
                if f["name"] == f["url"]:
                    try:
                        root = ET.fromstring(body)
                        t = root.find(".//title")
                        if t is not None and t.text:
                            f["name"] = t.text.strip()[:60]
                    except ET.ParseError:
                        pass
                self._apply_rules(f, items)
            except Exception as e:
                f["error"] = str(e)[:200] or type(e).__name__
        self.save()

    def _apply_rules(self, feed, items):
        seen = self.data["seen"].setdefault(feed["id"], [])
        first_time = not seen
        for it in items:
            if it["guid"] in seen or not it["link"]:
                continue
            seen.append(it["guid"])
            if first_time:
                continue   # 第一次订阅不把历史条目全下一遍
            for r in self.data["rules"]:
                if not r.get("enabled", True):
                    continue
                if r.get("feeds") and feed["id"] not in r["feeds"]:
                    continue
                if self.match(r, it["title"]):
                    try:
                        self.add_fn(it["link"], r.get("save_path") or None, r.get("category", ""))
                        self.log.insert(0, {"time": time.time(), "title": it["title"], "rule": r.get("name", "")})
                        self.log = self.log[:100]
                    except Exception as e:
                        self.log.insert(0, {"time": time.time(), "title": it["title"], "rule": f"失败：{e}"})
                    break
        del seen[:-2000]

    def view(self):
        return {
            "feeds": self.data["feeds"],
            "rules": self.data["rules"],
            "items": {k: v[:100] for k, v in self.items.items()},
            "log": self.log[:50],
        }


# ---------- 自动发现：丢一个网站地址进来，替你把 RSS 找出来 ----------
# 浏览器和各家阅读器都是先看页面 <head> 里的声明，再退回去猜常见路径，这里照做。
UA = "Mozilla/5.0 (compatible; KoiDown/1.0; +https://github.com/koi-apps/koi-bt)"
MAX_BYTES = 2 * 1024 * 1024
FEED_PATHS = ["/rss", "/rss.xml", "/feed", "/feed.xml", "/atom.xml", "/index.xml",
              "/rss/", "/feed/", "/?feed=rss2", "/feeds/posts/default"]

_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _links_in_html(html, base):
    """取 <link rel="alternate" type="application/rss+xml" href="..."> —— RSS 自动发现的标准写法。"""
    out = []
    for tag in _LINK_RE.findall(html[:400000]):
        a = {}
        for m in _ATTR_RE.finditer(tag):
            a[m.group(1).lower()] = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        if "alternate" not in a.get("rel", "").lower():
            continue
        t = a.get("type", "").lower()
        if "rss" in t or "atom" in t or "xml" in t:
            href = a.get("href", "").strip()
            if href:
                out.append((urllib.parse.urljoin(base, href), a.get("title", "").strip()))
    return out


def _feed_info(body, url, name_hint=""):
    """能解析成 RSS/Atom 就返回一张体检表，不能就返回 None。"""
    try:
        items = parse_feed(body)
    except Exception:
        return None
    if not items:
        return None
    title = name_hint
    if not title:
        try:
            root = ET.fromstring(body)
            for el in root.iter():
                if el.tag.split("}")[-1] == "title" and el.text and el.text.strip():
                    title = el.text.strip()[:60]
                    break
        except ET.ParseError:
            pass
    dl = sum(1 for i in items if i["link"].startswith("magnet:") or i["link"].lower().endswith(".torrent"))
    return {"url": url, "name": title or url, "count": len(items),
            "downloadable": dl, "sample": [i["title"][:70] for i in items[:3]]}


async def _get(session, url):
    try:
        async with session.get(url, headers={"User-Agent": UA}, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status >= 400:
                return None, r.status
            return await r.content.read(MAX_BYTES), r.status
    except Exception:
        return None, 0


async def discover(session, url):
    """返回 {ok, error, feeds:[...]}。feeds 里每条都已经验证过是真能解析的。"""
    url = url.strip()
    if not url:
        return {"ok": False, "error": "地址是空的", "feeds": []}
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    found, seen = [], set()

    async def take(u, how, name_hint=""):
        u = urllib.parse.urldefrag(u)[0]
        if u in seen or len(found) >= 10:
            return
        seen.add(u)
        body, _ = await _get(session, u)
        if body is None:
            return
        got = _feed_info(body, u, name_hint)
        if got:
            got["how"] = how
            found.append(got)

    body, status = await _get(session, url)
    if body is None:
        return {"ok": False, "feeds": [],
                "error": f"打不开这个地址（HTTP {status}）" if status else "打不开这个地址，可能是网址写错了、站点挡住了自动访问，或者要先登录"}

    seen.add(urllib.parse.urldefrag(url)[0])

    # 一、你填的本身就是 feed
    got = _feed_info(body, url)
    if got:
        got["how"] = "你填的地址本身就是订阅源"
        return {"ok": True, "feeds": [got]}

    # 二、页面 <head> 里声明的（最可靠）
    for href, title in _links_in_html(body.decode("utf-8", "replace"), url):
        await take(href, "页面里声明的", title)

    # 三、都没有就猜几个常见路径
    if not found:
        sp = urllib.parse.urlsplit(url)
        for p in FEED_PATHS:
            await take(f"{sp.scheme}://{sp.netloc}{p}", "猜到的常见路径")
            if found:
                break

    if not found:
        return {"ok": True, "feeds": [],
                "error": "这个网站没有公开的 RSS。常见原因：它压根没提供；或者要登录后在个人设置里生成专属地址"}
    return {"ok": True, "feeds": found}
