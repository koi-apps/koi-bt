"""在线更新（和 KOI MOD 同一套思路）：

服务器放 latest.json = {"manifest": base64(清单 JSON), "sig": base64(ECDSA P-256 签名)}，
清单 = {"v", "notes", "size", "sha256", "file", "mandatory"}。必须验签通过才认，防止被人冒充更新服务器。
下载整包 zip → 校验大小 + SHA-256 → 解压到临时目录 → 主程序存好断点退出 →
小脚本等主程序退出后把新文件覆盖到安装目录 → 启动新版本。
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from . import config as C

# 发布签名的公钥（私钥只在发布机上，不进安装包）
PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEx0FXzsMMpcjKpeHPUeoe24mdIqOX
1emn9V8mmFAKL4Thca+PAZqa27dw3xX7dn/8URzMFUrhdfYwJwsalCo9eA==
-----END PUBLIC KEY-----"""


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def same_file(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and _sha(a) == _sha(b)
    except OSError:
        return False


def ver_tuple(s):
    out = []
    for p in str(s).split("."):
        n = "".join(c for c in p if c.isdigit())
        out.append(int(n) if n else 0)
    return tuple(out + [0] * (3 - len(out)))


def verify(blob):
    """验签成功返回清单 dict，否则 None。"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        raw = base64.b64decode(blob["manifest"])
        sig = base64.b64decode(blob["sig"])
        key = serialization.load_pem_public_key(PUBLIC_KEY_PEM)
        key.verify(sig, raw, ec.ECDSA(hashes.SHA256()))
        return json.loads(raw)
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return None


class Updater:
    def __init__(self, settings, on_ready_to_quit):
        self.settings = settings
        self.on_ready_to_quit = on_ready_to_quit   # 主程序退出的回调（窗口版由 main.py 注入）
        self.info = None
        self.base = None
        self.state = {"stage": "idle", "done": 0, "total": 0, "msg": ""}
        self.stage_dir = None
        self.last_check = None

    def bases(self):
        custom = [u.strip().rstrip("/") for u in (self.settings.get("update_url") or "").split(",") if u.strip()]
        return custom or [C.OFFICIAL_UPDATE_URL]

    def _open(self, url, timeout=10):
        req = urllib.request.Request(url, headers={"User-Agent": f"KoiDown/{C.VERSION}"})
        return urllib.request.urlopen(req, timeout=timeout)

    def check(self):
        errs = []
        for base in self.bases():
            try:
                with self._open(f"{base}/latest.json", timeout=6) as r:
                    blob = json.load(r)
            except Exception as e:
                errs.append(f"{base}: {e}")
                continue
            m = verify(blob)
            if not m or not all(k in m for k in ("v", "size", "sha256", "file")):
                return self._result({"ok": False, "msg": "更新信息签名校验失败，已忽略（可能有人冒充更新服务器）"})
            self.info, self.base = m, base
            newer = ver_tuple(m["v"]) > ver_tuple(C.VERSION)
            return self._result({"ok": True, "update": newer, "current": C.VERSION, "version": m["v"],
                                 "notes": m.get("notes", ""), "size": m["size"], "mandatory": bool(m.get("mandatory")) and newer})
        return self._result({"ok": False, "msg": "连不上更新服务器", "detail": errs[-1] if errs else ""})

    def _result(self, r):
        r["time"] = time.time()
        self.last_check = r
        return r

    def start(self, auto_apply=False):
        """开始下载。默认下完只提示，等用户点「立即重启更新」或退出 KOI 时再装（auto_apply=True 才下完直接装）。"""
        self.auto_apply = auto_apply
        if not self.info or self.state["stage"] in ("downloading", "ready"):
            return self.state["stage"] == "ready"
        if not getattr(sys, "frozen", False):
            self.state = {"stage": "error", "done": 0, "total": 0, "msg": "源码运行的版本不能自动更新"}
            return False
        self.state = {"stage": "downloading", "done": 0, "total": int(self.info["size"]), "msg": ""}
        threading.Thread(target=self._download, daemon=True).start()
        return True

    def _download(self):
        m = self.info
        tmp = tempfile.gettempdir()
        zpath = os.path.join(tmp, f"koi_update_{m['v']}.zip")
        try:
            h = hashlib.sha256()
            url = m.get("url") or f"{self.base}/{m['file']}"   # 清单里有绝对地址就用它（GitHub Releases 固定到具体版本）
            with self._open(url, timeout=30) as r, open(zpath, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    self.state["done"] += len(chunk)
            if self.state["done"] != int(m["size"]) or h.hexdigest() != m["sha256"].lower():
                os.remove(zpath)
                self.state = {"stage": "error", "done": 0, "total": 0, "msg": "下载的文件校验不通过，请重试"}
                return
            self.state["stage"] = "unpacking"
            stage = os.path.join(tmp, f"koi_update_{m['v']}")
            self.stage_root = stage
            shutil.rmtree(stage, ignore_errors=True)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(stage)
            os.remove(zpath)
            # 压缩包里可能多包了一层文件夹
            exe_name = os.path.basename(sys.executable)
            subs = [d for d in os.listdir(stage) if os.path.isdir(os.path.join(stage, d))]
            if not os.path.exists(os.path.join(stage, exe_name)) and len(subs) == 1 \
                    and os.path.exists(os.path.join(stage, subs[0], exe_name)):
                stage = os.path.join(stage, subs[0])
            if not os.path.exists(os.path.join(stage, exe_name)):
                self.state = {"stage": "error", "done": 0, "total": 0, "msg": "更新包内容不对"}
                return
            self.stage_dir = stage
            self.state["stage"] = "ready"
            if getattr(self, "auto_apply", False):
                self.apply()
        except Exception as e:
            self.state = {"stage": "error", "done": 0, "total": 0, "msg": f"下载失败：{e}"}

    def _mark(self):
        try:
            (C.DATA_DIR / "just_updated.json").write_text(json.dumps(
                {"from": C.VERSION, "to": self.info["v"], "notes": self.info.get("notes", "")}, ensure_ascii=False), "utf-8")
        except Exception:
            pass

    def install_on_exit(self):
        """用户选了「稍后」：程序退出时装好，不重启，下次打开就是新版。"""
        if self.state["stage"] != "ready" or not self.stage_dir:
            return False
        self._mark()
        return self._apply_after_exit(sys.executable, os.path.dirname(sys.executable), relaunch=False)

    def apply(self):
        """原地换文件：Windows 允许给正在运行的 exe / 已加载的 dll 改名，所以把旧文件改名成 .koiold，
        新文件放到原位置；任何一个失败就整体回滚，旧版本照常运行。换完启动新版本，再让当前程序正常退出。
        （不用外部脚本：如果当前程序是被计划任务之类拉起的，退出时外部脚本会被一起杀掉。）"""
        if self.state["stage"] != "ready" or not self.stage_dir:
            return False
        exe = sys.executable
        dest = os.path.dirname(exe)
        self._mark()
        swapped, created = [], []
        try:
            for root, _dirs, files in os.walk(self.stage_dir):
                rel = os.path.relpath(root, self.stage_dir)
                tdir = os.path.normpath(os.path.join(dest, rel))
                if not os.path.isdir(tdir):
                    os.makedirs(tdir)
                    created.append(tdir)
                for f in files:
                    src, tgt = os.path.join(root, f), os.path.join(tdir, f)
                    if same_file(src, tgt):
                        continue   # 内容没变的不动（比如 Python 自带的库，有的文件运行中改不了名）
                    old = None
                    if os.path.exists(tgt):
                        old = tgt + ".koiold"
                        if os.path.exists(old):
                            try:
                                os.remove(old)
                            except OSError:
                                old = f"{tgt}.{int(time.time()*1000)}.koiold"
                        os.replace(tgt, old)
                    swapped.append((tgt, old))
                    shutil.copy2(src, tgt)
        except Exception as e:
            # 回滚：删新文件，旧文件改回原名
            for tgt, old in reversed(swapped):
                try:
                    if os.path.exists(tgt):
                        os.remove(tgt)
                    if old:
                        os.replace(old, tgt)
                except OSError:
                    pass
            # 有文件运行中换不了：改成「程序退出后由小脚本覆盖」
            if isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32):
                return self._apply_after_exit(exe, dest)
            self.state = {"stage": "error", "done": 0, "total": 0, "msg": f"安装失败，已恢复旧版本：{e}"}
            return False
        shutil.rmtree(self.stage_root, ignore_errors=True)
        # 新进程要和当前进程脱钩：清掉 PyInstaller 的环境变量，告诉它「旧的马上退出，别当成多开」
        env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        env["KOI_AFTER_UPDATE"] = "1"
        flags = 0x00000008 | 0x00000200       # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen([exe], cwd=dest, env=env, close_fds=True, creationflags=flags | 0x01000000)
        except OSError:
            subprocess.Popen([exe], cwd=dest, env=env, close_fds=True, creationflags=flags)
        self.state["stage"] = "applying"
        threading.Timer(0.3, self.on_ready_to_quit).start()
        return True


    def _apply_after_exit(self, exe, dest, relaunch=True):
        script = os.path.join(tempfile.gettempdir(), "koi_apply_update.cmd")
        with open(script, "w", encoding="gbk", errors="ignore") as f:
            f.write(f"""@echo off
set PID={os.getpid()}
:wait
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul && (ping -n 2 127.0.0.1 >nul & goto wait)
robocopy "{self.stage_dir}" "{dest}" /E /R:20 /W:1 /NFL /NDL /NJH /NJS /NP >nul
set PYINSTALLER_RESET_ENVIRONMENT=1
set KOI_AFTER_UPDATE=1
{'start "" "' + exe + '"' if relaunch else 'rem 退出时安装，不重启'}
rmdir /s /q "{self.stage_root}"
del "%~f0"
""")
        env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
        # 不能用 DETACHED_PROCESS：没有控制台的 cmd 跑到「tasklist | find」这种管道会永远卡住（10/09 在笔记本实测）
        flags = 0x00000200 | 0x08000000   # NEW_PROCESS_GROUP | NO_WINDOW（隐藏的控制台）
        try:
            subprocess.Popen(["cmd.exe", "/c", script], env=env, close_fds=True, creationflags=flags | 0x01000000)
        except OSError:
            subprocess.Popen(["cmd.exe", "/c", script], env=env, close_fds=True, creationflags=flags)
        self.state["stage"] = "applying"
        if relaunch:
            threading.Timer(0.3, self.on_ready_to_quit).start()
        return True

    def view(self):
        return {"state": self.state, "last_check": self.last_check, "current": C.VERSION,
                "frozen": bool(getattr(sys, "frozen", False))}


def cleanup_old_files():
    """新版本启动后删掉上次更新留下的 .koiold。"""
    if not getattr(sys, "frozen", False):
        return
    dest = os.path.dirname(sys.executable)
    for root, _dirs, files in os.walk(dest):
        for f in files:
            if f.endswith(".koiold"):
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass
