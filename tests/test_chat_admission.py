"""Admission cap for /v1/chat/completions and /v1/responses.

Chat/responses have no persisted task row like image/video do (see
tests/test_admission.py for that path), so admission is a simple in-memory counter
(app._CHAT_ACTIVE) that must return to 0 after every request path: success, error,
and client disconnect mid-stream.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


@pytest.fixture(autouse=True)
def reset_chat_active():
    app._CHAT_ACTIVE = 0
    yield
    app._CHAT_ACTIVE = 0


@pytest.fixture
def no_engine(monkeypatch):
    # The endpoint only needs safe_chat_stream; never touch the browser.
    monkeypatch.setattr(app, "engine", type("E", (), {"current_acc_id": None})())


def _stream_of(*chunks):
    return iter(chunks)


def _collect(response) -> list[str]:
    async def run():
        return [chunk async for chunk in response.body_iterator]
    return asyncio.run(run())


# ---------------------------- Unit tests on the primitive ----------------------------

def test_admit_chat_blocks_at_cap(monkeypatch):
    monkeypatch.setattr(app.CFG, "chat_queue_max", 2)
    app._admit_chat()
    app._admit_chat()
    with pytest.raises(app.HTTPException) as exc:
        app._admit_chat()
    assert exc.value.status_code == 429
    assert "Chat queue is full" in str(exc.value.detail)


def test_release_chat_frees_a_slot(monkeypatch):
    monkeypatch.setattr(app.CFG, "chat_queue_max", 1)
    app._admit_chat()
    with pytest.raises(app.HTTPException):
        app._admit_chat()
    app._release_chat()
    app._admit_chat()  # succeeds now that a slot was freed


def test_release_chat_never_goes_negative():
    app._release_chat()
    app._release_chat()
    app._release_chat()
    assert app._CHAT_ACTIVE == 0


def test_chat_queue_max_is_configurable():
    assert app.CFG.chat_queue_max >= 1


# ---------------------------- Endpoint-wiring tests ----------------------------

def test_chat_completions_releases_slot_after_success(no_engine, monkeypatch):
    monkeypatch.setattr(app, "safe_chat_stream", lambda *a, **k: _stream_of("hi"))
    req = app.ChatRequest(messages=[{"role": "user", "content": "hello"}])

    asyncio.run(app.chat_completions(req, True))

    assert app._CHAT_ACTIVE == 0


def test_chat_completions_releases_slot_after_streaming(no_engine, monkeypatch):
    monkeypatch.setattr(app, "safe_chat_stream",
                        lambda *a, **k: _stream_of("hello ", "world"))
    req = app.ChatRequest(messages=[{"role": "user", "content": "hello"}], stream=True)

    response = asyncio.run(app.chat_completions(req, True))
    _collect(response)

    assert app._CHAT_ACTIVE == 0


def test_chat_completions_releases_slot_on_early_stream_close(no_engine, monkeypatch):
    monkeypatch.setattr(app, "safe_chat_stream",
                        lambda *a, **k: _stream_of(*[f"chunk-{i}" for i in range(1000)]))
    req = app.ChatRequest(messages=[{"role": "user", "content": "hello"}], stream=True)

    response = asyncio.run(app.chat_completions(req, True))

    async def partial_then_close():
        received = []
        async for chunk in response.body_iterator:
            received.append(chunk)
            if len(received) >= 3:
                break
        await response.body_iterator.aclose()
        return received

    received = asyncio.run(partial_then_close())

    assert len(received) == 3
    assert app._CHAT_ACTIVE == 0


def test_chat_completions_429_when_cap_reached(no_engine, monkeypatch):
    monkeypatch.setattr(app.CFG, "chat_queue_max", 1)
    app._CHAT_ACTIVE = 1

    def should_not_be_called(*a, **k):
        raise AssertionError("should not be called")
    monkeypatch.setattr(app, "safe_chat_stream", should_not_be_called)

    req = app.ChatRequest(messages=[{"role": "user", "content": "hello"}])

    with pytest.raises(app.HTTPException) as exc:
        asyncio.run(app.chat_completions(req, True))
    assert exc.value.status_code == 429


def test_responses_api_releases_slot_after_success(no_engine, monkeypatch):
    monkeypatch.setattr(app, "safe_chat_stream", lambda *a, **k: _stream_of("hi"))
    req = app.ResponsesRequest(input="hello")

    asyncio.run(app.responses_api(req, True))

    assert app._CHAT_ACTIVE == 0


def test_responses_api_releases_slot_after_streaming(no_engine, monkeypatch):
    monkeypatch.setattr(app, "safe_chat_stream",
                        lambda *a, **k: _stream_of("hello ", "world"))
    req = app.ResponsesRequest(input="hello", stream=True)

    response = asyncio.run(app.responses_api(req, True))
    _collect(response)

    assert app._CHAT_ACTIVE == 0


# ---------------------- rate-limit headers on 429 ----------------------

def test_admit_chat_429_includes_rate_limit_headers(monkeypatch):
    monkeypatch.setattr(app.CFG, "chat_queue_max", 2)
    app._admit_chat()
    app._admit_chat()
    with pytest.raises(app.HTTPException) as exc:
        app._admit_chat()
    headers = exc.value.headers
    assert headers["Retry-After"] == str(app._RATE_LIMIT_RETRY_AFTER_SECONDS)
    assert headers["X-RateLimit-Limit-Requests"] == "2"
    assert headers["X-RateLimit-Remaining-Requests"] == "0"


# ---------------------- chat/task admission independence ----------------------

@pytest.fixture
def tasks(monkeypatch):
    store = SimpleNamespace(tasks={})
    monkeypatch.setattr(app, "store", store)
    return store


def test_chat_admission_does_not_block_task_admission(tasks, monkeypatch):
    monkeypatch.setattr(app.CFG, "chat_queue_max", 1)
    app._admit_chat()  # chat cap is now full
    # _admit_task's store.tasks scan is untouched by chat admission being saturated.
    app._admit_task("video", 4)
    app._admit_task("image", 8)
    with pytest.raises(app.HTTPException):
        app._admit_chat()  # chat cap is still independently enforced


def test_task_admission_does_not_block_chat_admission(tasks, monkeypatch):
    tasks.tasks = {f"v{n}": {"kind": "video", "status": "queued"} for n in range(4)}
    with pytest.raises(app.HTTPException):
        app._admit_task("video", 4)  # video is now at its cap
    # Chat admission's in-memory counter is untouched by the video queue being full.
    app._admit_chat()
    assert app._CHAT_ACTIVE == 1
