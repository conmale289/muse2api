"""Configuration defaults for the concurrency knob (P4, option 1).

All tasks share one Chromium process and one CDP connection, so the honest default is 1
(serialised). Raising it is an opt-in experiment, not the shipped behaviour.
"""
import re
import sys
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import config as config_mod


def test_default_concurrency_is_one(monkeypatch):
    # The repo's own .env may set this; remove it so the code default is what is tested.
    monkeypatch.delenv("MUSE2API_CONCURRENCY", raising=False)
    assert config_mod.Config().max_concurrency == 1


def test_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv("MUSE2API_CONCURRENCY", "3")
    assert config_mod.Config().max_concurrency == 3


@pytest.mark.parametrize("value", ["0", "-4", " 0 "])
def test_non_positive_values_clamp_to_one(monkeypatch, value):
    monkeypatch.setenv("MUSE2API_CONCURRENCY", value)
    assert config_mod.Config().max_concurrency == 1


def test_env_example_matches_the_code_default(monkeypatch):
    """Guard against the template and the code drifting apart again."""
    monkeypatch.delenv("MUSE2API_CONCURRENCY", raising=False)
    default = config_mod.Config().max_concurrency
    text = (root / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^MUSE2API_CONCURRENCY=(\d+)$", text, re.M)
    assert match, "MUSE2API_CONCURRENCY is missing from .env.example"
    assert int(match.group(1)) == default


def test_browser_lock_uses_the_configured_limit():
    """The semaphore must be built from the setting, not a hardcoded number."""
    import app
    assert app.GEN_LOCK.limit == app.CFG.max_concurrency
