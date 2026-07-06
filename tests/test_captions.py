"""Tests for clipfactory.media.captions: pure ASS document generation."""

from __future__ import annotations

import re

from clipfactory.media.captions import build_ass, format_ass_time
from clipfactory.schemas import RenderPreset, TranscriptSegment


def _segment(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text)


def _dialogue_lines(ass_text: str) -> list[str]:
    return [line for line in ass_text.splitlines() if line.startswith("Dialogue:")]


# ---------------------------------------------------------------------------
# format_ass_time
# ---------------------------------------------------------------------------


def test_format_ass_time_zero():
    assert format_ass_time(0) == "0:00:00.00"


def test_format_ass_time_fractional():
    assert format_ass_time(3661.5) == "1:01:01.50"


def test_format_ass_time_negative_clamped_to_zero():
    assert format_ass_time(-5) == "0:00:00.00"


# ---------------------------------------------------------------------------
# segment selection / shifting / clamping
# ---------------------------------------------------------------------------


def test_segments_outside_window_are_dropped():
    segments = [
        _segment(0, 5, "before"),
        _segment(10, 15, "inside"),
        _segment(50, 60, "after"),
    ]
    preset = RenderPreset()
    ass = build_ass(segments, clip_start=10, clip_end=20, preset=preset)
    dialogue = _dialogue_lines(ass)
    assert len(dialogue) == 1
    assert "inside" in dialogue[0]
    assert "before" not in ass.replace("Style: Cap", "")
    assert "after" not in ass


def test_segment_times_are_shifted_and_clamped():
    segments = [_segment(8, 25, "spanning the whole clip and beyond")]
    preset = RenderPreset()
    ass = build_ass(segments, clip_start=10, clip_end=20, preset=preset)
    dialogue = _dialogue_lines(ass)
    assert len(dialogue) == 1
    # Shifted: seg starts at 8 (before clip_start=10) -> clamped to 0.
    # Seg ends at 25 (after clip_end=20) -> clamped to window length (10).
    assert dialogue[0].startswith("Dialogue: 0,0:00:00.00,0:00:10.00,")


# ---------------------------------------------------------------------------
# line splitting
# ---------------------------------------------------------------------------


def test_lines_wrap_at_max_chars_on_word_boundaries():
    preset = RenderPreset(max_caption_line_chars=10)
    segments = [_segment(0, 5, "one two three four five")]
    ass = build_ass(segments, clip_start=0, clip_end=5, preset=preset)
    dialogue = _dialogue_lines(ass)
    assert len(dialogue) >= 1
    for line in dialogue:
        text = line.split(",", 9)[-1]
        for sub_line in text.split("\\N"):
            assert len(sub_line) <= 10 or " " not in sub_line  # single long word allowed through


def test_max_two_lines_per_event():
    preset = RenderPreset(max_caption_line_chars=8)
    segments = [_segment(0, 10, "aaa bbb ccc ddd eee fff ggg hhh")]
    ass = build_ass(segments, clip_start=0, clip_end=10, preset=preset)
    dialogue = _dialogue_lines(ass)
    for line in dialogue:
        text = line.split(",", 9)[-1]
        assert text.count("\\N") <= 1  # at most 2 lines per event


def test_long_segment_splits_into_multiple_events_proportional_time():
    preset = RenderPreset(max_caption_line_chars=6)
    # Enough words to require > 2 lines -> must split into multiple events.
    segments = [_segment(0, 12, "alpha beta gamma delta epsilon zeta eta theta iota kappa")]
    ass = build_ass(segments, clip_start=0, clip_end=12, preset=preset)
    dialogue = _dialogue_lines(ass)
    assert len(dialogue) > 1

    # Events must be sequential, non-overlapping, and cover [0, 12].
    def parse_time(t: str) -> float:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    times = []
    for line in dialogue:
        parts = line.split(",")
        times.append((parse_time(parts[1]), parse_time(parts[2])))
    times.sort()
    assert abs(times[0][0] - 0.0) < 0.01
    assert abs(times[-1][1] - 12.0) < 0.01
    for (_s1, e1), (s2, _e2) in zip(times, times[1:], strict=False):
        assert abs(e1 - s2) < 0.01


# ---------------------------------------------------------------------------
# escaping
# ---------------------------------------------------------------------------


def test_special_chars_are_escaped():
    segments = [_segment(0, 5, "curly {brace} and back\\slash")]
    preset = RenderPreset()
    ass = build_ass(segments, clip_start=0, clip_end=5, preset=preset)
    dialogue = _dialogue_lines(ass)
    text = dialogue[0].split(",", 9)[-1]
    assert "\\{" in text
    assert "\\}" in text
    assert "\\\\" in text


def test_newline_in_text_becomes_ass_linebreak():
    segments = [_segment(0, 5, "line one\nline two")]
    preset = RenderPreset(max_caption_line_chars=200)
    ass = build_ass(segments, clip_start=0, clip_end=5, preset=preset)
    dialogue = _dialogue_lines(ass)
    text = dialogue[0].split(",", 9)[-1]
    assert "\\N" in text


# ---------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------


def test_header_contains_playres_and_style():
    preset = RenderPreset(width=1080, height=1920, font="DejaVu Sans", font_size=64, caption_position=0.78)
    ass = build_ass([], clip_start=0, clip_end=5, preset=preset)
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "ScriptType: v4.00+" in ass
    style_line = next(line for line in ass.splitlines() if line.startswith("Style: Cap"))
    assert "DejaVu Sans" in style_line
    assert ",64," in style_line
    expected_margin_v = int(1920 * (1 - 0.78))
    assert re.search(rf",{expected_margin_v},1$", style_line)
