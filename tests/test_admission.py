"""Admission caps: images and video are both bounded.

Video used to have no cap at all, so N requests created N tasks and N threads while the
single browser could only drain one at a time.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


@pytest.fixture
def tasks(monkeypatch):
    store = SimpleNamespace(tasks={})
    monkeypatch.setattr(app, "store", store)
    return store


def test_admission_counts_queued_and_processing(tasks):
    tasks.tasks = {
        "a": {"kind": "video", "status": "queued"},
        "b": {"kind": "video", "status": "processing"},
        "c": {"kind": "video", "status": "completed"},
        "d": {"kind": "image", "status": "queued"},
    }
    # Two active videos: a third is fine, a third beyond a cap of 2 is not.
    app._admit_task("video", 3)
    with pytest.raises(app.HTTPException) as exc:
        app._admit_task("video", 2)
    assert exc.value.status_code == 429
    assert "Video queue is full" in str(exc.value.detail)


def test_kinds_are_capped_independently(tasks):
    tasks.tasks = {f"i{n}": {"kind": "image", "status": "queued"} for n in range(8)}
    with pytest.raises(app.HTTPException):
        app._admit_task("image", 8)
    app._admit_task("video", 4)  # no videos yet


def test_finished_tasks_do_not_count_against_the_cap(tasks):
    tasks.tasks = {f"v{n}": {"kind": "video", "status": "failed"} for n in range(50)}
    app._admit_task("video", 1)


def test_caps_are_configurable():
    assert app.CFG.image_queue_max >= 1
    assert app.CFG.video_queue_max >= 1


def test_both_endpoints_go_through_the_shared_helper():
    source = Path(app.__file__).read_text(encoding="utf-8")
    assert source.count("_admit_task(") >= 2
    video_block = source.split('async def create_video', 1)[1]
    video_block = video_block.split('def get_video', 1)[0]
    assert '_admit_task("video"' in video_block
    assert "TASK_ADMISSION_LOCK" in video_block


def test_admit_task_429_includes_rate_limit_headers(tasks):
    tasks.tasks = {f"v{n}": {"kind": "video", "status": "queued"} for n in range(4)}
    with pytest.raises(app.HTTPException) as exc:
        app._admit_task("video", 4)
    headers = exc.value.headers
    assert headers["Retry-After"] == str(app._RATE_LIMIT_RETRY_AFTER_SECONDS)
    assert headers["X-RateLimit-Limit-Requests"] == "4"
    assert headers["X-RateLimit-Remaining-Requests"] == "0"
