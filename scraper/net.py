"""HTTP 抓取：gzip、编码嗅探、重试、限速。仅标准库。"""
import gzip
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_CTX_VERIFIED = ssl.create_default_context()
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE  # 个别党媒站点证书链不全时的回退

_last_hit = {}
_host_locks = {}
_global_lock = threading.Lock()  # 只保护 _host_locks 字典本身


def _throttle(host, min_gap=0.8):
    """按 host 限速。抓取是多线程的（fetch_body 并发 6），锁必须按 host 粒度：
    同一站点请求串行且间隔 >= min_gap，不同站点互不阻塞。"""
    with _global_lock:
        lk = _host_locks.setdefault(host, threading.Lock())
    with lk:
        now = time.time()
        if host in _last_hit and now - _last_hit[host] < min_gap:
            time.sleep(min_gap - (now - _last_hit[host]))
            now = time.time()
        _last_hit[host] = now


def _decode(raw, ctype):
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif raw[:1] == b"\x78":
        try:
            raw = zlib.decompress(raw)
        except Exception:
            pass

    charset = None
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    if m:
        charset = m.group(1).lower()

    if not charset:
        head = raw[:2048].decode("ascii", "ignore")
        m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
        if m:
            charset = m.group(1).lower()

    for enc in filter(None, [charset, "utf-8", "gb18030"]):
        if enc in ("gb2312", "gbk"):
            enc = "gb18030"
        try:
            return raw.decode(enc, "ignore")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore")


def _open(req, timeout):
    """先按证书校验打开；仅当失败原因是证书链不全时降级重试。

    抓取虽然不带凭据，但正文完整性重要——全局关校验等于允许中间人
    篡改正文注入内容，所以默认校验，按需降级。
    """
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX_VERIFIED)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            return urllib.request.urlopen(req, timeout=timeout, context=_CTX)
        raise


def fetch(url, timeout=20, retries=3, referer=None):
    """返回 HTML 文本；失败返回 None。"""
    # 限速 key 用归一 host（小写、去端口）：同一站点不同端口应是同一个限速桶
    host = (urllib.parse.urlsplit(url).hostname or url).lower()
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer

    for attempt in range(retries):
        _throttle(host)
        try:
            req = urllib.request.Request(url, headers=headers)
            with _open(req, timeout) as r:
                raw = r.read()
                return _decode(raw, r.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 410):
                return None
            if attempt == retries - 1:
                return None
        except Exception:
            if attempt == retries - 1:
                return None
        time.sleep(1.2 * (attempt + 1) + random.random())
    return None
