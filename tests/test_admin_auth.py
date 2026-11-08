"""Admin credential separation.

The inference API key is handed to every downstream client. It must not be able to
publish code to the upstream repository, and operators must be able to give code-changing
actions a different credential.
"""
import sys
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(app.CFG, "api_key", "m2a_inference")
    monkeypatch.setattr(app.CFG, "maintainer_key", "")
    return app.CFG


def test_inference_key_is_accepted_for_normal_api_calls(keys):
    assert app.auth("Bearer m2a_inference") is True


def test_wrong_key_is_rejected(keys):
    with pytest.raises(app.HTTPException) as exc:
        app.auth("Bearer nope")
    assert exc.value.status_code == 401


def test_missing_header_is_rejected(keys):
    with pytest.raises(app.HTTPException) as exc:
        app.auth(None)
    assert exc.value.status_code == 401


def test_push_is_disabled_without_a_maintainer_key(keys):
    with pytest.raises(app.HTTPException) as exc:
        app.repo_push_auth("Bearer m2a_inference")
    assert exc.value.status_code == 403
    assert "MUSE2API_MAINTAINER_KEY" in str(exc.value.detail)


def test_push_accepts_the_maintainer_key(keys):
    keys.maintainer_key = "m2a_maint"
    assert app.repo_push_auth("Bearer m2a_maint") is True


def test_push_rejects_the_inference_key_when_a_maintainer_key_exists(keys):
    keys.maintainer_key = "m2a_maint"
    with pytest.raises(app.HTTPException) as exc:
        app.repo_push_auth("Bearer m2a_inference")
    assert exc.value.status_code == 401


def test_upgrade_prefers_the_maintainer_key(keys):
    keys.maintainer_key = "m2a_maint"
    assert app.maintainer_auth("Bearer m2a_maint") is True
    with pytest.raises(app.HTTPException):
        app.maintainer_auth("Bearer m2a_inference")


def test_upgrade_falls_back_to_the_inference_key(keys):
    # Documented fallback so existing installs keep the one-click upgrade working.
    assert app.maintainer_auth("Bearer m2a_inference") is True


def test_comparison_is_constant_time(keys, monkeypatch):
    calls = []
    real = app.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(app.secrets, "compare_digest", spy)
    app.auth("Bearer m2a_inference")
    assert calls, "the key comparison must go through secrets.compare_digest"
