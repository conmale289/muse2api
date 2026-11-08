"""muse2api — reverse-proxies the chat/image/video quota of free Muse (muse.ai) web accounts into an API.

OpenAI-compatible:
  POST /v1/chat/completions          (text/code chat, streaming supported, works with Codex)
  POST /v1/images/generations
  POST /v1/videos  +  GET /v1/videos/{task_id}
  GET  /v1/models
  GET  /v1/media/{name}

Admin (the account-pool UI is served at GET /):
  GET    /admin/status
  GET    /admin/accounts
  POST   /admin/accounts            (single / batch text / batch array)
  PATCH  /admin/accounts/{id}       (change label, enable/disable)
  DELETE /admin/accounts/{id}
  POST   /admin/accounts/{id}/test  (really open muse.ai to verify the session)
  POST   /admin/accounts/{id}/relogin
  GET    /admin/tasks               (task records)
  DELETE /admin/tasks/{id}  |  POST /admin/tasks/clear
  GET    /admin/media  |  POST /admin/media/delete  |  DELETE /admin/media/{name}
  GET    /admin/extension           (browser-extension zip for cookie capture)
  GET    /admin/cookie-helper       (command-line cookie helper script, advanced)
"""
from __future__ import annotations

from typing import Any

import asyncio
import copy
from contextlib import contextmanager
import base64
import collections
import binascii
import io
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
import zipfile

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import model_validator, BaseModel, Field, SecretStr, field_validator

from config import CFG
from engine import (ESSENTIAL_COOKIES, MAX_REFERENCE_IMAGE_BYTES,
                    MAX_REFERENCE_IMAGES, MAX_REFERENCE_TOTAL_BYTES,
                    REFERENCE_MIME_EXT, MuseAuthError, MuseEngine, MuseGenerationError,
                    MuseModelRefusalError, RefImageSafetyError, reference_kind,
                    _mp4_duration)
from engine import MuseProxyError
from store import Store, account_expiry, min_expiry
from proxy import normalize_server, request_proxy_context, split_proxy_url

import sys
log = logging.getLogger("muse2api")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)

try:
    CFG.ensure_dirs()
except OSError as exc:  # noqa: BLE001
    # The usual cause is an upgrade from the root-running image: the ./data bind mount
    # is still owned by root and the new unprivileged user cannot create its subdirs.
    log.error(
        "Cannot create the data directories under %s (%s). If you upgraded from a "
        "container that ran as root, hand the data directory over once: "
        "`sudo chown -R 1000:1000 data` (or rebuild with MUSE2API_UID/MUSE2API_GID).",
        CFG.data_dir, exc)
    raise


def _declared_version() -> str:
    """Read version.json so the OpenAPI version cannot drift from the release metadata."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "version.json"), encoding="utf-8") as handle:
            return str(json.load(handle).get("version") or "0.0.0")
    except Exception:  # noqa: BLE001
        return "0.0.0"


app = FastAPI(title="muse2api", version=_declared_version())

# Origins are open by default: this service authenticates with a Bearer key, not cookies,
# so allowing any origin adds no privilege risk, and restricting them would block
# browser-based agents (Open WebUI / LobeChat / various web clients) via CORS.
#
# Setting MUSE2API_CORS_ORIGINS to a comma-separated list narrows this. An empty value
# means "allow any origin" — not "allow none".
def _allowed_origins(raw: str | None) -> list[str]:
    """Parse MUSE2API_CORS_ORIGINS. An empty value means "allow any origin".

    Setting the variable to a comma-separated list narrows the policy; leaving it empty
    (or unset) keeps the permissive default that browser-based agents rely on.
    """
    origins = [origin.strip() for origin in (raw or "").split(",") if origin.strip()]
    return origins or ["*"]


_origins = _allowed_origins(CFG.cors_origins)
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware,
                   allow_origins=_origins,
                   allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)

store = Store(CFG)
engine = MuseEngine(CFG)
engine.proxy_resolver = lambda aid: (store.get_account(aid) or {}).get("proxy")


class ConcurrencyLock:
    """Multi-account concurrency scheduling lock.
    Maximum concurrent slots are bounded by CFG.max_concurrency;
    also provides acquire / release / locked interfaces compatible with a standard Lock,
    keeping legacy single-lock assertions and admin operations working."""
    def __init__(self, limit: int = 2):
        self.limit = max(1, limit)
        self._sem = threading.Semaphore(self.limit)
        self._count_lock = threading.Lock()
        self._active_count = 0

    def acquire(self, blocking: bool = True, timeout: float | None = -1) -> bool:
        if timeout is None:
            timeout = -1
        if timeout < 0:
            ok = self._sem.acquire(blocking=blocking)
        else:
            ok = self._sem.acquire(blocking=blocking, timeout=timeout)
        if ok:
            with self._count_lock:
                self._active_count += 1
        return ok

    def release(self):
        with self._count_lock:
            if self._active_count > 0:
                self._active_count -= 1
        self._sem.release()

    def locked(self) -> bool:
        with self._count_lock:
            return self._active_count > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AccountPoolManager:
    """Account concurrency conflict manager: prevents two concurrent tasks from using the same account, while different accounts run in parallel."""
    def __init__(self, store_instance):
        self.store = store_instance
        self.busy_accounts: set[str] = set()
        self.cond = threading.Condition()

    def acquire(self, account_id: str | None = None, preferred_id: str | None = None,
                rotate: bool = True, force_rotate: bool = False,
                timeout: float = 60.0, record_use: bool = True) -> dict:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MuseGenerationError("Timed out waiting for an idle account; all accounts are currently busy")

                if account_id:
                    acc = self.store.get_account(account_id)
                    if not acc or not acc.get("enabled", True):
                        raise MuseAuthError(f"Account {account_id} does not exist or is disabled")
                    if account_id not in self.busy_accounts:
                        self.busy_accounts.add(account_id)
                        return acc
                else:
                    pref = preferred_id if preferred_id not in self.busy_accounts else None
                    acc = self.store.pick_account(
                        preferred_id=pref,
                        rotate=rotate,
                        force_rotate=force_rotate,
                        exclude_ids=self.busy_accounts,
                        record_use=record_use,
                    )
                    if acc:
                        self.busy_accounts.add(acc["id"])
                        return acc

                live = [a for a in self.store.accounts if a.get("enabled", True) and a.get("cookies")]
                if not live:
                    raise MuseAuthError("No available account; import cookies on the admin page first")

                self.cond.wait(timeout=min(remaining, 0.5))

    def release(self, account_id: str | None):
        if not account_id:
            return
        with self.cond:
            self.busy_accounts.discard(account_id)
            self.cond.notify_all()


GEN_LOCK = ConcurrencyLock(limit=CFG.max_concurrency)
ACCOUNT_POOL = AccountPoolManager(store)
IMAGE_TASK_LOCK = threading.Lock()
# Admission and idempotency both read/modify task state, so they share one mutex.
TASK_ADMISSION_LOCK = IMAGE_TASK_LOCK
# Chat/responses have no persisted task row; count in-flight requests directly.
# Guarded by TASK_ADMISSION_LOCK, same mutex image/video admission already shares.
_CHAT_ACTIVE = 0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bind_account_proxy(account: dict):
    """Pin the selected account's route for the whole operation, including retries."""
    if hasattr(engine, "_ensure_local"):
        engine._ensure_local().proxy_snapshot = copy.deepcopy(account.get("proxy")) or None


def _clear_account_proxy():
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            del local.proxy_snapshot


def _account_proxy(account: dict):
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            return local.proxy_snapshot
    return account.get("proxy")


@contextmanager
def _account_operation(aid: str):
    """Maintenance and settings share the same account lease as generation."""
    with ACCOUNT_POOL.cond:
        account = store.get_account(aid)
        if not account:
            raise HTTPException(404, "Account not found")
        if aid in ACCOUNT_POOL.busy_accounts:
            raise HTTPException(409, "Account is running a task or check; try again later")
        ACCOUNT_POOL.busy_accounts.add(aid)
        account = copy.deepcopy(account)
    try:
        _bind_account_proxy(account)
        yield account
    finally:
        try:
            engine.close_current_page()
        finally:
            _clear_account_proxy()
            ACCOUNT_POOL.release(aid)


# ------------------------- OpenAI-style error responses -------------------------
# Most agents read errors from OpenAI's {"error": {"message": ...}} shape;
# FastAPI returns {"detail": ...} by default, so clients cannot see the reason and show "unknown error".
# So /v1/* is converted to the OpenAI shape; admin endpoints stay as-is (the UI relies on detail).
def _err_type(status: int) -> str:
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    # Never echo user-supplied proxy credentials in validation errors.
    errors = [{k: v for k, v in error.items() if k not in ("input", "ctx")}
              for error in exc.errors()]
    if request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "message": "Request validation failed: " + str(errors)[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": errors})

MODELS = [
    {"id": "muse-spark", "object": "model", "owned_by": "muse",
     "description": "Muse Spark — text/code chat (free web quota, streaming supported)",
     "context_window": 32768, "supports_streaming": True, "supports_vision": False,
     "modalities": ["text"]},
    {"id": "muse-image", "object": "model", "owned_by": "muse",
     "description": "Muse Image — text-to-image / image editing (free web quota)",
     "context_window": 4096, "supports_streaming": False, "supports_vision": True,
     "modalities": ["text", "image"]},
    {"id": "muse-video", "object": "model", "owned_by": "muse",
     "description": "Muse Video — text-to-video / image-to-video (free web quota)",
     "context_window": 4096, "supports_streaming": False, "supports_vision": True,
     "modalities": ["text", "image", "video"]},
]

# Downstream clients (Codex / Cline / others) pass OpenAI or Anthropic model names,
# so map them all onto Muse's real capabilities.
#
# Note: the muse.ai web app is an auto-routing agent and exposes no enumerable model list;
# only three real capabilities are reliably callable — Muse Spark (language/code),
# Muse Image (images), and Muse Video (videos). Aliases just save downstream config changes.
MODEL_ALIASES = {
    # ---- Text / code -> muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    "gpt-3.5-turbo": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4.1": "muse-spark",
    "gpt-4.1-mini": "muse-spark", "gpt-5": "muse-spark", "gpt-5-codex": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o3": "muse-spark",
    "o3-mini": "muse-spark", "o4-mini": "muse-spark",
    "codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark",
    "claude-3-haiku": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- Image generation -> muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    # ---- Video generation -> muse-video ----
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}

# Prefix rules applied after the exact-alias table. Keeps new model names working
# without maintaining a list of every release.
_MODEL_PREFIXES = (
    ("gpt-image", "muse-image"), ("dall-e", "muse-image"), ("dalle", "muse-image"),
    ("flux", "muse-image"), ("midjourney", "muse-image"), ("stable-diffusion", "muse-image"),
    ("sora", "muse-video"), ("veo", "muse-video"), ("kling", "muse-video"),
    ("runway", "muse-video"), ("pika", "muse-video"),
)


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    """Map a client model name onto one of the three real Muse capabilities.

    Prefix rules keep this forward compatible: a new "gpt-image-2" or "sora-3" is
    classified correctly without touching the alias table. Unknown names pass through
    unchanged, matching the previous behaviour.
    """
    n = (name or "").strip().lower()
    if not n:
        return default
    mapped = MODEL_ALIASES.get(n)
    if mapped:
        return mapped
    for prefix, target in _MODEL_PREFIXES:
        if n.startswith(prefix):
            return target
    return n


# ------------------------- Authentication -------------------------
def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    parts = authorization.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _key_matches(token: str, expected: str) -> bool:
    """Constant-time comparison; an empty expected key never matches."""
    if not token or not expected:
        return False
    return secrets.compare_digest(token, expected)


def auth(authorization: str | None = Header(default=None)):
    if not CFG.api_key:
        return True
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(401, "Missing Authorization: Bearer <key>")
    if not _key_matches(token, CFG.api_key):
        raise HTTPException(401, "Invalid API key")
    return True


def maintainer_auth(authorization: str | None = Header(default=None)):
    """Credential for admin actions that change code or credentials.

    MUSE2API_MAINTAINER_KEY separates this from the inference key that every downstream
    client holds. When it is unset the inference key is accepted, so existing installs
    keep working — but that exposure is logged at startup.
    """
    expected = CFG.maintainer_key or CFG.api_key
    if not expected:
        return True
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(401, "Missing Authorization: Bearer <key>")
    if not _key_matches(token, expected):
        raise HTTPException(401, "Invalid maintainer key")
    return True


def repo_push_auth(authorization: str | None = Header(default=None)):
    """Pushing to the upstream repository always requires the dedicated key.

    This endpoint can publish arbitrary code to the shared GitHub repo, so it must not be
    reachable with the inference key that every client already has.
    """
    if not CFG.maintainer_key:
        raise HTTPException(
            403,
            "Set MUSE2API_MAINTAINER_KEY to enable /admin/repo/push. The inference API key "
            "is shared with every downstream client and must not be able to push code.")
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(401, "Missing Authorization: Bearer <key>")
    if not _key_matches(token, CFG.maintainer_key):
        raise HTTPException(401, "Invalid maintainer key")
    return True


# ------------------------- Request models -------------------------

def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """Call /api/session to renew the account cookies, write them back to the store, and return the latest account dict.
    Accounts renewed recently (within 10 minutes) and in good state are reused, avoiding a 2-3s HTTP round trip on every request."""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm,
                                        **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"),
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"Session healthy (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except MuseProxyError:
        raise
    except Exception as exc:
        log.warning("HTTP pre-renewal for account %s failed: %s", acc_id, exc)
    return store.get_account(acc_id)


def _mark_chat_account(state: dict, ok: bool, note: str = "") -> None:
    """Record a chat outcome against whichever account the pool actually leased."""
    account_id = (state or {}).get("account_id")
    if account_id:
        store.mark(account_id, ok, note)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None,
                     preferred_id: str | None = None,
                     state: dict | None = None):
    """Run chat_stream in a dedicated thread while holding GEN_LOCK and emit chunks through a Queue.
    Whether the downstream client disconnects, errors, or times out, stop_event + a finally block guarantee GEN_LOCK is always released immediately, so it cannot deadlock.
    If a single account's VM hangs or its session fails before the first token, automatically switch to the next healthy account and retry once.

    `account_id` pins a specific account. When it is None the worker asks ACCOUNT_POOL to
    choose one, so the pool — not the request thread — decides which account is free;
    picking outside the pool let two concurrent requests both be sent to the same busy
    account. The chosen id is written to `state["account_id"]` when `state` is given."""
    import queue
    q = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    def _emit(item) -> bool:
        """Enqueue without ever blocking forever.

        The consumer stops draining as soon as the downstream client disconnects. A
        plain q.put() would then block this thread while it still holds GEN_LOCK,
        pinning a browser slot until the process restarts. Abort instead.
        """
        while not stop_event.is_set():
            try:
                q.put(item, timeout=1.0)
                return True
            except queue.Full:
                continue
        return False

    def _emit_done():
        """Deliver the terminal sentinel even when the queue is full.

        The sentinel must arrive or the consumer waits forever. Prefer waiting for room
        (a slow-but-alive consumer keeps every chunk); if that does not happen, the
        consumer is gone and dropping the oldest pending chunk is harmless.
        """
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                q.put(("done", None), timeout=0.5)
                return
            except queue.Full:
                continue
        while True:
            try:
                q.put_nowait(("done", None))
                return
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        leased_id = None
        try:
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    if leased_id:
                        ACCOUNT_POOL.release(leased_id)
                        leased_id = None
                    try:
                        alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=timeout)
                    except Exception as exc:
                        last_exc = exc
                        break
                    if not alt or alt["id"] == cur_id:
                        if alt:
                            leased_id = alt["id"]
                        break
                    cur_id = alt["id"]
                    leased_id = cur_id
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("[Chat auto-switch] Switching to fallback account %s (%s) and retrying...", alt.get("label"), cur_id)
                elif cur_id:
                    ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)
                    leased_id = cur_id
                else:
                    try:
                        first = ACCOUNT_POOL.acquire(
                            rotate=True, preferred_id=preferred_id, timeout=timeout)
                    except Exception as exc:
                        last_exc = exc
                        break
                    cur_id = first["id"]
                    leased_id = cur_id
                    cur_cookies = first["cookies"]
                    cur_exp = first.get("cookies_exp")
                if state is not None:
                    state["account_id"] = cur_id

                yielded = False
                try:
                    if cur_id:
                        _bind_account_proxy(store.get_account(cur_id) or {})
                        refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                        if refreshed:
                            cur_cookies = refreshed["cookies"]
                            cur_exp = refreshed.get("cookies_exp")
                    with GEN_LOCK:
                        if stop_event.is_set():
                            return
                        engine.start()
                        for chunk in engine.chat_stream(
                            cur_cookies, prompt, cur_exp, timeout,
                            account_id=cur_id, stop_event=stop_event
                        ):
                            yielded = True
                            if not _emit(("data", chunk)):
                                return
                            if stop_event.is_set():
                                return
                    if cur_id:
                        store.mark(cur_id, True, "")
                        _sync_cookies(cur_id)
                    return
                except MuseAuthError as exc:
                    last_exc = exc
                    if cur_id:
                        store.mark(cur_id, False, str(exc))
                    engine.close_current_page()
                    if yielded:
                        break
                except Exception as exc:
                    last_exc = exc
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                    if yielded:
                        break
            if last_exc is not None:
                _emit(("error", last_exc))
        finally:
            if leased_id:
                ACCOUNT_POOL.release(leased_id)
            engine.close_current_page()
            _clear_account_proxy()
            _emit_done()

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = Field(default=None, max_length=32)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    reference_image: str | None = None
    async_: bool = Field(False, alias="async")


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    # Bounded like ImageRequest.timeout: an unbounded value lets one request hold a
    # browser slot (and a daemon thread) for an arbitrary length of time.
    duration: int | None = Field(default=None, ge=1, le=60)
    size: str | None = Field(default=None, max_length=32)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    resolution: str | None = Field(default=None, max_length=32)
    timeout: int | None = Field(default=None, ge=10, le=3600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    image_url: Any = None
    reference_image: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions request.

    Be lenient with third-party clients: ignore unknown fields (Pydantic's default)
    and read only what we actually use; otherwise every agent's extra parameters would cause a 422.
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    prompt: str | None = None       # compatibility with clients that put prompt at the top level
    # These fields are declared only so they can be read; muse.ai does not act on them
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API (newer Codex uses this by default)."""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AccountProxy(BaseModel):
    server: str
    username: str = ""
    password: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_proxy_url(cls, value):
        return split_proxy_url(value) if isinstance(value, dict) else value

    @field_validator("server")
    @classmethod
    def validate_server(cls, value):
        return normalize_server(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        if ":" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Proxy username cannot contain a colon or control characters")
        try:
            value.encode("latin-1")
        except UnicodeError:
            raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value):
        if value is not None:
            try:
                value.get_secret_value().encode("latin-1")
            except UnicodeError:
                raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    def configuration(self, previous=None):
        previous = previous or {}
        password = self.password.get_secret_value() if self.password is not None else None
        if password is None:
            if previous.get("server") == self.server and previous.get("username", "") == self.username:
                password = previous.get("password", "")
            elif self.username:
                raise HTTPException(400, "Enter the proxy password when changing its server or username")
            else:
                password = ""
        return {"server": self.server, "username": self.username, "password": password}


class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # multi-line text, one account per line
    expires: dict[str, int] = Field(default_factory=dict)   # cookie name -> expiry timestamp
    proxy: AccountProxy | None = None


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    proxy: AccountProxy | None = None


class ProxyTestRequest(BaseModel):
    proxy: AccountProxy


# ------------------------- Prompt construction -------------------------
def _image_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        candidate = (value.get("image_url") or value.get("video_url") or value.get("audio_url")
                     or value.get("file_url") or value.get("url") or value.get("b64_json")
                     or value.get("data"))
        if isinstance(candidate, dict):
            candidate = candidate.get("url")
        return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None
    return None


def _reference_list(images: list | None, legacy: list[Any], field: str,
                    extra: dict[str, list | None] | None = None) -> list[str]:
    """Validate ordered references and make legacy precedence explicit.

    Order: images (or legacy single-image fields) first, then videos / audios / files.
    images may itself mix videos, audio, and documents; types are detected from MIME/file signature during preprocessing.
    """
    primary = next((ref for value in legacy if (ref := _image_value(value))), None)
    if primary and images:
        raise HTTPException(400, f"{field} cannot be used together with legacy single-image fields")
    groups: list[tuple[str, list]] = []
    if primary:
        groups.append((field, [primary]))
    elif images is not None:
        groups.append((field, images))
    for name, values in (extra or {}).items():
        if values is not None:
            if not isinstance(values, list):
                raise HTTPException(400, f"{name} must be an array")
            groups.append((name, values))
    total = sum(len(values) for _, values in groups)
    if total > MAX_REFERENCE_IMAGES:
        raise HTTPException(400, f"At most {MAX_REFERENCE_IMAGES} reference assets are allowed (currently {total})")
    refs = []
    for name, values in groups:
        for index, value in enumerate(values):
            ref = _image_value(value)
            if not ref:
                raise HTTPException(400, f"{name}[{index}] is not a valid reference asset")
            refs.append(ref)
    return refs


def _extra_media(req) -> dict[str, list | None]:
    return {"videos": req.videos, "audios": req.audios, "files": req.files}


def _image_references(req: ImageRequest) -> list[str]:
    return _reference_list(req.images, [req.reference_image, req.image], "images",
                           _extra_media(req))


def _video_references(req: VideoRequest) -> list[str]:
    return _reference_list(
        req.images, [req.reference_image, req.image_url, req.image], "images",
        _extra_media(req))


def _reference_kinds(prepared: list[str]) -> list[str]:
    """Preprocessed data URLs -> list of image/video/audio/document kinds (order preserved)."""
    return [reference_kind(ref[5:].split(";", 1)[0]) for ref in prepared]


_KIND_LABELS = {"image": ("张", "图片"), "video": ("段", "视频"),
                "audio": ("段", "音频"), "document": ("份", "文档")}


def _describe_kinds(kinds: list[str]) -> str:
    counts = collections.Counter(kinds)
    return "、".join(f"{counts[k]} {_KIND_LABELS[k][0]}{_KIND_LABELS[k][1]}"
                    for k in ("image", "video", "audio", "document") if counts[k])


# The legend is only included when the caller actually used these markers; a plain
# multi-image request keeps the simpler wording (see test_reference_media.py).
_REFERENCE_TOKEN = re.compile(r"\[(?:图|视频|音频|文档)\d+\]")

# One canonical "don't pull from the user's album" sentence, appended once at the end of
# every prompt. It used to be restated in five different wordings, buried mid-sentence;
# trailing instructions are followed more reliably, and a single wording is testable.
_NO_HISTORY_MARKER = "严禁使用历史相册或其他历史素材"
_NO_HISTORY_WITH_REFS = "仅使用本次请求附带的附件，" + _NO_HISTORY_MARKER
_NO_HISTORY_NO_REFS = "本次请求未附带参考素材，请直接依据文字描述创作，" + _NO_HISTORY_MARKER


def _history_rule(has_ref: bool) -> str:
    return _NO_HISTORY_WITH_REFS if has_ref else _NO_HISTORY_NO_REFS

# Composer prompts refer to assets as [Image N] / [Video N] / [Audio N], numbered per kind in upload order.
# Muse does not understand these markers, so the prompt spells out "marker -> which attachment".
_KIND_TOKENS = {"image": "图", "video": "视频", "audio": "音频", "document": "文档"}


def _reference_legend(kinds: list[str]) -> str:
    seen: collections.Counter = collections.Counter()
    parts = []
    for index, kind in enumerate(kinds, 1):
        seen[kind] += 1
        parts.append(f"[{_KIND_TOKENS[kind]}{seen[kind]}] 是第 {index} 个附件（{_KIND_LABELS[kind][1]}）")
    return "附件对照：" + "，".join(parts)


# Composition clauses, keyed by canonical ratio.
_RATIO_CLAUSES = {
    "9:16": "【画面构图与比例要求】：严格 9:16 竖屏满屏画幅（9:16 vertical portrait aspect ratio，高大于宽的手机全屏竖版画面），绝对不要生成横屏，保持垂直构图",
    "2:3": "【画面构图与比例要求】：2:3 竖向画幅（2:3 portrait aspect ratio，高大于宽）",
    "3:4": "【画面构图与比例要求】：3:4 竖向画幅（3:4 portrait aspect ratio）",
    "1:1": "【画面构图与比例要求】：1:1 正方形画幅（1:1 square aspect ratio）",
    "4:3": "【画面构图与比例要求】：4:3 比例画幅（4:3 aspect ratio）",
    "3:2": "【画面构图与比例要求】：3:2 横向画幅（3:2 landscape aspect ratio）",
    "16:9": "【画面构图与比例要求】：16:9 宽屏横屏画幅（16:9 widescreen landscape aspect ratio）",
    "21:9": "【画面构图与比例要求】：21:9 超宽画幅（21:9 ultrawide aspect ratio）",
}
# Canonical ratio -> w/h, ordered so a nearest-match scan is stable.
_KNOWN_RATIOS = [
    ("9:16", 9 / 16), ("2:3", 2 / 3), ("3:4", 3 / 4), ("1:1", 1.0),
    ("4:3", 4 / 3), ("3:2", 3 / 2), ("16:9", 16 / 9), ("21:9", 21 / 9),
]
_NAMED_RATIO_RE = re.compile(r"(\d{1,2})\s*[:/]\s*(\d{1,2})")
_PIXEL_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$")
# Values that mean "let the model choose" rather than a concrete geometry.
_UNCONSTRAINED = {"", "auto", "default", "any", "none", "自适应"}


def _nearest_ratio(value: float) -> str:
    return min(_KNOWN_RATIOS, key=lambda item: abs(value - item[1]))[0]


def _ratio_from(text: str | None) -> float | None:
    """Extract a w/h ratio from a named ratio ("9:16", "16/9") or a pixel size."""
    s = (text or "").strip().lower()
    if not s:
        return None
    m = _NAMED_RATIO_RE.search(s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return (w / h) if w and h else None
    m = _PIXEL_SIZE_RE.match(s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return (w / h) if w and h else None
    if "portrait" in s or "vertical" in s or "竖屏" in s or "竖向" in s:
        return 9 / 16
    if "landscape" in s or "horizontal" in s or "wide" in s or "横屏" in s or "横向" in s:
        return 16 / 9
    if "square" in s or "正方形" in s:
        return 1.0
    return None


def _composition_clause(aspect_ratio: str | None, size: str | None) -> str | None:
    """Pick the composition clause for a request.

    Both fields are parsed rather than substring-matched. The previous allowlist missed
    OpenAI's 1024x1792 / 1792x1024 sizes, so those requests fell through to a bare
    "size: WxH" restatement with no orientation instruction and could come back rotated.
    "auto" and friends mean "unconstrained" and produce no clause at all.
    """
    ar = (aspect_ratio or "").strip()
    sz = (size or "").strip()
    if ar.lower() in _UNCONSTRAINED:
        ar = ""
    if sz.lower() in _UNCONSTRAINED:
        sz = ""
    ratio = _ratio_from(ar)
    if ratio is not None:
        return _RATIO_CLAUSES[_nearest_ratio(ratio)]
    if ar:
        return f"【画面构图与比例要求】：{ar} 画面比例"
    ratio = _ratio_from(sz)
    if ratio is not None:
        return _RATIO_CLAUSES[_nearest_ratio(ratio)]
    if sz:
        return f"尺寸/比例：{sz}"
    return None


# Words that mean the user wants visible text in the output.
_TEXT_NOUN_RE = re.compile(
    r"文字|文本|标题|字幕|标语|台词|排版|字体|书名|标签|水印|商标|"
    r"\btext\b|\bword(?:s)?\b|\btitle\b|\bcaption\b|\bsubtitle\b|\bslogan\b|"
    r"\blettering\b|\btypography\b|\blabel\b|\blogo\b|\bwatermark\b|\bquote\b",
    re.I)
_REMOVE_TEXT_RE = re.compile(
    r"去(?:掉|除)|移除|删除|清除|抹除|不要|无需|"
    r"\bremove\b|\bdelete\b|\berase\b|\bclean\b|\bwithout\b",
    re.I)
_ADD_TEXT_RE = re.compile(
    r"加上|添加|写上|写入|标注|加字幕|"
    r"\badd\b|\bput\b|\bwrite\b|\binsert\b|\bshow\b|\bsay(?:ing)?\b",
    re.I)


def _requests_text(prompt: str, extra: str | None = None) -> bool:
    """True when the user asked for visible text in the output.

    Suppresses the blanket "remove all text / watermark / logo" clause, which directly
    contradicts requests like "put the title 'SUMMER SALE' on this poster". A pure
    cleanup request ("remove the watermark") keeps the clause.
    """
    text = f"{prompt or ''}\n{extra or ''}"
    if not _TEXT_NOUN_RE.search(text):
        return False
    if _ADD_TEXT_RE.search(text):
        return True
    return not _REMOVE_TEXT_RE.search(text)


def build_image_prompt(r: ImageRequest, kinds: list[str] | None = None) -> str:
    refs = _image_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if has_ref and any(k != "image" for k in kinds):
        parts = [f"基于我本次上传附带的参考素材（{_describe_kinds(kinds)}；{_reference_legend(kinds)}；"
                 f"各附件的用途以下面的描述为准）"
                 f"进行生图/编辑，输出一张图片：{r.prompt.strip()}"]
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(r.prompt):
        parts = [f"基于我本次上传附带的参考图片（{_reference_legend(kinds)}）进行生图/编辑：{r.prompt.strip()}"]
    elif has_ref:
        parts = [f"基于我本次上传附带的参考图片进行生图/编辑：{r.prompt.strip()}"]
    else:
        parts = [f"全新文生图创作（当前未提供任何参考图，请勿向用户索要原图，直接根据文字描述从零绘制生成一张全新图片）：{r.prompt.strip()}"]
    clause = _composition_clause(r.aspect_ratio, r.size)
    if clause:
        parts.append(clause)

    if has_ref and "image" in kinds and not _requests_text(r.prompt, r.extra):
        parts.append("【纯净画面要求】：彻底清除并去除参考图中的所有文字、水印、签名、角标及Logo标记（clean image without any watermark, text, or logo），输出绝对纯净无字画面")

    if r.extra:
        parts.append(r.extra)
    parts.append(_history_rule(has_ref))
    return "，".join(parts)


def _has_image_parts(messages) -> bool:
    """True when a chat request carries image content.

    Muse chat has no vision path, and flattening an image to a "[image]" placeholder
    makes the model answer as if it had seen the picture. Refuse instead.
    """
    for message in messages or []:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("image_url", "input_image"):
                    return True
    return False


def _content_text(content) -> str:
    """Normalize OpenAI content into plain text (supports multimodal list form)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif t == "image_url":
                    parts.append("[图片]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def build_chat_prompt(messages: list[ChatMessage]) -> str:
    """Flatten messages into a single prompt sent to muse.ai.

    The muse.ai web app keeps conversational context, but this API is stateless (each request may land on
    a different account/page), so folding history into the prompt is most controllable — which suits clients
    like Codex that send full history every turn.
    """
    system, turns = [], []
    for m in messages:
        role = (m.role or "").strip().lower()
        text = _content_text(m.content).strip()
        if role == "tool":
            # Tool results -> send as "information provided by the user"
            if text:
                turns.append(("user", "【工具执行结果】\n" + text))
            continue
        if not text:
            # Some clients send assistant messages with tool_calls but no text
            if role == "assistant" and m.tool_calls:
                turns.append(("assistant", "【请求调用工具】" + json.dumps(
                    m.tool_calls, ensure_ascii=False)[:600]))
            continue
        if role in ("system", "developer"):
            system.append(text)
        else:
            turns.append((role, text))

    # Single turn with no system instruction -> send the raw text, closest to natural conversation
    if len(turns) == 1 and not system and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    if system:
        sys_text = "\n\n".join(system)
        sys_text = sys_text.replace("danger-full-access", "standard-workspace-access")
        parts.append(f"背景与任务设定：\n{sys_text}")
    for role, text in turns:
        label = "助手" if role == "assistant" else "用户"
        parts.append(f"{label}：\n{text}")
    return "\n\n".join(parts)


# ------------------------- Tool calling (function calling) adapter -------------------------
# The muse.ai web model does **not** return structured tool_calls, so this layer adapts the protocol:
#   1) when a request includes tools, translate the definitions into a [tool-calling protocol] in the prompt;
#   2) the model outputs ```json {"tool": "...", "arguments": {...}} ``` per the protocol;
#   3) we parse that back into OpenAI tool_calls for the downstream agent.
#
# Note: this is best-effort, not a guarantee — the target is a general chat model without
# function-calling fine-tuning; protocol adherence must be measured in practice.
_TOOL_PROTOCOL_HEAD = """你可以根据需要调用以下工具来协助用户完成任务。
若需调用工具，请直接输出如下格式的 JSON 代码块（不要包含其他多余解释）：
```json
{"name": "<工具名>", "arguments": {<参数>}}
```
如果需要调用多个工具，请输出包含多个对象的 JSON 数组。
如果无需调用工具，请直接用自然语言回答。

可用工具列表：
"""


def _describe_params(params) -> str:
    """Render JSON Schema parameters as readable multi-line text."""
    if not isinstance(params, dict):
        return "      （无参数）"
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props:
        return "      （无参数）"
    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append("      - %s (%s, %s)%s" % (
            name, spec.get("type") or "any",
            "必填" if name in required else "可选",
            (" " + spec["description"]) if spec.get("description") else ""))
    return "\n".join(lines)


def build_tools_prompt(tools: list | None) -> str:
    """Translate tool definitions into a prompt snippet (empty string when there are no tools).

    Supports both Chat Completions' `{"type":"function","function":{...}}`
    and the Responses API's `{"type":"function","name":...,"parameters":...}`.
    """
    if not tools:
        return ""
    items = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        desc = fn.get("description") or ""
        items.append("%d. %s%s\n   参数：\n%s" % (
            len(items) + 1, fn["name"], (" — " + desc) if desc else "",
            _describe_params(fn.get("parameters"))))
    if not items:
        return ""
    return _TOOL_PROTOCOL_HEAD + "\n".join(items) + "\n"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


def _as_tool_calls(obj) -> list[dict] | None:
    """Convert parsed JSON into OpenAI tool_calls; return None if it does not look like a tool call."""
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("tool") or item.get("name") or item.get("function")
        if not isinstance(name, str) or not name:
            return None
        args = item.get("arguments")
        if args is None:
            args = item.get("parameters") or item.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function",
                    "function": {"name": name, "arguments": args}})
    return out or None


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """Extract tool calls from model output.

    Returns (tool_calls, remaining text); returns (None, original text) when nothing is found.
    Supported forms:
    1) ```json ... ``` code blocks;
    2) bare JSON (or preceded by the json/JSON keyword);
    3) complete JSON objects or arrays embedded in text.
    """
    if not text:
        return None, text
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            calls = _as_tool_calls(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if calls:
            return calls, (text[:m.start()] + text[m.end():]).strip()

    stripped = text.strip()
    clean_stripped = re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", stripped).rstrip("`").strip()
    if clean_stripped.startswith(("{", "[")):
        try:
            calls = _as_tool_calls(json.loads(clean_stripped))
            if calls:
                return calls, ""
        except Exception:
            pass

    m_json = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m_json:
        try:
            calls = _as_tool_calls(json.loads(m_json.group(1).strip()))
            if calls:
                rest = (text[:m_json.start()] + text[m_json.end():]).strip()
                if re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", rest).strip("`").strip() == "":
                    rest = ""
                return calls, rest
        except Exception:
            pass

    return None, text


def build_video_prompt(r: VideoRequest, kinds: list[str] | None = None) -> str:
    user_prompt = r.prompt.strip()
    dur = r.duration or 6
    refs = _video_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if any(k != "image" for k in kinds):
        # No longer default to "first image as the start frame": in practice it pulls the model back into image mode and ignores the reference video
        reference_instruction = (
            f"基于我本次上传的参考素材（{_describe_kinds(kinds)}）生成视频"
            f"（{_reference_legend(kinds)}；各附件的用途以下面的描述为准）")
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(user_prompt):
        reference_instruction = (
            f"基于我本次上传的 {len(refs)} 张参考图片生成视频（{_reference_legend(kinds)}；"
            "各图片的用途以下面的描述为准，未说明时以第一张作为主要起始画面）")
    elif len(refs) > 1:
        reference_instruction = (
            f"基于我本次上传的 {len(refs)} 张参考图片生成视频（以第一张作为主要起始画面，"
            "其余图片作为主体、场景与风格参考）")
    else:
        reference_instruction = (
            "基于我本次上传的参考图片附件作为第一帧参考图（"
            "必须严格以我当前刚刚上传并附带在此处的这张图片为起始帧）")

    gen_word = "图生视频" if all(k == "image" for k in kinds) else "视频"
    # Parse rather than substring-match: the old allowlist missed 1024x1792 /
    # 1792x1024, so those requests were silently treated as unconstrained. When no
    # ratio is given at all, leave the orientation unconstrained instead of guessing.
    ratio = _ratio_from(r.aspect_ratio) or _ratio_from(r.size)
    orientation = _nearest_ratio(ratio) if ratio else ""
    is_vertical = orientation in ("9:16", "2:3", "3:4")
    is_landscape = orientation in ("16:9", "3:2", "21:9")

    parts = []
    if is_vertical:
        if has_ref:
            parts.append(f"{reference_instruction}：生成一个严格9:16竖屏手机满屏的动态{gen_word}（9:16 vertical portrait video，720x1280，高大于宽的手机全屏竖版画面，严禁生成横屏或黑边，时长约 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成）：生成一个严格9:16竖屏手机满屏视频（9:16 vertical portrait video，720x1280，高大于宽的手机全屏竖版画面，严禁生成横屏或带有左右黑边，保持垂直构图，时长约 {dur} 秒）：{user_prompt}")
    elif is_landscape:
        if has_ref:
            parts.append(f"{reference_instruction}：生成一个16:9宽屏横屏{gen_word}（16:9 widescreen landscape video，时长约 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成）：生成一个16:9横屏宽屏视频（16:9 widescreen landscape video，时长约 {dur} 秒）：{user_prompt}")
    else:
        if has_ref:
            parts.append(f"{reference_instruction}：生成动态{gen_word}（时长约 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成）：生成一个视频（时长约 {dur} 秒）：{user_prompt}")

    if r.resolution:
        parts.append(f"画质规格：{r.resolution}")
    if r.extra:
        parts.append(r.extra)
    parts.append(_history_rule(has_ref))
    return "，".join(parts)


def media_url(name: str) -> str:
    """Media URL.

    When public_base is set, return an **absolute URL** — OpenAI-compatible clients (and various agent
    platforms) usually render or download data[].url directly, and relative paths would resolve against the
    client's own domain, causing 404s. Fall back to a relative path when public_base is empty.
    """
    base = _public_base()
    return f"{base}/v1/media/{name}" if base else f"/v1/media/{name}"


# ------------------------- Cookie parsing -------------------------
def parse_cookie_text(text: str) -> dict[str, str]:
    """Parse `a=1; b=2` / JSON / Set-Cookie lines into a dict."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        import json
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def parse_batch(text: str) -> list[tuple[str, dict]]:
    """Batch import: each line is `label | cookie-string`; the label is optional."""
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # a `|` inside a cookie is not a separator
                label, body = head.strip(), tail.strip()
        cookies = parse_cookie_text(body)
        if cookies:
            out.append((label, cookies))
    return out


# ------------------------- Core generation -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """Read cookies back from the browser after generation and write them into the account pool.

    Observed: **using the account does not extend the core cookies' expires** (hatch_vml is fixed
    at about 2 days), so what gets synced back here is mainly non-core cookies reissued on each visit
    (_fbp / wd / dpr, etc.) and any new entries, so account info does not lag behind reality.

    Note: this function **must never fail the generation result** — generation already succeeded;
    syncing is a bonus, and any problem only logs a warning.
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # Expiry must be **merged**, not overwritten: session cookies read back have expires = -1,
        # and overwriting wholesale would erase existing expiry records (learned the hard way).
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()))
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("Cookie sync failed (does not affect this generation): %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    on_acquired=None) -> tuple[dict, str | None]:
    """Generate one image/video, acquiring the account lease before the browser slot.

    Lock order is ACCOUNT_POOL -> GEN_LOCK everywhere in this module (here,
    safe_chat_stream, and the admin maintenance paths). Taking the browser slot first
    would let a task hold it while waiting for an account, which deadlocks against a
    chat request that holds an account and is waiting for the browser.
    """
    deadline = time.monotonic() + max(1, timeout)
    return _run_generation_locked(prompt, kind, timeout, account_id,
                                  on_progress, reference_image, reference_images,
                                  deadline=deadline, on_acquired=on_acquired)


def _run_generation_locked(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    deadline=None, on_acquired=None) -> tuple[dict, str | None]:
    remaining_wait = int(deadline - time.monotonic()) if deadline is not None else timeout
    preferred_id = getattr(engine, "current_acc_id", None)
    if kind == "video" and not account_id:
        # Prefer the last account that completed a video; session health alone is insufficient.
        recent = sorted(list(store.tasks.values()), key=lambda task: task.get("created_at", 0), reverse=True)
        for task in recent:
            if task.get("kind") != "video" or task.get("status") != "completed" or not task.get("account"):
                continue
            candidate = store.get_account(task["account"])
            if candidate and candidate.get("enabled", True) and candidate.get("ok") is not False:
                preferred_id = candidate["id"]
                break
    acc = ACCOUNT_POOL.acquire(account_id=account_id,
                               preferred_id=preferred_id,
                               timeout=max(1, remaining_wait))
    cur_acc = acc
    last_exc = None
    try:
        for attempt in range(2):
            if deadline is not None and time.monotonic() >= deadline:
                raise MuseGenerationError("Overall task deadline reached; stopped retrying")
            if attempt > 0:
                if isinstance(last_exc, MuseModelRefusalError):
                    break
                old_id = cur_acc["id"] if cur_acc else None
                if old_id:
                    ACCOUNT_POOL.release(old_id)
                cur_acc = None
                rem = int(deadline - time.monotonic()) if deadline is not None else timeout
                if rem <= 0:
                    break
                alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=rem)
                if not alt or (old_id and alt["id"] == old_id):
                    if alt:
                        cur_acc = alt
                    break
                cur_acc = alt
                log.info("[Image/video auto-switch] Switching to fallback account %s (%s) and retrying...", cur_acc.get("label"), cur_acc["id"])
            try:
                _bind_account_proxy(cur_acc)
                refreshed = _renew_and_persist(cur_acc["id"], wake_vm=True, force=(attempt > 0))
                if refreshed:
                    cur_acc = refreshed
                remaining = int(deadline - time.monotonic()) if deadline is not None else timeout
                if remaining <= 0:
                    raise MuseGenerationError("Overall task deadline reached; stopped retrying")
                # Account is already leased; the browser slot comes second. Never wait
                # for an account while holding this one (see _run_generation).
                if not GEN_LOCK.acquire(timeout=max(1, remaining)):
                    raise MuseGenerationError(
                        "Timed out waiting for the browser queue; try again later")
                try:
                    if on_acquired:
                        try:
                            on_acquired()
                        except Exception:
                            pass
                        on_acquired = None
                    engine.start()
                    remaining = int(deadline - time.monotonic()) if deadline is not None else timeout
                    if remaining <= 0:
                        raise MuseGenerationError("Overall task deadline reached; stopped retrying")
                    refs = list(reference_images or [])
                    engine_kwargs = {}
                    if len(refs) == 1 and not reference_image:
                        reference_image = refs[0]
                    elif len(refs) > 1:
                        engine_kwargs["reference_images"] = refs
                    res = engine.generate(cur_acc["cookies"], prompt, expect=kind,
                                          timeout=remaining, expires=cur_acc.get("cookies_exp"),
                                          account_id=cur_acc["id"], on_progress=on_progress,
                                          reference_image=reference_image, **engine_kwargs)
                    store.mark(cur_acc["id"], True, "")
                    _sync_cookies(cur_acc["id"])
                    return res, cur_acc["id"]
                finally:
                    GEN_LOCK.release()
            except MuseAuthError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], False, str(exc))
                engine.close_current_page()
            except MuseProxyError as exc:
                last_exc = exc
                store.touch_keepalive(cur_acc["id"], None, f"Proxy error: {str(exc)[:160]}")
                engine.close_current_page()
            except MuseGenerationError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], True, f"Task error: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                engine.close_current_page()
                last_exc = MuseGenerationError(f"Generation failed: {exc}")
        raise last_exc
    finally:
        if cur_acc:
            ACCOUNT_POOL.release(cur_acc.get("id"))
        engine.close_current_page()
        _clear_account_proxy()


# ------------------------- Basic endpoints -------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}


@app.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": MODELS}


@app.get("/v1/models/{model_id}")
def model_detail(model_id: str, _=Depends(auth)):
    """Single-model lookup; several clients validate a configured model before use."""
    resolved = resolve_model(model_id)
    for entry in MODELS:
        if entry["id"] == resolved:
            return {**entry, "id": resolved}
    raise HTTPException(404, f"Model '{model_id}' not found")


def _estimate_usage(prompt: str, output: str) -> dict:
    """Rough token counts (~4 characters per token).

    Muse exposes no real usage, and reporting a hard zero makes client-side budgeting
    and cost dashboards wrong. These are labelled as estimates in the README.
    """
    prompt_tokens = max(1, len(prompt or "") // 4)
    completion_tokens = max(1, len(output or "") // 4)
    return {"prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}


# ------------------------- Image generation -------------------------
def _image_response(req: ImageRequest, res: dict) -> dict:
    # Prefer the dimensions Muse actually produced over the requested ones; echoing the
    # request made client-side assertions meaningless when the ratio was not honoured.
    width, height = res.get("w"), res.get("h")
    size = f"{width}x{height}" if width and height else (req.size or "auto")
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"]),
            "size": size, "kind": res["kind"], "bytes": res["size"]}
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        with open(fpath, "rb") as f:
            item["b64_json"] = base64.b64encode(f.read()).decode()
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _generic_upload_mime(mime: str) -> bool:
    """Common generic multipart upload types (resolved by file signature) and aliases."""
    return mime in ("application/octet-stream", "binary/octet-stream") or mime in (
        "image/jpg", "audio/mp3", "audio/x-wav", "audio/wave", "audio/x-m4a",
        "audio/m4a", "audio/x-flac", "audio/x-aac", "video/x-m4v")


def _prepare_reference_images(images: list[str]) -> list[str]:
    """Validate and snapshot all sources before a task is accepted."""
    prepared = []
    total_bytes = 0
    for index, image in enumerate(images):
        try:
            b64, mime = engine._normalize_image(image)
            data = base64.b64decode(b64, validate=True)
        except (RefImageSafetyError, MuseGenerationError, binascii.Error, ValueError) as exc:
            raise HTTPException(400, f"images[{index}]: {exc}") from exc
        total_bytes += len(data)
        if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
            raise HTTPException(
                400, f"Total reference size exceeds the {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MiB limit")
        prepared.append(f"data:{mime};base64,{b64}")
    return prepared


def _admit_task(kind: str, limit: int) -> None:
    """Refuse a new task when too many of this kind are already queued or processing.

    Called with TASK_ADMISSION_LOCK held. Images and video both go through this so the
    two paths cannot drift apart again — video used to have no cap at all.
    """
    active = sum(1 for task in store.tasks.values()
                 if task.get("kind") == kind
                 and task.get("status") in ("queued", "processing"))
    if active >= limit:
        raise HTTPException(
            429, f"{kind.capitalize()} queue is full ({limit} active); retry later")


def _admit_chat() -> None:
    """Admission cap for /v1/chat/completions and /v1/responses.

    Chat has no store.tasks row to scan (unlike _admit_task), so this counts in-flight
    requests directly via a module-level counter. Call exactly once per request, right
    after validating the prompt/input is non-empty and before any account/browser work.
    Must be paired with exactly one _release_chat() call in a `finally` block so the
    slot is always freed -- on success, on any exception, and on client disconnect
    (GeneratorExit during streaming).
    """
    global _CHAT_ACTIVE
    with TASK_ADMISSION_LOCK:
        if _CHAT_ACTIVE >= CFG.chat_queue_max:
            raise HTTPException(
                429, f"Chat queue is full ({CFG.chat_queue_max} active); retry later")
        _CHAT_ACTIVE += 1


def _release_chat() -> None:
    global _CHAT_ACTIVE
    with TASK_ADMISSION_LOCK:
        _CHAT_ACTIVE = max(0, _CHAT_ACTIVE - 1)


def _reject_multiple_images(n: int | None) -> None:
    """Muse returns exactly one image per generation; say so instead of ignoring `n`."""
    if n and n > 1:
        raise HTTPException(
            400,
            f"n={n} is not supported: Muse returns one image per generation. "
            "Send the request n times if you need several.")


def _idempotency_headers(key: str | None, replayed: bool) -> dict:
    """Echo the client's key so a retry can be correlated with the original request."""
    if not key:
        return {}
    return {"Idempotency-Key": key,
            "Idempotent-Replayed": "true" if replayed else "false"}


def _queue_image(req: ImageRequest, prompt: str, reference_images: list[str],
                 idempotency_key: str | None = None):
    """Opt-in polling avoids reverse-proxy timeouts; no generation is repeated by polling."""
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "references": reference_images},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with TASK_ADMISSION_LOCK:
        if key_hash:
            existing = store.find_task_by_idempotency(key_hash)
            if existing:
                if existing.get("image_request_hash") != request_hash:
                    raise HTTPException(409, "Idempotency-Key reused with different request")
                return JSONResponse(status_code=202, content={
                    "id": existing["id"], "task_id": existing["id"],
                    "object": "image.task", "status": existing["status"],
                    "progress": existing.get("progress", 0),
                    "created_at": existing["created_at"]},
                    headers=_idempotency_headers(idempotency_key, replayed=True))
        _admit_task("image", CFG.image_queue_max)
        task = store.create_task("image", req.prompt)
        tid = task["id"]
        store.update_task(tid, api_prompt=prompt, size=req.size,
                          response_format=req.response_format, progress=0,
                          image_request_key=key_hash, image_request_hash=request_hash)

    def worker():
        timing = {"t0": time.time()}
        def _on_acquired():
            timing["t0"] = time.time()
            store.update_task(tid, status="processing", progress=5)
        try:
            res, acc_id = _run_generation(
                prompt, "image", req.timeout or CFG.image_timeout,
                reference_images=reference_images,
                on_progress=lambda p: store.update_task(tid, progress=p),
                on_acquired=_on_acquired)
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - timing["t0"], 1),
                              url=media_url(res["filename"]),
                              result={**{k: res[k] for k in ("filename", "size", "kind")},
                                      "url": media_url(res["filename"]),
                                      "w": res.get("w"), "h": res.get("h")})
        except Exception as exc:  # noqa: BLE001
            store.update_task(tid, status="failed", error=str(exc),
                              elapsed=round(time.time() - timing["t0"], 1))

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse(status_code=202, content={
        "id": tid, "task_id": tid, "object": "image.task", "status": "queued",
        "progress": 0, "created_at": task["created_at"]},
        headers=_idempotency_headers(idempotency_key, replayed=False))


@app.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: str | None = Header(default=None), _=Depends(auth)):
    _reject_multiple_images(req.n)
    refs = _prepare_reference_images(_image_references(req))
    return _queue_image(req, build_image_prompt(req, _reference_kinds(refs)), refs, idempotency_key)


@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "Image task not found")
    out = dict(task)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"]))
    return out


@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(auth)):
    _reject_multiple_images(req.n)
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _image_references(req))

    prompt = build_image_prompt(req, _reference_kinds(ref_images))
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_images)
    try:
        res, _acc = await asyncio.to_thread(
            _run_generation, prompt, "image", timeout, reference_images=ref_images)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _image_response(req, res)


@app.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(auth)):
    """OpenAI-compatible image-to-image/image-edit endpoint; accepts multipart/form-data and application/json."""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    n = 1
    ref_image_data: list[str] = []
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form(
            max_files=MAX_REFERENCE_IMAGES, max_fields=20)
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        try:
            n = int(form.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        # image may be repeated; video / audio / file also repeat, ordered after image
        img_fields = [(name, value) for name in ("image", "image[]", "video", "audio", "file")
                      for value in form.getlist(name)]
        if len(img_fields) > MAX_REFERENCE_IMAGES:
            raise HTTPException(400, f"At most {MAX_REFERENCE_IMAGES} reference assets are allowed (currently {len(img_fields)})")
        total_upload_bytes = 0
        mib = 1024 * 1024
        for index, (name, img_field) in enumerate(img_fields):
            if img_field and hasattr(img_field, "read"):
                ref_mime = (getattr(img_field, "content_type", "") or "").split(";", 1)[0].strip().lower()
                if ref_mime and ref_mime not in REFERENCE_MIME_EXT and not _generic_upload_mime(ref_mime):
                    raise HTTPException(400, f"{name}[{index}] file type is not supported ({ref_mime})")
                content = await img_field.read(MAX_REFERENCE_IMAGE_BYTES + 1)
                if len(content) > MAX_REFERENCE_IMAGE_BYTES:
                    raise HTTPException(400, f"{name}[{index}] exceeds the {MAX_REFERENCE_IMAGE_BYTES // mib} MiB limit")
                total_upload_bytes += len(content)
                if total_upload_bytes > MAX_REFERENCE_TOTAL_BYTES:
                    raise HTTPException(400, f"Total reference size exceeds the {MAX_REFERENCE_TOTAL_BYTES // mib} MiB limit")
                ref_mime = ref_mime or "application/octet-stream"
                ref_image_data.append(
                    f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}")
            elif isinstance(img_field, str) and img_field.strip():
                ref_image_data.append(img_field.strip())
            else:
                raise HTTPException(400, f"{name}[{index}] is not a valid reference asset")
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        try:
            n = int(body.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        raw_images = body.get("images")
        if raw_images is not None and not isinstance(raw_images, list):
            raise HTTPException(400, "images must be an array")
        legacy = [body.get("reference_image"), body.get("image_url"), body.get("image")]
        ref_image_data = _reference_list(raw_images, legacy, "images", {
            name: body.get(name) for name in ("videos", "audios", "files")})

    if not prompt:
        prompt = "参考此图片并进行生图创作"

    _reject_multiple_images(n)
    req_obj = ImageRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        images=ref_image_data,
        **{"async": async_mode}
    )
    ref_image_data = await asyncio.to_thread(
        _prepare_reference_images, ref_image_data)
    full_prompt = build_image_prompt(req_obj, _reference_kinds(ref_image_data))
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data)
    try:
        res, _acc = await asyncio.to_thread(
            _run_generation, full_prompt, "image", gen_timeout,
            reference_images=ref_image_data)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- Video generation (async tasks) -------------------------
@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def create_video(req: VideoRequest,
                       idempotency_key: str | None = Header(default=None),
                       _=Depends(auth)):
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _video_references(req))

    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    # Namespaced so a video key can never collide with an image key.
    key_hash = (hashlib.sha256(f"video:{idempotency_key}".encode()).hexdigest()
                if idempotency_key else None)
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "references": ref_images},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    prompt = build_video_prompt(req, _reference_kinds(ref_images))
    timeout = req.timeout or CFG.video_timeout
    with TASK_ADMISSION_LOCK:
        if key_hash:
            existing = store.find_task_by_idempotency(key_hash)
            if existing:
                if existing.get("image_request_hash") != request_hash:
                    raise HTTPException(409, "Idempotency-Key reused with different request")
                return JSONResponse(status_code=200, content=_video_object(existing),
                                    headers=_idempotency_headers(idempotency_key, True))
        _admit_task("video", CFG.video_queue_max)
        task = store.create_task("video", req.prompt)
        store.update_task(task["id"], api_prompt=prompt, progress=10,
                          duration=req.duration, size=req.size,
                          image_request_key=key_hash, image_request_hash=request_hash,
                          model=resolve_model(req.model, default="muse-video"))

    def worker():
        timing = {"t0": time.time()}
        def _on_acquired():
            timing["t0"] = time.time()
            store.update_task(task["id"], status="processing", progress=15)
        try:
            def prog_cb(p):
                store.update_task(task["id"], progress=p)
            res, acc_id = _run_generation(prompt, "video", timeout, on_progress=prog_cb,
                                          reference_images=ref_images, on_acquired=_on_acquired)
            vurl = media_url(res["filename"])
            measured = None
            try:
                with open(res["path"], "rb") as handle:
                    measured = _mp4_duration(handle.read())
            except Exception:  # noqa: BLE001
                measured = None
            store.update_task(task["id"], status="completed", progress=100, account=acc_id,
                              **({"duration": round(measured, 2)} if measured else {}),
                              elapsed=round(time.time() - timing["t0"], 1),
                              url=vurl,
                              video={"url": vurl},
                              result={"url": vurl,
                                      "filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"],
                                      "w": res.get("w"), "h": res.get("h")})
        except Exception as exc:  # noqa: BLE001
            store.update_task(task["id"], status="failed",
                              elapsed=round(time.time() - timing["t0"], 1), error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse(status_code=200, content=_video_object(store.get_task(task["id"])),
                        headers=_idempotency_headers(idempotency_key, replayed=False))


_VIDEO_MIME = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


def _video_object(task: dict) -> dict:
    """Render a task as an OpenAI-style video object, keeping the legacy fields.

    OpenAI's video API uses object="video", a status of queued/in_progress/completed/
    failed, and reports seconds/size/completed_at. Extra fields (task_id, url, video)
    are retained so existing callers keep working.
    """
    out = dict(task)
    status = out.get("status")
    if status in ("succeeded", "success", "done"):
        status = "completed"
    if status == "queued":
        status = "queued"
    if status == "processing":
        status = "in_progress"
    if status == "completed":
        progress = 100
    elif status == "in_progress":
        # Progress reports observed milestones only; Muse exposes no percentage.
        progress = max(out.get("progress", 0) or 0, 15)
    else:
        progress = out.get("progress", 0) or 0

    result = out.get("result") if isinstance(out.get("result"), dict) else {}
    vurl = out.get("url") or result.get("url")
    size = out.get("size") or ""
    if result.get("w") and result.get("h"):
        size = f"{result['w']}x{result['h']}"

    out.update({
        "object": "video",
        "status": status,
        "progress": progress,
        "model": out.get("model") or "muse-video",
        "seconds": str(out["duration"]) if out.get("duration") else "",
        "size": size,
        "completed_at": (out.get("updated_at") or out.get("created_at"))
        if status == "completed" else None,
        "expires_at": None,
        "error": {"message": out["error"]} if out.get("error") else None,
    })
    out.setdefault("task_id", out.get("id"))
    if vurl:
        out["url"] = vurl
        out.setdefault("video", {"url": vurl})
    return out


@app.get("/v1/videos/{task_id}/content")
def get_video_content(task_id: str, _=Depends(auth)):
    """OpenAI SDK `videos.content(id)` — stream the rendered file."""
    t = store.get_task(task_id)
    if not t or t.get("kind") != "video":
        raise HTTPException(404, "Video not found")
    result = t.get("result") if isinstance(t.get("result"), dict) else {}
    filename = result.get("filename")
    if not filename:
        raise HTTPException(409, "Video is not ready yet")
    path = os.path.join(CFG.media_dir, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Video file not found")
    ext = os.path.splitext(filename)[1].lower()
    return FileResponse(path, media_type=_VIDEO_MIME.get(ext, "application/octet-stream"),
                        filename=filename)


@app.delete("/v1/videos/{task_id}")
def delete_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    if not t or t.get("kind") != "video":
        raise HTTPException(404, "Video not found")
    result = t.get("result") if isinstance(t.get("result"), dict) else {}
    filename = result.get("filename")
    store.delete_task(task_id)
    if filename:
        try:
            os.remove(os.path.join(CFG.media_dir, filename))
        except OSError:
            pass
    return {"id": task_id, "object": "video.deleted", "deleted": True}


@app.get("/v1/videos/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return _video_object(t)


# ------------------------- Chat (OpenAI-compatible) -------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chat_chunk(cid: str, created: int, model: str, delta: dict,
                finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _want_usage(stream_options) -> bool:
    """Downstream stacks (LangChain, some SDKs/agent frameworks) send
    `stream_options.include_usage=true`, requiring a final chunk with
    `choices: []` + `usage` before [DONE]. Without it, a few frameworks wait forever for usage and hang."""
    if isinstance(stream_options, dict):
        return bool(stream_options.get("include_usage"))
    return False


def _pace_text(text: str, chunk_size: int = 2, delay: float = 0.012):
    """Smoothly split large text into micro-streamed tokens that mimic a native LLM typewriter.
    Tiny text (<= 3 chars) is released immediately with zero delay;
    large text (e.g. a bulk DOM refresh) is paced in chunk_size chunks with delay between them.
    """
    if not text:
        return
    if len(text) <= 3 or delay <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if i + chunk_size < len(text):
            time.sleep(delay)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):
    """OpenAI-compatible chat endpoint — works with a wide range of agent clients.

    Note: the muse.ai web app is an **auto-routing** agent with no model selector, so every request
    lands on the same web assistant (self-named Koda, backed by Muse-series language models). Thus
    the `model` field only provides downstream compatibility and does not affect routing.

    When a request includes `tools`, the [tool-calling protocol] is injected and the model's JSON is parsed back
    into `tool_calls` (muse.ai has no native function calling; this layer adapts the protocol).

    Chat and image/video generation share one browser instance and serialize through `GEN_LOCK`.
    """
    if _has_image_parts(req.messages):
        raise HTTPException(
            400,
            "Image input is not supported on /v1/chat/completions. "
            "Use /v1/images/edits with the image as a reference instead.")
    prompt = build_chat_prompt(req.messages) or (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "messages is empty")
    _admit_chat()

    # The tool-calling protocol is not injected by default — muse.ai's assistant explicitly refuses "pseudo tool calls",
    # and injection pollutes normal answers; see the tool_protocol comment in config.py.
    tool_note = build_tools_prompt(req.tools) if CFG.tool_protocol else ""
    if tool_note:
        # Put it at the end: the agent's own system prompt is at the start, and mid-prompt text is easily ignored;
        # immediately before the user message, compliance with trailing instructions is clearly higher.
        prompt = prompt + "\n\n" + tool_note

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    # The account is leased inside safe_chat_stream, so the pool decides which account is
    # free. Picking here would let two concurrent requests be sent to the same account.
    preferred_id = getattr(engine, "current_acc_id", None)
    chat_state: dict = {}

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    def tool_call_deltas(calls: list[dict]) -> list[list[dict]]:
        """Follow OpenAI convention and send two chunks: first id/name, then full arguments."""
        head = [{"index": i, "id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": ""}}
                for i, c in enumerate(calls)]
        body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                for i, c in enumerate(calls)]
        return [head, body]

    if req.stream:
        def sync_stream():
            try:
                # Send the initial role: assistant packet immediately after the handshake so downstream clients see the cursor without delay
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))

                stream_gen = safe_chat_stream({}, prompt, None, timeout, None,
                                              preferred_id=preferred_id, state=chat_state)
                streamed = ""
                if not tool_note:
                    for chunk in stream_gen:
                        streamed += chunk
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                    yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                else:
                    buf, mode = "", None
                    for chunk in stream_gen:
                        if mode == "text":
                            for piece in _pace_text(chunk):
                                yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            continue
                        buf += chunk
                        if mode is None:
                            head = buf.lstrip()
                            if len(head) >= 2:
                                if head[0] in "{[" or head.startswith("```"):
                                    mode = "maybe_tool"
                                else:
                                    mode = "text"
                                    for piece in _pace_text(buf):
                                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                                    buf = ""
                    streamed = buf
                    if mode == "text":
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                    else:
                        calls, rest = parse_tool_calls(buf)
                        if calls:
                            if rest:
                                for piece in _pace_text(rest):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            for piece in tool_call_deltas(calls):
                                yield _sse(_chat_chunk(cid, created, model, {"tool_calls": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="tool_calls"))
                        else:
                            if buf:
                                for piece in _pace_text(buf):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "usage": _estimate_usage(prompt, streamed)})
                yield "data: [DONE]\n\n"
            except MuseAuthError as exc:
                _mark_chat_account(chat_state, False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except MuseGenerationError as exc:
                _mark_chat_account(chat_state, True, f"Assistant timeout: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            except Exception as exc:
                yield _sse({"error": {"message": f"Internal error: {exc}", "type": "server_error", "code": 500}})
            finally:
                _release_chat()
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream({}, prompt, None, timeout, None,
                                        preferred_id=preferred_id, state=chat_state))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))
    finally:
        _release_chat()

    usage = _estimate_usage(prompt, text)
    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": rest or None,
                                             "tool_calls": calls}}],
                    "usage": usage}
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage}


def _responses_messages(req: ResponsesRequest) -> list[ChatMessage]:
    """Normalize Responses API instructions / input into messages."""
    msgs: list[ChatMessage] = []
    if req.instructions:
        msgs.append(ChatMessage(role="system", content=req.instructions))
    inp = req.input
    if isinstance(inp, str):
        msgs.append(ChatMessage(role="user", content=inp))
        return msgs
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if item.get("role"):                      # message form
                msgs.append(ChatMessage(role=item["role"],
                                        content=item.get("content")))
            elif itype == "input_text":
                msgs.append(ChatMessage(role="user", content=item.get("text")))
            elif itype == "function_call_output":
                msgs.append(ChatMessage(role="user", content="【工具执行结果】\n"
                                        + str(item.get("output") or "")))
            elif itype == "function_call":
                msgs.append(ChatMessage(role="assistant", content="【请求调用工具】"
                                        + str(item.get("name") or "")))
    return msgs


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/v1/responses")
async def responses_api(req: ResponsesRequest, _=Depends(auth)):
    """OpenAI Responses API — newer Codex uses this endpoint by default.

    Implements only the subset Codex actually uses: text in -> text out (including streaming events).
    muse.ai does not return structured tool_calls; see the README for tool-calling limits.
    """
    if isinstance(req.input, list) and any(
            isinstance(item, dict) and item.get("type") in ("input_image", "image_url")
            for item in req.input):
        raise HTTPException(
            400,
            "Image input is not supported on /v1/responses. "
            "Use /v1/images/edits with the image as a reference instead.")
    prompt = build_chat_prompt(_responses_messages(req))
    if not prompt:
        raise HTTPException(400, "input is empty")
    _admit_chat()

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    # Same opt-in protocol adapter as /v1/chat/completions; see config.py. With it off,
    # a client that sends `tools` still gets a normal answer rather than an error.
    tool_note = build_tools_prompt(req.tools) if (CFG.tool_protocol and req.tools) else ""
    if tool_note:
        prompt = prompt + "\n\n" + tool_note
    # Leased inside safe_chat_stream so the pool, not this thread, picks a free account.
    preferred_id = getattr(engine, "current_acc_id", None)
    chat_state: dict = {}

    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())

    def envelope(status: str, text: str = "",
                 tool_calls: list[dict] | None = None) -> dict:
        done = status == "completed"
        if tool_calls:
            output = [{"type": "function_call", "id": call["id"],
                       "call_id": call["id"], "name": call["function"]["name"],
                       "arguments": call["function"]["arguments"],
                       "status": "completed" if done else "in_progress"}
                      for call in tool_calls]
        else:
            output = [{"type": "message", "id": mid, "role": "assistant",
                       "status": "completed" if done else "in_progress",
                       "content": ([{"type": "output_text", "text": text,
                                     "annotations": []}] if text else [])}]
        usage = _estimate_usage(prompt, text)
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model, "output": output,
                "usage": {"input_tokens": usage["prompt_tokens"],
                          "output_tokens": usage["completion_tokens"],
                          "total_tokens": usage["total_tokens"]}}

    if req.stream:
        def sync_stream():
            try:
                yield _sse_event("response.created", {
                    "type": "response.created",
                    "response": envelope("in_progress")})
                def message_started():
                    return _sse_event("response.output_item.added", {
                        "type": "response.output_item.added", "output_index": 0,
                        "item": {"id": mid, "type": "message", "role": "assistant",
                                 "status": "in_progress", "content": []}})

                if tool_note:
                    # Buffered: streaming the raw protocol JSON would confuse clients,
                    # and a tool call can only be classified once it is complete.
                    full = "".join(safe_chat_stream({}, prompt, None, timeout, None,
                                                    preferred_id=preferred_id,
                                                    state=chat_state))
                    calls, rest = parse_tool_calls(full)
                else:
                    yield message_started()
                    yield _sse_event("response.content_part.added", {
                        "type": "response.content_part.added", "item_id": mid,
                        "output_index": 0, "content_index": 0,
                        "part": {"type": "output_text", "text": "",
                                 "annotations": []}})
                    full, calls, rest = "", None, ""
                    for chunk in safe_chat_stream({}, prompt, None, timeout, None,
                                                  preferred_id=preferred_id,
                                                  state=chat_state):
                        full += chunk
                        yield _sse_event("response.output_text.delta", {
                            "type": "response.output_text.delta", "item_id": mid,
                            "output_index": 0, "content_index": 0, "delta": chunk})
                if calls:
                    for index, call in enumerate(calls):
                        arguments = call["function"]["arguments"]
                        yield _sse_event("response.output_item.added", {
                            "type": "response.output_item.added", "output_index": index,
                            "item": {"id": call["id"], "type": "function_call",
                                     "call_id": call["id"],
                                     "name": call["function"]["name"],
                                     "arguments": "", "status": "in_progress"}})
                        yield _sse_event("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "item_id": call["id"], "output_index": index,
                            "delta": arguments})
                        yield _sse_event("response.function_call_arguments.done", {
                            "type": "response.function_call_arguments.done",
                            "item_id": call["id"], "output_index": index,
                            "arguments": arguments})
                        yield _sse_event("response.output_item.done", {
                            "type": "response.output_item.done", "output_index": index,
                            "item": {"id": call["id"], "type": "function_call",
                                     "call_id": call["id"],
                                     "name": call["function"]["name"],
                                     "arguments": arguments, "status": "completed"}})
                    yield _sse_event("response.completed", {
                        "type": "response.completed",
                        "response": envelope("completed", rest, calls)})
                else:
                    if tool_note:
                        yield message_started()
                        yield _sse_event("response.content_part.added", {
                            "type": "response.content_part.added", "item_id": mid,
                            "output_index": 0, "content_index": 0,
                            "part": {"type": "output_text", "text": "",
                                     "annotations": []}})
                        if full:
                            yield _sse_event("response.output_text.delta", {
                                "type": "response.output_text.delta", "item_id": mid,
                                "output_index": 0, "content_index": 0, "delta": full})
                    yield _sse_event("response.output_text.done", {
                        "type": "response.output_text.done", "item_id": mid,
                        "output_index": 0, "content_index": 0, "text": full})
                    yield _sse_event("response.output_item.done", {
                        "type": "response.output_item.done", "output_index": 0,
                        "item": {"id": mid, "type": "message", "role": "assistant",
                                 "status": "completed",
                                 "content": [{"type": "output_text", "text": full,
                                              "annotations": []}]}})
                    yield _sse_event("response.completed", {
                        "type": "response.completed",
                        "response": envelope("completed", full)})
            except Exception as exc:  # noqa: BLE001
                log.warning("Responses streaming failed: %s", exc)
                yield _sse_event("response.failed", {
                    "type": "response.failed",
                    "response": envelope("failed")})
            finally:
                _release_chat()
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream({}, prompt, None, timeout, None,
                                        preferred_id=preferred_id, state=chat_state))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))
    finally:
        _release_chat()

    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return envelope("completed", rest, calls)
    return envelope("completed", text)


@app.get("/v1/media/{name}")
def get_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Invalid file name")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    return FileResponse(p)


# ------------------------- Admin: overview -------------------------
@app.get("/admin/status")
def admin_status(_=Depends(auth)):
    base = _public_base()
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "tasks": store.list_tasks(20),
        "media_count": len(os.listdir(CFG.media_dir))
        if os.path.isdir(CFG.media_dir) else 0,
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "base_url": f"{base}/v1" if base else "",
        # Non-empty when a data file had to be quarantined at startup; the original is
        # preserved at the reported backup path.
        "corrupt_files": list(store.corrupt_files),
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


# ------------------------- Admin: account pool -------------------------
@app.get("/admin/accounts")
def list_accounts(_=Depends(auth)):
    return {"accounts": store.list_accounts(), "stats": store.stats()}


@app.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(auth)):
    proxy = req.proxy.configuration() if req.proxy else None
    added: list[dict] = []
    seen: list[dict] = []          # all cookies in this import, used to check core-cookie completeness

    if req.batch:
        for label, cookies in parse_batch(req.batch):
            acc = store.add_account(cookies, label)
            seen.append(cookies)
            added.append({"id": acc["id"], "label": acc["label"],
                          "cookie_count": len(cookies),
                          "expires_at": acc.get("expires_at")})

    cookies = dict(req.cookies)
    if req.cookie_header:
        cookies.update(parse_cookie_text(req.cookie_header))
    if cookies:
        acc = store.add_account(cookies, req.label, cookies_exp=req.expires)
        seen.append(cookies)
        added.append({"id": acc["id"], "label": acc["label"],
                      "cookie_count": len(cookies),
                      "expires_at": acc.get("expires_at")})

    if not added:
        raise HTTPException(400, "No cookies parsed; check the format")

    if proxy:
        for account in added:
            store.update_account(account["id"], proxy=proxy)

    # Pass if any account has all core cookies (batch imports are judged as a whole)
    missing = [n for n in ESSENTIAL_COOKIES
               if not any(n in c for c in seen)]
    return {"added": added, "count": len(added),
            "essential_missing": missing,
            "warning": (f"Missing core cookies: {', '.join(missing)}; this account may not be able to generate"
                        if missing else "")}


# One short-lived receipt per account; credentials are represented only by a hash.
_PROXY_TEST_RECEIPTS: dict[str, dict] = {}
_PROXY_TEST_RECEIPT_TTL = 300


def _proxy_test_identity(proxy: dict, cookies: dict | None) -> str:
    value = json.dumps({"proxy": proxy, "cookies": cookies or {}},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    with ACCOUNT_POOL.cond:
        existing = store.get_account(aid)
        if not existing:
            raise HTTPException(404, "Account not found")
        changes = {"label": req.label, "enabled": req.enabled}
        if "proxy" in req.model_fields_set:
            if aid in ACCOUNT_POOL.busy_accounts:
                raise HTTPException(409, "Account is running a task or check; change the proxy later")
            proxy = req.proxy.configuration(existing.get("proxy")) if req.proxy else {}
            if proxy != (existing.get("proxy") or {}):
                changes.update(proxy=proxy, synced_at=0, last_keepalive=0,
                               checked_at=0, proxy_status={}, ok=None,
                               note="Proxy changed; test again")
                receipt = _PROXY_TEST_RECEIPTS.get(aid)
                if (proxy and receipt and 0 <= time.monotonic() - receipt["at"] <= _PROXY_TEST_RECEIPT_TTL
                        and receipt["identity"] == _proxy_test_identity(proxy, existing.get("cookies"))):
                    changes["proxy_status"] = dict(receipt["result"])
                    changes["note"] = "Proxy settings saved; account browser state needs testing"
                engine._last_http_renew.pop(aid, None)
            _PROXY_TEST_RECEIPTS.pop(aid, None)
        acc = store.update_account(aid, **changes)
        return store.public_account(acc)


@app.post("/admin/accounts/{aid}/proxy/test")
def test_account_proxy(aid: str, req: ProxyTestRequest | None = None, _=Depends(auth)):
    import ipaddress
    import requests
    with _account_operation(aid) as acc:
        proxy = req.proxy.configuration(acc.get("proxy")) if req is not None else _account_proxy(acc)
        if not proxy:
            raise HTTPException(400, "Enter a proxy URL or save a proxy for this account first")
        identity = _proxy_test_identity(proxy, acc.get("cookies"))
        result = {"ok": False, "proxy_configured": True, "proxy_reachable": False,
                  "exit_ip": None, "session_ok": None, "workspace_status": None}
        try:
            # Fixed HTTPS endpoint receives no Muse cookies or proxy credentials.
            with request_proxy_context(proxy) as proxy_options, requests.Session() as session:
                session.trust_env = False
                response = session.get("https://api.ipify.org?format=json",
                                       timeout=(5, 10), allow_redirects=False,
                                       **proxy_options)
            if response.status_code != 200:
                raise MuseProxyError("Proxy egress check failed; check the proxy connection or credentials")
            result["exit_ip"] = str(ipaddress.ip_address(response.json()["ip"]))
            result["proxy_reachable"] = True
            checked = engine.renew_session_http(acc.get("cookies") or {}, acc.get("cookies_exp"),
                                                wake_vm=False, proxy=proxy)
            result.update(ok=True, session_ok=True, workspace_status=checked.get("status"),
                          message="Proxy egress and Muse session checks passed; verify generation with an account test and a real task")
        except MuseAuthError:
            result.update(session_ok=False, message="Proxy is reachable, but Muse session authentication failed; re-import cookies")
        except MuseProxyError as exc:
            result["message"] = str(exc)
        except MuseGenerationError as exc:
            result.update(session_ok=False, message=str(exc))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            result["message"] = "Proxy egress check failed; check the proxy address, credentials, and network"
        recorded = {**result, "checked_at": int(time.time())}
        tested_saved = False
        with ACCOUNT_POOL.cond:
            current = store.get_account(aid)
            if current and identity == _proxy_test_identity(proxy, current.get("cookies")):
                # Unsaved routes stay only in memory; saving the exact configuration may adopt this proof.
                _PROXY_TEST_RECEIPTS[aid] = {"identity": identity, "at": time.monotonic(),
                                           "result": dict(recorded)}
                tested_saved = proxy == _account_proxy(current)
                if tested_saved:
                    store.update_account(aid, proxy_status=dict(recorded))
        result["tested_saved_proxy"] = tested_saved
        return result


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "Account not found")
    with ACCOUNT_POOL.cond:
        _PROXY_TEST_RECEIPTS.pop(aid, None)
    return {"deleted": True, "id": aid}


@app.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(auth)):
    """Really open muse.ai to verify whether this account's cookies can still log in."""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc.get("cookies"):
        raise HTTPException(400, "This account has no cookies")

    def _probe():
        # Account first, browser second (see _run_generation for the ordering rule).
        with _account_operation(aid) as acc, GEN_LOCK:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
                synced = _sync_cookies(aid)
                quota = None
                quota_error = None
                try:  # Session validity and quota readability are independent results.
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"), account_id=aid)
                    if quota.get("found"):
                        quota["checked_at"] = int(time.time())
                        store.update_account(aid, quota=quota)
                    else:
                        quota_error = "No valid data read from the quota panel; existing cache left unchanged"
                        quota = None
                except Exception as exc:  # noqa: BLE001
                    quota_error = f"{type(exc).__name__}; quota cache not updated"
                store.mark(aid, True, "Session valid" + ("; quota read failed" if quota_error else ""))
                return {"ok": True, "message": "Session valid; generation should work",
                        "synced": synced, "quota": quota,
                        "quota_error": quota_error}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"Test unconfirmed (account state kept): {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@app.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(auth)):
    return await test_account(aid, _)


@app.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(auth)):
    """Update an account's cookies (to supply fresh cookies after session expiry)."""
    cookies = dict(payload.get("cookies") or {})
    if payload.get("cookie_header"):
        cookies.update(parse_cookie_text(payload["cookie_header"]))
    if not cookies:
        raise HTTPException(400, "No cookies parsed")
    exp = {k: int(v) for k, v in (payload.get("expires") or {}).items()
           if _pos(v)}
    # These are cookies from a "brand-new session", so the expiry-estimate anchor must reset to now;
    # otherwise the old session's anchor is reused and remaining days are underestimated.
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="Cookies updated",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "Account not found")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@app.post("/admin/relogin")
def relogin(_=Depends(auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "No available account")
    try:
        with _account_operation(acc["id"]) as selected, GEN_LOCK:
            engine.start()
            engine.refresh(selected["cookies"], selected.get("cookies_exp"), account_id=selected["id"])
            return {"ok": True, "account": selected["id"]}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ------------------------- Admin: quota -------------------------
@app.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(auth)):
    """Open muse.ai's Settings panel to read this account's quota (live) and cache it on the account record.

    Example return: {"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc.get("cookies"):
        raise HTTPException(400, "This account has no cookies")

    def _probe():
        # Account first, browser second (see _run_generation for the ordering rule).
        with _account_operation(aid) as acc, GEN_LOCK:
            engine.start()
            q = engine.quota(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
            if not q.get("found"):
                raise MuseGenerationError("No valid data read from the quota panel; existing cache left unchanged")
            q["checked_at"] = int(time.time())
            store.update_account(aid, quota=q)
            return q

    try:
        return await asyncio.to_thread(_probe)
    except HTTPException:
        raise
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Quota query failed: {exc}") from exc


@app.post("/admin/quota")
async def query_any_quota(_=Depends(auth)):
    """Query quota once using the least-recently-used available account (accounts in a pool usually
    share one muse.ai plan; with multiple accounts, query per account instead)."""
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "No available account")
    return await query_quota(acc["id"], _)


# ------------------------- Admin: connection info / API key -------------------------
def _public_base() -> str:
    return (CFG.public_base or "").rstrip("/")


@app.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    """Generate a new API key, write it to .env, and apply it immediately (no restart)."""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    old = CFG.api_key
    CFG.api_key = new_key
    _persist_env("MUSE2API_KEY", new_key)
    return {"ok": True, "api_key": new_key, "previous": old,
            "message": "New key generated and applied immediately; the old key is now invalid, update downstream projects"}


def _persist_env(key: str, value: str):
    """Write configuration back to .env (keep other lines, atomic replace).

    The file holds MUSE2API_KEY and GITHUB_TOKEN, so it is created 0600. os.replace
    preserves the temp file's mode, which would otherwise come from the umask.
    """
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ------------------------- Admin: cookie capture -------------------------
@app.get("/admin/cookie-helper")
def cookie_helper(download: int = 0):
    """Return the local cookie helper script (advanced; requires Python)."""
    p = os.path.join(BASE_DIR, "tools", "get_muse_cookie.py")
    if not os.path.isfile(p):
        raise HTTPException(404, "Helper script missing")
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="get_muse_cookie.py"'
    return FileResponse(p, media_type="text/x-python", headers=headers)


@app.get("/admin/extension")
def extension_zip():
    """Package the browser extension as a zip and return it (recommended; no command line).

    Users download, unzip -> load unpacked at chrome://extensions -> click once to import cookies.
    """
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "Extension directory missing")
    return Response(
        content=_extension_zip_bytes(src),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="muse2api-extension.zip"',
                 "Cache-Control": "no-store"})


_EXTENSION_ZIP_CACHE: dict = {"stamp": None, "bytes": b""}


def _extension_zip_bytes(src: str) -> bytes:
    """Build the extension zip once per change.

    This endpoint is unauthenticated (the admin page links to it directly), so rebuilding
    on every request was a cheap CPU amplification. Cache keyed on the directory's newest
    mtime and size.
    """
    stamp = []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(root, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            stamp.append((path, info.st_mtime_ns, info.st_size))
    stamp_key = tuple(stamp)
    if _EXTENSION_ZIP_CACHE["stamp"] == stamp_key:
        return _EXTENSION_ZIP_CACHE["bytes"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, _, _ in stamp:
            relative_path = os.path.relpath(path, src).replace(os.sep, "/")
            z.write(path, f"muse2api-extension/{relative_path}")
    data = buf.getvalue()
    _EXTENSION_ZIP_CACHE["stamp"] = stamp_key
    _EXTENSION_ZIP_CACHE["bytes"] = data
    return data


@app.get("/admin/extension/files")
def extension_files():
    """List the extension directory contents (for frontend display)."""
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "Extension directory missing")
    files = []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        for name in sorted(names):
            p = os.path.join(root, name)
            files.append(os.path.relpath(p, src).replace(os.sep, "/"))
    return {"files": sorted(files)}


# ------------------------- Admin: tasks / media -------------------------
@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    return {"tasks": store.list_tasks(limit)}


@app.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(auth)):
    if not store.delete_task(tid):
        raise HTTPException(404, "Task not found")
    return {"deleted": True}


@app.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(auth)):
    return {"removed": store.clear_tasks(int(payload.get("keep") or 0))}


@app.get("/admin/media")
def admin_media(_=Depends(auth)):
    d = CFG.media_dir
    items = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            items.append({"name": name, "url": media_url(name), "bytes": os.path.getsize(p),
                          "mtime": int(os.path.getmtime(p)),
                          "kind": "video" if ext in (".mp4", ".webm", ".mov") else "image"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"media": items, "count": len(items)}


@app.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names must be an array of file names")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
            errors.append(f"Invalid file name: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@app.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(auth)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Invalid file name")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    try:
        os.remove(p)
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"Delete failed: {e}")


# ------------------------- Frontend page -------------------------
def _admin_html() -> str:
    p = os.path.join(BASE_DIR, "admin.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8><body style='background:#0b0f19;"
                "color:#e6e8ee;font-family:system-ui;padding:40px'>"
                "<h2>muse2api</h2><p>The admin page file admin.html is missing.</p>"
                "<p>API available: <code>/v1/images/generations</code>, "
                "<code>/v1/videos</code></p></body>")


# The console is regenerated on every deploy; never let a browser or CDN serve a stale
# copy (that made "the page did not update" look like a code problem).
_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate"}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_admin_html(), headers=_NO_STORE)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(_admin_html(), headers=_NO_STORE)



# ------------------------- Automatic account keepalive and silent renewal -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "next_run": None,
}


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """Verify the upstream session through this account's saved route."""
    try:
        with _account_operation(aid) as acc:
            if not acc.get("cookies"):
                return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": "Account has no valid cookies"}
            res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=True,
                                            **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
            store.update_account(
                aid,
                cookies=res["cookies"],
                cookies_exp=res["cookies_exp"],
                ok=True if res.get("ok") else False,
                synced_at=int(time.time()),
            )
            vm_state = res.get("vm_state") or "RUNNING"
            store.touch_keepalive(aid, True, f"Session valid · auto-keepalive (VM: {vm_state})")
            quota = acc.get("quota")
            if check_quota and GEN_LOCK.acquire(blocking=False):
                try:
                    engine.start()
                    quota = engine.quota(res["cookies"], res["cookies_exp"],
                                         account_id=aid)
                    if not quota.get("found"):
                        raise MuseGenerationError("No valid data read from the quota panel; existing cache left unchanged")
                    quota["checked_at"] = int(time.time())
                    store.update_account(aid, quota=quota)
                except Exception as qe:  # noqa: BLE001
                    log.warning("Failed to read quota for account %s: %s", aid, qe)
                finally:
                    GEN_LOCK.release()
            updated = store.get_account(aid) or {}
            return {
                "ok": True,
                "id": aid,
                "label": acc.get("label", aid),
                "vm_id": res.get("vm_id"),
                "vm_state": vm_state,
                "wake_ok": res.get("wake_ok"),
                "expires_at": updated.get("expires_at"),
                "quota": quota,
            }
    except HTTPException as exc:
        return {"ok": False, "id": aid, "busy": exc.status_code == 409, "error": str(exc.detail)}
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, f"Keepalive authentication failed: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc)}
    except Exception as exc:
        store.touch_keepalive(aid, None, f"Keepalive unconfirmed (account state kept): {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc)}


_MEDIA_SWEEP_STATE = {"last": 0.0}


def _sweep_media_sync() -> dict:
    """Delete generated media older than CFG.media_retention_days.

    Files referenced by a queued or processing task are never removed: the worker still
    writes progress against that id and would otherwise fail at delivery.
    """
    days = getattr(CFG, "media_retention_days", 0) or 0
    if days <= 0:
        return {"removed": 0, "skipped": True}
    cutoff = time.time() - days * 86400
    protected = set()
    for task in list(store.tasks.values()):
        if task.get("status") in ("queued", "processing"):
            result = task.get("result") if isinstance(task.get("result"), dict) else {}
            if result.get("filename"):
                protected.add(result["filename"])

    removed, errors = 0, []
    media_dir = CFG.media_dir
    if os.path.isdir(media_dir):
        for name in os.listdir(media_dir):
            if name in protected:
                continue
            path = os.path.join(media_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError as exc:
                errors.append(f"{name}: {exc}")
    if removed:
        log.info("[Media retention] Removed %s file(s) older than %s day(s)", removed, days)
    return {"removed": removed, "errors": errors, "skipped": False}


async def run_keepalive_all(force: bool = False) -> dict:
    """Run account keepalive: force=True refreshes all enabled accounts; otherwise refresh expiring/unchecked accounts and keep VMs warm."""
    async with KEEPALIVE_LOCK:
        now = int(time.time())
        KEEPALIVE_STATE["running"] = True
        KEEPALIVE_STATE["last_run"] = now
        results = []
        skipped = []

        try:
            include_disabled = getattr(CFG, "keepalive_disabled_accounts", False)
            accounts = [a for a in store.list_accounts() if include_disabled or a.get("enabled", True)]
            # One flush for the whole sweep instead of one accounts.json rewrite per
            # account (each rewrite serialises every account's cookie jar).
            with store.batch_writes():
                for a in accounts:
                    aid = a["id"]
                    label = a.get("label", aid)
                    exp_at = a.get("expires_at")
                    last_ka = a.get("last_keepalive") or 0

                    needs_run = (
                        force
                        or not last_ka
                        or ((now - last_ka) > 6 * 3600 and
                            (not exp_at or exp_at - now < 36 * 3600 or
                             a.get("ok") is not True))
                    )
                    if not needs_run:
                        skipped.append({"id": aid, "label": label, "reason": "Session still fresh and recently kept alive"})
                        continue

                    log.info("[Auto-keepalive] Silently renewing and waking the VM for account %s (%s)...", label, aid)
                    res = await asyncio.to_thread(_probe_account_sync, aid, False)
                    results.append(res)
                    log.info("[Auto-keepalive] Account %s result: ok=%s expires_at=%s", label, res.get("ok"), res.get("expires_at"))
                    await asyncio.sleep(0.5)

            summary = {
                "checked_at": now,
                "refreshed_count": len(results),
                "refreshed": results,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
            KEEPALIVE_STATE["last_result"] = summary
            KEEPALIVE_STATE["next_run"] = now + 900
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


def _warmup_browser_sync():
    """Silently warm up the browser and the first available account's WebSocket tunnel in the background, so the first request after a restart is fast."""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    # Account first, browser second (see _run_generation for the ordering rule).
    try:
        acc = ACCOUNT_POOL.acquire(rotate=False, timeout=0.1, record_use=False)
    except Exception:  # noqa: BLE001
        return
    if not GEN_LOCK.acquire(blocking=False):
        ACCOUNT_POOL.release(acc["id"])
        return
    try:
        log.info("[Browser warmup] Prewarming a hot-standby tab for account %s (%s) in the background...", acc.get("label"), acc["id"])
        _bind_account_proxy(acc)
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("[Browser warmup] Hot-standby tab and WebSocket ready for account %s (%s)", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("[Browser warmup] Warmup failed: %s", exc)
    finally:
        # Warmup runs in a worker thread; do not retain its account reservation or page.
        engine.close_current_page()
        _clear_account_proxy()
        if acc:
            ACCOUNT_POOL.release(acc["id"])
        GEN_LOCK.release()


def _browser_health_check_sync() -> dict:
    """Detect a crashed or unresponsive shared Chromium and proactively recycle it.

    Runs from the background keepalive loop, not from a request thread. Only acts when
    GEN_LOCK is free (non-blocking acquire): every engine.start()/stop() call elsewhere
    in this module already runs under GEN_LOCK held by that caller, so a successful
    non-blocking acquire here proves no generation is in flight. If GEN_LOCK is held,
    this cycle is skipped and retried on the next tick.
    """
    healthy, detail = engine.is_healthy()
    reason = None if healthy else f"unhealthy: {detail}"
    if reason is None and CFG.browser_max_uptime_hours > 0:
        if engine.uptime_seconds > CFG.browser_max_uptime_hours * 3600:
            reason = f"max uptime exceeded ({CFG.browser_max_uptime_hours}h)"
    if reason is None:
        return {"recycled": False}
    if not GEN_LOCK.acquire(blocking=False):
        log.info("[Browser health] %s, but a generation is in-flight; deferring to next cycle", reason)
        return {"recycled": False, "deferred": True, "reason": reason}
    try:
        engine.recycle(reason)
        return {"recycled": True, "reason": reason}
    finally:
        GEN_LOCK.release()


async def _keepalive_loop():
    """Long-running background guard: polls account health every 15 minutes and keeps VMs warm."""
    log.info("[Auto-keepalive daemon] Started; check interval: 15 minutes")
    await asyncio.sleep(1)
    try:
        await asyncio.to_thread(_warmup_browser_sync)
    except Exception as e:  # noqa: BLE001
        log.warning("[Browser warmup] Error: %s", e)
    try:
        _MEDIA_SWEEP_STATE["last"] = time.time()
        await asyncio.to_thread(_sweep_media_sync)
    except Exception as e:  # noqa: BLE001
        log.warning("[Media retention] Initial sweep failed: %s", e)
    while True:
        try:
            await asyncio.to_thread(_browser_health_check_sync)
        except Exception as e:  # noqa: BLE001
            log.warning("[Browser health] Check failed: %s", e)
        try:
            await run_keepalive_all(force=False)
        except Exception as e:  # noqa: BLE001
            log.error("[Auto-keepalive daemon] Poll failed: %s", e)
        # The loop ticks every 15 minutes; retention only needs to run once a day.
        if time.time() - _MEDIA_SWEEP_STATE["last"] >= 86400:
            _MEDIA_SWEEP_STATE["last"] = time.time()
            try:
                await asyncio.to_thread(_sweep_media_sync)
            except Exception as e:  # noqa: BLE001
                log.warning("[Media retention] Sweep failed: %s", e)
        await asyncio.sleep(900)


@app.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(auth)):
    """Manually trigger one keepalive/renewal pass for all accounts."""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "A keepalive task is already running; try again shortly"}
    return await run_keepalive_all(force=force)


@app.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(auth)):
    """Manually run keepalive/renewal for a single account."""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@app.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(auth)):
    """Get the keepalive daemon task status."""
    return KEEPALIVE_STATE


# ------------------------- Live repo update check, notifications, and one-click upgrade -------------------------
REPO_URL = "https://github.com/czg86389-hub/muse2api"
TRACKED_REPO_PATHS = [
    "app.py", "engine.py", "store.py", "cdp.py", "config.py", "proxy.py",
    "admin.html", "README.md", "version.json", "requirements.txt",
    "Dockerfile", "docker-compose.yml", ".env.example", ".gitignore",
    "LICENSE", "extension", "deploy", "tools",
]
_UPDATE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _read_env_key(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                s = ln.strip()
                if s.startswith(key + "="):
                    return s.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _read_local_version() -> dict:
    p = os.path.join(BASE_DIR, "version.json")
    try:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {"version": "1.5.0", "highlights": []}


def _installed_sha_file() -> str:
    return os.path.join(CFG.data_dir, ".installed_sha")


def _git(args: list[str], timeout: int = 30, token: str = ""):
    """Run git in BASE_DIR.

    `token` authenticates GitHub network commands through GIT_CONFIG_* environment
    variables instead of a rewritten remote URL, so the credential never lands in
    .git/config, in `ps` output, or in an error message that echoes the remote.
    """
    import base64
    import subprocess
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        count = int(env.get("GIT_CONFIG_COUNT") or 0)
        env[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraheader"
        env[f"GIT_CONFIG_VALUE_{count}"] = f"Authorization: Basic {cred}"
        env["GIT_CONFIG_COUNT"] = str(count + 1)
    return subprocess.run(
        ["git", "-c", f"safe.directory={BASE_DIR}", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ensure_git_repo(token: str = ""):
    """Ensure BASE_DIR is a Git repo bound to czg86389-hub/muse2api.

    The stored remote URL is always token-free; callers pass `token` to the individual
    network commands. Re-running this also scrubs a tokenised origin written by older
    versions of this service.
    """
    git_dir = os.path.join(BASE_DIR, ".git")
    remote_url = f"{REPO_URL}.git"
    if not os.path.isdir(git_dir):
        _git(["init", "-b", "main"])
        _git(["remote", "add", "origin", remote_url])
        _git(["fetch", "origin", "main"], timeout=45, token=token)
        _git(["reset", "--mixed", "origin/main"])
    else:
        _git(["remote", "set-url", "origin", remote_url])
    _git(["config", "user.name", "czg86389-hub"])
    _git(["config", "user.email", "czg86389-hub@users.noreply.github.com"])


def _check_update_sync(force: bool = False) -> dict:
    """Check the official GitHub repo (czg86389-hub/muse2api) for a new version or commits.
    Results are cached for 90 seconds by default to avoid GitHub API rate limits from frequent refreshes."""
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and (now - _UPDATE_CACHE["ts"]) < 90:
        return _UPDATE_CACHE["data"]

    import requests
    token = _read_env_key("GITHUB_TOKEN")
    local_ver_obj = _read_local_version()
    local_version = str(local_ver_obj.get("version") or "1.5.0")

    has_git = os.path.isdir(os.path.join(BASE_DIR, ".git"))
    local_sha, local_msg, local_ts = "", "", 0
    if has_git:
        try:
            r_sha = _git(["rev-parse", "--short", "HEAD"])
            if r_sha.returncode == 0:
                local_sha = r_sha.stdout.strip()[:7]
            r_log = _git(["log", "-1", "--format=%s||%ct"])
            if r_log.returncode == 0 and "||" in r_log.stdout:
                parts = r_log.stdout.strip().split("||", 1)
                local_msg = parts[0]
                local_ts = int(parts[1])
        except Exception:
            pass

    if not local_sha and os.path.isfile(_installed_sha_file()):
        try:
            with open(_installed_sha_file(), encoding="utf-8") as f:
                local_sha = f.read().strip()[:7]
        except OSError:
            pass

    remote_version = local_version
    highlights = list(local_ver_obj.get("highlights") or [])
    highlights_en = list(local_ver_obj.get("highlights_en") or [])
    try:
        rv = requests.get(
            f"https://raw.githubusercontent.com/czg86389-hub/muse2api/main/version.json?t={int(now)}",
            timeout=6,
        )
        if rv.status_code == 200:
            rvj = rv.json()
            if isinstance(rvj, dict):
                remote_version = str(rvj.get("version") or remote_version)
                if rvj.get("highlights"):
                    highlights = list(rvj["highlights"])
                    # If the remote has no English description, reuse the local English text only when versions match; otherwise the UI falls back to Chinese
                    highlights_en = list(rvj.get("highlights_en")
                                         or (highlights_en if remote_version == local_version else []))
    except Exception:
        pass

    remote_sha, remote_msg, remote_time = "", "", ""
    recent_commits = []
    try:
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "muse2api-updater"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(
            "https://api.github.com/repos/czg86389-hub/muse2api/commits?sha=main&per_page=5",
            headers=headers,
            timeout=6,
        )
        if resp.status_code == 200 and isinstance(resp.json(), list):
            commits = resp.json()
            for idx, c in enumerate(commits):
                sha7 = (c.get("sha") or "")[:7]
                c_msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0]
                c_date = (((c.get("commit") or {}).get("committer") or {}).get("date") or "")
                c_url = c.get("html_url") or f"{REPO_URL}/commit/{sha7}"
                if idx == 0:
                    remote_sha = sha7
                    remote_msg = c_msg
                    remote_time = c_date
                recent_commits.append({
                    "sha": sha7,
                    "message": c_msg,
                    "date": c_date,
                    "url": c_url,
                })
    except Exception:
        pass

    # For Docker/ZIP deployments (no .git), record the initial baseline SHA when versions match on first run
    if not local_sha and remote_sha and local_version == remote_version:
        local_sha = remote_sha
        try:
            with open(_installed_sha_file(), "w", encoding="utf-8") as f:
                f.write(remote_sha)
        except OSError:
            pass

    def _parse_v(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in re.findall(r"\d+", v))
        except Exception:
            return (0,)

    has_update = False
    if remote_sha and local_sha:
        has_update = (remote_sha != local_sha)
    elif remote_version and local_version:
        has_update = _parse_v(remote_version) > _parse_v(local_version)

    data = {
        "repo_url": REPO_URL,
        "has_git": has_git,
        "local_version": local_version,
        "remote_version": remote_version,
        "local_sha": local_sha,
        "local_msg": local_msg,
        "local_ts": local_ts,
        "remote_sha": remote_sha,
        "remote_msg": remote_msg,
        "remote_time": remote_time,
        "has_update": has_update,
        "up_to_date": not has_update and bool(remote_sha or remote_version),
        "highlights": highlights,
        "highlights_en": highlights_en,
        "recent_commits": recent_commits,
        "checked_at": int(now),
    }
    _UPDATE_CACHE["ts"] = now
    _UPDATE_CACHE["data"] = data
    return data


def _upgrade_from_github_sync() -> dict:
    """Pull the latest code from GitHub and overwrite core files (works with Git and no-Git Docker/ZIP setups); never touches .env or data/."""
    import requests
    import tarfile

    token = _read_env_key("GITHUB_TOKEN")
    upgraded_via = ""
    try:
        _ensure_git_repo(token)
        f_res = _git(["fetch", "origin", "main"], timeout=45, token=token)
        if f_res.returncode == 0:
            _git(["checkout", "-f", "origin/main", "--", "."])
            _git(["reset", "--mixed", "origin/main"])
            upgraded_via = "git"
    except Exception as e:
        log.warning("Git pull failed; falling back to tarball update: %s", e)

    if not upgraded_via:
        resp = requests.get(
            "https://codeload.github.com/czg86389-hub/muse2api/tar.gz/refs/heads/main",
            timeout=60,
        )
        if resp.status_code != 200:
            raise HTTPException(502, f"Failed to download GitHub update package (HTTP {resp.status_code})")
        protected_files = {".env", "data/accounts.json", "data/tasks.json"}
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1].replace("\\", "/")
                if ".." in rel or rel in protected_files:
                    continue
                target_path = os.path.join(BASE_DIR, rel)
                if member.isdir():
                    os.makedirs(target_path, exist_ok=True)
                elif member.isfile():
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    fobj = tar.extractfile(member)
                    if fobj is not None:
                        with open(target_path, "wb") as out_f:
                            out_f.write(fobj.read())
        upgraded_via = "tarball"

    _UPDATE_CACHE["ts"] = 0.0
    status = _check_update_sync(force=True)
    if status.get("remote_sha"):
        try:
            with open(_installed_sha_file(), "w", encoding="utf-8") as f:
                f.write(status["remote_sha"])
            status["local_sha"] = status["remote_sha"]
            status["has_update"] = False
            status["up_to_date"] = True
        except OSError:
            pass
    return {
        "ok": True,
        "via": upgraded_via,
        "message": f"Successfully updated to the latest version {status.get('remote_version')} ({status.get('remote_sha')})",
        "status": status,
    }


@app.get("/admin/update/check")
@app.get("/admin/repo/status")
async def admin_check_update(force: bool = False):
    """Let every deployed node check the official GitHub repo for a new version in real time."""
    return await asyncio.to_thread(_check_update_sync, force)


@app.post("/admin/update/upgrade")
@app.post("/admin/repo/pull")
async def admin_upgrade_now(payload: dict = Body(default={}), _=Depends(maintainer_auth)):
    """One-click pull of the latest update from the official GitHub repo, with a smooth automatic restart."""
    res = await asyncio.to_thread(_upgrade_from_github_sync)
    restart = payload.get("restart", True) if isinstance(payload, dict) else True
    if restart:
        def _delayed_restart():
            time.sleep(0.5)
            try:
                engine.stop()
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_delayed_restart, daemon=True).start()
    return res


@app.post("/admin/repo/push")
async def admin_repo_push(payload: dict = Body(default={}), _=Depends(repo_push_auth)):
    """Maintainer-only: push this node's core code to the GitHub repo (automatically excludes .env and data/)."""
    msg = (payload.get("message") or "").strip() or f"chore: sync update ({time.strftime('%Y-%m-%d %H:%M:%S')})"
    new_token = (payload.get("github_token") or "").strip()
    if new_token:
        _persist_env("GITHUB_TOKEN", new_token)
        os.environ["GITHUB_TOKEN"] = new_token
    token = new_token or _read_env_key("GITHUB_TOKEN")

    def _do_push():
        _ensure_git_repo(token)
        existing_paths = [p for p in TRACKED_REPO_PATHS if os.path.exists(os.path.join(BASE_DIR, p))]
        _git(["add", "--", *existing_paths])
        st = _git(["status", "--porcelain", "--", *existing_paths])
        committed = False
        if st.stdout.strip():
            c_res = _git(["commit", "-m", msg])
            if c_res.returncode != 0:
                raise HTTPException(500, f"Git commit failed: {c_res.stderr or c_res.stdout}")
            committed = True
        p_res = _git(["push", "origin", "HEAD:main"], timeout=60, token=token)
        if p_res.returncode != 0:
            err = (p_res.stderr or p_res.stdout or "").strip()
            raise HTTPException(500, f"Git push failed: {err[:300]}")
        _UPDATE_CACHE["ts"] = 0.0
        status = _check_update_sync(force=True)
        return {
            "ok": True,
            "committed": committed,
            "message": "Successfully committed and pushed to the GitHub repo",
            "status": status,
        }

    return await asyncio.to_thread(_do_push)


@app.on_event("startup")
async def _startup():
    for task in list(store.tasks.values()):
        if task.get("kind") in ("image", "video") and task.get("status") in ("queued", "processing"):
            store.update_task(task["id"], status="failed", error="The service restart interrupted this task; submit it again")
    if not CFG.api_key:
        new_key = "m2a_" + secrets.token_hex(24)
        CFG.api_key = new_key
        _persist_env("MUSE2API_KEY", new_key)
        log.info("🔑 MUSE2API_KEY not found; generated an initial key: %s", new_key)
    if not CFG.maintainer_key:
        log.warning(
            "MUSE2API_MAINTAINER_KEY is not set: /admin/update/upgrade accepts the inference "
            "API key, which every downstream client holds. Set it to separate the two. "
            "/admin/repo/push stays disabled until it is set.")
    asyncio.create_task(_keepalive_loop())


@app.on_event("shutdown")
def _shutdown():
    try:
        store.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to flush deferred account bookkeeping: %s", exc)
    engine.stop()
