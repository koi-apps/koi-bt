# 隐私说明 / Privacy

KOI BT 不收集、不上传任何个人数据，没有统计、遥测或广告。
KOI BT collects no personal data and has no analytics, telemetry or ads.

程序只会在以下情况联网（都是功能本身需要的）：
It only connects to the network for these purposes:

1. **下载本身 / Downloading**：BitTorrent 网络（tracker、DHT、其他用户）和你添加的下载地址。Your BitTorrent swarms, trackers, DHT, and the URLs you add.
2. **公开 tracker 列表 / Public tracker list**：从 [ngosang/trackerslist](https://github.com/ngosang/trackerslist) 获取，可在设置里关闭或更换地址。Fetched from ngosang/trackerslist; can be disabled in Settings.
3. **检查更新 / Update check**：从本仓库的 GitHub Releases 读取 `latest.json`，每 6 小时一次，可在设置里关闭。Reads latest.json from this repository's GitHub Releases every 6 hours; can be disabled.
4. **RSS 订阅 / RSS feeds**：只访问你自己添加的订阅地址。Only the feeds you add.

所有设置和下载记录只保存在你自己电脑上（`%APPDATA%\KOI BT`）。远程控制默认关闭，开启后需要密码。
All settings and history stay on your computer (`%APPDATA%\KOI BT`). Remote access is off by default and password-protected when enabled.

卸载：在 Windows「设置 → 应用」里卸载 KOI BT；如需同时删除设置，删除 `%APPDATA%\KOI BT` 文件夹。
Uninstall from Windows Settings → Apps; delete `%APPDATA%\KOI BT` to remove settings as well.
