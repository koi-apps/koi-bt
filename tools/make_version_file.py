"""生成 PyInstaller 的 Windows 版本信息文件（exe 属性里的产品名、版本号；代码签名要求这些元数据一致）。"""
import re, sys
from pathlib import Path
root = Path(__file__).resolve().parent.parent
v = re.search(r'^VERSION = "(.+)"', (root / "koi/config.py").read_text("utf-8"), re.M).group(1)
t = tuple(int(x) for x in (v.split(".") + ["0"] * 4)[:4])
(root / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'KOI BT contributors'),
    StringStruct('FileDescription', 'KOI BT'),
    StringStruct('FileVersion', '{v}'),
    StringStruct('InternalName', 'KOI BT'),
    StringStruct('LegalCopyright', 'GPL-3.0'),
    StringStruct('OriginalFilename', 'KOI BT.exe'),
    StringStruct('ProductName', 'KOI BT'),
    StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])]
)
""", "utf-8")
print("version_info.txt", v)
