"""Observability: admission-rejection/recycle counters, /admin/status's observability
block, _http_exc's 401/429/5xx logging, and (if implemented) /metrics.

These counters are process-local and reset on restart -- they exist to give an
operator a live window into the current process's recent behaviour, not a durable
audit trail.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


@pytest.fixture(autouse=True)
def reset_observability_counters():
    app._CHAT_REJECTED = 0
    app._TASK_REJECTED = {"image": 0, "video": 0}
    yield
    app._CHAT_REJECTED = 0
    app._TASK_REJECTED = {"image": 0, "video": 0}


@pytest.fixture
def tasks(monkeypatch):
    store = SimpleNamespace(tasks={})
    monkeypatch.setattr(app, "store", store)
    return store


# ---------------------------- counter increments ----------------------------

def test_task_rejection_increments_the_per_kind_counter(tasks):
    tasks.tasks = {f"i{n}": {"kind": "image", "status": "queued"} for n in range(8)}
    with pytest.raises(app.HTTPException):
        app._admit_task("image", 8)
    assert app._TASK_REJECTED["image"] == 1
    assert app._TASK_REJECTED["video"] == 0


def test_chat_rejection_increments_the_counter(monkeypatch):
    monkeypatch.setattr(app.CFG, "chat_queue_max", 1)
    app._CHAT_ACTIVE = 1
    try:
        with pytest.raises(app.HTTPException):
            app._admit_chat()
        assert app._CHAT_REJECTED == 1
    finally:
        app._CHAT_ACTIVE = 0


# ---------------------------- /admin/status's new block ----------------------------

def test_admin_status_includes_observability_block(monkeypatch):
    monkeypatch.setattr(app, "_CHAT_REJECTED", 3)
    monkeypatch.setattr(app, "_TASK_REJECTED", {"image": 1, "video": 2})
    monkeypatch.setattr(app, "_CONSECUTIVE_GEN_FAILURES", 4)
    monkeypatch.setattr(app, "_RECYCLE_COUNT", 5)
    monkeypatch.setattr(app, "_LAST_RECYCLE_REASON", "unhealthy: boom")
    monkeypatch.setattr(app, "_LAST_RECYCLE_AT", 123.0)

    result = app.admin_status(True)

    assert result["observability"] == {
        "chat_rejected": 3,
        "task_rejected": {"image": 1, "video": 2},
        "consecutive_generation_failures": 4,
        "recycle_count": 5,
        "last_recycle_reason": "unhealthy: boom",
        "last_recycle_at": 123.0,
    }


def test_admin_status_observability_defaults(monkeypatch):
    monkeypatch.setattr(app, "_CHAT_REJECTED", 0)
    monkeypatch.setattr(app, "_TASK_REJECTED", {"image": 0, "video": 0})
    monkeypatch.setattr(app, "_CONSECUTIVE_GEN_FAILURES", 0)
    monkeypatch.setattr(app, "_RECYCLE_COUNT", 0)
    monkeypatch.setattr(app, "_LAST_RECYCLE_REASON", None)
    monkeypatch.setattr(app, "_LAST_RECYCLE_AT", 0.0)

    result = app.admin_status(True)

    block = result["observability"]
    assert block["chat_rejected"] == 0
    assert block["task_rejected"] == {"image": 0, "video": 0}
    assert block["consecutive_generation_failures"] == 0
    assert block["recycle_count"] == 0
    assert block["last_recycle_reason"] is None
    assert block["last_recycle_at"] is None


# ---------------------------- _http_exc logging ----------------------------

def _request(path: str = "/admin/status") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


def test_http_exc_logs_429(caplog):
    import asyncio
    with caplog.at_level("WARNING", logger="muse2api"):
        asyncio.run(app._http_exc(_request(), HTTPException(429, "Chat queue is full")))
    assert any(r.levelname == "WARNING" and "429" in r.getMessage() for r in caplog.records)


def test_http_exc_logs_401(caplog):
    import asyncio
    with caplog.at_level("WARNING", logger="muse2api"):
        asyncio.run(app._http_exc(_request(), HTTPException(401, "Unauthorized")))
    assert any(r.levelname == "WARNING" and "401" in r.getMessage() for r in caplog.records)


def test_http_exc_logs_5xx_at_error_level(caplog):
    import asyncio
    with caplog.at_level("ERROR", logger="muse2api"):
        asyncio.run(app._http_exc(_request(), HTTPException(502, "Upstream failed")))
    assert any(r.levelname == "ERROR" and "502" in r.getMessage() for r in caplog.records)


def test_http_exc_does_not_log_404(caplog):
    import asyncio
    with caplog.at_level("WARNING", logger="muse2api"):
        asyncio.run(app._http_exc(_request(), HTTPException(404, "Not found")))
    assert not any(r.name == "muse2api" for r in caplog.records)


# ---------------------------- /metrics (item 7) ----------------------------

def test_metrics_returns_prometheus_text(monkeypatch):
    monkeypatch.setattr(app.CFG, "api_key", "")  # unauthenticated in this test env, matches other admin-route tests
    response = app.metrics(True)
    body = response.body.decode()
    assert "# TYPE muse2api_chat_rejected_total counter" in body
    assert 'muse2api_task_rejected_total{kind="image"}' in body
    assert 'muse2api_task_rejected_total{kind="video"}' in body
    assert "# TYPE muse2api_accounts gauge" in body
    assert response.media_type == "text/plain; version=0.0.4"


def test_metrics_requires_auth_when_key_configured(monkeypatch):
    monkeypatch.setattr(app.CFG, "api_key", "secret-key")
    with pytest.raises(app.HTTPException) as exc:
        app.auth(authorization=None)
    assert exc.value.status_code == 401
    # /metrics depends on the same `auth` callable FastAPI would inject per-request;
    # this proves the dependency itself rejects a missing key the same way it does
    # for every other /admin/* route -- app.metrics's own Depends(auth) wiring is
    # exercised by FastAPI's dependency injection at request time, not by a direct
    # Python call in this test.
