"""Batch 3 hardening: image limits, size reporting, chat image refusal, retention,
version source, batched writes, extension permissions and the no-flash marker."""
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app
from store import Store


# ------------------------- Q3: n>1 is refused -------------------------------
def test_n_greater_than_one_is_rejected():
    with pytest.raises(app.HTTPException) as exc:
        app._reject_multiple_images(2)
    assert exc.value.status_code == 400
    assert "one image per generation" in str(exc.value.detail)


def test_n_one_and_none_are_allowed():
    app._reject_multiple_images(1)
    app._reject_multiple_images(None)
    app._reject_multiple_images(0)


# --------------------- Q4: report the produced dimensions --------------------
def test_image_response_prefers_real_dimensions(monkeypatch):
    monkeypatch.setattr(app, "media_url", lambda name: f"/v1/media/{name}")
    req = app.ImageRequest(prompt="p", size="1024x1024")
    out = app._image_response(req, {"filename": "a.png", "size": 10, "kind": "image",
                                    "w": 1024, "h": 1792})
    assert out["data"][0]["size"] == "1024x1792"


def test_image_response_falls_back_to_the_request(monkeypatch):
    monkeypatch.setattr(app, "media_url", lambda name: f"/v1/media/{name}")
    req = app.ImageRequest(prompt="p", size="1024x1024")
    out = app._image_response(req, {"filename": "a.png", "size": 10, "kind": "image"})
    assert out["data"][0]["size"] == "1024x1024"


# ------------------- Q5: chat refuses image content -------------------------
def test_image_parts_are_detected():
    message = app.ChatMessage(role="user", content=[
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
    ])
    assert app._has_image_parts([message]) is True


def test_text_only_messages_are_not_flagged():
    message = app.ChatMessage(role="user", content="hello")
    assert app._has_image_parts([message]) is False


# ----------------- Q6: duration is soft and measurable ----------------------
def test_video_prompt_no_longer_claims_a_strict_duration():
    prompt = app.build_video_prompt(app.VideoRequest(prompt="a wave", duration=8))
    assert "严格" not in prompt
    assert "时长约 8 秒" in prompt


def test_mp4_duration_parser_is_available():
    assert callable(app._mp4_duration)


# --------------------- U5: version comes from version.json ------------------
def test_openapi_version_matches_version_json():
    declared = json.loads((root / "version.json").read_text(encoding="utf-8"))["version"]
    assert app.app.version == declared


# ------------------- P6: batched account writes -----------------------------
def _store(tmp_path):
    return Store(SimpleNamespace(
        accounts_file=str(tmp_path / "accounts.json"),
        tasks_file=str(tmp_path / "tasks.json"),
        task_retention=0))


def test_batch_writes_collapse_into_one_flush(tmp_path):
    store = _store(tmp_path)
    store.add_account({"hatch_sess": "s"}, "one")
    store.add_account({"hatch_sess": "s"}, "two")
    before = os.stat(store.cfg.accounts_file).st_mtime_ns

    with store.batch_writes():
        for account in store.accounts:
            store.touch_keepalive(account["id"], True, "batched")
        # Nothing is written while the batch is open.
        assert os.stat(store.cfg.accounts_file).st_mtime_ns == before

    assert os.stat(store.cfg.accounts_file).st_mtime_ns != before


def test_structural_changes_ignore_the_batch(tmp_path):
    store = _store(tmp_path)
    with store.batch_writes():
        store.add_account({"hatch_sess": "s"}, "urgent")
    saved = json.loads(Path(store.cfg.accounts_file).read_text(encoding="utf-8"))
    assert len(saved) == 1, "an import must be durable even mid-sweep"


# --------------------- P7: media retention sweep ----------------------------
def test_media_retention_removes_old_files(monkeypatch, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    old = media / "old.png"
    new = media / "new.png"
    protected = media / "busy.mp4"
    for path in (old, new, protected):
        path.write_bytes(b"x")
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(protected, (long_ago, long_ago))

    monkeypatch.setattr(app, "CFG", SimpleNamespace(media_dir=str(media),
                                                    media_retention_days=30))
    monkeypatch.setattr(app, "store", SimpleNamespace(tasks={
        "t1": {"status": "processing", "result": {"filename": "busy.mp4"}}}))

    result = app._sweep_media_sync()

    assert result["removed"] == 1
    assert not old.exists()
    assert new.exists()
    assert protected.exists(), "a file referenced by a running task must survive"


def test_media_retention_disabled_keeps_everything(monkeypatch, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    old = media / "old.png"
    old.write_bytes(b"x")
    long_ago = time.time() - 400 * 86400
    os.utime(old, (long_ago, long_ago))

    monkeypatch.setattr(app, "CFG", SimpleNamespace(media_dir=str(media),
                                                    media_retention_days=0))
    monkeypatch.setattr(app, "store", SimpleNamespace(tasks={}))

    assert app._sweep_media_sync()["skipped"] is True
    assert old.exists()


# ------------------- R12: extension zip is cached ---------------------------
def test_extension_zip_is_built_once(monkeypatch):
    calls = []
    real_zip = app.zipfile.ZipFile

    class CountingZip(real_zip):
        def __init__(self, *args, **kwargs):
            calls.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(app.zipfile, "ZipFile", CountingZip)
    src = str(root / "extension")
    first = app._extension_zip_bytes(src)
    second = app._extension_zip_bytes(src)
    assert first == second
    assert len(calls) == 1, "a second request must reuse the cached archive"


# ------------------- U3: extension permissions ------------------------------
def test_extension_does_not_request_every_site_at_install():
    manifest = json.loads((root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert "http://*/*" not in manifest["host_permissions"]
    assert "https://*/*" not in manifest["host_permissions"]
    assert "https://muse.ai/*" in manifest["host_permissions"]
    assert "http://*/*" in manifest["optional_host_permissions"]


# ------------------- U1: no flash of untranslated content ------------------
def test_admin_page_hides_until_translated():
    html = (root / "admin.html").read_text(encoding="utf-8")
    assert 'body class="i18n-pending"' in html
    assert "body.i18n-pending{visibility:hidden}" in html
    assert "classList.remove('i18n-pending')" in html


# ------------------- U6: tabs are deep-linkable ----------------------------
def test_admin_tabs_restore_from_the_hash():
    html = (root / "admin.html").read_text(encoding="utf-8")
    assert "hashchange" in html
    assert "history.replaceState" in html
