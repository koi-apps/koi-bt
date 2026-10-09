"""把 PyInstaller 打好的 dist/KOI BT 整理成 MSIX 的目录结构（清单 + 图标），之后用 Windows SDK 的 makeappx 打包。

python tools/build_msix_layout.py <identity> <publisher> <publisher_display_name>
三个值在微软商店后台：应用 → 产品管理 → 产品标识（Product identity）。
"""
import re, shutil, sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
identity, publisher, pub_name = sys.argv[1], sys.argv[2], sys.argv[3]
v = re.search(r'^VERSION = "(.+)"', (ROOT / "koi/config.py").read_text("utf-8"), re.M).group(1)
out = ROOT / "dist" / "msix"
shutil.rmtree(out, ignore_errors=True)
shutil.copytree(ROOT / "dist" / "KOI BT", out)
assets = out / "Assets"
assets.mkdir()
logo = Image.open(ROOT / "static/logo.png").convert("RGBA")
def square(n, name, pad=0.12):
    canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    inner = int(n * (1 - 2 * pad))
    canvas.paste(logo.resize((inner, inner), Image.LANCZOS), ((n - inner) // 2, (n - inner) // 2))
    canvas.save(assets / name)
square(50, "StoreLogo.png", 0.04)
square(44, "Square44x44Logo.png", 0.04)
square(150, "Square150x150Logo.png")
wide = Image.new("RGBA", (310, 150), (0, 0, 0, 0))
wide.paste(logo.resize((110, 110), Image.LANCZOS), (100, 20))
wide.save(assets / "Wide310x150Logo.png")
m = (ROOT / "installer/msix/AppxManifest.xml").read_text("utf-8")
m = m.replace("__IDENTITY__", identity).replace("__PUBLISHER__", publisher).replace("__PUBLISHER_NAME__", pub_name).replace("__VERSION__", v + ".0")
(out / "AppxManifest.xml").write_text(m, "utf-8")
print("msix layout ready", out, v)
