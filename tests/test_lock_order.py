"""Lock ordering: the account lease is always taken before the browser slot.

Generation used to take GEN_LOCK first and then wait for an account, while chat took an
account first and then waited for GEN_LOCK. With the semaphore exhausted and every account
leased, neither side could progress until a timeout fired.
"""
import sys
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


class FakeEngine:
    current_acc_id = None

    def __init__(self):
        self.generated = 0

    def start(self):
        pass

    def close_current_page(self):
        pass

    def reset_thread(self):
        pass

    def generate(self, *args, **kwargs):
        self.generated += 1
        return {"filename": "x.png", "size": 1, "kind": "image"}


class RecordingPool:
    """Fails if an account is acquired while a browser slot is already held."""

    def __init__(self):
        self.acquisitions = []
        self.releases = []
        self.violations = []

    def acquire(self, **kwargs):
        if app.GEN_LOCK.locked():
            self.violations.append(kwargs)
        self.acquisitions.append(kwargs)
        return {"id": "acc-1", "label": "stub", "cookies": {}, "cookies_exp": {}}

    def release(self, account_id):
        self.releases.append(account_id)


@pytest.fixture
def generation_env(monkeypatch):
    pool = RecordingPool()
    engine = FakeEngine()
    monkeypatch.setattr(app, "ACCOUNT_POOL", pool)
    monkeypatch.setattr(app, "engine", engine)
    monkeypatch.setattr(app, "store", app.store)
    monkeypatch.setattr(app.store, "mark", lambda *a, **k: None)
    monkeypatch.setattr(app, "_sync_cookies", lambda *a, **k: None)
    monkeypatch.setattr(app, "_renew_and_persist", lambda *a, **k: None)
    return pool, engine


def test_generation_acquires_the_account_before_the_browser(generation_env):
    pool, engine = generation_env

    result, account_id = app._run_generation("a lighthouse", "image", 30)

    assert account_id == "acc-1"
    assert result["filename"] == "x.png"
    assert engine.generated == 1
    assert pool.violations == [], "an account was acquired while GEN_LOCK was held"
    assert not app.GEN_LOCK.locked(), "GEN_LOCK leaked"


def test_generation_releases_both_locks_on_failure(generation_env, monkeypatch):
    pool, engine = generation_env

    def explode(*args, **kwargs):
        raise app.MuseGenerationError("boom")

    monkeypatch.setattr(engine, "generate", explode)

    with pytest.raises(app.MuseGenerationError):
        app._run_generation("a lighthouse", "image", 5)

    assert not app.GEN_LOCK.locked(), "GEN_LOCK leaked after a failed generation"
    assert pool.releases, "the account lease leaked after a failed generation"
    assert pool.violations == []


def test_no_admin_path_takes_the_browser_before_the_account():
    source = Path(app.__file__).read_text(encoding="utf-8")
    assert "with GEN_LOCK, _account_operation" not in source, (
        "admin maintenance must acquire the account lease before GEN_LOCK")
