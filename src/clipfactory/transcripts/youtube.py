"""Transcript retrieval: youtube-transcript-api primary, yt-dlp auto-subs fallback.

See docs/ARCHITECTURE.md §5: subtitles are fetched, never generated with
Whisper, to keep the pipeline instant and free.
"""

from __future__ import annotations

import logging

import httpx

from clipfactory.schemas import TranscriptSegment

logger = logging.getLogger(__name__)

_MUTED_TEXTS = {"[music]", "[applause]", "[laughter]"}


class TranscriptError(RuntimeError):
    pass


class NoTranscriptAvailable(TranscriptError):
    pass


def _clean_snippets(raw: list[tuple[float, float, str]]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for start, duration, text in raw:
        cleaned = " ".join(text.split())
        if not cleaned or cleaned.lower() in _MUTED_TEXTS:
            continue
        segments.append(TranscriptSegment(start=start, end=start + max(duration, 0.0), text=cleaned))
    return segments


def _language_priority(preferred_languages: list[str]) -> list[str]:
    langs = [lang for lang in preferred_languages if lang]
    if "en" not in langs:
        langs.append("en")
    return langs


def _fetch_via_transcript_api(video_id: str, preferred_languages: list[str]) -> tuple[str, list[TranscriptSegment]] | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import CouldNotRetrieveTranscript, NoTranscriptFound
    except ImportError:
        return None

    languages = _language_priority(preferred_languages)
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_transcript(languages)
        except NoTranscriptFound:
            candidates = sorted(transcript_list, key=lambda t: t.is_generated)
            if not candidates:
                return None
            transcript = candidates[0]
        fetched = transcript.fetch()
    except CouldNotRetrieveTranscript as exc:
        logger.info("youtube_transcript_api found nothing for %s: %s", video_id, exc)
        return None
    except Exception as exc:
        raise TranscriptError(f"youtube_transcript_api failed for {video_id!r}: {exc}") from exc

    raw = [(snippet.start, snippet.duration, snippet.text) for snippet in fetched]
    segments = _clean_snippets(raw)
    if not segments:
        return None
    return fetched.language_code, segments


def _pick_caption_track(tracks: dict, preferred_languages: list[str]) -> tuple[str, str] | None:
    languages = _language_priority(preferred_languages)
    for lang in languages:
        for candidate_lang, formats in tracks.items():
            if candidate_lang == lang or candidate_lang.startswith(f"{lang}-"):
                for fmt in formats:
                    if fmt.get("ext") == "json3" and fmt.get("url"):
                        return candidate_lang, fmt["url"]
    for candidate_lang, formats in tracks.items():
        for fmt in formats:
            if fmt.get("ext") == "json3" and fmt.get("url"):
                return candidate_lang, fmt["url"]
    return None


def _fetch_via_yt_dlp(video_id: str, preferred_languages: list[str]) -> tuple[str, list[TranscriptSegment]] | None:
    try:
        import yt_dlp
    except ImportError:
        return None

    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "quiet": True,
        "no_warnings": True,
        "subtitlesformat": "json3",
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise TranscriptError(f"yt-dlp caption fetch failed for {video_id!r}: {exc}") from exc

    if not isinstance(info, dict):
        return None

    tracks = info.get("subtitles") or {}
    picked = _pick_caption_track(tracks, preferred_languages)
    if picked is None:
        tracks = info.get("automatic_captions") or {}
        picked = _pick_caption_track(tracks, preferred_languages)
    if picked is None:
        return None

    language_code, json3_url = picked
    try:
        response = httpx.get(json3_url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise TranscriptError(f"Failed to download captions for {video_id!r}: {exc}") from exc

    raw: list[tuple[float, float, str]] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(seg.get("utf8", "") for seg in segs)
        start_ms = event.get("tStartMs", 0)
        duration_ms = event.get("dDurationMs", 0)
        raw.append((start_ms / 1000.0, duration_ms / 1000.0, text))

    segments = _clean_snippets(raw)
    if not segments:
        return None
    return language_code, segments


def fetch_transcript(yt_video_id: str, preferred_languages: list[str]) -> tuple[str, list[TranscriptSegment]]:
    """Fetch a transcript, preferring youtube-transcript-api over yt-dlp auto-captions."""
    result = _fetch_via_transcript_api(yt_video_id, preferred_languages)
    if result is None:
        result = _fetch_via_yt_dlp(yt_video_id, preferred_languages)
    if result is None:
        raise NoTranscriptAvailable(f"No transcript available for video {yt_video_id!r}")
    return result


def segments_to_json(segments: list[TranscriptSegment]) -> list[dict]:
    return [segment.model_dump() for segment in segments]


def segments_from_json(data: list[dict]) -> list[TranscriptSegment]:
    return [TranscriptSegment.model_construct(**item) for item in data]
