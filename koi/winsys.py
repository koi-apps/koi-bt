"""和 Windows 系统打交道的部分：剪贴板、防睡眠、关机、文件关联。非 Windows 下全部安静地什么都不做。"""
import os
import subprocess
import sys

IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

_last_seq = None


def clipboard_changed_text():
    """剪贴板变了才返回新文字，没变返回 None。"""
    global _last_seq
    if not IS_WIN:
        return None
    seq = _user32.GetClipboardSequenceNumber()
    if seq == _last_seq:
        return None
    first = _last_seq is None
    _last_seq = seq
    if first:
        return None  # 启动时剪贴板里原有的内容不算
    CF_UNICODETEXT = 13
    if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _user32.OpenClipboard(None):
        return None
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def keep_awake(on):
    """有任务在下载时阻止系统自动睡眠（显示器照常可以关）。"""
    if not IS_WIN:
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    _kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def power_action(kind):
    if not IS_WIN:
        print("（非 Windows）本该执行：", kind)
        return
    if kind == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "60", "/c", "KOI BT：全部下载完成，60 秒后关机。取消请运行 shutdown /a"])
    elif kind == "sleep":
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])


def cancel_shutdown():
    if IS_WIN:
        subprocess.Popen(["shutdown", "/a"])


def exe_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}" "%1"'


def register_associations():
    """把磁力链和 .torrent 关联到 KOI（只写当前用户，不需要管理员）。"""
    if not IS_WIN:
        return False, "只支持 Windows"
    from . import config as C
    if C.IS_STORE:
        return True, "商店版安装时已经自动关联了磁力链和 .torrent 文件。如果没生效，在 Windows「设置 → 应用 → 默认应用」里选 KOI BT"
    import winreg

    cmd = exe_command()
    icon = sys.executable if getattr(sys, "frozen", False) else ""

    def setkey(path, values):
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, path)
        for name, val in values.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.CloseKey(k)

    base = r"Software\Classes"
    setkey(rf"{base}\magnet", {"": "URL:Magnet Link", "URL Protocol": ""})
    setkey(rf"{base}\magnet\shell\open\command", {"": cmd})
    if icon:
        setkey(rf"{base}\magnet\DefaultIcon", {"": f'"{icon}",0'})
    setkey(rf"{base}\KOI.Torrent", {"": "BT 种子文件"})
    setkey(rf"{base}\KOI.Torrent\shell\open\command", {"": cmd})
    if icon:
        setkey(rf"{base}\KOI.Torrent\DefaultIcon", {"": f'"{icon}",0'})
    setkey(rf"{base}\.torrent", {"": "KOI.Torrent", "Content Type": "application/x-bittorrent"})
    # thunder:// 之类也交给我们
    for proto in ("thunder", "qqdl", "flashget"):
        setkey(rf"{base}\{proto}", {"": f"URL:{proto}", "URL Protocol": ""})
        setkey(rf"{base}\{proto}\shell\open\command", {"": cmd})
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass
    return True, "已关联：磁力链、.torrent、thunder://、qqdl://、flashget://"


def find_player():
    """找本机的 PotPlayer / VLC / MPC，用来边下边播。"""
    if not IS_WIN:
        return None
    cands = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        cands += [
            os.path.join(base, "DAUM", "PotPlayer", "PotPlayerMini64.exe"),
            os.path.join(base, "PotPlayer", "PotPlayerMini64.exe"),
            os.path.join(base, "DAUM", "PotPlayer", "PotPlayerMini.exe"),
            os.path.join(base, "VideoLAN", "VLC", "vlc.exe"),
            os.path.join(base, "MPC-HC", "mpc-hc64.exe"),
            os.path.join(base, "MPC-BE x64", "mpc-be64.exe"),
        ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def open_with_player(url):
    p = find_player()
    if p:
        subprocess.Popen([p, url])
        return os.path.basename(p)
    return None


def open_folder(path, select=None):
    if IS_WIN:
        if select and os.path.exists(select):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(select)])
        else:
            os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def free_space(path):
    import shutil
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def system_lang():
    """Windows 界面语言 → zh-CN / zh-TW / en。"""
    try:
        if IS_WIN:
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if lid in (0x0404, 0x0C04, 0x1404):
                return "zh-TW"
            if lid & 0x3FF == 0x04:
                return "zh-CN"
            return "en"
        import locale
        loc = (locale.getlocale()[0] or "").lower()
        return "zh-TW" if loc.startswith(("zh_tw", "zh_hk")) else "zh-CN" if loc.startswith("zh") else "en"
    except Exception:
        return "zh-CN"


_TR = {"lang": None, "d": {}}


def T(text, lang):
    """后端少量文字（托盘菜单、窗口标题、系统通知）的翻译，词典和界面共用 static/i18n。"""
    if lang == "zh-CN" or not text:
        return text
    if _TR["lang"] != lang:
        import json
        from . import config as C
        try:
            j = json.loads((C.STATIC_DIR / "i18n" / f"{lang}.json").read_text("utf-8"))
            _TR["d"] = j["phrases"] if lang == "zh-TW" else j
            _TR["chars"] = j.get("chars", {}) if lang == "zh-TW" else {}
        except Exception:
            _TR["d"], _TR["chars"] = {}, {}
        _TR["lang"] = lang
    if text in _TR["d"]:
        return _TR["d"][text]
    if lang == "zh-TW":
        return "".join(_TR["chars"].get(c, c) for c in text)
    return text
