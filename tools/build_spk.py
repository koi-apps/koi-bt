"""打群晖 DSM 7 套件（.spk）：自带 Python 和全部依赖，装上就能用，不依赖套件中心里的 Python。

python tools/build_spk.py x86_64     → dist/KOI-BT-<版本>-x86_64.spk
python tools/build_spk.py armv8      → dist/KOI-BT-<版本>-armv8.spk

装好后：套件中心「打开」或浏览器访问 http://NAS:18790；下载文件存在共享文件夹 KOI-BT 里。
"""
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PBS = "https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.12.15%2B20261003-{triple}-install_only_stripped.tar.gz"
ARCHES = {
    # DSM 平台名：和 SynoCommunity 的 x64 / armv8 套件保持一致
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu", "pip": "manylinux2014_x86_64",
        "dsm": "apollolake avoton braswell broadwell broadwellnk broadwellnkv2 broadwellntbap bromolow denverton "
               "epyc7002 geminilake geminilakenk grantley icelaked kvmx64 purley r1000 r1000nk v1000 v1000nk x86_64",
    },
    "armv8": {
        "triple": "aarch64-unknown-linux-gnu", "pip": "manylinux2014_aarch64",
        "dsm": "armada37xx rtd1296 rtd1619b aarch64",
    },
}
PKG = "koi-bt"
SHARE = "KOI-BT"
DEPS = ["libtorrent==2.1.1", "aiohttp>=3.9", "segno>=1.6", "cryptography>=42"]


def version():
    return re.search(r'^VERSION = "(.+)"', (ROOT / "koi/config.py").read_text("utf-8"), re.M).group(1)


def add_bytes(tar, name, data, mode=0o644):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mode = mode
    ti.mtime = int(time.time())
    tar.addfile(ti, io.BytesIO(data))


def png(size):
    buf = io.BytesIO()
    Image.open(ROOT / "static/logo.png").convert("RGBA").resize((size, size), Image.LANCZOS).save(buf, "PNG")
    return buf.getvalue()


START_STOP = r'''#!/bin/sh
# KOI BT 启动 / 停止 / 状态（DSM 7）
PKG_DIR="/var/packages/koi-bt/target"
VAR_DIR="${SYNOPKG_PKGVAR:-/var/packages/koi-bt/var}"
PID_FILE="${VAR_DIR}/koi-bt.pid"
LOG_FILE="${VAR_DIR}/koi-bt.log"

share_path() {
    # DSM 7 会把套件申请的共享文件夹链接到 /var/packages/<套件>/shares/<名字>
    if [ -e "/var/packages/koi-bt/shares/KOI-BT" ]; then
        readlink -f "/var/packages/koi-bt/shares/KOI-BT"
    else
        echo "${VAR_DIR}/downloads"
    fi
}

is_running() {
    [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null
}

case "$1" in
    start)
        is_running && exit 0
        DL="$(share_path)"
        mkdir -p "${DL}"
        cd "${PKG_DIR}/app" || exit 1
        env KOI_HOME="${VAR_DIR}" KOI_DOCKER=1 KOI_DOWNLOAD_DIR="${DL}" \
            KOI_PASSWORD_FILE="${VAR_DIR}/initial_password" \
            KOI_UPDATE_HINT="请在群晖套件中心更新 KOI BT" \
            PYTHONPATH="${PKG_DIR}/lib" PYTHONUNBUFFERED=1 \
            "${PKG_DIR}/python/bin/python3" main.py --headless >> "${LOG_FILE}" 2>&1 &
        echo $! > "${PID_FILE}"
        sleep 2
        is_running || exit 1
        exit 0
        ;;
    stop)
        if is_running; then
            kill "$(cat "${PID_FILE}")"
            for i in 1 2 3 4 5 6 7 8 9 10; do is_running || break; sleep 1; done
            is_running && kill -9 "$(cat "${PID_FILE}")"
        fi
        rm -f "${PID_FILE}"
        exit 0
        ;;
    status)
        is_running && exit 0 || exit 3
        ;;
    log)
        echo "${LOG_FILE}"
        exit 0
        ;;
    *)
        exit 1
        ;;
esac
'''

POSTINST = r'''#!/bin/sh
# 安装向导里填的密码：只交给程序用一次（首次启动时写进设置后删除）
VAR_DIR="${SYNOPKG_PKGVAR:-/var/packages/koi-bt/var}"
mkdir -p "${VAR_DIR}"
if [ -n "${wizard_password}" ]; then
    printf '%s' "${wizard_password}" > "${VAR_DIR}/initial_password"
    chmod 600 "${VAR_DIR}/initial_password"
fi
exit 0
'''

PREUNINST = r'''#!/bin/sh
# 卸载向导里勾了「同时删除设置」才清掉设置和下载记录；不勾的话保留，重装后接着用
# 下载好的文件在共享文件夹 KOI-BT 里，这里不碰
if [ "${wizard_delete_data}" = "true" ]; then
    VAR_DIR="$(readlink -f "${SYNOPKG_PKGVAR:-/var/packages/koi-bt/var}")"
    case "${VAR_DIR}" in
        */koi-bt|*/koi-bt/var) find "${VAR_DIR}" -mindepth 1 -delete ;;
    esac
fi
exit 0
'''

NOOP = "#!/bin/sh\nexit 0\n"


def wizard(lang):
    zh = lang == "chs"
    return json.dumps([{
        "step_title": "KOI BT" if not zh else "KOI BT 设置",
        "items": [
            {"type": "password", "desc": ("设置网页登录密码。浏览器打开 http://群晖IP:18790 时要输入它（用户名 admin）。"
                                          if zh else "Set the web login password. You will enter it at http://NAS-IP:18790 (username: admin)."),
             "subitems": [{"key": "wizard_password", "desc": "密码" if zh else "Password",
                           "validator": {"allowBlank": False, "minLength": 6}}]},
            {"desc": ("下载的文件保存在共享文件夹 <b>KOI-BT</b> 里，在 File Station 里就能看到。<br>"
                      "想让下载更快：在路由器上把 51413 端口（TCP 和 UDP）转发到这台群晖。"
                      if zh else "Downloads are saved to the shared folder <b>KOI-BT</b> (visible in File Station).<br>"
                                 "For faster downloads, forward port 51413 (TCP and UDP) on your router to this NAS.")},
        ]}], ensure_ascii=False).encode()


def uninstall_wizard(lang):
    zh = lang == "chs"
    return json.dumps([{
        "step_title": "卸载 KOI BT" if zh else "Uninstall KOI BT",
        "items": [{"type": "multiselect",
                   "desc": ("下载好的文件在共享文件夹 KOI-BT 里，卸载不会删除它们。"
                            if zh else "Your downloaded files in the KOI-BT shared folder are not deleted."),
                   "subitems": [{"key": "wizard_delete_data", "defaultValue": False,
                                 "desc": ("同时删除设置和下载任务列表（不勾的话保留，重新安装后接着用）"
                                          if zh else "Also delete settings and the task list (if unchecked, they are kept for a reinstall)")}]}]}],
        ensure_ascii=False).encode()


def build(arch):
    a = ARCHES[arch]
    v = version()
    work = Path(tempfile.mkdtemp(prefix="spk-"))
    target = work / "target"
    target.mkdir()
    # 1. 便携版 Python
    print("下载 Python …")
    with urllib.request.urlopen(PBS.format(triple=a["triple"]), timeout=120) as r:
        with tarfile.open(fileobj=io.BytesIO(r.read()), mode="r:gz") as t:
            t.extractall(target)          # 解出来是 target/python
    # 用不上的标准库部分删掉，减小套件体积
    pyroot = target / "python"
    for rel in ("include", "share", "lib/python3.12/test", "lib/python3.12/idlelib", "lib/python3.12/tkinter",
                "lib/python3.12/turtledemo", "lib/python3.12/ensurepip", "lib/python3.12/lib2to3",
                "lib/python3.12/unittest/test", "lib/python3.12/config-3.12-" + a["triple"].replace("-unknown", "")):
        shutil.rmtree(pyroot / rel, ignore_errors=True)
    for p in list(pyroot.rglob("*.a")) + list(pyroot.rglob("__pycache__")):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
    for p in list((pyroot / "lib").glob("libtcl*")) + list((pyroot / "lib").glob("tcl9*")) + list((pyroot / "lib").glob("tk9*")):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
    for p in (pyroot / "lib").glob("libtk*"):
        p.unlink()
    shutil.rmtree(pyroot / "lib" / "tcl8.6", ignore_errors=True)
    shutil.rmtree(pyroot / "lib" / "tk8.6", ignore_errors=True)
    # 2. 依赖（按目标架构下载现成的 wheel）
    print("安装依赖 …")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(target / "lib"),
                    "--platform", a["pip"], "--python-version", "3.12", "--only-binary=:all:",
                    "--implementation", "cp", *DEPS], check=True)
    # 3. 程序本体
    app = target / "app"
    app.mkdir()
    for name in ("koi", "static", "browser_extension"):
        shutil.copytree(ROOT / name, app / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("main.py", "LICENSE"):
        shutil.copy2(ROOT / name, app / name)
    # 4. DSM 桌面图标（套件中心「打开」按钮 / 主菜单）
    ui = target / "ui"
    (ui / "images").mkdir(parents=True)
    for s in (16, 24, 32, 48, 64, 72, 256):
        (ui / "images" / f"koi-bt-{s}.png").write_bytes(png(s))
    (ui / "config").write_text(json.dumps({".url": {"com.koiapps.koibt": {
        "title": "KOI BT", "desc": "BitTorrent & HTTP downloader", "icon": "images/koi-bt-{0}.png",
        "type": "url", "protocol": "http", "port": "18790", "url": "/", "allUsers": True,
        "grantPrivilege": "all", "advanceGrantPrivilege": True}}}, indent=2))
    (target / "koi-bt.sc").write_text(
        '[koi-bt]\ntitle="KOI BT (Web)"\ndesc="KOI BT"\nport_forward="yes"\ndst.ports="18790/tcp"\n\n'
        '[koi-bt-bt]\ntitle="KOI BT (BitTorrent)"\ndesc="KOI BT"\nport_forward="yes"\ndst.ports="51413/tcp,51413/udp"\n')
    # 5. package.tgz
    pkg_tgz = io.BytesIO()
    with tarfile.open(fileobj=pkg_tgz, mode="w:gz") as t:
        for p in sorted(target.rglob("*")):
            t.add(p, arcname=str(p.relative_to(target)), recursive=False)
    pkg_data = pkg_tgz.getvalue()
    # 6. INFO
    info = {
        "package": PKG, "version": f"{v}-1", "os_min_ver": "7.0-40000", "arch": a["dsm"],
        "displayname": "KOI BT", "maintainer": "koi-apps", "maintainer_url": "https://github.com/koi-apps/koi-bt",
        "distributor": "koi-apps", "distributor_url": "https://github.com/koi-apps/koi-bt",
        "support_url": "https://github.com/koi-apps/koi-bt/issues",
        "description": "Free, open-source BitTorrent downloader.",
        "description_chs": "开源免费的 BT 下载器。",
        "description_cht": "開源免費的 BT 下載器。",
        "thirdparty": "yes", "startable": "yes", "dsmuidir": "ui", "dsmappname": "com.koiapps.koibt",
        "adminport": "18790", "adminprotocol": "http", "adminurl": "/",
        "ctl_stop": "yes", "silent_install": "no", "silent_upgrade": "yes",
        "extractsize": str(sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) // 1024),
    }
    info_txt = "".join(f'{k}="{val}"\n' for k, val in info.items()).encode()
    # 7. 拼 .spk（不压缩的 tar）
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    spk = out / f"KOI-BT-{v}-{arch}.spk"
    with tarfile.open(spk, "w") as t:
        add_bytes(t, "INFO", info_txt)
        add_bytes(t, "package.tgz", pkg_data)
        for name, body in (("start-stop-status", START_STOP), ("postinst", POSTINST), ("preinst", NOOP),
                           ("preuninst", PREUNINST), ("postuninst", NOOP), ("preupgrade", NOOP), ("postupgrade", NOOP)):
            add_bytes(t, f"scripts/{name}", body.encode(), 0o755)
        add_bytes(t, "conf/privilege", json.dumps({"defaults": {"run-as": "package"}}).encode())
        add_bytes(t, "conf/resource", json.dumps({
            "port-config": {"protocol-file": "koi-bt.sc"},
            "data-share": {"shares": [{"name": SHARE, "permission": {"rw": [PKG]}}]},
        }).encode())
        add_bytes(t, "WIZARD_UIFILES/install_uifile", wizard("enu"))
        add_bytes(t, "WIZARD_UIFILES/install_uifile_chs", wizard("chs"))
        add_bytes(t, "WIZARD_UIFILES/install_uifile_enu", wizard("enu"))
        add_bytes(t, "WIZARD_UIFILES/uninstall_uifile", uninstall_wizard("enu"))
        add_bytes(t, "WIZARD_UIFILES/uninstall_uifile_chs", uninstall_wizard("chs"))
        add_bytes(t, "WIZARD_UIFILES/uninstall_uifile_enu", uninstall_wizard("enu"))
        add_bytes(t, "PACKAGE_ICON.PNG", png(64))
        add_bytes(t, "PACKAGE_ICON_256.PNG", png(256))
    shutil.rmtree(work, ignore_errors=True)
    print(spk, f"{spk.stat().st_size / 1e6:.1f} MB")
    return spk


if __name__ == "__main__":
    for arch in (sys.argv[1:] or ["x86_64", "armv8"]):
        build(arch)
