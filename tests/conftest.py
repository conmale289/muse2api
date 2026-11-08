"""Shared pytest configuration.

Most files in this directory are standalone scripts rather than pytest modules: they
drive a real Chromium, parse command-line arguments, or call asyncio.run() at import
time. Collecting them would launch a browser or abort the whole session, so they are
ignored here and stay runnable directly:

    python3 tests/test_admin_layout.py
    python3 tests/test_media_selection.py
    python3 tests/test_proxy_status_receipt.py
    python3 tests/test_reference_upload_browser.py
    python3 tests/test_proxy_url_import.py
    python3 tests/test_quota_account_binding.py
    python3 tests/test_vm_wait.py
    python3 tests/test_wake_confirmation.py

New tests should be plain pytest modules; add them nowhere and they are collected
automatically.
"""

collect_ignore = [
    "test_admin_layout.py",
    "test_media_selection.py",
    "test_proxy_status_receipt.py",
    "test_proxy_url_import.py",
    "test_quota_account_binding.py",
    "test_reference_upload_browser.py",
    "test_vm_wait.py",
    "test_wake_confirmation.py",
]
