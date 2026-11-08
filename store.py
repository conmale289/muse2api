"""Account pool and task storage (JSON on disk, atomic writes)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid

from contextlib import contextmanager

from proxy import public_proxy

log = logging.getLogger("muse2api.store")

_LOCK = threading.Lock()

# Login-session presence check only; availability still requires upstream auth
# (same rule as engine). hatch_vml is an optional workspace token, so its absence
# must not reject a session import.
ESSENTIAL_COOKIES = ("hatch_sess",)


def _is_pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


# hatch_vml lasts about two days and is not extended by use; it is the cookie that
# determines account lifetime. Observed: synced_at changed and new cookies appeared
# after generation, but the expires values of the four core cookies never moved.
# If hatch_vml's expires is unavailable, estimate from import time + 2 days instead
# of falling back to hatch_native_auth_device's 30 days (that value does not decide
# lifetime and would mislead users).
VML_TTL = 2 * 86400


def min_expiry(cookies_exp: dict | None) -> int | None:
    """Earliest expiry among the core cookies (generic fallback; does not decide
    which cookie determines lifetime)."""
    if not cookies_exp:
        return None
    vals = []
    for name in ESSENTIAL_COOKIES:
        v = cookies_exp.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if not vals:
        for v in cookies_exp.values():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
    return min(vals) if vals else None


def account_expiry(cookies_exp: dict | None,
                   base_ts: int | None = None) -> int | None:
    """Effective account expiry time.

    Priority:
      1) hatch_vml's real expires value (most accurate);
      2) otherwise estimate base_ts (import/sync time) + VML_TTL, since lifetime is 2 days;
      3) otherwise fall back to the earliest core-cookie expiry.
    """
    ce = cookies_exp or {}
    vml = ce.get("hatch_vml")
    if _is_pos(vml):
        return int(vml)
    if base_ts:
        return int(base_ts) + VML_TTL
    return min_expiry(ce)


class StoreCorruptError(RuntimeError):
    """A data file exists but cannot be parsed and could not be moved aside."""


def _read(path: str, default, corrupt_files: list | None = None, expect=None):
    """Load JSON, distinguishing "missing" from "unusable".

    A missing file legitimately means "empty state". An unreadable one — bad JSON *or*
    valid JSON of the wrong shape — must never be reported as empty: the next _write
    would persist that empty state and destroy every account cookie or task record with
    no way back. So move the bad file aside, log loudly, and record it for the admin
    status endpoint. If it cannot even be moved aside, fail closed rather than proceed
    towards overwriting it.
    """
    if not os.path.isfile(path):
        return default
    data = default
    problem = ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if expect is not None and not isinstance(data, expect):
            problem = (f"expected {expect.__name__} at the top level, "
                       f"got {type(data).__name__}")
    except Exception as exc:  # noqa: BLE001
        data = default
        problem = str(exc)
    if not problem:
        return data

    backup = f"{path}.corrupt-{int(time.time())}"
    try:
        os.replace(path, backup)
    except OSError as move_exc:
        raise StoreCorruptError(
            f"{path} is unusable ({problem}) and could not be moved aside ({move_exc}); "
            "repair or remove it before starting the service") from move_exc
    log.error(
        "Unusable data at %s (%s). Original preserved at %s; serving empty state. "
        "Restore that file and restart to recover.", path, problem, backup)
    if corrupt_files is not None:
        corrupt_files.append({"path": path, "backup": backup, "error": problem})
    return default


def _write(path: str, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        # Populated when a data file had to be quarantined; surfaced by /admin/status.
        self.corrupt_files: list[dict] = []
        self.accounts: list[dict] = _read(
            cfg.accounts_file, [], self.corrupt_files, expect=list)
        for a in self.accounts:
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                a["expires_at"] = int(ce["hatch_vml"])
        self.tasks: dict[str, dict] = _read(
            cfg.tasks_file, {}, self.corrupt_files, expect=dict)
        # key hash -> task id, so idempotency lookup does not scan every task.
        self._idem_index: dict[str, str] = {}
        for tid, task in self.tasks.items():
            key = task.get("image_request_key")
            if key:
                self._idem_index[key] = tid
        # last_used/use_count are hot-path bookkeeping; flushing them on every pick meant
        # rewriting every account's cookies for each request.
        self._accounts_dirty = False
        self._last_accounts_flush = time.monotonic()
        self._defer_writes = False

    # ---------- Accounts ----------
    def add_account(self, cookies: dict, label: str = "",
                    cookies_exp: dict | None = None) -> dict:
        with _LOCK:
            aid = uuid.uuid4().hex[:12]
            exp = {k: int(v) for k, v in (cookies_exp or {}).items()
                   if _is_pos(v)}
            now = int(time.time())
            acc = {"id": aid, "label": label or aid, "cookies": cookies,
                   "cookies_exp": exp, "expires_at": account_expiry(exp, now),
                   "expiry_anchor": None,
                   "enabled": True, "created_at": now,
                   "last_used": None, "last_keepalive": None, "use_count": 0,
                   "ok": None, "note": "", "synced_at": None}
            self.accounts.append(acc)
            self._save_accounts_locked(force=True)
            return acc

    @staticmethod
    def public_account(account: dict) -> dict:
        item = {k: v for k, v in account.items() if k not in ("cookies", "proxy")}
        item["proxy"] = public_proxy(account.get("proxy"))
        item["cookie_count"] = len(account.get("cookies", {}))
        return item

    def list_accounts(self) -> list[dict]:
        out = []
        for a in self.accounts:
            item = self.public_account(a)
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                item["expires_at"] = int(ce["hatch_vml"])
            item["cookie_count"] = len(a.get("cookies", {}))
            item["essential_ok"] = all(
                a.get("cookies", {}).get(n) for n in ESSENTIAL_COOKIES)
            # Whether expiry comes from hatch_vml's measured expires or a 2-day estimate.
            item["expires_estimated"] = not _is_pos(ce.get("hatch_vml"))
            out.append(item)
        return out

    def delete_account(self, aid: str) -> bool:
        with _LOCK:
            n = len(self.accounts)
            self.accounts = [a for a in self.accounts if a["id"] != aid]
            self._save_accounts_locked(force=True)
            return len(self.accounts) != n

    def pick_account(self, preferred_id: str | None = None, rotate: bool = True,
                     force_rotate: bool = False, exclude_id: str | None = None,
                     exclude_ids: set[str] | list[str] | None = None,
                     record_use: bool = True) -> dict | None:
        """Select an available account. Prefer reusing the currently warmed healthy
        account (to avoid reconnect cost); rotate after 15 sticky uses or on error.
        exclude_ids: account IDs to exclude (for example accounts busy in other
        concurrent threads)."""
        with _LOCK:
            live = [a for a in self.accounts if a.get("enabled", True) and a.get("cookies")]
            if not live:
                return None
            blocked = set(exclude_ids or [])
            if exclude_id:
                blocked.add(exclude_id)
            if blocked:
                available = [a for a in live if a["id"] not in blocked]
                if available:
                    live = available
                elif exclude_ids is not None:
                    # In concurrent mode every busy account must be avoided, but no
                    # idle account remains.
                    return None
                elif len(live) > 1:
                    live = [a for a in live if a["id"] != exclude_id] or live
            if preferred_id and not force_rotate:
                for a in live:
                    if a["id"] == preferred_id and a.get("ok") is not False:
                        sticky_n = getattr(self, "_sticky_count", 0) if getattr(self, "_sticky_id", None) == preferred_id else 0
                        if not rotate or sticky_n < 15:
                            self._sticky_id = preferred_id
                            self._sticky_count = sticky_n + 1
                            if record_use:
                                self._record_use(a)
                            return a
            healthy = [a for a in live if a.get("ok") is not False]
            candidates = healthy if healthy else live
            candidates.sort(key=lambda a: (a.get("last_used") or 0.0, a.get("use_count") or 0))
            acc = candidates[0]
            self._sticky_id = acc["id"]
            self._sticky_count = 1
            if record_use:
                self._record_use(acc)
            return acc

    def _record_use(self, account: dict) -> None:
        """Update LRU bookkeeping in memory and flush at most once a minute.

        Writing accounts.json (every account's full cookie jar) on every pick dominated
        per-request cost. The in-memory values are what pick_account sorts by, so only a
        crash loses this bookkeeping, and only for the last minute.
        """
        account["last_used"] = time.time()
        account["use_count"] = account.get("use_count", 0) + 1
        self._accounts_dirty = True
        now = time.monotonic()
        if now - self._last_accounts_flush >= 60:
            self._flush_accounts_locked()

    def _save_accounts_locked(self, force: bool = False) -> None:
        """Persist the account file unless a batch is in progress.

        `force` is for structural changes (add/delete/import) that must be durable even
        while a keepalive sweep is batching status updates.
        """
        self._accounts_dirty = True
        if force or not self._defer_writes:
            self._flush_accounts_locked()

    def _flush_accounts_locked(self) -> None:
        self._accounts_dirty = False
        self._last_accounts_flush = time.monotonic()
        _write(self.cfg.accounts_file, self.accounts)

    @contextmanager
    def batch_writes(self):
        """Collapse the per-account writes of a sweep into a single flush."""
        with _LOCK:
            previous = self._defer_writes
            self._defer_writes = True
        try:
            yield
        finally:
            with _LOCK:
                self._defer_writes = previous
                if not self._defer_writes and self._accounts_dirty:
                    self._flush_accounts_locked()

    def flush(self) -> None:
        """Persist deferred bookkeeping (called on shutdown)."""
        with _LOCK:
            if self._accounts_dirty:
                self._flush_accounts_locked()

    def touch_keepalive(self, aid: str, ok: bool | None = True, note: str = ""):
        """ok=None records an unconfirmed check only; keeps the previous account
        status, successful keepalive time, and expiry."""
        with _LOCK:
            now_ts = int(time.time())
            for a in self.accounts:
                if a["id"] == aid:
                    if ok is not None:
                        a["ok"] = ok
                    if ok is True:
                        a["last_keepalive"] = now_ts
                        # Only trust the real cookie expiry or the original import
                        # anchor; never inflate 48h based on a successful check.
                        a["expires_at"] = account_expiry(
                            a.get("cookies_exp"), a.get("expiry_anchor") or a.get("created_at"))
                    if note:
                        a["note"] = note[:300]
                    a["checked_at"] = now_ts
            self._save_accounts_locked()

    def mark(self, aid: str, ok: bool, note: str = ""):
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    a["ok"] = ok
                    a["note"] = note[:300]
                    a["checked_at"] = int(time.time())
            self._save_accounts_locked()

    def get_account(self, aid: str) -> dict | None:
        for a in self.accounts:
            if a["id"] == aid:
                return a
        return None

    def update_account(self, aid: str, **kw) -> dict | None:
        """Update label / enabled state / cookies / expiry, etc. None values are skipped.

        Passing cookies_exp automatically recalculates expires_at, so callers do not
        need to compute it. Maintenance rules for the expiry_anchor estimate (to
        avoid repeatedly inflating expiry):
          - real hatch_vml expires read        -> clear the anchor and use the real value;
          - unavailable and anchor is empty    -> pin the anchor to now (once);
          - brand-new session cookie (re-login) -> caller explicitly passes expiry_anchor=now.
        """
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    for k, v in kw.items():
                        if v is not None or k == "ok":
                            a[k] = v
                    if "cookies_exp" in kw and kw["cookies_exp"] is not None:
                        ce = a["cookies_exp"]
                        if _is_pos(ce.get("hatch_vml")):
                            a["expiry_anchor"] = None
                        elif not a.get("expiry_anchor"):
                            a["expiry_anchor"] = int(time.time())
                        a["expires_at"] = account_expiry(
                            ce, a.get("expiry_anchor") or a.get("created_at"))
                    self._save_accounts_locked()
                    return a
        return None

    def stats(self) -> dict:
        now = int(time.time())
        total = len(self.accounts)
        enabled = sum(1 for a in self.accounts if a.get("enabled", True))
        bad = sum(1 for a in self.accounts
                  if a.get("enabled", True) and a.get("ok") is False)
        healthy = sum(1 for a in self.accounts
                      if a.get("enabled", True) and a.get("ok") is True)
        expiring = sum(1 for a in self.accounts
                       if a.get("enabled", True)
                       and a.get("expires_at")
                       and a["expires_at"] - now < 3 * 86400)
        expired = sum(1 for a in self.accounts
                      if a.get("enabled", True)
                      and a.get("expires_at") and a["expires_at"] <= now)
        return {"total": total, "enabled": enabled, "disabled": total - enabled,
                "healthy": healthy, "error": bad,
                "expiring": expiring, "expired": expired}

    # ---------- Tasks ----------
    def create_task(self, kind: str, prompt: str) -> dict:
        with _LOCK:
            tid = "task_" + uuid.uuid4().hex[:20]
            t = {"id": tid, "object": "task", "kind": kind, "prompt": prompt,
                 "status": "queued", "created_at": int(time.time()),
                 "updated_at": int(time.time()), "result": None, "error": None}
            self.tasks[tid] = t
            self._prune_tasks_locked()
            _write(self.cfg.tasks_file, self.tasks)
            return t

    def update_task(self, tid: str, **kw):
        with _LOCK:
            t = self.tasks.get(tid)
            if not t:
                return None
            t.update(kw)
            t["updated_at"] = int(time.time())
            key = kw.get("image_request_key")
            if key:
                self._idem_index[key] = tid
            if kw.get("status") in ("completed", "failed"):
                self._prune_tasks_locked()
            _write(self.cfg.tasks_file, self.tasks)
            return t

    def find_task_by_idempotency(self, key_hash: str) -> dict | None:
        """Look up a task by its Idempotency-Key hash without scanning every task."""
        with _LOCK:
            tid = self._idem_index.get(key_hash)
            if not tid:
                return None
            task = self.tasks.get(tid)
            if task is None:
                self._idem_index.pop(key_hash, None)
            return task

    def _prune_tasks_locked(self) -> None:
        """Keep at most cfg.task_retention tasks, dropping the oldest finished ones.

        Queued/processing tasks are never pruned: dropping one would orphan a running
        worker that still reports progress to that id.
        """
        keep = getattr(self.cfg, "task_retention", 0) or 0
        excess = len(self.tasks) - keep if keep > 0 else 0
        if excess <= 0:
            return
        terminal = [(tid, task) for tid, task in self.tasks.items()
                    if task.get("status") in ("completed", "failed")]
        terminal.sort(key=lambda item: item[1].get("created_at") or 0)
        for tid, task in terminal[:excess]:
            self.tasks.pop(tid, None)
            self._forget_idempotency(task)

    def get_task(self, tid: str) -> dict | None:
        return self.tasks.get(tid)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        items = sorted(self.tasks.values(),
                       key=lambda t: t.get("created_at") or 0, reverse=True)
        return items[:limit]

    def delete_task(self, tid: str) -> bool:
        with _LOCK:
            task = self.tasks.pop(tid, None)
            if task is None:
                return False
            self._forget_idempotency(task)
            _write(self.cfg.tasks_file, self.tasks)
            return True

    def clear_tasks(self, keep: int = 0) -> int:
        with _LOCK:
            items = sorted(self.tasks.values(),
                           key=lambda t: t.get("created_at") or 0, reverse=True)
            keep_ids = {t["id"] for t in items[:keep]}
            removed = len(self.tasks) - len(keep_ids)
            for tid in [t for t in self.tasks if t not in keep_ids]:
                self._forget_idempotency(self.tasks.pop(tid))
            _write(self.cfg.tasks_file, self.tasks)
            return max(0, removed)

    def _forget_idempotency(self, task: dict | None) -> None:
        key = (task or {}).get("image_request_key")
        if key and self._idem_index.get(key) == (task or {}).get("id"):
            self._idem_index.pop(key, None)
