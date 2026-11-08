"""Muse generation engine: drives muse.ai through a real web session to produce images/videos.

Verified flow:
  inject cookies -> open https://muse.ai/ -> locate textarea (placeholder=message)
  -> Input.insertText -> click "Send"
  -> wait for a new attachment container [data-testid^=hatch-chat-attachment-presentation-]
  -> read blob bytes from img/video inside that container -> save to disk
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import http.client
import ipaddress
import mimetypes
import math
import urllib.error
import urllib.parse
import urllib.request
import json
import logging
import os
import re
import shutil
import struct
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import unicodedata

from cdp import CDP, http_json
from proxy import HTTPProxyRelay, request_proxy_context

log = logging.getLogger("muse2api")

ATT_SEL = '[data-testid^="hatch-chat-attachment-presentation-"]'
# Reference limits mirror the muse.ai web app (maxFilesPerUpload=10 / maxFileSize=40 MiB /
# maxVideoDurationSeconds=120). Constant names keep *_IMAGES, but they count all media.
MAX_REFERENCE_IMAGES = 10
MAX_REFERENCE_IMAGE_BYTES = 40 * 1024 * 1024
MAX_REFERENCE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_REFERENCE_VIDEO_SECONDS = 120
_MIB = 1024 * 1024

# Media types accepted by the Muse composer -> on-disk extension.
# Images/videos/documents come from Muse's frontend allowlist; audio is not on it,
# but the composer accepts it and the model can read wav content in practice, so allow it.
REFERENCE_MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
    "image/avif": ".avif",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/mp4": ".m4a",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/aac": ".aac",
    "audio/opus": ".opus",
    "application/pdf": ".pdf", "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
_MIME_ALIASES = {
    "image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg",
    "video/x-m4v": "video/mp4", "video/mov": "video/quicktime",
    "audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg", "audio/mpeg3": "audio/mpeg",
    "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav",
    "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4", "audio/x-flac": "audio/flac",
    "audio/x-aac": "audio/aac", "audio/aacp": "audio/aac",
}
# These declared types are untrusted; detect by file signature instead.
_GENERIC_MIMES = {"", "application/octet-stream", "binary/octet-stream",
                  "application/binary", "application/unknown"}


def reference_kind(mime: str) -> str:
    """image / video / audio / document。"""
    head = (mime or "").split("/", 1)[0]
    return head if head in ("image", "video", "audio") else "document"


def _sniff_mime(data: bytes) -> str | None:
    """Detect media type from file signature; return None if unknown."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis"):
            return "image/heic"
        if brand in (b"mif1", b"msf1", b"heif"):
            return "image/heif"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand.startswith(b"3g"):
            return "video/3gpp"
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "audio/aac"
    if data.startswith(b"OggS"):
        return "audio/opus" if b"OpusHead" in data[:64] else "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    return None


def _mp4_duration(data: bytes) -> float | None:
    """Read the moov/mvhd duration (seconds) from mp4/mov; return None if unparseable."""
    def boxes(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            size = int.from_bytes(data[pos:pos + 4], "big")
            kind = data[pos + 4:pos + 8]
            header = 8
            if size == 1 and pos + 16 <= end:
                size = int.from_bytes(data[pos + 8:pos + 16], "big")
                header = 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                return
            yield kind, pos + header, pos + size
            pos += size

    for kind, body, end in boxes(0, len(data)):
        if kind != b"moov":
            continue
        for sub, sbody, send in boxes(body, end):
            if sub != b"mvhd" or send - sbody < 20:
                continue
            if data[sbody] == 1:
                if send - sbody < 32:
                    return None
                scale = int.from_bytes(data[sbody + 20:sbody + 24], "big")
                duration = int.from_bytes(data[sbody + 24:sbody + 32], "big")
            else:
                scale = int.from_bytes(data[sbody + 12:sbody + 16], "big")
                duration = int.from_bytes(data[sbody + 16:sbody + 20], "big")
            return duration / scale if scale else None
    return None


def _webm_duration(data: bytes) -> float | None:
    """Read EBML Segment/Info duration; unknown or malformed metadata fails closed."""
    def vint(pos, end, keep_marker=False):
        if pos >= end or not data[pos]:
            raise ValueError("invalid EBML integer")
        width = 1
        while width <= 8 and not (data[pos] & (0x80 >> (width - 1))):
            width += 1
        if width > 8 or pos + width > end:
            raise ValueError("truncated EBML integer")
        value = int.from_bytes(data[pos:pos + width], "big")
        if not keep_marker:
            value &= (1 << (7 * width)) - 1
            if value == (1 << (7 * width)) - 1:
                value = None
        return value, pos + width

    def elements(start, end):
        while start < end:
            tag, pos = vint(start, end, True)
            size, body = vint(pos, end)
            stop = end if size is None else body + size
            if stop > end or stop <= start:
                raise ValueError("invalid EBML element size")
            yield tag, body, stop
            start = stop

    try:
        for tag, body, end in elements(0, len(data)):
            if tag != 0x18538067:  # Segment
                continue
            for tag, body, end in elements(body, end):
                if tag != 0x1549A966:  # Info
                    continue
                scale, duration = 1000000, None
                for tag, body, end in elements(body, end):
                    if tag == 0x2AD7B1 and 0 < end - body <= 8:
                        scale = int.from_bytes(data[body:end], "big")
                    elif tag == 0x4489 and end - body in (4, 8):
                        duration = struct.unpack(">f" if end - body == 4 else ">d", data[body:end])[0]
                return duration * scale / 1e9 if duration is not None and scale else None
    except (ValueError, struct.error):
        return None
    return None


# Core cookies that decide account lifetime (missing/expired = session invalid).
# hatch_sess is the login credential; hatch_vml can be issued by the workspace, so its absence must not block import.
ESSENTIAL_COOKIES = ("hatch_sess",)


class MuseAuthError(RuntimeError):
    pass


class MuseGenerationError(RuntimeError):
    pass


class MuseProxyError(MuseGenerationError):
    """The account's configured proxy failed; this is not Cookie invalidation."""


class MuseModelRefusalError(MuseGenerationError):
    """Muse completed the turn with text instead of the requested media."""


class RefImageSafetyError(MuseGenerationError):
    """A reference image source is forbidden by the configured input policy."""


class _RefImageHTTPHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, address: str):
        super().__init__()
        self.address = address

    def _connection(self, host, connection_type, **kwargs):
        connection = connection_type(host, **kwargs)

        def create_connection(address, timeout, source_address):
            return socket.create_connection((self.address, address[1]), timeout, source_address)

        # Keep the original host for Host, TLS SNI and certificate validation;
        # only the socket destination is pinned to the checked address.
        connection._create_connection = create_connection
        return connection

    def http_open(self, req):
        return self.do_open(lambda host, **kw: self._connection(
            host, http.client.HTTPConnection, **kw), req)

    def https_open(self, req):
        return self.do_open(lambda host, **kw: self._connection(
            host, http.client.HTTPSConnection, **kw), req)


class _NoRefImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_public_ref_image(url: str):
    """Open one public HTTP(S) URL with DNS rebinding and redirect checks."""
    for _ in range(11):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise RefImageSafetyError("Reference-image redirects must be valid http(s) URLs")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        checked = []
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if not ip.is_global:
                raise RefImageSafetyError(
                    "Reference-image address rejected: public addresses only; set "
                    "MUSE2API_REF_IMAGE_PRIVATE_FETCH=1")
            checked.append(str(ip))
        if not checked:
            raise urllib.error.URLError("Reference-image domain did not resolve to any address")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRefImageRedirect(),
            _RefImageHTTPHandler(checked[0]))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            return opener.open(req, timeout=20)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
            if exc.code not in (301, 302, 303, 307, 308) or not location:
                raise
            exc.close()
            url = urllib.parse.urljoin(url, location)
    raise urllib.error.URLError("Too many reference-image redirects")


class MuseEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.browser: CDP | None = None
        self._local = threading.local()
        self._start_lock = threading.Lock()
        self._active_contexts: set[str] = set()
        self._contexts_lock = threading.Lock()
        self._proxy_relays: set[HTTPProxyRelay] = set()
        self._download_dirs: set[str] = set()
        self.proxy_resolver = None
        self._last_http_renew: dict[str, float] = {}
        self._log = None
        self._started_at: float = 0.0  # monotonic time of last successful CDP connect
        os.makedirs(cfg.profile_dir, exist_ok=True)

    def _ensure_local(self):
        local = getattr(self, "_local", None)
        if local is None:
            local = threading.local()
            self._local = local
        return local

    @property
    def page(self) -> CDP | None:
        return getattr(self._ensure_local(), "page", None)

    @page.setter
    def page(self, val: CDP | None):
        self._ensure_local().page = val

    @property
    def current_acc_id(self) -> str | None:
        return getattr(self._ensure_local(), "current_acc_id", None)

    @current_acc_id.setter
    def current_acc_id(self, val: str | None):
        self._ensure_local().current_acc_id = val

    @property
    def current_context_id(self) -> str | None:
        return getattr(self._ensure_local(), "current_context_id", None)

    @current_context_id.setter
    def current_context_id(self, val: str | None):
        self._ensure_local().current_context_id = val

    # ---------------- Browser lifecycle ----------------
    def _debug_url(self):
        return f"http://127.0.0.1:{self.cfg.cdp_port}/json/version"

    def start(self):
        # Hold the lock for the whole startup, not just the fast-path probe: two
        # threads that both pass the check would spawn two browsers against the same
        # --user-data-dir and --remote-debugging-port, and the singleton-lock cleanup
        # below would delete the lock out from under the winner.
        with self._start_lock:
            self._start_locked()

    def _start_locked(self):
        if self.proc and self.proc.poll() is None and self.browser:
            return
        env = dict(os.environ)
        env.setdefault("HOME", self.cfg.home_dir)
        env["PATH"] = (self.cfg.extra_path + os.pathsep + env.get("PATH", "")) if self.cfg.extra_path else env.get("PATH", "")
        args = [
            self.cfg.chromium,
            "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--disable-background-networking",
            "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1440,2400",
            f"--remote-debugging-port={self.cfg.cdp_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.cfg.profile_dir}",
            "about:blank",
        ]
        # Try to reuse an existing healthy CDP endpoint
        if not self.proc:
            try:
                v = http_json(self._debug_url(), timeout=1)
                if v and "webSocketDebuggerUrl" in v:
                    self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)
                    self._started_at = time.monotonic()
                    return
            except Exception:
                pass

        # Clean up stale lock files
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lp = os.path.join(self.cfg.profile_dir, lock_name)
            if os.path.exists(lp) or os.path.islink(lp):
                try:
                    os.unlink(lp)
                except Exception:
                    pass

        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self._log = open(os.path.join(self.cfg.data_dir, "chromium.log"), "ab", buffering=0)
        cwd_dir = self.cfg.home_dir if (self.cfg.home_dir and os.path.isdir(self.cfg.home_dir)) else None
        self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT,
                                     env=env, cwd=cwd_dir)
        last = None
        for _ in range(90):
            try:
                v = http_json(self._debug_url(), timeout=2)
                self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)
                self._started_at = time.monotonic()
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1)
        raise MuseGenerationError(f"Chromium failed to start: {last}")

    def is_healthy(self) -> tuple[bool, str]:
        """Liveness probe for the background health check (app.py's keepalive loop).

        Read-only; does not take self._start_lock, so it can run concurrently with a
        legitimate start/stop without blocking either side. A stale read just means this
        cycle's verdict is skipped or retried next tick -- never a crash, because every
        mutation of self.proc/self.browser happens under GEN_LOCK held by the caller
        (see _start_locked's call sites in app.py), except the process-exiting restart
        path in /admin/update/upgrade, which calls os._exit(0) right after.
        """
        if not self.proc or not self.browser:
            return False, "not started"
        code = self.proc.poll()
        if code is not None:
            return False, f"process exited (code {code})"
        try:
            http_json(self._debug_url(), timeout=3)
        except Exception as exc:  # noqa: BLE001
            return False, f"CDP endpoint unresponsive: {exc}"
        return True, "ok"

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_at if self._started_at else 0.0

    def recycle(self, reason: str) -> None:
        """Forcefully tear down the shared Chromium so the next start() spawns fresh.

        Caller MUST already hold GEN_LOCK via a non-blocking acquire -- see app.py's
        _browser_health_check_sync, the only intended caller. Reuses stop() entirely;
        no new teardown logic is added here.
        """
        log.warning("[Browser health] Recycling Chromium: %s", reason)
        self.stop()

    def stop(self):
        with self._start_lock:
            self._close_page()
            if self.browser:
                with self._contexts_lock:
                    for ctx_id in list(self._active_contexts):
                        try:
                            self.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
                        except Exception:
                            pass
                    self._active_contexts.clear()
                try:
                    self.browser.close()
                except Exception:
                    pass
                self.browser = None
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
            self.proc = None
            with self._contexts_lock:
                relays = list(self._proxy_relays)
                self._proxy_relays.clear()
            for relay in relays:
                relay.close()
            with self._contexts_lock:
                download_dirs = list(self._download_dirs)
                self._download_dirs.clear()
            for directory in download_dirs:
                shutil.rmtree(directory, ignore_errors=True)

    # ---------------- Page ----------------
    @property
    def download_dir(self) -> str:
        return getattr(self._ensure_local(), "download_dir", None) or self.cfg.download_dir

    def _new_download_dir(self) -> str:
        os.makedirs(self.cfg.download_dir, exist_ok=True)
        directory = tempfile.mkdtemp(prefix="context-", dir=self.cfg.download_dir)
        self._ensure_local().download_dir = directory
        with self._contexts_lock:
            self._download_dirs.add(directory)
        return directory

    def _close_page(self):
        """Close this thread's page and dispose its isolated BrowserContext to fully release memory and Session."""
        p = self.page
        ctx_id = self.current_context_id
        if p:
            try:
                p.close()
            except Exception:
                pass
            self.page = None
        if ctx_id and self.browser:
            try:
                self.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
            except Exception:
                pass
            with self._contexts_lock:
                self._active_contexts.discard(ctx_id)
            self.current_context_id = None
        self.current_acc_id = None
        local = self._ensure_local()
        relay = getattr(local, "proxy_relay", None)
        if relay:
            relay.close()
            with self._contexts_lock:
                self._proxy_relays.discard(relay)
            local.proxy_relay = None
        local.proxy_config = None
        directory = getattr(local, "download_dir", None)
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
            with self._contexts_lock:
                self._download_dirs.discard(directory)
            local.download_dir = None

    def _open_page(self, proxy: dict | None = None):
        """Create an isolated account context; never fall back to shared cookies."""
        if not self.browser:
            error = MuseProxyError if proxy else MuseGenerationError
            raise error("Browser is not connected; cannot create an isolated account session; refusing to fall back to a shared tab")
        page = None
        try:
            if proxy:
                relay = HTTPProxyRelay(proxy)
                self._ensure_local().proxy_relay = relay
                with self._contexts_lock:
                    self._proxy_relays.add(relay)
                res = self.browser.send("Target.createBrowserContext", {
                    "proxyServer": relay.url,
                    "proxyBypassList": "<-loopback>",
                })
            else:
                res = self.browser.send("Target.createBrowserContext")
            ctx_id = res.get("result", {}).get("browserContextId")
            if not ctx_id:
                raise MuseGenerationError("Chromium did not return an isolated context ID")
            self.current_context_id = ctx_id
            with self._contexts_lock:
                self._active_contexts.add(ctx_id)
            t_res = self.browser.send("Target.createTarget", {
                "url": "about:blank", "browserContextId": ctx_id,
            })
            target_id = t_res.get("result", {}).get("targetId")
            if not target_id:
                raise MuseGenerationError("Chromium did not return an isolated page ID")
            ws_url = f"ws://127.0.0.1:{self.cfg.cdp_port}/devtools/page/{target_id}"
            page = CDP(ws_url, timeout=180)
            page.send("Network.enable")
            page.send("Page.enable")
            page.send("Runtime.enable")
            page.send("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": self._new_download_dir(),
                "browserContextId": ctx_id,
            })
            return page
        except Exception:
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            self._close_page()
            error = MuseProxyError if proxy else MuseGenerationError
            raise error("Cannot create an isolated browser session for this account; refusing to fall back to a shared tab or direct connection") from None

    @staticmethod
    def renew_session_http(cookies: dict, expires: dict | None = None,
                           wake_vm: bool = True, proxy: dict | None = None) -> dict:
        """Call muse.ai/api/session directly to renew hatch_vml (+48h) / hatch_sess (+30d) / hatch_gw (+1y),
        and wake the cloud VM via /api/hatch/vm/wake when needed."""
        import requests
        with request_proxy_context(proxy) as proxy_options:
            cur_cookies = dict(cookies or {})
            cur_exp = dict(expires or {})
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                "Origin": "https://muse.ai",
                "Referer": "https://muse.ai/thread/new",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Cookie": "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v),
            }
            try:
                r = requests.get("https://muse.ai/api/session", headers=headers,
                                 timeout=12, allow_redirects=False, **proxy_options)
            except requests.RequestException as exc:
                # Do not echo request details: exceptions may contain credential-bearing proxy URLs.
                if proxy:
                    raise MuseProxyError(
                        f"Account proxy request failed ({type(exc).__name__}); check the proxy connection or credentials. No direct fallback was attempted") from None
                raise MuseGenerationError(
                    f"/api/session network request failed ({type(exc).__name__}); check the server network/proxy and retry") from None
            if proxy and r.status_code == 407:
                raise MuseProxyError("Account proxy authentication failed (HTTP 407); check the proxy username and password")
            if r.status_code == 401:
                raise MuseAuthError("Session authentication failed (/api/session HTTP 401); confirm login on the website and re-import cookies")
            if r.status_code != 200:
                hint = ("Access denied; check the server egress/region/access restrictions. This does not prove the Cookie expired"
                        if r.status_code == 403 else "Upstream request failed; retry later and check the server network")
                raise MuseGenerationError(f"/api/session HTTP {r.status_code}：{hint}")
            try:
                sj = r.json()
            except ValueError:
                raise MuseGenerationError("/api/session HTTP 200 returned non-JSON; session state is unconfirmed") from None
            if not isinstance(sj, dict) or sj.get("status") != "assigned":
                raise MuseGenerationError("/api/session HTTP 200 did not return an assigned session; check the account/workspace on the website")
            for c in r.cookies:
                if c.value:
                    cur_cookies[c.name] = c.value
                if c.expires:
                    cur_exp[c.name] = int(float(c.expires))
            vm_id = sj.get("vm_id")
            vm_state = sj.get("vm_state")
            wake_ok = False
            wake_requested = False
            if wake_vm and vm_id and vm_state != "DISABLED":
                try:
                    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v)
                    rw = requests.post("https://muse.ai/api/hatch/vm/wake", headers=headers, json={
                        "vm_id": vm_id,
                        "retry_count": 0,
                        "connect_attempt_id": str(uuid.uuid4()),
                    }, timeout=8, allow_redirects=False, **proxy_options)
                    if proxy and rw.status_code == 407:
                        raise MuseProxyError("Account proxy authentication failed (HTTP 407); VM wake was not completed")
                    wake_requested = rw.status_code == 200
                    if wake_requested:
                        # HTTP 200 acknowledges the request; only session state confirms the VM woke.
                        verify = requests.get("https://muse.ai/api/session", headers=headers,
                                              timeout=12, allow_redirects=False, **proxy_options)
                        if proxy and verify.status_code == 407:
                            raise MuseProxyError("Account proxy authentication failed (HTTP 407); VM state confirmation was not completed")
                        if verify.status_code == 200:
                            verified_session = verify.json()
                            if isinstance(verified_session, dict) and verified_session.get("status") == "assigned":
                                vm_state = verified_session.get("vm_state") or vm_state
                                wake_ok = vm_state == "RUNNING"
                except MuseProxyError:
                    raise
                except requests.RequestException as exc:
                    if proxy:
                        raise MuseProxyError(
                            f"Waking or confirming the VM through the account proxy failed ({type(exc).__name__}); no direct fallback was attempted") from None
                except Exception:
                    pass
            return {
                "ok": sj.get("status") == "assigned",
                "status": sj.get("status"),
                "vm_id": vm_id,
                "vm_state": vm_state,
                "wake_ok": wake_ok,
                "wake_requested": wake_requested,
                "cookies": cur_cookies,
                "cookies_exp": cur_exp,
            }

    def _apply_cookies(self, page: CDP, cookies: dict, expires: dict | None = None):
        """Inject cookies. Clear old account cookies for isolation, and never pass a past expires value so Chromium does not drop hatch_vml."""
        try:
            page.send("Network.clearBrowserCookies")
        except Exception:
            pass
        now = time.time()
        for name, value in cookies.items():
            if not value:
                continue
            exp = (expires or {}).get(name)
            try:
                exp_val = float(exp) if exp and float(exp) > now + 3600 else (now + 7 * 86400)
            except (TypeError, ValueError):
                exp_val = now + 7 * 86400
            for dom in (".muse.ai", "muse.ai"):
                params = {
                    "name": name,
                    "value": value,
                    "domain": dom,
                    "path": "/",
                    "secure": True,
                    "expires": exp_val,
                }
                try:
                    page.send("Network.setCookie", params)
                except Exception:  # noqa: BLE001
                    pass

    def read_cookies(self) -> dict[str, dict]:
        """Read cookies back from the current page (**including httpOnly**, which page JS cannot do).

        Returns {name: {"value":..., "expires": unix seconds or -1}}.
        Purpose: muse.ai renews some cookies on access; reading them back after generation
        and writing them to the account pool keeps accounts from expiring.
        """
        if not self.page:
            return {}
        try:
            msg = self.page.send("Network.getCookies",
                                 {"urls": [self.cfg.site_url]}, timeout=20)
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, dict] = {}
        for c in (msg.get("result", {}).get("cookies") or []):
            name = c.get("name")
            if not name:
                continue
            try:
                exp = int(float(c.get("expires", -1)))
            except (TypeError, ValueError):
                exp = -1
            out[name] = {"value": c.get("value", ""), "expires": exp}
        return out

    def _wait_ws_ready(self, page: CDP, timeout: float = 15.0) -> bool:
        """Wait until the muse.ai page finishes React hydration and is no longer Connecting..."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = page.js("""(function(){
                    if (document.readyState !== 'complete') return 'loading';
                    if (!document.querySelector('textarea')) return 'no-ta';
                    var h = document.querySelector('[data-hatch-shell-hydration-state]');
                    if (h && h.getAttribute('data-hatch-shell-hydration-state') !== 'hydrated') return 'hydrating';
                    var b = document.body ? (document.body.innerText || '') : '';
                    if (b.indexOf('Connecting...') !== -1) return 'connecting';
                    // Empty composers may not render Send until text has been entered.
                    if (document.querySelector('textarea').disabled) return 'input-disabled';
                    return 'ready';
                })()""")
                if st == "ready":
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def reset_thread(self, for_chat: bool = False):
        """Dismiss leftover dialogs and ensure a clean session with a ready WebSocket.
        For plain text chat (for_chat=True), reuse the current warm page when it has no attachments, is not stuck, and has few bubbles, achieving ~2s first-token latency."""
        if not self.page:
            return
        try:
            needs_nav = self.page.js("""(function(forChat){
                var d = document.querySelector('[role="dialog"]');
                if (d) {
                    var b = d.querySelector('button[aria-label*="close" i], button');
                    if (b) b.click();
                }
                var scope = document.querySelector('main,[class*="chat-scroll"],[class*="hatch-chat-scroll"]') || document.body;
                var bubbleCount = scope ? scope.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]').length : 0;
                var hasAtts = document.querySelectorAll('[data-testid^="hatch-chat-attachment-presentation-"]').length > 0;
                var hasStop = !!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
                    || document.querySelector('button[aria-label*="Stop" i]')
                    || document.querySelector('button[aria-label*="停止"]'));
                var bodyTxt = document.body ? (document.body.innerText || '') : '';
                var hasStuck = bodyTxt.indexOf('Still sending') !== -1 || bodyTxt.indexOf('Connecting...') !== -1;
                if (hasStop || hasStuck || hasAtts) return true;
                if (forChat) {
                    return bubbleCount >= 24;
                }
                return (window.location.pathname !== '/thread/new') || bubbleCount > 0;
            })(%s)""" % ("true" if for_chat else "false"))
            if needs_nav:
                self.page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
                t_end = time.time() + 10.0
                while time.time() < t_end:
                    time.sleep(0.08)
                    ready = self.page.js("""(function(){
                        return document.readyState === 'complete'
                            && !!document.querySelector('textarea')
                            && document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]').length === 0;
                    })()""")
                    if ready:
                        break
                self._wait_ws_ready(self.page, timeout=12.0)
        except Exception:
            pass

    def ensure_page(self, cookies: dict, expires: dict | None = None, account_id: str | None = None):
        resolver = getattr(self, "proxy_resolver", None)
        local = self._ensure_local()
        # A lease snapshots routing; a concurrent config edit must never change its exit.
        proxy = copy.deepcopy(local.proxy_snapshot if hasattr(local, "proxy_snapshot")
                              else resolver(account_id) if resolver and account_id else None)
        if (self.page is not None
                and getattr(self._ensure_local(), "proxy_config", None) == proxy
                and (account_id is None or getattr(self, "current_acc_id", None) == account_id)):
            try:
                if self.page.js("!!document.querySelector('textarea')") and self._wait_ws_ready(self.page, timeout=8.0):
                    return self.page
            except Exception:
                pass
        self._close_page()
        # Call /api/session on the main path only when more than 10 minutes have passed since the last HTTP renewal, to avoid repeated blocking on account switches
        last_map = getattr(self, "_last_http_renew", None)
        if last_map is None:
            last_map = {}
            self._last_http_renew = last_map
        now_ts = time.time()
        if not account_id or (now_ts - last_map.get(account_id, 0) > 600):
            try:
                renewed = self.renew_session_http(cookies, expires, wake_vm=True,
                                                  **({"proxy": proxy} if proxy else {}))
                if renewed.get("cookies"):
                    cookies = renewed["cookies"]
                if renewed.get("cookies_exp"):
                    expires = renewed["cookies_exp"]
                if account_id:
                    last_map[account_id] = now_ts
            except (MuseAuthError, MuseProxyError):
                raise
            except Exception as e:
                log.warning("Pre-renewal via /api/session failed (continuing with browser load): %s", e)

        page = self._open_page(proxy=proxy) if proxy else self._open_page()
        self.page = page  # Cleanup must also own pages that never become ready.
        self._ensure_local().proxy_config = proxy
        self._apply_cookies(page, cookies, expires)
        page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
        if self._wait_ws_ready(page, timeout=self.cfg.login_wait):
            self.current_acc_id = account_id
            return page
        try:
            body = (page.js("document.body ? document.body.innerText.slice(0,1200) : ''") or "").lower()
        except Exception:
            body = ""
        self._close_page()
        if proxy and re.search(r"err_(?:proxy|tunnel)|proxy authentication|代理认证失败", body, re.I):
            raise MuseProxyError("Account proxy browser connection failed; check the node or credentials. No direct fallback was attempted")
        if "connecting..." in body:
            raise MuseGenerationError("Page gateway connection timed out (Connecting...); switching accounts automatically")
        # Distinguish an expired session from a page that never became ready.
        if re.search(r"log in|sign in|create an account|登录|use another account", body):
            raise MuseAuthError("Session was signed out by muse.ai (possibly displaced by another login or flagged by risk control);"
                                "re-import cookies with the browser extension")
        raise MuseGenerationError("muse.ai page load timed out (chat input did not appear); check the server network and retry. Session invalidity was not confirmed")

    def close_current_page(self):
        """Explicitly release the page and isolated context held by the current thread."""
        self._close_page()

    def refresh(self, cookies: dict, expires: dict | None = None, account_id: str | None = None):
        self._close_page()
        return self.ensure_page(cookies, expires, account_id=account_id)

    # ---------------- Quota reading (Settings panel) ----------------
    # muse.ai quota lives under the bottom Settings menu -> Settings item -> settings panel,
    # General -> Usage section, shaped like:
    #   Free plan
    #   Weekly limit resets on Sep 30
    #   1% used
    #   Additional tokens / Never expires / 0% used (2B tokens left)
    def _click_point(self, x: int, y: int):
        for t in ("mousePressed", "mouseReleased"):
            self.page.send("Input.dispatchMouseEvent",
                           {"type": t, "x": x, "y": y,
                            "button": "left", "clickCount": 1})

    _CLICK_JS = (
        "(function(){var sel=%s;"
        "var b=[...document.querySelectorAll(sel)]"
        ".filter(function(x){return x.offsetParent!==null;})[0];"
        "if(!b)return null;var r=b.getBoundingClientRect();"
        "return JSON.stringify({x:Math.round(r.x+r.width/2),"
        "y:Math.round(r.y+r.height/2)});})()")

    _QUOTA_SETTINGS_JS = r"""(() => {
        const visible = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden';
        const labels = ['Settings', '設定', '设置'];
        const b = [...document.querySelectorAll('[data-testid="hatch-dock-more"]')].find(visible)
            || [...document.querySelectorAll('button,[role=button]')].find(e =>
                visible(e) && labels.includes((e.getAttribute('aria-label') || '').trim()));
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});
    })()"""

    _QUOTA_MENU_JS = r"""(() => {
        const visible = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden';
        const labels = ['Settings', '設定', '设置'];
        const exact = e => labels.includes((e.textContent || '').trim());
        // Menu items include a shortcut suffix (for example 設定Ctrl+,).
        const b = [...document.querySelectorAll('[role=menuitem]')].find(e =>
            visible(e) && (exact(e) || [...e.querySelectorAll('span,div')].some(exact)))
            || [...document.querySelectorAll('[role=menu] button,[role=menu] span,[role=menu] div')]
                .find(e => visible(e) && exact(e));
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});
    })()"""

    _QUOTA_DIALOG_JS = r"""(() => {
        const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')]
            .find(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden');
        return d ? (d.innerText || '') : '';
    })()"""

    def _quota_click(self, expr: str, error: str, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while True:
            raw = self.page.js(expr)
            if raw:
                pt = json.loads(raw)
                self._click_point(pt['x'], pt['y'])
                return
            if time.monotonic() >= deadline:
                raise MuseGenerationError(error)
            time.sleep(0.15)

    def _dismiss_quota_ui(self):
        # A failed lookup can leave a menu open; clean up menus and dialogs.
        for _ in range(2):
            for typ in ('keyDown', 'keyUp'):
                self.page.send('Input.dispatchKeyEvent',
                               {'type': typ, 'key': 'Escape', 'code': 'Escape',
                                'windowsVirtualKeyCode': 27, 'nativeVirtualKeyCode': 27})
            time.sleep(0.1)  # Let the UI finish closing before the next interaction.

    def quota(self, cookies: dict, expires: dict | None = None,
              account_id: str | None = None) -> dict:
        """Read one account's quota in a fresh, disposable browser context."""
        # Muse's Settings menu can remain in a half-closed React state after a read.
        # A disposable context is slower, but prevents consecutive reads from
        # inheriting stale UI and keeps account cookies strictly isolated.
        self._close_page()
        try:
            self.ensure_page(cookies, expires, account_id=account_id)
            self._quota_click(self._QUOTA_SETTINGS_JS, 'Settings entry not found (Settings/設定/设置)')
            self._quota_click(self._QUOTA_MENU_JS, 'Settings menu did not show a recognizable settings item')
            deadline = time.monotonic() + 12.0
            while True:
                parsed = self._parse_quota(self.page.js(self._QUOTA_DIALOG_JS) or '')
                if parsed['found']:
                    return parsed
                if time.monotonic() >= deadline:
                    raise MuseGenerationError('Quota panel returned no valid usage data; old cache was not overwritten')
                time.sleep(0.2)
        finally:
            self._close_page()

    @staticmethod
    def _parse_quota(txt: str) -> dict:
        """Parse English, traditional Chinese and simplified Chinese usage text."""
        normalized = unicodedata.normalize('NFKC', txt or '')
        lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
        # Avoid unrelated account/profile text before the Usage section.
        start = next((i + 1 for i, ln in enumerate(lines)
                      if ln.lower() in ('usage', '用量', '使用量')), 0)
        lines = lines[start:]
        end = next((i for i, ln in enumerate(lines)
                    if ln.lower() in ('language', '语言', '語言')), len(lines))
        lines = lines[:end]
        out: dict = {'raw': '\n'.join(lines[:40])}
        for ln in lines:
            if re.fullmatch(r'.{1,60}?(?:\s+plan|計劃|计划|方案)', ln, re.I):
                out['plan'] = ln
                break
        text = '\n'.join(lines)
        reset = re.search(r'Weekly limit resets? on ([^\n]+)', text, re.I)
        if not reset:
            reset = re.search(
                r'(?:每(?:週|周|星期)|週|周)(?:上限|限額|限额|額度|额度)'
                r'(?:會|会|將|将)?(?:於|于|在)\s*([^\n]+?)\s*(?:重設|重置)', text)
        if reset:
            out['weekly_reset'] = reset.group(1).strip()

        extra_at = next((i for i, ln in enumerate(lines) if re.match(
            r'^(?:(?:Additional|Extra)\s+tokens|(?:額外|额外)(?:的)?(?:符元|符記|代幣|代币|令牌|憑證|凭证|tokens))',
            ln, re.I)), len(lines))

        def percentage(line):
            m = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:used\b|已(?:使)?用)', line, re.I)
            if not m:
                m = re.search(r'已(?:使)?用\s*(\d+(?:\.\d+)?)\s*%', line)
            if not m:
                return None
            value = float(m.group(1))
            if not 0 <= value <= 100:
                return None
            return int(value) if value.is_integer() else value

        for ln in lines[:extra_at]:
            value = percentage(ln)
            if value is not None:
                out['weekly_used_pct'] = value
                break
        for ln in lines[extra_at:]:
            value = percentage(ln)
            if value is not None:
                out['extra_used_pct'] = value
                left = re.search(r'\(([^)]+)\)', ln)
                if left:
                    out['extra_left'] = left.group(1).strip()
                break
        if re.search(r'Never expires|永不過期|永不过期', text, re.I):
            out['extra_expires'] = 'never'
        # A plan label alone is not a successful quota read. Preserve old cache.
        out['found'] = 'weekly_used_pct' in out
        return out

    # ---------------- Attachments (generation results) ----------------
    _ATT_JS = (
        "(function(){"
        "var list = []; var seen = new Set();"
        "function addEl(el, tid){"
        "  if(!el || seen.has(el)) return;"
        "  seen.add(el);"
        "  if(el.closest('form, [class*=chat-user-bubble], [class*=\"group/msg\"]')) return;"
        "  var v = el.querySelector('video') || (el.tagName === 'VIDEO' ? el : null);"
        "  var img = el.querySelector('img') || (el.tagName === 'IMG' ? el : null);"
        "  var isVid = (tid || '').includes('video') || !!v;"
        "  var primary = isVid ? (v || img) : (img || v);"
        "  var src = primary ? (primary.currentSrc || primary.src || '') : '';"
        "  if(src && !seen.has(src)){"
        "    seen.add(src);"
        "    list.push({"
        "      tid: tid || el.getAttribute('data-testid') || (isVid ? 'video' : 'image'),"
        "      hasVideo: !!v,"
        "      hasImg: !!img,"
        "      src: src,"
        "      vSrc: v ? (v.currentSrc || v.src || '') : '',"
        "      iSrc: img ? (img.currentSrc || img.src || '') : '',"
        "      w: primary ? (primary.videoWidth || primary.naturalWidth || 0) : 0,"
        "      h: primary ? (primary.videoHeight || primary.naturalHeight || 0) : 0"
        "    });"
        "  }"
        "}"
        "document.querySelectorAll('[data-testid^=\"hatch-chat-attachment-presentation-\"]').forEach(function(a){ addEl(a, a.getAttribute('data-testid')); });"
        "document.querySelectorAll('div[class*=\"hatch-agent-bubble-bg\"] img, div[class*=\"hatch-agent-bubble-bg\"] video').forEach(function(m){"
        "  var s = m.currentSrc || m.src || '';"
        "  if(s && !s.includes('avatar') && !s.includes('emoji')) addEl(m.parentElement || m, 'agent-media');"
        "});"
        "return JSON.stringify(list);"
        "})()"
    )

    def attachments(self) -> list[dict]:
        try:
            raw = self.page.js(self._ATT_JS)
            return json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []

    # ---------------- Sending ----------------
    # Verify that text really entered the composer and that the Send button was really rendered.
    # Both conditions matter: the Send button renders only when React state contains text.
    # Its presence proves React received the input, not just that the DOM value changed.
    _SEND_STATE_JS = (
        "(function(){var ta=document.querySelector('textarea');"
        "var b=[...document.querySelectorAll('button,[role=button]')]"
        ".find(function(x){return /send/i.test(x.getAttribute('aria-label')||'');});"
        "return JSON.stringify({v:ta?ta.value:'',btn:b?(b.disabled?2:1):0});})()"
    )

    def _send(self, prompt: str):
        # 1. Ensure the textarea is scrolled into view and has real focus
        try:
            self.page.js("""(function(){
                var ta = document.querySelector('textarea');
                if (ta) {
                    ta.scrollIntoView({block: 'center', inline: 'nearest'});
                    ta.focus();
                }
            })()""")
        except Exception:
            pass
        time.sleep(0.1)

        rect = self.page.js(
            "(function(){var t=document.querySelector('textarea');if(!t)return null;"
            "var r=t.getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(r.left+r.width/2),"
            "y:Math.round(r.top+r.height/2)});})()")
        if not rect:
            raise MuseGenerationError("Chat input box not found")
        c = json.loads(rect)
        for t in ("mousePressed", "mouseReleased"):
            self.page.send("Input.dispatchMouseEvent",
                           {"type": t, "x": c["x"], "y": c["y"],
                            "button": "left", "clickCount": 1})
        time.sleep(0.1)

        # Trigger the React 18 prototype setter plus input/change events to sync the send-button state
        # (For 100KB+ contexts from DeepSeek/Codex, the prototype setter takes <1s and avoids Input.insertText character-by-character stalls.)
        _SETTER_JS = (
            "(function(t){var ta=document.querySelector('textarea');"
            "if(!ta) return 0;"
            "ta.focus();"
            "var s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
            "s.call(ta,t);"
            "ta.dispatchEvent(new Event('input',{bubbles:true}));"
            "ta.dispatchEvent(new Event('change',{bubbles:true}));"
            "return (ta.value||'').length;})(%s)")
        val_len = self.page.js(_SETTER_JS % json.dumps(prompt)) or 0
        if not val_len and len(prompt) < 500:
            self.page.send("Input.insertText", {"text": prompt})
            self.page.js(_SETTER_JS % json.dumps(prompt))

        # Wait for the send button to be ready and click it
        clicked = "no-button"
        t_deadline = time.time() + 3.0
        while time.time() < t_deadline:
            res = self.page.js(
                "(function(){var b=[...document.querySelectorAll('button,[role=button]')]"
                ".filter(function(x){return x.offsetParent!==null;})"
                ".find(function(x){return /发送|send/i.test(x.getAttribute('aria-label')||'')"
                "||/发送|send/i.test(x.getAttribute('data-testid')||'')"
                "||/send/i.test(x.innerText||'');});"
                "if(!b)return 'no-button';"
                "if(b.disabled)return 'disabled';"
                "b.click();return 'clicked';})()")
            if res == "clicked":
                clicked = "clicked"
                break
            time.sleep(0.08)

        if clicked != "clicked":
            # Fallback: Ctrl+Enter or plain Enter
            for combo in ({"modifiers": 1}, {}):
                for t in ("keyDown", "char", "keyUp"):
                    params = {"type": t, "key": "Enter", "code": "Enter",
                              "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                              "modifiers": combo.get("modifiers", 0)}
                    if t == "char":
                        params["text"] = "\r"
                        params["unmodifiedText"] = "\r"
                    self.page.send("Input.dispatchKeyEvent", params)
                time.sleep(1.0)
                try:
                    if self.page.js("!(document.querySelector('textarea')||{value:''}).value"):
                        clicked = "enter-sent"
                        break
                except Exception:
                    pass
        time.sleep(0.3)
        return clicked

    # ---------------- Waiting for generation ----------------
    def _last_attachment(self) -> dict | None:
        atts = self.attachments()
        return atts[-1] if atts else None

    def _scroll_bottom(self):
        """Scroll to the bottom of the chat. muse.ai's chat scroll container is an inner div (not document),
        and the virtual list renders nodes by scroll position; without scrolling to the bottom, new messages are not in the DOM at all."""
        try:
            self.page.js(
                "(function(){"
                "var els=[...document.querySelectorAll('*')].filter(function(e){"
                "var s=getComputedStyle(e);"
                "return (s.overflowY==='auto'||s.overflowY==='scroll')"
                "&&e.scrollHeight>e.clientHeight+100;});"
                "els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});"
                "if(els[0])els[0].scrollTop=els[0].scrollHeight;"
                "var s=document.scrollingElement||document.body;"
                "s.scrollTop=s.scrollHeight;"
                "var el=document.querySelector('textarea');"
                "if(el)el.scrollIntoView({block:'end'});return 1;})()")
        except Exception:  # noqa: BLE001
            pass

    def _wait_attachment(self, baseline_src: str, timeout: int, expect: str,
                         on_progress=None, base_agent_cnt: int = 0,
                         base_att_cnt: int = 0, stop_event=None, baseline_sources=None) -> dict | None:
        baseline_sources = set(baseline_sources or ()) | {baseline_src}
        deadline = time.time() + timeout
        t_start = time.time()
        stable_src, stable_n = "", 0
        last_txt, txt_stable = "", 0
        refusal_last, refusal_stable = "", 0
        seen_stop = False
        previous_state = None
        last_activity = t_start
        progress_sent = 0
        # Adaptive cadence: poll quickly while the page is changing (first token, new
        # attachment) and back off to the old 0.6 s once nothing moves. The previous
        # fixed 0.6 s added up to 0.6 s of dead time to every generation.
        poll_delay = 0.2
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("Client disconnected; aborting generation task")
            time.sleep(poll_delay)
            atts = self.attachments()
            # Strictly select attachments by expected type; never accept cross-modal fallbacks (e.g. treating a video request as an image cover)
            matching_atts = []
            for a in atts:
                tid = (a.get("tid") or "").lower()
                has_video = bool(a.get("hasVideo"))
                src = a.get("src") or ""
                v_src = a.get("vSrc") or ""
                if expect == "video":
                    # A tid containing "video" is not enough: muse first marks data-testid as
                    # ...-presentation-video while <video> may not be mounted yet, so _ATT_JS's
                    # primary = v || img falls back to the cover img, with src/w/h all still-frame values.
                    # Accept only a real video source (vSrc / hasVideo / video extension).
                    is_match = (
                        has_video
                        or bool(v_src)
                        or src.endswith((".mp4", ".webm", ".mov"))
                    )
                else:
                    is_match = ("image" in tid) or (not has_video and not src.endswith((".mp4", ".webm", ".mov")))
                if is_match:
                    matching_atts.append(a)

            att = matching_atts[-1] if matching_atts else None
            if att:
                src = att.get("src") or ""
                v_src = att.get("vSrc") or ""
                w = att.get("w", 0) or 0
                h = att.get("h", 0) or 0
                # Never let a video task deliver a cover src as the result; without vSrc, accept only src with an explicit video extension
                if expect == "video":
                    check_src = v_src if v_src else (
                        src if src.endswith((".mp4", ".webm", ".mov")) else ""
                    )
                else:
                    check_src = src
                if check_src and check_src not in baseline_sources:
                    if w > 0 and h > 0:
                        return att
                    if check_src == stable_src:
                        stable_n += 1
                    else:
                        stable_src, stable_n = check_src, 0
                    if stable_n >= 1:
                        return att
            elapsed = time.time() - t_start
            cur_cnt = base_agent_cnt
            cur_txt = ""
            has_stop = False
            tail = ""
            try:
                st_raw = self.page.js("""(function(){
                    var bs=[].slice.call(document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]'))
                        .filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});
                    var lastTxt = bs.length ? (bs[bs.length-1].innerText||'').trim() : '';
                    var hasStop = !!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
                        || document.querySelector('button[aria-label*="Stop" i]')
                        || document.querySelector('button[aria-label*="停止"]'));
                    var tail = document.body ? (document.body.innerText||'').slice(-1200) : '';
                    return JSON.stringify({cnt: bs.length, txt: lastTxt, stop: hasStop, tail: tail,
                        path: location.pathname || '', href: location.href || ''});
                })()""")
                st = json.loads(st_raw) if st_raw else {}
            except Exception:
                st = {}
            tail = st.get("tail") or ""
            cur_cnt = int(st.get("cnt") or 0)
            cur_txt = st.get("txt") or ""
            has_stop = bool(st.get("stop"))
            path = st.get("path") or ""
            seen_stop = seen_stop or has_stop
            state = (cur_cnt, cur_txt, len(atts), has_stop, path)
            changed = previous_state is None or state != previous_state
            if changed:
                last_activity = time.time()
                previous_state = state
                poll_delay = 0.2
            else:
                poll_delay = min(0.6, poll_delay * 1.5)
            # Always scroll: Muse lazily renders attachments into view, so skipping this
            # when idle could stop new media from ever appearing.
            self._scroll_bottom()

            if on_progress:
                milestone = 25 if seen_stop else 20
                if cur_cnt > base_agent_cnt:
                    milestone = 40
                if milestone > progress_sent:
                    try:
                        on_progress(milestone)
                        progress_sent = milestone
                    except Exception:
                        pass

            if re.search(r"额度不足|积分不足|out of credits|达到上限|token limit", tail, re.I):
                raise MuseGenerationError("Account quota exhausted")
            # A Stop button is also shown for messages never acknowledged by the gateway.
            if elapsed > 30.0 and cur_cnt == base_agent_cnt and re.search(
                    r"Still sending|Delivery not confirmed|正在发送|未确认送达", tail, re.I):
                raise MuseGenerationError(
                    "Prompt was not delivered to the backend (Still sending / Delivery not confirmed); stopping the wait and retrying")

            refusal_source = cur_txt if cur_cnt > base_agent_cnt else (
                tail if seen_stop and not has_stop and elapsed > 20.0 else "")
            refusal = self._media_refusal_text(refusal_source, expect)
            if refusal and not has_stop and len(atts) <= base_att_cnt:
                if refusal == refusal_last:
                    refusal_stable += 1
                else:
                    refusal_last, refusal_stable = refusal, 0
                if refusal_stable >= 4 and elapsed > 8.0:
                    raise MuseModelRefusalError(f"The model did not generate media and returned text only: {refusal}")
            else:
                refusal_last, refusal_stable = "", 0

            if cur_cnt > base_agent_cnt and cur_txt and not has_stop and len(atts) <= base_att_cnt:
                # Media-status text can precede a real attachment; do not fail on it.
                is_media_report = self._media_status_pending(cur_txt)
                if not is_media_report:
                    if cur_txt == last_txt:
                        txt_stable += 1
                    else:
                        last_txt, txt_stable = cur_txt, 0
                    if txt_stable >= 15 and elapsed > 8.0:
                        clean = self._clean_model_text(cur_txt)
                        raise MuseModelRefusalError(
                            f"The model did not generate media and returned text only: {clean}")
                else:
                    txt_stable = 0
            else:
                txt_stable = 0

            # Muse accepted the click but never created a thread. Requiring a completed
            # Stop-button lifecycle, an idle page, no messages/attachments and /thread/new
            # avoids treating ordinary long video rendering as failure.
            if (expect == "video" and seen_stop and not has_stop
                    and path.rstrip("/").endswith("/thread/new")
                    and cur_cnt <= base_agent_cnt and len(atts) <= base_att_cnt
                    and elapsed >= 180.0 and time.time() - last_activity >= 90.0):
                raise MuseGenerationError(
                    "Muse did not establish a video task thread; the page stayed unresponsive after submission (stopped waiting early)")
        return None

    @staticmethod
    def _clean_model_text(text: str, limit: int = 240) -> str:
        clean = re.sub(r"\s+", " ", text or "").strip()
        return clean[:limit]

    @staticmethod
    def _media_status_pending(text: str) -> bool:
        """A success claim can precede an attachment; it is never delivery proof."""
        if re.search(
                r"(?:未|没(?:有)?|无法|不能|未能)[^。！？!?\n]{0,16}(?:生成|制作|提供|完成)|"
                r"(?:生成|制作|渲染|处理)[^。！？!?\n]{0,12}(?:失败|未成功)|"
                r"\b(?:not|never|hasn['’]t|wasn['’]t|isn['’]t)\s+(?:(?:be|been|successfully)\s+){0,3}"
                r"(?:generated|created|completed|ready)|"
                r"\b(?:no|without)\s+(?:video|clip|image|picture|animation)\b"
                r"[^.\n]{0,60}\b(?:generated|created|ready)|"
                r"\b(?:failed|cannot|can['’]t|could not|couldn['’]t|unable to)\b"
                r"[^.\n]{0,60}\b(?:generat(?:e|ed|ing)|creat(?:e|ed|ing)|render|provide|video|image)\b|"
                r"\b(?:video|image|render|upload)\b[^.\n]{0,32}\bfailed\b",
                text, re.I):
            return False
        return bool(re.search(
            r"\.(?:webp|png|jpe?g|mp4|webm)|imagine_media|deliverable|"
            r"generated\s+.*image|verified\s+generated|artifact|"
            r"^(?:已(?:经)?(?:成功)?生成|生成好了|生成完毕|已完成|做好了)(?:[，。！\s]|$)|"
            r"(?:视频|影片|短片|图片|图像)[^。！？!?\n]{0,40}"
            r"(?:已(?:经)?(?:成功)?生成|生成(?:成功|好了|完毕|完成)|已(?:经)?完成|做好了)|"
            r"(?:已(?:经)?(?:成功)?生成|生成好了|生成完毕|已完成|做好了)"
            r"[^。！？!?\n]{0,32}(?:视频|影片|短片|图片|图像)|"
            r"\b(?:video|clip|image|picture|animation)\b[^.\n]{0,60}"
            r"\b(?:generated|created|rendered|completed|ready)\b|"
            r"\b(?:generated|created|rendered|completed)\b[^.\n]{0,32}"
            r"\b(?:video|clip|image|picture|animation)\b", text, re.I))

    @classmethod
    def _media_refusal_text(cls, text: str, expect: str) -> str:
        """Return only an explicit media refusal line, never an arbitrary page tail."""
        if not text:
            return ""
        patterns = [
            r"^(?:没生成成|视频没生成成功|视频未生成成功|视频生成失败)[^\n]{0,240}"
            r"(?:国家|地区|country|region)[^\n]{0,160}(?:不支持|不可用|无法|not available|unavailable|not supported)",
            r"^(?:could not|couldn't|cannot|can't|unable to)[^\n]{0,120}video[^\n]{0,160}"
            r"(?:country|region|not available|unavailable|not supported)",
        ] if expect == "video" else [
            r"^(?:图片没生成成功|图片未生成成功|图片生成失败|无法生成图片)",
            r"^(?:could not|couldn't|cannot|can't|unable to)[^\n]{0,120}image",
        ]
        for line in reversed((text or "").splitlines()):
            clean = cls._clean_model_text(line)
            if clean and any(re.search(pattern, clean, re.I) for pattern in patterns):
                return clean
        return ""

    # ---------------- Fetching bytes ----------------
    _EXTRACT_JS = r"""
    (async function(src, expect){
      try{
        var u = src;
        if(!u) return JSON.stringify({ok:false,err:'no-media-src'});
        var r = await fetch(u);
        if(!r.ok) return JSON.stringify({ok:false,err:'media-http-'+r.status});
        var b = await r.blob();
        var ab = await b.arrayBuffer();
        var bytes = new Uint8Array(ab);
        var s = '';
        for(var i=0; i<bytes.length; i+=65536){
          s += String.fromCharCode.apply(null, bytes.subarray(i, i+65536));
        }
        return JSON.stringify({ok:true, mime:b.type||'', size:b.size, url:u, b64:btoa(s)});
      }catch(e){
        return JSON.stringify({ok:false, err:String(e)});
      }
    })(%s, %s)
    """

    # ---------------- Text / code chat ----------------
    _AGENT_TEXT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "for(var i=bs.length-1;i>=0;i--){"
        "var cs=bs[i].className||'';"
        "if(/hatch-agent-bubble-bg/.test(cs))return bs[i].innerText||'';}"
        "return '';})()"
    )
    _USER_COUNT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "var n=0;for(var i=0;i<bs.length;i++){"
        "if(/chat-user-bubble/.test(bs[i].className||''))n++;}"
        "return String(n);})()"
    )
    _AGENT_COUNT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "var n=0;for(var i=0;i<bs.length;i++){"
        "if(/hatch-agent-bubble-bg/.test(bs[i].className||''))n++;}"
        "return String(n);})()"
    )
    _POLL_CHAT_JS = (
        "(function(){"
        "var els=[...document.querySelectorAll('*')].filter(function(e){"
        "var s=getComputedStyle(e);"
        "return (s.overflowY==='auto'||s.overflowY==='scroll')&&e.scrollHeight>e.clientHeight+100;});"
        "els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});"
        "if(els[0])els[0].scrollTop=els[0].scrollHeight;"
        "var scope=document.querySelector('main,[class*=\"chat-scroll\"],[class*=\"hatch-chat-scroll\"]')||document.body;"
        "var bs=[].slice.call(scope.querySelectorAll('div[class*=\"hatch-chat-groupable-bubble\"]'))"
        ".filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});"
        "var nonEmpty=bs.filter(function(b){return ((b.innerText||'').trim().length)>0;});"
        "var txt=nonEmpty.length?(nonEmpty[nonEmpty.length-1].innerText||'').trim():'';"
        "var stop=!!(document.querySelector('[data-testid=\"hatch-composer-stop-button\"]')"
        "||document.querySelector('button[aria-label*=\"Stop\" i]')"
        "||document.querySelector('button[aria-label*=\"停止\"]'));"
        "return JSON.stringify({cnt:nonEmpty.length,total:bs.length,txt:txt,stop:stop});})()"
    )

    def _agent_text(self) -> str:
        """Text of the last assistant bubble (empty string if unavailable)."""
        try:
            return (self.page.js(self._AGENT_TEXT_JS) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _agent_count(self) -> int:
        try:
            return int(self.page.js(self._AGENT_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _user_count(self) -> int:
        try:
            return int(self.page.js(self._USER_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _poll_chat(self) -> tuple[int, str, bool]:
        try:
            raw = self.page.js(self._POLL_CHAT_JS)
            if raw:
                d = json.loads(raw)
                return int(d.get("cnt") or 0), (d.get("txt") or "").strip(), bool(d.get("stop"))
        except Exception:
            pass
        return 0, "", False

    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,
                    timeout: int | None = None, account_id: str | None = None,
                    stop_event=None):
        """Send a message and stream incremental text."""
        timeout = int(timeout or getattr(self.cfg, "chat_timeout", 300))
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=True)
        base_agent, base_text, _ = self._poll_chat()
        self._send(prompt)

        t_sent = time.time()
        deadline = t_sent + timeout
        first_token_deadline = min(deadline, t_sent + 40.0)
        sent, last, stable = "", None, 0
        got_first = False

        # 1. Wait for the assistant to start producing text (60ms sampling; yield the first delta immediately)
        while time.time() < first_token_deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(0.06)
            cnt, cur, has_stop = self._poll_chat()
            if not cur or (cnt <= base_agent and cur == base_text):
                if time.time() - t_sent > 14.0:
                    try:
                        tail = self.page.js("document.body.innerText.slice(-500)") or ""
                    except Exception:
                        tail = ""
                    if "Still sending" in tail or "Connecting..." in tail:
                        raise MuseGenerationError("Cloud VM connection timed out (Still sending)")
                continue

            delta = cur[len(sent):] if cur.startswith(sent) else cur
            if delta:
                sent = cur
                yield delta
                last = cur
                got_first = True
                break

        if not got_first:
            raise MuseGenerationError("Timed out waiting for the assistant's first token")

        # 2. Continuously capture incremental text
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(0.06)
            cnt, cur, has_stop = self._poll_chat()
            if not cur:
                continue

            if cur != last:
                delta = cur[len(sent):] if cur.startswith(sent) else cur
                if delta:
                    sent = cur
                    yield delta
                last, stable = cur, 0
            else:
                stable += 1
                # The Stop button disappearing means frontend generation has finished; exit normally after 3 stable samples (~0.18s)
                if not has_stop and stable >= 3:
                    return
                # While the Stop button remains, never truncate early (model thinking, code blocks, or network jitter); allow up to stable >= 80 (~5s) to avoid hangs
                if has_stop and stable >= 80:
                    return

        raise MuseGenerationError("Timed out waiting for the assistant reply")

    def chat(self, cookies: dict, prompt: str, expires: dict | None = None,
             timeout: int | None = None, account_id: str | None = None) -> str:
        """Send a message and return the full reply text (non-streaming)."""
        out = ""
        for chunk in self.chat_stream(cookies, prompt, expires, timeout, account_id=account_id):
            out += chunk
        return out

    def extract_bytes(self, src: str, expect: str = "image", retries: int = 4):
        last = "unknown"
        for _ in range(retries):
            raw = self.page.js(self._EXTRACT_JS % (json.dumps(src), json.dumps(expect)),
                               await_promise=True, timeout=600)
            try:
                info = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:  # noqa: BLE001
                info = {"ok": False, "err": f"parse failed {str(raw)[:150]}"}
            if info.get("ok"):
                return base64.b64decode(info["b64"]), info.get("mime", ""), info.get("url", "")
            last = info.get("err", "unknown")
            time.sleep(2)
        raise MuseGenerationError(f"Failed to retrieve generation result: {last}")

    # ---------------- Download fallback ----------------
    def _download_fallback(self, src: str, timeout: int = 180) -> str | None:
        directory = self.download_dir
        before = set(os.listdir(directory))
        # ponytail: fail closed when the selected result has no local download;
        # never click an unrelated/global button that can return the upload.
        clicked = self.page.js("""(function(src){
            var media=[...document.querySelectorAll('img,video')]
                .find(m=>(m.currentSrc||m.src||'')===src);
            var node=media && media.closest('[data-testid^="hatch-chat-attachment-presentation-"]');
            if(!node) return 'none';
            node=node.closest('[class*="group/widget-presentation"]')||node;
            var b=[...node.querySelectorAll('button,[role=button]')]
                .find(x=>/下载|保存|download/i.test(x.getAttribute('aria-label')||''));
            if(!b) return 'none';
            b.click();return 'ok';
        })(%s)""" % json.dumps(src))
        if clicked == "none":
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.5)
            new = [f for f in (set(os.listdir(directory)) - before)
                   if not f.endswith(".crdownload")]
            if new:
                p = os.path.join(directory, max(
                    new, key=lambda f: os.path.getmtime(os.path.join(directory, f))))
                if os.path.getsize(p) > 0:
                    return p
        return None

    def _normalize_image(self, img: str) -> tuple[str, str]:
        """Normalize one reference file (image/video/audio/document) and enforce
        source, MIME, size and video-duration policy."""
        if not img:
            return "", "image/png"
        img = str(img).strip()

        def checked(data: bytes, mime: str, fallback: str = "image/png",
                    name: str = "") -> tuple[str, str]:
            mime = (mime or "").split(";", 1)[0].strip().lower()
            mime = _MIME_ALIASES.get(mime, mime)
            if mime in _GENERIC_MIMES:
                mime = (_sniff_mime(data)
                        or _MIME_ALIASES.get(mimetypes.guess_type(name)[0] or "",
                                             mimetypes.guess_type(name)[0] or "")
                        or "")
            if mime not in REFERENCE_MIME_EXT:
                raise MuseGenerationError(
                    f"Reference file type is not supported ({mime or 'unknown'}); "
                    "supported: images, video (mp4/mov/webm/3gp), audio (mp3/wav/m4a/ogg/flac/aac/opus), and PDF/Word")
            if len(data) > MAX_REFERENCE_IMAGE_BYTES:
                raise MuseGenerationError(
                    f"Single reference asset exceeds the {MAX_REFERENCE_IMAGE_BYTES // _MIB} MiB limit")
            if mime.startswith("video/"):
                seconds = _webm_duration(data) if mime == "video/webm" else _mp4_duration(data)
                if seconds is None or not math.isfinite(seconds) or seconds <= 0:
                    raise MuseGenerationError("Cannot confirm reference video duration; provide an MP4/MOV/WebM/3GP file with valid duration metadata")
                if seconds > MAX_REFERENCE_VIDEO_SECONDS:
                    raise MuseGenerationError(
                        f"Reference video is {seconds:.0f}s, exceeding the {MAX_REFERENCE_VIDEO_SECONDS}s limit")
            return base64.b64encode(data).decode("ascii"), mime

        if img.startswith("data:"):
            head, sep, payload = img.partition(",")
            if not sep:
                raise MuseGenerationError("Invalid reference Data URL format")
            mime = head[5:].split(";", 1)[0]
            try:
                data = (base64.b64decode(payload.strip(), validate=True)
                        if ";base64" in head.lower()
                        else urllib.parse.unquote_to_bytes(payload))
            except (binascii.Error, ValueError) as exc:
                raise MuseGenerationError("Invalid reference Data URL encoding") from exc
            return checked(data, mime)

        if img.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(img, headers={"User-Agent": "Mozilla/5.0"})
                response = (urllib.request.urlopen(req, timeout=20)
                            if self.cfg.ref_image_private_fetch
                            else _open_public_ref_image(img))
                with response as resp:
                    declared = resp.headers.get("Content-Type")
                    mime = resp.headers.get_content_type() if declared else ""
                    data = resp.read(MAX_REFERENCE_IMAGE_BYTES + 1)
                return checked(data, mime, name=urllib.parse.urlsplit(img).path)
            except (RefImageSafetyError, MuseGenerationError):
                raise
            except Exception as exc:
                raise MuseGenerationError(
                    f"Failed to download remote reference media: {type(exc).__name__}") from exc

        # Raw Base64 contains no server-side path and remains safe in restricted mode.
        try:
            return checked(base64.b64decode(img, validate=True), "")
        except (binascii.Error, ValueError):
            pass

        if not self.cfg.ref_image_local_files:
            raise RefImageSafetyError(
                "Reference local paths are rejected; to read server files, explicitly set "
                "MUSE2API_REF_IMAGE_LOCAL_FILES=1")
        if os.path.isfile(img):
            try:
                if os.path.getsize(img) > MAX_REFERENCE_IMAGE_BYTES:
                    raise MuseGenerationError(
                        f"Single reference asset exceeds the {MAX_REFERENCE_IMAGE_BYTES // _MIB} MiB limit")
                with open(img, "rb") as f:
                    return checked(f.read(MAX_REFERENCE_IMAGE_BYTES + 1),
                                   mimetypes.guess_type(img)[0] or "", name=img)
            except MuseGenerationError:
                raise
            except Exception as exc:
                raise MuseGenerationError(
                    f"Failed to read local reference media: {type(exc).__name__}") from exc
        raise MuseGenerationError("Reference media must be a URL, Data URL, local file, or valid Base64")

    def _clear_attachments(self):
        """Clear leftover attachment thumbnails in the chat composer (handles Chinese/English UI)."""
        try:
            self.page.js(
                "(function(){"
                "var btns=Array.from(document.querySelectorAll('button[aria-label]')).filter(function(b){"
                "var al=b.getAttribute('aria-label')||'';"
                "return /remove\\s*attachment|移除附件|删除附件|移除|删除/i.test(al);"
                "});"
                "btns.forEach(function(b){b.click();});"
                "Array.from(document.querySelectorAll('input[type=\"file\"]')).forEach(function(inp){inp.value='';});"
                "return btns.length;"
                "})()")
            time.sleep(0.3)
        except Exception:
            pass

    # The new muse page no longer has hatch-composer-placeholder-overlay and does not wrap a <form>:
    # the composer container is two levels above the textarea, where the attachment's "Remove attachment" button lives.
    # Prefer the old structure (overlay / form); for the new structure, walk up from the textarea to the nearest layer.
    _COMPOSER_ROOTS_JS = """
        function __composerRoots(){
            var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
            var legacy = ov ? (ov.closest('form') || ov.parentElement.parentElement.parentElement) : null;
            if (legacy) return [legacy];
            var ta = document.querySelector('textarea');
            if (!ta) return [];
            var form = ta.closest('form');
            if (form) return [form];
            var out = [], el = ta.parentElement;
            for (var i = 0; i < 4 && el && el !== document.body; i++) { out.push(el); el = el.parentElement; }
            return out;
        }
    """

    _FILE_INPUT_JS = "(function(){" + _COMPOSER_ROOTS_JS + """
        var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
        var roots = __composerRoots();
        for (var r of roots) {
            var hit = inputs.find(function(x){ return r.contains(x); });
            if (hit) return hit;
        }
        return null;
    })()"""

    _ATTACHED_COUNT_JS = "(function(){" + _COMPOSER_ROOTS_JS + """
        var rx = /remove\\s*attachment|移除附件|删除附件/i;
        var bubble = '[class*=chat-user-bubble], [class*="group/msg"], [class*=hatch-chat-groupable-bubble]';
        var roots = __composerRoots();
        for (var r of roots) {
            var n = Array.from(r.querySelectorAll('button[aria-label]')).filter(function(b){
                return rx.test(b.getAttribute('aria-label') || '') && !b.closest(bubble);
            }).length;
            if (n) return n;
        }
        return 0;
    })()"""

    def _set_input_files(self, paths: list[str]):
        """Hand disk files directly to the upload control via CDP (large videos no longer go through base64 injection)."""
        res = self.page.send("Runtime.evaluate", {
            "expression": self._FILE_INPUT_JS, "returnByValue": False})
        object_id = (res.get("result", {}).get("result", {}) or {}).get("objectId")
        if not object_id:
            raise MuseGenerationError("Upload control not found; generation stopped")
        self.page.send("DOM.setFileInputFiles",
                       {"files": paths, "objectId": object_id}, timeout=60)

    def _attach_images(self, image_data: list[str]) -> dict | None:
        """Attach reference media (image/video/audio/document) one by one and confirm all entered the composer.

        Returns {"dir": temp directory, "hashes": set of media sha256}; the caller deletes the
        temp directory after generation (Chromium reads file content at send time) and uses hashes to
        prevent treating just-uploaded references as generated results.
        """
        if len(image_data) > MAX_REFERENCE_IMAGES:
            raise MuseGenerationError(f"At most {MAX_REFERENCE_IMAGES} reference assets are allowed")
        files = []
        total_bytes = 0
        for index, value in enumerate(image_data):
            if not value:
                continue
            b64, mime = self._normalize_image(value)
            if not b64:
                raise MuseGenerationError(f"Failed to read reference asset #{index + 1}; generation stopped")
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MuseGenerationError(f"Invalid encoding for reference asset #{index + 1}") from exc
            total_bytes += len(data)
            if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
                raise MuseGenerationError(
                    f"Total reference media exceeds the {MAX_REFERENCE_TOTAL_BYTES // _MIB} MiB limit")
            files.append((index + 1, data, mime))
        if not files:
            return None

        self._clear_attachments()
        # Snap Chromium cannot read the host's /tmp; profile_dir is shared with it.
        os.makedirs(self.cfg.profile_dir, exist_ok=True)
        upload_dir = tempfile.mkdtemp(prefix="muse2api-refs-", dir=self.cfg.profile_dir)
        hashes = set()
        try:
            for index, data, mime in files:
                path = os.path.join(
                    upload_dir, f"reference_{index}{REFERENCE_MIME_EXT.get(mime, '.bin')}")
                with open(path, "wb") as f:
                    f.write(data)
                hashes.add(hashlib.sha256(data).hexdigest())
                self._set_input_files([path])
                time.sleep(0.35)
        except MuseGenerationError:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        except Exception as e:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise MuseGenerationError("Failed to attach reference media; generation stopped") from e

        try:
            # A remove button proves attachment presence, not upstream processing success.
            deadline = time.monotonic() + 30.0
            attached_count = 0
            while time.monotonic() < deadline:
                attached_count = self.page.js(self._ATTACHED_COUNT_JS)
                if int(attached_count or 0) >= len(files):
                    break
                time.sleep(0.3)
            else:
                raise MuseGenerationError(
                    f"Reference media upload was not confirmed ({int(attached_count or 0)}/{len(files)}); generation stopped")
            time.sleep(0.5)
            return {"dir": upload_dir, "hashes": hashes}
        except Exception:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise

    def _attach_image(self, image_data: str):
        """Backward-compatible single-reference wrapper."""
        self._attach_images([image_data])
    # ---------------- Main flow ----------------
    def generate(self, cookies: dict, prompt: str, expect: str = "image",
                 timeout: int = 240, expires: dict | None = None, account_id: str | None = None,
                 on_progress=None, reference_image: str | None = None,
                 reference_images: list[str] | None = None,
                 stop_event=None) -> dict:
        deadline = time.monotonic() + timeout
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=False)
        self._scroll_bottom()
        refs = list(reference_images or ([] if not reference_image else [reference_image]))
        upload = self._attach_images(refs) if refs else None
        if not refs:
            self._clear_attachments()
        try:
            return self._generate_attached(prompt, expect, deadline, on_progress,
                                           stop_event, (upload or {}).get("hashes") or set())
        finally:
            if upload:
                shutil.rmtree(upload["dir"], ignore_errors=True)

    def _generate_attached(self, prompt: str, expect: str, deadline: float,
                           on_progress, stop_event, upload_hashes: set) -> dict:
        atts_before = self.attachments()
        baseline_sources = {a.get(k) for a in atts_before for k in ("src", "vSrc", "iSrc") if a.get(k)}
        base = atts_before[-1] if atts_before else {}
        baseline_src = base.get("src") or ""
        base_agent_cnt = self._agent_count()
        if time.monotonic() >= deadline:
            raise MuseGenerationError("Task preparation timed out; sending stopped")
        if self._send(prompt) not in ("clicked", "enter-sent"):
            raise MuseGenerationError("Prompt delivery was not confirmed; generation stopped")
        os.makedirs(self.cfg.media_dir, exist_ok=True)
        # If the page renders just-uploaded media as a "result", identify it by hash and keep waiting for the real output
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("Task cancelled")
            att = self._wait_attachment(
                baseline_src, max(0, deadline - time.monotonic()), expect, on_progress=on_progress,
                base_agent_cnt=base_agent_cnt, base_att_cnt=len(atts_before),
                stop_event=stop_event, baseline_sources=baseline_sources
            )
            if not att:
                self._debug_dump("no-attachment")
                raise MuseGenerationError("Generation timed out; no new result appeared")

            data = mime = url = None
            selected_src = (att.get("vSrc") if expect == "video" else None) or att.get("src") or ""
            try:
                data, mime, url = self.extract_bytes(selected_src, expect=expect)
            except Exception:  # noqa: BLE001
                self._debug_dump("extract-fail")

            if data and hashlib.sha256(data).hexdigest() in upload_hashes:
                log.warning("Attachment matched this request's uploaded reference media; ignoring and waiting: %s", selected_src[:80])
                baseline_sources |= {att.get(k) for k in ("src", "vSrc", "iSrc") if att.get(k)}
                time.sleep(0.1)
                continue

            if data:
                ext = self._pick_ext(mime, url, expect)
                name = f"{uuid.uuid4().hex}{ext}"
                dst = os.path.join(self.cfg.media_dir, name)
                with open(dst, "wb") as f:
                    f.write(data)
                kind = "video" if ext in (".mp4", ".webm", ".mov") else "image"
                if expect == "video" and kind != "video":
                    raise MuseGenerationError(f"Expected a video but extracted an image ({ext}); refusing to deliver an image as video")
                return {"path": dst, "filename": name, "size": len(data), "ext": ext, "mime": mime,
                        "kind": kind,
                        "via": "blob", "attachment": att.get("tid"),
                        "w": att.get("w"), "h": att.get("h")}

            path = self._download_fallback(selected_src)
            if not path:
                raise MuseGenerationError("Generated, but the file could not be retrieved")
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            if digest in upload_hashes:
                os.remove(path)
                log.warning("Downloaded file is this request's uploaded reference media; ignoring and waiting")
                baseline_sources |= {att.get(k) for k in ("src", "vSrc", "iSrc") if att.get(k)}
                time.sleep(0.1)
                continue
            ext = os.path.splitext(path)[1].lower() or ".bin"
            name = f"{uuid.uuid4().hex}{ext}"
            dst = os.path.join(self.cfg.media_dir, name)
            shutil.move(path, dst)
            kind = "video" if ext in (".mp4", ".webm", ".mov") else "image"
            if expect == "video" and kind != "video":
                    raise MuseGenerationError(f"Expected a video but downloaded an image ({ext}); refusing to deliver an image as video")
            return {"path": dst, "filename": name, "size": os.path.getsize(dst), "ext": ext, "mime": "",
                    "kind": kind,
                    "via": "download", "attachment": att.get("tid"),
                    "w": att.get("w"), "h": att.get("h")}
        raise MuseGenerationError("The page returned only this request's uploaded reference media; no new result appeared")

    @staticmethod
    def _pick_ext(mime: str, url: str, expect: str) -> str:
        m = (mime or "").lower()
        for key, ext in (("mp4", ".mp4"), ("webm", ".webm"), ("png", ".png"),
                         ("jpeg", ".jpg"), ("jpg", ".jpg"), ("webp", ".webp"),
                         ("gif", ".gif")):
            if key in m:
                return ext
        for e in (".mp4", ".webm", ".png", ".jpg", ".webp"):
            if e in (url or "").lower():
                return e
        return ".mp4" if expect == "video" else ".png"

    def _debug_dump(self, tag: str):
        try:
            info = self.page.js(
                "JSON.stringify({atts:[...document.querySelectorAll('" + ATT_SEL + "')]"
                ".map(function(a){var m=a.querySelector('img,video');return {"
                "tid:a.getAttribute('data-testid'),"
                "src:m?(m.currentSrc||m.src||'').slice(0,60):''};}),"
                "buttons:[...document.querySelectorAll('button,[role=button]')]"
                ".filter(b=>b.offsetParent!==null)"
                ".map(b=>b.getAttribute('aria-label')||b.innerText.trim().slice(0,20))"
                ".filter(Boolean).slice(-40),"
                "tail:document.body.innerText.slice(-500)})")
            with open(os.path.join(self.cfg.data_dir, f"debug-{tag}.json"), "w",
                      encoding="utf-8") as f:
                f.write(str(info))
        except Exception:  # noqa: BLE001
            pass
