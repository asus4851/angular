"""Analyzer protocol, factory, and shared post-processing."""

from __future__ import annotations

import logging
from typing import Protocol

from clipfactory.config import Settings, get_settings
from clipfactory.schemas import AnalysisConfig, Moment, TranscriptSegment

logger = logging.getLogger(__name__)


class AnalysisError(RuntimeError):
    """Raised when an analyzer fails to produce moments."""


class Analyzer(Protocol):
    """Module contract: transcript in, ranked viral moments out."""

    def find_moments(
        self,
        segments: list[TranscriptSegment],
        video_title: str,
        config: AnalysisConfig,
    ) -> list[Moment]: ...


def get_analyzer(settings: Settings | None = None) -> Analyzer:
    """Pick ClaudeAnalyzer when an API key is configured, else the offline heuristic."""
    settings = settings or get_settings()
    if settings.anthropic_api_key:
        from clipfactory.analysis.claude import ClaudeAnalyzer

        logger.info("Using ClaudeAnalyzer (model=%s)", settings.anthropic_model)
        return ClaudeAnalyzer(settings=settings)

    from clipfactory.analysis.heuristic import HeuristicAnalyzer

    logger.info("Using HeuristicAnalyzer (no ANTHROPIC_API_KEY set)")
    return HeuristicAnalyzer()


def _overlap_ratio(a: Moment, b: Moment) -> float:
    """Overlap length as a fraction of the shorter moment's duration."""
    start = max(a.start_sec, b.start_sec)
    end = min(a.end_sec, b.end_sec)
    overlap = max(0.0, end - start)
    shorter = min(a.duration, b.duration)
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def finalize_moments(
    moments: list[Moment],
    segments: list[TranscriptSegment],
    config: AnalysisConfig,
) -> list[Moment]:
    """Shared post-processing: clamp, filter, dedupe overlaps, rank, truncate.

    Applied by every analyzer so pipeline downstream always sees consistent,
    non-overlapping, in-bounds candidates regardless of which backend ran.
    """
    transcript_end = max((s.end for s in segments), default=0.0)

    clamped: list[Moment] = []
    for m in moments:
        # 1. Clamp end to the transcript end (models can hallucinate past the end).
        start = max(0.0, m.start_sec)
        end = min(m.end_sec, transcript_end) if transcript_end > 0 else m.end_sec
        if end <= start:
            continue

        # 2. Drop by duration (post transcript-clamp, pre max-clip-trim).
        duration = end - start
        if duration < config.min_clip_sec or duration > config.max_clip_sec * 1.5:
            continue

        # 3. Clamp to max_clip_sec by trimming the end.
        if end > start + config.max_clip_sec:
            end = start + config.max_clip_sec

        # 4. Drop by score.
        if m.score < config.min_score:
            continue

        clamped.append(m.model_copy(update={"start_sec": start, "end_sec": end}))

    # Highest score first so dedup keeps the stronger of any overlapping pair.
    clamped.sort(key=lambda m: m.score, reverse=True)

    kept: list[Moment] = []
    for candidate in clamped:
        if any(_overlap_ratio(candidate, existing) > 0.5 for existing in kept):
            continue
        kept.append(candidate)

    kept.sort(key=lambda m: m.score, reverse=True)
    return kept[: config.max_clips]
