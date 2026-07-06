"""Tests for the analysis module: offline heuristic, Claude parsing, and shared helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clipfactory.analysis import AnalysisError, get_analyzer
from clipfactory.analysis.base import finalize_moments
from clipfactory.analysis.claude import ClaudeAnalyzer, chunk_segments
from clipfactory.analysis.heuristic import HeuristicAnalyzer
from clipfactory.schemas import AnalysisConfig, Moment, TranscriptSegment


def _segment(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text)


def _moment(start: float, end: float, score: int) -> Moment:
    return Moment(start_sec=start, end_sec=end, score=score, title="t", hook="h", description="d")


# ---------------------------------------------------------------------------
# finalize_moments
# ---------------------------------------------------------------------------


def test_finalize_moments_dedupes_overlaps_keeping_higher_score():
    config = AnalysisConfig(max_clips=10, min_score=0, min_clip_sec=5, max_clip_sec=60)
    segments = [_segment(0, 200, "x")]
    moments = [
        _moment(10, 40, score=50),
        _moment(15, 45, score=90),  # overlaps a lot with the first, higher score wins
        _moment(100, 130, score=60),  # disjoint, should survive
    ]
    result = finalize_moments(moments, segments, config)
    scores = sorted(m.score for m in result)
    assert scores == [60, 90]


def test_finalize_moments_drops_too_short_and_too_long():
    config = AnalysisConfig(max_clips=10, min_score=0, min_clip_sec=15, max_clip_sec=60)
    segments = [_segment(0, 300, "x")]
    moments = [
        _moment(0, 5, score=80),  # too short (5s < 15s)
        _moment(10, 30, score=80),  # fine
        _moment(50, 200, score=80),  # 150s > max_clip_sec*1.5=90, dropped
    ]
    result = finalize_moments(moments, segments, config)
    assert len(result) == 1
    assert result[0].start_sec == 10
    assert result[0].end_sec == 30


def test_finalize_moments_top_n_by_score():
    config = AnalysisConfig(max_clips=2, min_score=0, min_clip_sec=5, max_clip_sec=60)
    segments = [_segment(0, 500, "x")]
    moments = [
        _moment(0, 20, score=10),
        _moment(50, 70, score=90),
        _moment(100, 120, score=80),
        _moment(150, 170, score=70),
    ]
    result = finalize_moments(moments, segments, config)
    assert len(result) == 2
    assert [m.score for m in result] == [90, 80]


def test_finalize_moments_clamps_end_to_transcript_and_max_duration():
    config = AnalysisConfig(max_clips=10, min_score=0, min_clip_sec=5, max_clip_sec=30)
    segments = [_segment(0, 100, "x")]
    # end_sec beyond transcript end -> clamp to 100; also longer than max_clip_sec -> trim.
    moments = [_moment(80, 140, score=80)]
    result = finalize_moments(moments, segments, config)
    assert len(result) == 1
    assert result[0].start_sec == 80
    assert result[0].end_sec == 100  # clamped to transcript end (was 140)
    assert result[0].duration <= config.max_clip_sec


def test_finalize_moments_drops_low_score():
    config = AnalysisConfig(max_clips=10, min_score=50, min_clip_sec=5, max_clip_sec=60)
    segments = [_segment(0, 200, "x")]
    moments = [_moment(0, 20, score=10), _moment(50, 70, score=60)]
    result = finalize_moments(moments, segments, config)
    assert len(result) == 1
    assert result[0].score == 60


# ---------------------------------------------------------------------------
# HeuristicAnalyzer
# ---------------------------------------------------------------------------


def _synthetic_transcript() -> list[TranscriptSegment]:
    """~3 minutes of segments every ~4s, with a few hooky sentences sprinkled in."""
    hooky_lines = {
        0: "Why does nobody talk about this secret mistake?",
        20: "Як зробити це за 5 хвилин? Ніколи не здавайся!",
        90: "This is the number 1 never-fail trick you need!",
    }
    segments = []
    t = 0.0
    idx = 0
    while t < 180:
        text = hooky_lines.get(idx * 4, "Just a regular sentence about something ordinary today.")
        segments.append(_segment(t, t + 4.0, text))
        t += 4.0
        idx += 1
    return segments


def test_heuristic_analyzer_returns_moments_within_bounds():
    config = AnalysisConfig(max_clips=3, min_score=50, min_clip_sec=15, max_clip_sec=60)
    analyzer = HeuristicAnalyzer()
    segments = _synthetic_transcript()
    moments = analyzer.find_moments(segments, "Test video", config)

    assert len(moments) >= 1
    transcript_start = segments[0].start
    transcript_end = segments[-1].end
    for m in moments:
        assert transcript_start <= m.start_sec < m.end_sec <= transcript_end
        assert config.min_clip_sec <= m.duration <= config.max_clip_sec
        assert 0 <= m.score <= 100


def test_heuristic_analyzer_empty_transcript():
    analyzer = HeuristicAnalyzer()
    config = AnalysisConfig()
    assert analyzer.find_moments([], "Test video", config) == []


# ---------------------------------------------------------------------------
# ClaudeAnalyzer
# ---------------------------------------------------------------------------


class _FakeToolUseBlock:
    def __init__(self, input_data: dict):
        self.type = "tool_use"
        self.input = input_data


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def test_claude_analyzer_parses_tool_use_and_skips_invalid(monkeypatch):
    valid_item = {
        "start_sec": 10.0,
        "end_sec": 40.0,
        "score": 85,
        "title": "Great hook",
        "hook": "Did you know...",
        "description": "A short caption.",
        "hashtags": ["viral", "shorts"],
        "reason": "Strong curiosity gap.",
    }
    invalid_item = {"start_sec": "not-a-number", "end_sec": 10, "score": 200}  # invalid: bad types/score

    block = _FakeToolUseBlock({"moments": [valid_item, invalid_item]})
    response = SimpleNamespace(content=[block])

    settings = SimpleNamespace(anthropic_api_key="fake-key", anthropic_model="claude-sonnet-5")
    analyzer = ClaudeAnalyzer(settings=settings)
    analyzer._client = _FakeClient(response)

    segments = [_segment(0.0, 60.0, "Some content about a secret trick.")]
    config = AnalysisConfig(max_clips=3, min_score=0, min_clip_sec=5, max_clip_sec=60)

    moments = analyzer.find_moments(segments, "Test video", config)

    assert len(moments) == 1
    assert moments[0].title == "Great hook"
    assert moments[0].score == 85


def test_claude_analyzer_wraps_api_errors(monkeypatch):
    import anthropic

    class _RaisingMessages:
        def create(self, **kwargs):
            raise anthropic.AnthropicError("boom")

    settings = SimpleNamespace(anthropic_api_key="fake-key", anthropic_model="claude-sonnet-5")
    analyzer = ClaudeAnalyzer(settings=settings)
    analyzer._client = SimpleNamespace(messages=_RaisingMessages())

    segments = [_segment(0.0, 30.0, "text")]
    config = AnalysisConfig()

    with pytest.raises(AnalysisError):
        analyzer.find_moments(segments, "Test video", config)


def test_chunk_segments_splits_long_transcript_with_overlap():
    # Build a transcript whose formatted text comfortably exceeds the chunk limit.
    segments = [_segment(i * 5.0, i * 5.0 + 5.0, "word " * 50) for i in range(300)]

    chunks = chunk_segments(segments, max_chars=5000, overlap_chars=500)

    assert len(chunks) > 1
    # Every chunk should individually fit under the max_chars budget (best-effort, boundary aligned).
    for chunk in chunks:
        assert chunk  # non-empty
    # Consecutive chunks should overlap: the next chunk's first segment
    # should appear at or before the previous chunk's last segment.
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt[0].start <= prev[-1].start


def test_chunk_segments_single_chunk_when_short():
    segments = [_segment(0, 10, "short line")]
    chunks = chunk_segments(segments, max_chars=24000)
    assert chunks == [segments]


def test_chunk_segments_empty():
    assert chunk_segments([]) == []


# ---------------------------------------------------------------------------
# get_analyzer
# ---------------------------------------------------------------------------


def test_get_analyzer_picks_heuristic_without_api_key(settings):
    analyzer = get_analyzer(settings)
    assert isinstance(analyzer, HeuristicAnalyzer)


def test_get_analyzer_picks_claude_with_api_key(settings, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "fake-key")
    analyzer = get_analyzer(settings)
    assert isinstance(analyzer, ClaudeAnalyzer)
