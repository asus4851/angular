"""ORM entities. Single source of truth for the domain model and statuses.

See docs/ARCHITECTURE.md §3 for the entity relationship overview.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Platform(str, enum.Enum):
    LOCAL = "local"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


class VideoStatus(str, enum.Enum):
    NEW = "new"
    TRANSCRIBED = "transcribed"
    ANALYZED = "analyzed"
    SKIPPED = "skipped"  # e.g. no transcript available
    FAILED = "failed"


class CandidateStatus(str, enum.Enum):
    PENDING = "pending"  # waiting for human approval
    APPROVED = "approved"
    REJECTED = "rejected"


class ClipStatus(str, enum.Enum):
    QUEUED = "queued"
    RENDERING = "rendering"
    RENDERED = "rendered"
    FAILED = "failed"


class PostStatus(str, enum.Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobType(str, enum.Enum):
    POLL_CHANNEL = "poll_channel"  # payload: {"channel_id": int}
    FETCH_TRANSCRIPT = "fetch_transcript"  # payload: {"video_id": int}
    ANALYZE_VIDEO = "analyze_video"  # payload: {"video_id": int}
    RENDER_CLIP = "render_clip"  # payload: {"candidate_id": int}
    PUBLISH_POST = "publish_post"  # payload: {"post_id": int}


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    yt_channel_id: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    check_interval_min: Mapped[int] = mapped_column(Integer, default=30)
    max_clips_per_video: Mapped[int] = mapped_column(Integer, default=3)
    min_score: Mapped[int] = mapped_column(Integer, default=60)
    language: Mapped[str] = mapped_column(String(8), default="")  # preferred transcript language
    render_preset: Mapped[dict] = mapped_column(JSON, default=dict)  # overrides for RenderPreset
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    routes: Mapped[list[Route]] = relationship(back_populates="channel", cascade="all, delete-orphan")
    videos: Mapped[list[Video]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[Platform] = mapped_column(Enum(Platform, values_callable=lambda e: [m.value for m in e]))
    name: Mapped[str] = mapped_column(String(128))
    credentials_encrypted: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    routes: Mapped[list[Route]] = relationship(back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("platform", "name", name="uq_account_platform_name"),)


class Route(Base):
    """Assignment: videos from a channel are published to an account."""

    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    title_template: Mapped[str] = mapped_column(String(512), default="{title}")
    description_template: Mapped[str] = mapped_column(Text, default="{description}\n\n{hashtags}")
    extra_hashtags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channel: Mapped[Channel] = relationship(back_populates="routes")
    account: Mapped[Account] = relationship(back_populates="routes")

    __table_args__ = (UniqueConstraint("channel_id", "account_id", name="uq_route_channel_account"),)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    yt_video_id: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    duration_sec: Mapped[float | None] = mapped_column(Float, default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    status: Mapped[VideoStatus] = mapped_column(
        Enum(VideoStatus, values_callable=lambda e: [m.value for m in e]), default=VideoStatus.NEW
    )
    error: Mapped[str] = mapped_column(Text, default="")
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channel: Mapped[Channel] = relationship(back_populates="videos")
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="video", uselist=False, cascade="all, delete-orphan"
    )
    candidates: Mapped[list[ClipCandidate]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), unique=True)
    language: Mapped[str] = mapped_column(String(8), default="")
    source: Mapped[str] = mapped_column(String(16), default="auto")  # auto | manual
    segments: Mapped[list] = mapped_column(JSON, default=list)  # [{"start": s, "end": s, "text": str}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped[Video] = relationship(back_populates="transcript")


class ClipCandidate(Base):
    __tablename__ = "clip_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    start_sec: Mapped[float] = mapped_column(Float)
    end_sec: Mapped[float] = mapped_column(Float)
    score: Mapped[int] = mapped_column(Integer, default=0)  # 0..100
    title: Mapped[str] = mapped_column(String(512), default="")
    hook: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[CandidateStatus] = mapped_column(
        Enum(CandidateStatus, values_callable=lambda e: [m.value for m in e]),
        default=CandidateStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped[Video] = relationship(back_populates="candidates")
    clip: Mapped[Clip | None] = relationship(back_populates="candidate", uselist=False, cascade="all, delete-orphan")


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("clip_candidates.id", ondelete="CASCADE"), unique=True)
    file_path: Mapped[str] = mapped_column(String(1024), default="")
    duration_sec: Mapped[float | None] = mapped_column(Float, default=None)
    width: Mapped[int] = mapped_column(Integer, default=1080)
    height: Mapped[int] = mapped_column(Integer, default=1920)
    status: Mapped[ClipStatus] = mapped_column(
        Enum(ClipStatus, values_callable=lambda e: [m.value for m in e]), default=ClipStatus.QUEUED
    )
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    rendered_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    candidate: Mapped[ClipCandidate] = relationship(back_populates="clip")
    posts: Mapped[list[Post]] = relationship(back_populates="clip", cascade="all, delete-orphan")


class Post(Base):
    """A publication of a clip. Targeted either via a Route (channel->account
    assignment) or ad-hoc via a direct account_id — exactly one of the two is set."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"))
    route_id: Mapped[int | None] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), nullable=True, default=None
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, default=None
    )
    status: Mapped[PostStatus] = mapped_column(
        Enum(PostStatus, values_callable=lambda e: [m.value for m in e]), default=PostStatus.PENDING
    )
    external_id: Mapped[str] = mapped_column(String(256), default="")
    external_url: Mapped[str] = mapped_column(String(1024), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    clip: Mapped[Clip] = relationship(back_populates="posts")
    route: Mapped[Route | None] = relationship()
    account: Mapped[Account | None] = relationship()

    __table_args__ = (
        UniqueConstraint("clip_id", "route_id", name="uq_post_clip_route"),
        UniqueConstraint("clip_id", "account_id", name="uq_post_clip_account"),
    )

    @property
    def target_account(self) -> Account | None:
        """The account this post publishes to, whichever way it was targeted."""
        return self.route.account if self.route is not None else self.account


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[JobType] = mapped_column(Enum(JobType, values_callable=lambda e: [m.value for m in e]))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, values_callable=lambda e: [m.value for m in e]), default=JobStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=4)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_jobs_status_run_at", "status", "run_at"),)
