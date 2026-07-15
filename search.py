import sys
import json
import re
import os
import urllib.parse
import base64
import hashlib
import io
import ssl
import urllib.request
import urllib.error
import atexit
import threading
import socket
import ipaddress
import http.client
import gzip
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from freshness_cache import FreshnessCache, classify_cache_policy, make_cache_key
from discovery import (
    LinkGraph,
    canonicalize_url,
    extract_html_links,
    generate_query_expansions,
    host_matches_domain,
    is_public_http_url,
    relevance_score,
    tokenize_query,
)


_persistent_cache = None
_persistent_cache_lock = threading.Lock()
_smart_search_lock = threading.RLock()
_force_fresh = False


def _get_persistent_cache() -> FreshnessCache:
    """Create the process-wide bounded cache exactly once, even on cold concurrency."""
    global _persistent_cache
    if _persistent_cache is None:
        with _persistent_cache_lock:
            if _persistent_cache is None:
                configured = os.environ.get("WEB_SEARCH_CACHE_PATH")
                if configured:
                    cache_path = Path(configured).expanduser()
                else:
                    cache_path = Path(__file__).resolve().parent / "cache" / "cache.sqlite3"
                _persistent_cache = FreshnessCache(
                    cache_path,
                    memory_max_entries=int(os.environ.get("WEB_SEARCH_MEMORY_CACHE_ENTRIES", "512")),
                    memory_max_bytes=int(os.environ.get("WEB_SEARCH_MEMORY_CACHE_BYTES", str(32 * 1024 * 1024))),
                    disk_max_entries=int(os.environ.get("WEB_SEARCH_DISK_CACHE_ENTRIES", "20000")),
                    disk_max_bytes=int(os.environ.get("WEB_SEARCH_DISK_CACHE_BYTES", str(512 * 1024 * 1024))),
                )
    return _persistent_cache


def _close_persistent_cache() -> None:
    """Flush batched LRU touches and close the single cache connection."""
    global _persistent_cache
    with _persistent_cache_lock:
        cache = _persistent_cache
        _persistent_cache = None
    if cache is not None:
        cache.close()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_iso(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


atexit.register(_close_persistent_cache)

# ============================================================
# AD DETECTION - Enhanced for Baidu/Bing/Google
# ============================================================

AD_PATTERNS_GENERAL = [
    r'adsbygoogle', r'(?:class|id)="[^"]*\bad-(?:container|wrapper|banner|slot)\b[^"]*"',
    r'data-ad(?:s|slot|client|format)?=',
    r'aria-label="\s*(?:ad|ads|advertisement|sponsored|promoted)\s*"',
    r'role="advertisement"',
]

# Baidu-specific ad patterns (百度广告特征)
AD_PATTERNS_BAIDU = [
    r'class="[^"]*ec_tuiguang[^"]*"',    # 百度推广 class
    r'data-tuiguang',                      # 推广标记
    r'百度推广', r'百度营销', r'百度竞价',
    r'class="[^"]*c-tips-icon-text[^"]*">\s*广告',
    r'class="[^"]*tuiguang[^"]*"',
    r'aria-label="[^"]*广告[^"]*"',
    r'\[广告\]', r'\[推广\]',
    r'class="[^"]*ecommerce[^"]*"',
    r'data-click.*tuiguang',
    r'class="[^"]*c-tip-icon[^"]*".*广告',
    r'class="[^"]*res-top[^"]*".*广告',
    r'type="ad"',
    r'data-landurl',                       # 百度推广落地链接
]

# Bing-specific ad patterns
AD_PATTERNS_BING = [
    r'class="[^"]*b_ad[^"]*"',            # Bing ad container
    r'data-tag="Ads_Multi"',
    r'class="[^"]*b_algo[^"]*".*b_adurl', # Ad URL pattern
    r'aria-label="Ad"',
    r'class="[^"]*adsMv[^"]*"',
]

# Google-specific ad patterns
AD_PATTERNS_GOOGLE = [
    r'data-text-ad="1"',
    r'class="[^"]*ads-ad[^"]*"',
    r'class="[^"]*uEierd[^"]*"',          # Google ad class
    r'aria-label="Ads"',
    r'class="[^"]*commercial-unit[^"]*"',
    r'jscontroller="[^"]*".*data-text-ad',
]

_filter_stats: Dict[str, int] = {}
_filter_stats_lock = threading.Lock()


def _record_filter(reason: str) -> None:
    with _filter_stats_lock:
        _filter_stats[reason] = _filter_stats.get(reason, 0) + 1


def is_ad_url(url: str) -> bool:
    """Detect high-confidence ad-network and paid-click destinations."""
    try:
        parsed = urllib.parse.urlsplit(url or "")
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
    except ValueError:
        return False
    ad_domains = (
        "doubleclick.net", "googlesyndication.com", "googleadservices.com",
        "adservice.google.com", "ads.microsoft.com",
        "recommend_list.baidu.com", "pos.baidu.com", "union.baidu.com",
    )
    matched = any(host == domain or host.endswith("." + domain) for domain in ad_domains)
    matched = matched or (host.endswith("bing.com") and path.startswith("/aclick"))
    if matched:
        _record_filter("ad_network_url")
    return matched


def score_spam_result(title: str, snippet: str, url: str) -> Tuple[float, List[str]]:
    """Score uncertain affiliate/SEO promotion without aggressively deleting it."""
    score = 0.0
    reasons: List[str] = []
    try:
        parsed = urllib.parse.urlsplit(url or "")
    except ValueError:
        return 0.0, ["unparseable_url"]
    query_keys = {key.casefold() for key, _ in urllib.parse.parse_qsl(parsed.query)}
    affiliate_keys = {"aff", "affiliate", "affid", "partner", "referral", "refid", "coupon"}
    if query_keys & affiliate_keys:
        score += 0.35
        reasons.append("affiliate_parameter")
    path = parsed.path.casefold()
    if any(segment in path for segment in ("/sponsored/", "/advertorial/", "/paid-content/")):
        score += 0.55
        reasons.append("paid_content_path")
    text = f"{title} {snippet}".casefold()
    promotional = ["buy now", "coupon code", "limited offer", "best price",
                   "立即购买", "优惠券", "限时优惠", "全网最低"]
    promo_hits = sum(phrase in text for phrase in promotional)
    if promo_hits:
        score += min(0.45, 0.2 * promo_hits)
        reasons.append("promotional_language")
    title_tokens = re.findall(r"[a-z0-9\u3400-\u9fff]+", (title or "").casefold())
    if title_tokens and max(title_tokens.count(token) for token in set(title_tokens)) >= 4:
        score += 0.2
        reasons.append("title_keyword_stuffing")
    return min(1.0, round(score, 3)), reasons


# ============================================================
# FINGERPRINT POOL + CAPTCHA RETRY (anti-detection)
# ============================================================
import random, time as _time
import threading
import email.utils
from datetime import datetime, timezone

_host_rate_lock = threading.RLock()
_host_last_request: Dict[str, float] = {}


def _ssl_context():
    if os.environ.get("WEB_SEARCH_INSECURE_TLS") == "1":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    context = ssl.create_default_context()
    try:
        context.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:
        pass
    return context


def _allow_private_urls() -> bool:
    return os.environ.get("WEB_SEARCH_ALLOW_PRIVATE_URLS") == "1"


def _resolve_allowed_addresses(url: str) -> Tuple[str, ...]:
    """Resolve every connection attempt anew and retain only allowed addresses."""
    if not is_public_http_url(url, allow_private=_allow_private_urls()):
        return ()
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    try:
        addresses = {
            info[4][0].split("%", 1)[0]
            for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if info and len(info) >= 5 and info[4]
        }
        if not _allow_private_urls():
            if not addresses or not all(
                ipaddress.ip_address(address).is_global for address in addresses
            ):
                return ()
        return tuple(sorted(addresses, key=lambda value: (":" in value, value)))
    except (OSError, ValueError, UnicodeError):
        return ()


def _is_safe_fetch_url(url: str, *, resolve_dns: bool = True) -> bool:
    """Allow public HTTP(S) destinations and reject local/private resolution."""
    if not is_public_http_url(url, allow_private=_allow_private_urls()):
        return False
    if _allow_private_urls() or not resolve_dns:
        return True
    return bool(_resolve_allowed_addresses(url))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a verified IP while keeping TLS SNI/certificate host checks."""

    def __init__(self, hostname: str, address: str, port: int, timeout: int):
        self._verified_hostname = hostname
        super().__init__(address, port, timeout=timeout, context=_ssl_context())

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._verified_hostname
        )


class _PinnedResponse:
    def __init__(self, response, connection, url: str):
        self._response = response
        self._connection = connection
        self._url = url
        self.status = response.status
        self.reason = response.reason
        self.headers = response.headers

    def read(self, *args, **kwargs):
        return self._response.read(*args, **kwargs)

    def geturl(self):
        return self._url

    def close(self):
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _pinned_open_once(request: urllib.request.Request, *, timeout: int):
    """Open one HTTP hop through an IP fixed to this exact DNS validation."""
    url = request.full_url
    parsed = urllib.parse.urlsplit(url)
    addresses = _resolve_allowed_addresses(url)
    if not addresses:
        raise urllib.error.URLError("blocked non-public or unresolved address")
    address = addresses[0]
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    if parsed.scheme.casefold() == "https":
        connection = _PinnedHTTPSConnection(parsed.hostname or "", address, port, timeout)
    else:
        connection = http.client.HTTPConnection(address, port, timeout=timeout)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    headers = {key: value for key, value in request.header_items() if key.casefold() != "host"}
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    host_header = parsed.hostname or ""
    if parsed.port and parsed.port != default_port:
        host_header = f"{host_header}:{parsed.port}"
    headers["Host"] = host_header
    try:
        connection.request(request.get_method(), path, body=request.data, headers=headers)
        return _PinnedResponse(connection.getresponse(), connection, url)
    except Exception:
        connection.close()
        raise


def _guarded_urlopen(request, *, timeout: int):
    """Follow untrusted redirects with DNS validation pinned at every connection."""
    current = request if isinstance(request, urllib.request.Request) else urllib.request.Request(str(request))
    for _redirect in range(10):
        response = _pinned_open_once(current, timeout=timeout)
        status = int(response.status)
        location = response.headers.get("Location")
        if status in {301, 302, 303, 307, 308} and location:
            next_url = urllib.parse.urljoin(current.full_url, location)
            old_host = (urllib.parse.urlsplit(current.full_url).hostname or "").casefold()
            new_host = (urllib.parse.urlsplit(next_url).hostname or "").casefold()
            method = current.get_method()
            data = current.data
            if status == 303 or (status in {301, 302} and method not in {"GET", "HEAD"}):
                method, data = "GET", None
            headers = {
                key: value for key, value in current.header_items()
                if key.casefold() != "host"
                and not (old_host != new_host and key.casefold() in {"authorization", "cookie"})
            }
            response.close()
            if not is_public_http_url(next_url, allow_private=_allow_private_urls()):
                raise urllib.error.URLError("blocked redirect to non-public address")
            current = urllib.request.Request(
                next_url, data=data, headers=headers, method=method
            )
            continue
        if status == 304 or status >= 400:
            headers, reason, url = response.headers, response.reason, response.geturl()
            response.close()
            raise urllib.error.HTTPError(url, status, reason, headers, None)
        return response
    raise urllib.error.URLError("too many redirects")


def _retry_after_seconds(value: Optional[str], fallback: float) -> float:
    if value:
        try:
            return max(0.0, min(60.0, float(value)))
        except (TypeError, ValueError):
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, min(60.0, (parsed - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return fallback


def _fetch_bytes(url: str, *, headers: Optional[Dict[str, str]] = None,
                 timeout: int = 20, max_bytes: int = 8 * 1024 * 1024,
                 retries: int = 2, max_retry_delay: Optional[float] = None) -> bytes:
    """Bounded GET with per-host pacing, Retry-After and transient retries."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    min_interval = float(os.environ.get("WEB_SEARCH_MIN_HOST_INTERVAL", "0.2"))
    last_error = None
    for attempt in range(retries + 1):
        with _host_rate_lock:
            remaining = min_interval - (_time.monotonic() - _host_last_request.get(host, 0.0))
            if remaining > 0:
                _time.sleep(remaining)
            _host_last_request[host] = _time.monotonic()
        request = urllib.request.Request(
            url,
            headers={"Accept-Encoding": "identity", **(headers or {})},
        )
        try:
            with _guarded_urlopen(request, timeout=timeout) as response:
                declared = response.headers.get("Content-Length") if hasattr(response, "headers") else None
                if declared and str(declared).isdigit() and int(declared) > max_bytes:
                    raise ValueError(f"response exceeds byte limit ({declared}>{max_bytes})")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise ValueError(f"response exceeds byte limit ({max_bytes})")
                return payload
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {408, 425, 429, 500, 502, 503, 504} or attempt >= retries:
                raise
            delay = _retry_after_seconds(
                exc.headers.get("Retry-After") if exc.headers else None,
                min(30.0, (2 ** attempt) + random.uniform(0, 1.0)),
            )
            if max_retry_delay is not None:
                delay = min(delay, max_retry_delay)
            _time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            _time.sleep(min(10.0, (2 ** attempt) + random.uniform(0, 1.0)))
    raise last_error or RuntimeError("request failed")


def _fetch_json(url: str, *, headers: Optional[Dict[str, str]] = None,
                timeout: int = 20, retries: int = 2,
                max_retry_delay: Optional[float] = None) -> Dict[str, Any]:
    payload = _fetch_bytes(url, headers=headers, timeout=timeout, retries=retries,
                           max_retry_delay=max_retry_delay)
    return json.loads(payload.decode("utf-8"))

# Each profile carries the extra values a stealth init script needs to keep the
# JS-visible environment internally consistent (a spoofed UA whose WebGL renderer
# still says "SwiftShader", or whose navigator.languages disagrees with the
# locale, is a louder tell than no spoofing at all). WebGL vendor/renderer are
# chosen to match each profile's platform.
_FINGERPRINTS = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36", "platform": "Win32", "locale": "zh-CN", "tz": "Asia/Shanghai", "vp": {"width": 1920, "height": 1080}, "webgl_vendor": "Google Inc. (Intel)", "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)", "hardware_concurrency": 8, "device_memory": 8},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36", "platform": "Win32", "locale": "en-US", "tz": "America/New_York", "vp": {"width": 1536, "height": 864}, "webgl_vendor": "Google Inc. (NVIDIA)", "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)", "hardware_concurrency": 12, "device_memory": 8},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36", "platform": "Linux x86_64", "locale": "en-GB", "tz": "Europe/London", "vp": {"width": 1440, "height": 900}, "webgl_vendor": "Google Inc. (Intel)", "webgl_renderer": "ANGLE (Intel, Mesa Intel(R) UHD Graphics (CML GT2), OpenGL 4.6)", "hardware_concurrency": 8, "device_memory": 8},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36", "platform": "MacIntel", "locale": "ja-JP", "tz": "Asia/Tokyo", "vp": {"width": 1680, "height": 1050}, "webgl_vendor": "Google Inc. (Apple)", "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)", "hardware_concurrency": 10, "device_memory": 8},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0", "platform": "Win32", "locale": "de-DE", "tz": "Europe/Berlin", "vp": {"width": 1366, "height": 768}, "webgl_vendor": "Google Inc. (AMD)", "webgl_renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)", "hardware_concurrency": 16, "device_memory": 16},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36", "platform": "MacIntel", "locale": "fr-FR", "tz": "Europe/Paris", "vp": {"width": 1280, "height": 800}, "webgl_vendor": "Google Inc. (Apple)", "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)", "hardware_concurrency": 8, "device_memory": 8},
]

try:
    _configured_fingerprints = json.loads(os.environ.get("WEB_SEARCH_FINGERPRINTS_JSON", "null"))
    if isinstance(_configured_fingerprints, list) and _configured_fingerprints:
        _FINGERPRINTS = _configured_fingerprints
except (TypeError, ValueError, json.JSONDecodeError):
    pass

_fp_cooldown = {}  # {(engine, fp_index): cooldown_until_timestamp}
_fp_failures = {}  # {(engine, fp_index): consecutive verification failures}
_fp_last_used = {}  # {engine: fp_index}

def _get_fingerprint(engine: str = "general"):
    """Get a random fingerprint that is not in cooldown."""
    now = _time.time()
    for i in range(len(_FINGERPRINTS)):
        try:
            hit = _get_persistent_cache().get(
                "fingerprint-cooldown", make_cache_key(engine, i)
            )
            if hit.fresh and isinstance(hit.value, dict):
                _fp_cooldown[(engine, i)] = max(
                    _fp_cooldown.get((engine, i), 0), float(hit.value.get("until", 0))
                )
        except Exception:
            pass
    last_used = _fp_last_used.get(engine, -1)
    available = [
        i for i in range(len(_FINGERPRINTS))
        if _fp_cooldown.get((engine, i), 0) < now and i != last_used
    ]
    if not available:
        # All in cooldown or only one left, pick any non-cooldown
        available = [
            i for i in range(len(_FINGERPRINTS))
            if _fp_cooldown.get((engine, i), 0) < now
        ]
    if not available:
        # Keep forward progress: select the profile whose cooldown ends first.
        earliest = min(range(len(_FINGERPRINTS)), key=lambda i: _fp_cooldown.get((engine, i), 0))
        wait_for = max(0.0, _fp_cooldown.get((engine, earliest), 0) - now)
        if wait_for:
            _time.sleep(min(5.0, wait_for))
        available = [earliest]
    idx = random.choice(available)
    _fp_last_used[engine] = idx
    return idx, _FINGERPRINTS[idx]

def _cooldown_fingerprint(idx, engine: str = "general", base_seconds: int = 45):
    """Cool one engine/profile pair with bounded exponential backoff."""
    key = (engine, idx)
    failures = _fp_failures.get(key, 0) + 1
    _fp_failures[key] = failures
    seconds = min(15 * 60, base_seconds * (2 ** (failures - 1)))
    until = _time.time() + seconds + random.uniform(0, min(15, seconds * 0.2))
    _fp_cooldown[key] = until
    try:
        _get_persistent_cache().set(
            "fingerprint-cooldown", make_cache_key(engine, idx),
            {"until": until, "failures": failures}, ttl=max(1, until - _time.time()),
        )
    except Exception:
        pass

def _mark_fingerprint_success(idx, engine: str = "general"):
    """Clear consecutive-failure state after a successful organic result page."""
    _fp_failures.pop((engine, idx), None)
    _fp_cooldown.pop((engine, idx), None)
    try:
        _get_persistent_cache().delete("fingerprint-cooldown", make_cache_key(engine, idx))
    except Exception:
        pass

_CAPTCHA_BODY_SIGNALS = [
    "安全验证", "人机验证", "请完成下方验证", "拖动左侧滑块",
    "captcha", "verify you are human", "unusual traffic",
    "robot check", "prove you're not a robot", "recaptcha",
    "please verify", "滑动验证", "点击验证", "智能验证",
    "访问过于频繁", "请求频率过高", "temporarily blocked",
    # Bing serves challenge pages in the fingerprint's locale.
    "résoudre le défi", "une dernière étape", "one last step",
    "löse die aufgabe", "resuelve el desafío", "解決してください",
    "solve the challenge",
]
_CAPTCHA_URL_SIGNALS = ["/sorry/", "captcha", "challenge", "verify"]

# Specific -> generic classifiers. Used ONLY to cool an engine down
# proportionally (an interactive slider means the engine is fairly sure we are a
# bot; a soft interstitial is more recoverable) and to report honestly which
# kind of wall a channel hit. This recognizes challenges to back off politely --
# it does not, and must not, attempt to solve or bypass any of them.
_CAPTCHA_TYPE_SIGNALS = [
    ("slider", ["滑块", "拖动左侧", "拖动滑块", "向右滑", "滑动验证", "slide to", "drag the slider", "swipe"]),
    ("rotate", ["旋转", "rotate the", "turn the image"]),
    ("click-select", ["依次点击", "顺序点击", "点选", "点击文字", "click the", "select each", "click in order"]),
    ("image-grid", ["recaptcha", "hcaptcha", "select all images", "选择所有图", "squares with", "matching images"]),
    ("interstitial", ["unusual traffic", "one last step", "une dernière étape", "异常流量",
                      "访问过于频繁", "请求频率过高", "systems have detected", "temporarily blocked"]),
]

# Cool interactive/hard challenges longer than soft interstitials.
_CAPTCHA_COOLDOWN_BASE = {
    "slider": 60, "rotate": 60, "image-grid": 75, "click-select": 60,
    "interstitial": 30, "generic": 45,
}


def _captcha_page_signals(page) -> Tuple[str, str, str]:
    """(body, title, url) lowercased, best-effort; empty strings on failure."""
    try:
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        if not body:
            try:
                body = page.text_content("body") or ""
            except Exception:
                body = ""
        return body[:5000].lower(), (page.title() or "").lower(), (page.url or "").lower()
    except Exception:
        return "", "", ""


def _captcha_type(page) -> str:
    """Classify a verification page for backoff/telemetry. Returns '' when the
    page is not a challenge. Recognition only -- never an attempt to solve."""
    body, title, url = _captcha_page_signals(page)
    blob = body + " " + title
    is_captcha = (any(s in blob for s in _CAPTCHA_BODY_SIGNALS)
                  or any(s in url for s in _CAPTCHA_URL_SIGNALS))
    if not is_captcha:
        return ""
    for label, needles in _CAPTCHA_TYPE_SIGNALS:
        if any(n in blob for n in needles):
            return label
    return "generic"


def _is_captcha_page(page) -> bool:
    """True when the current page is a CAPTCHA/verification wall."""
    return bool(_captcha_type(page))


def _element_text(element) -> str:
    """Read visible text, falling back to DOM text when CSS marks it hidden."""
    if not element:
        return ""
    try:
        value = (element.inner_text() or "").strip()
    except Exception:
        value = ""
    if value:
        return value
    try:
        return (element.text_content() or "").strip()
    except Exception:
        return ""

def _pin_candidates_for_url(url: str) -> Dict[str, Tuple[str, ...]]:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold()
    addresses = _resolve_allowed_addresses(url)
    return {host: addresses} if host and addresses else {}


def _locale_languages(locale: str) -> List[str]:
    """Turn a profile locale into a plausible navigator.languages list."""
    locale = (locale or "en-US").replace("_", "-")
    primary = locale.split("-")[0]
    langs = [locale, primary] if primary and primary != locale else [locale]
    if primary != "en" and "en" not in langs:
        langs.append("en")
    seen: set = set()
    out: List[str] = []
    for lang in langs:
        if lang and lang not in seen:
            seen.add(lang)
            out.append(lang)
    return out


def _context_extra_headers(fp: Dict[str, Any]) -> Dict[str, str]:
    """Accept-Language header consistent with the profile locale (Playwright's
    locale drives a single value; a descending-q multi-language list matches a
    real browser more closely)."""
    langs = _locale_languages(str(fp.get("locale", "en-US")))
    parts = [langs[0]]
    q = 9
    for lang in langs[1:]:
        parts.append(f"{lang};q=0.{q}")
        q = max(1, q - 1)
    return {"Accept-Language": ", ".join(parts)}


# Static body of the stealth init script.  It reads a `cfg` object that
# _build_stealth_script prepends, so all per-profile values stay consistent and
# the whole thing is injected through a SINGLE context.add_init_script call.
# Every patch is individually try/guarded: one failing override must not abort
# the rest of the script.
_STEALTH_BODY = r"""
const def = (obj, prop, getter) => {
  try { Object.defineProperty(obj, prop, {get: getter, configurable: true}); } catch (e) {}
};
try { Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined, configurable: true}); } catch (e) {}
def(navigator, 'webdriver', () => undefined);
def(navigator, 'platform', () => cfg.platform);
def(navigator, 'languages', () => cfg.languages);
def(navigator, 'language', () => cfg.languages[0]);
def(navigator, 'hardwareConcurrency', () => cfg.hardwareConcurrency);
def(navigator, 'deviceMemory', () => cfg.deviceMemory);
try {
  if (!window.chrome) { window.chrome = {}; }
  if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  if (!window.chrome.app) {
    window.chrome.app = {isInstalled: false,
      InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'},
      RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}};
  }
} catch (e) {}
try {
  const orig = window.navigator.permissions && window.navigator.permissions.query;
  if (orig) {
    window.navigator.permissions.query = (params) => (
      params && params.name === 'notifications'
        ? Promise.resolve({state: (typeof Notification !== 'undefined' ? Notification.permission : 'default')})
        : orig.call(window.navigator.permissions, params)
    );
  }
} catch (e) {}
try {
  const mk = (name, desc) => ({name: name, filename: 'internal-pdf-viewer', description: desc, length: 1});
  const plugins = [
    mk('PDF Viewer', 'Portable Document Format'),
    mk('Chrome PDF Viewer', 'Portable Document Format'),
    mk('Chromium PDF Viewer', 'Portable Document Format'),
    mk('Microsoft Edge PDF Viewer', 'Portable Document Format'),
    mk('WebKit built-in PDF', 'Portable Document Format'),
  ];
  def(navigator, 'plugins', () => plugins);
  def(navigator, 'mimeTypes', () => [{type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'}]);
} catch (e) {}
try {
  const patchGl = (proto) => {
    if (!proto) return;
    const gp = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return cfg.webglVendor;    // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return cfg.webglRenderer;   // UNMASKED_RENDERER_WEBGL
      return gp.apply(this, arguments);
    };
  };
  patchGl(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGl(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
} catch (e) {}
try {
  // Subtle, per-context-stable canvas readback noise defeats hash-based canvas
  // fingerprinting without visibly altering the page (only the read-out copy is
  // perturbed, by at most 1/255 on the red channel).
  const seed = cfg.canvasSeed;
  const orig = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function () {
    const res = orig.apply(this, arguments);
    try {
      const d = res.data;
      for (let i = 0; i < d.length; i += 4) {
        const n = ((seed + i) % 3) - 1;
        d[i] = Math.max(0, Math.min(255, d[i] + n));
      }
    } catch (e) {}
    return res;
  };
} catch (e) {}
"""


def _build_stealth_script(fp: Dict[str, Any], chromium_major) -> str:
    """Build the per-profile stealth init script (injected once per context)."""
    try:
        major = int(str(chromium_major).split(".")[0])
    except (TypeError, ValueError):
        major = 126
    locale = str(fp.get("locale", "en-US"))
    cfg = {
        "platform": fp.get("platform", "Win32"),
        "languages": _locale_languages(locale),
        "webglVendor": fp.get("webgl_vendor", "Google Inc. (Intel)"),
        "webglRenderer": fp.get(
            "webgl_renderer",
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ),
        "hardwareConcurrency": int(fp.get("hardware_concurrency", 8) or 8),
        "deviceMemory": int(fp.get("device_memory", 8) or 8),
        "major": major,
        "canvasSeed": random.randint(1, 250),
    }
    return "(() => {\nconst cfg = " + json.dumps(cfg) + ";\n" + _STEALTH_BODY + "\n})();"


def _human_jitter(base_ms: float, intensity: int = 0) -> float:
    """A positive, right-skewed dwell time (ms). Real dwell times are lognormal-
    ish, not uniform; intensity stretches them for escalation retries."""
    scale = 1.0 + 0.6 * max(0, intensity)
    val = base_ms * scale * (0.6 + random.random() * 0.8)
    val += random.expovariate(1.0 / (110.0 * scale))
    return max(30.0, min(9000.0, val))


def _humanize_page(page, fp, intensity: int = 0) -> None:
    """Best-effort human-like interaction (jittered dwell, mouse drift, natural
    scroll). Fully guarded: behavioral simulation must never be what kills a
    channel, so every failure is swallowed. Intensity scales with the retry/
    escalation level, so each successive attempt looks more patiently human."""
    try:
        vp = fp.get("vp", {}) if isinstance(fp, dict) else {}
        w = max(64, int(vp.get("width", 1280)))
        h = max(64, int(vp.get("height", 800)))
        x = random.randint(2, w - 2)
        y = random.randint(2, h - 2)
        for _ in range(2 + max(0, intensity) * 2):
            nx = min(w - 1, max(1, x + random.randint(-w // 4, w // 4)))
            ny = min(h - 1, max(1, y + random.randint(-h // 4, h // 4)))
            try:
                page.mouse.move(nx, ny, steps=random.randint(4, 12))
            except Exception:
                break
            x, y = nx, ny
            try:
                page.wait_for_timeout(int(_human_jitter(55, intensity)))
            except Exception:
                break
        for _ in range(1 + max(0, intensity)):
            try:
                page.mouse.wheel(0, random.randint(220, 720))
                page.wait_for_timeout(int(_human_jitter(120, intensity)))
            except Exception:
                break
        try:
            page.wait_for_timeout(int(_human_jitter(140, intensity)))
        except Exception:
            pass
    except Exception:
        pass


_SYNC_PLAYWRIGHT_SOURCE: Optional[str] = None


def _get_sync_playwright():
    """Return a ``sync_playwright`` factory, preferring patchright when present.

    patchright is a drop-in Playwright fork that closes the CDP ``Runtime.enable``
    leak most modern anti-bot vendors (Cloudflare/DataDome/Akamai) fingerprint;
    installing it (``uv add patchright`` / ``pip install patchright``) upgrades
    every browser channel with zero code change here. Falls back to vanilla
    Playwright, and to ``None`` if neither import succeeds. Set
    WEB_SEARCH_DISABLE_PATCHRIGHT=1 to force vanilla Playwright even when
    patchright is installed."""
    global _SYNC_PLAYWRIGHT_SOURCE
    disabled = os.environ.get("WEB_SEARCH_DISABLE_PATCHRIGHT", "").strip().lower() in {
        "1", "true", "yes", "on"}
    if not disabled:
        try:
            from patchright.sync_api import sync_playwright as _sp
            _SYNC_PLAYWRIGHT_SOURCE = "patchright"
            return _sp
        except Exception:
            pass
    try:
        from playwright.sync_api import sync_playwright as _sp
        _SYNC_PLAYWRIGHT_SOURCE = "playwright"
        return _sp
    except Exception:
        _SYNC_PLAYWRIGHT_SOURCE = None
        return None


def _new_browser_with_fingerprint(p, fp_idx=None, engine: str = "general",
                                  host_pins: Optional[Dict[str, Tuple[str, ...]]] = None):
    """Create a new browser with a specific or random fingerprint."""
    if fp_idx is None:
        fp_idx, fp = _get_fingerprint(engine)
    else:
        fp = _FINGERPRINTS[fp_idx % len(_FINGERPRINTS)]

    if host_pins is None:
        engine_urls = {
            "google": "https://www.google.com/",
            "bing": "https://www.bing.com/",
            "baidu": "https://www.baidu.com/",
        }
        engine_url = engine_urls.get(engine.casefold())
        host_pins = _pin_candidates_for_url(engine_url) if engine_url else {}
    normalized_pins: Dict[str, str] = {}
    for host, addresses in (host_pins or {}).items():
        if addresses:
            normalized_pins[host.casefold()] = addresses[fp_idx % len(addresses)]
    if engine.casefold() in {"google", "bing", "baidu"} and not normalized_pins:
        raise RuntimeError(f"{engine} host did not resolve to a verified address")
    
    locale = str(fp.get("locale", "en-US"))
    launch_args = ["--disable-blink-features=AutomationControlled",
                   "--disable-gpu", "--disable-dev-shm-usage",
                   "--no-first-run", "--no-default-browser-check",
                   f"--lang={locale}"]
    if normalized_pins and not _allow_private_urls():
        rules = []
        for host, address in normalized_pins.items():
            target = f"[{address}]" if ":" in address else address
            rules.append(f"MAP {host} {target}")
        rules.append("EXCLUDE localhost")
        launch_args.append("--host-resolver-rules=" + ",".join(rules))
    # WEB_SEARCH_HEADFUL=1 runs a visible browser (needs a display) for the very
    # hardest targets; default stays headless.
    headful = os.environ.get("WEB_SEARCH_HEADFUL", "").strip().lower() in {"1", "true", "yes", "on"}
    launch_options: Dict[str, Any] = {
        "headless": not headful,
        "args": launch_args,
        # Chromium adds "--enable-automation" by default; its presence (infobar,
        # navigator.webdriver=true, differing defaults) is a loud automation tell.
        "ignore_default_args": ["--enable-automation"],
    }
    configured_browser = os.environ.get("WEB_SEARCH_CHROMIUM_EXECUTABLE", "").strip()
    if configured_browser:
        launch_options["executable_path"] = configured_browser
    else:
        project_browsers = Path(__file__).resolve().parent / "browsers"
        local_candidates = sorted(
            project_browsers.glob("chromium-*/chrome-win*/chrome.exe"), reverse=True
        )
        if local_candidates:
            launch_options["executable_path"] = str(local_candidates[0])
    browser = p.chromium.launch(**launch_options)
    try:
        chromium_major = str(getattr(browser, "version", "126")).split(".", 1)[0]
        user_agent = str(fp["ua"]).replace("{major}", chromium_major)
        context = browser.new_context(
            user_agent=user_agent,
            locale=fp["locale"],
            timezone_id=fp["tz"],
            viewport=fp["vp"],
            color_scheme="light",
            extra_http_headers=_context_extra_headers(fp),
        )
        load_heavy_assets = os.environ.get("WEB_SEARCH_LOAD_HEAVY_ASSETS") == "1"
        def _route_request(route):
            try:
                request = route.request
                request_url = request.url
                if request_url.startswith(("http://", "https://")):
                    if not _is_safe_fetch_url(request_url, resolve_dns=False):
                        route.abort()
                        return
                    if (not load_heavy_assets
                            and request.resource_type in {"image", "media", "font"}):
                        route.abort()
                        return
                    request_host = (urllib.parse.urlsplit(request_url).hostname or "").casefold()
                    if not _allow_private_urls() and request_host not in normalized_pins:
                        route.abort()
                        return
                if is_ad_url(request_url):
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                try:
                    route.abort()
                except Exception:
                    pass
        context.route("**/*", _route_request)
        context.add_init_script(_build_stealth_script(fp, chromium_major))
        return browser, context, fp_idx
    except Exception:
        browser.close()
        raise

def is_ad(html: str, text: str, engine: str = "general") -> bool:
    patterns = list(AD_PATTERNS_GENERAL)
    if engine == "baidu":
        patterns.extend(AD_PATTERNS_BAIDU)
    elif engine == "bing":
        patterns.extend(AD_PATTERNS_BING)
    elif engine == "google":
        patterns.extend(AD_PATTERNS_GOOGLE)
    if any(re.search(pattern, html or "", re.IGNORECASE | re.DOTALL) for pattern in patterns):
        _record_filter(f"{engine}_structural_ad")
        return True
    return is_ad_text(text)

def is_ad_text(text: str) -> bool:
    """Return True only for explicit ad labels, not incidental substrings.

    This deliberately avoids checks such as ``"ad" in text`` which classify
    normal words including "download", "roadmap", and "Adobe" as ads.
    """
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t:
        return False
    label = r"(?:ad|ads|advertisement|sponsored|promoted|广告|推广|赞助|百度推广|百度营销)"
    matched = bool(
        re.fullmatch(rf"\s*{label}\s*", t, re.IGNORECASE)
        or re.match(rf"\s*{label}\s*[:：|·•-]", t, re.IGNORECASE)
        or re.match(r"\s*sponsored\s+(?:result|listing|link|content)\b", t, re.IGNORECASE)
        or re.match(r"\s*promoted\s+by\b", t, re.IGNORECASE)
        or re.search(rf"[\[【(]\s*{label}\s*[\]】)]", t, re.IGNORECASE)
    )
    if matched:
        _record_filter("explicit_ad_label")
    return matched

# ============================================================
# URL RESOLUTION
# ============================================================

def resolve_bing_redirect(url: str) -> str:
    if not url:
        return url
    if "bing.com/ck/a" in url:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        if "u" in params:
            encoded = params["u"][0]
            if encoded.startswith("a1"):
                encoded = encoded[2:]
            try:
                padding = 4 - len(encoded) % 4
                if padding != 4:
                    encoded += "=" * padding
                decoded = base64.urlsafe_b64decode(encoded).decode("utf-8", errors="ignore")
                if decoded.startswith("http"):
                    return decoded
            except Exception:
                pass
    if not url.startswith("http") and len(url) > 20:
        try:
            padding = 4 - len(url) % 4
            url_padded = url + "=" * padding if padding != 4 else url
            decoded = base64.urlsafe_b64decode(url_padded).decode("utf-8", errors="ignore")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass
    return url

def resolve_baidu_redirect(url: str) -> str:
    """Baidu wraps URLs in baidu.com/link?url=... redirects."""
    if not url:
        return url
    # Baidu direct links are fine; only redirect links need resolution
    # For now, keep baidu redirect URLs as-is since resolving requires HTTP follow
    return url

# ============================================================
# PLAYWRIGHT BROWSER FACTORY
# ============================================================

def _new_stealth_browser(p, fp_idx=None, engine: str = "general",
                         host_pins: Optional[Dict[str, Tuple[str, ...]]] = None):
    """Compatibility wrapper around the rotating fingerprint factory.

    Keep one browser factory contract everywhere: callers always receive
    ``(browser, context, fingerprint_id)`` and can cool down the exact profile
    that encountered a verification page.
    """
    return _new_browser_with_fingerprint(p, fp_idx, engine, host_pins=host_pins)

# ============================================================
# SEARCH ENGINES
# ============================================================

def _skip_browser() -> bool:
    """True when WEB_SEARCH_SKIP_BROWSER is set: disable browser (Playwright/
    Chromium) channels so the skill runs in sandboxes that cannot launch a
    browser (e.g. Codex's exec_command).  Browser SERP channels then return
    empty (stale cache is still served by _run_channel_cached) and deep reads
    fall back to plain-HTTP extraction via _http_extract_content."""
    return os.environ.get("WEB_SEARCH_SKIP_BROWSER", "").strip().lower() in {"1", "true", "yes", "on"}

def playwright_google_search(query: str, limit: int = 20) -> Tuple[List[Dict], Optional[str]]:
    """Google search with CAPTCHA retry and fingerprint rotation."""
    if _skip_browser():
        return [], "Google skipped: WEB_SEARCH_SKIP_BROWSER set (browser channels disabled)"
    sync_playwright = _get_sync_playwright()
    if sync_playwright is None:
        return [], "Playwright not available"
    
    max_attempts = min(3, len(_FINGERPRINTS))
    last_error = None
    for attempt in range(max_attempts):
        results = []
        seen_titles = set()
        used_fp = None
        with sync_playwright() as p:
            try:
                browser, context, used_fp = _new_stealth_browser(p, engine="google")
            except Exception as exc:
                last_error = f"Google browser setup failed: {exc}"
                continue
            try:
                try:
                    page = context.new_page()
                    url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&num={limit + 15}&hl=en"
                    response = page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector("div.g h3, div[data-hveid] h3", timeout=4000)
                        page.wait_for_timeout(400)
                    except Exception:
                        page.wait_for_timeout(1500)
                    if response and response.status in {403, 429, 503}:
                        last_error = f"Google HTTP {response.status}"
                        _cooldown_fingerprint(used_fp, "google")
                        continue
                except Exception as e:
                    last_error = f"Google load failed: {e}"
                    _cooldown_fingerprint(used_fp, "google")
                    continue

                cap_kind = _captcha_type(page)
                if cap_kind:
                    last_error = f"Google CAPTCHA detected [{cap_kind}]"
                    _cooldown_fingerprint(used_fp, "google",
                                          base_seconds=_CAPTCHA_COOLDOWN_BASE.get(cap_kind, 45))
                    continue

                _humanize_page(page, _FINGERPRINTS[used_fp], attempt)
                blocks = page.query_selector_all("div.g, div[data-hveid]")
                for block in blocks:
                    try:
                        outer = block.evaluate("el => el.outerHTML")
                        if is_ad(outer, "", "google"):
                            continue
                        h3 = block.query_selector("h3")
                        if not h3:
                            continue
                        title = _element_text(h3)
                        link_el = block.query_selector("a[href]")
                        href = link_el.get_attribute("href") if link_el else ""
                        if not title or len(title) < 5 or not href or "google.com" in href:
                            continue
                        snippet_el = block.query_selector("[data-sncf], .VwiC3b, span.st, div.IsZvec, [style*='-webkit-line-clamp']")
                        snippet = _element_text(snippet_el)[:500]
                        if is_ad("", title + " " + snippet, "google"):
                            continue
                        title_key = title.lower().strip()
                        if title_key in seen_titles:
                            continue
                        seen_titles.add(title_key)
                        results.append({"engine": "Google", "title": title, "url": href,
                                        "snippet": snippet, "type": "organic", "rank": len(results) + 1})
                        if len(results) >= limit:
                            break
                    except Exception:
                        continue
            finally:
                browser.close()
        if results:
            _mark_fingerprint_success(used_fp, "google")
            return results, None
        last_error = last_error or "Google returned 0 results"
        if used_fp is not None:
            _cooldown_fingerprint(used_fp, "google", base_seconds=20)
    return [], last_error or "Google returned 0 results"

def playwright_bing_search(query: str, limit: int = 20) -> Tuple[List[Dict], Optional[str]]:
    """Bing search with CAPTCHA retry and fingerprint rotation."""
    if _skip_browser():
        return [], "Bing skipped: WEB_SEARCH_SKIP_BROWSER set (browser channels disabled)"
    sync_playwright = _get_sync_playwright()
    if sync_playwright is None:
        return [], "Playwright not available"
    last_error = None
    for _attempt in range(min(3, len(_FINGERPRINTS))):
        results = []
        seen_titles = set()
        used_fp = None
        with sync_playwright() as p:
            try:
                browser, context, used_fp = _new_stealth_browser(p, engine="bing")
            except Exception as exc:
                last_error = f"Bing browser setup failed: {exc}"
                continue
            try:
                page = context.new_page()
                url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&count={limit + 15}"
                try:
                    response = page.goto(url, timeout=45000, wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector("li.b_algo", timeout=5000)
                        page.wait_for_timeout(400)
                    except Exception:
                        page.wait_for_timeout(1800)
                    if response and response.status in {403, 429, 503}:
                        last_error = f"Bing HTTP {response.status}"
                        _cooldown_fingerprint(used_fp, "bing")
                        continue
                except Exception as e:
                    last_error = f"Bing load failed: {e}"
                    _cooldown_fingerprint(used_fp, "bing")
                    continue
                cap_kind = _captcha_type(page)
                if cap_kind:
                    last_error = f"Bing CAPTCHA detected [{cap_kind}]"
                    _cooldown_fingerprint(used_fp, "bing",
                                          base_seconds=_CAPTCHA_COOLDOWN_BASE.get(cap_kind, 45))
                    continue
                _humanize_page(page, _FINGERPRINTS[used_fp], _attempt)
                for block in page.query_selector_all("li.b_algo"):
                    try:
                        outer = block.evaluate("el => el.outerHTML")
                        if is_ad(outer, "", "bing"):
                            continue
                        title_el = block.query_selector("h2 a")
                        if not title_el:
                            continue
                        title = _element_text(title_el)
                        href = title_el.get_attribute("href") or ""
                        if not title or len(title) < 5 or not href:
                            continue
                        real_url = resolve_bing_redirect(href)
                        snippet_el = block.query_selector("p.b_lineclamp2, .b_caption p, span.st")
                        snippet = _element_text(snippet_el)[:500]
                        if is_ad("", title + " " + snippet, "bing"):
                            continue
                        title_key = title.lower().strip()
                        if title_key in seen_titles:
                            continue
                        seen_titles.add(title_key)
                        results.append({"engine": "Bing", "title": title, "url": real_url,
                                        "snippet": snippet, "type": "organic", "rank": len(results) + 1})
                        if len(results) >= limit:
                            break
                    except Exception:
                        continue
            finally:
                browser.close()
        if results:
            _mark_fingerprint_success(used_fp, "bing")
            return results, None
        last_error = last_error or "Bing returned 0 results"
        if used_fp is not None:
            _cooldown_fingerprint(used_fp, "bing", base_seconds=20)
    return [], last_error or "Bing returned 0 results"

_baidu_captcha_cooldown_until = 0.0

def playwright_baidu_search(query: str, limit: int = 20) -> Tuple[List[Dict], Optional[str]]:
    """Baidu search with CAPTCHA retry and fingerprint rotation."""
    if _skip_browser():
        return [], "Baidu skipped: WEB_SEARCH_SKIP_BROWSER set (browser channels disabled)"
    global _baidu_captcha_cooldown_until
    if _time.time() < _baidu_captcha_cooldown_until:
        return [], "Baidu skipped: CAPTCHA cooldown from earlier this session"
    sync_playwright = _get_sync_playwright()
    if sync_playwright is None:
        return [], "Playwright not available"
    
    max_attempts = min(3, len(_FINGERPRINTS))
    last_error = None
    for _attempt in range(max_attempts):
        results = []
        seen_titles = set()
        used_fp = None
        with sync_playwright() as p:
            try:
                browser, context, used_fp = _new_stealth_browser(p, engine="baidu")
            except Exception as exc:
                last_error = f"Baidu browser setup failed: {exc}"
                continue
            try:
                page = context.new_page()
                url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}&rn={limit + 15}"
                try:
                    page.wait_for_timeout(random.randint(500, 1500))
                    response = page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector("div.c-container, div.result", timeout=5000)
                        page.wait_for_timeout(random.randint(500, 900))
                    except Exception:
                        page.wait_for_timeout(random.randint(1500, 2500))
                    if response and response.status in {403, 429, 503}:
                        last_error = f"Baidu HTTP {response.status}"
                        _cooldown_fingerprint(used_fp, "baidu")
                        continue
                except Exception as e:
                    last_error = f"Baidu load failed: {e}"
                    _cooldown_fingerprint(used_fp, "baidu")
                    continue

                cap_kind = _captcha_type(page)
                if cap_kind:
                    last_error = f"Baidu CAPTCHA detected [{cap_kind}]"
                    _cooldown_fingerprint(used_fp, "baidu",
                                          base_seconds=_CAPTCHA_COOLDOWN_BASE.get(cap_kind, 45))
                    continue

                _humanize_page(page, _FINGERPRINTS[used_fp], _attempt)
                # Parse while Playwright and the page are still alive.
                blocks = page.query_selector_all(
                    "div.c-container, div.result, div[class*='result'], div[class*='container']"
                )
                for block in blocks:
                    try:
                        outer = block.evaluate("el => el.outerHTML")
                        if is_ad(outer, "", "baidu"):
                            continue
                        ad_label = block.query_selector(
                            ".c-tips-icon-text, .tuiguang-label, span[class*='tuiguang']"
                        )
                        if ad_label and is_ad_text(_element_text(ad_label)):
                            continue
                        tuiguang_attr = block.get_attribute("data-tuiguang")
                        if tuiguang_attr:
                            continue
                        title_el = block.query_selector("h3 a, .c-title a, a[href]")
                        if not title_el:
                            continue
                        title = _element_text(title_el)
                        href = title_el.get_attribute("href") or ""
                        if not title or len(title) < 5 or not href:
                            continue
                        # Baidu often exposes the direct destination as the result container's mu.
                        direct_url = block.get_attribute("mu") or ""
                        real_url = direct_url if direct_url.startswith("http") else resolve_baidu_redirect(href)
                        snippet_el = block.query_selector(
                            ".c-abstract, .c-span-last, p[class*='content'], "
                            "span[class*='content'], div[class*='abstract']"
                        )
                        snippet = _element_text(snippet_el)[:500]
                        combined_text = title + " " + snippet
                        if is_ad_text(combined_text) or is_ad("", combined_text, "baidu"):
                            continue
                        title_key = title.lower().strip()
                        if title_key in seen_titles:
                            continue
                        seen_titles.add(title_key)
                        results.append({"engine": "Baidu", "title": title, "url": real_url,
                                        "snippet": snippet, "type": "organic", "rank": len(results) + 1})
                        if len(results) >= limit:
                            break
                    except Exception:
                        continue
            finally:
                browser.close()
        if results:
            _mark_fingerprint_success(used_fp, "baidu")
            return results, None
        last_error = last_error or "Baidu returned 0 results"
        if used_fp is not None:
            _cooldown_fingerprint(used_fp, "baidu", base_seconds=20)
    if last_error and "CAPTCHA" in last_error:
        # Baidu blocks at the IP level; retrying every round just burns time and
        # hammers a host that already flagged us.  Back off for the session --
        # _run_channel_cached still serves stale Baidu results meanwhile, so
        # coverage is preserved while the request footprint drops.
        _baidu_captcha_cooldown_until = _time.time() + 600
    return [], last_error or "Baidu returned 0 results"

# ============================================================
# DEEP CONTENT EXTRACTION
# ============================================================

# === CONTENT CACHE (P1: avoid re-fetching same URL) ===
_content_cache = {}  # {url: extracted_text}
_extraction_metadata = {}  # normalized URL -> structured extraction metadata
_CACHE_MAX = 50

def _cache_get(url: str) -> Optional[str]:
    if url not in _content_cache:
        return None
    value = _content_cache.pop(url)
    _content_cache[url] = value
    return value

def _cache_set(url: str, text: str):
    if len(_content_cache) >= _CACHE_MAX:
        # Evict oldest entry (dict preserves insertion order in Python 3.7+)
        oldest = next(iter(_content_cache))
        del _content_cache[oldest]
    _content_cache[url] = text

def get_extraction_metadata(url: str) -> Dict[str, Any]:
    """Return metadata captured by the latest content extraction for a URL."""
    return dict(_extraction_metadata.get(normalize_url(url), {}))


def _revalidate_cached_resource(url: str, hit, persistent: FreshnessCache,
                                content_key: str, policy) -> bool:
    """Refresh a cached resource cheaply when its HTTP validators are unchanged."""
    if not hit or hit.is_failure or not (hit.etag or hit.last_modified):
        return False
    headers = {"User-Agent": "web-search-skill/22.0"}
    if hit.etag:
        headers["If-None-Match"] = hit.etag
    if hit.last_modified:
        headers["If-Modified-Since"] = hit.last_modified
    request = urllib.request.Request(url, headers=headers, method="HEAD")
    current_etag = None
    current_modified = None
    try:
        with _guarded_urlopen(request, timeout=15) as response:
            current_etag = response.headers.get("ETag")
            current_modified = response.headers.get("Last-Modified")
            # ETag is the stronger validator.  If one was cached, a changed or
            # missing current ETag must not be overruled by an unchanged date.
            if hit.etag:
                unchanged = bool(current_etag and hit.etag == current_etag)
            else:
                unchanged = bool(
                    hit.last_modified and current_modified
                    and hit.last_modified == current_modified
                )
    except urllib.error.HTTPError as exc:
        unchanged = exc.code == 304
    except Exception:
        return False
    if unchanged:
        refreshed_value = hit.value
        if isinstance(hit.value, dict):
            refreshed_value = dict(hit.value)
            refreshed_value["validated_at"] = _utc_now_iso()
            refreshed_value["cache_state"] = "revalidated"
        persistent.set(
            "content", content_key, refreshed_value,
            ttl=policy.success_ttl, stale_ttl=policy.stale_ttl,
            etag=current_etag or hit.etag,
            last_modified=current_modified or hit.last_modified,
            content_hash=hit.content_hash,
        )
        return True
    return False


def _extract_pdf_content(url: str, max_chars: int, query: str,
                         persistent: FreshnessCache, content_key: str,
                         stale_text: Optional[str],
                         requested_url: Optional[str] = None) -> Optional[str]:
    """Download a bounded PDF and extract its text and embedded web links."""
    policy = classify_cache_policy(query, "pdf", url=url, content_type="application/pdf")
    max_bytes = int(os.environ.get("WEB_SEARCH_MAX_RESOURCE_BYTES", str(25 * 1024 * 1024)))
    try:
        ua = str(_FINGERPRINTS[0]["ua"]).replace("{major}", "126")
        request = urllib.request.Request(
            url,
            headers={"User-Agent": ua, "Accept": "application/pdf,*/*;q=0.8"},
        )
        with _guarded_urlopen(request, timeout=35) as response:
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared and declared > max_bytes:
                raise ValueError(f"PDF exceeds byte limit ({declared}>{max_bytes})")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"PDF exceeds byte limit ({max_bytes})")
            final_url = response.geturl()
            headers = {key.lower(): value for key, value in response.headers.items()}
        if not data.startswith(b"%PDF"):
            return None
        try:
            from pypdf import PdfReader
        except ImportError:
            return stale_text[:max_chars] if stale_text else None
        reader = PdfReader(io.BytesIO(data), strict=False)
        page_text = []
        failed_pages = 0
        hard_char_limit = int(os.environ.get("WEB_SEARCH_MAX_PDF_CHARS", "500000"))
        for page in reader.pages:
            try:
                text = (page.extract_text() or "").strip()
            except Exception:
                failed_pages += 1
                continue
            if text:
                page_text.append(text)
            if sum(len(part) for part in page_text) >= hard_char_limit:
                break
        content = "\n\n".join(page_text).strip()
        if not content:
            return stale_text[:max_chars] if stale_text else None
        embedded_urls = []
        for match in re.findall(r"https?://[^\s<>\]\[()]+", content):
            candidate = canonicalize_url(match.rstrip(".,;:"))
            if candidate and candidate not in embedded_urls:
                embedded_urls.append(candidate)
            if len(embedded_urls) >= 100:
                break
        links = [{"url": link, "kind": "link", "anchor": "PDF embedded URL",
                  "rel": [], "mime_type": ""} for link in embedded_urls]
        original_url = requested_url or url
        document = {
            "content": content,
            "requested_url": original_url,
            "final_url": canonicalize_url(final_url) or final_url,
            "canonical_url": canonicalize_url(final_url) or final_url,
            "title": "",
            "content_type": headers.get("content-type", "application/pdf"),
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "content_chars": len(content),
            "content_hash": hashlib.sha256(data).hexdigest(),
            "truncated": len(content) > max_chars or len(page_text) < len(reader.pages),
            "pages_extracted": len(page_text),
            "pages_total": len(reader.pages),
            "pages_failed": failed_pages,
            "links": links,
            "cache_state": "live",
            "discovered_at": _utc_now_iso(),
            "validated_at": _utc_now_iso(),
        }
        meta = dict(document)
        meta.pop("content", None)
        _extraction_metadata[normalize_url(url)] = meta
        _extraction_metadata[normalize_url(original_url)] = meta
        _extraction_metadata[normalize_url(final_url)] = meta
        persistent.set(
            "content", content_key, document,
            ttl=policy.success_ttl, stale_ttl=policy.stale_ttl,
            etag=headers.get("etag"), last_modified=headers.get("last-modified"),
            content_hash=document["content_hash"],
        )
        return content[:max_chars]
    except Exception:
        return stale_text[:max_chars] if stale_text else None

def _content_failure_ttl(policy) -> float:
    """Hold content-extraction failures longer than the generic policy so
    hostile or broken pages are not re-attempted by every review pass."""
    return max(policy.failure_ttl,
               float(os.environ.get("WEB_SEARCH_CONTENT_FAILURE_TTL", "1800")))

_deep_read_host_locks: Dict[str, threading.Lock] = {}
_deep_read_host_locks_guard = threading.Lock()

def _deep_read_serialized(url: str, max_chars: int, query: str) -> Optional[str]:
    """Deep-read one URL; reads run concurrently across hosts but are
    serialized per host so parallelism never raises per-site request rates."""
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    with _deep_read_host_locks_guard:
        lock = _deep_read_host_locks.setdefault(host, threading.Lock())
    with lock:
        return playwright_extract_content(url, max_chars=max_chars, query=query)

def _http_extract_content(url: str, max_chars: int = 50000, query: str = "") -> Optional[str]:
    """Browser-free deep read for WEB_SEARCH_SKIP_BROWSER mode: fetch over plain
    HTTP (SSRF-guarded via _guarded_urlopen) and extract with trafilatura,
    mirroring the Playwright path's extraction and cache write.  PDFs reuse the
    existing HTTP PDF extractor.  On any fetch/extract failure it returns stale
    cache or None WITHOUT caching a failure, so a sandbox run never suppresses a
    later browser-capable run that shares the same cache."""
    url = resolve_bing_redirect(url)
    if not url.startswith("http") or not _is_safe_fetch_url(url, resolve_dns=False):
        return None
    persistent = _get_persistent_cache()
    content_key = make_cache_key(normalize_url(url))
    is_pdf_url = urllib.parse.urlsplit(url).path.lower().endswith(".pdf")
    policy = classify_cache_policy(query, "pdf" if is_pdf_url else "auto", url=url)
    hit = persistent.get("content", content_key)
    stale_text = None
    if hit.hit and not hit.is_failure:
        val = hit.value
        if isinstance(val, dict):
            stale_text = val.get("content") or None
        elif isinstance(val, str):
            stale_text = val or None
        if stale_text and hit.fresh and not _force_fresh:
            if isinstance(val, dict):
                meta = dict(val); meta.pop("content", None)
                meta["cache_state"] = hit.state
                _extraction_metadata[normalize_url(url)] = meta
            return stale_text[:max_chars]
    if is_pdf_url:
        return _extract_pdf_content(url, max_chars, query, persistent, content_key, stale_text)
    try:
        ua = str(_FINGERPRINTS[0]["ua"]).replace("{major}", "126")
        request = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        max_bytes = int(os.environ.get("WEB_SEARCH_MAX_RESOURCE_BYTES", str(25 * 1024 * 1024)))
        with _guarded_urlopen(request, timeout=30) as response:
            ctype = (response.headers.get("Content-Type") or "").lower()
            final_url = response.geturl()
            raw = response.read(max_bytes + 1)[:max_bytes]
            headers = {k.lower(): v for k, v in response.headers.items()}
    except Exception:
        return stale_text[:max_chars] if stale_text else None
    if "application/pdf" in ctype or raw[:5] == b"%PDF-":
        return _extract_pdf_content(url, max_chars, query, persistent, content_key, stale_text)
    charset = "utf-8"
    m = re.search(r"charset=([\w.-]+)", ctype)
    if m:
        charset = m.group(1)
    try:
        html = raw.decode(charset, errors="replace")
    except (LookupError, TypeError):
        html = raw.decode("utf-8", errors="replace")
    if not html or len(html) < 200:
        return stale_text[:max_chars] if stale_text else None
    result = None
    try:
        import trafilatura
        extracted = trafilatura.extract(
            html, include_tables=True, include_links=False, include_images=False,
            no_fallback=False, favor_precision=False, favor_recall=True, target_language=None,
        )
        if extracted and len(extracted.strip()) > 100:
            lines = [ln.strip() for ln in extracted.split("\n")
                     if ln.strip() and len(ln.strip()) >= 10 and not is_ad_text(ln.strip())]
            result = "\n".join(lines)
    except Exception:
        pass
    if not result or len(result) < 100:
        return stale_text[:max_chars] if stale_text else None
    try:
        links = [
            {"url": link.url, "kind": link.kind, "anchor": link.anchor,
             "rel": list(link.rel), "mime_type": link.mime_type}
            for link in extract_html_links(html, final_url)
        ]
    except Exception:
        links = []
    canonical_link = next((l["url"] for l in links if l["kind"] == "canonical"), "")
    full_chars = len(result)
    hard_store_limit = max(max_chars, int(os.environ.get("WEB_SEARCH_MAX_STORED_CONTENT_CHARS", "500000")))
    stored_content = result[:hard_store_limit]
    document = {
        "content": stored_content,
        "requested_url": url,
        "final_url": canonicalize_url(final_url) or final_url,
        "canonical_url": canonical_link or canonicalize_url(final_url),
        "content_type": headers.get("content-type", "text/html"),
        "etag": headers.get("etag"),
        "last_modified": headers.get("last-modified"),
        "content_chars": full_chars,
        "content_hash": hashlib.sha256(result.encode("utf-8", errors="ignore")).hexdigest(),
        "truncated": full_chars > max_chars,
        "links": links,
        "cache_state": "live",
        "discovered_at": _utc_now_iso(),
        "validated_at": _utc_now_iso(),
    }
    meta = dict(document); meta.pop("content", None)
    _extraction_metadata[normalize_url(url)] = meta
    persistent.set("content", content_key, document,
                   ttl=policy.success_ttl, stale_ttl=policy.stale_ttl,
                   etag=document.get("etag"), last_modified=document.get("last_modified"),
                   content_hash=document["content_hash"])
    return stored_content[:max_chars]

def playwright_extract_content(url: str, max_chars: int = 50000,
                               query: str = "") -> Optional[str]:
    """Extract main content from a URL using Playwright + trafilatura (Readability algorithm).
    Falls back to manual selector-based extraction if trafilatura fails.
    Results are cached to avoid redundant fetches."""
    if _skip_browser():
        return _http_extract_content(url, max_chars=max_chars, query=query)
    url = resolve_bing_redirect(url)
    if not url.startswith("http") or not _is_safe_fetch_url(url, resolve_dns=False):
        return None

    persistent = _get_persistent_cache()
    content_key = make_cache_key(normalize_url(url))
    is_pdf_url = urllib.parse.urlsplit(url).path.lower().endswith(".pdf")
    policy = classify_cache_policy(query, "pdf" if is_pdf_url else "auto", url=url)
    persistent_hit = persistent.get("content", content_key)
    stale_text = None
    policy_fresh = False
    if persistent_hit.hit and not persistent_hit.is_failure:
        if isinstance(persistent_hit.value, dict):
            stale_text = persistent_hit.value.get("content") or None
            cached_content_type = persistent_hit.value.get("content_type", "")
            if cached_content_type:
                policy = classify_cache_policy(
                    query, "auto", url=url, content_type=cached_content_type
                )
                if "application/pdf" in cached_content_type.casefold():
                    is_pdf_url = True
            cached_meta = dict(persistent_hit.value)
            cached_meta.pop("content", None)
        elif isinstance(persistent_hit.value, str):
            stale_text = persistent_hit.value
            cached_meta = {}
        age = 0.0
        if persistent_hit.created_at is not None:
            age = max(0.0, _time.time() - float(persistent_hit.created_at))
        policy_fresh = bool(
            persistent_hit.fresh
            and (persistent_hit.created_at is None or age <= policy.success_ttl)
        )
        if isinstance(persistent_hit.value, dict):
            cached_meta["cache_state"] = (
                persistent_hit.state if policy_fresh else "stale-policy"
            )
            _extraction_metadata[normalize_url(url)] = cached_meta
        if policy_fresh and not _force_fresh:
            return stale_text[:max_chars] if stale_text else None
    elif persistent_hit.fresh and persistent_hit.is_failure and not _force_fresh:
        return None

    if stale_text and _revalidate_cached_resource(
        url, persistent_hit, persistent, content_key, policy
    ):
        metadata = _extraction_metadata.get(normalize_url(url), {})
        metadata["cache_state"] = "revalidated"
        metadata["validated_at"] = _utc_now_iso()
        _extraction_metadata[normalize_url(url)] = metadata
        return stale_text[:max_chars]

    if is_pdf_url:
        return _extract_pdf_content(url, max_chars, query, persistent, content_key, stale_text)

    navigation_url = url
    try:
        probe = urllib.request.Request(
            url,
            headers={"User-Agent": "web-search-skill/22.0", "Accept": "*/*"},
            method="HEAD",
        )
        with _guarded_urlopen(probe, timeout=6) as response:
            probe_final = response.geturl()
            probe_headers = {
                str(key).casefold(): value for key, value in response.headers.items()
            }
        probe_mime = probe_headers.get("content-type", "").casefold()
        probe_disposition = probe_headers.get("content-disposition", "").casefold()
        if "application/pdf" in probe_mime or ".pdf" in probe_disposition:
            return _extract_pdf_content(
                probe_final, max_chars, query, persistent, content_key, stale_text,
                requested_url=url,
            )
        if is_public_http_url(probe_final, allow_private=_allow_private_urls()):
            navigation_url = probe_final
    except Exception:
        pass

    host_pins = _pin_candidates_for_url(navigation_url)
    if not host_pins and not _allow_private_urls():
        return stale_text[:max_chars] if stale_text else None

    sync_playwright = _get_sync_playwright()
    if sync_playwright is None:
        return stale_text[:max_chars] if stale_text else None

    html = None
    fallback_text = ""
    final_url = navigation_url
    page_title = ""
    response_headers: Dict[str, str] = {}
    detected_pdf = False
    last_content_error = ""
    content_engine = f"content:{(urllib.parse.urlsplit(navigation_url).hostname or 'unknown').lower()}"
    for _attempt in range(min(2, len(_FINGERPRINTS))):
        try:
            with sync_playwright() as p:
                browser, context, used_fp = _new_stealth_browser(
                    p, engine=content_engine, host_pins=host_pins
                )
                try:
                    page = context.new_page()
                    response = page.goto(
                        navigation_url, timeout=25000, wait_until="domcontentloaded"
                    )
                    status = None
                    if response is not None:
                        try:
                            status = int(response.status)
                        except (TypeError, ValueError):
                            status = None
                    if status is not None and status >= 400:
                        last_content_error = f"content HTTP {status}"
                        if status in {403, 429} or status >= 500:
                            _cooldown_fingerprint(used_fp, content_engine)
                            continue
                        break
                    response_headers = {
                        str(key).lower(): value
                        for key, value in (dict(response.headers).items() if response else [])
                    }
                    mime = response_headers.get("content-type", "").casefold()
                    disposition = response_headers.get("content-disposition", "").casefold()
                    if "application/pdf" in mime or ".pdf" in disposition:
                        final_url = page.url
                        final_host = (urllib.parse.urlsplit(final_url).hostname or "").casefold()
                        if not _allow_private_urls() and final_host not in host_pins:
                            last_content_error = "blocked non-public PDF redirect"
                            break
                        detected_pdf = True
                        _mark_fingerprint_success(used_fp, content_engine)
                        break
                    try:
                        page.wait_for_load_state("networkidle", timeout=2200)
                    except Exception:
                        pass
                    page.wait_for_timeout(300)
                    if _is_captcha_page(page):
                        last_content_error = "content CAPTCHA detected"
                        _cooldown_fingerprint(used_fp, content_engine)
                        continue
                    html = page.content()
                    final_url = page.url
                    final_host = (urllib.parse.urlsplit(final_url).hostname or "").casefold()
                    if not _allow_private_urls() and final_host not in host_pins:
                        html = None
                        last_content_error = "blocked non-public final URL"
                        break
                    page_title = page.title()
                    try:
                        fallback_text = _element_text(page.query_selector("body"))
                    except Exception:
                        fallback_text = ""
                    _mark_fingerprint_success(used_fp, content_engine)
                    break
                finally:
                    browser.close()
        except Exception as exc:
            last_content_error = last_content_error or "content browser load failed"
            try:
                _cooldown_fingerprint(used_fp, content_engine, base_seconds=20)
            except (NameError, UnboundLocalError):
                pass
            if "timeout" in str(exc).casefold():
                break  # a fresh fingerprint rarely fixes a navigation timeout; retrying costs another full goto

    if detected_pdf:
        return _extract_pdf_content(
            final_url or url, max_chars, query, persistent, content_key, stale_text,
            requested_url=url,
        )

    if not html or len(html) < 200:
        if not stale_text and last_content_error:
            persistent.set_failure("content", content_key, last_content_error,
                                   ttl=_content_failure_ttl(policy))
        return stale_text[:max_chars] if stale_text else None

    result = None

    # P0: Try trafilatura first (Readability-style universal extraction)
    try:
        import trafilatura
        extracted = trafilatura.extract(
            html,
            include_tables=True,
            include_links=False,
            include_images=False,
            no_fallback=False,
            favor_precision=False,
            favor_recall=True,
            target_language=None,
        )
        if extracted and len(extracted.strip()) > 100:
            lines = [
                line.strip() for line in extracted.split("\n")
                if line.strip() and len(line.strip()) >= 10 and not is_ad_text(line.strip())
            ]
            result = "\n".join(lines)
    except Exception:
        pass

    # Reuse the already-rendered body rather than launching a second browser.
    if not result or len(result) < 100:
        lines = [
            line.strip() for line in fallback_text.split("\n")
            if line.strip() and len(line.strip()) >= 15 and not is_ad_text(line.strip())
        ]
        result = "\n".join(lines)

    if result:
        policy = classify_cache_policy(
            query, "auto", url=final_url,
            content_type=response_headers.get("content-type", ""),
        )
        discovered = extract_html_links(html, final_url)
        links = [
            {"url": link.url, "kind": link.kind, "anchor": link.anchor,
             "rel": list(link.rel), "mime_type": link.mime_type}
            for link in discovered
        ]
        canonical_link = next((link["url"] for link in links if link["kind"] == "canonical"), "")
        full_chars = len(result)
        hard_store_limit = max(
            max_chars,
            int(os.environ.get("WEB_SEARCH_MAX_STORED_CONTENT_CHARS", "500000")),
        )
        stored_content = result[:hard_store_limit]
        full_hash = hashlib.sha256(result.encode("utf-8", errors="ignore")).hexdigest()
        document = {
            "content": stored_content,
            "requested_url": url,
            "final_url": canonicalize_url(final_url) or final_url,
            "canonical_url": canonical_link or canonicalize_url(final_url),
            "title": page_title,
            "content_type": response_headers.get("content-type", "text/html"),
            "etag": response_headers.get("etag"),
            "last_modified": response_headers.get("last-modified"),
            "content_chars": full_chars,
            "content_hash": full_hash,
            "truncated": full_chars > max_chars,
            "storage_truncated": full_chars > hard_store_limit,
            "links": links,
            "cache_state": "live",
            "discovered_at": _utc_now_iso(),
            "validated_at": _utc_now_iso(),
        }
        meta = dict(document)
        meta.pop("content", None)
        _extraction_metadata[normalize_url(url)] = meta
        _cache_set(url, stored_content)
        persistent.set("content", content_key, document,
                       ttl=policy.success_ttl, stale_ttl=policy.stale_ttl,
                       etag=document.get("etag"), last_modified=document.get("last_modified"),
                       content_hash=document["content_hash"])
        return stored_content[:max_chars]
    if stale_text:
        return stale_text[:max_chars]
    persistent.set_failure("content", content_key, "content extraction returned empty",
                           ttl=_content_failure_ttl(policy))
    return None

def playwright_scrape_page(url: str, query: str) -> List[Dict]:
    content = playwright_extract_content(
        url,
        max_chars=int(os.environ.get("WEB_SEARCH_MAX_CONTENT_CHARS", "50000")),
        query=query,
    )
    if not content or len(content) < 100:
        return []
    metadata = get_extraction_metadata(url)
    title = metadata.get("title") or urllib.parse.urlsplit(url).netloc
    return [{
        "engine": "OfficialPage",
        "source": f"Official Page ({urllib.parse.urlparse(url).netloc})",
        "title": title,
        "url": metadata.get("final_url") or url,
        "canonical_url": metadata.get("canonical_url") or canonicalize_url(url),
        "content": content,
        "content_chars": metadata.get("content_chars", len(content)),
        "content_truncated": metadata.get("truncated", False),
        "type": "official_document",
        "relevance": relevance_score(query, title=title, text=content[:5000], url=url),
    }]

# ============================================================
# RESULT MERGING & DEDUP
# ============================================================

def normalize_title(title: str) -> str:
    """Normalize title for dedup comparison."""
    t = title.lower().strip()
    # Remove common suffixes like "- Wikipedia", "_百度百科" etc.
    t = re.sub(r'\s*[-|]\s*(wikipedia|百度百科|知乎|reddit|medium|github).*$', '', t)
    # Remove special chars
    t = re.sub(r'[^\w\s一-鿿]', '', t)
    return t.strip()

def normalize_url(url: str) -> str:
    """Build a cache/graph identity without merging distinct web origins."""
    canonical = canonicalize_url(url)
    if not canonical:
        return ""
    parsed = urllib.parse.urlsplit(canonical)
    path = parsed.path.rstrip("/") or "/"
    normalized = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{path}"
    if parsed.query:
        identity_query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        identity_query.sort(key=lambda item: (item[0].casefold(), item[1]))
        normalized += f"?{urllib.parse.urlencode(identity_query, doseq=True)}"
    return normalized

def merge_search_results(all_results: List[Dict], query: str = "") -> List[Dict]:
    """
    Merge results from multiple search engines with dedup.
    Priority: Google > Bing > Baidu (for same-content results).
    Keeps track of which engines found each result.
    """
    merged: List[Dict] = []
    url_index: Dict[str, int] = {}
    title_host_index: Dict[Tuple[str, str], int] = {}

    for raw in all_results:
        r = dict(raw)
        title = r.get("title", "").strip()
        url = r.get("url", "").strip()
        if not title or is_ad_url(url):
            continue
        spam_score, spam_reasons = score_spam_result(title, r.get("snippet", ""), url)
        if spam_score >= 0.9:
            _record_filter("high_confidence_seo_spam")
            continue
        r["spam_score"] = spam_score
        if spam_reasons:
            r["spam_reasons"] = spam_reasons

        canonical = (r.get("canonical_url") or r.get("final_url") or url).strip()
        title_key = normalize_title(title)
        url_key = normalize_url(canonical)
        try:
            host = (urllib.parse.urlsplit(canonical or url).hostname or "").lower()
        except ValueError:
            host = ""
        if host.startswith("www."):
            host = host[4:]
        row_type = str(r.get("type") or "organic")
        row_types = [str(value) for value in (r.get("types") or []) if value]
        if row_type not in row_types:
            row_types.append(row_type)
        r["types"] = row_types
        if query:
            r["relevance"] = relevance_score(
                query, title=title, snippet=r.get("snippet", ""), url=canonical or url
            )

        matched_index = url_index.get(url_key) if url_key else None
        if matched_index is None and not url_key and host:
            matched_index = title_host_index.get((title_key, host))
        if matched_index is None and not host and title_key:
            matched_index = title_host_index.get((title_key, ""))

        if matched_index is not None:
            existing = merged[matched_index]
            engines = existing.get("engines", [])
            engine = r.get("engine", "Unknown")
            if engine not in engines:
                engines.append(engine)
            existing["engines"] = engines
            existing["found_by"] = list(engines)
            if len(r.get("snippet", "")) > len(existing.get("snippet", "")):
                existing["snippet"] = r["snippet"]
            if len(r.get("content", "")) > len(existing.get("content", "")):
                existing["content"] = r["content"]
            if canonical and not existing.get("canonical_url"):
                existing["canonical_url"] = canonical
            if url.startswith("http") and (not existing.get("url", "").startswith("http") or
                                           "bing.com/ck/a" in existing.get("url", "") or
                                           "baidu.com/link" in existing.get("url", "")):
                existing["url"] = url
            existing["source"] = ", ".join(engines)
            ranks = existing.setdefault("channel_ranks", {})
            ranks[engine] = r.get("rank", len(ranks) + 1)

            types = existing.setdefault("types", [existing.get("type", "organic")])
            for value in row_types:
                if value not in types:
                    types.append(value)
            type_priority = {
                "organic": 0, "community": 1, "related": 1,
                "academic": 2, "code": 2, "official_document": 3,
            }
            if type_priority.get(row_type, 1) > type_priority.get(existing.get("type", "organic"), 1):
                existing["type"] = row_type

            incoming_metadata = r.get("metadata")
            if isinstance(incoming_metadata, dict) and incoming_metadata:
                metadata_by_engine = existing.setdefault("metadata_by_engine", {})
                metadata_by_engine[engine] = dict(incoming_metadata)
                existing_metadata = existing.setdefault("metadata", {})
                for key, value in incoming_metadata.items():
                    if existing_metadata.get(key) in (None, "", [], {}):
                        existing_metadata[key] = value
            for field in ("open_access_pdf", "final_url", "published_at",
                          "discovered_at", "validated_at"):
                if r.get(field) not in (None, "") and existing.get(field) in (None, ""):
                    existing[field] = r[field]
            if query:
                existing["relevance"] = max(
                    float(existing.get("relevance", 0) or 0),
                    float(r.get("relevance", 0) or 0),
                )
        else:
            engine = r.get("engine", "Unknown")
            r["engines"] = [engine]
            r["found_by"] = [engine]
            r["source"] = r.get("source") or engine
            r["channel_ranks"] = {engine: r.get("rank", 1)}
            if isinstance(r.get("metadata"), dict) and r["metadata"]:
                r["metadata_by_engine"] = {engine: dict(r["metadata"])}
            if canonical:
                r.setdefault("canonical_url", canonical)
            index = len(merged)
            merged.append(r)
            if url_key:
                url_index[url_key] = index
            title_host_index[(title_key, host)] = index

    def ranking_key(item: Dict):
        ranks = item.get("channel_ranks", {})
        rrf = sum(1.0 / (60 + max(1, int(rank or 1))) for rank in ranks.values())
        relevance = float(item.get("relevance", 0) or 0)
        return (-relevance, -len(item.get("engines", [])), -rrf,
                normalize_title(item.get("title", "")))

    return sorted(merged, key=ranking_key)

# ============================================================
# VENDOR REGISTRY
# ============================================================

VENDOR_REGISTRY = {
    "qwen": {"keywords": ["qwen", "通义千问", "qwq", "qvq"], "hf_author": "Qwen", "official_domains": ["qwen.ai", "aliyun.com"], "doc_patterns": ["https://help.aliyun.com/zh/model-studio/getting-started/models", "https://qwen.ai/blog"]},
    "meta": {"keywords": ["llama", "meta ai"], "hf_author": "meta-llama", "official_domains": ["ai.meta.com", "llama.com"], "doc_patterns": ["https://ai.meta.com/blog/"]},
    "google": {"keywords": ["gemma", "gemini", "palm"], "hf_author": "google", "official_domains": ["ai.google", "blog.google"], "doc_patterns": ["https://ai.google.dev/gemini-api/docs/models/gemini"], "known_models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-pro", "gemini-2.0-flash", "gemini-1.5-pro"]},
    "anthropic": {"keywords": ["claude", "anthropic", "fable", "mythos"], "official_domains": ["anthropic.com"], "doc_patterns": ["https://docs.anthropic.com/en/docs/about-claude/models"], "known_models": ["claude-opus-4", "claude-sonnet-4", "claude-haiku-4", "claude-fable-5", "claude-mythos-5", "claude-4-opus", "claude-4-sonnet", "claude-3.5-sonnet"]},
    "openai": {"keywords": ["gpt", "openai", "o1", "o3", "o4"], "official_domains": ["openai.com", "platform.openai.com"], "doc_patterns": [], "known_models": ["gpt-5", "gpt-5.6", "gpt-4o", "gpt-4-turbo", "o3", "o4-mini", "o1"]},
    "mistral": {"keywords": ["mistral", "mixtral"], "hf_author": "mistralai", "official_domains": ["mistral.ai"], "doc_patterns": ["https://mistral.ai/news/"]},
    "deepseek": {"keywords": ["deepseek"], "hf_author": "deepseek-ai", "official_domains": ["deepseek.com"], "doc_patterns": ["https://api-docs.deepseek.com/"], "known_models": ["deepseek-r2", "deepseek-r1", "deepseek-v3"]},
}

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def detect_vendor(q):
    ql = q.lower()
    for v, info in VENDOR_REGISTRY.items():
        if any(kw in ql for kw in info["keywords"]):
            return v
    return None

def extract_version(q):
    m = re.search(r'[\s\-_]?(v?\d+[\d.]*|[rv]\d+)', q, re.IGNORECASE)
    return m.group(1).lower() if m else None

def fuzzy_match(model, query, version):
    mc = model.lower().replace("-","").replace("_","").replace(" ","")
    qc = query.lower().replace("-","").replace("_","").replace(" ","")
    if qc in mc or mc in qc: return True
    if version:
        vc = version.lower().replace("-","").replace("_","").replace(".","")
        parts = re.split(r'[\d.]+|[rv]\d+', query.lower())
        for p in parts:
            pc = p.strip().replace("-","").replace("_","")
            if len(pc) > 1 and pc in mc and vc in mc: return True
    return False

def fetch_simple(url):
    try:
        return _fetch_bytes(url, headers={"User-Agent": "web-search-skill/22.0"},
                            timeout=15, retries=2).decode("utf-8", errors="ignore")
    except Exception:
        return None

def search_hf(author, query, limit=10):
    variants = list(set([query, query.replace(" ",""), re.sub(r'\s+','-',query)]))
    all_r = {}
    for v in variants:
        html = fetch_simple(f"https://huggingface.co/api/models?author={author}&search={urllib.parse.quote(v)}&sort=lastModified&direction=-1&limit={limit}")
        if not html: continue
        try:
            for m in json.loads(html):
                mid = m.get("id","")
                if mid.startswith(f"{author}/") and mid not in all_r:
                    all_r[mid] = {"source": f"HuggingFace Official ({author})", "title": mid, "date": m.get("lastModified","")[:10], "likes": m.get("likes",0), "downloads": m.get("downloads",0), "url": f"https://huggingface.co/{mid}", "type": "Open Source"}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return list(all_r.values())[:limit]

def assess_sufficiency(result: Dict, query: str) -> Tuple[bool, str]:
    categories = ["official_open_source", "official_commercial_api", "official_blog_docs",
                  "academic_results", "code_results", "community_results", "related_results"]
    rows = list(result.get("combined_results", []))
    if not rows:
        rows = [row for cat in categories for row in result.get(cat, [])]

    unique_rows = []
    seen_sources = set()
    for row in rows:
        url_key = normalize_url(row.get("canonical_url") or row.get("final_url") or row.get("url", ""))
        source_key = url_key or (normalize_title(row.get("title", "")), row.get("source", ""))
        if not source_key or source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        unique_rows.append(row)

    all_content = []
    seen_bodies = set()
    rich_sources = 0
    snippet_sources = 0
    domains = set()
    source_families = set()
    for row in unique_rows:
        url = row.get("canonical_url") or row.get("final_url") or row.get("url", "")
        domain = (urllib.parse.urlsplit(url).hostname or "").lower() if url else ""
        if domain:
            domains.add(domain)
        source_families.add(row.get("type") or row.get("engine") or row.get("source") or "unknown")
        content = (row.get("content") or "").strip()
        snippet = (row.get("snippet") or "").strip()
        if content:
            body_key = re.sub(r"\s+", " ", content).strip().lower()
            if body_key in seen_bodies:
                continue
            seen_bodies.add(body_key)
            all_content.append(content)
            if len(content) > 200:
                rich_sources += 1
        elif snippet:
            all_content.append(snippet)
            snippet_sources += 1

    total_text = " ".join(all_content).lower()
    total_chars = len(total_text)
    q_lower = query.lower()
    query_terms = [t for t in re.split(r'[\s\-_]+', q_lower) if len(t) > 2]
    query_coverage = sum(1 for t in query_terms if t in total_text) if query_terms else 1
    has_deep_content = rich_sources >= 2
    has_enough_text = total_chars > 1500
    has_query_coverage = query_coverage >= max(1, len(query_terms) * 0.6)
    has_multi_source = len(unique_rows) >= 3 and len(domains) >= 3
    has_content_diversity = len(seen_bodies) >= 2
    is_sufficient = (has_deep_content and has_enough_text and has_query_coverage and
                     has_multi_source and has_content_diversity)
    reasons = []
    if not has_deep_content: reasons.append(f"仅{rich_sources}个页面有深度内容(需≥2)")
    if not has_enough_text: reasons.append(f"总文本量{total_chars}字符(需≥1500)")
    if not has_query_coverage: reasons.append(f"查询关键词覆盖率{query_coverage}/{len(query_terms)}(需≥60%)")
    if not has_multi_source: reasons.append(f"独立来源/域名{len(unique_rows)}/{len(domains)}(均需≥3)")
    if not has_content_diversity: reasons.append(f"独立正文数量{len(seen_bodies)}(需≥2)")
    reason = "信息充分" if is_sufficient else "; ".join(reasons)
    return is_sufficient, reason

def generate_expansion_queries(query: str, vendor: Optional[str],
                               existing_results: Optional[List[Dict]] = None) -> List[str]:
    """Generate bounded, deduplicated, result-aware query directions."""
    official_domains = VENDOR_REGISTRY.get(vendor, {}).get("official_domains", []) if vendor else []
    planned = generate_query_expansions(
        query,
        existing_results=existing_results or [],
        quality_domains=official_domains,
        max_queries=int(os.environ.get("WEB_SEARCH_MAX_QUERY_EXPANSIONS", "24")),
    )
    priority = {
        "quality_domain": 0,
        "docs_zh": 1, "docs_en": 1,
        "filetype_pdf": 2,
        "official_zh": 3, "official_en": 3,
        "bilingual": 4,
        "exact": 5,
        "related_term": 7,
    }
    planned = sorted(
        enumerate(planned),
        key=lambda pair: (priority.get(pair[1].reason, 6), pair[0]),
    )
    return [item.query for _, item in planned]

# ============================================================
# MULTI-ENGINE SEARCH WITH MERGE
# ============================================================

# ============================================================
# MAIN SEARCH ORCHESTRATOR
# ============================================================


def _smart_search_impl(query: str, limit: int = 15, max_iterations: int = 3,
                       fresh: bool = False,
                       review_queries: Optional[List[str]] = None,
                       alt_queries: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    global _force_fresh
    _force_fresh = bool(fresh or os.environ.get("WEB_SEARCH_FRESH") == "1")
    with _filter_stats_lock:
        _filter_stats.clear()
    vendor = detect_vendor(query)
    result = {
        "query": query, "detected_vendor": vendor,
        "alt_queries": dict(alt_queries or {}),
        "official_open_source": [], "official_commercial_api": [],
        "official_blog_docs": [], "community_results": [],
        "academic_results": [],
        "code_results": [],
        "related_results": [],
        "resources": [],
        "related_links": [],
        "search_log": [],
        "engine_status_summary": {},
        "queries_tried": [],
        "query_plan": [],
        "model_review": {
            "requested_queries": [],
            "applied_queries": [],
        },
    }
    seen_review_queries = set()
    model_query_queue = []
    for value in review_queries or []:
        cleaned = re.sub(r"\s+", " ", str(value)).strip()
        folded = cleaned.casefold()
        if cleaned and folded not in seen_review_queries and folded != query.casefold():
            seen_review_queries.add(folded)
            model_query_queue.append(cleaned)
    result["model_review"]["requested_queries"] = list(model_query_queue)
    visited_urls = set()
    failed_urls: Dict[str, int] = {}
    total_fetch_attempts = 0
    all_queries_tried = set()
    original_query = query
    link_graph = LinkGraph(
        original_query,
        max_depth=int(os.environ.get("WEB_SEARCH_LINK_DEPTH", "2")),
        max_nodes=int(os.environ.get("WEB_SEARCH_LINK_MAX_NODES", "200")),
        max_edges=int(os.environ.get("WEB_SEARCH_LINK_MAX_EDGES", "500")),
        per_domain_limit=int(os.environ.get("WEB_SEARCH_LINKS_PER_DOMAIN", "20")),
        allow_private=_allow_private_urls(),
    )
    resource_urls = set()
    related_urls = set()

    def register_document(item: Dict[str, Any], source_url: str,
                          metadata: Dict[str, Any]) -> None:
        """Attach extraction metadata and feed its links into the bounded graph."""
        if not metadata:
            return
        for field in ["final_url", "canonical_url", "content_type", "content_hash",
                      "content_chars", "cache_state", "discovered_at", "validated_at"]:
            if metadata.get(field) not in (None, ""):
                item[field] = metadata[field]
        item["content_truncated"] = bool(metadata.get("truncated", False))
        canonical_source = canonicalize_url(source_url)
        seed = link_graph.nodes.get(canonical_source)
        if seed is None:
            seed = link_graph.add_seed(
                source_url,
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
            )
        if not seed:
            return
        link_graph.mark_visited(seed.url)
        links = metadata.get("links", [])
        link_graph.add_discovered_links(seed.url, links)
        for link in links:
            link_url = link.get("url", "")
            if not link_url:
                continue
            record = {
                "url": link_url,
                "kind": link.get("kind", "link"),
                "anchor": link.get("anchor", ""),
                "parent_url": metadata.get("canonical_url") or metadata.get("final_url") or source_url,
                "discovered_from": item.get("source") or item.get("engine") or "content",
                "relevance": relevance_score(
                    original_query,
                    anchor=link.get("anchor", ""),
                    url=link_url,
                ),
            }
            if record["kind"] in {"pdf", "attachment", "feed"}:
                key = normalize_url(link_url)
                if key not in resource_urls:
                    resource_urls.add(key)
                    result["resources"].append(record)
            else:
                key = normalize_url(link_url)
                if key not in related_urls:
                    related_urls.add(key)
                    result["related_links"].append(record)
    
    for iteration in range(max_iterations):
        log_entry = {"iteration": iteration + 1, "actions": [], "engines": {}}
        current_query = original_query if iteration == 0 else query
        
        # Phase 1: Official Sources (first iteration only)
        if iteration == 0 and vendor and vendor in VENDOR_REGISTRY:
            reg = VENDOR_REGISTRY[vendor]
            if "hf_author" in reg:
                try:
                    hf_res = search_hf(reg["hf_author"], original_query, limit)
                    result["official_open_source"].extend(hf_res)
                    log_entry["actions"].append(f"HF official: {len(hf_res)} results")
                except Exception as e:
                    log_entry["actions"].append(f"HF failed: {e}")
            if "doc_patterns" in reg:
                for doc_url in reg["doc_patterns"]:
                    try:
                        total_fetch_attempts += 1
                        doc_res = playwright_scrape_page(doc_url, original_query)
                        for r in doc_res:
                            target = result["official_commercial_api"] if "Commercial" in r.get("type","") else result["official_blog_docs"]
                            if not any(x["title"].lower() == r["title"].lower() for x in target):
                                target.append(r)
                            register_document(r, doc_url, get_extraction_metadata(doc_url))
                            visited_urls.add(normalize_url(doc_url))
                        log_entry["actions"].append(f"Scraped {doc_url}: {len(doc_res)} results")
                    except Exception as e:
                        log_entry["actions"].append(f"Scrape {doc_url} failed: {e}")

        # Phase 2: Multi-Engine Search (Google + Bing + Baidu → merge)
        if current_query not in all_queries_tried:
            all_queries_tried.add(current_query)
            result["queries_tried"].append(current_query)
            
            # Translations describe the base query; expansion/review rounds
            # arrive already written in the language of the gap they target.
            merged_results, engine_status = search_all_engines_extended(
                current_query, limit, vendor,
                alt_queries=alt_queries if current_query == original_query else None,
            )
            log_entry["engines"] = engine_status
            
            # Update global engine status
            for eng, status in engine_status.items():
                if eng not in result["engine_status_summary"]:
                    result["engine_status_summary"][eng] = []
                result["engine_status_summary"][eng].append(status)
            
            official_domains = VENDOR_REGISTRY.get(vendor, {}).get("official_domains", []) if vendor else []
            valid_count = 0
            specificity_tokens = {
                token for token in tokenize_query(original_query)
                if len(token) >= 3 and token not in {
                    "latest", "major", "release", "changes", "documentation",
                    "docs", "official", "guide", "tutorial", "version", "news",
                    "最新", "官方", "文档", "教程", "发布", "版本",
                }
            }
            vertical_link_threshold = float(
                os.environ.get("WEB_SEARCH_VERTICAL_LINK_MIN_RELEVANCE", "0.35")
            )
            for r in merged_results:
                if not isinstance(r, dict) or "title" not in r: continue
                if "error" in r: continue
                valid_count += 1
                url_str = r.get("url", "")
                r["relevance"] = relevance_score(
                    original_query,
                    title=r.get("title", ""),
                    snippet=r.get("snippet", ""),
                    url=url_str,
                )
                result_types = set(r.get("types") or [r.get("type", "organic")])
                should_expand_links = bool(
                    "organic" in result_types
                    or "official_document" in result_types
                    or float(r.get("relevance", 0) or 0) >= vertical_link_threshold
                )
                if url_str.startswith("http") and should_expand_links:
                    link_graph.add_seed(
                        url_str,
                        title=r.get("title", ""),
                        snippet=r.get("snippet", ""),
                        relevance=relevance_score(
                            original_query,
                            title=r.get("title", ""),
                            snippet=r.get("snippet", ""),
                            url=url_str,
                        ),
                    )
                open_pdf = r.get("open_access_pdf") or (r.get("metadata") or {}).get("open_access_pdf")
                if open_pdf and should_expand_links:
                    if url_str.startswith("http"):
                        link_graph.add_link(
                            url_str, open_pdf, reason="pdf", anchor=r.get("title", ""),
                            relevance=relevance_score(original_query, title=r.get("title", ""), url=open_pdf),
                        )
                    pdf_key = normalize_url(open_pdf)
                    if pdf_key and pdf_key not in resource_urls:
                        resource_urls.add(pdf_key)
                        result["resources"].append({
                            "url": open_pdf, "kind": "pdf", "anchor": r.get("title", ""),
                            "parent_url": url_str, "discovered_from": r.get("engine", "academic"),
                            "relevance": relevance_score(original_query, title=r.get("title", ""), url=open_pdf),
                        })
                try:
                    domain = urllib.parse.urlparse(url_str).netloc if url_str.startswith("http") else ""
                except ValueError:
                    domain = ""
                is_official = any(host_matches_domain(domain, d) for d in official_domains) if domain else False
                host_labels = set(re.findall(r"[a-z0-9]+", domain.casefold()))
                is_likely_primary = bool(
                    not is_official
                    and r.get("type", "organic") == "organic"
                    and min((int(value or 999) for value in r.get("channel_ranks", {}).values()), default=999) <= 5
                    and specificity_tokens.intersection(host_labels)
                )
                r["is_official"] = is_official
                r["is_likely_primary"] = is_likely_primary
                if is_likely_primary:
                    r["source_role"] = "likely_primary"
                if is_official:
                    r["source"] = f"{r.get('source','')} (Official: {domain})"
                    if not any(x["title"].lower() == r["title"].lower() for x in result["official_blog_docs"]):
                        result["official_blog_docs"].append(r)
                else:
                    result_types = set(r.get("types") or [r.get("type", "")])
                    if "academic" in result_types:
                        target = result["academic_results"]
                    elif "code" in result_types:
                        target = result["code_results"]
                    else:
                        target = result["community_results"]
                    if not any(normalize_url(x.get("url", "")) == normalize_url(r.get("url", ""))
                               for x in target):
                        target.append(r)
            
            working_engines = sum(1 for s in engine_status.values() if "✅" in s)
            log_entry["actions"].append(f"Multi-engine '{current_query}': {valid_count} merged results ({working_engines} engines working)")
        
        # Phase 3: Deep Content Extraction
        all_candidates = []
        for cat in ["official_blog_docs", "official_open_source", "official_commercial_api",
                    "academic_results", "code_results", "community_results"]:
            for r in result.get(cat, []):
                url = r.get("url", "")
                if (url and normalize_url(url) not in visited_urls and not r.get("content")
                        and url.startswith("http")):
                    row_types = set(r.get("types") or [r.get("type", "organic")])
                    if cat.startswith("official_") or r.get("is_official") or r.get("is_likely_primary"):
                        source_priority = 0
                    elif "organic" in row_types:
                        source_priority = 1
                    elif "code" in row_types or "community" in row_types:
                        source_priority = 2
                    elif "academic" in row_types:
                        source_priority = 3
                    else:
                        source_priority = 2
                    has_snip = 0 if r.get("snippet") else 1
                    # Prioritize results confirmed by multiple engines
                    engine_count = len(r.get("engines", []))
                    relevance = float(r.get("relevance", 0) or 0)
                    all_candidates.append((source_priority, -relevance, has_snip,
                                           -engine_count, cat, r))
        
        all_candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        
        pages_read = 0
        attempts_this_iter = 0
        attempted_this_iter = set()
        max_pages_per_iter = int(os.environ.get("WEB_SEARCH_DEEP_PAGES_PER_ROUND", "5"))
        max_attempts_per_iter = int(os.environ.get("WEB_SEARCH_DEEP_ATTEMPTS_PER_ROUND", "8"))
        max_content_chars = int(os.environ.get("WEB_SEARCH_MAX_CONTENT_CHARS", "50000"))
        deep_workers = max(1, int(os.environ.get("WEB_SEARCH_DEEP_WORKERS", "4")))

        # Reads run in waves of deep_workers; per-host locks in
        # _deep_read_serialized keep per-site request rates unchanged.
        candidate_rows = iter(all_candidates)
        with ThreadPoolExecutor(max_workers=deep_workers) as deep_pool:
            exhausted = False
            while not exhausted:
                batch_cap = min(deep_workers,
                                max_attempts_per_iter - attempts_this_iter,
                                max_pages_per_iter - pages_read)
                if batch_cap <= 0:
                    break
                batch = []
                while len(batch) < batch_cap:
                    try:
                        _, _, _, _, cat, r = next(candidate_rows)
                    except StopIteration:
                        exhausted = True
                        break
                    url = r.get("url", "")
                    url_key = normalize_url(url)
                    if (not url or url_key in visited_urls or url_key in attempted_this_iter or
                            failed_urls.get(url_key, 0) >= 2):
                        continue
                    attempted_this_iter.add(url_key)
                    attempts_this_iter += 1
                    total_fetch_attempts += 1
                    batch.append((url, url_key, r,
                                  deep_pool.submit(_deep_read_serialized, url,
                                                   max_content_chars, original_query)))
                for url, url_key, r, future in batch:
                    try:
                        content = future.result()
                        if content and len(content) > 100:
                            r["content"] = content
                            metadata = get_extraction_metadata(url)
                            r["content_chars"] = metadata.get("content_chars", len(content))
                            r["content_truncated"] = metadata.get("truncated", len(content) >= max_content_chars)
                            register_document(r, url, metadata)
                            visited_urls.add(url_key)
                            pages_read += 1
                            log_entry["actions"].append(f"Deep read: {url[:80]}... ({len(content)} chars)")
                        else:
                            failed_urls[url_key] = failed_urls.get(url_key, 0) + 1
                            log_entry["actions"].append(f"Deep read EMPTY: {url[:80]}...")
                    except Exception as e:
                        failed_urls[url_key] = failed_urls.get(url_key, 0) + 1
                        log_entry["actions"].append(f"Deep read FAILED: {url[:60]}... ({e})")

        # Follow the highest-value URLs discovered from the pages just read.
        remaining_attempts = max(0, max_attempts_per_iter - attempts_this_iter)
        remaining_successes = max(0, max_pages_per_iter - pages_read)
        if remaining_attempts and remaining_successes:
            graph_batch = link_graph.select_next_batch(limit=remaining_attempts)
            graph_nodes = iter(graph_batch)
            with ThreadPoolExecutor(max_workers=deep_workers) as deep_pool:
                exhausted = False
                while not exhausted:
                    batch_cap = min(deep_workers,
                                    max_attempts_per_iter - attempts_this_iter,
                                    max_pages_per_iter - pages_read)
                    if batch_cap <= 0:
                        for node in graph_nodes:
                            link_graph.release(node.url)
                        break
                    batch = []
                    while len(batch) < batch_cap:
                        try:
                            node = next(graph_nodes)
                        except StopIteration:
                            exhausted = True
                            break
                        node_key = normalize_url(node.url)
                        if (node_key in visited_urls or node_key in attempted_this_iter or
                                failed_urls.get(node_key, 0) >= 2):
                            link_graph.release(node.url)
                            continue
                        attempted_this_iter.add(node_key)
                        attempts_this_iter += 1
                        total_fetch_attempts += 1
                        batch.append((node, node_key,
                                      deep_pool.submit(_deep_read_serialized, node.url,
                                                       max_content_chars, original_query)))
                    for node, node_key, future in batch:
                        try:
                            content = future.result()
                            if not content or len(content) <= 100:
                                failed_urls[node_key] = failed_urls.get(node_key, 0) + 1
                                link_graph.release(node.url)
                                continue
                            metadata = get_extraction_metadata(node.url)
                            linked = {
                                "engine": "LinkGraph",
                                "source": "LinkGraph",
                                "found_by": ["LinkGraph"],
                                "engines": ["LinkGraph"],
                                "title": metadata.get("title") or node.title or node.url,
                                "url": node.url,
                                "snippet": node.snippet or "",
                                "content": content,
                                "content_chars": metadata.get("content_chars", len(content)),
                                "content_truncated": metadata.get("truncated", False),
                                "type": "related",
                                "depth": node.depth,
                                "discovery_reasons": sorted(node.reasons),
                                "relevance": node.relevance,
                            }
                            register_document(linked, node.url, metadata)
                            if not any(normalize_url(row.get("url", "")) == node_key
                                       for row in result["related_results"]):
                                result["related_results"].append(linked)
                            visited_urls.add(node_key)
                            pages_read += 1
                            log_entry["actions"].append(
                                f"Link follow d={node.depth}: {node.url[:75]}... ({len(content)} chars)"
                            )
                        except Exception as exc:
                            failed_urls[node_key] = failed_urls.get(node_key, 0) + 1
                            link_graph.release(node.url)
                            log_entry["actions"].append(
                                f"Link follow FAILED: {node.url[:60]}... ({exc})"
                            )
        
        # Phase 4: Assess Sufficiency
        is_sufficient, reason = assess_sufficiency(result, original_query)
        log_entry["assessment"] = reason
        log_entry["is_sufficient"] = is_sufficient
        result["search_log"].append(log_entry)
        
        minimum_rounds = min(max_iterations, int(os.environ.get("WEB_SEARCH_MIN_QUERY_ROUNDS", "2")))
        if is_sufficient and iteration + 1 >= minimum_rounds and not model_query_queue:
            break
        
        # Phase 5: Generate Expansion Queries
        if iteration < max_iterations - 1:
            observed_results = merge_search_results([
                row
                for category in ["official_open_source", "official_commercial_api",
                                 "official_blog_docs", "academic_results", "code_results",
                                 "community_results", "related_results"]
                for row in result.get(category, [])
            ], original_query)
            expansions = generate_expansion_queries(original_query, vendor, observed_results)
            reddit_statuses = result["engine_status_summary"].get("Reddit", [])
            if any("❌" in status for status in reddit_statuses):
                indexed_reddit = f"{original_query} site:reddit.com"
                expansions = [indexed_reddit] + [q for q in expansions if q.casefold() != indexed_reddit.casefold()]
            result["query_plan"] = expansions
            next_query = None
            selected_by_model = False
            while model_query_queue and next_query is None:
                candidate = model_query_queue.pop(0)
                if candidate not in all_queries_tried:
                    next_query = candidate
                    selected_by_model = True
            for eq in expansions:
                if next_query is None and eq not in all_queries_tried:
                    next_query = eq
                    break
            if next_query:
                if selected_by_model:
                    result["model_review"]["applied_queries"].append(next_query)
                query = next_query
            else:
                break
    
    result["total_pages_read"] = len(visited_urls)
    result["total_pages_attempted"] = total_fetch_attempts
    result["sufficient"] = assess_sufficiency(result, result["query"])[0]
    result["cache"] = _get_persistent_cache().stats()
    result["cache"]["force_fresh"] = _force_fresh
    result["link_graph"] = link_graph.to_dict()
    result["combined_results"] = merge_search_results([
        row
        for category in ["official_open_source", "official_commercial_api", "official_blog_docs",
                         "academic_results", "code_results", "community_results", "related_results"]
        for row in result.get(category, [])
    ], original_query)
    with _filter_stats_lock:
        filter_snapshot = dict(_filter_stats)
    # Ad/spam filter counters, NOT a content summary despite the name -- how
    # many results were dropped and why. For evidence to synthesize from, see
    # review_packet.top_evidence (or combined_results without --summary).
    result["filtered_summary"] = {
        "total": sum(filter_snapshot.values()),
        "reasons": dict(sorted(filter_snapshot.items())),
    }
    channel_summary = {}
    for channel, statuses in result["engine_status_summary"].items():
        channel_rows = [
            row for row in result["combined_results"]
            if channel in (row.get("found_by") or row.get("engines") or [row.get("engine")])
        ]
        cache_states: Dict[str, int] = {}
        for row in channel_rows:
            state = row.get("cache_state", "unknown")
            cache_states[state] = cache_states.get(state, 0) + 1
        channel_summary[channel] = {
            "statuses": statuses,
            "unique_results": len(channel_rows),
            "cache_states": cache_states,
        }
    result["channels"] = channel_summary
    coverage_domains = {
        (urllib.parse.urlsplit(row.get("canonical_url") or row.get("url", "")).hostname or "").lower()
        for row in result["combined_results"]
        if row.get("canonical_url") or row.get("url")
    }
    coverage_domains.discard("")
    result["coverage"] = {
        "queries": len(result["queries_tried"]),
        "channels": len(channel_summary),
        "unique_results": len(result["combined_results"]),
        "independent_domains": len(coverage_domains),
        "resources": len(result["resources"]),
        "related_links": len(result["related_links"]),
        "pages_read": len(visited_urls),
        "pages_attempted": total_fetch_attempts,
    }
    if not result["query_plan"]:
        result["query_plan"] = generate_expansion_queries(
            original_query, vendor, result["combined_results"]
        )
    result["review_packet"] = build_model_review_packet(result)
    
    return result


def smart_search(query: str, limit: int = 15, max_iterations: int = 3,
                 fresh: bool = False,
                 review_queries: Optional[List[str]] = None,
                 alt_queries: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run one isolated search session; calls serialize while channels stay concurrent."""
    global _force_fresh
    with _smart_search_lock:
        previous_fresh = _force_fresh
        try:
            return _smart_search_impl(
                query, limit, max_iterations, fresh=fresh,
                review_queries=review_queries, alt_queries=alt_queries,
            )
        finally:
            _force_fresh = previous_fresh


def build_model_review_packet(result: Dict[str, Any], top_k: int = 12) -> Dict[str, Any]:
    """Build a compact evidence/gap packet for an outer model judgment pass.

    The model proposes only search directions.  Retrieval, retries, filtering,
    caching, and provenance remain deterministic inside this module.
    """
    combined = result.get("combined_results") or []
    coverage = result.get("coverage") or {}
    failed_channels = []
    warning_channels = []
    for channel, statuses in (result.get("engine_status_summary") or {}).items():
        if any("❌" in status for status in statuses):
            failed_channels.append(channel)
        elif any("⚠️" in status for status in statuses):
            warning_channels.append(channel)

    type_counts: Dict[str, int] = {}
    corroborated = 0
    for row in combined:
        for value in set(row.get("types") or [row.get("type", "unknown")]):
            type_counts[value] = type_counts.get(value, 0) + 1
        if len(row.get("found_by") or row.get("engines") or []) >= 2:
            corroborated += 1

    gaps = []
    if failed_channels:
        gaps.append("unavailable_channels:" + ",".join(sorted(failed_channels)))
    if warning_channels:
        gaps.append("partial_channels:" + ",".join(sorted(warning_channels)))
    if int(coverage.get("independent_domains", 0)) < 3:
        gaps.append("fewer_than_3_independent_domains")
    if int(coverage.get("pages_read", 0)) < 2:
        gaps.append("fewer_than_2_deeply_read_pages")
    if corroborated < 2:
        gaps.append("weak_cross_channel_corroboration")
    if not result.get("resources"):
        gaps.append("no_primary_documents_or_attachments")
    if not type_counts.get("organic"):
        gaps.append("no_general_web_evidence")
    if not result.get("sufficient"):
        gaps.append("deterministic_sufficiency_not_met")

    rounds_tried = len(result.get("queries_tried") or [])
    fallback_hint = None
    if not result.get("sufficient"):
        if rounds_tried < 2:
            fallback_hint = (
                "Coverage does not meet the sufficiency bar yet (see gaps). Try 1-3 "
                "targeted --review-query rounds first (see suggested_queries / "
                "decision_contract) before falling back to other tools."
            )
        else:
            fallback_hint = (
                f"Coverage still does not meet the sufficiency bar after {rounds_tried} "
                "query round(s) (see gaps). If more review-query rounds are unlikely to "
                "close these gaps, supplement with other web-search, browsing, or "
                "domain-specific tools available in this environment instead of "
                "reporting the query as unanswerable."
            )

    evidence = []
    for row in combined[:max(1, int(top_k))]:
        metadata = row.get("metadata") or {}
        evidence.append({
            "title": row.get("title", ""),
            "url": row.get("canonical_url") or row.get("url", ""),
            "snippet": (row.get("snippet") or "")[:320],
            "types": row.get("types") or [row.get("type", "unknown")],
            "relevance": row.get("relevance", 0),
            "found_by": row.get("found_by") or row.get("engines") or [],
            "cache_state": row.get("cache_state", "unknown"),
            "published": metadata.get("published") or metadata.get("publication_year")
                         or metadata.get("year") or row.get("published_at"),
            "discovered_at": row.get("discovered_at"),
            "validated_at": row.get("validated_at"),
        })

    return {
        "query": result.get("query", ""),
        "sufficient": bool(result.get("sufficient")),
        "coverage": coverage,
        "type_counts": dict(sorted(type_counts.items())),
        "corroborated_results": corroborated,
        "failed_channels": sorted(failed_channels),
        "warning_channels": sorted(warning_channels),
        "gaps": gaps,
        "fallback_hint": fallback_hint,
        "top_evidence": evidence,
        "suggested_queries": (result.get("query_plan") or [])[:8],
        "decision_contract": {
            "stop": "true only when evidence is current, diverse, corroborated, and answers the query",
            "queries": "0-3 non-duplicate targeted queries for the next all-channel round",
            "focus": "missing entities, dates, source types, contradictions, or citation links",
            "reason": "brief evidence-based rationale",
        },
    }


def compact_search_output(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return the small view intended for intermediate model review rounds."""
    cache_stats = result.get("cache") or {}
    return {
        "query": result.get("query"),
        "detected_vendor": result.get("detected_vendor"),
        "alt_queries": result.get("alt_queries", {}),
        "sufficient": result.get("sufficient"),
        "queries_tried": result.get("queries_tried", []),
        "model_review": result.get("model_review", {}),
        "coverage": result.get("coverage", {}),
        "channels": result.get("channels", {}),
        "filtered_summary": result.get("filtered_summary", {}),
        # Just degraded/last_error, not the full result["cache"] stats blob: if
        # SQLite lock contention forced a fallback to an in-memory cache for
        # this process, --summary output would otherwise give no indication.
        "cache": {"degraded": cache_stats.get("degraded", False),
                  "last_error": cache_stats.get("last_error")},
        "review_packet": result.get("review_packet", {}),
        "resources": (result.get("resources") or [])[:20],
        "search_log": result.get("search_log", []),
    }




# ============================================================
# ACADEMIC / CODE / COMMUNITY SEARCH ENGINES (API-based)
# ============================================================

_semantic_scholar_cooldown_until = 0.0

def search_semantic_scholar(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    global _semantic_scholar_cooldown_until
    if _time.time() < _semantic_scholar_cooldown_until:
        return [], "SemanticScholar skipped: rate-limited earlier this session (429 cooldown)"
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={urllib.parse.quote(query)}&limit={limit}&fields=title,abstract,year,citationCount,url,externalIds,openAccessPdf"
    headers = {"User-Agent": "web-search-skill/22.0"}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    try:
        data = _fetch_json(url, headers=headers, timeout=15,
                           retries=2 if api_key else 1,
                           max_retry_delay=None if api_key else 8.0)
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            _semantic_scholar_cooldown_until = _time.time() + 600
        return [], f"SemanticScholar failed: {exc}"

    results = []
    malformed = 0
    for paper in (data.get("data") or []):
        try:
            if not isinstance(paper, dict):
                raise TypeError("paper is not an object")
            title = (paper.get("title") or "").strip()
            if not title:
                continue
            abstract = (paper.get("abstract") or "").strip()
            open_access = paper.get("openAccessPdf") or {}
            if not isinstance(open_access, dict):
                open_access = {}
            results.append({
                "engine": "SemanticScholar", "title": title,
                "url": paper.get("url") or "", "snippet": abstract[:500],
                "type": "academic", "rank": len(results) + 1,
                "metadata": {
                    "year": paper.get("year"),
                    "citations": paper.get("citationCount", 0),
                    "external_ids": paper.get("externalIds") or {},
                    "open_access_pdf": open_access.get("url"),
                },
            })
            if len(results) >= limit:
                break
        except Exception:
            malformed += 1
    warning = f"SemanticScholar skipped {malformed} malformed records" if malformed else None
    return results, warning


def search_crossref(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search Crossref's public works API for DOI-backed scholarly records."""
    contact = os.environ.get("WEB_SEARCH_CONTACT", "").strip()
    params = {
        "query.bibliographic": query,
        "rows": min(max(1, int(limit)), 100),
        "select": "DOI,title,abstract,published,URL,type,author,is-referenced-by-count",
    }
    if contact and "@" in contact:
        params["mailto"] = contact
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": f"web-search-skill/22.0 (mailto:{contact or 'unset'})",
                     "Accept": "application/json"},
        )
        data = _fetch_json(url, headers=dict(request.header_items()), timeout=20, retries=2)
    except Exception as exc:
        return [], f"Crossref failed: {exc}"
    results = []
    malformed = 0
    for work in (data.get("message", {}).get("items") or []):
        try:
            if not isinstance(work, dict):
                raise TypeError("work is not an object")
            titles = work.get("title") or []
            title = (titles[0] if titles else "").strip()
            if not title:
                continue
            abstract = re.sub(r"<[^>]+>", " ", work.get("abstract") or "")
            abstract = re.sub(r"\s+", " ", abstract).strip()
            date_parts = (work.get("published") or {}).get("date-parts") or []
            published = "-".join(str(value) for value in date_parts[0]) if date_parts else ""
            doi = work.get("DOI") or ""
            work_url = work.get("URL") or (f"https://doi.org/{doi}" if doi else "")
            results.append({
                "engine": "Crossref", "title": title, "url": work_url,
                "snippet": abstract[:500], "type": "academic", "rank": len(results) + 1,
                "metadata": {"doi": doi, "published": published,
                             "citations": work.get("is-referenced-by-count", 0),
                             "work_type": work.get("type", "")},
            })
        except Exception:
            malformed += 1
    warning = f"Crossref skipped {malformed} malformed records" if malformed else None
    return results, warning


def search_openalex(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search OpenAlex works; an API key is optional but increases allowance."""
    params = {"search": query, "per-page": min(max(1, int(limit)), 100)}
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    contact = os.environ.get("WEB_SEARCH_CONTACT", "").strip()
    if api_key:
        params["api_key"] = api_key
    if contact and "@" in contact:
        params["mailto"] = contact
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": f"web-search-skill/22.0 (mailto:{contact or 'unset'})",
                     "Accept": "application/json"},
        )
        data = _fetch_json(url, headers=dict(request.header_items()), timeout=20, retries=2)
    except Exception as exc:
        return [], f"OpenAlex failed: {exc}"
    results = []
    malformed = 0
    for work in (data.get("results") or []):
        try:
            if not isinstance(work, dict):
                raise TypeError("work is not an object")
            title = (work.get("display_name") or work.get("title") or "").strip()
            if not title:
                continue
            authors = [
                entry.get("author", {}).get("display_name", "")
                for entry in (work.get("authorships") or [])[:5]
                if entry.get("author", {}).get("display_name")
            ]
            primary = work.get("primary_location") or {}
            best_oa = work.get("best_oa_location") or {}
            doi = work.get("doi") or ""
            work_url = primary.get("landing_page_url") or doi or work.get("id") or ""
            source_name = (primary.get("source") or {}).get("display_name", "")
            snippet_parts = []
            if authors:
                snippet_parts.append(", ".join(authors))
            if source_name:
                snippet_parts.append(source_name)
            if work.get("publication_year"):
                snippet_parts.append(str(work["publication_year"]))
            results.append({
                "engine": "OpenAlex", "title": title, "url": work_url,
                "snippet": " | ".join(snippet_parts)[:500], "type": "academic",
                "rank": len(results) + 1,
                "metadata": {"doi": doi, "publication_year": work.get("publication_year"),
                             "citations": work.get("cited_by_count", 0),
                             "open_access_pdf": best_oa.get("pdf_url")},
            })
        except Exception:
            malformed += 1
    warning = f"OpenAlex skipped {malformed} malformed records" if malformed else None
    return results, warning

def search_arxiv(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search arXiv API for preprints with high-specificity relevance filtering."""
    url = f"https://export.arxiv.org/api/query?search_query=all:{urllib.parse.quote(query)}&start=0&max_results={limit}&sortBy=submittedDate&sortOrder=descending"
    try:
        xml_data = _fetch_bytes(
            url, headers={"User-Agent": "web-search-skill/22.0"},
            timeout=20, retries=2,
        ).decode("utf-8", errors="replace")
        results = []
        entries = re.findall(r'<entry>(.*?)</entry>', xml_data, re.DOTALL)
        # Build high-specificity terms for filtering
        all_terms = [t.lower() for t in re.split(r'[\s\-_]+', query) if len(t) > 2]
        low_spec = {"benchmark", "benchmarks", "comparison", "comparisons", "vs", "versus",
                    "review", "evaluation", "analysis", "performance", "latest", "news",
                    "release", "model", "models", "paper", "research", "study"}
        high_spec = [t for t in all_terms if t not in low_spec]
        for entry in entries:
            title_m = re.search(r'<title>(.*?)</title>', entry, re.DOTALL)
            summary_m = re.search(r'<summary>(.*?)</summary>', entry, re.DOTALL)
            published_m = re.search(r'<published>(.*?)</published>', entry)
            link_m = re.search(r'<id>(.*?)</id>', entry)
            if not title_m:
                continue
            title = re.sub(r'\s+', ' ', title_m.group(1)).strip()
            summary = re.sub(r'\s+', ' ', summary_m.group(1)).strip()[:300] if summary_m else ""
            published = published_m.group(1)[:10] if published_m else ""
            link = link_m.group(1).strip() if link_m else ""
            # Relevance filter: require at least one high-specificity term match
            combined_text = (title + " " + summary).lower()
            if high_spec:
                is_relevant = any(qt in combined_text for qt in high_spec)
            else:
                is_relevant = any(qt in combined_text for qt in all_terms) if all_terms else True
            if is_relevant:
                results.append({"engine": "arXiv", "title": f"[{published}] {title}" if published else title, "url": link, "snippet": summary, "type": "academic", "rank": len(results) + 1})
        return results, None
    except Exception as e:
        return [], f"arXiv failed: {e}"

def search_wikipedia(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search Wikipedia via MediaWiki's public search API, routing CJK queries to the Chinese wiki."""
    lang = "zh" if _CJK_CHAR_PATTERN.search(query) else "en"
    params = {
        "action": "query", "list": "search", "format": "json",
        "srsearch": query, "srlimit": min(max(1, int(limit)), 50),
        "srprop": "snippet|timestamp",
    }
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    contact = os.environ.get("WEB_SEARCH_CONTACT", "").strip()
    try:
        # Wikimedia's robot policy (https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy)
        # 403s generic User-Agents under load; identifying a contact keeps this
        # channel in their good-citizen bucket instead of the throttled default.
        data = _fetch_json(
            url, headers={"User-Agent": f"web-search-skill/22.0 (mailto:{contact or 'unset'})",
                          "Accept": "application/json"},
            timeout=15, retries=2,
        )
    except Exception as exc:
        return [], f"Wikipedia failed: {exc}"
    results = []
    malformed = 0
    for page in (data.get("query", {}).get("search") or []):
        try:
            if not isinstance(page, dict):
                raise TypeError("page is not an object")
            title = (page.get("title") or "").strip()
            if not title:
                continue
            snippet = re.sub(r"<[^>]+>", "", page.get("snippet") or "")
            snippet = re.sub(r"\s+", " ", snippet).strip()
            page_url = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            results.append({
                "engine": "Wikipedia", "title": title, "url": page_url,
                "snippet": snippet[:500], "type": "reference", "rank": len(results) + 1,
                "metadata": {"lang": lang, "last_edited": page.get("timestamp", "")},
            })
        except Exception:
            malformed += 1
    warning = f"Wikipedia skipped {malformed} malformed records" if malformed else None
    return results, warning


def search_dblp(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search DBLP's computer science bibliography API."""
    params = {"q": query, "format": "json", "h": min(max(1, int(limit)), 100)}
    url = "https://dblp.org/search/publ/api?" + urllib.parse.urlencode(params)
    try:
        data = _fetch_json(
            url, headers={"User-Agent": "web-search-skill/22.0", "Accept": "application/json"},
            timeout=15, retries=2,
        )
    except Exception as exc:
        return [], f"DBLP failed: {exc}"
    results = []
    malformed = 0
    hits = ((data.get("result") or {}).get("hits") or {}).get("hit") or []
    if isinstance(hits, dict):  # DBLP omits the list wrapper for a single hit
        hits = [hits]
    for hit in hits:
        try:
            info = hit.get("info") if isinstance(hit, dict) else None
            if not isinstance(info, dict):
                raise TypeError("hit.info is not an object")
            title = (info.get("title") or "").strip().rstrip(".")
            if not title:
                continue
            authors_field = (info.get("authors") or {}).get("author") or []
            if isinstance(authors_field, dict):  # same single-vs-list quirk as hits
                authors_field = [authors_field]
            authors = [a.get("text", "") for a in authors_field if isinstance(a, dict) and a.get("text")]
            venue = info.get("venue") or ""
            year = info.get("year") or ""
            work_url = info.get("ee") or info.get("url") or ""
            snippet_parts = []
            if authors:
                snippet_parts.append(", ".join(authors[:5]))
            if venue:
                snippet_parts.append(str(venue))
            if year:
                snippet_parts.append(str(year))
            results.append({
                "engine": "DBLP", "title": title, "url": work_url,
                "snippet": " | ".join(snippet_parts)[:500], "type": "academic",
                "rank": len(results) + 1,
                "metadata": {"venue": venue, "year": year, "pub_type": info.get("type", "")},
            })
        except Exception:
            malformed += 1
    warning = f"DBLP skipped {malformed} malformed records" if malformed else None
    return results, warning


def search_pubmed(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search PubMed via NCBI E-utilities (esearch for PMIDs, esummary for metadata)."""
    contact = os.environ.get("WEB_SEARCH_CONTACT", "").strip()
    api_key = os.environ.get("NCBI_API_KEY", "").strip()
    limit = min(max(1, int(limit)), 50)
    common = {"tool": "web-search-skill"}
    if contact and "@" in contact:
        common["email"] = contact
    if api_key:
        common["api_key"] = api_key
    headers = {"User-Agent": "web-search-skill/22.0", "Accept": "application/json"}
    search_params = {"db": "pubmed", "term": query, "retmode": "json", "retmax": limit, **common}
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(search_params)
    try:
        search_data = _fetch_json(search_url, headers=headers, timeout=15, retries=2)
    except Exception as exc:
        return [], f"PubMed esearch failed: {exc}"
    pmids = [pmid for pmid in (search_data.get("esearchresult") or {}).get("idlist") or [] if pmid]
    if not pmids:
        return [], None
    summary_params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "json", **common}
    summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urllib.parse.urlencode(summary_params)
    try:
        summary_data = _fetch_json(summary_url, headers=headers, timeout=15, retries=2)
    except Exception as exc:
        return [], f"PubMed esummary failed: {exc}"
    results = []
    malformed = 0
    summary_result = summary_data.get("result") or {}
    for uid in summary_result.get("uids") or []:
        try:
            doc = summary_result.get(uid)
            if not isinstance(doc, dict):
                raise TypeError("docsum is not an object")
            title = (doc.get("title") or "").strip().rstrip(".")
            if not title:
                continue
            authors = [a.get("name", "") for a in (doc.get("authors") or []) if isinstance(a, dict) and a.get("name")]
            journal = doc.get("fulljournalname") or doc.get("source") or ""
            pubdate = doc.get("pubdate") or ""
            doi = next(
                (aid.get("value", "") for aid in (doc.get("articleids") or [])
                 if isinstance(aid, dict) and aid.get("idtype") == "doi"),
                "",
            )
            snippet_parts = []
            if authors:
                snippet_parts.append(", ".join(authors[:5]))
            if journal:
                snippet_parts.append(str(journal))
            if pubdate:
                snippet_parts.append(str(pubdate))
            results.append({
                "engine": "PubMed", "title": title,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                "snippet": " | ".join(snippet_parts)[:500], "type": "academic",
                "rank": len(results) + 1,
                "metadata": {"doi": doi, "pubdate": pubdate, "journal": journal},
            })
        except Exception:
            malformed += 1
    warning = f"PubMed skipped {malformed} malformed records" if malformed else None
    return results, warning


def search_github(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search GitHub API for repositories."""
    url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page={limit}"
    try:
        headers = {
            "User-Agent": "WebSearchSkill/18.0",
            "Accept": "application/vnd.github.v3+json"
        }
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        data = _fetch_json(url, headers=headers, timeout=15, retries=2)
    except Exception as e:
        return [], f"GitHub failed: {e}"
    results = []
    malformed = 0
    for repo in (data.get("items") or []):
        try:
            if not isinstance(repo, dict):
                raise TypeError("repository is not an object")
            name = repo.get("full_name", "")
            desc = repo.get("description", "") or ""
            stars = repo.get("stargazers_count", 0)
            lang = repo.get("language", "") or ""
            updated = repo.get("updated_at", "")[:10]
            html_url = repo.get("html_url", "")
            if not name:
                continue
            snippet_parts = []
            if desc:
                snippet_parts.append(desc[:200])
            snippet_parts.append(f"⭐{stars} | {lang} | Updated: {updated}")
            results.append({
                "engine": "GitHub",
                "title": name,
                "url": html_url,
                "snippet": " | ".join(snippet_parts),
                "type": "code",
                "rank": len(results) + 1,
                "metadata": {"stars": stars, "language": lang, "updated": updated}
            })
        except Exception:
            malformed += 1
    warning = f"GitHub skipped {malformed} malformed records" if malformed else None
    return results, warning

def search_hackernews(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search Hacker News Algolia API for discussions."""
    url = f"https://hn.algolia.com/api/v1/search?query={urllib.parse.quote(query)}&tags=story&hitsPerPage={limit}"
    try:
        data = _fetch_json(url, headers={"User-Agent": "web-search-skill/22.0"},
                           timeout=15, retries=2)
    except Exception as e:
        return [], f"HackerNews failed: {e}"
    results = []
    malformed = 0
    for hit in (data.get("hits") or []):
        try:
            if not isinstance(hit, dict):
                raise TypeError("hit is not an object")
            title = hit.get("title", "")
            if not title:
                continue
            points = hit.get("points", 0)
            comments = hit.get("num_comments", 0)
            created = hit.get("created_at", "")[:10]
            story_url = hit.get("url", "") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            hn_url = f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            results.append({
                "engine": "HackerNews",
                "title": f"[{created}] {title}",
                "url": story_url,
                "snippet": f"⬆️{points} points | 💬{comments} comments | HN discussion: {hn_url}",
                "type": "community",
                "rank": len(results) + 1,
                "metadata": {"points": points, "comments": comments, "hn_url": hn_url}
            })
        except Exception:
            malformed += 1
    warning = f"HackerNews skipped {malformed} malformed records" if malformed else None
    return results, warning

def search_reddit(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Search Reddit through its JSON endpoints, with optional bearer auth."""
    params = urllib.parse.urlencode({"q": query, "sort": "relevance", "t": "year", "limit": limit, "raw_json": 1})
    token = os.environ.get("REDDIT_ACCESS_TOKEN", "").strip()
    endpoints = [f"https://oauth.reddit.com/search?{params}"] if token else [
        f"https://www.reddit.com/search.json?{params}",
        f"https://old.reddit.com/search.json?{params}",
    ]
    contact = os.environ.get("WEB_SEARCH_CONTACT", "unset-contact")
    headers = {
        "User-Agent": os.environ.get(
            "WEB_SEARCH_USER_AGENT", f"web-search-skill/22.0 (contact: {contact})"
        ),
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    errors = []
    for url in endpoints:
        try:
            data = _fetch_json(url, headers=headers, timeout=15, retries=1)
            results = []
            malformed = 0
            for child in (data.get("data", {}).get("children") or []):
                try:
                    if not isinstance(child, dict) or not isinstance(child.get("data"), dict):
                        raise TypeError("post is not an object")
                    post = child["data"]
                    title = post.get("title", "")
                    if not title:
                        continue
                    subreddit = post.get("subreddit", "")
                    score = post.get("score", 0)
                    comments = post.get("num_comments", 0)
                    selftext = post.get("selftext", "") or ""
                    permalink = f"https://www.reddit.com{post.get('permalink', '')}"
                    snippet = selftext[:500] if selftext else f"r/{subreddit} | ⬆️{score} | 💬{comments}"
                    results.append({
                        "engine": "Reddit", "title": f"[r/{subreddit}] {title}",
                        "url": permalink, "snippet": snippet, "type": "community",
                        "rank": len(results) + 1,
                        "metadata": {"subreddit": subreddit, "score": score, "comments": comments},
                    })
                except Exception:
                    malformed += 1
            warning = f"Reddit skipped {malformed} malformed records" if malformed else None
            return results, warning
        except Exception as exc:
            errors.append(f"{urllib.parse.urlsplit(url).hostname}: {exc}")
    return [], "Reddit failed: " + " | ".join(errors)

_SOGOU_LINK_JS_PATTERN = re.compile(r'window\.location\.replace\("([^"]+)"\)')
_SOGOU_LINK_META_PATTERN = re.compile(r'content="0;URL=\'([^\']+)\'"', re.IGNORECASE)

def _resolve_sogou_link(url: str) -> str:
    """Sogou wraps result URLs in /link?url=... pages that redirect via JS/meta."""
    if "/link?url=" not in url:
        return url
    try:
        payload = _fetch_bytes(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=8, max_bytes=65536, retries=0,
        ).decode("utf-8", errors="ignore")
    except Exception:
        return url
    match = _SOGOU_LINK_JS_PATTERN.search(payload) or _SOGOU_LINK_META_PATTERN.search(payload)
    if match:
        target = match.group(1)
        if target.startswith("http") and is_public_http_url(
            target, allow_private=_allow_private_urls()
        ):
            return target
    return url

_zhihu_captcha_cooldown_until = 0.0

def search_zhihu(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Zhihu coverage via Sogou's ``insite=zhihu.com`` vertical.

    Zhihu's own search requires login signatures (x-zse-96), and Bing/Google
    treat ``site:`` operator queries as bot signals; Sogou's public Zhihu
    vertical is the least hostile path and also spreads browser channels
    across four distinct hosts.
    """
    if _skip_browser():
        return [], "Zhihu skipped: WEB_SEARCH_SKIP_BROWSER set (browser channels disabled)"
    global _zhihu_captcha_cooldown_until
    if _time.time() < _zhihu_captcha_cooldown_until:
        return [], "Zhihu (Sogou) skipped: CAPTCHA cooldown from earlier this session"
    sync_playwright = _get_sync_playwright()
    if sync_playwright is None:
        return [], "Playwright not available"
    host_pins = _pin_candidates_for_url("https://www.sogou.com/")
    if not host_pins and not _allow_private_urls():
        return [], "Zhihu (Sogou) host did not resolve to a verified address"
    last_error = None
    for _attempt in range(min(2, len(_FINGERPRINTS))):
        results = []
        seen_titles = set()
        used_fp = None
        with sync_playwright() as p:
            try:
                browser, context, used_fp = _new_stealth_browser(
                    p, engine="sogou", host_pins=host_pins
                )
            except Exception as exc:
                last_error = f"Zhihu (Sogou) browser setup failed: {exc}"
                continue
            try:
                page = context.new_page()
                url = ("https://www.sogou.com/sogou?insite=zhihu.com&query="
                       + urllib.parse.quote(query))
                try:
                    page.wait_for_timeout(random.randint(400, 1200))
                    response = page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector(".results .vrwrap, .results .rb", timeout=5000)
                        page.wait_for_timeout(400)
                    except Exception:
                        page.wait_for_timeout(1500)
                    if response and response.status in {403, 429, 503}:
                        last_error = f"Zhihu (Sogou) HTTP {response.status}"
                        _cooldown_fingerprint(used_fp, "sogou")
                        continue
                except Exception as e:
                    last_error = f"Zhihu (Sogou) load failed: {e}"
                    _cooldown_fingerprint(used_fp, "sogou")
                    continue
                cap_kind = _captcha_type(page)
                if "antispider" in (page.url or "").lower() or cap_kind:
                    kind = cap_kind or "antispider"
                    last_error = f"Zhihu (Sogou) CAPTCHA detected [{kind}]"
                    _cooldown_fingerprint(used_fp, "sogou",
                                          base_seconds=_CAPTCHA_COOLDOWN_BASE.get(cap_kind or "generic", 45))
                    continue
                _humanize_page(page, _FINGERPRINTS[used_fp], _attempt)
                for block in page.query_selector_all(".results .vrwrap, .results .rb"):
                    try:
                        title_el = block.query_selector("h3 a")
                        if not title_el:
                            continue
                        title = _element_text(title_el)
                        href = title_el.get_attribute("href") or ""
                        if href.startswith("/"):
                            href = "https://www.sogou.com" + href
                        if not title or len(title) < 5 or not href.startswith("http"):
                            continue
                        snippet_el = block.query_selector(
                            ".str-text-info, .ft, .str_info, p"
                        )
                        snippet = _element_text(snippet_el)[:500]
                        if is_ad_text(title + " " + snippet):
                            continue
                        title_key = title.lower().strip()
                        if title_key in seen_titles:
                            continue
                        seen_titles.add(title_key)
                        real_url = _resolve_sogou_link(href)
                        host = (urllib.parse.urlsplit(real_url).hostname or "").casefold()
                        if not (host.endswith("zhihu.com") or host.endswith("sogou.com")):
                            continue
                        results.append({"engine": "Zhihu", "title": title, "url": real_url,
                                        "snippet": snippet, "type": "community",
                                        "rank": len(results) + 1})
                        if len(results) >= limit:
                            break
                    except Exception:
                        continue
            finally:
                browser.close()
        if results:
            _mark_fingerprint_success(used_fp, "sogou")
            return results, None
        last_error = last_error or "Zhihu (Sogou) returned 0 results"
        if used_fp is not None:
            _cooldown_fingerprint(used_fp, "sogou", base_seconds=20)
    if last_error and "CAPTCHA" in last_error:
        # Sogou blocks at the IP level; retrying every round just burns time.
        _zhihu_captcha_cooldown_until = _time.time() + 600
    return [], last_error or "Zhihu (Sogou) returned 0 results"

def search_csdn(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """CSDN blog/community search via its public JSON endpoint."""
    params = urllib.parse.urlencode({
        "q": query, "t": "all", "p": 1, "s": 0, "tm": 0, "size": min(limit + 5, 30),
    })
    url = f"https://so.csdn.net/api/v3/search?{params}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
        "Referer": "https://so.csdn.net/",
    }
    try:
        data = _fetch_json(url, headers=headers, timeout=15, retries=1, max_retry_delay=8.0)
    except Exception as exc:
        return [], f"CSDN failed: {exc}"
    results = []
    seen_titles = set()
    for item in (data.get("result_vos") or []):
        if not isinstance(item, dict):
            continue
        title = re.sub(r"</?em>", "", str(item.get("title") or "")).strip()
        link = str(item.get("url_location") or item.get("url") or "").strip()
        if link.startswith("http") and "ops_request_misc" in link:
            link = link.split("?", 1)[0]  # strip search-attribution tracking params
        snippet = re.sub(r"</?em>", "", str(item.get("description") or item.get("digest") or "")).strip()[:500]
        if not title or len(title) < 5 or not link.startswith("http"):
            continue
        title_key = title.lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        results.append({"engine": "CSDN", "title": title, "url": link,
                        "snippet": snippet, "type": "community", "rank": len(results) + 1})
        if len(results) >= limit:
            break
    return results, None

def search_stackoverflow(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Stack Overflow search via the public StackExchange API."""
    from html import unescape
    params = {
        "order": "desc", "sort": "relevance", "q": query,
        "site": "stackoverflow", "pagesize": min(limit + 5, 30),
    }
    api_key = os.environ.get("STACKEXCHANGE_API_KEY", "").strip()
    if api_key:
        params["key"] = api_key
    url = "https://api.stackexchange.com/2.3/search/excerpts?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "web-search-skill/22.0", "Accept": "application/json"}
    try:
        payload = _fetch_bytes(url, headers=headers, timeout=15, retries=1, max_retry_delay=8.0)
        if payload[:2] == b"\x1f\x8b":  # the API may gzip regardless of Accept-Encoding
            payload = gzip.decompress(payload)
        data = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        return [], f"StackOverflow failed: {exc}"
    results = []
    seen_questions = set()
    for item in (data.get("items") or []):
        if not isinstance(item, dict):
            continue
        question_id = item.get("question_id")
        title = unescape(re.sub(r"<[^>]+>", "", str(item.get("title") or ""))).strip()
        if not question_id or question_id in seen_questions or not title or len(title) < 5:
            continue
        seen_questions.add(question_id)
        snippet = unescape(re.sub(r"<[^>]+>", "", str(item.get("excerpt") or ""))).strip()[:500]
        results.append({"engine": "StackOverflow", "title": title,
                        "url": f"https://stackoverflow.com/q/{question_id}",
                        "snippet": snippet, "type": "community",
                        "rank": len(results) + 1})
        if len(results) >= limit:
            break
    return results, None

def search_v2ex(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """V2EX topic search via the public SOV2EX API."""
    url = "https://www.sov2ex.com/api/search?" + urllib.parse.urlencode(
        {"q": query, "size": min(limit + 5, 30)})
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
               "Accept": "application/json"}
    try:
        data = _fetch_json(url, headers=headers, timeout=15, retries=1, max_retry_delay=8.0)
    except Exception as exc:
        return [], f"V2EX failed: {exc}"
    results = []
    for hit in (data.get("hits") or []):
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        topic_id = source.get("id")
        title = str(source.get("title") or "").strip()
        if not topic_id or not title or len(title) < 5:
            continue
        snippet = str(source.get("content") or "").strip()[:500]
        row = {"engine": "V2EX", "title": title,
               "url": f"https://www.v2ex.com/t/{topic_id}",
               "snippet": snippet, "type": "community", "rank": len(results) + 1}
        published = str(source.get("created") or "").strip()
        if published:
            row["published"] = published
        results.append(row)
        if len(results) >= limit:
            break
    return results, None

def search_juejin(query: str, limit: int = 10) -> Tuple[List[Dict], Optional[str]]:
    """Juejin article search via its public search endpoint."""
    url = "https://api.juejin.cn/search_api/v1/search?" + urllib.parse.urlencode(
        {"query": query, "id_type": 0, "limit": min(limit + 5, 20), "search_type": 0})
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
               "Accept": "application/json"}
    try:
        data = _fetch_json(url, headers=headers, timeout=15, retries=1, max_retry_delay=8.0)
    except Exception as exc:
        return [], f"Juejin failed: {exc}"
    if data.get("err_no") not in (0, None):
        return [], f"Juejin failed: err_no={data.get('err_no')} {str(data.get('err_msg'))[:80]}"
    results = []
    for row in (data.get("data") or []):
        model = (row.get("result_model") if isinstance(row, dict) else None) or {}
        info = model.get("article_info") or {}
        article_id = str(info.get("article_id") or "").strip()
        title = re.sub(r"<[^>]+>", "", str(info.get("title") or "")).strip()
        if not article_id or not title or len(title) < 5:
            continue
        snippet = str(info.get("brief_content") or "").strip()[:500]
        results.append({"engine": "Juejin", "title": title,
                        "url": f"https://juejin.cn/post/{article_id}",
                        "snippet": snippet, "type": "community", "rank": len(results) + 1})
        if len(results) >= limit:
            break
    return results, None

# ============================================================
# EXTENDED MULTI-ENGINE SEARCH (replaces search_all_engines)
# ============================================================


# ============================================================
def _run_channel_cached(name: str, func, query: str, limit: int,
                        resource_type: str = "serp") -> Tuple[List[Dict], Optional[str], str]:
    """Run one independent channel with fresh/stale cache semantics."""
    cache = _get_persistent_cache()
    namespace = f"channel:{name.lower()}"
    cache_key = make_cache_key(query.strip(), int(limit))
    policy = classify_cache_policy(query, resource_type)
    hit = cache.get(namespace, cache_key)
    cached_results: List[Dict] = []
    cached_error: Optional[str] = None
    if hit.hit and not hit.is_failure:
        if isinstance(hit.value, dict) and isinstance(hit.value.get("results"), list):
            cached_value = hit.value["results"]
            cached_error = hit.value.get("error") or None
        elif isinstance(hit.value, list):  # backwards-compatible v20 cache entries
            cached_value = hit.value
        else:
            cached_value = []
        cache_created_at = _timestamp_iso(hit.created_at)
        cached_results = [
            dict(
                item,
                cache_state=hit.state,
                cache_created_at=cache_created_at,
                served_at=_utc_now_iso(),
            )
            for item in cached_value[:limit]
            if isinstance(item, dict)
        ]
        if hit.fresh and not _force_fresh:
            return cached_results, cached_error, "fresh"
    elif hit.fresh and hit.is_failure and not _force_fresh:
        return [], hit.error or "cached channel failure", "failure"

    try:
        live_results, error = func(query, limit)
    except Exception as exc:
        live_results, error = [], f"Exception: {exc}"

    now = _utc_now_iso()
    live_results = [
        dict(
            item, cache_state="live", discovery_query=query,
            discovered_at=item.get("discovered_at") or now,
            validated_at=now,
        )
        for item in (live_results or [])[:limit]
        if isinstance(item, dict)
    ]
    if live_results:
        ttl = policy.success_ttl if not error else max(policy.failure_ttl, policy.success_ttl // 4)
        cache.set(
            namespace, cache_key,
            {"results": live_results, "error": error, "complete": not bool(error)},
            ttl=ttl, stale_ttl=policy.stale_ttl,
        )
        if cached_results and (not _force_fresh or bool(error)):
            combined = merge_search_results(live_results + cached_results, query)
            return combined, error, "live+stale"
        return live_results, error, "live"

    if cached_results:
        return cached_results, error or cached_error or "live refresh returned 0 results", "stale"
    if error:
        cache.set_failure(namespace, cache_key, error, ttl=policy.failure_ttl)
    else:
        # A valid zero-result response prevents repeatedly hammering a channel,
        # but expires quickly so newly indexed material can appear soon.
        cache.set(
            namespace, cache_key,
            {"results": [], "error": None, "complete": True},
            ttl=min(policy.success_ttl, 300), stale_ttl=0,
        )
    return [], error, "live"


# English-indexed sources cannot match CJK-only queries and Chinese community
# sources rank CJK queries far better, so language-affine channels may swap in
# a caller-supplied translation of the round query.  General engines
# (Google/Bing) always receive the original query.
_CHANNEL_LANGUAGE_AFFINITY = {
    "Baidu": "zh", "Zhihu": "zh", "CSDN": "zh", "V2EX": "zh", "Juejin": "zh",
    "StackOverflow": "en", "HackerNews": "en", "Reddit": "en",
    "SemanticScholar": "en", "arXiv": "en", "Crossref": "en", "OpenAlex": "en",
    "GitHub": "en", "DBLP": "en", "PubMed": "en",
    # Wikipedia has no fixed affinity: it self-detects CJK in whatever query it
    # receives and picks the wiki-language subdomain internally (like Google/Bing,
    # it always receives the base query rather than a swapped translation).
}

_CJK_CHAR_PATTERN = re.compile(r"[㐀-䶿一-鿿]")
_LATIN_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+#-]*")

def _effective_channel_query(channel: str, query: str,
                             alt_queries: Optional[Dict[str, str]] = None) -> str:
    """Pick the query variant a language-affine channel should run.

    Falls back to the Latin tokens of a mixed CJK query for English-indexed
    channels when no translation was supplied, so they are not queried with
    text their index cannot contain.
    """
    affinity = _CHANNEL_LANGUAGE_AFFINITY.get(channel)
    if not affinity:
        return query
    alt = str((alt_queries or {}).get(affinity) or "").strip()
    if alt:
        return alt
    if affinity == "en" and _CJK_CHAR_PATTERN.search(query):
        tokens = _LATIN_TOKEN_PATTERN.findall(query)
        # Digit-only leftovers like a bare year would query English indexes
        # with pure noise; require at least one alphabetic token.
        if any(re.search(r"[A-Za-z]", token) for token in tokens):
            latin = " ".join(tokens)
            if len(latin) >= 4:
                return latin
    return query

def search_all_engines_extended(query: str, limit: int = 15, vendor: Optional[str] = None,
                                alt_queries: Optional[Dict[str, str]] = None) -> Tuple[List[Dict], Dict[str, str]]:
    """
    Search ALL engines (web + academic + code + community), merge results.
    Each engine is wrapped in try/except - failures are logged but never crash the search.
    Returns (merged_results, engine_status).
    """
    # Every configured channel participates in every query round.  Vertical
    # channels may return zero relevant rows, but are never treated as fallbacks.
    _browser_channel_count = 4  # Google, Bing, Baidu, Zhihu (via Sogou)
    providers = [
        ("Google", playwright_google_search, limit, "serp"),
        ("Bing", playwright_bing_search, limit, "serp"),
        ("Baidu", playwright_baidu_search, limit, "serp"),
        ("Zhihu", search_zhihu, min(limit, 10), "community"),
        ("arXiv", search_arxiv, min(limit, 20), "serp"),
        ("Crossref", search_crossref, min(limit, 20), "serp"),
        ("OpenAlex", search_openalex, min(limit, 20), "serp"),
        ("DBLP", search_dblp, min(limit, 20), "serp"),
        ("PubMed", search_pubmed, min(limit, 20), "serp"),
        ("Wikipedia", search_wikipedia, min(limit, 10), "serp"),
        ("GitHub", search_github, min(limit, 20), "serp"),
        ("HackerNews", search_hackernews, min(limit, 20), "community"),
        ("CSDN", search_csdn, min(limit, 10), "community"),
        ("StackOverflow", search_stackoverflow, min(limit, 15), "community"),
        ("V2EX", search_v2ex, min(limit, 15), "community"),
        ("Juejin", search_juejin, min(limit, 15), "community"),
    ]
    # Keyless SemanticScholar is throttled into permanent 429s and anonymous
    # Reddit search is IP-blocked, so these channels only run with credentials.
    if os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip():
        providers.append(("SemanticScholar", search_semantic_scholar, min(limit, 20), "serp"))
    if os.environ.get("REDDIT_ACCESS_TOKEN", "").strip():
        providers.append(("Reddit", search_reddit, min(limit, 20), "community"))
    engine_status: Dict[str, str] = {}
    channel_outputs: Dict[str, List[Dict]] = {}

    # Keep browser engines sequential to avoid several Chromium processes
    # competing for CPU/RAM; each hits a distinct host (Google/Bing/Baidu/
    # Sogou).  Lightweight API channels run concurrently while those browser
    # searches are in progress.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    routed_queries = {
        name: _effective_channel_query(name, query, alt_queries)
        for name, *_ in providers
    }

    def _status_suffix(name: str) -> str:
        routed = routed_queries[name]
        return f" (q: {routed[:60]!r})" if routed != query else ""

    api_providers = providers[_browser_channel_count:]
    with ThreadPoolExecutor(max_workers=min(8, len(api_providers))) as executor:
        futures = {
            executor.submit(_run_channel_cached, name, func, routed_queries[name],
                            provider_limit, resource_type): name
            for name, func, provider_limit, resource_type in api_providers
        }
        for name, func, provider_limit, resource_type in providers[:_browser_channel_count]:
            results, error, cache_state = _run_channel_cached(
                name, func, routed_queries[name], provider_limit, resource_type
            )
            channel_outputs[name] = results
            if error and results:
                engine_status[name] = f"⚠️ {len(results)} partial results [{cache_state}]; {error}"
            elif error:
                engine_status[name] = f"❌ [{cache_state}] {error}"
            else:
                engine_status[name] = f"✅ {len(results)} results [{cache_state}]{_status_suffix(name)}"

        for future in as_completed(futures):
            name = futures[future]
            try:
                results, error, cache_state = future.result()
            except Exception as exc:
                results, error, cache_state = [], f"Exception: {exc}", "live"
            channel_outputs[name] = results
            if error and results:
                engine_status[name] = f"⚠️ {len(results)} partial results [{cache_state}]; {error}"
            elif error:
                engine_status[name] = f"❌ [{cache_state}] {error}"
            else:
                engine_status[name] = f"✅ {len(results)} results [{cache_state}]{_status_suffix(name)}"

    all_raw = [item for name, *_ in providers for item in channel_outputs.get(name, [])]
    ordered_status = {name: engine_status[name] for name, *_ in providers if name in engine_status}
    return merge_search_results(all_raw, query), ordered_status

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    usage = (
        "Usage: search.py <query> [--limit N] [--max-iter N] [--fresh] "
        "[--plan-only] [--summary] [--review-query QUERY ...] "
        "[--query-en TEXT] [--query-zh TEXT]\n\n"
        "All enabled channels run for every query round. Repeat --review-query "
        "with targeted directions selected by a model review pass; --summary "
        "emits a compact review packet while the default keeps the full evidence. "
        "--query-en/--query-zh supply translations of the base query so "
        "language-affine channels (StackOverflow/HN/arXiv/... vs "
        "Baidu/Zhihu/CSDN/V2EX/Juejin) search in the language their index holds."
    )
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(usage)
        sys.exit(0 if len(sys.argv) >= 2 else 1)
    query = sys.argv[1]
    limit = 15
    max_iter = 3
    fresh = False
    plan_only = False
    summary_only = False
    review_queries: List[str] = []
    alt_queries: Dict[str, str] = {}
    i = 2
    # Value-bearing flags need a following token; boolean flags don't consume one.
    # Anything else (typo'd flag, or a value-flag with no value left) is a hard
    # error instead of being silently skipped -- a swallowed --fresh/--limit typo
    # used to run "successfully" against the wrong settings with zero warning.
    known_value_flags = {"--limit", "--max-iter", "--review-query", "--query-en", "--query-zh"}
    while i < len(sys.argv):
        token = sys.argv[i]
        if token in known_value_flags and i + 1 >= len(sys.argv):
            print(f"Error: {token} requires a value.\n\n{usage}", file=sys.stderr)
            sys.exit(1)
        if token == "--limit":
            limit = max(1, int(sys.argv[i + 1])); i += 2
        elif token == "--max-iter":
            max_iter = max(1, int(sys.argv[i + 1])); i += 2
        elif token == "--review-query":
            review_queries.append(sys.argv[i + 1]); i += 2
        elif token == "--query-en":
            alt_queries["en"] = sys.argv[i + 1]; i += 2
        elif token == "--query-zh":
            alt_queries["zh"] = sys.argv[i + 1]; i += 2
        elif token == "--fresh":
            fresh = True; i += 1
        elif token == "--plan-only":
            plan_only = True; i += 1
        elif token == "--summary":
            summary_only = True; i += 1
        else:
            print(f"Error: unrecognized argument {token!r}.\n\n{usage}", file=sys.stderr)
            sys.exit(1)
    try:
        if plan_only:
            vendor = detect_vendor(query)
            output = {"query": query, "detected_vendor": vendor,
                      "expansions": generate_expansion_queries(query, vendor)}
        else:
            output = smart_search(
                query, limit, max_iter, fresh=fresh, review_queries=review_queries,
                alt_queries=alt_queries or None,
            )
            if summary_only:
                output = compact_search_output(output)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        _close_persistent_cache()
