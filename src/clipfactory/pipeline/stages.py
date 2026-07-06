"""Job handlers: poll -> transcript -> analyze -> render -> publish.

Every `handle_*` function is idempotent: it re-checks the relevant entity's
state first and returns early if the work is already done, since jobs can be
re-delivered after a crash (see docs/ARCHITECTURE.md §4). Submodules are
imported at module level (not the names within them) so tests can monkeypatch
e.g. `stages.transcripts_yt.fetch_transcript` without touching the real one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from string import Formatter

from sqlalchemy.orm import Session

from clipfactory import analysis, crypto, media, publish
from clipfactory.ingest import youtube as ingest_yt
from clipfactory.models import (
    CandidateStatus,
    Channel,
    Clip,
    ClipCandidate,
    ClipStatus,
    JobType,
    Platform,
    Post,
    PostStatus,
    Video,
    VideoStatus,
    utcnow,
)
from clipfactory.pipeline import queue
from clipfactory.schemas import AnalysisConfig, PostMetadata, RenderPreset
from clipfactory.transcripts import youtube as transcripts_yt

logger = logging.getLogger(__name__)

# Padding applied by media.downloader.download_section on each side of a
# candidate's [start_sec, end_sec] window; mirrored here to translate the
# candidate's original-video timestamps into source-file-relative ones.
_DOWNLOAD_PADDING_SEC = 5.0


def handle_poll_channel(session: Session, payload: dict) -> None:
    channel = session.get(Channel, payload["channel_id"])
    if channel is None or not channel.enabled:
        return

    new_videos = ingest_yt.discover_new_videos(session, channel)
    session.flush()
    for video in new_videos:
        queue.enqueue(session, JobType.FETCH_TRANSCRIPT, {"video_id": video.id})


def handle_fetch_transcript(session: Session, payload: dict) -> None:
    video = session.get(Video, payload["video_id"])
    if video is None or video.status != VideoStatus.NEW:
        return

    channel = video.channel
    preferred_languages = [channel.language] if channel and channel.language else []

    try:
        language, segments = transcripts_yt.fetch_transcript(video.yt_video_id, preferred_languages)
    except transcripts_yt.NoTranscriptAvailable as exc:
        video.status = VideoStatus.SKIPPED
        video.error = str(exc)
        return

    from clipfactory.models import Transcript

    session.add(
        Transcript(
            video_id=video.id,
            language=language,
            source="auto",
            segments=transcripts_yt.segments_to_json(segments),
        )
    )
    video.status = VideoStatus.TRANSCRIBED
    video.error = ""
    session.flush()
    queue.enqueue(session, JobType.ANALYZE_VIDEO, {"video_id": video.id})


def handle_analyze_video(session: Session, payload: dict) -> None:
    video = session.get(Video, payload["video_id"])
    if video is None or video.status != VideoStatus.TRANSCRIBED:
        return

    channel = video.channel
    transcript = video.transcript
    segments = transcripts_yt.segments_from_json(transcript.segments) if transcript else []

    config = AnalysisConfig(
        max_clips=channel.max_clips_per_video,
        min_score=channel.min_score,
        language=channel.language,
    )
    analyzer = analysis.get_analyzer()
    moments = analyzer.find_moments(segments, video.title, config)

    candidates = []
    for moment in moments:
        candidate = ClipCandidate(
            video_id=video.id,
            start_sec=moment.start_sec,
            end_sec=moment.end_sec,
            score=moment.score,
            title=moment.title,
            hook=moment.hook,
            description=moment.description,
            hashtags=moment.hashtags,
            reason=moment.reason,
        )
        session.add(candidate)
        candidates.append(candidate)

    video.status = VideoStatus.ANALYZED
    session.flush()
    logger.info("handle_analyze_video: video %d -> %d candidate(s)", video.id, len(candidates))

    if channel.auto_approve:
        for candidate in candidates:
            approve_candidate(session, candidate)


def approve_candidate(session: Session, candidate: ClipCandidate) -> None:
    """Approve a candidate, ensure its Clip row exists, and enqueue rendering.

    Shared by the analyze-video auto-approve path and manual moderation (CLI
    `approve` command / the dashboard API).
    """
    candidate.status = CandidateStatus.APPROVED
    session.flush()

    clip = candidate.clip
    if clip is None:
        clip = Clip(candidate_id=candidate.id, status=ClipStatus.QUEUED)
        session.add(clip)
        session.flush()

    queue.enqueue(session, JobType.RENDER_CLIP, {"candidate_id": candidate.id})


def handle_render_clip(session: Session, payload: dict) -> None:
    candidate = session.get(ClipCandidate, payload["candidate_id"])
    if candidate is None or candidate.status != CandidateStatus.APPROVED:
        return

    clip = candidate.clip
    if clip is None:
        clip = Clip(candidate_id=candidate.id, status=ClipStatus.QUEUED)
        session.add(clip)
        session.flush()
    if clip.status == ClipStatus.RENDERED:
        return

    video = candidate.video
    channel = video.channel
    from clipfactory.config import get_settings

    settings = get_settings()

    clip.status = ClipStatus.RENDERING
    clip.error = ""
    session.flush()

    try:
        if video.yt_video_id.startswith("demo"):
            # Demo videos ship a pre-placed local source file (no network):
            # it already spans the whole video at the original time base, so
            # no download padding applies.
            source = settings.sources_dir / f"{video.yt_video_id}.mp4"
            source_start_sec = candidate.start_sec
            source_end_sec = candidate.end_sec
        else:
            source = media.download_section(
                video.yt_video_id, candidate.start_sec, candidate.end_sec, settings.sources_dir
            )
            # Mirrors downloader.download_section's padding exactly: it pads
            # by up to 5s on each side, clamped to not go below 0.
            actual_source_start_original = max(0.0, candidate.start_sec - _DOWNLOAD_PADDING_SEC)
            source_start_sec = candidate.start_sec - actual_source_start_original
            source_end_sec = source_start_sec + (candidate.end_sec - candidate.start_sec)

        transcript = video.transcript
        segments = transcripts_yt.segments_from_json(transcript.segments) if transcript else []
        preset = RenderPreset(**(channel.render_preset or {}))
        output = settings.clips_dir / f"clip_{candidate.id}.mp4"

        media.render_clip(
            source,
            output,
            source_start_sec,
            source_end_sec,
            segments,
            candidate.start_sec,
            candidate.end_sec,
            preset,
        )

        clip.status = ClipStatus.RENDERED
        clip.file_path = str(output)
        clip.duration_sec = media.probe_duration(output)
        clip.width = preset.width
        clip.height = preset.height
        clip.rendered_at = utcnow()
        clip.error = ""
    except Exception as exc:
        clip.status = ClipStatus.FAILED
        clip.error = str(exc)[:2000]
        raise

    session.flush()

    for route in channel.routes:
        if not route.enabled or not route.account.enabled:
            continue
        post = session.query(Post).filter(Post.clip_id == clip.id, Post.route_id == route.id).one_or_none()
        if post is None:
            post = Post(clip_id=clip.id, route_id=route.id, status=PostStatus.PENDING)
            session.add(post)
            session.flush()
        queue.enqueue(session, JobType.PUBLISH_POST, {"post_id": post.id})


class _SafeFormatter(Formatter):
    """A string.Formatter where missing/bad fields render as empty instead of raising."""

    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, "")
        try:
            return super().get_value(key, args, kwargs)
        except (KeyError, IndexError):
            return ""

    def format_field(self, value, format_spec):
        try:
            return super().format_field(value, format_spec)
        except ValueError:
            return str(value)


_safe_formatter = _SafeFormatter()


def _safe_format(template: str, **context: str) -> str:
    try:
        return _safe_formatter.vformat(template, (), context)
    except (KeyError, IndexError, ValueError):
        return template


def handle_publish_post(session: Session, payload: dict) -> None:
    post = session.get(Post, payload["post_id"])
    if post is None or post.status not in (PostStatus.PENDING, PostStatus.UPLOADING):
        return

    route = post.route
    account = route.account
    clip = post.clip
    candidate = clip.candidate
    video = candidate.video
    channel = video.channel

    post.status = PostStatus.UPLOADING
    session.flush()

    hashtags = list(dict.fromkeys([*candidate.hashtags, *route.extra_hashtags]))
    context = {
        "title": candidate.title,
        "description": candidate.description,
        "hook": candidate.hook,
        "hashtags": " ".join(f"#{tag.lstrip('#')}" for tag in hashtags),
        "channel": channel.title,
        "video_title": video.title,
    }
    title = _safe_format(route.title_template, **context)
    description = _safe_format(route.description_template, **context)
    metadata = PostMetadata(title=title, description=description, hashtags=hashtags)

    creds = crypto.decrypt_credentials(account.credentials_encrypted) if account.credentials_encrypted else {}
    credentials = {"name": account.name, **creds} if account.platform == Platform.LOCAL else creds

    publisher = publish.get_publisher(account.platform)

    try:
        result = publisher.publish(Path(clip.file_path), metadata, credentials)
    except publish.PublishError as exc:
        post.error = str(exc)[:2000]
        if exc.retryable:
            post.status = PostStatus.PENDING
            raise
        post.status = PostStatus.FAILED
        return

    post.status = PostStatus.PUBLISHED
    post.external_id = result.external_id
    post.external_url = result.external_url
    post.error = ""
    post.published_at = utcnow()


HANDLERS = {
    JobType.POLL_CHANNEL: handle_poll_channel,
    JobType.FETCH_TRANSCRIPT: handle_fetch_transcript,
    JobType.ANALYZE_VIDEO: handle_analyze_video,
    JobType.RENDER_CLIP: handle_render_clip,
    JobType.PUBLISH_POST: handle_publish_post,
}
