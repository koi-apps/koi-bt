"""RSS 订阅 + 规则自动下载（追剧）。"""
import asyncio
import json
import re
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

import aiohttp

from . import config as C
from . import webfeed


MAX_TRIES = 3          # 一个帖子进去看过但没拿到链接，最多再给它两次机会


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
    def __init__(self, add_fn, settings=None, notify=None):
        # add_fn(link, save_path, category)：加成功返回空串，已经有了就返回跳过的原因
        self.add_fn = add_fn
        self.settings = settings if settings is not None else {}
        self.notify = notify or (lambda *a, **k: None)
        self.data = {"feeds": [], "rules": [], "seen": {}, "log": [], "tries": {}}
        try:
            self.data.update(json.loads(C.RSS_FILE.read_text("utf-8")))
        except (OSError, ValueError):
            pass
        self.items = {}        # feed_id -> 最近的条目

    # 下载记录以前是纯内存的，KOI BT 一重启就归零，用户看到的永远是 0 条。
    # 挂进 data 里跟着 save() 走，重启还在。
    @property
    def log(self):
        return self.data.setdefault("log", [])

    @log.setter
    def log(self, v):
        self.data["log"] = v

    def save(self):
        C.RSS_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")

    # ---------- 订阅 ----------
    def add_feed(self, url, name="", interval_min=15, kind="rss", pattern=""):
        f = {"id": uuid.uuid4().hex[:8], "url": url.strip(), "name": name or url.strip(), "interval": int(interval_min),
             "last": 0, "error": "", "type": kind, "pattern": pattern}
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
            if f.get("type") == "web":
                await self._refresh_web(session, f)
                continue
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

    async def catchup(self, session, f):
        """把这个源现在列表里已有的条目也过一遍规则。

        首次订阅是只记账不下载的（不然一订阅就灌几十个任务），但用户往往
        就是冲着「现在这些」来的，所以留个手动补抓的口子。
        """
        self.data["seen"][f["id"]] = []
        if f.get("type") == "web":
            # 补抓是用户主动点的，一次多走几个帖子页，免得他点七八遍
            items, err = await webfeed.fetch(session, f["url"], f.get("pattern", ""), set(),
                                             first_time=False, budget=webfeed.CATCHUP_PER_ROUND)
            f["error"] = err
        else:
            items, err = [], ""
            try:
                async with session.get(f["url"], headers={"User-Agent": "KoiDown RSS"},
                                       timeout=aiohttp.ClientTimeout(total=30)) as r:
                    items = parse_feed(await r.read())
                f["error"] = ""
            except Exception as e:
                f["error"] = str(e)[:200] or type(e).__name__
        if items:
            self.items[f["id"]] = items[:200]
            self._apply_rules(f, items, force=True)
        self.save()
        return len([i for i in items if i["link"]])

    async def _refresh_web(self, session, f):
        """网页订阅：列表页 → 帖子页 → 磁力链。去重交给 _apply_rules，这里只管抓。"""
        known = set(self.data["seen"].get(f["id"], []))
        items, err = await webfeed.fetch(session, f["url"], f.get("pattern", ""), known,
                                         first_time=not known)
        f["error"] = err
        if items:
            old = {i["guid"]: i for i in self.items.get(f["id"], [])}
            for it in items:                      # 已经抓到过链接的，别被这轮的空值盖掉
                if not it["link"] and old.get(it["guid"], {}).get("link"):
                    it["link"] = old[it["guid"]]["link"]
            self.items[f["id"]] = items[:200]
            self._apply_rules(f, items)

    def _apply_rules(self, feed, items, force=False):
        seen = self.data["seen"].setdefault(feed["id"], [])
        first_time = not seen and not force
        tries = self.data.setdefault("tries", {}).setdefault(feed["id"], {})
        for it in items:
            if it["guid"] in seen:
                continue
            if not it["link"]:
                # tried=True 表示确实进去看过、里面就是没有。
                # 但「这一次没拿到」不等于「永远拿不到」—— 网络抽风、种子缓存临时挂了
                # 都会这样，一次就死心会把本该下的帖子永久漏掉。试够 MAX_TRIES 次才记死。
                # 没有 tried 标记的是这轮没排上队（或本来就是 RSS 的空条目），留着下轮再说。
                if it.get("tried"):
                    n = tries.get(it["guid"], 0) + 1
                    if n >= MAX_TRIES:
                        tries.pop(it["guid"], None)
                        seen.append(it["guid"])
                    else:
                        tries[it["guid"]] = n
                continue
            tries.pop(it["guid"], None)
            seen.append(it["guid"])
            if first_time:
                continue   # 第一次订阅不把历史条目全下一遍
            for r in self.data["rules"]:
                if not r.get("enabled", True):
                    continue
                if r.get("feeds") and feed["id"] not in r["feeds"]:
                    continue
                if self.match(r, it["title"]):
                    self._download(it, r)
                    break
        del seen[:-2000]
        if len(tries) > 500:                      # 别让重试计数无限长
            for k in list(tries)[:-500]:
                tries.pop(k, None)

    def _download(self, it, rule):
        """把一个条目里的链接都加进去。

        一个帖子里挂着几十条磁力链是常事（「30 部合集」这种），以前只加第一条，
        于是一个合集帖永远只下第一部。现在整帖都加，已经有的那些由 add_fn 挡掉。
        """
        cap = int(self.settings.get("rss_max_per_item", 50) or 0)
        links = it.get("links") or ([it["link"]] if it.get("link") else [])
        if cap > 0 and len(links) > cap:
            links, cut = links[:cap], len(links) - cap
        else:
            cut = 0
        added, skipped, errs = 0, 0, []
        for link in links:
            try:
                why = self.add_fn(link, rule.get("save_path") or None, rule.get("category", ""))
                if why:
                    skipped += 1
                else:
                    added += 1
            except Exception as e:
                errs.append(str(e))
        note = ""
        if len(links) > 1 or skipped or errs:
            note = f"{len(links)} 条：加 {added}"
            if skipped:
                note += f"，跳过 {skipped}（已经有了）"
            if cut:
                note += f"，超出上限没加 {cut}"
        if errs:
            note = (note + "，" if note else "") + f"失败 {len(errs)}：{errs[0][:60]}"
        self.log.insert(0, {"time": time.time(), "title": it["title"], "rule": rule.get("name", ""), "note": note})
        self.log = self.log[:100]
        if added:
            self.notify("info", "订阅自动下载", f"{it['title'][:40]}（{added} 条）")

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

    found, seen, failed = [], set(), []

    async def take(u, how, name_hint="", note_fail=False):
        """note_fail=True 的是页面明确声明过的地址，抓不动要记下来告诉用户，别说成「没有 RSS」。"""
        u = urllib.parse.urldefrag(u)[0]
        if u in seen or len(found) >= 10:
            return
        seen.add(u)
        body, st = await _get(session, u)
        got = _feed_info(body, u, name_hint) if body is not None else None
        if got:
            got["how"] = how
            found.append(got)
        elif note_fail:
            failed.append((u, st if body is None else -1))

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
        await take(href, "页面里声明的", title, note_fail=True)

    # 三、都没有就猜几个常见路径（慢一点打，免得被当成刷接口）
    if not found:
        sp = urllib.parse.urlsplit(url)
        for i, p in enumerate(FEED_PATHS):
            if i:
                await asyncio.sleep(0.25)
            await take(f"{sp.scheme}://{sp.netloc}{p}", "猜到的常见路径")
            if found:
                break

    if not found:
        if failed:
            u, st = failed[0]
            why = f"对方返回 HTTP {st}" if st > 0 else ("内容不是 RSS" if st == -1 else "连不上")
            return {"ok": True, "feeds": [],
                    "error": f"这个页面声明了订阅源 {u}，但抓不下来（{why}）。多半是站点在限流或挡自动访问，隔一会儿再点一次试试"}
        return {"ok": True, "feeds": [],
                "error": "这个网站没有公开的 RSS。常见原因：它压根没提供；或者要登录后在个人设置里生成专属地址"}
    return {"ok": True, "feeds": found}
