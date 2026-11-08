"""Request validation, CORS parsing, credential handling and .env permissions."""
import base64
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


# --------------------------- Video request bounds ---------------------------
def test_video_timeout_has_an_upper_bound():
    with pytest.raises(ValidationError):
        app.VideoRequest(prompt="x", timeout=10 ** 9)


def test_video_timeout_rejects_zero_and_negative():
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            app.VideoRequest(prompt="x", timeout=bad)


def test_video_duration_is_bounded():
    for bad in (0, -3, 61):
        with pytest.raises(ValidationError):
            app.VideoRequest(prompt="x", duration=bad)


def test_video_defaults_still_work():
    req = app.VideoRequest(prompt="x")
    assert req.duration is None and req.timeout is None


def test_video_accepts_a_reasonable_request():
    req = app.VideoRequest(prompt="x", duration=8, timeout=600, size="1280x720")
    assert req.duration == 8


# ------------------------------- CORS parsing -------------------------------
def test_cors_empty_means_any_origin():
    assert app._allowed_origins("") == ["*"]
    assert app._allowed_origins(None) == ["*"]


def test_cors_list_is_honoured():
    assert app._allowed_origins("https://a.example, https://b.example") == [
        "https://a.example", "https://b.example"]


def test_cors_ignores_blank_entries():
    assert app._allowed_origins("https://a.example, ,") == ["https://a.example"]


# ---------------------------- Git credentials -------------------------------
def test_token_is_passed_via_environment_not_argv(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    app._git(["push", "origin", "HEAD:main"], token="ghp_secret_value")

    assert "ghp_secret_value" not in " ".join(captured["cmd"])
    header = captured["env"]["GIT_CONFIG_VALUE_0"]
    assert header.startswith("Authorization: Basic ")
    decoded = base64.b64decode(header.split(" ", 2)[2]).decode()
    assert decoded == "x-access-token:ghp_secret_value"
    assert captured["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_no_token_means_no_auth_header(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    app._git(["status"])

    assert "GIT_CONFIG_VALUE_0" not in captured["env"]


def test_ensure_git_repo_never_stores_a_token(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(app, "BASE_DIR", str(repo))
    monkeypatch.setattr(app, "_git", lambda args, **kw: calls.append((args, kw)))

    app._ensure_git_repo("ghp_secret_value")

    remote_calls = [args for args, _ in calls if args[:2] == ["remote", "set-url"]]
    assert remote_calls, "origin must be rewritten"
    for args in remote_calls:
        url = args[3]
        assert "x-access-token" not in url
        assert "ghp_secret_value" not in url
        assert url == f"{app.REPO_URL}.git"


# ----------------------------- .env permissions -----------------------------
def test_persist_env_creates_a_private_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app.CFG, "base_dir", str(tmp_path))
    app._persist_env("MUSE2API_KEY", "m2a_test")

    path = tmp_path / ".env"
    assert path.read_text(encoding="utf-8") == "MUSE2API_KEY=m2a_test\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_persist_env_preserves_other_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(app.CFG, "base_dir", str(tmp_path))
    (tmp_path / ".env").write_text("OTHER=1\nMUSE2API_KEY=old\n", encoding="utf-8")
    app._persist_env("MUSE2API_KEY", "new")

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OTHER=1" in text
    assert "MUSE2API_KEY=new" in text
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
