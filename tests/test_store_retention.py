"""Task retention and the idempotency index.

Every task write rewrites the whole tasks.json, so unbounded history made each progress
update slower, and the idempotency lookup scanned every task on each submit.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from store import Store


def _cfg(tmp_path, retention=0):
    return SimpleNamespace(
        accounts_file=str(tmp_path / "accounts.json"),
        tasks_file=str(tmp_path / "tasks.json"),
        task_retention=retention,
    )


def test_history_is_pruned_to_the_retention_limit(tmp_path):
    store = Store(_cfg(tmp_path, retention=5))
    ids = []
    for index in range(12):
        task = store.create_task("image", f"prompt-{index}")
        ids.append(task["id"])
        store.update_task(task["id"], status="completed")

    assert len(store.tasks) == 5
    assert ids[-1] in store.tasks, "the newest task must survive"
    assert ids[0] not in store.tasks, "the oldest task must be dropped"


def test_running_tasks_are_never_pruned(tmp_path):
    store = Store(_cfg(tmp_path, retention=2))
    running = store.create_task("video", "still going")
    store.update_task(running["id"], status="processing")
    for index in range(6):
        task = store.create_task("image", f"done-{index}")
        store.update_task(task["id"], status="completed")

    assert running["id"] in store.tasks, "a processing task must not be dropped"
    assert len(store.tasks) <= 3  # the running one plus the retention window


def test_zero_retention_disables_pruning(tmp_path):
    store = Store(_cfg(tmp_path, retention=0))
    for index in range(20):
        task = store.create_task("image", f"p{index}")
        store.update_task(task["id"], status="completed")
    assert len(store.tasks) == 20


def test_idempotency_index_finds_and_forgets(tmp_path):
    store = Store(_cfg(tmp_path))
    task = store.create_task("image", "p")
    store.update_task(task["id"], image_request_key="hash-1", image_request_hash="req-1")

    found = store.find_task_by_idempotency("hash-1")
    assert found is not None and found["id"] == task["id"]

    assert store.delete_task(task["id"]) is True
    assert store.find_task_by_idempotency("hash-1") is None


def test_idempotency_index_is_rebuilt_on_load(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg)
    task = store.create_task("image", "p")
    store.update_task(task["id"], image_request_key="hash-2", image_request_hash="req-2")

    reloaded = Store(cfg)
    assert reloaded.find_task_by_idempotency("hash-2")["id"] == task["id"]


def test_clear_tasks_drops_index_entries(tmp_path):
    store = Store(_cfg(tmp_path))
    task = store.create_task("image", "p")
    store.update_task(task["id"], image_request_key="hash-3", image_request_hash="req-3")

    assert store.clear_tasks(0) == 1
    assert store.find_task_by_idempotency("hash-3") is None


def test_pick_account_does_not_rewrite_the_file_every_time(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg)
    store.add_account({"hatch_sess": "session"}, "acct")
    before = os.stat(cfg.accounts_file).st_mtime_ns

    for _ in range(10):
        assert store.pick_account(record_use=True) is not None

    assert os.stat(cfg.accounts_file).st_mtime_ns == before, (
        "picking an account must not rewrite every account's cookies")

    store.flush()
    assert os.stat(cfg.accounts_file).st_mtime_ns != before, "flush must persist the bookkeeping"
