"""Source acquisition: yt-dlp downloads (module: media).

Downloads only the fragment of a YouTube video that a candidate clip needs,
padded by a few seconds on each side so the renderer has slack to make a
frame-accurate cut. Falls back to a full download for edge cases (e.g. very
short videos) where a ranged download offers no benefit.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Seconds of padding requested on each side of [start_sec, end_sec] so the
# renderer can seek/cut precisely without hitting a missing keyframe at the edge.
PADDING_SEC = 5.0

_FORMAT = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best"


class DownloadError(RuntimeError):
    """Raised when yt-dlp fails to fetch a video or section."""


def _existing_output(dest_dir: Path, stem: str) -> Path | None:
    """Return an already-downloaded file for this stem, if any (cache hit).

    Only the final merged `{stem}.mp4` counts as a cache hit -- leftover
    fragments from an interrupted download (e.g. `{stem}.mp4.part`,
    `{stem}.f140.m4a`) must never be mistaken for a completed download.
    """
    for candidate in sorted(dest_dir.glob(f"{stem}.mp4")):
        if candidate.name.endswith(".part"):
            continue
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def download_section(yt_video_id: str, start_sec: float, end_sec: float, dest_dir: Path) -> Path:
    """Download only the [start_sec, end_sec] fragment (+padding) of a video.

    Uses yt_dlp's `download_ranges` / `force_keyframes_at_cuts` so only the
    needed portion of the source is fetched. Returns the path to the merged
    mp4. If a matching file already exists in `dest_dir`, it is returned
    without re-downloading.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{yt_video_id}_{start_sec:.1f}_{end_sec:.1f}".replace(".", "p")

    cached = _existing_output(dest_dir, stem)
    if cached is not None:
        logger.info("download_section: cache hit for %s -> %s", stem, cached)
        return cached

    from yt_dlp.utils import download_range_func

    padded_start = max(0.0, start_sec - PADDING_SEC)
    padded_end = end_sec + PADDING_SEC

    opts = {
        "format": _FORMAT,
        "download_ranges": download_range_func([], [[padded_start, padded_end]]),
        "force_keyframes_at_cuts": True,
        "outtmpl": str(dest_dir / f"{stem}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    url = f"https://www.youtube.com/watch?v={yt_video_id}"
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # yt_dlp raises its own DownloadError subclasses
        raise DownloadError(f"Failed to download section {padded_start}-{padded_end} of {yt_video_id!r}: {exc}") from exc

    result = _existing_output(dest_dir, stem)
    if result is None:
        raise DownloadError(f"yt-dlp reported success but no output file found for {stem!r}")
    return result
