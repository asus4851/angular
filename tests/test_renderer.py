"""Tests for clipfactory.media.renderer: ffmpeg command building + real render integration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipfactory.media.renderer import (
    build_ffmpeg_command,
    escape_filter_path,
    make_test_video,
    probe_duration,
    render_clip,
)
from clipfactory.schemas import RenderPreset, TranscriptSegment


def _preset(**overrides) -> RenderPreset:
    defaults = dict(video_bitrate="1M", audio_bitrate="96k")
    defaults.update(overrides)
    return RenderPreset(**defaults)


# ---------------------------------------------------------------------------
# build_ffmpeg_command
# ---------------------------------------------------------------------------


def test_crop_mode_uses_vf_with_scale_and_crop():
    preset = _preset(mode="crop", width=1080, height=1920)
    cmd = build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), 2.0, 8.0, preset, None)

    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in vf
    assert "crop=1080:1920" in vf
    assert "-filter_complex" not in cmd

    assert cmd[cmd.index("-ss") + 1] == "2.000"
    assert cmd[cmd.index("-t") + 1] == "6.000"
    assert cmd[cmd.index("-b:v") + 1] == "1M"
    assert cmd[cmd.index("-b:a") + 1] == "96k"
    assert str(Path("in.mp4")) in cmd
    assert cmd[-1] == str(Path("out.mp4"))


def test_blur_pad_mode_uses_filter_complex():
    preset = _preset(mode="blur-pad", width=1080, height=1920)
    cmd = build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), 0.0, 10.0, preset, None)

    assert "-filter_complex" in cmd
    assert "-vf" not in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "boxblur=20:5" in fc
    assert "overlay=(W-w)/2:(H-h)/2" in fc
    assert "split" in fc


def test_ass_path_is_appended_and_escaped():
    preset = _preset(mode="crop")
    ass_path = Path("/tmp/some dir:weird/clip.ass")
    cmd = build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), 0.0, 5.0, preset, ass_path)
    vf = cmd[cmd.index("-vf") + 1]
    assert "ass=" in vf
    escaped = escape_filter_path(str(ass_path))
    assert escaped in vf
    assert "\\:" in escaped  # colon escaped for ffmpeg filter syntax


def test_escape_filter_path_escapes_colon_backslash_quote():
    raw = r"C:\videos\it's here.ass"
    escaped = escape_filter_path(raw)
    assert escaped == r"C\:\\videos\\it\'s here.ass"


def test_unsupported_mode_raises():
    from clipfactory.media.renderer import RenderError

    preset = RenderPreset.model_construct(
        width=1080, height=1920, mode="nonsense", burn_captions=False,
        font="x", font_size=1, caption_position=0.5, max_caption_line_chars=10,
        video_bitrate="1M", audio_bitrate="96k",
    )
    with pytest.raises(RenderError):
        build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), 0.0, 1.0, preset, None)


# ---------------------------------------------------------------------------
# real ffmpeg integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_render_clip_end_to_end(tmp_path):
    source = make_test_video(tmp_path / "source.mp4", duration=10.0)
    assert source.exists()

    output = tmp_path / "clip.mp4"
    segments = [
        TranscriptSegment(start=2.0, end=4.0, text="hello there world"),
        TranscriptSegment(start=5.0, end=7.0, text="second caption line here"),
    ]
    preset = _preset(mode="crop", width=1080, height=1920, burn_captions=True)

    result = render_clip(
        source=source,
        output=output,
        source_start_sec=2.0,
        source_end_sec=8.0,
        segments=segments,
        clip_start_sec=2.0,
        clip_end_sec=8.0,
        preset=preset,
    )

    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0

    # temp .ass should be cleaned up after a successful render
    assert not output.with_suffix(".ass").exists()

    duration = probe_duration(output)
    assert abs(duration - 6.0) < 0.5

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(output),
        ],
        capture_output=True,
        check=True,
    )
    width_str, height_str = probe.stdout.decode().strip().split(",")
    assert int(width_str) == 1080
    assert int(height_str) == 1920
