"""Admin i18n coverage gate.

English is applied at runtime by matching the Chinese source against dictionaries. Nothing
enforced that a rich-text block with data-i18n had an English counterpart, so drift was
silent until a user saw Chinese in English mode.
"""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
HTML = (root / "admin.html").read_text(encoding="utf-8")


def _markup() -> str:
    """The HTML body before the single <script> block (comments there mention data-i18n)."""
    return HTML.split("<script>", 1)[0]


def _html_block() -> str:
    return HTML.split("const I18N_EN_HTML = {", 1)[1].split("\n};", 1)[0]


def test_every_data_i18n_key_has_an_english_block():
    used = set(re.findall(r'data-i18n="([^"]+)"', _markup()))
    defined = set(re.findall(r'^\s*"([^"]+)":', _html_block(), re.M))
    assert used, "expected data-i18n attributes in admin.html"
    assert used <= defined, f"missing English for: {sorted(used - defined)}"


def test_no_orphan_english_blocks():
    used = set(re.findall(r'data-i18n="([^"]+)"', _markup()))
    defined = set(re.findall(r'^\s*"([^"]+)":', _html_block(), re.M))
    assert defined <= used, f"English block with no data-i18n target: {sorted(defined - used)}"


def test_english_blocks_contain_no_chinese():
    block = _html_block()
    han = re.compile(r"[\u4e00-\u9fff]")
    assert not han.search(block), "an English block still contains Chinese text"


def test_whole_sentence_dictionary_has_no_duplicate_keys():
    block = HTML.split("const I18N_EN = {", 1)[1].split("\n};", 1)[0]
    keys = re.findall(r'^\s*"((?:[^"\\]|\\.)+)":', block, re.M)
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"duplicate I18N_EN keys: {sorted(duplicates)}"


def test_every_pattern_entry_is_a_pair():
    block = HTML.split("const I18N_EN_PATTERNS = ([", 1)[1].split("\n]);", 1)[0]
    entries = [line for line in block.splitlines() if line.strip().startswith("[")]
    assert entries, "expected pattern entries"
    for line in entries:
        assert line.rstrip().endswith("],"), f"malformed pattern entry: {line[:60]}"


def test_patterns_are_valid_regular_expressions():
    block = HTML.split("const I18N_EN_PATTERNS = ([", 1)[1].split("\n]);", 1)[0]
    patterns = re.findall(r'^\s*\["((?:[^"\\]|\\.)+)",', block, re.M)
    assert patterns
    for pattern in patterns:
        re.compile(pattern)
