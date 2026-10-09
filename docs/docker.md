# 在群晖 / NAS 上用 Docker 运行 KOI BT

KOI BT 的 Docker 版没有桌面窗口，用浏览器管理：`http://NAS的IP:18790`。Intel 和 ARM 机型都支持。

## 群晖 Container Manager（图形界面）

1. 打开「Container Manager」→「注册表」→ 右上角「设置」→「新增」：名称填 `ghcr`，网址填 `https://ghcr.io`，确定。
   然后选中 ghcr，点「使用」。
2. 搜索 `koi-apps/koi-bt`，下载 `latest` 标签。
3. 「映像」里选中它 →「运行」：
   - 端口：`18790 → 18790`（网页界面），`51413 → 51413` 的 TCP 和 UDP 各一条（BT 端口，让别人能连进你）
   - 存储空间：新建两个文件夹并映射：
     - `docker/koi-bt/config` → `/config`（设置和任务记录）
     - 你想存下载文件的文件夹，比如 `downloads` → `/downloads`
   - 环境变量：`KOI_PASSWORD` = 你想设置的登录密码（不填的话会自动生成一个，在容器日志里能看到）
4. 浏览器打开 `http://NAS的IP:18790`，输入密码就能用了。

## docker compose

```yaml
services:
  koi-bt:
    image: ghcr.io/koi-apps/koi-bt:latest
    container_name: koi-bt
    restart: unless-stopped
    ports:
      - "18790:18790"
      - "51413:51413"
      - "51413:51413/udp"
    environment:
      - KOI_PASSWORD=换成你的密码
      - TZ=Asia/Shanghai
    volumes:
      - ./config:/config
      - /volume1/downloads:/downloads
```

## 说明

- 用户名默认是 `admin`，可以用环境变量 `KOI_USERNAME` 改。qBittorrent 手机 App、Sonarr / Radarr 选 qBittorrent 类型，填同一个地址和账号密码就能连。
- 更新：拉取新镜像后重建容器（Container Manager 里「项目」或「容器」→「重置」），设置和任务都在 `/config`，不会丢。
- 路由器上把 51413 端口（TCP + UDP）转发到 NAS，下载会更快。
