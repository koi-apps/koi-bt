"""CI 发版用：给打好的更新包生成签名清单 latest.json。

python tools/release_ci.py <版本号> <zip 路径> <owner/repo> "<更新说明>"
私钥从环境变量 KOI_UPDATE_SIGNING_KEY 读（PEM 文本，放在 GitHub 仓库的 Secrets 里）。
没有私钥（比如别人 fork 的仓库）就跳过，不生成 latest.json —— 只有官方发布能触发自动更新。
"""
import base64, hashlib, json, os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

ver, zpath, repo, notes = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""
key_pem = os.environ.get("KOI_UPDATE_SIGNING_KEY", "").strip()
if not key_pem:
    print("没有 KOI_UPDATE_SIGNING_KEY，跳过签名清单")
    sys.exit(0)

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

data = zpath.read_bytes()
manifest = {
    "v": ver,
    "file": zpath.name,
    "url": f"https://github.com/{repo}/releases/download/v{ver}/{zpath.name}",
    "size": len(data),
    "sha256": hashlib.sha256(data).hexdigest(),
    "notes": notes,
    "mandatory": False,
    "time": int(time.time()),
}
raw = json.dumps(manifest, ensure_ascii=False).encode()
key = serialization.load_pem_private_key(key_pem.encode(), None)
sig = key.sign(raw, ec.ECDSA(hashes.SHA256()))
Path("latest.json").write_text(json.dumps({"manifest": base64.b64encode(raw).decode(), "sig": base64.b64encode(sig).decode()}), encoding="utf-8")
print("latest.json 已生成", manifest)
