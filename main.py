"""KOI BT启动入口：单实例、窗口、托盘。

python main.py             打开窗口（有 pywebview 用独立窗口，否则用浏览器）
python main.py --headless  只跑后台，浏览器访问 http://127.0.0.1:18790
python main.py <磁力链/种子文件>  已经开着就把它交给正在运行的那个
"""
import asyncio
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from koi import config as C


LOG_FILE = C.DATA_DIR / "koi.log"


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + str(msg) + "\n")
    except OSError:
        pass


def setup_logging():
    """打包成无控制台程序后 print 和报错都看不见，全部写进 koi.log；崩溃时 faulthandler 记下各线程现场。"""
    import faulthandler
    import traceback
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
            LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
    except OSError:
        pass
    fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = fh
    faulthandler.enable(file=fh, all_threads=True)
    sys.excepthook = lambda *a: log("未捕获异常:\n" + "".join(traceback.format_exception(*a)))
    threading.excepthook = lambda a: log(f"线程 {a.thread.name} 异常:\n" + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    log(f"===== 启动 v{C.VERSION} =====")


def title_now():
    try:
        from koi.winsys import T
        from koi.server import resolve_lang
        return T(C.APP_NAME, resolve_lang(C.load_settings()["language"]))
    except Exception:
        return C.APP_NAME


def start_hang_watchdog():
    """黑匣子：窗口一旦"未响应"超过 3 秒，把所有线程卡在哪一行写进 hang.log。"""
    if sys.platform != "win32":
        return
    import ctypes
    import faulthandler
    user32 = ctypes.windll.user32
    hang_file = C.DATA_DIR / "hang.log"

    def run():
        hung_since = None
        dumped = False
        while True:
            time.sleep(1)
            hwnd = user32.FindWindowW(None, title_now())
            if not hwnd:
                continue
            if user32.IsHungAppWindow(hwnd):
                hung_since = hung_since or time.time()
                if not dumped and time.time() - hung_since > 3:
                    with open(hang_file, "a", encoding="utf-8") as f:
                        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 窗口未响应 =====\n")
                        f.flush()
                        faulthandler.dump_traceback(file=f, all_threads=True)
                    dumped = True
            else:
                hung_since, dumped = None, False

    threading.Thread(target=run, name="hang-watchdog", daemon=True).start()


def docker_setup(core):
    """Docker / NAS：默认打开远程访问（不然连不进网页），下载目录指向 /downloads，密码从环境变量来。"""
    import secrets
    s, changed = core.settings, {}
    dl = os.environ.get("KOI_DOWNLOAD_DIR", "/downloads")
    if not C.SETTINGS_FILE.exists() or s.get("save_path", "").startswith(str(os.path.expanduser("~"))):
        changed["save_path"] = dl
    pw = os.environ.get("KOI_PASSWORD")
    pw_file = os.environ.get("KOI_PASSWORD_FILE")
    if not pw and pw_file and os.path.exists(pw_file):
        with open(pw_file, encoding="utf-8") as f:
            pw = f.read().strip()
        os.remove(pw_file)
    user = os.environ.get("KOI_USERNAME")
    if pw:
        changed["remote_password"] = pw
    elif not s.get("remote_password"):
        changed["remote_password"] = secrets.token_urlsafe(9)
        print("=" * 60)
        print(f"KOI BT 初始密码 / initial password:  {changed['remote_password']}")
        print("可以在设置里修改，或用环境变量 KOI_PASSWORD 指定 / change it in Settings or set KOI_PASSWORD")
        print("=" * 60, flush=True)
    if user:
        changed["remote_username"] = user
    changed["remote_enabled"] = True
    for k in ("clipboard_watch", "keep_awake", "close_to_tray"):
        changed[k] = False
    core.bt.apply_settings(changed)


def port_in_use(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def forward_to_running(args):
    """已经有一个 KOI 在跑：把参数里的链接/种子交给它，再把它的窗口叫出来。"""
    import base64
    links = [a for a in args if not a.startswith("--") and not os.path.isfile(a)]
    files = [a for a in args if os.path.isfile(a)]
    base = f"http://127.0.0.1:{C.UI_PORT}"
    if links:
        req = urllib.request.Request(base + "/api/add", data=json.dumps({"links": "\n".join(links)}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    for f in files:
        boundary = "----koi" + str(time.time()).replace(".", "")
        with open(f, "rb") as fh:
            blob = fh.read()
        name = os.path.basename(f).encode()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"".encode() + name +
                b"\"\r\nContent-Type: application/x-bittorrent\r\n\r\n" + blob + f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(base + "/api/add", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        urllib.request.urlopen(req, timeout=5).read()
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/api/show", data=b"{}", headers={"Content-Type": "application/json"}), timeout=3)
    except Exception:
        webbrowser.open(base)


def main():
    args = sys.argv[1:]
    headless = "--headless" in args
    if os.environ.pop("KOI_AFTER_UPDATE", None):
        for _ in range(100):
            if not port_in_use(C.UI_PORT):
                break
            time.sleep(0.1)
    if port_in_use(C.UI_PORT):
        forward_to_running(args)
        return

    if not headless:
        setup_logging()
    from aiohttp import web
    from koi.server import Core, build_app
    from koi.updater import cleanup_old_files
    threading.Thread(target=cleanup_old_files, daemon=True).start()

    core = Core()
    if os.environ.get("KOI_DOCKER"):
        docker_setup(core)
    for a in args:
        if a.startswith("--"):
            continue
        try:
            if os.path.isfile(a):
                with open(a, "rb") as fh:
                    core.bt.add_torrent_bytes(fh.read())
            else:
                core.add_link(a)
        except Exception as e:
            print("添加失败", a, e)

    app = build_app(core)

    window = {"w": None}

    async def show(_req):
        if core.show_window:
            core.show_window()
        return web.json_response({"ok": True})
    app.router.add_post("/api/show", show)

    # 一直监听所有网卡：远程开关在中间件里判断，开关即时生效不用重启（没开时外部访问一律 403）
    host = "0.0.0.0"
    url = f"http://127.0.0.1:{C.UI_PORT}/"

    if headless:
        web.run_app(app, host=host, port=C.UI_PORT, print=None)
        return

    try:
        import webview
    except ImportError:
        webview = None

    if webview is None:
        core.show_window = lambda: webbrowser.open(url)
        threading.Timer(1.0, webbrowser.open, [url]).start()
        web.run_app(app, host=host, port=C.UI_PORT, print=None)
        return

    # 后台线程跑服务，主线程给窗口
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, host, C.UI_PORT).start())
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    while not port_in_use(C.UI_PORT):
        time.sleep(0.1)

    quitting = {"v": False}
    from koi.server import resolve_lang
    from koi.winsys import T
    lang = resolve_lang(core.settings["language"])
    core.lang = lang
    w = webview.create_window(T(C.APP_NAME, lang), url, width=1240, height=800, min_size=(860, 540))
    window["w"] = w

    def _show():
        try:
            w.show()
            w.restore()
        except Exception as e:
            log(f"show_window 失败: {e!r}")

    def show_window():
        # 永远在独立线程里叫窗口：不能卡住后台事件循环，也不能在界面线程里同步等自己
        threading.Thread(target=_show, daemon=True).start()
    core.show_window = show_window

    tray = start_tray(core, show_window, lambda: do_quit())

    def on_closing():
        log("on_closing")
        if core.settings["close_to_tray"] and tray and not quitting["v"]:
            # 这个回调跑在界面线程里，不能在这里同步 hide（会和 pywebview 的 Invoke 互等），放到别的线程稍后执行
            threading.Timer(0.05, lambda: w.hide()).start()
            return False   # 拦住关闭，缩到托盘
        quitting["v"] = True
        return True

    def do_quit():
        quitting["v"] = True
        try:
            w.destroy()
        except Exception:
            pass

    core.quit_app = do_quit

    def pick_path(kind, start):
        try:
            t = webview.FOLDER_DIALOG if kind == "folder" else webview.OPEN_DIALOG
            r = w.create_file_dialog(t, directory=start if start and os.path.isdir(start) else "")
            if r:
                return r[0] if isinstance(r, (list, tuple)) else r
        except Exception as e:
            log(f"选择文件夹失败: {e!r}")
        return None
    core.pick_path = pick_path
    w.events.closing += on_closing
    start_hang_watchdog()
    log("webview start")
    webview.start(private_mode=False, storage_path=str(C.DATA_DIR / "webview"))

    # 用户选了「稍后更新」：退出时把新版本装好，下次打开就是新版
    try:
        core.updater.install_on_exit()
    except Exception as e:
        log(f"退出时安装更新失败: {e!r}")
    if tray:
        try:
            tray.stop()
        except Exception:
            pass
    fut = asyncio.run_coroutine_threadsafe(runner.cleanup(), loop)
    try:
        fut.result(timeout=15)
    except Exception:
        pass


def start_tray(core, show_window, quit_fn):
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return None
    icon_img = Image.open(C.STATIC_DIR / "logo.png")

    def toggle_alt(icon, item):
        core.bt.apply_settings({"alt_enabled": not core.settings["alt_enabled"]})

    from koi.winsys import T
    L = lambda t: T(t, getattr(core, "lang", "zh-CN"))
    menu = pystray.Menu(
        pystray.MenuItem(L("打开 KOI BT"), lambda i, it: show_window(), default=True),
        pystray.MenuItem(L("乌龟模式（备用限速）"), toggle_alt, checked=lambda it: core.settings["alt_enabled"]),
        pystray.MenuItem(L("全部暂停"), lambda i, it: [core.bt.action(t["id"], "pause") if t["type"] == "bt" else core.http.action(t["id"], "pause") for t in core.snapshot()]),
        pystray.MenuItem(L("全部开始"), lambda i, it: [core.bt.action(t["id"], "resume") if t["type"] == "bt" else core.http.action(t["id"], "resume") for t in core.snapshot()]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(L("退出"), lambda i, it: quit_fn()),
    )
    icon = pystray.Icon("koi", icon_img, L(C.APP_NAME), menu)

    def notify(title, body):
        try:
            icon.notify(L(body) or L(title), L(title))
        except Exception:
            pass
    core.os_notify = notify
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


if __name__ == "__main__":
    main()
