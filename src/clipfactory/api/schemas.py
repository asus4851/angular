"""Response/request DTOs for the HTTP API.

These are API-layer schemas only (never returned secrets, joined display
fields, etc.) — distinct from `clipfactory.schemas`, which holds the
inter-module DTOs used by the pipeline.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from clipfactory.models import CandidateStatus, ClipStatus, JobType, Platform, PostStatus, VideoStatus


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    yt_channel_id: str
    title: str
    url: str
    enabled: bool
    auto_approve: bool
    check_interval_min: int
    max_clips_per_video: int
    min_score: int
    language: str
    created_at: datetime
    last_checked_at: datetime | None = None


class ChannelCreate(BaseModel):
    url: str
    auto_approve: bool = False
    check_interval_min: int = 30
    max_clips_per_video: int = 3
    min_score: int = 60
    language: str = ""


class ChannelUpdate(BaseModel):
    enabled: bool | None = None
    auto_approve: bool | None = None
    check_interval_min: int | None = None
    min_score: int | None = None
    max_clips_per_video: int | None = None
    language: str | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: Platform
    name: str
    enabled: bool
    has_credentials: bool
    created_at: datetime


class AccountCreate(BaseModel):
    platform: Platform
    name: str
    credentials: dict | None = None


class AccountUpdate(BaseModel):
    enabled: bool | None = None
    credentials: dict | None = None


class RouteOut(BaseModel):
    id: int
    channel_id: int
    account_id: int
    channel_title: str
    account_name: str
    account_platform: Platform
    enabled: bool
    title_template: str
    description_template: str
    extra_hashtags: list
    created_at: datetime


class RouteCreate(BaseModel):
    channel_id: int
    account_id: int
    title_template: str = "{title}"
    description_template: str = "{description}\n\n{hashtags}"
    extra_hashtags: list[str] = []


class RouteUpdate(BaseModel):
    enabled: bool | None = None
    title_template: str | None = None
    description_template: str | None = None
    extra_hashtags: list[str] | None = None


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    yt_video_id: str
    title: str
    duration_sec: float | None = None
    published_at: datetime | None = None
    status: VideoStatus
    error: str
    discovered_at: datetime
    candidate_count: int = 0


class VideoImportRequest(BaseModel):
    url: str
    max_clips: int | None = None
    min_score: int | None = None
    language: str | None = None


class VideoImportOut(BaseModel):
    id: int
    title: str
    duration_sec: float | None = None
    channel_title: str
    already_imported: bool = False


class VideoAnalyzeRequest(BaseModel):
    max_clips: int | None = None
    min_score: int | None = None
    language: str | None = None


class ChannelCatalogItem(BaseModel):
    yt_video_id: str
    title: str
    duration_sec: float | None = None
    imported: bool = False
    video_id: int | None = None


class ChannelImportRequest(BaseModel):
    yt_video_id: str
    title: str | None = None
    max_clips: int | None = None
    min_score: int | None = None


class ChannelImportOut(BaseModel):
    video_id: int
    already_imported: bool = False


class ApproveRequest(BaseModel):
    account_ids: list[int] | None = None


class ClipOut(BaseModel):
    id: int
    candidate_title: str
    status: ClipStatus
    duration_sec: float | None = None
    has_posts: bool = False


class ClipPublishRequest(BaseModel):
    account_ids: list[int]


class CandidateOut(BaseModel):
    id: int
    video_id: int
    video_title: str
    start_sec: float
    end_sec: float
    score: int
    title: str
    hook: str
    description: str
    hashtags: list
    reason: str
    status: CandidateStatus
    created_at: datetime
    clip_id: int | None = None
    clip_status: ClipStatus | None = None


class PostOut(BaseModel):
    id: int
    clip_id: int
    route_id: int
    account_platform: Platform
    account_name: str
    status: PostStatus
    external_id: str
    external_url: str
    error: str
    attempts: int
    created_at: datetime
    published_at: datetime | None = None


class StatsOut(BaseModel):
    videos: dict[str, int]
    candidates: dict[str, int]
    clips: dict[str, int]
    posts: dict[str, int]
    jobs: dict[str, int]
    recent_failed_jobs: list[dict]


class FailedJobOut(BaseModel):
    type: JobType
    error: str
    run_at: datetime
