"""OpenAI-compatible video API shape, content download and delete."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


def test_video_object_uses_openai_status_names():
    task = {"id": "t1", "kind": "video", "status": "processing", "progress": 15,
            "created_at": 100, "updated_at": 120, "duration": 8, "size": "1280x720",
            "model": "muse-video", "error": None, "result": None}
    obj = app._video_object(task)
    assert obj["object"] == "video"
    assert obj["status"] == "in_progress"
    assert obj["seconds"] == "8"
    assert obj["size"] == "1280x720"
    assert obj["task_id"] == "t1"


def test_completed_video_reports_progress_and_completion_time():
    task = {"id": "t2", "kind": "video", "status": "completed", "progress": 100,
            "created_at": 100, "updated_at": 150, "duration": None,
            "url": "https://example.test/v.mp4", "result": {"w": 720, "h": 1280,
                                                            "filename": "v.mp4"}}
    obj = app._video_object(task)
    assert obj["status"] == "completed"
    assert obj["progress"] == 100
    assert obj["completed_at"] == 150
    assert obj["size"] == "720x1280", "the real dimensions must win over the request"
    assert obj["video"] == {"url": "https://example.test/v.mp4"}


def test_failed_video_exposes_an_error_object():
    task = {"id": "t3", "kind": "video", "status": "failed", "error": "boom",
            "created_at": 1, "updated_at": 2}
    obj = app._video_object(task)
    assert obj["error"] == {"message": "boom"}
    assert obj["completed_at"] is None


def test_content_endpoint_reports_not_ready(monkeypatch):
    monkeypatch.setattr(app, "store", SimpleNamespace(
        get_task=lambda tid: {"id": tid, "kind": "video", "status": "queued",
                              "result": None}))
    with pytest.raises(app.HTTPException) as exc:
        app.get_video_content("t1", None)
    assert exc.value.status_code == 409


def test_content_endpoint_404_for_unknown_video(monkeypatch):
    monkeypatch.setattr(app, "store", SimpleNamespace(get_task=lambda tid: None))
    with pytest.raises(app.HTTPException) as exc:
        app.get_video_content("nope", None)
    assert exc.value.status_code == 404


def test_content_endpoint_serves_the_rendered_file(monkeypatch, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(app, "CFG", SimpleNamespace(media_dir=str(media)))
    monkeypatch.setattr(app, "store", SimpleNamespace(
        get_task=lambda tid: {"id": tid, "kind": "video", "status": "completed",
                              "result": {"filename": "clip.mp4"}}))

    response = app.get_video_content("t1", None)
    assert response.media_type == "video/mp4"
    assert Path(response.path) == media / "clip.mp4"


def test_delete_removes_the_task_and_the_file(monkeypatch, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "clip.mp4").write_bytes(b"x")
    deleted = []
    monkeypatch.setattr(app, "CFG", SimpleNamespace(media_dir=str(media)))
    monkeypatch.setattr(app, "store", SimpleNamespace(
        get_task=lambda tid: {"id": tid, "kind": "video", "status": "completed",
                              "result": {"filename": "clip.mp4"}},
        delete_task=lambda tid: deleted.append(tid) or True))

    result = app.delete_video("t1", None)
    assert result["deleted"] is True
    assert deleted == ["t1"]
    assert not (media / "clip.mp4").exists()


def test_routes_are_registered():
    paths = {getattr(route, "path", "") for route in app.app.routes}
    assert "/v1/videos/{task_id}/content" in paths
    assert "/v1/videos/{task_id}" in paths
    assert any(getattr(route, "methods", None) and "DELETE" in route.methods
               and route.path == "/v1/videos/{task_id}" for route in app.app.routes)
