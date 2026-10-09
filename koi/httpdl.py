"""网页直链多线程下载：IDM 式动态分段——哪段先下完，就把最慢那段剩下的劈一半接着下。"""
import asyncio
import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
from collections import deque
from pathlib import Path

import aiohttp

from . import config as C

MIN_SPLIT = 1024 * 1024          # 剩余不到 1MB 的段不再劈
CHUNK = 256 * 1024
HTTP_ERR = {
    401: "需要登录（401）。用浏览器扩展下载可以带上登录状态",
    403: "服务器拒绝（403）：链接可能过期了，或者要登录。用浏览器扩展下载试试",
    404: "文件不存在（404）：链接失效了",
    410: "文件已被删除（410）",
    429: "请求太频繁被服务器限制（429），过一会儿再试",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


def decode_special(url):
    """迅雷 / QQ旋风 / 快车 的专用链接，本质是 Base64 包了一层真实地址。"""
    url = url.strip()
    low = url.lower()
    try:
        if low.startswith("thunder://"):
            raw = base64.b64decode(url[10:] + "==").decode("utf-8", "ignore")
            return raw[2:-2] if raw.startswith("AA") and raw.endswith("ZZ") else raw
        if low.startswith("qqdl://"):
            return base64.b64decode(url[7:] + "==").decode("utf-8", "ignore")
        if low.startswith("flashget://"):
            raw = base64.b64decode(url[11:].split("&")[0] + "==").decode("utf-8", "ignore")
            return raw.replace("[FLASHGET]", "")
    except Exception:
        pass
    return url


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" .")
    return name[:200] or "download"


def name_from_headers(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        return safe_name(urllib.parse.unquote(m.group(1).strip('"')))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if m:
        raw = m.group(1)
        try:
            raw = raw.encode("latin-1").decode("utf-8")
        except Exception:
            pass
        return safe_name(raw)
    path = urllib.parse.urlparse(str(resp.url)).path
    return safe_name(urllib.parse.unquote(path.rsplit("/", 1)[-1]) or "download")


def unique_path(p: Path):
    if not p.exists() and not Path(str(p) + ".koipart").exists():
        return p
    stem, suf = p.stem, p.suffix
    for i in range(1, 1000):
        q = p.with_name(f"{stem} ({i}){suf}")
        if not q.exists() and not Path(str(q) + ".koipart").exists():
            return q
    return p


class HttpTask:
    def __init__(self, d):
        self.id = d["id"]
        self.url = d["url"]
        self.headers = d.get("headers", {})
        self.save_path = d["save_path"]
        self.filename = d.get("filename", "")
        self.size = d.get("size", 0)            # 0 = 未知
        self.ranges = d.get("ranges", None)     # 是否支持分段
        self.etag = d.get("etag", "")
        self.segments = d.get("segments", [])   # [{start, end(含), pos}]
        self.state = d.get("state", "queued")   # queued / running / paused / done / error
        self.error = d.get("error", "")
        self.added = d.get("added", time.time())
        self.completed = d.get("completed", 0)
        self.category = d.get("category", "")
        self.threads = d.get("threads", 16)
        self.speed = 0
        self.active_conns = 0
        self.conn_limit = self.threads
        self.throttled = False
        self._last_bytes = self.done_bytes()
        self.history = deque(maxlen=300)
        self.task = None

    def to_dict(self):
        d = {k: getattr(self, k) for k in (
            "id", "url", "headers", "save_path", "filename", "size", "ranges", "etag",
            "state", "error", "added", "completed", "category", "threads")}
        d["segments"] = [{"start": s["start"], "end": s["end"], "pos": s["pos"]} for s in self.segments]
        return d

    def save(self):
        (C.HTTP_DIR / f"{self.id}.json").write_text(json.dumps(self.to_dict(), ensure_ascii=False), "utf-8")

    def done_bytes(self):
        if self.state == "done":
            return self.size
        return sum(s["pos"] - s["start"] for s in self.segments)

    @property
    def path(self):
        return Path(self.save_path) / self.filename

    @property
    def part(self):
        return Path(str(self.path) + ".koipart")


class HttpManager:
    def __init__(self, settings, notify, on_torrent):
        self.settings = settings
        self.notify = notify
        self.on_torrent = on_torrent   # 链接其实是 .torrent 文件时交给 BT 内核
        self.tasks = {}
        self.max_running = 3
        for f in C.HTTP_DIR.glob("*.json"):
            try:
                t = HttpTask(json.loads(f.read_text("utf-8")))
                if t.state == "running":
                    t.state = "queued"
                self.tasks[t.id] = t
            except Exception as e:
                print("恢复直链任务失败", f.name, e)
        self.session = None
        self.loop = None

    async def start(self):
        self.loop = asyncio.get_running_loop()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
        self.session = aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=0, ssl=False))

    async def stop(self):
        for t in self.tasks.values():
            if t.task:
                t.task.cancel()
        await asyncio.sleep(0.2)
        for t in self.tasks.values():
            t.save()
        if self.session:
            await self.session.close()

    # ---------- 添加 ----------
    def add(self, url, save_path=None, headers=None, filename="", category=""):
        url = decode_special(url)
        if url.startswith("magnet:"):
            return ("magnet", url)
        tid = "h" + hashlib.sha1(f"{url}{time.time()}".encode()).hexdigest()[:15]
        t = HttpTask({
            "id": tid, "url": url, "headers": headers or {},
            "save_path": save_path or self.settings["save_path"],
            "filename": safe_name(filename) if filename else "",
            "category": category, "threads": int(self.settings["http_threads"]),
        })
        self.tasks[tid] = t
        t.save()
        self.schedule()
        return ("http", tid)

    # ---------- 调度 ----------
    def schedule(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 从别的线程（托盘菜单）或者事件循环还没起来时调用
            if getattr(self, "loop", None):
                self.loop.call_soon_threadsafe(self.schedule)
            return
        running = [t for t in self.tasks.values() if t.state == "running"]
        for t in sorted(self.tasks.values(), key=lambda t: t.added):
            if len(running) >= self.max_running:
                break
            if t.state == "queued":
                t.state = "running"
                t.error = ""
                t.task = asyncio.ensure_future(self._run(t))
                running.append(t)

    def action(self, tid, act, **kw):
        t = self.tasks.get(tid)
        if not t:
            return
        if act == "pause" and t.state in ("running", "queued"):
            t.state = "paused"
            if t.task:
                t.task.cancel()
            t.save()
        elif act in ("resume", "force_start") and t.state in ("paused", "error"):
            t.state = "queued"
            t.save()
            self.schedule()
        elif act == "remove":
            if t.task:
                t.task.cancel()
            if kw.get("delete_files"):
                for p in (t.part, t.path):
                    try:
                        p.unlink()
                    except OSError:
                        pass
            else:
                try:
                    t.part.unlink()
                except OSError:
                    pass
            (C.HTTP_DIR / f"{tid}.json").unlink(missing_ok=True)
            self.tasks.pop(tid, None)
            self.schedule()
        elif act == "category":
            t.category = kw.get("category", "")
            t.save()

    # ---------- 下载 ----------
    def _hdrs(self, t, extra=None):
        h = {"User-Agent": UA, "Accept-Encoding": "identity"}
        h.update(t.headers or {})
        if extra:
            h.update(extra)
        return h

    async def _probe(self, t):
        """先用浏览器 UA 试；有的服务器（如 Hetzner）拦"假浏览器"，断开就换成下载工具 UA 再试，选中的 UA 记下来给所有分段用。"""
        if "User-Agent" in (t.headers or {}):
            return await self._probe_once(t)
        last = None
        for ua in (UA, f"KoiDown/{C.VERSION}"):
            t.headers = dict(t.headers or {}, **{"User-Agent": ua})
            try:
                return await self._probe_once(t)
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientResponseError, RuntimeError) as e:
                last = e
        raise last

    async def _probe_once(self, t):
        async with self.session.get(t.url, headers=self._hdrs(t, {"Range": "bytes=0-0"}), allow_redirects=True) as r:
            if r.status >= 400:
                raise RuntimeError(HTTP_ERR.get(r.status, f"服务器返回 {r.status}"))
            ctype = r.headers.get("Content-Type", "")
            if not t.filename:
                t.filename = name_from_headers(r, t.url)
            if r.status == 206:
                cr = r.headers.get("Content-Range", "")
                m = re.search(r"/(\d+)", cr)
                t.size = int(m.group(1)) if m else 0
                t.ranges = t.size > 0
            else:
                t.size = int(r.headers.get("Content-Length", 0) or 0)
                t.ranges = False
            t.etag = r.headers.get("ETag", "") or r.headers.get("Last-Modified", "")
            t.url = str(r.url) if not t.headers.get("Cookie") else t.url  # 跟随跳转后的地址，带 Cookie 的保留原地址
            return ctype

    async def _run(self, t):
        try:
            fresh = not t.segments
            if fresh:
                ctype = await self._probe(t)
                if "bittorrent" in ctype or t.filename.lower().endswith(".torrent"):
                    async with self.session.get(t.url, headers=self._hdrs(t)) as r:
                        data = await r.read()
                    (C.HTTP_DIR / f"{t.id}.json").unlink(missing_ok=True)
                    self.tasks.pop(t.id, None)
                    self.on_torrent(data, t.save_path)
                    self.schedule()
                    return
                t.path.parent.mkdir(parents=True, exist_ok=True)
                if not t.part.exists():
                    # 新任务避免覆盖同名文件
                    p = unique_path(t.path)
                    t.filename = p.name
                if t.ranges and t.size > 0:
                    n = max(1, min(t.threads, t.size // MIN_SPLIT or 1))
                    step = t.size // n
                    t.segments = [{"start": i * step, "end": (t.size - 1 if i == n - 1 else (i + 1) * step - 1), "pos": i * step} for i in range(n)]
                else:
                    t.segments = [{"start": 0, "end": -1, "pos": 0}]
                with open(t.part, "wb") as f:
                    if t.size > 0 and t.ranges:
                        f.truncate(t.size)
                t.save()
            elif not t.part.exists():
                raise RuntimeError("临时文件不见了，请删除任务重新下载")
            elif not t.ranges:
                # 不支持断点的只能从头来
                for s in t.segments:
                    s["pos"] = s["start"]

            fh = open(t.part, "r+b")
            lock = asyncio.Lock()
            try:
                if t.ranges:
                    await self._multi(t, fh, lock)
                else:
                    await self._single(t, fh, lock)
            finally:
                fh.close()

            if t.state != "running":
                return
            final = unique_path(t.path) if t.path.exists() else t.path
            os.replace(t.part, final)
            t.filename = final.name
            if not t.size:
                t.size = final.stat().st_size
            t.state = "done"
            t.completed = time.time()
            t.speed = 0
            t.save()
            self.notify("done", "下载完成", t.filename)
        except asyncio.CancelledError:
            t.save()
            raise
        except Exception as e:
            t.state = "error"
            t.error = str(e) or type(e).__name__
            t.speed = 0
            t.save()
        finally:
            t.active_conns = 0
            if t.state != "running":
                self.schedule()

    async def _single(self, t, fh, lock):
        seg = t.segments[0]
        seg["pos"] = 0
        async with self.session.get(t.url, headers=self._hdrs(t)) as r:
            if r.status >= 400:
                raise RuntimeError(f"服务器返回 {r.status}")
            t.active_conns = 1
            fh.seek(0)
            fh.truncate(0)
            async for chunk in r.content.iter_chunked(CHUNK):
                fh.write(chunk)
                seg["pos"] += len(chunk)
        t.size = seg["pos"]

    async def _multi(self, t, fh, lock):
        t.conn_limit = t.threads
        workers = [asyncio.ensure_future(self._worker(t, fh, lock, i)) for i in range(t.threads)]
        last_save = time.time()
        try:
            while any(not w.done() for w in workers):
                await asyncio.sleep(0.5)
                if t.conn_limit < t.threads and time.time() - getattr(t, "last_throttle", 0) > 3:
                    t.conn_limit += 1
                    t.last_throttle = time.time()
                if time.time() - last_save > 5:
                    t.save()
                    last_save = time.time()
            for w in workers:
                if w.exception():
                    raise w.exception()
        finally:
            for w in workers:
                w.cancel()
        if any(s["pos"] <= s["end"] for s in t.segments):
            raise RuntimeError("有分段没下完，点继续重试")

    def _steal(self, t):
        """动态分段：找剩余最多的那段，把后一半劈出来。"""
        busy = [s for s in t.segments if s["end"] - s["pos"] + 1 > 2 * MIN_SPLIT]
        if not busy:
            return None
        s = max(busy, key=lambda s: s["end"] - s["pos"])
        mid = s["pos"] + (s["end"] - s["pos"] + 1) // 2
        new = {"start": mid, "end": s["end"], "pos": mid}
        s["end"] = mid - 1
        t.segments.append(new)
        return new

    async def _worker(self, t, fh, lock, idx):
        while True:
            if idx >= t.conn_limit:
                # 服务器限了连接数：多出来的线程先歇着，限流解除后再上
                if not any(s["pos"] <= s["end"] for s in t.segments):
                    return
                await asyncio.sleep(1)
                continue
            # 先找没人认领、没下完的段，没有就从最大的段劈一半
            seg = next((s for s in t.segments if s["pos"] <= s["end"] and not s.get("_busy")), None)
            if seg is None:
                seg = self._steal(t)
            if seg is None:
                return
            seg["_busy"] = True
            try:
                ok = await self._fetch_seg(t, fh, lock, seg)
            finally:
                seg.pop("_busy", None)
            if not ok:
                await asyncio.sleep(3)

    @staticmethod
    def _throttle(t):
        """连接太多被拒：每次少开 1 条（不低于正在跑通的数量），3 秒没再被拒就加回 1 条，最后稳定在服务器允许的数量。"""
        now = time.time()
        t.throttled = True
        if now - getattr(t, "last_throttle", 0) < 0.5:
            return  # 同一瞬间被拒的只算一次
        t.conn_limit = max(1, t.active_conns, t.conn_limit - 1)
        t.last_throttle = now

    async def _fetch_seg(self, t, fh, lock, seg):
        """下一段。返回 False = 被服务器限流，这段先放回去。"""
        tries = 0
        while seg["pos"] <= seg["end"]:
            try:
                rng = f"bytes={seg['pos']}-{seg['end']}"
                async with self.session.get(t.url, headers=self._hdrs(t, {"Range": rng})) as r:
                    if r.status in (429, 503) or (r.status == 403 and t.active_conns > 0):
                        self._throttle(t)
                        return False
                    if r.status != 206:
                        raise RuntimeError(f"分段请求返回 {r.status}")
                    t.active_conns += 1
                    try:
                        async for chunk in r.content.iter_chunked(CHUNK):
                            room = seg["end"] - seg["pos"] + 1
                            if room <= 0:
                                break  # 这段后半截被别的线程接走了
                            chunk = chunk[:room]
                            async with lock:
                                fh.seek(seg["pos"])
                                fh.write(chunk)
                            seg["pos"] += len(chunk)
                            tries = 0
                    finally:
                        t.active_conns -= 1
            except asyncio.CancelledError:
                raise
            except aiohttp.ServerDisconnectedError:
                if t.active_conns > 0:
                    self._throttle(t)
                    return False
                tries += 1
                if tries > 6:
                    raise RuntimeError("服务器一直断开连接")
                await asyncio.sleep(min(2 ** tries, 20))
            except Exception as e:
                tries += 1
                if tries > 6:
                    raise RuntimeError(f"分段反复失败：{e}")
                await asyncio.sleep(min(2 ** tries, 20))
        return True

    # ---------- 展示 ----------
    def tick(self):
        now = int(time.time())
        total = 0
        for t in self.tasks.values():
            b = t.done_bytes()
            t.speed = max(0, b - t._last_bytes) if t.state == "running" else 0
            t._last_bytes = b
            total += t.speed
            t.history.append((now, t.speed, 0))
        return total

    def snapshot(self):
        out = []
        for t in self.tasks.values():
            done = t.done_bytes()
            prog = 1.0 if t.state == "done" else (done / t.size if t.size else 0)
            left = (t.size - done) if t.size else 0
            state = {"queued": "排队中", "running": "下载中", "paused": "已暂停", "done": "已完成", "error": "出错"}[t.state]
            if t.state == "running" and t.size == 0 and done == 0:
                state = "连接中"
            health = {"level": "done", "text": "已完成"} if t.state == "done" else \
                {"level": "red", "text": t.error} if t.state == "error" else \
                {"level": "paused", "text": state} if t.state in ("paused", "queued") else \
                {"level": "green", "text": f"{t.active_conns} 线程下载中" + ("" if t.ranges else "（服务器不支持多线程，单线程下载）") + (f"（服务器限制连接数，已自动降到 {t.conn_limit} 线程）" if t.throttled and t.conn_limit < t.threads else "")}
            out.append({
                "type": "http", "id": t.id, "name": t.filename or t.url, "state": state,
                "paused": t.state in ("paused", "error"), "queued": t.state == "queued",
                "progress": prog, "size": t.size, "done": done, "down": t.speed, "up": 0,
                "eta": int(left / t.speed) if t.speed and left else None,
                "save_path": t.save_path, "added": t.added, "category": t.category,
                "completed": t.completed or None,
                "error": t.error if t.state == "error" else "", "health": health,
                "threads": t.active_conns, "url": t.url, "ranges": t.ranges,
                "segments": [[s["start"], s["end"], s["pos"]] for s in t.segments][:64] if t.size else [],
            })
        return out

    def any_running(self):
        return any(t.state in ("running", "queued") for t in self.tasks.values())
