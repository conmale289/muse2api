"""Chromium startup must be serialized end to end.

_start_lock used to guard only the fast-path probe, so two threads that both passed the
check could spawn two browsers against the same --user-data-dir and debug port, and the
singleton-lock cleanup would delete the lock out from under the winner.
"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import engine as engine_mod


class FakeProc:
    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _make_engine(tmp_path):
    cfg = SimpleNamespace(
        profile_dir=str(tmp_path / "profile"),
        data_dir=str(tmp_path / "data"),
        download_dir=str(tmp_path / "downloads"),
        home_dir=str(tmp_path),
        chromium="/bin/true",
        cdp_port=19999,
        extra_path="",
    )
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return engine_mod.MuseEngine(cfg)


def test_concurrent_start_spawns_one_browser(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    state = {"inflight": 0, "peak": 0, "calls": 0, "spawns": 0}

    def fake_http(url, timeout=1):
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        try:
            state["calls"] += 1
            time.sleep(0.05)              # widen the race window
            if state["calls"] == 1:
                raise RuntimeError("chromium is not up yet")
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"}
        finally:
            state["inflight"] -= 1

    def fake_popen(*args, **kwargs):
        state["spawns"] += 1
        return FakeProc()

    monkeypatch.setattr(engine_mod, "http_json", fake_http)
    monkeypatch.setattr(engine_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(engine_mod, "CDP", lambda *a, **k: object())

    threads = [threading.Thread(target=eng.start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads), "start() did not return"
    assert state["peak"] == 1, "two threads entered browser startup at the same time"
    assert state["spawns"] == 1, f"expected one Chromium spawn, got {state['spawns']}"


def test_start_is_idempotent_after_success(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    spawns = {"n": 0}

    monkeypatch.setattr(engine_mod, "http_json",
                        lambda url, timeout=1: {"webSocketDebuggerUrl": "ws://127.0.0.1:1/x"})

    def fake_popen(*args, **kwargs):
        spawns["n"] += 1
        return FakeProc()

    monkeypatch.setattr(engine_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(engine_mod, "CDP", lambda *a, **k: object())

    # The healthy-CDP probe succeeds immediately, so nothing should ever be spawned.
    eng.start()
    eng.start()
    assert spawns["n"] == 0
