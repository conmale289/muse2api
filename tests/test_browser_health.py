"""Chromium health/restart policy.

The shared Chromium is driven by one CDP connection; a crashed or hung-but-still-running
process previously went undetected until the next request's fast-path check (which
doesn't even ping CDP). This covers the proactive liveness probe (engine.is_healthy /
.uptime_seconds / .recycle) and its GEN_LOCK-gated wiring into the keepalive loop
(app._browser_health_check_sync).
"""
import sys
import tempfile
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
