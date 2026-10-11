"""视频网站下载（YouTube、B 站、X/Twitter 等上千个网站），用开源的 yt-dlp。

- 视频和音频分开的网站（YouTube 高清、B 站）要用 ffmpeg 合并，ffmpeg 由 imageio-ffmpeg 自带
- 界面上和直链任务长得一样（type = http），id 以 v 开头
- 暂停 = 停掉 yt-dlp；继续 = 重新跑一遍，yt-dlp 会接着 .part 文件续传
"""
import glob
import hashlib
import json
import os
import threading
import time

from . import config as C
from . import net

VIDEO_DIR = C.DATA_DIR / "video"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

_extractors = None
_lock = threading.Lock()


def _load_extractors():
    global _extractors
    with _lock:
        if _extractors is None:
            from yt_dlp.extractor import gen_extractor_classes
            _extractors = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]
    return _extractors


def is_video_page(url):
    """这个网址是不是 yt-dlp 认得的视频网站页面（不算通用的「网页里找视频」）。"""
    try:
        return any(ie.suitable(url) for ie in _load_extractors())
    except Exception:
        return False


def ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


class _Stop(Exception):
    pass


class VideoTask:
    FIELDS = ("id", "url", "save_path", "category", "title", "filename", "vid", "state", "size", "done", "added", "completed", "error")

    def __init__(self, **kw):
        self.id = kw.get("id") or "v" + hashlib.sha1(f"{kw['url']}{time.time()}".encode()).hexdigest()[:15]
        self.url = kw["url"]
        self.save_path = kw["save_path"]
        self.category = kw.get("category", "")
        self.title = kw.get("title", "")
        self.filename = kw.get("filename", "")
        self.vid = kw.get("vid", "")             # 网站上的视频 id，文件名里 [ ] 中那段
        self.state = kw.get("state", "queued")          # queued / running / paused / done / error
        self.size = kw.get("size", 0)
        self.done = kw.get("done", 0)
        self.added = kw.get("added") or int(time.time())
        self.completed = kw.get("completed", 0)
        self.error = kw.get("error", "")
        self.speed = 0
        self.eta = None
        self.part = ""          # 例如「视频 1/2」
        self.thread = None
        self.stop = False
        self.history = []

    def save(self):
        (VIDEO_DIR / f"{self.id}.json").write_text(json.dumps({k: getattr(self, k) for k in self.FIELDS}, ensure_ascii=False), "utf-8")


class VideoManager:
    def __init__(self, settings, notify):
        self.settings = settings
        self.notify = notify
        self.tasks = {}
        for f in VIDEO_DIR.glob("*.json"):
            try:
                t = VideoTask(**json.loads(f.read_text("utf-8")))
                if t.state == "running":
                    t.state = "queued"
                self.tasks[t.id] = t
            except Exception as e:
                print("恢复视频任务失败", f.name, e)
        self.schedule()

    # ---------- 添加 / 调度 ----------
    def add(self, url, save_path=None, category=""):
        t = VideoTask(url=url, save_path=save_path or self.settings["save_path"], category=category)
        os.makedirs(t.save_path, exist_ok=True)
        self.tasks[t.id] = t
        t.save()
        self.schedule()
        return t.id

    def schedule(self):
        limit = int(self.settings.get("max_active_downloads", 5)) or 999
        running = sum(1 for t in self.tasks.values() if t.state == "running")
        for t in sorted(self.tasks.values(), key=lambda x: x.added):
            if running >= max(limit, 1):
                break
            if t.state == "queued":
                t.state = "running"
                t.stop = False
                t.thread = threading.Thread(target=self._run, args=(t,), name=f"video-{t.id}", daemon=True)
                t.thread.start()
                running += 1

    def _opts(self, t):
        ff = ffmpeg_path()
        opts = {
            "outtmpl": os.path.join(t.save_path, "%(title).150B [%(id)s].%(ext)s"),
            "format": "bv*+ba/b" if ff else "b",
            "merge_output_format": "mp4",
            "continuedl": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [lambda d: self._hook(t, d)],
            "postprocessor_hooks": [lambda d: self._pp_hook(t, d)],
        }
        if ff:
            opts["ffmpeg_location"] = ff
        if net.enabled(self.settings) and self.settings.get("proxy_http", True):
            opts["proxy"] = net.url(self.settings)
        return opts

    def _hook(self, t, d):
        if t.stop:
            raise _Stop()
        if d.get("status") == "downloading":
            info = d.get("info_dict") or {}
            t.title = t.title or info.get("title", "")
            t.vid = t.vid or info.get("id", "")
            t.size = d.get("total_bytes") or d.get("total_bytes_estimate") or t.size
            t.done = d.get("downloaded_bytes") or 0
            t.speed = int(d.get("speed") or 0)
            t.eta = d.get("eta")
            fmts = info.get("requested_formats") or []
            if len(fmts) > 1:
                fid = info.get("format_id", "")
                idx = next((i for i, f in enumerate(fmts) if f.get("format_id") == fid), 0)
                t.part = f"{'视频' if idx == 0 else '音频'} {idx + 1}/{len(fmts)}"
        elif d.get("status") == "finished":
            t.done = t.size

    def _pp_hook(self, t, d):
        if d.get("status") == "started" and d.get("postprocessor") == "Merger":
            t.part = "合并音视频"

    def _run(self, t):
        import yt_dlp
        t.error = ""
        try:
            with yt_dlp.YoutubeDL(self._opts(t)) as ydl:
                info = ydl.extract_info(t.url, download=True)
                t.title = info.get("title") or t.title
                path = info.get("requested_downloads", [{}])[0].get("filepath") or ydl.prepare_filename(info)
                t.filename = os.path.basename(path)
                try:
                    t.size = t.done = os.path.getsize(path)
                except OSError:
                    pass
            t.state = "done"
            t.completed = int(time.time())
            t.part = ""
            self.notify("done", "下载完成", t.filename or t.title)
        except _Stop:
            pass                                           # 暂停 / 删除，状态由 action 设好了
        except Exception as e:
            if not t.stop:
                t.state = "error"
                t.error = _friendly(str(e))
        finally:
            t.speed = 0
            t.thread = None
            t.save()
            self.schedule()

    # ---------- 操作 ----------
    def action(self, tid, act, **kw):
        t = self.tasks.get(tid)
        if not t:
            return
        if act == "pause" and t.state in ("running", "queued"):
            t.stop = True
            t.state = "paused"
            t.save()
        elif act in ("resume", "force_start") and t.state in ("paused", "error"):
            t.state = "queued"
            t.save()
            self.schedule()
        elif act == "remove":
            t.stop = True
            if t.thread:
                t.thread.join(timeout=10)
            if kw.get("delete_files"):
                vid = t.vid or _vid(t)
                tag = f"[{vid}]" if vid else None
                pats = [os.path.join(t.save_path, t.filename)] if t.filename else []
                if tag:
                    pats.append(os.path.join(t.save_path, f"*{glob.escape(tag)}*"))
                for pat in pats:
                    for p in glob.glob(pat):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
            (VIDEO_DIR / f"{t.id}.json").unlink(missing_ok=True)
            self.tasks.pop(tid, None)
            self.schedule()

    def tick(self):
        now = int(time.time())
        for t in self.tasks.values():
            t.history.append((now, t.speed, 0))
            del t.history[:-600]

    def any_running(self):
        return any(t.state in ("running", "queued") for t in self.tasks.values())

    def snapshot(self):
        out = []
        for t in self.tasks.values():
            prog = 1.0 if t.state == "done" else (t.done / t.size if t.size else 0)
            state = {"queued": "排队中", "running": "下载中", "paused": "已暂停", "done": "已完成", "error": "出错"}[t.state]
            if t.state == "running" and not t.size:
                state = "解析视频中"
            health = {"level": "done", "text": "已完成"} if t.state == "done" else \
                {"level": "red", "text": t.error} if t.state == "error" else \
                {"level": "paused", "text": state} if t.state in ("paused", "queued") else \
                {"level": "green", "text": "视频网站下载" + (f"：{t.part}" if t.part else "")}
            out.append({
                "type": "http", "video": True, "id": t.id, "name": t.filename or t.title or t.url, "state": state,
                "paused": t.state in ("paused", "error"), "queued": t.state == "queued",
                "progress": prog, "size": t.size, "done": t.done, "down": t.speed, "up": 0,
                "eta": int(t.eta) if t.eta else None,
                "save_path": t.save_path, "added": t.added, "category": t.category,
                "completed": t.completed or None,
                "error": t.error if t.state == "error" else "", "health": health,
                "threads": 1, "url": t.url, "ranges": True, "segments": [],
            })
        return out


def _vid(t):
    """文件名里方括号中的视频 id，删除时用来找 .part 等残留文件。"""
    import re
    m = re.search(r"\[([^\[\]]+)\]\.[^.]+$", t.filename or "")
    return m.group(1) if m else ""


def _friendly(msg):
    """把 yt-dlp 的报错翻成人话。"""
    low = msg.lower()
    if "geo" in low or "not available in your country" in low:
        return "这个视频在你所在的地区看不了，可以在设置里配代理再试"
    if "private" in low or "sign in" in low or "login" in low or "members" in low:
        return "这个视频要登录或者是会员 / 私密视频，下载不了"
    if "unsupported url" in low:
        return "不支持这个网站"
    if "deleted" in low or "unavailable" in low or "not found" in low or "404" in low:
        return "视频不存在或已被删除"
    return msg.replace("ERROR: ", "")[:200]
