"""A client that disconnects mid-stream must not pin a browser slot.

safe_chat_stream feeds an SSE generator from a worker thread. The worker used to call a
plain q.put() while holding GEN_LOCK, so once the queue filled and the consumer was gone
it blocked forever and never released the slot. Two such events wedged the service.
"""
import sys
import time
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


class FakeEngine:
    """Yields far more chunks than the queue can hold."""

    def __init__(self, chunks: int = 1000):
        self.chunks = chunks
        self.finished = False

    def start(self):
        pass

    def chat_stream(self, cookies, prompt, expires, timeout, account_id=None, stop_event=None):
        for index in range(self.chunks):
            if stop_event is not None and stop_event.is_set():
                return
            yield f"chunk-{index}"
        self.finished = True

    def close_current_page(self):
        pass

    def reset_thread(self):
        pass


class StubPool:
    def acquire(self, **kwargs):
        return {"id": "acc-1", "label": "stub", "cookies": {}, "cookies_exp": {}}

    def release(self, account_id):
        pass


@pytest.fixture
def fake_engine(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(app, "engine", engine)
    monkeypatch.setattr(app, "ACCOUNT_POOL", StubPool())
    monkeypatch.setattr(app, "_renew_and_persist", lambda *a, **k: None)
    return engine


def test_disconnect_releases_the_browser_slot(fake_engine):
    assert not app.GEN_LOCK.locked()

    stream = app.safe_chat_stream({}, "hello", None, 30, "acc-1")
    received = []
    for chunk in stream:
        received.append(chunk)
        if len(received) >= 10:
            break                      # client disconnects mid-answer
    stream.close()

    deadline = time.monotonic() + 10
    while app.GEN_LOCK.locked() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert not app.GEN_LOCK.locked(), "GEN_LOCK stayed held after the client disconnected"
    assert len(received) == 10
    assert not fake_engine.finished, "worker should have aborted, not drained every chunk"


def test_complete_stream_still_terminates(fake_engine):
    fake_engine.chunks = 5
    assert "".join(app.safe_chat_stream({}, "hi", None, 30, "acc-1")) == "chunk-0chunk-1chunk-2chunk-3chunk-4"
    assert not app.GEN_LOCK.locked()
