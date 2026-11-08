"""Prompt assembly: composition ratios and the text-preservation gate.

Covers two defects:
  * the aspect-ratio allowlist missed OpenAI's 1024x1792 / 1792x1024 sizes, so those
    requests got no orientation instruction at all;
  * a blanket "remove all text / watermark / logo" clause was appended to every
    reference-image request, contradicting prompts that ask for text.
"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


REF = "data:image/png;base64,iVBORw0KGgo="


def _prompt(prompt: str, **kw) -> str:
    return app.build_image_prompt(app.ImageRequest(prompt=prompt, **kw))


def test_openai_portrait_size_is_classified_as_portrait():
    out = _prompt("a lighthouse", size="1024x1792")
    assert "9:16" in out
    assert "竖屏" in out
    # The old code fell through to a bare restatement with no orientation language.
    assert "尺寸/比例：1024x1792" not in out


def test_openai_landscape_size_is_classified_as_landscape():
    out = _prompt("a lighthouse", size="1792x1024")
    assert "16:9" in out
    assert "横屏" in out


def test_square_size_is_classified_as_square():
    assert "1:1" in _prompt("a lighthouse", size="1024x1024")


def test_three_by_two_size_maps_to_landscape_not_portrait():
    # 1536x1024 = 1.5; nearest canonical ratio is 3:2, which is landscape.
    out = _prompt("a lighthouse", size="1536x1024")
    assert "3:2" in out
    assert "竖屏" not in out


def test_named_ratio_still_wins_over_size():
    out = _prompt("a lighthouse", aspect_ratio="9:16", size="1920x1080")
    assert "9:16" in out


def test_unparseable_size_falls_back_to_restatement():
    out = _prompt("a lighthouse", size="huge")
    assert "尺寸/比例：huge" in out


def test_auto_size_adds_no_constraint():
    out = _prompt("a lighthouse", size="auto")
    assert "尺寸/比例" not in out
    assert "画面构图与比例要求" not in out


def test_auto_aspect_ratio_adds_no_constraint():
    out = _prompt("a lighthouse", aspect_ratio="auto")
    assert "画面构图与比例要求" not in out


def test_unconstrained_video_is_not_forced_vertical():
    out = app.build_video_prompt(app.VideoRequest(prompt="a wave"))
    assert "9:16" not in out
    assert "16:9" not in out


def test_text_request_keeps_the_clean_picture_clause_out():
    out = _prompt("put the title 'SUMMER SALE' on this poster", image=REF)
    assert "纯净画面要求" not in out


def test_chinese_text_request_also_suppresses_the_clause():
    out = _prompt("在海报上写上标题“夏季促销”", image=REF)
    assert "纯净画面要求" not in out


def test_plain_reference_request_keeps_the_clause():
    out = _prompt("a cat sitting on a mat", image=REF)
    assert "纯净画面要求" in out


def test_watermark_removal_request_keeps_the_clause():
    out = _prompt("remove the watermark from this photo", image=REF)
    assert "纯净画面要求" in out


def test_clause_never_applies_without_a_reference_image():
    assert "纯净画面要求" not in _prompt("a cat sitting on a mat")


def test_video_orientation_uses_the_same_classifier():
    portrait = app.build_video_prompt(app.VideoRequest(prompt="a wave", size="1024x1792"))
    assert "9:16" in portrait
    landscape = app.build_video_prompt(app.VideoRequest(prompt="a wave", size="1792x1024"))
    assert "16:9" in landscape


# ------------------------- Q7: one canonical history rule -------------------
LEGACY_HISTORY_PHRASES = (
    "严禁使用历史图片",
    "严禁参考任何历史图片",
    "请勿查找历史相册",
    "严禁使用历史素材",
    "严禁使用历史图片或其他图像",
    "严禁使用历史图片或任何其他图像",
)


def _all_prompts():
    """Every branch of both prompt builders, with and without references."""
    single = app.ImageRequest(prompt="a cat", image=REF)
    return {
        "image/no-ref": _prompt("a cat"),
        "image/one-ref": _prompt("a cat", image=REF),
        "image/two-refs": _prompt("a cat", images=[REF, REF]),
        "image/two-refs-markers": _prompt("use [图1] and [图2]", images=[REF, REF]),
        "image/ref-extra": _prompt("a cat", image=REF, extra="warm light"),
        "image/ref-portrait": _prompt("a cat", image=REF, size="1024x1792"),
        "video/no-ref": app.build_video_prompt(app.VideoRequest(prompt="a wave")),
        "video/one-ref": app.build_video_prompt(app.VideoRequest(prompt="a wave", image=REF)),
        "video/two-refs": app.build_video_prompt(app.VideoRequest(prompt="a wave", images=[REF, REF])),
        "video/two-refs-markers": app.build_video_prompt(
            app.VideoRequest(prompt="use [图1] and [图2]", images=[REF, REF])),
        "video/one-ref-portrait": app.build_video_prompt(
            app.VideoRequest(prompt="a wave", image=REF, aspect_ratio="9:16")),
        "video/one-ref-landscape": app.build_video_prompt(
            app.VideoRequest(prompt="a wave", image=REF, aspect_ratio="16:9")),
    }


def test_every_prompt_states_the_history_rule_exactly_once():
    for name, prompt in _all_prompts().items():
        assert prompt.count(app._NO_HISTORY_MARKER) == 1, (
            f"{name}: expected exactly one canonical history rule")


def test_no_prompt_uses_a_legacy_history_phrasing():
    for name, prompt in _all_prompts().items():
        for phrase in LEGACY_HISTORY_PHRASES:
            assert phrase not in prompt, f"{name}: legacy phrasing still present: {phrase}"


def test_history_rule_is_the_last_instruction():
    for name, prompt in _all_prompts().items():
        assert prompt.endswith(app._NO_HISTORY_MARKER), (
            f"{name}: the history rule must be the trailing instruction")


def test_history_rule_wording_depends_on_references():
    assert app._history_rule(True).startswith("仅使用本次请求附带的附件")
    assert app._history_rule(False).startswith("本次请求未附带参考素材")
