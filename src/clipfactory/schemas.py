"""Pydantic DTOs: the contracts between modules.

Every module (ingest, analysis, media, publish, pipeline, api) talks to the
others exclusively through these types plus the ORM entities in models.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TranscriptSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def _end_after_start(self) -> TranscriptSegment:
        if self.end < self.start:
            self.end = self.start
        return self


class VideoInfo(BaseModel):
    """Discovered video metadata (module: ingest)."""

    yt_video_id: str
    title: str = ""
    url: str = ""
    duration_sec: float | None = None
    published_at: datetime | None = None


class ChannelInfo(BaseModel):
    """Resolved channel metadata (module: ingest)."""

    yt_channel_id: str
    title: str = ""
    url: str = ""


class Moment(BaseModel):
    """A potentially viral moment suggested by the analyzer (module: analysis)."""

    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)
    score: int = Field(ge=0, le=100)
    title: str = ""
    hook: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def duration(self) -> float:
        return self.end_sec - self.start_sec


class RenderPreset(BaseModel):
    """How to turn a source fragment into a vertical clip (module: media)."""

    model_config = ConfigDict(extra="forbid")

    width: int = 1080
    height: int = 1920
    mode: Literal["crop", "blur-pad"] = "crop"
    burn_captions: bool = True
    font: str = "DejaVu Sans"
    font_size: int = 64
    # 0..1, vertical center of the caption block from the top
    caption_position: float = 0.78
    max_caption_line_chars: int = 24
    video_bitrate: str = "6M"
    audio_bitrate: str = "192k"


class PostMetadata(BaseModel):
    """Everything a publisher needs besides the video file (module: publish)."""

    title: str
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)


class PublishResult(BaseModel):
    external_id: str = ""
    external_url: str = ""


class AnalysisConfig(BaseModel):
    """Per-channel knobs passed to the analyzer."""

    max_clips: int = 3
    min_score: int = 60
    min_clip_sec: float = 15.0
    max_clip_sec: float = 60.0
    language: str = ""  # hint for output language of titles/hashtags
