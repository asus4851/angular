"""Job handlers: poll -> transcript -> analyze -> render -> publish.

Every `handle_*` function is idempotent: it re-checks the relevant entity's
state first and returns early if the work is already done, since jobs can be
re-delivered after a crash (see docs/ARCHITECTURE.md §4). Submodules are
imported at module level (not the names within them) so tests can monkeypatch
e.g. `stages.transcripts_yt.fetch_transcript` without touching the real one.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from string import Formatter

from sqlalchemy.orm import Session

from clipfactory import analysis, crypto, media, publish
from clipfactory.config import get_settings
from clipfactory.ingest import youtube as ingest_yt
from clipfactory.media.downloader import PADDING_SEC
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

# How many app-level retries a "no transcript yet" video gets, and for how
# long after publish, before we give up and mark it SKIPPED for good. Fresh
# uploads often don't have auto-captions ready immediately.
_TRANSCRIPT_RETRY_MAX = 3
_TRANSCRIPT_RETRY_DELAY = timedelta(hours=2)
_TRANSCRIPT_RETRY_WINDOW = timedelta(hours=24)


def handle_poll_channel(session: Session, payload: dict) -> None:
    channel = session.get(Channel, payload["channel_id"])
    if channel is None or not channel.enabled:
        return

    new_videos = ingest_yt.discover_new_videos(session, channel)
    session.flush()
    for video in new_videos:
        queue.enqueue(session, JobType.FETCH_TRANSCRIPT, {"video_id": video.id})


def _enqueue_analyze_video(session: Session, video: Video, payload: dict) -> None:
    analyze_payload: dict = {"video_id": video.id}
    if payload.get("analysis_overrides"):
        analyze_payload["overrides"] = payload["analysis_overrides"]
    queue.enqueue(session, JobType.ANALYZE_VIDEO, analyze_payload)


def handle_fetch_transcript(session: Session, payload: dict) -> None:
    from clipfactory.models import Transcript

    video = session.get(Video, payload["video_id"])
    if video is None or video.status != VideoStatus.NEW:
        return

    # Query the table directly rather than the `video.transcript` relationship:
    # touching that relationship here (before any Transcript row exists) would
    # cache "no transcript" on this in-session Video object, which the later
    # success path's `session.add(Transcript(video_id=...))` -- a plain insert
    # that bypasses the relationship -- would never invalidate.
    existing_transcript = session.query(Transcript).filter(Transcript.video_id == video.id).one_or_none()
    if existing_transcript is not None:
        # A video reset to NEW for re-import (e.g. after a failed analysis)
        # that already has a Transcript row must not attempt to insert a
        # second one -- `transcripts.video_id` is unique. Just resume from
        # where the pipeline actually is.
        video.status = VideoStatus.TRANSCRIBED
        video.error = ""
        session.flush()
        _enqueue_analyze_video(session, video, payload)
        return

    channel = video.channel
    preferred_languages = [channel.language] if channel and channel.language else []

    try:
        language, segments = transcripts_yt.fetch_transcript(video.yt_video_id, preferred_languages)
    except transcripts_yt.NoTranscriptAvailable as exc:
        retries = payload.get("transcript_retries", 0)
        is_fresh = video.published_at is None or video.published_at > utcnow() - _TRANSCRIPT_RETRY_WINDOW
        if retries < _TRANSCRIPT_RETRY_MAX and is_fresh:
            # Auto-captions for a just-published video may simply not exist
            # yet; keep the video NEW and try again later instead of
            # dead-ending it as SKIPPED.
            video.error = f"no transcript yet (retry {retries + 1}/{_TRANSCRIPT_RETRY_MAX}): {exc}"
            session.flush()
            queue.enqueue(
                session,
                JobType.FETCH_TRANSCRIPT,
                {**payload, "transcript_retries": retries + 1},
                run_at=utcnow() + _TRANSCRIPT_RETRY_DELAY,
            )
            return
        video.status = VideoStatus.SKIPPED
        video.error = str(exc)
        return

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
    _enqueue_analyze_video(session, video, payload)


def handle_analyze_video(session: Session, payload: dict) -> None:
    video_id = payload["video_id"]
    video = session.get(Video, video_id)
    # FAILED is accepted alongside TRANSCRIBED so a prior analyzer crash
    # (which persists FAILED, see the except-block below) can be retried by
    # job redelivery instead of being silently skipped forever.
    if video is None or video.status not in (VideoStatus.TRANSCRIBED, VideoStatus.FAILED):
        return

    channel = video.channel
    transcript = video.transcript
    segments = transcripts_yt.segments_from_json(transcript.segments) if transcript else []

    overrides = payload.get("overrides") or {}
    config = AnalysisConfig(
        max_clips=overrides.get("max_clips", channel.max_clips_per_video),
        min_score=overrides.get("min_score", channel.min_score),
        language=overrides.get("language", channel.language),
    )
    analyzer = analysis.get_analyzer()
    try:
        moments = analyzer.find_moments(segments, video.title, config)
    except Exception as exc:
        # The handler runs inside a session_scope() that rolls back on
        # exception, which would otherwise silently discard this FAILED
        # write along with the error message -- commit it explicitly before
        # re-raising so it's actually visible (and so a re-import isn't
        # blocked by a video stuck at TRANSCRIBED with an empty error).
        session.rollback()
        video = session.get(Video, video_id)
        video.status = VideoStatus.FAILED
        video.error = str(exc)[:2000]
        session.commit()
        raise

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
    video.error = ""
    session.flush()
    logger.info("handle_analyze_video: video %d -> %d candidate(s)", video.id, len(candidates))

    if channel.auto_approve:
        for candidate in candidates:
            approve_candidate(session, candidate)


def approve_candidate(
    session: Session, candidate: ClipCandidate, account_ids: list[int] | None = None
) -> None:
    """Approve a candidate, ensure its Clip row exists, and enqueue rendering.

    Shared by the analyze-video auto-approve path and manual moderation (CLI
    `approve` command / the dashboard API). `account_ids` optionally adds
    ad-hoc publish targets on top of the channel's routes; the posts are
    created now and picked up for publishing once the clip is rendered.
    """
    candidate.status = CandidateStatus.APPROVED
    session.flush()

    clip = candidate.clip
    if clip is None:
        clip = Clip(candidate_id=candidate.id, status=ClipStatus.QUEUED)
        session.add(clip)
        session.flush()

    if account_ids:
        publish_clip_to_accounts(session, clip, account_ids)

    queue.enqueue(session, JobType.RENDER_CLIP, {"candidate_id": candidate.id})


def publish_clip_to_accounts(session: Session, clip: Clip, account_ids: list[int]) -> list[Post]:
    """Create ad-hoc Posts targeting accounts directly (no route needed).

    PUBLISH_POST jobs are enqueued immediately regardless of clip status:
    handle_publish_post itself guards on the clip being RENDERED and raises
    (for a backoff retry) otherwise, since a render triggered by this same
    approval may not have committed yet by the time we get here -- waiting
    for handle_render_clip's own tail-scan to pick the post up instead would
    race it.
    """
    from clipfactory.models import Account

    existing_posts = session.query(Post).filter(Post.clip_id == clip.id).all()
    targeted_account_ids = {
        post.account_id if post.account_id is not None else post.route.account_id
        for post in existing_posts
    }
    targeted_account_ids.discard(None)

    posts = []
    for account_id in dict.fromkeys(account_ids):
        if account_id in targeted_account_ids:
            logger.info(
                "publish_clip_to_accounts: skipping account %s, clip %d already targets it",
                account_id,
                clip.id,
            )
            continue
        account = session.get(Account, account_id)
        if account is None or not account.enabled:
            logger.warning("publish_clip_to_accounts: skipping unknown/disabled account %s", account_id)
            continue
        post = (
            session.query(Post).filter(Post.clip_id == clip.id, Post.account_id == account_id).one_or_none()
        )
        if post is None:
            post = Post(clip_id=clip.id, account_id=account_id, status=PostStatus.PENDING)
            session.add(post)
            session.flush()
        posts.append(post)
        targeted_account_ids.add(account_id)
        if post.status == PostStatus.PENDING:
            queue.enqueue(session, JobType.PUBLISH_POST, {"post_id": post.id})
    return posts


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
    settings = get_settings()

    clip_id = clip.id
    clip.status = ClipStatus.RENDERING
    clip.error = ""
    # Commit (not just flush) before the multi-minute download/ffmpeg work
    # below: holding an open write transaction across it would keep SQLite's
    # single writer lock the whole time, starving every other job. This also
    # makes the RENDERING state actually crash-durable.
    session.commit()

    try:
        local_source = settings.sources_dir / "local" / f"{video.yt_video_id}.mp4"
        if local_source.exists():
            # Locally-sourced videos (demos, manual imports) ship a
            # pre-placed file spanning the whole video at the original time
            # base, so no download padding applies.
            source = local_source
            source_start_sec = candidate.start_sec
            source_end_sec = candidate.end_sec
        else:
            source = media.download_section(
                video.yt_video_id, candidate.start_sec, candidate.end_sec, settings.sources_dir
            )
            # Mirrors downloader.download_section's padding exactly: it pads
            # by up to 5s on each side, clamped to not go below 0.
            actual_source_start_original = max(0.0, candidate.start_sec - PADDING_SEC)
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
        # We're past the commit() above, so a plain `session.rollback()` here
        # only discards this handler's own (uncommitted) changes -- it can't
        # undo the RENDERING commit. Without the rollback+refetch+commit,
        # this FAILED write would otherwise be silently discarded by the
        # worker's session_scope() rollback when it catches the re-raise.
        session.rollback()
        clip = session.get(Clip, clip_id)
        clip.status = ClipStatus.FAILED
        clip.error = str(exc)[:2000]
        session.commit()
        raise

    session.flush()

    adhoc_targets = {
        post.account_id
        for post in session.query(Post).filter(Post.clip_id == clip.id, Post.account_id.isnot(None)).all()
    }

    for route in channel.routes:
        if not route.enabled or not route.account.enabled:
            continue
        if route.account_id in adhoc_targets:
            logger.info(
                "handle_render_clip: skipping route %d, clip %d already has an ad-hoc post to account %s",
                route.id,
                clip.id,
                route.account_id,
            )
            continue
        post = session.query(Post).filter(Post.clip_id == clip.id, Post.route_id == route.id).one_or_none()
        if post is None:
            post = Post(clip_id=clip.id, route_id=route.id, status=PostStatus.PENDING)
            session.add(post)
            session.flush()
        queue.enqueue(session, JobType.PUBLISH_POST, {"post_id": post.id})

    # Ad-hoc posts created at approve time (direct account targets) are
    # normally already enqueued by publish_clip_to_accounts; this is a
    # harmless, deduped safety net for the (race-prone) case where the
    # render committed and started before that enqueue did.
    for post in clip.posts:
        if post.route_id is None and post.status == PostStatus.PENDING:
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

    def get_field(self, field_name, args, kwargs):
        # get_field resolves attribute/index access (e.g. "{title.upper}" or
        # "{0[foo]}"), which get_value alone doesn't cover: it can raise
        # AttributeError, TypeError, etc. on top of KeyError/IndexError (e.g.
        # a plain str title has no arbitrary attribute), which used to
        # propagate out of vformat() and crash the publish job entirely.
        try:
            return super().get_field(field_name, args, kwargs)
        except Exception:
            return "", field_name

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
    post_id = payload["post_id"]
    post = session.get(Post, post_id)
    if post is None or post.status not in (PostStatus.PENDING, PostStatus.UPLOADING):
        return

    route = post.route
    account = post.target_account
    if account is None:
        post.status = PostStatus.FAILED
        post.error = "post has neither a route nor an account target"
        return
    clip = post.clip
    candidate = clip.candidate
    video = candidate.video
    channel = video.channel

    if clip.status != ClipStatus.RENDERED:
        # publish_clip_to_accounts enqueues ad-hoc posts unconditionally, so
        # this job can be claimed before the render (or even the API
        # transaction that approved the candidate) has committed. Raising
        # lets the normal job-retry backoff handle waiting for it, instead
        # of the post being stuck PENDING forever with nothing to re-drive it.
        raise RuntimeError(f"clip {clip.id} not rendered yet (status={clip.status.value})")

    post.status = PostStatus.UPLOADING
    # Commit before the (potentially slow, network-bound) publisher call
    # below so it doesn't hold SQLite's write lock, and so UPLOADING is
    # actually durable if the process dies mid-upload.
    session.commit()

    extra_hashtags = route.extra_hashtags if route is not None else []
    title_template = route.title_template if route is not None else "{title}"
    description_template = route.description_template if route is not None else "{description}\n\n{hashtags}"

    hashtags = list(dict.fromkeys([*candidate.hashtags, *extra_hashtags]))
    context = {
        "title": candidate.title,
        "description": candidate.description,
        "hook": candidate.hook,
        "hashtags": " ".join(f"#{tag.lstrip('#')}" for tag in hashtags),
        "channel": channel.title,
        "video_title": video.title,
    }
    title = _safe_format(title_template, **context)
    description = _safe_format(description_template, **context)
    metadata = PostMetadata(title=title, description=description, hashtags=hashtags)

    creds = crypto.decrypt_credentials(account.credentials_encrypted) if account.credentials_encrypted else {}
    credentials = {"name": account.name, **creds} if account.platform == Platform.LOCAL else creds

    publisher = publish.get_publisher(account.platform)

    try:
        result = publisher.publish(Path(clip.file_path), metadata, credentials)
    except publish.PublishError as exc:
        error_msg = str(exc)[:2000]
        if exc.retryable:
            # Past the UPLOADING commit above, so this rollback only clears
            # this handler's own (empty, so far) uncommitted changes -- it's
            # the refetch-then-commit that guarantees this write survives the
            # worker's session_scope() rollback on the re-raise below.
            session.rollback()
            post = session.get(Post, post_id)
            post.attempts += 1
            post.error = error_msg
            if post.attempts >= get_settings().job_max_attempts:
                # Retries exhausted: the PUBLISH_POST job itself would also
                # give up here and mark itself FAILED, but the Post used to
                # stay PENDING forever with no error -- the API's retry
                # endpoint only accepts FAILED posts, so this was a dead end
                # a human could never recover from. Fail the post too, and
                # complete the job (no raise) instead of letting it exhaust
                # its own attempts redundantly.
                post.status = PostStatus.FAILED
                session.commit()
                return
            post.status = PostStatus.PENDING
            session.commit()
            raise
        session.rollback()
        post = session.get(Post, post_id)
        post.status = PostStatus.FAILED
        post.error = error_msg
        session.commit()
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
