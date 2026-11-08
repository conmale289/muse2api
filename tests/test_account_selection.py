"""Chat must let ACCOUNT_POOL choose the account, not pick one outside the pool.

store.pick_account() has no knowledge of which accounts are leased, so two concurrent
requests could both be told to use account A while account B sat idle.
"""
import sys
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


class FakeEngine:
    current_acc_id = None

    def start(self):
        pass

    def close_current_page(self):
        pass

    def reset_thread(self):
        pass

    def chat_stream(self, cookies, prompt, expires, timeout, account_id=None, stop_event=None):
        yield "hello"
        yield " world"


class RecordingPool:
    def __init__(self):
        self.calls = []

    def acquire(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"id": "picked-1", "label": "picked", "cookies": {}, "cookies_exp": {}}

    def release(self, account_id):
        self.calls.append({"release": account_id})


@pytest.fixture
def chat_env(monkeypatch):
    pool = RecordingPool()
    monkeypatch.setattr(app, "ACCOUNT_POOL", pool)
    monkeypatch.setattr(app, "engine", FakeEngine())
    monkeypatch.setattr(app, "_renew_and_persist", lambda *a, **k: None)
    monkeypatch.setattr(app.store, "mark", lambda *a, **k: None)
    monkeypatch.setattr(app, "_sync_cookies", lambda *a, **k: None)
    return pool


def test_unpinned_chat_asks_the_pool_to_choose(chat_env):
    state = {}

    text = "".join(app.safe_chat_stream(
        {}, "hi", None, 10, None, preferred_id="preferred-acc", state=state))

    assert text == "hello world"
    acquire_calls = [c for c in chat_env.calls if "release" not in c]
    assert len(acquire_calls) == 1
    assert acquire_calls[0]["preferred_id"] == "preferred-acc"
    assert "account_id" not in acquire_calls[0], "the pool must choose, not be pinned"
    assert acquire_calls[0]["rotate"] is True
    assert state["account_id"] == "picked-1"
    assert {"release": "picked-1"} in chat_env.calls


def test_pinned_chat_still_honours_the_explicit_account(chat_env):
    state = {}

    "".join(app.safe_chat_stream(
        {}, "hi", None, 10, "explicit-acc", state=state))

    acquire_calls = [c for c in chat_env.calls if "release" not in c]
    assert acquire_calls[0]["account_id"] == "explicit-acc"
    assert state["account_id"] == "explicit-acc"


def test_pool_failure_surfaces_as_an_error(chat_env, monkeypatch):
    def refuse(**kwargs):
        raise app.MuseAuthError("No available account")

    monkeypatch.setattr(chat_env, "acquire", refuse)

    with pytest.raises(app.MuseAuthError):
        "".join(app.safe_chat_stream({}, "hi", None, 10, None))


def test_chat_endpoints_no_longer_pick_accounts_themselves():
    source = Path(app.__file__).read_text(encoding="utf-8")
    chat_block = source.split('@app.post("/v1/chat/completions")', 1)[1]
    chat_block = chat_block.split('@app.post("/v1/responses")', 1)[0]
    assert "store.pick_account" not in chat_block

    responses_block = source.split('@app.post("/v1/responses")', 1)[1]
    responses_block = responses_block.split('@app.get("/v1/media/{name}")', 1)[0]
    assert "store.pick_account" not in responses_block
