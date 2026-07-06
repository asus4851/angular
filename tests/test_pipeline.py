"""Tests for clipfactory.pipeline: queue, stage handlers, worker."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipfactory.models import (
    Account,
    CandidateStatus,
    Channel,
    Clip,
    ClipCandidate,
    ClipStatus,
    Job,
    JobStatus,
    JobType,
    Platform,
    Post,
    PostStatus,
    Route,
    Transcript,
    Video,
    VideoStatus,
)
from clipfactory.pipeline import approve_candidate, drain_queue, enqueue
from clipfactory.pipeline import queue as pipeline_queue
from clipfactory.pipeline import stages
from clipfactory.publish import PublishError
from clipfactory.schemas import Moment, TranscriptSegment
from clipfactory.transcripts import youtube as transcripts_yt
from clipfactory.transcripts.youtube import NoTranscriptAvailable

# ---------------------------------------------------------------------------
# queue.py
# ---------------------------------------------------------------------------


def test_enqueue_dedupes_identical_payload(db):
    with db() as session:
        job1 = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        job2 = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        assert job1.id == job2.id
        assert session.query(Job).count() == 1


def test_enqueue_no_dedupe_creates_separate_jobs(db):
    with db() as session:
        job1 = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        job2 = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1}, dedupe=False)
        assert job1.id != job2.id
        assert session.query(Job).count() == 2


def test_enqueue_different_payload_not_deduped(db):
    with db() as session:
        job1 = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        job2 = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 2})
        assert job1.id != job2.id


def test_enqueue_dedupe_ignores_done_jobs(db):
    with db() as session:
        job1 = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        pipeline_queue.complete(session, job1)
        session.flush()
        job2 = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        assert job1.id != job2.id


def test_claim_next_returns_oldest_due_job(db):
    with db() as session:
        enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 2}, dedupe=False)

        claimed = pipeline_queue.claim_next(session)
        assert claimed is not None
        assert claimed.status == JobStatus.RUNNING


def test_claim_next_returns_none_when_empty(db):
    with db() as session:
        assert pipeline_queue.claim_next(session) is None


def test_claim_next_skips_not_yet_due_jobs(db):
    from datetime import timedelta

    from clipfactory.models import utcnow

    with db() as session:
        enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1}, run_at=utcnow() + timedelta(hours=1))
        assert pipeline_queue.claim_next(session) is None


def test_complete_marks_job_done(db):
    with db() as session:
        job = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        claimed = pipeline_queue.claim_next(session)
        pipeline_queue.complete(session, claimed)
        session.flush()
        assert session.get(Job, job.id).status == JobStatus.DONE


def test_fail_requeues_with_backoff_until_max_attempts(db):
    from clipfactory.models import utcnow

    with db() as session:
        job = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        job.max_attempts = 2
        session.flush()
        before = utcnow()

        claimed = pipeline_queue.claim_next(session)
        pipeline_queue.fail(session, claimed, "boom 1")
        session.flush()
        refreshed = session.get(Job, job.id)
        assert refreshed.status == JobStatus.QUEUED
        assert refreshed.attempts == 1
        assert refreshed.last_error == "boom 1"
        # Backoff is 30 * 2**1 = 60s in the future.
        assert refreshed.run_at > before + __import__("datetime").timedelta(seconds=55)

        # Force it due now so the second attempt can be claimed immediately.
        refreshed.run_at = utcnow()
        session.flush()
        claimed2 = pipeline_queue.claim_next(session)
        assert claimed2 is not None
        pipeline_queue.fail(session, claimed2, "boom 2")
        session.flush()
        final = session.get(Job, job.id)
        assert final.status == JobStatus.FAILED
        assert final.attempts == 2


# ---------------------------------------------------------------------------
# stages.py: handle_fetch_transcript
# ---------------------------------------------------------------------------


def _make_channel(session, **overrides) -> Channel:
    defaults = dict(yt_channel_id="UC_test0000000000000000", title="Test Channel")
    defaults.update(overrides)
    channel = Channel(**defaults)
    session.add(channel)
    session.flush()
    return channel


def _make_video(session, channel: Channel, **overrides) -> Video:
    defaults = dict(channel_id=channel.id, yt_video_id="vid1", title="A video", status=VideoStatus.NEW)
    defaults.update(overrides)
    video = Video(**defaults)
    session.add(video)
    session.flush()
    return video


def test_handle_fetch_transcript_happy_path(db, monkeypatch):
    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel)

        segments = [TranscriptSegment(start=0.0, end=5.0, text="hello world")]
        monkeypatch.setattr(transcripts_yt, "fetch_transcript", lambda vid, langs: ("en", segments))

        stages.handle_fetch_transcript(session, {"video_id": video.id})
        session.flush()

        refreshed = session.get(Video, video.id)
        assert refreshed.status == VideoStatus.TRANSCRIBED
        assert refreshed.transcript is not None
        assert refreshed.transcript.language == "en"

        analyze_jobs = session.query(Job).filter(Job.type == JobType.ANALYZE_VIDEO).all()
        assert len(analyze_jobs) == 1
        assert analyze_jobs[0].payload == {"video_id": video.id}


def test_handle_fetch_transcript_no_transcript_marks_skipped(db, monkeypatch):
    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel)

        def _raise(vid, langs):
            raise NoTranscriptAvailable("nope")

        monkeypatch.setattr(transcripts_yt, "fetch_transcript", _raise)

        stages.handle_fetch_transcript(session, {"video_id": video.id})
        session.flush()

        refreshed = session.get(Video, video.id)
        assert refreshed.status == VideoStatus.SKIPPED
        assert refreshed.error == "nope"
        assert session.query(Job).filter(Job.type == JobType.ANALYZE_VIDEO).count() == 0


def test_handle_fetch_transcript_ignores_non_new_video(db, monkeypatch):
    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel, status=VideoStatus.ANALYZED)

        called = False

        def _fake(vid, langs):
            nonlocal called
            called = True
            return "en", []

        monkeypatch.setattr(transcripts_yt, "fetch_transcript", _fake)
        stages.handle_fetch_transcript(session, {"video_id": video.id})
        assert not called


def test_handle_fetch_transcript_missing_video_is_noop(db):
    with db() as session:
        # Should not raise even though the video doesn't exist.
        stages.handle_fetch_transcript(session, {"video_id": 999999})


# ---------------------------------------------------------------------------
# stages.py: handle_analyze_video / approve_candidate
# ---------------------------------------------------------------------------


class _FakeAnalyzer:
    def __init__(self, moments):
        self._moments = moments

    def find_moments(self, segments, video_title, config):
        return self._moments


def test_handle_analyze_video_creates_candidates_without_auto_approve(db, monkeypatch):
    with db() as session:
        channel = _make_channel(session, auto_approve=False)
        video = _make_video(session, channel, status=VideoStatus.TRANSCRIBED)
        session.add(
            Transcript(
                video_id=video.id,
                language="en",
                segments=transcripts_yt.segments_to_json([TranscriptSegment(start=0, end=30, text="x")]),
            )
        )
        session.flush()

        moments = [Moment(start_sec=0, end_sec=20, score=80, title="Hook")]
        monkeypatch.setattr(stages.analysis, "get_analyzer", lambda: _FakeAnalyzer(moments))

        stages.handle_analyze_video(session, {"video_id": video.id})
        session.flush()

        refreshed = session.get(Video, video.id)
        assert refreshed.status == VideoStatus.ANALYZED
        candidates = session.query(ClipCandidate).filter(ClipCandidate.video_id == video.id).all()
        assert len(candidates) == 1
        assert candidates[0].status == CandidateStatus.PENDING
        assert session.query(Job).filter(Job.type == JobType.RENDER_CLIP).count() == 0


def test_handle_analyze_video_auto_approve_enqueues_render(db, monkeypatch):
    with db() as session:
        channel = _make_channel(session, auto_approve=True)
        video = _make_video(session, channel, status=VideoStatus.TRANSCRIBED)
        session.add(
            Transcript(
                video_id=video.id,
                language="en",
                segments=transcripts_yt.segments_to_json([TranscriptSegment(start=0, end=30, text="x")]),
            )
        )
        session.flush()

        moments = [Moment(start_sec=0, end_sec=20, score=80, title="Hook")]
        monkeypatch.setattr(stages.analysis, "get_analyzer", lambda: _FakeAnalyzer(moments))

        stages.handle_analyze_video(session, {"video_id": video.id})
        session.flush()

        candidate = session.query(ClipCandidate).filter(ClipCandidate.video_id == video.id).one()
        assert candidate.status == CandidateStatus.APPROVED
        assert candidate.clip is not None
        assert candidate.clip.status == ClipStatus.QUEUED

        render_jobs = session.query(Job).filter(Job.type == JobType.RENDER_CLIP).all()
        assert len(render_jobs) == 1
        assert render_jobs[0].payload == {"candidate_id": candidate.id}


def test_approve_candidate_is_idempotent_about_clip_creation(db):
    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel, status=VideoStatus.ANALYZED)
        candidate = ClipCandidate(video_id=video.id, start_sec=0, end_sec=10, score=50)
        session.add(candidate)
        session.flush()

        approve_candidate(session, candidate)
        session.flush()
        session.expire(candidate, ["clip"])
        clip_id = candidate.clip.id

        # Calling again should not create a second Clip row.
        approve_candidate(session, candidate)
        session.flush()
        session.expire(candidate, ["clip"])
        assert candidate.clip.id == clip_id
        assert session.query(Clip).filter(Clip.candidate_id == candidate.id).count() == 1
        # The second RENDER_CLIP enqueue is deduped against the still-queued first one.
        assert session.query(Job).filter(Job.type == JobType.RENDER_CLIP).count() == 1


# ---------------------------------------------------------------------------
# stages.py: handle_publish_post
# ---------------------------------------------------------------------------


def _make_publish_fixture(session, tmp_path: Path, *, title_template="{title}", description_template="{description}\n\n{hashtags}"):
    channel = _make_channel(session, title="My Channel")
    video = _make_video(session, channel, title="My Video")
    candidate = ClipCandidate(
        video_id=video.id,
        start_sec=0,
        end_sec=10,
        score=90,
        title="Great Hook",
        description="A cool moment",
        hook="Did you know?",
        hashtags=["one", "two"],
    )
    session.add(candidate)
    session.flush()

    clip_file = tmp_path / "clip.mp4"
    clip_file.write_bytes(b"fake-mp4")
    clip = Clip(candidate_id=candidate.id, status=ClipStatus.RENDERED, file_path=str(clip_file))
    session.add(clip)

    account = Account(platform=Platform.LOCAL, name="acct1")
    session.add(account)
    session.flush()

    route = Route(
        channel_id=channel.id,
        account_id=account.id,
        title_template=title_template,
        description_template=description_template,
        extra_hashtags=["three"],
    )
    session.add(route)
    session.flush()

    post = Post(clip_id=clip.id, route_id=route.id, status=PostStatus.PENDING)
    session.add(post)
    session.flush()
    return post


def test_handle_publish_post_local_end_to_end(db, settings, tmp_path):
    with db() as session:
        post = _make_publish_fixture(session, tmp_path)

        stages.handle_publish_post(session, {"post_id": post.id})
        session.flush()

        refreshed = session.get(Post, post.id)
        assert refreshed.status == PostStatus.PUBLISHED
        assert refreshed.external_url.startswith("file://")
        assert refreshed.published_at is not None

        export_dir = settings.export_dir / "acct1"
        files = list(export_dir.glob("*.mp4"))
        assert len(files) == 1
        sidecar = files[0].with_suffix(".json")
        assert sidecar.exists()
        import json

        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["title"] == "Great Hook"
        # hashtags deduped: candidate hashtags first, then route extras.
        assert data["hashtags"] == ["one", "two", "three"]
        assert "#one #two #three" in data["description"]


def test_handle_publish_post_missing_template_key_renders_empty(db, settings, tmp_path):
    with db() as session:
        post = _make_publish_fixture(session, tmp_path, title_template="{title} {nope}")
        stages.handle_publish_post(session, {"post_id": post.id})
        session.flush()
        # Should not raise despite the unknown "{nope}" field; rendered as empty.
        refreshed = session.get(Post, post.id)
        assert refreshed.status == PostStatus.PUBLISHED


def test_handle_publish_post_ignores_already_published(db, tmp_path):
    with db() as session:
        post = _make_publish_fixture(session, tmp_path)
        post.status = PostStatus.PUBLISHED
        session.flush()

        stages.handle_publish_post(session, {"post_id": post.id})
        # Nothing should have changed (no re-publish attempt / no error).
        assert session.get(Post, post.id).status == PostStatus.PUBLISHED


def test_handle_publish_post_retryable_error_requeues_and_raises(db, tmp_path, monkeypatch):
    with db() as session:
        post = _make_publish_fixture(session, tmp_path)

        class _FlakyPublisher:
            platform = "local"

            def publish(self, clip_path, metadata, credentials):
                raise PublishError("temporary outage", retryable=True)

        monkeypatch.setattr(stages.publish, "get_publisher", lambda platform: _FlakyPublisher())

        with pytest.raises(PublishError):
            stages.handle_publish_post(session, {"post_id": post.id})
        session.flush()

        refreshed = session.get(Post, post.id)
        assert refreshed.status == PostStatus.PENDING
        assert refreshed.error == "temporary outage"


def test_handle_publish_post_non_retryable_error_marks_failed(db, tmp_path, monkeypatch):
    with db() as session:
        post = _make_publish_fixture(session, tmp_path)

        class _BrokenPublisher:
            platform = "local"

            def publish(self, clip_path, metadata, credentials):
                raise PublishError("bad credentials", retryable=False)

        monkeypatch.setattr(stages.publish, "get_publisher", lambda platform: _BrokenPublisher())

        # Should NOT raise: a non-retryable failure completes the job.
        stages.handle_publish_post(session, {"post_id": post.id})
        session.flush()

        refreshed = session.get(Post, post.id)
        assert refreshed.status == PostStatus.FAILED
        assert refreshed.error == "bad credentials"


# ---------------------------------------------------------------------------
# worker.py: drain_queue
# ---------------------------------------------------------------------------


def test_drain_queue_runs_full_pipeline_to_published(db, settings, monkeypatch, tmp_path):
    from clipfactory import media

    with db() as session:
        channel = _make_channel(session, auto_approve=True, min_score=0)
        account = Account(platform=Platform.LOCAL, name="acct1")
        session.add(account)
        session.flush()
        route = Route(channel_id=channel.id, account_id=account.id)
        session.add(route)

        video = _make_video(session, channel, status=VideoStatus.TRANSCRIBED)
        session.add(
            Transcript(
                video_id=video.id,
                language="en",
                segments=transcripts_yt.segments_to_json([TranscriptSegment(start=0, end=30, text="x")]),
            )
        )
        session.flush()

        moments = [Moment(start_sec=0, end_sec=10, score=90, title="Hook", hashtags=["a"])]
        monkeypatch.setattr(stages.analysis, "get_analyzer", lambda: _FakeAnalyzer(moments))

        source = settings.sources_dir / "demo_vid.mp4"
        media.make_test_video(source, duration=15.0)
        # Simulate a "demo"-style video so handle_render_clip skips the downloader.
        video.yt_video_id = "demovid1"
        source_renamed = settings.sources_dir / "demovid1.mp4"
        source.rename(source_renamed)

        enqueue(session, JobType.ANALYZE_VIDEO, {"video_id": video.id})

    processed = drain_queue(max_jobs=20)
    assert processed >= 3  # analyze -> render -> publish

    with db() as session:
        posts = session.query(Post).all()
        assert len(posts) == 1
        assert posts[0].status == PostStatus.PUBLISHED

        clips = session.query(Clip).all()
        assert len(clips) == 1
        assert clips[0].status == ClipStatus.RENDERED
        assert Path(clips[0].file_path).exists()
