"""Video processing: download source fragments, build captions, render 9:16 clips."""

from __future__ import annotations

from clipfactory.media.captions import build_ass, format_ass_time
from clipfactory.media.downloader import PADDING_SEC, DownloadError, download_section
from clipfactory.media.renderer import (
    RenderError,
    build_ffmpeg_command,
    make_test_video,
    probe_duration,
    render_clip,
)

__all__ = [
    "DownloadError",
    "download_section",
    "PADDING_SEC",
    "build_ass",
    "format_ass_time",
    "RenderError",
    "build_ffmpeg_command",
    "render_clip",
    "probe_duration",
    "make_test_video",
]
