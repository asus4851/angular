"""ffmpeg-based rendering: cut + 9:16 reframe + caption burn-in (module: media)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from clipfactory.media.captions import build_ass
from clipfactory.schemas import RenderPreset, TranscriptSegment

logger = logging.getLogger(__name__)


class RenderError(RuntimeError):
    """Raised when ffmpeg/ffprobe fails."""


def escape_filter_path(path: str) -> str:
    """Escape a filesystem path for use as an ffmpeg filter argument (e.g. `ass=...`).

    Backslash must be escaped first so the escapes added for the other
    metacharacters aren't themselves re-escaped. `,` and `;` are filtergraph
    separators and `[`/`]` delimit link labels, so any of them appearing in a
    path (e.g. `clip [final].ass`) would otherwise corrupt the filtergraph.
    """
    return (
        path.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def build_ffmpeg_command(
    source: Path,
    output: Path,
    start_sec: float,
    end_sec: float,
    preset: RenderPreset,
    ass_path: Path | None,
) -> list[str]:
    """Build the ffmpeg argv for cutting+reframing (+captioning) a clip.

    `start_sec`/`end_sec` are SOURCE-FILE-relative: `source` is typically the
    padded fragment produced by `downloader.download_section`, so these are
    NOT the original-video timestamps of the clip — the caller must translate.

    Uses `-ss` before `-i` (fast seek) plus `-t` for the duration instead of
    `-to`, since `-to` after a pre-input `-ss` is interpreted relative to the
    original timeline, not the seek point. Combined with re-encoding this is
    frame-accurate enough for short-form clips.
    """
    duration = max(0.0, end_sec - start_sec)
    w, h = preset.width, preset.height

    if preset.mode == "crop":
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        if ass_path is not None:
            vf += f",ass={escape_filter_path(str(ass_path))}"
        video_args = ["-vf", vf]
    elif preset.mode == "blur-pad":
        filter_complex = (
            "split[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )
        if ass_path is not None:
            filter_complex += f",ass={escape_filter_path(str(ass_path))}"
        video_args = ["-filter_complex", filter_complex]
    else:
        raise RenderError(f"Unsupported render mode: {preset.mode!r}")

    return [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        *video_args,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        preset.video_bitrate,
        "-c:a",
        "aac",
        "-b:a",
        preset.audio_bitrate,
        "-movflags",
        "+faststart",
        "-r",
        "30",
        "-shortest",
        str(output),
    ]


def render_clip(
    source: Path,
    output: Path,
    source_start_sec: float,
    source_end_sec: float,
    segments: list[TranscriptSegment],
    clip_start_sec: float,
    clip_end_sec: float,
    preset: RenderPreset,
) -> Path:
    """Cut, reframe to 9:16, and (optionally) burn captions into a clip.

    Two time bases are involved and must line up:
      - `source_start_sec`/`source_end_sec` are SOURCE-FILE-relative seconds
        fed straight to ffmpeg's `-ss`/`-t` (see `build_ffmpeg_command`). The
        source file is usually the padded fragment from
        `downloader.download_section`, which starts a few seconds before the
        moment of interest — not at t=0 of the original video.
      - `clip_start_sec`/`clip_end_sec` are ORIGINAL-VIDEO-relative seconds
        (the candidate's timestamps in the full transcript). They select and
        shift the caption segments via `captions.build_ass`, which re-bases
        them so the clip's first frame is t=0.
      - Callers must ensure `source_end_sec - source_start_sec == clip_end_sec
        - clip_start_sec` (same duration) so that ffmpeg's rendered t=0
        matches the captions' t=0.

    Writes a temporary `.ass` file next to `output` when
    `preset.burn_captions` and `segments` is non-empty; it is deleted on
    success and kept (for debugging) if ffmpeg fails.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    ass_path: Path | None = None

    if preset.burn_captions and segments:
        ass_path = output.with_suffix(".ass")
        ass_path.write_text(build_ass(segments, clip_start_sec, clip_end_sec, preset), encoding="utf-8")

    cmd = build_ffmpeg_command(source, output, source_start_sec, source_end_sec, preset, ass_path)
    logger.info("render_clip: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True)

    if result.returncode != 0:
        stderr_tail = result.stderr[-800:].decode("utf-8", errors="replace")
        raise RenderError(f"ffmpeg failed (code {result.returncode}): {stderr_tail}")

    if ass_path is not None:
        ass_path.unlink(missing_ok=True)

    return output


def probe_duration(path: Path) -> float:
    """Return the duration (seconds) of a media file via ffprobe."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr_tail = result.stderr[-800:].decode("utf-8", errors="replace")
        raise RenderError(f"ffprobe failed (code {result.returncode}): {stderr_tail}")
    return float(result.stdout.decode().strip())


def make_test_video(path: Path, duration: float = 30.0) -> Path:
    """Generate a synthetic test video (video+audio) for demos and integration tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1280x720:rate=30:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr_tail = result.stderr[-800:].decode("utf-8", errors="replace")
        raise RenderError(f"ffmpeg failed to generate test video (code {result.returncode}): {stderr_tail}")
    return path
