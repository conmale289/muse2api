"""A corrupt data file must never be reported as empty state.

`_read` used to swallow every parse error and return the default, so the next write
persisted that default over the real file — silently destroying every account cookie.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import store as store_mod


def _cfg(tmp_path):
    return SimpleNamespace(
        accounts_file=str(tmp_path / "accounts.json"),
        tasks_file=str(tmp_path / "tasks.json"),
    )


def test_missing_files_are_not_corruption(tmp_path):
    instance = store_mod.Store(_cfg(tmp_path))
    assert instance.accounts == []
    assert instance.tasks == {}
    assert instance.corrupt_files == []


def test_valid_files_load_cleanly(tmp_path):
    cfg = _cfg(tmp_path)
    Path(cfg.accounts_file).write_text('[{"id": "abc", "cookies": {}}]', encoding="utf-8")
    Path(cfg.tasks_file).write_text('{"t1": {"id": "t1"}}', encoding="utf-8")
    instance = store_mod.Store(cfg)
    assert instance.accounts == [{"id": "abc", "cookies": {}}]
    assert instance.tasks == {"t1": {"id": "t1"}}
    assert instance.corrupt_files == []


def test_truncated_accounts_file_is_quarantined_not_lost(tmp_path):
    cfg = _cfg(tmp_path)
    broken = '{"accounts": [{"id": "abc"'
    Path(cfg.accounts_file).write_text(broken, encoding="utf-8")

    instance = store_mod.Store(cfg)

    assert instance.accounts == []
    assert len(instance.corrupt_files) == 1
    record = instance.corrupt_files[0]
    assert record["path"] == cfg.accounts_file

    backup = Path(record["backup"])
    assert backup.exists(), "the unreadable file must be preserved"
    assert backup.read_text(encoding="utf-8") == broken
    assert not Path(cfg.accounts_file).exists(), "the bad file must be moved out of the way"


def test_later_write_cannot_overwrite_the_quarantined_data(tmp_path):
    cfg = _cfg(tmp_path)
    original = '{"broken": '
    Path(cfg.accounts_file).write_text(original, encoding="utf-8")

    instance = store_mod.Store(cfg)
    instance.add_account({"hatch_sess": "s"}, "fresh")

    # The new accounts file is a fresh document; the original bytes survive in the backup.
    assert Path(cfg.accounts_file).exists()
    backup = Path(instance.corrupt_files[0]["backup"])
    assert backup.read_text(encoding="utf-8") == original


def test_wrong_shape_json_is_quarantined(tmp_path):
    cfg = _cfg(tmp_path)
    Path(cfg.tasks_file).write_text("[]", encoding="utf-8")  # valid JSON, wrong shape
    instance = store_mod.Store(cfg)
    # A list where a dict is expected would crash the first .values() call, so it is
    # treated as unusable data rather than loaded.
    assert instance.tasks == {}
    assert len(instance.corrupt_files) == 1
    assert Path(instance.corrupt_files[0]["backup"]).read_text(encoding="utf-8") == "[]"


def test_unmovable_corrupt_file_fails_closed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    Path(cfg.accounts_file).write_text("not json at all", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(store_mod.os, "replace", refuse)

    with pytest.raises(store_mod.StoreCorruptError):
        store_mod.Store(cfg)
