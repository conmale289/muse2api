"""Model lookup, usage estimates, idempotency headers and video idempotency."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app
from store import Store


# ------------------------------- Model mapping -------------------------------
def test_new_image_model_names_are_classified():
    assert app.resolve_model("gpt-image-2") == "muse-image"
    assert app.resolve_model("gpt-image-1.5") == "muse-image"
    assert app.resolve_model("dall-e-4") == "muse-image"


def test_new_video_model_names_are_classified():
    assert app.resolve_model("sora-3") == "muse-video"
    assert app.resolve_model("veo-4") == "muse-video"


def test_exact_aliases_still_win():
    assert app.resolve_model("gpt-4o") == "muse-spark"
    assert app.resolve_model("claude-sonnet-4") == "muse-spark"


def test_unknown_names_pass_through():
    assert app.resolve_model("some-custom-model") == "some-custom-model"


def test_empty_name_uses_the_default():
    assert app.resolve_model("") == "muse-image"
    assert app.resolve_model(None, default="muse-spark") == "muse-spark"


def test_model_detail_resolves_aliases():
    assert app.model_detail("dall-e-3", None)["id"] == "muse-image"
    assert app.model_detail("muse-spark", None)["id"] == "muse-spark"


def test_model_detail_404s_for_unknown():
    with pytest.raises(app.HTTPException) as exc:
        app.model_detail("not-a-model", None)
    assert exc.value.status_code == 404


def test_model_entries_carry_capability_metadata():
    for entry in app.MODELS:
        assert "context_window" in entry
        assert "supports_streaming" in entry
        assert "modalities" in entry


# ---------------------------------- Usage ----------------------------------
def test_usage_estimate_is_not_zero():
    usage = app._estimate_usage("a" * 400, "b" * 200)
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 150


def test_usage_estimate_never_reports_zero_tokens():
    usage = app._estimate_usage("", "")
    assert usage["total_tokens"] >= 2


# ---------------------------- Idempotency headers ----------------------------
def test_idempotency_headers_are_echoed():
    assert app._idempotency_headers("key", False) == {
        "Idempotency-Key": "key", "Idempotent-Replayed": "false"}
    assert app._idempotency_headers("key", True)["Idempotent-Replayed"] == "true"


def test_no_header_without_a_key():
    assert app._idempotency_headers(None, False) == {}


# --------------------------- Video idempotency ------------------------------
@pytest.fixture
def video_env(monkeypatch, tmp_path):
    store = Store(SimpleNamespace(
        accounts_file=str(tmp_path / "accounts.json"),
        tasks_file=str(tmp_path / "tasks.json"),
        task_retention=0))
    monkeypatch.setattr(app, "store", store)
    monkeypatch.setattr(app, "_run_generation", lambda *a, **k: (
        {"filename": "v.mp4", "size": 10, "kind": "video", "w": 720, "h": 1280}, "acc-1"))
    return store


def _body(response) -> dict:
    return json.loads(response.body)


def test_video_idempotency_replays_the_same_task(video_env):
    req = app.VideoRequest(prompt="a wave")

    first = asyncio.run(app.create_video(req, "video-key-1", None))
    second = asyncio.run(app.create_video(req, "video-key-1", None))

    assert _body(first)["id"] == _body(second)["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert len(video_env.tasks) == 1, "a replay must not create a second task"


def test_video_idempotency_rejects_a_different_request(video_env):
    asyncio.run(app.create_video(app.VideoRequest(prompt="a wave"), "video-key-2", None))
    with pytest.raises(app.HTTPException) as exc:
        asyncio.run(app.create_video(app.VideoRequest(prompt="a storm"), "video-key-2", None))
    assert exc.value.status_code == 409


def test_video_and_image_keys_do_not_collide(video_env):
    video = asyncio.run(app.create_video(app.VideoRequest(prompt="a wave"), "shared", None))
    # The image hash is namespaced differently, so no video task matches it.
    image_hash = __import__("hashlib").sha256(b"shared").hexdigest()
    assert video_env.find_task_by_idempotency(image_hash) is None
    assert _body(video)["object"] == "video"
