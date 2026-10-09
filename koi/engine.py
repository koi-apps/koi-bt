"""BT 内核：libtorrent 会话、任务管理、红绿灯、吸血屏蔽、分享率、边下边播的数据支持。"""
import ipaddress
import re
import shutil
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

import libtorrent as lt

from . import config as C

CGNAT = ipaddress.ip_network("100.64.0.0/10")

# 只下不传的客户端（参考 qBittorrent-Enhanced-Edition 的默认屏蔽表）
LEECH_PEER_ID = re.compile(r"^-(XL|SD|XF|QD|BN|DL|TS|FG|TT|NX|SP|GT|BC00|UW)\d*", re.I)
LEECH_CLIENT = re.compile(
    r"xunlei|thunder|xfplay|qqdownload|qq旋风|baidu|dandelion|torrentstorm|"
    r"offline[- ]?download|anacrolix/torrent v0\.0|go\.torrent|cacao_torrent|"
    r"dt/torrent|hp/torrent|^7\.|^0\.0\.0|unknown \[ff\]",
    re.I,
)

STATE_TEXT = {
    "checking_files": "校验中",
    "downloading_metadata": "获取种子信息",
    "downloading": "下载中",
    "finished": "已完成",
    "seeding": "做种中",
    "checking_resume_data": "校验中",
}


def ih_of(h):
    return str(h.info_hash())


class Engine:
    def __init__(self, settings, notify):
        self.settings = settings
        self.notify = notify          # notify(kind, title, body)
        self.lock = threading.RLock()
        self.trackers = self._load_tracker_cache()
        self.net = {"upnp": "进行中", "external_ip": None, "cgnat": False, "incoming": False, "listen_ok": None}
        self.avail_cache = {}
        self.first_seen = {}
        self.pending_select = set()   # 等用户选文件的任务
        self.meta = {}                # ih -> {category, done_notified, moved}
        self.banned = {}              # ip -> (客户端名, 时间)
        self.ban_count = 0
        self.history = deque(maxlen=300)   # 全局速度曲线 (时间, 下, 上)
        self.t_history = {}               # 单任务速度曲线
        self.stream_watch = {}            # ih -> {file, pos} 播放器正在读的位置

        params = lt.session_params()
        try:
            params = lt.read_session_params(C.SESSION_FILE.read_bytes())
        except (OSError, RuntimeError):
            pass
        params.settings = self._lt_settings()
        self.ses = lt.session(params)
        self._apply_ip_filter()
        self._load_resumes()

    # ================= 设置 =================
    def _alt_active(self):
        s = self.settings
        if s["alt_enabled"]:
            return True
        if not s["schedule_enabled"]:
            return False
        now = time.localtime()
        if now.tm_wday not in s["schedule_days"]:
            return False
        cur = now.tm_hour * 60 + now.tm_min
        a = [int(x) for x in s["schedule_from"].split(":")]
        b = [int(x) for x in s["schedule_to"].split(":")]
        fa, fb = a[0] * 60 + a[1], b[0] * 60 + b[1]
        return fa <= cur < fb if fa <= fb else (cur >= fa or cur < fb)

    def _lt_settings(self):
        s = self.settings
        port = int(s["listen_port"])
        alt = self._alt_active()
        self._alt_now = alt
        dl = s["alt_download_limit_kb"] if alt else s["download_limit_kb"]
        ul = s["alt_upload_limit_kb"] if alt else s["upload_limit_kb"]
        iface = s["bind_interface"].strip()
        listen = f"{iface}:{port}" if iface else f"0.0.0.0:{port},[::]:{port}"
        enc = {"prefer": 1, "require": 0, "disable": 2}[s["encryption"]]  # pe_policy: forced=0 enabled=1 disabled=2
        return {
            "user_agent": f"KoiDown/{C.VERSION} libtorrent/{lt.__version__}",
            "peer_fingerprint": "-KO1100-",
            "listen_interfaces": listen,
            "outgoing_interfaces": iface,
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "alert_mask": lt.alert_category.error
            | lt.alert_category.status
            | lt.alert_category.storage
            | lt.alert_category.port_mapping,
            "download_rate_limit": int(dl) * 1024,
            "upload_rate_limit": int(ul) * 1024,
            "connections_limit": int(s["max_connections"]),
            "active_downloads": int(s["max_active_downloads"]),
            "active_seeds": 10,
            "active_limit": int(s["max_active_downloads"]) + 20,
            "dont_count_slow_torrents": True,
            "dht_bootstrap_nodes": "dht.libtorrent.org:25401,router.bittorrent.com:6881,"
            "router.utorrent.com:6881,dht.transmissionbt.com:6881",
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "torrent_connect_boost": 30,
            "out_enc_policy": enc,
            "in_enc_policy": enc,
        }

    def apply_settings(self, new):
        for k, v in new.items():
            if k in C.DEFAULT_SETTINGS:
                self.settings[k] = C.coerce(k, v)
        C.save_settings(self.settings)
        self.ses.apply_settings(self._lt_settings())
        self._apply_ip_filter()

    def tick_schedule(self):
        """每分钟检查一次时段限速有没有切换。"""
        if self._alt_active() != getattr(self, "_alt_now", None):
            self.ses.apply_settings(self._lt_settings())

    # ================= tracker =================
    def _load_tracker_cache(self):
        try:
            return [l.strip() for l in C.TRACKER_CACHE.read_text().splitlines() if l.strip()]
        except OSError:
            return []

    def refresh_trackers(self):
        url = self.settings["tracker_url"]
        urls = [url]
        if "raw.githubusercontent.com/ngosang" in url:
            urls.append("https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/" + url.rsplit("/", 1)[-1])
        for u in urls:
            try:
                with urllib.request.urlopen(u, timeout=10) as r:
                    lst = [l.strip() for l in r.read().decode().splitlines() if l.strip()]
                if lst:
                    self.trackers = lst
                    C.TRACKER_CACHE.write_text("\n".join(lst))
                    with self.lock:
                        for h in self.ses.get_torrents():
                            self._add_trackers(h)
                    return len(lst)
            except Exception:
                continue
        return 0

    def _add_trackers(self, h):
        if not self.settings["auto_trackers"]:
            return
        # 私有种子不能乱加 tracker
        ti = h.torrent_file()
        if ti is not None and ti.priv():
            return
        have = {t["url"] for t in h.trackers()}
        for i, url in enumerate(self.trackers):
            if url not in have:
                h.add_tracker({"url": url, "tier": 100 + i})

    # ================= 添加 =================
    def _prepare(self, atp, save_path, category, select_files):
        atp.save_path = save_path or self.settings["save_path"]
        Path(atp.save_path).mkdir(parents=True, exist_ok=True)
        private = atp.ti is not None and atp.ti.priv()
        if self.settings["auto_trackers"] and not private:
            atp.trackers = list(atp.trackers) + [t for t in self.trackers if t not in atp.trackers]
        if select_files is None:
            select_files = self.settings["select_files_on_add"]
        if select_files:
            if atp.ti is None:
                # 磁力链：先不下载任何文件，拿到文件列表后让用户选
                atp.flags |= lt.torrent_flags.default_dont_download
            # 种子文件：前端在添加前就已经展示过文件列表
        return atp

    def preview_torrent(self, data):
        ti = lt.torrent_info(lt.bdecode(data))
        fs = ti.files()
        return {
            "name": ti.name(),
            "size": ti.total_size(),
            "info_hash": str(ti.info_hash()),
            "files": [{"index": i, "path": fs.file_path(i), "size": fs.file_size(i)} for i in range(fs.num_files())],
        }

    def add_magnet(self, uri, save_path=None, category="", select_files=None, paused=False):
        atp = lt.parse_magnet_uri(uri.strip())
        self._prepare(atp, save_path, category, select_files)
        if paused:
            atp.flags |= lt.torrent_flags.paused
            atp.flags &= ~lt.torrent_flags.auto_managed
        with self.lock:
            h = self.ses.add_torrent(atp)
        ih = ih_of(h)
        if atp.flags & lt.torrent_flags.default_dont_download:
            self.pending_select.add(ih)
        return self._after_add(h, category)

    def add_torrent_bytes(self, data, save_path=None, category="", priorities=None, select_files=None, paused=False):
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(lt.bdecode(data))
        self._prepare(atp, save_path, category, False)
        if priorities:
            atp.file_priorities = [int(p) for p in priorities]
        if paused:
            atp.flags |= lt.torrent_flags.paused
            atp.flags &= ~lt.torrent_flags.auto_managed
        with self.lock:
            h = self.ses.add_torrent(atp)
        return self._after_add(h, category)

    def _after_add(self, h, category):
        ih = ih_of(h)
        self.first_seen.setdefault(ih, time.time())
        self.meta.setdefault(ih, {})["category"] = category or self.meta.get(ih, {}).get("category", "")
        h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
        return ih

    def _load_resumes(self):
        import json
        for f in C.RESUME_DIR.glob("*.fastresume"):
            try:
                atp = lt.read_resume_data(f.read_bytes())
                h = self.ses.add_torrent(atp)
                ih = ih_of(h)
                self.first_seen[ih] = time.time()
                mf = f.with_suffix(".meta")
                if mf.exists():
                    self.meta[ih] = json.loads(mf.read_text("utf-8"))
                    if self.meta[ih].get("pending_select"):
                        self.pending_select.add(ih)
            except Exception as e:
                print("恢复失败", f.name, e)

    def _save_meta(self, ih):
        import json
        m = dict(self.meta.get(ih, {}))
        m["pending_select"] = ih in self.pending_select
        (C.RESUME_DIR / f"{ih}.meta").write_text(json.dumps(m, ensure_ascii=False), "utf-8")

    # ================= 操作 =================
    def find(self, ih):
        for h in self.ses.get_torrents():
            if ih_of(h) == ih:
                return h
        return None

    def action(self, ih, act, **kw):
        h = self.find(ih)
        if not h:
            return
        if act == "pause":
            h.unset_flags(lt.torrent_flags.auto_managed)
            h.pause()
            h.save_resume_data()
        elif act == "resume":
            h.set_flags(lt.torrent_flags.auto_managed)
            h.resume()
            self.meta.setdefault(ih, {}).pop("seed_stopped", None)
        elif act == "force_start":  # 无视队列上限强制开始
            h.unset_flags(lt.torrent_flags.auto_managed)
            h.resume()
        elif act == "recheck":
            h.force_recheck()
        elif act == "reannounce":
            self._add_trackers(h)
            h.force_reannounce()
            h.force_dht_announce()
        elif act == "top":
            h.queue_position_top()
        elif act == "up":
            h.queue_position_up()
        elif act == "down":
            h.queue_position_down()
        elif act == "bottom":
            h.queue_position_bottom()
        elif act == "sequential":
            on = not bool(h.status().flags & lt.torrent_flags.sequential_download)
            if on:
                h.set_flags(lt.torrent_flags.sequential_download)
            else:
                h.unset_flags(lt.torrent_flags.sequential_download)
        elif act == "remove":
            self.remove(ih, bool(kw.get("delete_files")))
        elif act == "limit":
            h.set_download_limit(int(kw.get("down_kb", 0)) * 1024 or -1)
            h.set_upload_limit(int(kw.get("up_kb", 0)) * 1024 or -1)
        elif act == "category":
            self.meta.setdefault(ih, {})["category"] = kw.get("category", "")
            self._save_meta(ih)
        elif act == "move":
            dest = kw.get("path", "").strip()
            if dest:
                Path(dest).mkdir(parents=True, exist_ok=True)
                h.move_storage(dest)

    def remove(self, ih, delete_files=False):
        h = self.find(ih)
        if not h:
            return
        opt = lt.options_t.delete_files if delete_files else 0
        with self.lock:
            self.ses.remove_torrent(h, opt)
        for ext in (".fastresume", ".meta"):
            (C.RESUME_DIR / f"{ih}{ext}").unlink(missing_ok=True)
        for d in (self.avail_cache, self.first_seen, self.meta, self.t_history):
            d.pop(ih, None)
        self.pending_select.discard(ih)

    def set_file_priorities(self, ih, prios):
        h = self.find(ih)
        if h:
            h.prioritize_files([int(p) for p in prios])
            if ih in self.pending_select:
                self.pending_select.discard(ih)
                h.unset_flags(lt.torrent_flags.default_dont_download) if hasattr(h, "unset_flags") else None
                self._save_meta(ih)
            h.save_resume_data(lt.save_resume_flags_t.save_info_dict)

    def files(self, ih):
        h = self.find(ih)
        if not h or not h.status().has_metadata:
            return []
        fs = h.torrent_file().files()
        prog = h.file_progress()
        prios = h.get_file_priorities()
        return [
            {"index": i, "path": fs.file_path(i), "size": fs.file_size(i), "done": prog[i], "priority": prios[i]}
            for i in range(fs.num_files())
            if not fs.file_flags(i) & fs.flag_pad_file
        ]

    def export_torrent(self, ih):
        """磁力链拿到信息后导出 .torrent 文件。"""
        h = self.find(ih)
        if not h or not h.status().has_metadata:
            return None, None
        ct = lt.create_torrent(h.torrent_file())
        for t in h.trackers():
            ct.add_tracker(t["url"], t["tier"])
        return h.status().name, lt.bencode(ct.generate())

    def create_torrent(self, path, trackers, private=False, comment="", start_seeding=True):
        p = Path(path)
        if not p.exists():
            raise ValueError("路径不存在")
        fs = lt.file_storage()
        lt.add_files(fs, str(p))
        if fs.num_files() == 0:
            raise ValueError("里面没有文件")
        ct = lt.create_torrent(fs)
        for i, t in enumerate([t.strip() for t in trackers.splitlines() if t.strip()]):
            ct.add_tracker(t, i)
        ct.set_priv(bool(private))
        if comment:
            ct.set_comment(comment)
        ct.set_creator(f"KOI BT {C.VERSION}")
        lt.set_piece_hashes(ct, str(p.parent))
        data = lt.bencode(ct.generate())
        out = C.TORRENT_DIR / f"{p.name}.torrent"
        out.write_bytes(data)
        if start_seeding:
            atp = lt.add_torrent_params()
            atp.ti = lt.torrent_info(lt.bdecode(data))
            atp.save_path = str(p.parent)
            atp.flags |= lt.torrent_flags.seed_mode
            with self.lock:
                h = self.ses.add_torrent(atp)
            ih = self._after_add(h, "")
            self.meta.setdefault(ih, {})["own"] = True   # 自己分享的种子，不受「做种多久就停」限制
            self._save_meta(ih)
        return str(out), data

    # ================= 红绿灯 =================
    def _reachable(self, h, st):
        ih = str(st.info_hash)
        cached = self.avail_cache.get(ih)
        if cached and time.time() - cached[0] < 3:
            return cached[1]
        avail = h.piece_availability()
        have = st.pieces
        prios = h.get_piece_priorities()
        wanted = [i for i, p in enumerate(prios) if p > 0] or range(len(avail))
        ok = sum(1 for i in wanted if have[i] or avail[i] > 0)
        frac = ok / max(1, len(wanted))
        self.avail_cache[ih] = (time.time(), frac)
        return frac

    def _health(self, h, st, ih):
        age = time.time() - self.first_seen.get(ih, time.time())
        if ih in self.pending_select and st.has_metadata:
            return {"level": "select", "text": "种子信息拿到了，请选择要下载的文件"}
        if st.is_seeding or st.is_finished:
            return {"level": "done", "text": "已完成"}
        if not st.has_metadata:
            if age > 180 and st.num_peers == 0:
                return {"level": "red", "text": "3 分钟没找到任何人，种子可能已经死了"}
            return {"level": "wait", "text": "正在全网找种子信息…"}
        if st.num_seeds > 0 or st.distributed_copies >= 1:
            return {"level": "green", "text": "线上有完整文件，可以下完"}
        reach = self._reachable(h, st)
        if reach >= 0.9999:
            return {"level": "green", "text": "线上各部分凑得齐，可以下完"}
        if age < 120:
            return {"level": "wait", "text": f"观察中：目前最多能下到 {reach*100:.1f}%"}
        if st.list_seeds > 0:
            return {"level": "yellow", "text": f"tracker 说有 {st.list_seeds} 个人有完整文件，但还没连上。目前最多下到 {reach*100:.1f}%"}
        return {"level": "red", "text": f"线上没有完整文件，最多只能下到 {reach*100:.1f}%。建议换一个做种多的种子"}

    # ================= 列表 =================
    def snapshot(self):
        out = []
        for h in self.ses.get_torrents():
            st = h.status()
            ih = str(st.info_hash)
            paused = bool(st.flags & lt.torrent_flags.paused)
            auto = bool(st.flags & lt.torrent_flags.auto_managed)
            state = STATE_TEXT.get(str(st.state), str(st.state))
            if paused:
                state = "排队中" if auto else "已暂停"
                if st.is_finished and self.meta.get(ih, {}).get("seed_stopped"):
                    state = "已完成（做种结束）"
            selecting = ih in self.pending_select
            if selecting:
                state = "等待选择文件" if st.has_metadata else "获取种子信息"
            if st.has_incoming:
                self.net["incoming"] = True
            left = st.total_wanted - st.total_wanted_done
            rate = st.download_payload_rate
            ratio = st.all_time_upload / st.total_wanted_done if st.total_wanted_done else 0
            out.append({
                "type": "bt",
                "id": ih,
                "name": st.name or "（获取名字中）",
                "state": state,
                "paused": paused and not auto,
                "queued": paused and auto,
                "progress": 0 if selecting else st.progress,
                "size": st.total_wanted,
                "done": st.total_wanted_done,
                "down": rate,
                "up": st.upload_payload_rate,
                "uploaded": st.all_time_upload,
                "ratio": round(ratio, 2),
                "seeds": st.num_seeds,
                "peers": st.num_peers - st.num_seeds,
                "seeds_total": max(st.list_seeds, st.num_complete),
                "peers_total": max(st.list_peers, st.num_incomplete),
                "availability": round(st.distributed_copies, 3) if st.has_metadata else None,
                "eta": int(left / rate) if rate > 0 and left > 0 else None,
                "save_path": st.save_path,
                "added": st.added_time,
                "queue": st.queue_position,
                "sequential": bool(st.flags & lt.torrent_flags.sequential_download),
                "category": self.meta.get(ih, {}).get("category", ""),
                "error": st.errc.message() if st.errc.value() else "",
                "health": ({"level": "done", "text": state} if st.is_finished else {"level": "paused", "text": state}) if paused else self._health(h, st, ih),
                "need_select": ih in self.pending_select and st.has_metadata,
                "seed": self._seed_info(st, ih, paused),
                "has_metadata": st.has_metadata,
            })
        return out

    def _seed_info(self, st, ih, paused):
        """正在做种时给界面：还要做多久（按设置的时长 / 分享率，哪个先到算哪个）。"""
        if not st.is_seeding or paused:
            return None
        s = self.settings
        if self.meta.get(ih, {}).get("own"):
            return {"left": None, "why": "own"}
        left = None
        if s["seed_time_limit_min"] > 0:
            left = max(0, s["seed_time_limit_min"] * 60 - int(st.seeding_duration.total_seconds()))
        ratio_target = s["ratio_limit"] if s["ratio_limit"] > 0 else None
        return {"left": left, "ratio_target": ratio_target, "why": "limit" if (left is not None or ratio_target) else "forever"}

    def details(self, ih):
        h = self.find(ih)
        if not h:
            return None
        st = h.status()
        peers = []
        for p in h.get_peer_info():
            client = p.client.decode("utf-8", "replace") if isinstance(p.client, bytes) else str(p.client)
            peers.append({
                "ip": f"{p.ip[0]}:{p.ip[1]}",
                "client": client or self._pid_prefix(p) or "未知",
                "progress": p.progress,
                "down": p.payload_down_speed,
                "up": p.payload_up_speed,
                "downloaded": p.total_download,
                "uploaded": p.total_upload,
                "seed": bool(p.flags & lt.peer_info.seed),
                "incoming": not bool(p.flags & lt.peer_info.local_connection) if hasattr(lt.peer_info, "local_connection") else None,
                "encrypted": bool(p.flags & (lt.peer_info.rc4_encrypted | lt.peer_info.plaintext_encrypted)),
                "source": "DHT" if p.source & lt.peer_info.dht else "PEX" if p.source & lt.peer_info.pex else "LSD" if p.source & lt.peer_info.lsd else "Tracker",
            })
        peers.sort(key=lambda p: -(p["down"] + p["up"]))
        trackers = []
        for t in h.trackers():
            msg, status = "", "未联系"
            seeds = peers_n = -1
            for ep in t.get("endpoints", []):
                for ih_ in ep.get("info_hashes", [])[:1]:
                    if ih_.get("message"):
                        msg = ih_["message"]
                    if ih_.get("updating"):
                        status = "正在联系"
                    elif ih_.get("fails", 0) > 0:
                        status = "失败"
                    elif ih_.get("start_sent"):
                        status = "正常"
                    seeds = max(seeds, ih_.get("scrape_complete", -1))
                    peers_n = max(peers_n, ih_.get("scrape_incomplete", -1))
            trackers.append({"url": t["url"], "tier": t["tier"], "status": status, "message": msg, "seeds": seeds, "peers": peers_n})
        trackers.sort(key=lambda t: ({"正常": 0, "正在联系": 1, "未联系": 2, "失败": 3}[t["status"]], t["tier"]))
        # 分块图：压缩到 300 格，每格 = 已下完的比例
        pieces = list(st.pieces) if st.has_metadata else []
        bars = []
        if pieces:
            n = len(pieces)
            k = min(300, n)
            for i in range(k):
                a, b = i * n // k, max(i * n // k + 1, (i + 1) * n // k)
                bars.append(round(sum(pieces[a:b]) / (b - a), 2))
        ti = h.torrent_file() if st.has_metadata else None
        return {
            "peers": peers[:200],
            "trackers": trackers,
            "pieces": bars,
            "history": list(self.t_history.get(ih, [])),
            "info": {
                "name": st.name,
                "info_hash": ih,
                "save_path": st.save_path,
                "size": st.total_wanted,
                "pieces": st.num_pieces,
                "piece_size": ti.piece_length() if ti else 0,
                "total_pieces": ti.num_pieces() if ti else 0,
                "comment": ti.comment() if ti else "",
                "creator": ti.creator() if ti else "",
                "private": ti.priv() if ti else False,
                "added": st.added_time,
                "completed": st.completed_time,
                "downloaded": st.all_time_download,
                "uploaded": st.all_time_upload,
                "wasted": st.total_failed_bytes + st.total_redundant_bytes,
                "connections": st.num_connections,
                "magnet": lt.make_magnet_uri(h),
                "down_limit": h.download_limit() // 1024 if h.download_limit() > 0 else 0,
                "up_limit": h.upload_limit() // 1024 if h.upload_limit() > 0 else 0,
            },
        }

    @staticmethod
    def _pid_prefix(p):
        try:
            raw = bytes.fromhex(str(p.pid))[:8]
            s = raw.decode("latin-1")
            return s if s.startswith("-") else ""
        except Exception:
            return ""

    # ================= 吸血屏蔽 =================
    def _is_leech(self, p):
        client = p.client.decode("utf-8", "replace") if isinstance(p.client, bytes) else str(p.client)
        pid = self._pid_prefix(p)
        extra = [x.strip().lower() for x in self.settings["ban_list_extra"].split(",") if x.strip()]
        if LEECH_PEER_ID.match(pid) or LEECH_CLIENT.search(client):
            return client or pid
        low = (client + " " + pid).lower()
        for kw in extra:
            if kw in low:
                return client or pid
        # 行为检测：从我们这拿了 20MB 以上，自己却一直报告进度 0，还一点都不给我们
        if p.total_upload > 20 * 1024 * 1024 and p.progress == 0 and p.total_download == 0:
            return (client or pid or "未知") + "（只拿不给）"
        return None

    def scan_leechers(self):
        if not self.settings["anti_leech"]:
            return
        new = False
        for h in self.ses.get_torrents():
            for p in h.get_peer_info():
                ip = p.ip[0]
                if ip in self.banned:
                    continue
                why = self._is_leech(p)
                if why:
                    self.banned[ip] = (why, time.time())
                    self.ban_count += 1
                    new = True
        if new:
            self._apply_ip_filter()

    def _apply_ip_filter(self):
        f = lt.ip_filter()
        ips = list(self.banned) if self.settings["anti_leech"] else []
        for line in self.settings["ip_blocklist"].splitlines():
            line = line.split("#")[0].strip()
            if line:
                ips.append(line)
        for item in ips:
            try:
                if "-" in item:
                    a, b = [x.strip() for x in item.split("-", 1)]
                elif "/" in item:
                    net = ipaddress.ip_network(item, strict=False)
                    a, b = str(net[0]), str(net[-1])
                else:
                    a = b = item
                f.add_rule(a, b, 1)
            except Exception:
                continue
        self.ses.set_ip_filter(f)

    def unban_all(self):
        self.banned.clear()
        self._apply_ip_filter()

    # ================= 定时任务 =================
    def tick(self):
        """每秒调一次：速度曲线。"""
        down = up = 0
        now = time.time()
        for h in self.ses.get_torrents():
            st = h.status()
            down += st.download_payload_rate
            up += st.upload_payload_rate
            ih = str(st.info_hash)
            dq = self.t_history.setdefault(ih, deque(maxlen=300))
            dq.append((int(now), st.download_payload_rate, st.upload_payload_rate))
        self.history.append((int(now), down, up))
        return down, up

    def tick_slow(self):
        """每 5 秒：吸血扫描、分享率、边下边播的优先级。"""
        self.scan_leechers()
        s = self.settings
        for h in self.ses.get_torrents():
            st = h.status()
            if not st.is_seeding or st.flags & lt.torrent_flags.paused:
                continue
            if self.meta.get(str(st.info_hash), {}).get("own"):
                continue
            ratio = st.all_time_upload / max(1, st.total_wanted_done)
            stop = (not s["seed_after_done"]) \
                or (s["ratio_limit"] > 0 and ratio >= s["ratio_limit"]) \
                or (s["seed_time_limit_min"] > 0 and st.seeding_duration.total_seconds() >= s["seed_time_limit_min"] * 60)
            if stop:
                h.unset_flags(lt.torrent_flags.auto_managed)
                h.pause()
                ih = str(st.info_hash)
                self.meta.setdefault(ih, {})["seed_stopped"] = True
                self._save_meta(ih)
        self._stream_deadlines()

    def any_downloading(self):
        for h in self.ses.get_torrents():
            st = h.status()
            if not st.is_finished and not (st.flags & lt.torrent_flags.paused):
                return True
        return False

    # ================= 边下边播 =================
    def stream_file_info(self, ih, index):
        h = self.find(ih)
        if not h or not h.status().has_metadata:
            return None
        ti = h.torrent_file()
        fs = ti.files()
        return {
            "path": Path(h.status().save_path) / fs.file_path(index),
            "size": fs.file_size(index),
            "offset": fs.file_offset(index),
            "piece_len": ti.piece_length(),
            "name": fs.file_name(index),
            "handle": h,
        }

    def prepare_stream(self, ih, index):
        """开始播放：打开顺序下载，优先下文件开头和结尾（视频索引一般在两头）。"""
        info = self.stream_file_info(ih, index)
        if not info:
            return None
        h = info["handle"]
        prios = h.get_file_priorities()
        if prios[index] == 0:
            prios[index] = 4
            h.prioritize_files(prios)
        if ih in self.pending_select:
            self.pending_select.discard(ih)
            self._save_meta(ih)
        h.set_flags(lt.torrent_flags.sequential_download)
        h.unset_flags(lt.torrent_flags.default_dont_download) if hasattr(h, "unset_flags") else None
        h.resume()
        pl = info["piece_len"]
        first = info["offset"] // pl
        last = (info["offset"] + info["size"] - 1) // pl
        for i, pc in enumerate(range(first, min(first + 5, last + 1))):
            h.set_piece_deadline(pc, 500 + i * 200)
        for i, pc in enumerate(range(max(first, last - 2), last + 1)):
            h.set_piece_deadline(pc, 800 + i * 200)
        self.stream_watch[ih] = {"file": index, "pos": 0}
        return info

    def want_range(self, ih, index, start, end):
        """播放器要读 [start, end) 这段：把对应的块设为最急。返回这段是否已经齐了。"""
        info = self.stream_file_info(ih, index)
        h = info["handle"]
        pl = info["piece_len"]
        a = (info["offset"] + start) // pl
        b = (info["offset"] + max(start, end - 1)) // pl
        self.stream_watch[ih] = {"file": index, "pos": start}
        ready = True
        for i, pc in enumerate(range(a, b + 1)):
            if not h.have_piece(pc):
                ready = False
                h.set_piece_deadline(pc, 100 + i * 50)
        return ready

    def _stream_deadlines(self):
        # 播放位置往后预读约 20 块
        for ih, w in list(self.stream_watch.items()):
            info = self.stream_file_info(ih, w["file"])
            if not info:
                self.stream_watch.pop(ih, None)
                continue
            h = info["handle"]
            pl = info["piece_len"]
            a = (info["offset"] + w["pos"]) // pl
            last = (info["offset"] + info["size"] - 1) // pl
            for i, pc in enumerate(range(a, min(a + 20, last + 1))):
                if not h.have_piece(pc):
                    h.set_piece_deadline(pc, 1000 + i * 300)

    # ================= 后台事件 =================
    def handle_alerts(self):
        events = []
        for a in self.ses.pop_alerts():
            t = type(a).__name__
            if t == "save_resume_data_alert":
                ih = ih_of(a.handle)
                (C.RESUME_DIR / f"{ih}.fastresume").write_bytes(lt.write_resume_data_buf(a.params))
                self._save_meta(ih)
            elif t == "metadata_received_alert":
                h = a.handle
                ih = ih_of(h)
                if ih in self.pending_select:
                    fs = h.torrent_file().files()
                    real = [i for i in range(fs.num_files()) if not fs.file_flags(i) & fs.flag_pad_file]
                    if len(real) <= 1:  # 单文件不用选
                        self.set_file_priorities(ih, [4] * fs.num_files())
                    else:
                        events.append(("select", ih))
                self._add_trackers(h)
                h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
            elif t == "torrent_finished_alert":
                h = a.handle
                ih = ih_of(h)
                st = h.status()
                m = self.meta.setdefault(ih, {})
                if st.total_wanted_done > 0 and not m.get("done_notified") and ih not in self.pending_select:
                    m["done_notified"] = True
                    events.append(("done", st.name))
                    dest = self.settings["move_done_to"].strip()
                    if dest and not m.get("moved"):
                        cat = m.get("category", "")
                        target = str(Path(dest) / cat) if cat else dest
                        Path(target).mkdir(parents=True, exist_ok=True)
                        h.move_storage(target)
                        m["moved"] = True
                h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
                self.stream_watch.pop(ih, None)
            elif t == "portmap_alert":
                self.net["upnp"] = f"成功（外部端口 {a.external_port}）"
            elif t == "portmap_error_alert":
                if not self.net["upnp"].startswith("成功"):
                    self.net["upnp"] = "失败：路由器没开 UPnP 或不支持"
            elif t == "external_ip_alert":
                ip = str(a.external_address)
                self.net["external_ip"] = ip
                try:
                    addr = ipaddress.ip_address(ip)
                    self.net["cgnat"] = addr.version == 4 and (addr in CGNAT or addr.is_private)
                except ValueError:
                    pass
            elif t == "listen_succeeded_alert":
                self.net["listen_ok"] = True
            elif t == "listen_failed_alert":
                self.net["listen_ok"] = False
            elif t in ("storage_moved_alert",):
                a.handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
            elif t in ("file_error_alert", "torrent_error_alert"):
                h = a.handle
                ih = ih_of(h)
                m = self.meta.setdefault(ih, {})
                now = time.time()
                # 文件被移走 / 删了 / 没勾的文件的边角数据丢了：自动清错误 + 重新校验一次，缺的部分会重新下
                if not m.get("auto_fixed") or now - m["auto_fixed"] > 600:
                    m["auto_fixed"] = now
                    try:
                        h.clear_error()
                        h.force_recheck()
                    except Exception:
                        pass
                    events.append(("error", f"{h.status().name}：读写文件出错，已自动重新校验（{a.message()[-80:]}）"))
                # 10 分钟内同一个任务只提示一次，不刷屏
        return events

    def save_all(self):
        for h in self.ses.get_torrents():
            h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
        C.SESSION_FILE.write_bytes(lt.write_session_params_buf(self.ses.session_state()))

    def summary_part(self):
        return {
            "dht": self.ses.is_dht_running(),
            "dht_nodes": None,
            "trackers": len(self.trackers),
            "net": self.net,
            "banned": self.ban_count,
            "banned_list": [{"ip": ip, "client": c, "time": t} for ip, (c, t) in list(self.banned.items())[-100:]],
            "alt_active": getattr(self, "_alt_now", False),
        }
