# KOI BT 无界面版（群晖 / NAS / Linux 服务器）：浏览器访问 http://<IP>:18790 管理下载
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    KOI_HOME=/config \
    KOI_DOCKER=1 \
    KOI_DOWNLOAD_DIR=/downloads

WORKDIR /app
RUN pip install --no-cache-dir "libtorrent==2.1.1" "aiohttp>=3.9" "segno>=1.6" "cryptography>=42"
COPY koi ./koi
COPY static ./static
COPY browser_extension ./browser_extension
COPY main.py LICENSE ./

VOLUME ["/config", "/downloads"]
# 18790 网页界面 / 51413 BT 端口（TCP + UDP）
EXPOSE 18790 51413/tcp 51413/udp
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18790/api/ext/ping',timeout=4)"
CMD ["python", "main.py", "--headless"]
