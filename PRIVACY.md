# 隐私说明 / Privacy

KOI BT 不收集、不上传任何个人数据，没有统计、遥测或广告。
KOI BT collects no personal data and has no analytics, telemetry or ads.

程序只会在以下情况联网（都是功能本身需要的）：
It only connects to the network for these purposes:

1. **下载本身 / Downloading**：BitTorrent 网络（tracker、DHT、其他用户）和你添加的下载地址。Your BitTorrent swarms, trackers, DHT, and the URLs you add.
2. **公开 tracker 列表 / Public tracker list**：从 [ngosang/trackerslist](https://github.com/ngosang/trackerslist) 获取，可在设置里关闭或更换地址。Fetched from ngosang/trackerslist; can be disabled in Settings.
3. **检查更新 / Update check**：从本仓库的 GitHub Releases 读取 `latest.json`，每次启动和之后每小时一次，可在设置里关闭。Reads latest.json from this repository's GitHub Releases on startup and then hourly; can be disabled.
4. **RSS 订阅 / RSS feeds**：只访问你自己添加的订阅地址。Only the feeds you add.

**BitTorrent 协议本身的特性**：下载和做种时，你的 IP 地址和端口会被同一个种子里的其他用户、tracker 服务器和 DHT 网络看到——所有 BT 软件都是这样。如需隐藏 IP，可以在设置里把 KOI BT 绑定到 VPN 网卡。
**How BitTorrent works**: while downloading or seeding, your IP address and port are visible to other peers in the same torrent, to trackers and to the DHT network — this is true for every BitTorrent client. You can bind KOI BT to a VPN interface in Settings to hide it.

所有设置和下载记录只保存在你自己电脑上（`%APPDATA%\KOI BT`）。远程控制默认关闭，开启后需要密码。
All settings and history stay on your computer (`%APPDATA%\KOI BT`). Remote access is off by default and password-protected when enabled.

卸载：在 Windows「设置 → 应用」里卸载 KOI BT；如需同时删除设置，删除 `%APPDATA%\KOI BT` 文件夹。
Uninstall from Windows Settings → Apps; delete `%APPDATA%\KOI BT` to remove settings as well.
