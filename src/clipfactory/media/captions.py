"""Transcript -> ASS subtitle document (module: media).

Pure text generation: no IO here. `renderer.py` is responsible for writing
the returned string to a `.ass` file before invoking ffmpeg.
"""

from __future__ import annotations

from clipfactory.schemas import RenderPreset, TranscriptSegment

_ASS_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,0,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def format_ass_time(seconds: float) -> str:
    """Format seconds as ASS time `H:MM:SS.cc` (centiseconds, unpadded hours)."""
    seconds = max(0.0, seconds)
    total_centis = round(seconds * 100)
    centis = total_centis % 100
    total_seconds = total_centis // 100
    secs = total_seconds % 60
    total_minutes = total_seconds // 60
    mins = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours}:{mins:02d}:{secs:02d}.{centis:02d}"


def _escape_ass_text(text: str) -> str:
    """Escape ASS override-block special chars and turn newlines into \\N."""
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return text.replace("\n", "\\N").replace("\r", "")


def _clip_segments(
    segments: list[TranscriptSegment], clip_start: float, clip_end: float
) -> list[tuple[float, float, str]]:
    """Keep segments overlapping the clip window, shift to clip-relative 0, clamp."""
    window = max(0.0, clip_end - clip_start)
    kept: list[tuple[float, float, str]] = []
    for seg in segments:
        if seg.end <= clip_start or seg.start >= clip_end:
            continue
        start = max(0.0, min(seg.start - clip_start, window))
        end = max(0.0, min(seg.end - clip_start, window))
        if end <= start:
            continue
        text = seg.text.strip()
        if not text:
            continue
        kept.append((start, end, text))
    return kept


def _wrap_lines(text: str, max_chars: int) -> list[str]:
    """Greedy word-boundary wrap; never splits a word even if it exceeds max_chars.

    Explicit newlines in the source text are preserved as forced line breaks
    (each paragraph is wrapped independently).
    """
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or len(candidate) <= max_chars:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines or [""]


def _chunk_events(
    start: float, end: float, text: str, max_chars: int
) -> list[tuple[float, float, list[str]]]:
    """Wrap text into lines, group into <=2-line events, split time proportionally."""
    lines = _wrap_lines(text, max_chars)
    chunks = [lines[i : i + 2] for i in range(0, len(lines), 2)] or [[""]]

    if len(chunks) == 1:
        return [(start, end, chunks[0])]

    duration = end - start
    char_counts = [len("".join(chunk)) or 1 for chunk in chunks]
    total_chars = sum(char_counts)

    events: list[tuple[float, float, list[str]]] = []
    cursor = start
    for i, (chunk, chars) in enumerate(zip(chunks, char_counts, strict=True)):
        if i == len(chunks) - 1:
            chunk_end = end
        else:
            chunk_end = cursor + duration * (chars / total_chars)
        events.append((cursor, chunk_end, chunk))
        cursor = chunk_end
    return events


def build_ass(
    segments: list[TranscriptSegment], clip_start: float, clip_end: float, preset: RenderPreset
) -> str:
    """Build a full ASS document for the [clip_start, clip_end] window (original-video time).

    Output is clip-relative: Dialogue time 0 corresponds to `clip_start`.
    """
    margin_v = int(preset.height * (1 - preset.caption_position))
    header = _ASS_HEADER_TEMPLATE.format(
        width=preset.width,
        height=preset.height,
        font=preset.font,
        font_size=preset.font_size,
        margin_v=margin_v,
    )

    lines: list[str] = [header]
    for start, end, text in _clip_segments(segments, clip_start, clip_end):
        for ev_start, ev_end, chunk in _chunk_events(start, end, text, preset.max_caption_line_chars):
            ass_text = "\\N".join(_escape_ass_text(line) for line in chunk)
            lines.append(
                f"Dialogue: 0,{format_ass_time(ev_start)},{format_ass_time(ev_end)},Cap,,0,0,0,,{ass_text}\n"
            )

    return "".join(lines)
