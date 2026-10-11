"""代理：BT（libtorrent）、直链下载（aiohttp）、零碎的网络请求（tracker 列表、种子缓存）都从这里拿代理设置。

设置项：
- proxy_type: none / socks5 / http
- proxy_host / proxy_port / proxy_user / proxy_pass
- proxy_bt: BT 的连接（tracker、DHT 以外的 peer 连接）走不走代理
- proxy_http: 直链下载和其他网页请求走不走代理
"""
import http.client
import ssl
import urllib.error
import urllib.parse
import urllib.request

import libtorrent as lt


def enabled(s):
    return s.get("proxy_type", "none") != "none" and s.get("proxy_host") and int(s.get("proxy_port") or 0) > 0


def url(s):
    """socks5://user:pass@host:port 这种格式，给 aiohttp / python-socks 用。"""
    if not enabled(s):
        return None
    auth = ""
    if s.get("proxy_user"):
        auth = urllib.parse.quote(s["proxy_user"], safe="") + ":" + urllib.parse.quote(s.get("proxy_pass", ""), safe="") + "@"
    return f"{s['proxy_type']}://{auth}{s['proxy_host']}:{int(s['proxy_port'])}"


def lt_settings(s):
    """libtorrent 的代理设置。不开代理时显式设成 none，免得改了设置后残留。"""
    if not (enabled(s) and s.get("proxy_bt", True)):
        return {"proxy_type": lt.proxy_type_t.none}
    has_pw = bool(s.get("proxy_user"))
    kind = {
        "socks5": lt.proxy_type_t.socks5_pw if has_pw else lt.proxy_type_t.socks5,
        "http": lt.proxy_type_t.http_pw if has_pw else lt.proxy_type_t.http,
    }[s["proxy_type"]]
    return {
        "proxy_type": kind,
        "proxy_hostname": s["proxy_host"],
        "proxy_port": int(s["proxy_port"]),
        "proxy_username": s.get("proxy_user", ""),
        "proxy_password": s.get("proxy_pass", ""),
        "proxy_hostnames": True,            # 域名也交给代理解析，不在本地查 DNS
        "proxy_peer_connections": True,
        "proxy_tracker_connections": True,
    }


def aiohttp_connector(s, **kw):
    """直链下载用的连接器：socks 代理要靠 aiohttp-socks；http 代理 aiohttp 自己就支持（在请求时传 proxy=）。"""
    import aiohttp
    if enabled(s) and s.get("proxy_http", True) and s["proxy_type"] == "socks5":
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(url(s), rdns=True, **kw)
    return aiohttp.TCPConnector(**kw)


def aiohttp_proxy(s):
    """http 代理时每个请求要带的 proxy 参数；socks 走连接器，这里返回 None。"""
    if enabled(s) and s.get("proxy_http", True) and s["proxy_type"] == "http":
        return url(s)
    return None


def urlopen(s, req, timeout=20):
    """代替 urllib.request.urlopen：按设置走代理。返回的对象支持 read() 和 with。"""
    if isinstance(req, str):
        req = urllib.request.Request(req)
    if not (enabled(s) and s.get("proxy_http", True)):
        return urllib.request.urlopen(req, timeout=timeout)
    if s["proxy_type"] == "http":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url(s), "https": url(s)}))
        return opener.open(req, timeout=timeout)
    # socks5：用 python-socks 建好隧道，再在上面跑 HTTP(S)
    from python_socks.sync import Proxy
    u = urllib.parse.urlsplit(req.full_url)
    https = u.scheme == "https"
    port = u.port or (443 if https else 80)
    sock = Proxy.from_url(url(s), rdns=True).connect(dest_host=u.hostname, dest_port=port, timeout=timeout)
    if https:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout) if https else http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    conn.sock = sock
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    headers = dict(req.header_items())
    headers.setdefault("Host", u.hostname)
    conn.request(req.get_method(), path, body=req.data, headers=headers)
    resp = conn.getresponse()
    if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
        loc = urllib.parse.urljoin(req.full_url, resp.getheader("Location"))
        conn.close()
        return urlopen(s, urllib.request.Request(loc, headers=headers), timeout)
    if resp.status >= 400:
        raise urllib.error.HTTPError(req.full_url, resp.status, resp.reason, resp.headers, None)
    return resp


def test(s, timeout=15):
    """设置页「测试代理」：通过代理访问一个网页，返回 (成功?, 说明)。"""
    if not enabled(s):
        return False, "没开代理"
    try:
        with urlopen(dict(s, proxy_http=True), "https://api.ipify.org?format=text", timeout=timeout) as r:
            ip = r.read(64).decode("ascii", "ignore").strip()
        return True, ip
    except Exception as e:
        return False, str(e)[:200]
