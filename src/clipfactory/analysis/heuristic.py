"""Offline fallback analyzer: no API key required, used for demo and tests."""

from __future__ import annotations

import logging
import re

from clipfactory.analysis.base import finalize_moments
from clipfactory.schemas import AnalysisConfig, Moment, TranscriptSegment

logger = logging.getLogger(__name__)

_HOOK_WORDS = (
    # English
    "how",
    "why",
    "never",
    "secret",
    "mistake",
    # Ukrainian
    "як",
    "чому",
    "ніколи",
    "секрет",
    "помилка",
)

_NUMBER_RE = re.compile(r"\d")


def _hook_score(text: str) -> float:
    lowered = text.lower()
    score = 0.0
    if "?" in text:
        score += 1.0
    if _NUMBER_RE.search(text):
        score += 0.5
    for word in _HOOK_WORDS:
        if word in lowered:
            score += 1.0
            break
    return score


def _energy_score(text: str, duration: float) -> float:
    words = text.split()
    wps = len(words) / duration if duration > 0 else 0.0
    # ~2.5 words/sec is lively speech; scale toward 1.0 around there.
    energy = min(wps / 2.5, 1.0)
    exclaims = text.count("!")
    excitement = min(exclaims / 3, 1.0)
    return energy + excitement


class HeuristicAnalyzer:
    """Deterministic scoring over sliding windows of transcript segments.

    No network calls: used when ANTHROPIC_API_KEY is unset, and in tests.
    """

    def find_moments(
        self,
        segments: list[TranscriptSegment],
        video_title: str,
        config: AnalysisConfig,
    ) -> list[Moment]:
        if not segments:
            return []

        candidates: list[tuple[Moment, float]] = []
        n = len(segments)

        i = 0
        while i < n:
            span_start = segments[i].start
            j = i
            while j < n and segments[j].end - span_start < config.min_clip_sec:
                j += 1
            if j >= n:
                break
            # Extend the window while it still fits inside max_clip_sec.
            while j + 1 < n and segments[j + 1].end - span_start <= config.max_clip_sec:
                j += 1

            span_end = segments[j].end
            duration = span_end - span_start
            window_segment_count = max(1, j - i + 1)
            if config.min_clip_sec <= duration <= config.max_clip_sec:
                span_segments = segments[i : j + 1]
                full_text = " ".join(s.text for s in span_segments)
                raw = _hook_score(span_segments[0].text) + _energy_score(full_text, duration)
                candidates.append((self._build_moment(span_segments, span_start, span_end), raw))

            # Step roughly half a window forward.
            step = max(1, window_segment_count // 2)
            i += step

        if not candidates:
            return []

        max_raw = max(raw for _, raw in candidates) or 1.0
        moments: list[Moment] = []
        for moment, raw in candidates:
            scaled = int(round((raw / max_raw) * 100))
            if raw == max_raw:
                scaled = max(scaled, 70)
            scaled = max(0, min(100, scaled))
            moments.append(moment.model_copy(update={"score": scaled}))

        return finalize_moments(moments, segments, config)

    @staticmethod
    def _build_moment(
        span_segments: list[TranscriptSegment],
        start: float,
        end: float,
    ) -> Moment:
        first_text = span_segments[0].text.strip()
        title_source = re.split(r"(?<=[.!?])\s+", first_text.strip())[0] if first_text else ""
        title = title_source[:60] if title_source else "Untitled moment"
        description = " ".join(s.text for s in span_segments[:2]).strip()
        return Moment(
            start_sec=start,
            end_sec=end,
            score=0,
            title=title,
            hook=first_text,
            description=description,
            hashtags=["shorts", "viral"],
            reason="heuristic: hook/energy score",
        )
