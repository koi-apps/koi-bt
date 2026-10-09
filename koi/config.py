"""路径、版本、设置读写。"""
import json
import os
import sys
from pathlib import Path

APP_NAME = "KOI BT"
GITHUB_REPO = "koi-apps/koi-bt"     # 官方发布仓库：更新从这里的 Releases 拿
OFFICIAL_UPDATE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest/download"
VERSION = "1.4.0"
UI_PORT = 18790

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
STATIC_DIR = BASE_DIR / "static"
def _default_data_dir():
    r"""Windows：%APPDATA%\KOI BT；其他系统：~/.config/koi-bt。早期版本放在 ~/.koi_downloader，首次启动自动搬过来。"""
    if os.environ.get("KOI_HOME"):
        return Path(os.environ["KOI_HOME"])
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        new = Path(os.environ["APPDATA"]) / "KOI BT"
    else:
        new = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "koi-bt"
    legacy = Path.home() / ".koi_downloader"
    if legacy.is_dir() and not new.exists():
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(new)
        except OSError:
            return legacy
    return new


DATA_DIR = _default_data_dir()
RESUME_DIR = DATA_DIR / "resume"
HTTP_DIR = DATA_DIR / "http"
TORRENT_DIR = DATA_DIR / "torrents"
SETTINGS_FILE = DATA_DIR / "settings.json"
SESSION_FILE = DATA_DIR / "session.dat"
TRACKER_CACHE = DATA_DIR / "trackers.txt"
RSS_FILE = DATA_DIR / "rss.json"

for d in (DATA_DIR, RESUME_DIR, HTTP_DIR, TORRENT_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    # 基本
    "save_path": str(Path.home() / "Downloads"),
    "move_done_to": "",            # 下完后移动到（空 = 不移动）
    "select_files_on_add": True,   # 多文件种子添加时先让我选文件
    "clipboard_watch": True,       # 复制磁力链 / 种子链接自动弹出
    "watch_dir": "",               # 监视文件夹：丢进去的 .torrent 自动添加
    "keep_awake": True,            # 有任务在下载时阻止电脑睡眠
    "close_to_tray": True,
    "notify_done": True,
    "after_all_done": "none",      # none / sleep / shutdown
    # 连接
    "listen_port": 51413,
    "max_connections": 500,
    "max_active_downloads": 5,
    "auto_trackers": True,
    "tracker_url": "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
    "encryption": "prefer",        # prefer / require / disable
    "bind_interface": "",          # 绑定网卡（挂 VPN 时防泄露），空 = 全部
    # 速度
    "download_limit_kb": 0,
    "upload_limit_kb": 0,
    "alt_download_limit_kb": 1024,  # 备用限速（乌龟模式）
    "alt_upload_limit_kb": 256,
    "alt_enabled": False,
    "schedule_enabled": False,      # 按时段自动切换到备用限速
    "schedule_from": "08:00",
    "schedule_to": "23:00",
    "schedule_days": [0, 1, 2, 3, 4, 5, 6],  # 0 = 周一
    # 做种
    "seed_after_done": True,
    "ratio_limit": 0.0,             # 分享率到达后停止做种，0 = 不限
    "seed_time_limit_min": 10,      # 下完后做种多少分钟就停，0 = 一直做种（seed_after_done=False 表示下完就停）
    # 吸血屏蔽
    "anti_leech": True,
    "ban_list_extra": "",           # 自定义要屏蔽的客户端关键字，逗号分隔
    "ip_blocklist": "",             # 自定义 IP 黑名单，一行一个
    # 网页直链
    "http_threads": 16,
    # 远程控制（手机访问）
    "remote_enabled": False,
    "remote_password": "",
    "remote_username": "admin",     # 给 qBittorrent 类遥控 App 用
    "categories": {},               # 分类名 -> {save_path}
    "language": "auto",             # auto / zh-CN / zh-TW / en
    # 在线更新
    "update_url": "",               # 空 = 官方 GitHub Releases；可以填自建的更新地址，逗号分隔多个
    "auto_update_check": True,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    saved = {}
    try:
        saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
        s.update(saved)
    except (OSError, ValueError):
        pass
    # 1.1.2 起默认做种 10 分钟；老版本存下来的「0 = 不限」是旧默认值，不是用户自己选的
    if saved and saved.get("_v", 0) < 2:
        if saved.get("seed_time_limit_min", 0) == 0:
            s["seed_time_limit_min"] = 10
        s["_v"] = 2
        save_settings(s)
    s["_v"] = 2
    return s


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")


def coerce(key, value):
    if key not in DEFAULT_SETTINGS:
        return value
    """按默认值的类型转换前端传来的设置。"""
    t = type(DEFAULT_SETTINGS[key])
    if t is bool:
        return bool(value)
    if t is list:
        return list(value)
    if t is dict:
        return dict(value)
    return t(value)
