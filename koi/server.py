"""本地接口：界面用的 /api/*、边下边播 /stream、qBittorrent 兼容接口 /api/v2/*。"""
import asyncio
import ipaddress
import hashlib
import json
import mimetypes
import os
import re
import secrets
import time
import urllib.parse
from collections import deque
from pathlib import Path

import libtorrent as lt
from aiohttp import web

from . import config as C
from . import winsys
from .engine import Engine
from .httpdl import HttpManager, decode_special
from .rss import RssManager
from .updater import Updater

LINK_RE = re.compile(r"^(magnet:\?|thunder://|qqdl://|flashget://|https?://\S+\.torrent(\?\S*)?$)", re.I)
DL_EXT = re.compile(r"^https?://\S+\.(zip|rar|7z|exe|msi|iso|dmg|apk|mp4|mkv|avi|mov|mp3|flac|pdf|tar|gz|xz|bin|img)(\?\S*)?$", re.I)


class Core:
    """把 BT、直链、RSS 粘在一起，外加通知和各种定时任务。"""

    def __init__(self):
        self.settings = C.load_settings()
        self.events = deque(maxlen=200)
        self.event_id = 0
        self.os_notify = None          # 托盘气泡通知，由 app.py 注入
        self.show_window = None        # 把窗口叫到前台，由 app.py 注入
        self.bt = Engine(self.settings, self.notify)
        self.http = HttpManager(self.settings, self.notify, self._torrent_from_http)
        self.rss = RssManager(self._rss_add)
        self.was_busy = False
        self.sid_tokens = set()
        self.quit_app = None            # 让程序正常退出（存好断点），由 main.py 注入
        self.pick_path = None           # 弹出系统选择文件夹窗口，由 main.py 注入（窗口版才有）
        self.updater = Updater(self.settings, lambda: self.quit_app() if self.quit_app else None)
        self._update_notified = None
        # 刚更新完：新版本第一次打开时弹「已更新到 vX」
        self.just_updated = None
        try:
            j = json.loads((C.DATA_DIR / "just_updated.json").read_text("utf-8"))
            if j.get("to") == C.VERSION:
                self.just_updated = j
            else:
                (C.DATA_DIR / "just_updated.json").unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    # ---------- 通知 ----------
    def notify(self, kind, title, body=""):
        self.event_id += 1
        self.events.append({"id": self.event_id, "kind": kind, "title": title, "body": body, "time": time.time()})
        if kind in ("done", "error", "all_done") and self.settings["notify_done"] and self.os_notify:
            try:
                self.os_notify(title, body)
            except Exception:
                pass

    # ---------- 添加（所有入口最后都走这里） ----------
    def add_link(self, link, save_path=None, category="", select_files=None, headers=None, paused=False):
        link = decode_special(link.strip())
        if link.startswith("magnet:"):
            return "bt", self.bt.add_magnet(link, save_path, category, select_files, paused)
        if re.fullmatch(r"[0-9a-fA-F]{40}", link):
            return "bt", self.bt.add_magnet(f"magnet:?xt=urn:btih:{link}", save_path, category, select_files, paused)
        if link.lower().startswith(("http://", "https://", "ftp://")):
            return self.http.add(link, save_path, headers, category=category)
        raise ValueError("认不出这个链接")

    def _torrent_from_http(self, data, save_path):
        ih = self.bt.add_torrent_bytes(data, save_path)
        self.notify("info", "已从网页链接拿到种子", ih)

    def _rss_add(self, link, save_path, category):
        self.add_link(link, save_path, category, select_files=False)
        self.notify("info", "RSS 自动下载", link[:80])

    # ---------- 合并后的列表 ----------
    def snapshot(self):
        lst = self.bt.snapshot() + self.http.snapshot()
        lst.sort(key=lambda t: -t["added"])
        return lst

    def summary(self, lst):
        try:
            dht_nodes = self.bt.ses.status().dht_nodes
        except Exception:
            dht_nodes = None
        d = self.bt.summary_part()
        d.update({
            "down": sum(t["down"] for t in lst),
            "up": sum(t["up"] for t in lst),
            "count": len(lst),
            "dht_nodes": dht_nodes,
            "settings": self.settings,
            "version": C.VERSION,
            "history": list(self.bt.history)[-120:],
            "event_id": self.event_id,
            "player": bool(winsys.find_player()),
            "categories": self.settings.get("categories", {}),
            "update": self.updater.view(),
            "just_updated": self.just_updated,
        })
        return d

    # ---------- 诊断：为什么不动 ----------
    def diagnose(self, ih):
        h = self.bt.find(ih)
        if not h:
            return []
        st = h.status()
        out = []

        def add(level, title, detail, fix=None):
            out.append({"level": level, "title": title, "detail": detail, "fix": fix})

        if st.errc.value():
            add("bad", "任务出错", st.errc.message(), "clear_error")
        free = winsys.free_space(st.save_path)
        left = st.total_wanted - st.total_wanted_done
        if free is not None and left > 0 and free < left:
            add("bad", "磁盘空间不够", f"还要 {left/2**30:.1f} GB，磁盘只剩 {free/2**30:.1f} GB", None)
        elif free is not None:
            add("ok", "磁盘空间", f"剩余 {free/2**30:.1f} GB")
        paused = bool(st.flags & lt.torrent_flags.paused)
        auto = bool(st.flags & lt.torrent_flags.auto_managed)
        if paused and auto:
            add("warn", "在排队", f"同时下载上限是 {self.settings['max_active_downloads']} 个，它在等前面的下完", "force_start")
        elif paused:
            add("warn", "已暂停", "你手动暂停了", "resume")
        trs = self.bt.details(ih)["trackers"]
        okn = sum(1 for t in trs if t["status"] == "正常")
        failn = sum(1 for t in trs if t["status"] == "失败")
        if trs and okn == 0:
            add("bad" if failn == len(trs) else "warn", "tracker 全都连不上", f"{len(trs)} 个 tracker，正常 0 个，失败 {failn} 个", "reannounce")
        else:
            add("ok", "tracker", f"{okn}/{len(trs)} 个正常")
        try:
            dn = self.bt.ses.status().dht_nodes
        except Exception:
            dn = None
        if dn is not None:
            add("ok" if dn > 20 else "warn", "DHT 网络", f"已连接 {dn} 个节点" + ("" if dn > 20 else "，太少，磁力链会很难找到人"))
        n = self.bt.net
        if n["cgnat"]:
            add("warn", "你在运营商内网（CGNAT）", "别人连不进你，只能你去连别人，能连上的人会少一半。可以打电话给运营商要独立公网 IP")
        elif n["incoming"]:
            add("ok", "端口", "有人能主动连进你")
        else:
            add("warn", "端口", f"还没有人主动连进来过。端口映射：{n['upnp']}")
        if st.has_metadata:
            if st.num_seeds == 0 and st.distributed_copies < 1 and not st.is_seeding:
                reach = self.bt._reachable(h, st)
                add("bad", "线上没有完整文件", f"当前连上的人凑起来最多能下到 {reach*100:.1f}%。等有完整文件的人上线，或者换一个种子", "reannounce")
            elif not st.is_seeding:
                add("ok", "做种人数", f"连上 {st.num_seeds} 个有完整文件的人，可用性 {st.distributed_copies:.2f}")
        else:
            add("warn" if st.num_peers == 0 else "ok", "种子信息", "还在找种子信息" + ("，目前一个人都没找到" if st.num_peers == 0 else f"，已连上 {st.num_peers} 人"), "reannounce")
        lim = self.settings["alt_download_limit_kb"] if self.bt._alt_active() else self.settings["download_limit_kb"]
        if lim:
            add("warn", "全局限速中", f"下载被限制在 {lim} KB/s" + ("（乌龟模式 / 时段限速）" if self.bt._alt_active() else ""), "unlimit")
        if h.download_limit() > 0:
            add("warn", "这个任务单独限速了", f"{h.download_limit()//1024} KB/s", "unlimit_one")
        if st.num_peers == 0 and st.connect_candidates == 0 and st.has_metadata and not st.is_seeding:
            add("bad", "没有可以连的人", "tracker 和 DHT 都没给出任何人", "reannounce")
        return out

    def fix(self, ih, what):
        h = self.bt.find(ih)
        if what == "clear_error" and h:
            h.clear_error()
            h.resume()
        elif what in ("force_start", "resume", "reannounce"):
            self.bt.action(ih, what)
            if what == "reannounce" and h:
                h.resume()
        elif what == "unlimit":
            self.bt.apply_settings({"download_limit_kb": 0, "alt_enabled": False, "schedule_enabled": False})
        elif what == "unlimit_one" and h:
            h.set_download_limit(-1)
        elif what == "all":
            for item in self.diagnose(ih):
                if item["fix"] and item["level"] != "ok" and item["fix"] != "unlimit":
                    self.fix(ih, item["fix"])

    # ---------- 定时 ----------
    async def loop(self):
        n = 0
        while True:
            try:
                for kind, val in self.bt.handle_alerts():
                    if kind == "done":
                        self.notify("done", "下载完成", val)
                    elif kind == "select":
                        self.notify("select", "请选择要下载的文件", val)
                        if self.show_window:
                            self.show_window()
                    elif kind == "error":
                        self.notify("error", "出错了", val)
                if n % 2 == 0:
                    self.bt.tick()
                    self.http.tick()
                if n % 2 == 1 and self.settings["clipboard_watch"] and not getattr(self, "_clip_busy", False):
                    # 读剪贴板可能被别的程序卡住（延迟渲染），放到线程池里，不阻塞事件循环
                    self._clip_busy = True
                    asyncio.get_running_loop().run_in_executor(None, self._check_clipboard)
                if n % 10 == 0:
                    self.bt.tick_slow()
                    self._watch_dir()
                    self._busy_check()
                if n % 60 == 0:
                    winsys.keep_awake(self.settings["keep_awake"] and self.was_busy)
                    self.bt.tick_schedule()
                    if self.http.session:
                        asyncio.ensure_future(self.rss.refresh(self.http.session))
                if n % 120 == 0 and n:
                    self.bt.save_all()
                if n % (6 * 3600 * 2) == 0 and n:
                    asyncio.get_running_loop().run_in_executor(None, self.bt.refresh_trackers)
                if self.settings["auto_update_check"] and (n == 30 or (n and n % (6 * 3600 * 2) == 0)):
                    asyncio.get_running_loop().run_in_executor(None, self._auto_check_update)
            except Exception as e:
                print("后台循环出错：", repr(e))
            n += 1
            await asyncio.sleep(0.5)

    def _auto_check_update(self):
        r = self.updater.check()
        if r.get("ok") and r.get("update") and self._update_notified != r["version"]:
            self._update_notified = r["version"]
            self.notify("update", f"发现新版本 {r['version']}", r.get("notes", ""))

    def _check_clipboard(self):
        try:
            txt = winsys.clipboard_changed_text()
        finally:
            self._clip_busy = False
        if not txt:
            return
        links = [l.strip() for l in txt.strip().splitlines() if LINK_RE.match(l.strip()) or DL_EXT.match(l.strip())]
        if links:
            self.notify("clipboard", "发现下载链接", "\n".join(links[:50]))
            if self.show_window:
                self.show_window()

    def _watch_dir(self):
        d = self.settings["watch_dir"].strip()
        if not d or not os.path.isdir(d):
            return
        for f in Path(d).glob("*.torrent"):
            try:
                self.bt.add_torrent_bytes(f.read_bytes(), select_files=False)
                f.rename(f.with_suffix(".torrent.added"))
                self.notify("info", "监视文件夹自动添加", f.name)
            except Exception as e:
                self.notify("error", "监视文件夹添加失败", f"{f.name}: {e}")
                try:
                    f.rename(f.with_suffix(".torrent.failed"))
                except OSError:
                    pass

    def _busy_check(self):
        busy = self.bt.any_downloading() or self.http.any_running()
        if self.was_busy and not busy:
            self.notify("all_done", "全部下载完成", "")
            act = self.settings["after_all_done"]
            if act != "none":
                self.notify("info", "即将" + ("关机" if act == "shutdown" else "睡眠"), "60 秒后执行")
                winsys.power_action(act)
                self.settings["after_all_done"] = "none"   # 一次性，和迅雷一样
                C.save_settings(self.settings)
        self.was_busy = busy


# ======================= 路由 =======================

def local_ipv4s():
    """本机所有 IPv4（局域网 / Tailscale / 公网），不含 127.x。"""
    import socket
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    for target in ("192.168.0.1", "100.100.100.100", "8.8.8.8"):   # 不会真发包，只是让系统选出口网卡
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 80))
                ips.add(s.getsockname()[0])
        except OSError:
            pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def ext_dir():
    d = C.BASE_DIR / "browser_extension"
    if not d.exists():
        import sys
        d = C.Path(sys.executable).parent / "browser_extension"
    return str(d)


def resolve_lang(pref, accept=""):
    """auto：手机等远程浏览器按它自己的语言；本机按 Windows 界面语言。"""
    if pref in ("zh-CN", "zh-TW", "en"):
        return pref
    a = accept.lower()
    if a:
        first = a.split(",")[0]
        if first.startswith(("zh-tw", "zh-hk", "zh-mo", "zh-hant")):
            return "zh-TW"
        if first.startswith("zh"):
            return "zh-CN"
        return "en"
    return winsys.system_lang()


def is_local(req):
    peer = req.transport.get_extra_info("peername") if req.transport else None
    return bool(peer) and peer[0] in ("127.0.0.1", "::1")


def make_token(pw):
    return hashlib.sha256(("koi:" + pw).encode()).hexdigest()


def build_app(core: Core):
    routes = web.RouteTableDef()
    S = core.settings

    @web.middleware
    async def auth(req, handler):
        # 防 CSRF / DNS rebinding：别的网站不能借浏览器偷偷调本机接口（比如删任务删文件）
        host = req.host.rsplit(":", 1)[0].strip("[]")
        if not S["remote_enabled"] and host not in ("127.0.0.1", "localhost", "::1"):
            raise web.HTTPForbidden(text="bad host")
        origin = req.headers.get("Origin")
        if req.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if not (origin.startswith(("chrome-extension://", "moz-extension://", "extension://"))
                    or urllib.parse.urlparse(origin).netloc == req.host):
                raise web.HTTPForbidden(text="cross-origin request blocked")
        if is_local(req):
            return await handler(req)
        if not S["remote_enabled"]:
            raise web.HTTPForbidden(text="未开启远程访问")
        path = req.path
        if path in ("/login", "/api/login", "/api/lang", "/api/v2/auth/login") or path.startswith("/static/"):
            return await handler(req)
        tok = req.cookies.get("koi_token") or req.cookies.get("SID")
        if S["remote_password"] and (tok == make_token(S["remote_password"]) or tok in core.sid_tokens):
            return await handler(req)
        if path.startswith("/api/v2/"):
            raise web.HTTPForbidden(text="Forbidden")
        if path.startswith("/api/"):
            raise web.HTTPUnauthorized(text="需要密码")
        raise web.HTTPFound("/login")

    # ---------- 页面 ----------
    @routes.get("/")
    async def index(_):
        return web.FileResponse(C.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/login")
    async def login_page(_):
        return web.FileResponse(C.STATIC_DIR / "login.html")

    @routes.post("/api/login")
    async def login(req):
        b = await req.json()
        if S["remote_password"] and b.get("password") == S["remote_password"]:
            r = web.json_response({"ok": True})
            r.set_cookie("koi_token", make_token(S["remote_password"]), max_age=30 * 86400, httponly=True)
            return r
        await asyncio.sleep(1)
        return web.json_response({"ok": False}, status=401)

    @routes.get("/static/{name:.+}")
    async def static_file(req):
        root = C.STATIC_DIR.resolve()
        f = (root / req.match_info["name"]).resolve()
        if root not in f.parents or not f.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(f, headers={"Cache-Control": "no-cache"})

    # ---------- 列表 / 状态 ----------
    @routes.get("/api/state")
    async def state(req):
        since = int(req.query.get("since", 0))
        lst = core.snapshot()
        ev = [e for e in core.events if e["id"] > since]
        return web.json_response({"list": lst, "summary": core.summary(lst), "events": ev})

    # ---------- 添加 ----------
    @routes.post("/api/preview")
    async def preview(req):
        reader = await req.multipart()
        out = []
        async for part in reader:
            if part.name == "files":
                data = await part.read()
                try:
                    p = core.bt.preview_torrent(data)
                    p["filename"] = part.filename
                    out.append(p)
                except Exception as e:
                    out.append({"filename": part.filename, "error": str(e)})
        return web.json_response(out)

    @routes.post("/api/add")
    async def add(req):
        added, errors = [], []
        opts = {"save_path": None, "category": "", "select_files": None, "priorities": None, "paused": False}
        links, files = [], []
        if req.content_type.startswith("multipart/"):
            reader = await req.multipart()
            async for part in reader:
                if part.name == "files":
                    files.append((part.filename, await part.read()))
                elif part.name == "links":
                    links += (await part.text()).splitlines()
                elif part.name in ("save_path", "category"):
                    opts[part.name] = (await part.text()).strip() or (None if part.name == "save_path" else "")
                elif part.name == "select_files":
                    opts["select_files"] = (await part.text()) == "1"
                elif part.name == "paused":
                    opts["paused"] = (await part.text()) == "1"
                elif part.name == "priorities":
                    opts["priorities"] = json.loads(await part.text())  # {文件名: [优先级...]}
        else:
            b = await req.json()
            links = (b.get("links") or b.get("magnets") or "").splitlines()
            opts["save_path"] = b.get("save_path") or None
            opts["category"] = b.get("category", "")
            if "select_files" in b:
                opts["select_files"] = bool(b["select_files"])
            opts["paused"] = bool(b.get("paused"))
            headers = b.get("headers") or {}
        if opts["category"] and not opts["save_path"]:
            cat = S.get("categories", {}).get(opts["category"])
            if cat and cat.get("save_path"):
                opts["save_path"] = cat["save_path"]
        for fname, data in files:
            try:
                pr = (opts["priorities"] or {}).get(fname)
                added.append(core.bt.add_torrent_bytes(data, opts["save_path"], opts["category"], pr, paused=opts["paused"]))
            except Exception as e:
                errors.append(f"{fname}: {e}")
        for line in links:
            line = line.strip()
            if not line:
                continue
            try:
                kind, tid = core.add_link(line, opts["save_path"], opts["category"], opts["select_files"],
                                          headers if not req.content_type.startswith("multipart/") else None, opts["paused"])
                added.append(tid)
            except Exception as e:
                errors.append(f"{line[:50]}: {e}")
        return web.json_response({"added": added, "errors": errors})

    # ---------- 操作 ----------
    @routes.post("/api/action")
    async def action(req):
        b = await req.json()
        act = b.get("action")
        for tid in b.get("ids", []):
            if tid.startswith("h") and tid in core.http.tasks:
                if act == "open":
                    t = core.http.tasks[tid]
                    winsys.open_folder(t.save_path, str(t.path) if t.state == "done" else None)
                else:
                    core.http.action(tid, act, **b)
            else:
                if act == "open":
                    h = core.bt.find(tid)
                    if h:
                        st = h.status()
                        winsys.open_folder(st.save_path, os.path.join(st.save_path, st.name))
                else:
                    core.bt.action(tid, act, **b)
        return web.json_response({"ok": True})

    @routes.get("/api/files/{ih}")
    async def files(req):
        return web.json_response(core.bt.files(req.match_info["ih"]))

    @routes.post("/api/files/{ih}")
    async def set_files(req):
        b = await req.json()
        core.bt.set_file_priorities(req.match_info["ih"], b["priorities"])
        return web.json_response({"ok": True})

    @routes.get("/api/details/{ih}")
    async def details(req):
        ih = req.match_info["ih"]
        if ih in core.http.tasks:
            t = core.http.tasks[ih]
            return web.json_response({"http": True, "history": list(t.history), "info": {"url": t.url, "name": t.filename, "save_path": t.save_path, "size": t.size, "ranges": t.ranges}})
        d = core.bt.details(ih)
        if d is None:
            raise web.HTTPNotFound()
        return web.json_response(d)

    @routes.get("/api/diagnose/{ih}")
    async def diagnose(req):
        return web.json_response(core.diagnose(req.match_info["ih"]))

    @routes.post("/api/fix/{ih}")
    async def fix(req):
        b = await req.json()
        core.fix(req.match_info["ih"], b.get("what", "all"))
        return web.json_response({"ok": True})

    @routes.get("/api/export/{ih}")
    async def export(req):
        name, data = core.bt.export_torrent(req.match_info["ih"])
        if not data:
            raise web.HTTPNotFound(text="还没拿到种子信息")
        fn = urllib.parse.quote(f"{name}.torrent")
        return web.Response(body=data, content_type="application/x-bittorrent",
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})

    @routes.post("/api/create")
    async def create(req):
        b = await req.json()
        loop = asyncio.get_running_loop()
        try:
            out, _ = await loop.run_in_executor(None, lambda: core.bt.create_torrent(
                b["path"], b.get("trackers", ""), b.get("private", False), b.get("comment", ""), b.get("seed", True)))
            return web.json_response({"ok": True, "file": out})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    @routes.get("/api/lang")
    async def lang(req):
        return web.json_response({"lang": resolve_lang(S["language"], req.headers.get("Accept-Language", ""))})

    @routes.post("/api/pick")
    async def pick(req):
        """弹出系统的「选择文件夹 / 文件」窗口（只在本机窗口里有用；手机远程访问时前端会直接让手动输入）。"""
        b = await req.json()
        if not core.pick_path or not is_local(req):
            return web.json_response({"ok": False, "manual": True})
        path = await asyncio.get_running_loop().run_in_executor(None, core.pick_path, b.get("kind", "folder"), b.get("start", ""))
        return web.json_response({"ok": bool(path), "path": path or ""})

    @routes.get("/api/disk")
    async def disk(req):
        p = req.query.get("path") or S["save_path"]
        while p and not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        return web.json_response({"free": winsys.free_space(p)})

    @routes.post("/api/open_path")
    async def open_path(req):
        b = await req.json()
        winsys.open_folder(b["path"])
        return web.json_response({"ok": True})

    # ---------- 设置 ----------
    @routes.post("/api/settings")
    async def settings(req):
        b = await req.json()
        core.bt.apply_settings(b)
        return web.json_response(S)

    @routes.post("/api/categories")
    async def categories(req):
        b = await req.json()
        cats = dict(S.get("categories", {}))
        if b.get("delete"):
            cats.pop(b["name"], None)
        else:
            cats[b["name"]] = {"save_path": b.get("save_path", "")}
        S["categories"] = cats
        C.save_settings(S)
        return web.json_response(cats)

    @routes.post("/api/trackers/refresh")
    async def trackers(_):
        n = await asyncio.get_running_loop().run_in_executor(None, core.bt.refresh_trackers)
        return web.json_response({"count": n})

    @routes.post("/api/unban")
    async def unban(_):
        core.bt.unban_all()
        return web.json_response({"ok": True})

    @routes.post("/api/associate")
    async def associate(_):
        ok, msg = winsys.register_associations()
        return web.json_response({"ok": ok, "message": msg})

    @routes.post("/api/cancel_shutdown")
    async def cancel_shutdown(_):
        winsys.cancel_shutdown()
        return web.json_response({"ok": True})

    # ---------- 在线更新 ----------
    @routes.post("/api/update/check")
    async def update_check(_):
        r = await asyncio.get_running_loop().run_in_executor(None, core.updater.check)
        return web.json_response(r)

    @routes.post("/api/update/start")
    async def update_start(_):
        ok = core.updater.start()
        return web.json_response({"ok": ok, **core.updater.view()})

    @routes.get("/api/update/status")
    async def update_status(_):
        return web.json_response(core.updater.view())

    @routes.post("/api/update/ack")
    async def update_ack(_):
        core.just_updated = None
        (C.DATA_DIR / "just_updated.json").unlink(missing_ok=True)
        return web.json_response({"ok": True})

    @routes.post("/api/update/apply")
    async def update_apply(_):
        ok = core.updater.apply()
        return web.json_response({"ok": ok, **core.updater.view()})

    # ---------- RSS ----------
    @routes.get("/api/rss")
    async def rss_view(_):
        return web.json_response(core.rss.view())

    @routes.post("/api/rss")
    async def rss_op(req):
        b = await req.json()
        op = b.get("op")
        if op == "add_feed":
            core.rss.add_feed(b["url"], b.get("name", ""), b.get("interval", 15))
            await core.rss.refresh(core.http.session, force=True)
        elif op == "remove_feed":
            core.rss.remove_feed(b["id"])
        elif op == "refresh":
            await core.rss.refresh(core.http.session, force=True)
        elif op == "set_rule":
            core.rss.set_rule(b["rule"])
        elif op == "remove_rule":
            core.rss.remove_rule(b["id"])
        elif op == "download":
            core.add_link(b["link"], None, "", select_files=None)
        elif op == "test_rule":
            items = [i for v in core.rss.items.values() for i in v]
            return web.json_response([i["title"] for i in items if core.rss.match(b["rule"], i["title"])][:50])
        return web.json_response(core.rss.view())

    # ---------- 边下边播 ----------
    @routes.get("/play/{ih}/{idx}")
    async def play_page(req):
        ih, idx = req.match_info["ih"], int(req.match_info["idx"])
        info = core.bt.prepare_stream(ih, idx)
        if not info:
            raise web.HTTPNotFound(text="还没拿到种子信息")
        name = urllib.parse.quote(info["name"])
        html = (C.STATIC_DIR / "player.html").read_text("utf-8")
        html = html.replace("__SRC__", f"/stream/{ih}/{idx}/{name}").replace("__NAME__", info["name"].replace("<", "&lt;"))
        return web.Response(text=html, content_type="text/html")

    @routes.post("/api/play_external/{ih}/{idx}")
    async def play_external(req):
        ih, idx = req.match_info["ih"], int(req.match_info["idx"])
        info = core.bt.prepare_stream(ih, idx)
        if not info:
            return web.json_response({"ok": False, "error": "还没拿到种子信息"})
        url = f"http://127.0.0.1:{C.UI_PORT}/stream/{ih}/{idx}/{urllib.parse.quote(info['name'])}"
        player = winsys.open_with_player(url)
        return web.json_response({"ok": bool(player), "player": player, "url": url})

    @routes.get("/stream/{ih}/{idx}/{name}")
    async def stream(req):
        ih, idx = req.match_info["ih"], int(req.match_info["idx"])
        info = core.bt.stream_file_info(ih, idx)
        if not info:
            raise web.HTTPNotFound()
        size = info["size"]
        start, end = 0, size - 1
        rng = req.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
        if start >= size:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        ctype = mimetypes.guess_type(info["name"])[0] or "application/octet-stream"
        if info["name"].lower().endswith(".mkv"):
            ctype = "video/webm"   # 让 Chromium 尝试直接播 mkv
        resp = web.StreamResponse(status=206 if rng else 200, headers={
            "Content-Type": ctype, "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            **({"Content-Range": f"bytes {start}-{end}/{size}"} if rng else {}),
        })
        await resp.prepare(req)
        pos = start
        pl = info["piece_len"]
        try:
            while pos <= end:
                # 一次最多读到当前块的末尾
                piece_end = ((info["offset"] + pos) // pl + 1) * pl - info["offset"]
                upto = min(end + 1, piece_end, pos + 1024 * 1024)
                waited = 0
                while not core.bt.want_range(ih, idx, pos, upto):
                    await asyncio.sleep(0.2)
                    waited += 0.2
                    if waited > 120:
                        raise asyncio.TimeoutError()
                with open(info["path"], "rb") as f:
                    f.seek(pos)
                    data = f.read(upto - pos)
                if not data:
                    await asyncio.sleep(0.3)
                    continue
                await resp.write(data)
                pos += len(data)
        except (ConnectionResetError, asyncio.CancelledError, asyncio.TimeoutError):
            pass
        return resp

    # ======== qBittorrent WebAPI v2 兼容（Sonarr / Radarr / 手机遥控 App） ========
    def qb_state(t):
        if t["type"] != "bt":
            return "downloading"
        done = t["health"]["level"] == "done"
        if t["error"]:
            return "error"
        if t["paused"]:
            return "pausedUP" if done else "pausedDL"
        if t["queued"]:
            return "queuedUP" if done else "queuedDL"
        if not t["has_metadata"]:
            return "metaDL"
        if t["state"] == "校验中":
            return "checkingUP" if done else "checkingDL"
        if done:
            return "uploading" if t["up"] > 0 else "stalledUP"
        return "downloading" if t["down"] > 0 else "stalledDL"

    def qb_torrent(t):
        content = os.path.join(t["save_path"], t["name"])
        return {
            "hash": t["id"], "name": t["name"], "size": t["size"], "total_size": t["size"],
            "progress": t["progress"], "dlspeed": t["down"], "upspeed": t["up"],
            "eta": t["eta"] if t["eta"] is not None else 8640000, "state": qb_state(t),
            "category": t.get("category", ""), "tags": "", "save_path": t["save_path"],
            "content_path": content, "ratio": t.get("ratio", 0), "added_on": int(t["added"]),
            "completion_on": -1, "num_seeds": t.get("seeds", 0), "num_leechs": t.get("peers", 0),
            "num_complete": t.get("seeds_total", 0), "num_incomplete": t.get("peers_total", 0),
            "downloaded": t["done"], "uploaded": t.get("uploaded", 0), "amount_left": t["size"] - t["done"],
            "priority": t.get("queue", 0) + 1 if t.get("queue", -1) >= 0 else 0,
            "seq_dl": t.get("sequential", False), "availability": t.get("availability") or 0,
            "seeding_time": 0, "ratio_limit": -2, "seeding_time_limit": -2, "magnet_uri": "",
            "dl_limit": 0, "up_limit": 0, "force_start": False, "auto_tmm": False, "tracker": "",
        }

    async def qb_form(req):
        if req.content_type.startswith("multipart/"):
            data, files = {}, []
            reader = await req.multipart()
            async for part in reader:
                if part.filename:
                    files.append((part.filename, await part.read()))
                else:
                    data[part.name] = await part.text()
            return data, files
        return dict(await req.post()), []

    async def qb_params(req):
        p = dict(req.query)
        if req.method == "POST":
            data, _ = await qb_form(req)
            p.update(data)
        return p

    def qb_hashes(data):
        hs = data.get("hashes", "")
        if hs == "all":
            return [t["id"] for t in core.snapshot()]
        return [h.lower() for h in hs.split("|") if h]

    @routes.post("/api/v2/auth/login")
    async def qb_login(req):
        data, _ = await qb_form(req)
        user_ok = not S["remote_username"] or data.get("username", "") == S["remote_username"]
        if is_local(req) or (S["remote_password"] and user_ok and data.get("password") == S["remote_password"]):
            sid = secrets.token_hex(16)
            core.sid_tokens.add(sid)
            r = web.Response(text="Ok.")
            r.set_cookie("SID", sid, httponly=True)
            return r
        return web.Response(text="Fails.")

    @routes.post("/api/v2/auth/logout")
    async def qb_logout(_):
        return web.Response(text="Ok.")

    @routes.route("*", "/api/v2/app/version")
    async def qb_version(_):
        return web.Response(text="v4.6.7")

    @routes.route("*", "/api/v2/app/webapiVersion")
    async def qb_api_version(_):
        return web.Response(text="2.9.3")

    @routes.route("*", "/api/v2/app/buildInfo")
    async def qb_build(_):
        return web.json_response({"qt": "6.7", "libtorrent": lt.__version__, "boost": "", "openssl": "", "bitness": 64})

    @routes.route("*", "/api/v2/app/preferences")
    async def qb_prefs(_):
        return web.json_response({
            "save_path": S["save_path"], "temp_path_enabled": False, "temp_path": "",
            "max_ratio_enabled": S["ratio_limit"] > 0, "max_ratio": S["ratio_limit"] or -1,
            "max_seeding_time_enabled": S["seed_time_limit_min"] > 0, "max_seeding_time": S["seed_time_limit_min"] or -1,
            "max_ratio_act": 0, "queueing_enabled": True, "max_active_downloads": S["max_active_downloads"],
            "max_active_torrents": S["max_active_downloads"] + 20, "max_active_uploads": 10,
            "dht": True, "pex": True, "lsd": True, "listen_port": S["listen_port"], "upnp": True,
            "dl_limit": S["download_limit_kb"] * 1024, "up_limit": S["upload_limit_kb"] * 1024,
            "auto_tmm_enabled": False, "create_subfolder_enabled": True, "start_paused_enabled": False,
        })

    @routes.post("/api/v2/app/setPreferences")
    async def qb_setprefs(_):
        return web.Response(text="")

    @routes.route("*", "/api/v2/app/defaultSavePath")
    async def qb_savepath(_):
        return web.Response(text=S["save_path"])

    @routes.route("*", "/api/v2/torrents/info")
    async def qb_info(req):
        query = await qb_params(req)
        lst = [t for t in core.snapshot() if t["type"] == "bt"]
        cat = query.get("category")
        if cat is not None:
            lst = [t for t in lst if t.get("category", "") == cat]
        hs = query.get("hashes")
        if hs:
            want = set(hs.lower().split("|"))
            lst = [t for t in lst if t["id"] in want]
        flt = query.get("filter", "all")
        if flt in ("downloading", "seeding", "completed", "paused", "stopped", "active"):
            done = lambda t: t["health"]["level"] == "done"
            lst = [t for t in lst if {
                "downloading": not done(t), "seeding": done(t) and not t["paused"], "completed": done(t),
                "paused": t["paused"], "stopped": t["paused"], "active": (t["down"] + t["up"]) > 0}[flt]]
        return web.json_response([qb_torrent(t) for t in lst])

    @routes.route("*", "/api/v2/torrents/properties")
    async def qb_props(req):
        query = await qb_params(req)
        d = core.bt.details(query.get("hash", "").lower())
        if not d:
            raise web.HTTPNotFound()
        i = d["info"]
        return web.json_response({"save_path": i["save_path"], "total_size": i["size"], "piece_size": i["piece_size"],
                                  "pieces_num": i["total_pieces"], "comment": i["comment"], "addition_date": i["added"],
                                  "completion_date": i["completed"] or -1, "total_downloaded": i["downloaded"],
                                  "total_uploaded": i["uploaded"], "seeds": 0, "peers": len(d["peers"]), "is_private": i["private"]})

    @routes.route("*", "/api/v2/torrents/files")
    async def qb_files(req):
        query = await qb_params(req)
        fs = core.bt.files(query.get("hash", "").lower())
        return web.json_response([{"index": f["index"], "name": f["path"], "size": f["size"],
                                   "progress": f["done"] / f["size"] if f["size"] else 1, "priority": 1 if f["priority"] else 0,
                                   "is_seed": f["done"] == f["size"]} for f in fs])

    @routes.route("*", "/api/v2/torrents/trackers")
    async def qb_trackers(req):
        query = await qb_params(req)
        d = core.bt.details(query.get("hash", "").lower())
        if not d:
            raise web.HTTPNotFound()
        stmap = {"正常": 2, "正在联系": 3, "未联系": 1, "失败": 4}
        return web.json_response([{"url": t["url"], "status": stmap[t["status"]], "tier": t["tier"], "msg": t["message"],
                                   "num_seeds": t["seeds"], "num_leeches": t["peers"], "num_peers": -1, "num_downloaded": -1}
                                  for t in d["trackers"]])

    @routes.post("/api/v2/torrents/add")
    async def qb_add(req):
        data, files = await qb_form(req)
        save = data.get("savepath") or None
        cat = data.get("category", "")
        if cat and not save:
            save = (S.get("categories", {}).get(cat) or {}).get("save_path") or None
        paused = str(data.get("paused", data.get("stopped", "false"))).lower() == "true"
        ok = False
        for url in data.get("urls", "").splitlines():
            if url.strip():
                core.add_link(url.strip(), save, cat, select_files=False, paused=paused)
                ok = True
        for fn, blob in files:
            core.bt.add_torrent_bytes(blob, save, cat, paused=paused)
            ok = True
        return web.Response(text="Ok." if ok else "Fails.")

    for name, act in (("pause", "pause"), ("stop", "pause"), ("resume", "resume"), ("start", "resume"),
                      ("recheck", "recheck"), ("reannounce", "reannounce"), ("topPrio", "top"),
                      ("bottomPrio", "bottom"), ("increasePrio", "up"), ("decreasePrio", "down")):
        async def h(req, act=act):
            data, _ = await qb_form(req)
            for ih in qb_hashes(data):
                core.bt.action(ih, act)
            return web.Response(text="")
        routes.post(f"/api/v2/torrents/{name}")(h)

    @routes.post("/api/v2/torrents/delete")
    async def qb_delete(req):
        data, _ = await qb_form(req)
        for ih in qb_hashes(data):
            core.bt.remove(ih, data.get("deleteFiles") == "true")
        return web.Response(text="")

    @routes.post("/api/v2/torrents/setCategory")
    async def qb_setcat(req):
        data, _ = await qb_form(req)
        for ih in qb_hashes(data):
            core.bt.action(ih, "category", category=data.get("category", ""))
        return web.Response(text="")

    @routes.route("*", "/api/v2/torrents/categories")
    async def qb_cats(_):
        return web.json_response({k: {"name": k, "savePath": v.get("save_path", "")} for k, v in S.get("categories", {}).items()})

    @routes.post("/api/v2/torrents/createCategory")
    async def qb_mkcat(req):
        data, _ = await qb_form(req)
        cats = dict(S.get("categories", {}))
        cats[data["category"]] = {"save_path": data.get("savePath", "")}
        S["categories"] = cats
        C.save_settings(S)
        return web.Response(text="")

    @routes.post("/api/v2/torrents/editCategory")
    async def qb_editcat(req):
        return await qb_mkcat(req)

    @routes.post("/api/v2/torrents/setShareLimits")
    async def qb_share(_):
        return web.Response(text="")

    @routes.post("/api/v2/torrents/setForceStart")
    async def qb_force(req):
        data, _ = await qb_form(req)
        for ih in qb_hashes(data):
            core.bt.action(ih, "force_start" if data.get("value") == "true" else "resume")
        return web.Response(text="")

    @routes.route("*", "/api/v2/torrents/tags")
    async def qb_tags(_):
        return web.json_response([])

    @routes.route("*", "/api/v2/transfer/info")
    async def qb_transfer(_):
        lst = core.snapshot()
        return web.json_response({"dl_info_speed": sum(t["down"] for t in lst), "up_info_speed": sum(t["up"] for t in lst),
                                  "dl_info_data": 0, "up_info_data": 0, "dl_rate_limit": S["download_limit_kb"] * 1024,
                                  "up_rate_limit": S["upload_limit_kb"] * 1024, "dht_nodes": 0,
                                  "connection_status": "connected" if core.bt.net["incoming"] else "firewalled"})

    @routes.route("*", "/api/v2/sync/maindata")
    async def qb_maindata(req):
        query = await qb_params(req)
        lst = [t for t in core.snapshot() if t["type"] == "bt"]
        down = sum(t["down"] for t in lst)
        up = sum(t["up"] for t in lst)
        return web.json_response({
            "rid": int(query.get("rid", 0)) + 1, "full_update": True,
            "torrents": {t["id"]: qb_torrent(t) for t in lst},
            "categories": {k: {"name": k, "savePath": v.get("save_path", "")} for k, v in S.get("categories", {}).items()},
            "tags": [],
            "server_state": {"dl_info_speed": down, "up_info_speed": up, "connection_status": "connected",
                             "free_space_on_disk": winsys.free_space(S["save_path"]) or 0},
        })

    # ---------- 浏览器扩展接管 ----------
    @routes.post("/api/ext/add")
    async def ext_add(req):
        """浏览器扩展把下载交过来，可以带 Cookie / Referer，网盘之类要登录的链接才下得动。"""
        b = await req.json()
        headers = {}
        if b.get("cookie"):
            headers["Cookie"] = b["cookie"]
        if b.get("referer"):
            headers["Referer"] = b["referer"]
        if b.get("ua"):
            headers["User-Agent"] = b["ua"]
        try:
            kind, tid = core.add_link(b["url"], None, "", None, headers)
            fn = b.get("filename")
            if kind == "http" and fn and tid in core.http.tasks and not core.http.tasks[tid].filename:
                core.http.tasks[tid].filename = os.path.basename(fn)
            core.notify("info", "浏览器发来下载", b.get("filename") or b["url"][:80])
            res = {"ok": True}
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        return web.json_response(res)

    @routes.get("/api/ext/ping")
    async def ext_ping(req):
        if req.query.get("from") == "ext":
            core.ext_seen = {"time": time.time(), "ua": req.headers.get("User-Agent", "")[:200]}
        return web.json_response({"app": "koi", "version": C.VERSION})

    # ---------- 远程访问 ----------
    @routes.get("/api/remote/info")
    async def remote_info(_):
        port = C.UI_PORT
        addrs = []
        for ip in local_ipv4s():
            a = ipaddress.ip_address(ip)
            kind = "tailscale" if a in ipaddress.ip_network("100.64.0.0/10") else "lan" if a.is_private else "public"
            addrs.append({"ip": ip, "kind": kind, "url": f"http://{ip}:{port}/"})
        addrs.sort(key=lambda x: {"lan": 0, "tailscale": 1, "public": 2}[x["kind"]])
        return web.json_response({"enabled": S["remote_enabled"], "has_password": bool(S["remote_password"]),
                                  "username": S["remote_username"], "port": port, "addrs": addrs,
                                  "ext": getattr(core, "ext_seen", None), "ext_dir": ext_dir()})

    @routes.get("/api/remote/qr")
    async def remote_qr(req):
        import io
        import segno
        buf = io.BytesIO()
        segno.make(req.query.get("text", ""), error="m").save(buf, kind="svg", scale=5, border=2, dark="#0a1020", light="#ffffff")
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml")

    @routes.post("/api/open_ext_folder")
    async def open_ext_folder(_):
        winsys.open_folder(ext_dir())
        return web.json_response({"ok": True})

    app = web.Application(client_max_size=100 * 1024 * 1024, middlewares=[auth])
    app.add_routes(routes)

    async def lifecycle(app):
        await core.http.start()
        core.http.schedule()
        task = asyncio.ensure_future(core.loop())
        asyncio.get_running_loop().run_in_executor(None, core.bt.refresh_trackers)
        yield
        task.cancel()
        await core.http.stop()
        core.bt.save_all()
        await asyncio.sleep(1.5)
        core.bt.handle_alerts()

    app.cleanup_ctx.append(lifecycle)
    return app
