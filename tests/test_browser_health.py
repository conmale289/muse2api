"""Chromium health/restart policy.

The shared Chromium is driven by one CDP connection; a crashed or hung-but-still-running
process previously went undetected until the next request's fast-path check (which
doesn't even ping CDP). This covers the proactive liveness probe (engine.is_healthy /
.uptime_seconds / .recycle) and its GEN_LOCK-gated wiring into the keepalive loop
(app._browser_health_check_sync).
"""
import sys
import tempfile
import time
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app
import engine as engine_module
from config import Config


# ---------------------------- engine.py-level unit tests ----------------------------

@pytest.fixture
def fresh_engine():
    with tempfile.TemporaryDirectory() as home:
        cfg = Config(base_dir=home, profile_root=home)
        yield engine_module.MuseEngine(cfg)


def test_is_healthy_false_when_not_started(fresh_engine):
    assert fresh_engine.is_healthy() == (False, "not started")


def test_is_healthy_false_when_process_exited(fresh_engine):
    fresh_engine.proc = type("P", (), {"poll": lambda self: 1})()
    fresh_engine.browser = object()
    assert fresh_engine.is_healthy() == (False, "process exited (code 1)")


def test_is_healthy_false_when_cdp_ping_fails(fresh_engine, monkeypatch):
    fresh_engine.proc = type("P", (), {"poll": lambda self: None})()
    fresh_engine.browser = object()

    def raiser(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(engine_module, "http_json", raiser)

    healthy, detail = fresh_engine.is_healthy()
    assert healthy is False
    assert "CDP endpoint unresponsive" in detail


def test_is_healthy_true_when_ping_succeeds(fresh_engine, monkeypatch):
    fresh_engine.proc = type("P", (), {"poll": lambda self: None})()
    fresh_engine.browser = object()
    monkeypatch.setattr(engine_module, "http_json", lambda *a, **k: {"ok": True})

    assert fresh_engine.is_healthy() == (True, "ok")


def test_uptime_seconds_zero_before_start(fresh_engine):
    assert fresh_engine._started_at == 0.0
    assert fresh_engine.uptime_seconds == 0.0


def test_recycle_calls_stop(fresh_engine, monkeypatch):
    calls = []
    monkeypatch.setattr(fresh_engine, "stop", lambda: calls.append(1))

    fresh_engine.recycle("reason")

    assert calls == [1]


# ---------------------------- app.py-level wiring tests ----------------------------

class StubEngine:
    def __init__(self, healthy=(True, "ok"), uptime=0.0):
        self._healthy = healthy
        self.uptime_seconds = uptime
        self.recycle_calls = []

    def is_healthy(self):
        return self._healthy

    def recycle(self, reason):
        self.recycle_calls.append(reason)


@pytest.fixture(autouse=True)
def gen_lock_released():
    # Guard against a prior failed test leaking a held GEN_LOCK into this file.
    assert not app.GEN_LOCK.locked()
    yield
    while app.GEN_LOCK.locked():
        app.GEN_LOCK.release()


def test_health_check_noop_when_healthy_and_uptime_disabled(monkeypatch):
    stub = StubEngine(healthy=(True, "ok"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)

    result = app._browser_health_check_sync()

    assert result == {"recycled": False}
    assert stub.recycle_calls == []


def test_health_check_recycles_when_gen_lock_free(monkeypatch):
    stub = StubEngine(healthy=(False, "boom"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)

    result = app._browser_health_check_sync()

    assert stub.recycle_calls == ["unhealthy: boom"]
    assert result["recycled"] is True
    assert "boom" in result["reason"]
    assert app.GEN_LOCK.locked() is False


def test_health_check_defers_when_gen_lock_held(monkeypatch):
    stub = StubEngine(healthy=(False, "boom"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)

    # GEN_LOCK is a counting semaphore sized to CFG.max_concurrency (>= 1); exhaust every
    # slot so the health check's own non-blocking acquire genuinely fails, simulating
    # "a generation is in-flight" regardless of how many concurrent slots are configured.
    held = [app.GEN_LOCK.acquire(blocking=False) for _ in range(app.GEN_LOCK.limit)]
    assert all(held)
    try:
        result = app._browser_health_check_sync()
    finally:
        for _ in held:
            app.GEN_LOCK.release()

    assert result == {"recycled": False, "deferred": True, "reason": "unhealthy: boom"}
    assert stub.recycle_calls == []


def test_max_uptime_trigger(monkeypatch):
    stub = StubEngine(healthy=(True, "ok"), uptime=99999.0)
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0.001)

    result = app._browser_health_check_sync()

    assert stub.recycle_calls
    assert "max uptime exceeded" in stub.recycle_calls[0]
    assert result["recycled"] is True


# ---------------------- _note_generation_result unit tests ----------------------

@pytest.fixture(autouse=True)
def reset_consecutive_gen_failures():
    app._CONSECUTIVE_GEN_FAILURES = 0
    yield
    app._CONSECUTIVE_GEN_FAILURES = 0


def test_note_generation_result_resets_on_success():
    app._CONSECUTIVE_GEN_FAILURES = 3
    app._note_generation_result(None)
    assert app._CONSECUTIVE_GEN_FAILURES == 0


def test_note_generation_result_increments_on_plain_generation_error():
    app._CONSECUTIVE_GEN_FAILURES = 0
    app._note_generation_result(app.MuseGenerationError("timeout"))
    app._note_generation_result(app.MuseGenerationError("timeout"))
    assert app._CONSECUTIVE_GEN_FAILURES == 2


def test_note_generation_result_ignores_auth_error():
    app._CONSECUTIVE_GEN_FAILURES = 0
    app._note_generation_result(app.MuseAuthError("bad session"))
    assert app._CONSECUTIVE_GEN_FAILURES == 0


@pytest.mark.parametrize("exc_cls", [
    app.MuseProxyError, app.MuseModelRefusalError, app.RefImageSafetyError])
def test_note_generation_result_ignores_proxy_and_refusal_and_ref_image_errors(exc_cls):
    app._CONSECUTIVE_GEN_FAILURES = 0
    app._note_generation_result(exc_cls("nope"))
    assert app._CONSECUTIVE_GEN_FAILURES == 0


# ---------------------- third recycle trigger tests ----------------------

def test_consecutive_failure_trigger_recycles(monkeypatch):
    stub = StubEngine(healthy=(True, "ok"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)
    monkeypatch.setattr(app.CFG, "browser_max_consecutive_failures", 5)
    app._CONSECUTIVE_GEN_FAILURES = 5

    result = app._browser_health_check_sync()

    assert result["recycled"] is True
    assert "consecutive generation failures" in result["reason"]
    assert app._CONSECUTIVE_GEN_FAILURES == 0


def test_consecutive_failure_trigger_disabled_by_default_zero(monkeypatch):
    stub = StubEngine(healthy=(True, "ok"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)
    monkeypatch.setattr(app.CFG, "browser_max_consecutive_failures", 0)
    app._CONSECUTIVE_GEN_FAILURES = 999

    result = app._browser_health_check_sync()

    assert result == {"recycled": False}


def test_consecutive_failure_trigger_below_threshold_is_a_noop(monkeypatch):
    stub = StubEngine(healthy=(True, "ok"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)
    monkeypatch.setattr(app.CFG, "browser_max_consecutive_failures", 5)
    app._CONSECUTIVE_GEN_FAILURES = 4

    result = app._browser_health_check_sync()

    assert result == {"recycled": False}


def test_is_healthy_false_takes_priority_over_consecutive_failures(monkeypatch):
    stub = StubEngine(healthy=(False, "boom"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)
    monkeypatch.setattr(app.CFG, "browser_max_consecutive_failures", 5)
    app._CONSECUTIVE_GEN_FAILURES = 999

    result = app._browser_health_check_sync()

    assert result["reason"] == "unhealthy: boom"


# ---------------------- recycle-counter bookkeeping tests ----------------------

def test_recycle_updates_counters(monkeypatch):
    stub = StubEngine(healthy=(False, "boom"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)
    before = app._RECYCLE_COUNT

    result = app._browser_health_check_sync()

    assert result["recycled"] is True
    assert app._RECYCLE_COUNT == before + 1
    assert app._LAST_RECYCLE_REASON == "unhealthy: boom"
    assert abs(time.time() - app._LAST_RECYCLE_AT) < 5


def test_deferred_recycle_does_not_touch_counters(monkeypatch):
    stub = StubEngine(healthy=(False, "boom"))
    monkeypatch.setattr(app, "engine", stub)
    monkeypatch.setattr(app.CFG, "browser_max_uptime_hours", 0)
    before_count = app._RECYCLE_COUNT
    before_reason = app._LAST_RECYCLE_REASON

    held = [app.GEN_LOCK.acquire(blocking=False) for _ in range(app.GEN_LOCK.limit)]
    assert all(held)
    try:
        result = app._browser_health_check_sync()
    finally:
        for _ in held:
            app.GEN_LOCK.release()

    assert result["recycled"] is False
    assert app._RECYCLE_COUNT == before_count
    assert app._LAST_RECYCLE_REASON == before_reason


# ------------- end-to-end wiring tests using real _run_generation/safe_chat_stream -------------

class _GenFakeEngine:
    current_acc_id = None

    def __init__(self, exc=None):
        self.exc = exc

    def start(self):
        pass

    def close_current_page(self):
        pass

    def reset_thread(self):
        pass

    def generate(self, *args, **kwargs):
        if self.exc is not None:
            raise self.exc
        return {"filename": "x.png", "size": 1, "kind": "image"}


class _GenStubPool:
    def acquire(self, **kwargs):
        return {"id": "acc-1", "label": "stub", "cookies": {}, "cookies_exp": {}}

    def release(self, account_id):
        pass


@pytest.fixture
def run_generation_env(monkeypatch):
    monkeypatch.setattr(app, "ACCOUNT_POOL", _GenStubPool())
    monkeypatch.setattr(app, "store", app.store)
    monkeypatch.setattr(app.store, "mark", lambda *a, **k: None)
    monkeypatch.setattr(app, "_sync_cookies", lambda *a, **k: None)
    monkeypatch.setattr(app, "_renew_and_persist", lambda *a, **k: None)


def test_run_generation_failure_increments_the_streak(run_generation_env, monkeypatch):
    monkeypatch.setattr(app, "engine", _GenFakeEngine(exc=app.MuseGenerationError("boom")))
    app._CONSECUTIVE_GEN_FAILURES = 0

    with pytest.raises(app.MuseGenerationError):
        app._run_generation("a lighthouse", "image", 5)

    assert app._CONSECUTIVE_GEN_FAILURES == 1


def test_run_generation_success_resets_the_streak(run_generation_env, monkeypatch):
    monkeypatch.setattr(app, "engine", _GenFakeEngine())
    app._CONSECUTIVE_GEN_FAILURES = 3

    app._run_generation("a lighthouse", "image", 5)

    assert app._CONSECUTIVE_GEN_FAILURES == 0


class _ChatFakeEngine:
    def __init__(self, exc=None):
        self.exc = exc

    def start(self):
        pass

    def chat_stream(self, cookies, prompt, expires, timeout, account_id=None, stop_event=None):
        if self.exc is not None:
            raise self.exc
        yield "chunk-0"

    def close_current_page(self):
        pass

    def reset_thread(self):
        pass


class _ChatStubPool:
    def acquire(self, **kwargs):
        return {"id": "acc-1", "label": "stub", "cookies": {}, "cookies_exp": {}}

    def release(self, account_id):
        pass


@pytest.fixture
def safe_chat_stream_env(monkeypatch):
    monkeypatch.setattr(app, "ACCOUNT_POOL", _ChatStubPool())
    monkeypatch.setattr(app, "_renew_and_persist", lambda *a, **k: None)


def test_safe_chat_stream_failure_increments_the_streak(safe_chat_stream_env, monkeypatch):
    monkeypatch.setattr(app, "engine", _ChatFakeEngine(exc=app.MuseGenerationError("timeout")))
    app._CONSECUTIVE_GEN_FAILURES = 0

    with pytest.raises(app.MuseGenerationError):
        list(app.safe_chat_stream({}, "hello", None, 30, "acc-1"))

    assert app._CONSECUTIVE_GEN_FAILURES == 1


def test_safe_chat_stream_success_resets_the_streak(safe_chat_stream_env, monkeypatch):
    monkeypatch.setattr(app, "engine", _ChatFakeEngine())
    app._CONSECUTIVE_GEN_FAILURES = 2

    list(app.safe_chat_stream({}, "hi", None, 30, "acc-1"))

    assert app._CONSECUTIVE_GEN_FAILURES == 0
