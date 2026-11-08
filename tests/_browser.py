"""Locate a Chromium executable for the standalone browser tests.

These tests run as scripts (`python3 tests/test_xxx.py`), so they cannot rely on pytest
fixtures. They used to call shutil.which("chromium") directly, which returns None on
macOS and Windows installs and makes subprocess.Popen raise TypeError. Use the same
discovery the service uses: MUSE2API_CHROMIUM first, then a list of known locations.
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import _detect_chromium


def chromium_path(required: bool = True) -> str | None:
    """Return an executable path, or None (or exit) when Chromium is unavailable."""
    candidate = _detect_chromium()
    resolved = shutil.which(candidate) or (candidate if os.path.isfile(candidate) else None)
    if resolved:
        return resolved
    if required:
        raise SystemExit(
            "Chromium required. Set MUSE2API_CHROMIUM to the browser executable, "
            "or install chromium/google-chrome on PATH.")
    return None
