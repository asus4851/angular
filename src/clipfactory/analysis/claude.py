"""Claude-backed analyzer: tool-use structured output with transcript chunking."""

from __future__ import annotations

import logging
import time

import anthropic
from pydantic import ValidationError

from clipfactory.analysis.base import AnalysisError, finalize_moments
from clipfactory.config import Settings, get_settings
from clipfactory.schemas import AnalysisConfig, Moment, TranscriptSegment

logger = logging.getLogger(__name__)

_MAX_CHUNK_CHARS = 24_000
_CHUNK_OVERLAP_CHARS = 2_000
_RETRY_SLEEP_SEC = 5.0

_MOMENTS_TOOL = {
    "name": "report_moments",
    "description": "Report the most potentially viral self-contained moments found in the transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start_sec": {"type": "number", "description": "Clip start, seconds from video start."},
                        "end_sec": {"type": "number", "description": "Clip end, seconds from video start."},
                        "score": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "Virality potential, 0-100.",
                        },
                        "title": {"type": "string", "description": "Catchy overlay title."},
                        "hook": {"type": "string", "description": "First-seconds text hook."},
                        "description": {"type": "string", "description": "1-2 sentence post caption."},
                        "hashtags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "3-6 lowercase hashtags without the # symbol.",
                        },
                        "reason": {"type": "string", "description": "One sentence why it can go viral."},
                    },
                    "required": [
                        "start_sec",
                        "end_sec",
                        "score",
                        "title",
                        "hook",
                        "description",
                        "hashtags",
                        "reason",
                    ],
                },
            },
        },
        "required": ["moments"],
    },
}


def format_transcript(segments: list[TranscriptSegment]) -> str:
    """Render segments as timestamped lines: `[start-end] text`."""
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in segments)


def chunk_segments(
    segments: list[TranscriptSegment],
    max_chars: int = _MAX_CHUNK_CHARS,
    overlap_chars: int = _CHUNK_OVERLAP_CHARS,
) -> list[list[TranscriptSegment]]:
    """Split segments into chunks whose formatted text stays under ``max_chars``.

    Splits happen on segment boundaries; consecutive chunks overlap by
    roughly ``overlap_chars`` of trailing text so moments near a cut point
    aren't lost.
    """
    if not segments:
        return []

    lines = [f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in segments]
    line_lens = [len(line) + 1 for line in lines]
    total_len = sum(line_lens)
    if total_len <= max_chars:
        return [segments]

    n = len(segments)
    chunks: list[list[TranscriptSegment]] = []
    start_idx = 0
    while start_idx < n:
        cur_len = 0
        end_idx = start_idx
        while end_idx < n and (cur_len == 0 or cur_len + line_lens[end_idx] <= max_chars):
            cur_len += line_lens[end_idx]
            end_idx += 1
        chunks.append(segments[start_idx:end_idx])
        if end_idx >= n:
            break

        # Step back from end_idx to build ~overlap_chars of shared context.
        overlap_len = 0
        back_idx = end_idx
        while back_idx > start_idx and overlap_len < overlap_chars:
            back_idx -= 1
            overlap_len += line_lens[back_idx]
        start_idx = back_idx if back_idx > start_idx else end_idx

    return chunks


class ClaudeAnalyzer:
    """Finds viral moments via Claude tool-use with structured JSON output."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)

    def find_moments(
        self,
        segments: list[TranscriptSegment],
        video_title: str,
        config: AnalysisConfig,
    ) -> list[Moment]:
        if not segments:
            return []

        all_moments: list[Moment] = []
        for chunk in chunk_segments(segments):
            all_moments.extend(self._analyze_chunk(chunk, video_title, config))

        return finalize_moments(all_moments, segments, config)

    def _analyze_chunk(
        self,
        segments: list[TranscriptSegment],
        video_title: str,
        config: AnalysisConfig,
    ) -> list[Moment]:
        system = self._build_system_prompt(config)
        user_message = (
            f"Video title: {video_title}\n\nTranscript (timestamps in seconds):\n"
            f"{format_transcript(segments)}"
        )

        try:
            response = self._create_with_retry(system, user_message)
        except anthropic.AnthropicError as exc:
            raise AnalysisError(f"Claude analysis request failed: {exc}") from exc

        return self._parse_response(response)

    def _create_with_retry(self, system: str, user_message: str):
        try:
            return self._call(system, user_message)
        except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            status_code = getattr(exc, "status_code", None)
            if isinstance(exc, anthropic.RateLimitError) or (status_code is not None and status_code >= 500):
                logger.warning("Claude request failed (%s), retrying once in %ss", exc, _RETRY_SLEEP_SEC)
                time.sleep(_RETRY_SLEEP_SEC)
                return self._call(system, user_message)
            raise

    def _call(self, system: str, user_message: str):
        return self._client.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=4096,
            system=system,
            tools=[_MOMENTS_TOOL],
            tool_choice={"type": "tool", "name": "report_moments"},
            messages=[{"role": "user", "content": user_message}],
        )

    @staticmethod
    def _build_system_prompt(config: AnalysisConfig) -> str:
        language_hint = (
            f" Write titles, hooks, descriptions and hashtags in {config.language}."
            if config.language
            else " Write titles, hooks, descriptions and hashtags in the transcript's own language."
        )
        return (
            "You are a short-form content strategist. Given a video transcript with timestamps, "
            "find the most potentially viral self-contained moments suitable for Reels/Shorts/TikTok. "
            "Each clip must start at a natural hook and end at a natural conclusion, with a duration "
            f"between {config.min_clip_sec:.0f} and {config.max_clip_sec:.0f} seconds. "
            "Score each moment 0-100 for virality potential, weighing hook strength, emotion, curiosity "
            "gap, payoff, and standalone comprehensibility (it must make sense with zero prior context). "
            "The title is a catchy on-screen overlay title." + language_hint + " The hook is the exact "
            "first-seconds text that grabs attention. The description is a 1-2 sentence post caption. "
            "Hashtags are 3-6 relevant lowercase words without the # symbol. The reason is one sentence "
            "explaining why the moment can go viral. Prefer fewer, stronger moments over many weak ones."
        )

    @staticmethod
    def _parse_response(response) -> list[Moment]:
        moments: list[Moment] = []
        for block in getattr(response, "content", []):
            if getattr(block, "type", None) != "tool_use":
                continue
            raw_moments = (block.input or {}).get("moments", [])
            for item in raw_moments:
                try:
                    moments.append(Moment(**item))
                except (ValidationError, TypeError) as exc:
                    logger.warning("Skipping invalid moment from Claude: %s", exc)
        return moments
