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


def test_requeue_stale_running_requeues_old_running_jobs(db):
    from datetime import timedelta

    from clipfactory.models import utcnow

    with db() as session:
        job = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        claimed = pipeline_queue.claim_next(session)
        assert claimed.status == JobStatus.RUNNING
        session.flush()

        # Simulate a worker that died mid-handler a while ago: back-date
        # updated_at past the staleness threshold with a raw UPDATE so the
        # mapper's onupdate=utcnow doesn't immediately overwrite it.
        from sqlalchemy import update

        from clipfactory.models import Job as JobModel

        session.execute(
            update(JobModel).where(JobModel.id == job.id).values(updated_at=utcnow() - timedelta(minutes=45))
        )
        session.flush()

        count = pipeline_queue.requeue_stale_running(session, older_than_min=30)
        assert count == 1
        session.flush()

        refreshed = session.get(Job, job.id)
        assert refreshed.status == JobStatus.QUEUED
        assert refreshed.run_at <= utcnow()


def test_requeue_stale_running_leaves_recent_running_jobs_alone(db):
    with db() as session:
        job = enqueue(session, JobType.POLL_CHANNEL, {"channel_id": 1})
        pipeline_queue.claim_next(session)
        session.flush()

        count = pipeline_queue.requeue_stale_running(session, older_than_min=30)
        assert count == 0

        refreshed = session.get(Job, job.id)
        assert refreshed.status == JobStatus.RUNNING


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
    from datetime import timedelta

    from clipfactory.models import utcnow

    with db() as session:
        channel = _make_channel(session)
        # An old video (not "fresh") should be SKIPPED immediately rather
        # than entering the fresh-video retry loop (see the test below).
        video = _make_video(session, channel, published_at=utcnow() - timedelta(days=30))

        def _raise(vid, langs):
            raise NoTranscriptAvailable("nope")

        monkeypatch.setattr(transcripts_yt, "fetch_transcript", _raise)

        stages.handle_fetch_transcript(session, {"video_id": video.id})
        session.flush()

        refreshed = session.get(Video, video.id)
        assert refreshed.status == VideoStatus.SKIPPED
        assert refreshed.error == "nope"
        assert session.query(Job).filter(Job.type == JobType.ANALYZE_VIDEO).count() == 0


def test_handle_fetch_transcript_fresh_video_retries_instead_of_skipping(db, monkeypatch):
    """A just-published video whose captions aren't ready yet should be
    retried later instead of dead-ending as SKIPPED (fix: fresh videos)."""
    from datetime import timedelta

    from clipfactory.models import utcnow

    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel, published_at=utcnow() - timedelta(hours=1))

        def _raise(vid, langs):
            raise NoTranscriptAvailable("not ready")

        monkeypatch.setattr(transcripts_yt, "fetch_transcript", _raise)

        stages.handle_fetch_transcript(session, {"video_id": video.id})
        session.flush()

        refreshed = session.get(Video, video.id)
        # Still NEW -- not SKIPPED -- so a later poll/import can retry it.
        assert refreshed.status == VideoStatus.NEW

        retry_jobs = session.query(Job).filter(Job.type == JobType.FETCH_TRANSCRIPT).all()
        assert len(retry_jobs) == 1
        assert retry_jobs[0].payload == {"video_id": video.id, "transcript_retries": 1}
        assert retry_jobs[0].run_at > utcnow() + timedelta(hours=1)


def test_handle_fetch_transcript_fresh_video_gives_up_after_max_retries(db, monkeypatch):
    from datetime import timedelta

    from clipfactory.models import utcnow

    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel, published_at=utcnow() - timedelta(hours=1))

        def _raise(vid, langs):
            raise NoTranscriptAvailable("still not ready")

        monkeypatch.setattr(transcripts_yt, "fetch_transcript", _raise)

        stages.handle_fetch_transcript(session, {"video_id": video.id, "transcript_retries": 3})
        session.flush()

        refreshed = session.get(Video, video.id)
        assert refreshed.status == VideoStatus.SKIPPED
        assert session.query(Job).filter(Job.type == JobType.FETCH_TRANSCRIPT).count() == 0


def test_handle_fetch_transcript_reimport_with_existing_transcript_skips_refetch(db, monkeypatch):
    """A video reset to NEW that already has a Transcript (re-import) must
    resume the pipeline instead of crashing on the unique constraint."""
    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel, status=VideoStatus.NEW)
        session.add(
            Transcript(
                video_id=video.id,
                language="en",
                segments=transcripts_yt.segments_to_json([TranscriptSegment(start=0, end=5, text="hi")]),
            )
        )
        session.flush()

        called = False

        def _fake(vid, langs):
            nonlocal called
            called = True
            return "en", []

        monkeypatch.setattr(transcripts_yt, "fetch_transcript", _fake)

        stages.handle_fetch_transcript(session, {"video_id": video.id, "analysis_overrides": {"max_clips": 5}})
        session.flush()

        assert not called, "fetch_transcript should not be called when a transcript already exists"
        refreshed = session.get(Video, video.id)
        assert refreshed.status == VideoStatus.TRANSCRIBED
        assert session.query(Transcript).filter(Transcript.video_id == video.id).count() == 1

        analyze_jobs = session.query(Job).filter(Job.type == JobType.ANALYZE_VIDEO).all()
        assert len(analyze_jobs) == 1
        assert analyze_jobs[0].payload == {"video_id": video.id, "overrides": {"max_clips": 5}}


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


class _BrokenAnalyzer:
    def find_moments(self, segments, video_title, config):
        raise RuntimeError("analyzer exploded")


def test_handle_analyze_video_failure_persists_failed_status_and_raises(db, monkeypatch):
    """A crashing analyzer must not leave the video silently stuck at
    TRANSCRIBED with an empty error -- the handler runs inside a
    session_scope() that rolls back on exception, so the FAILED write has to
    be committed explicitly before re-raising."""
    # Committed in its own session/transaction first, exactly like a real
    # worker run: FETCH_TRANSCRIPT's session_scope() would have already
    # committed the video+transcript before ANALYZE_VIDEO is even claimed.
    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel, status=VideoStatus.TRANSCRIBED)
        session.add(
            Transcript(
                video_id=video.id,
                language="en",
                segments=transcripts_yt.segments_to_json([TranscriptSegment(start=0, end=30, text="x")]),
            )
        )
        video_id = video.id

    monkeypatch.setattr(stages.analysis, "get_analyzer", lambda: _BrokenAnalyzer())

    with db() as session:
        with pytest.raises(RuntimeError, match="analyzer exploded"):
            stages.handle_analyze_video(session, {"video_id": video_id})

    # Re-open a fresh session/transaction to prove the write actually
    # committed rather than merely being visible before the outer rollback.
    with db() as session:
        refreshed = session.get(Video, video_id)
        assert refreshed.status == VideoStatus.FAILED
        assert "analyzer exploded" in refreshed.error


def test_handle_analyze_video_retries_a_previously_failed_video(db, monkeypatch):
    """Job redelivery after the crash above must not be a no-op: the top
    guard has to accept FAILED (not just TRANSCRIBED)."""
    with db() as session:
        channel = _make_channel(session)
        video = _make_video(session, channel, status=VideoStatus.FAILED, error="boom")
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
        assert refreshed.error == ""
        assert session.query(ClipCandidate).filter(ClipCandidate.video_id == video.id).count() == 1


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
# stages.py: handle_render_clip / publish_clip_to_accounts
# ---------------------------------------------------------------------------


def _make_render_candidate(session, channel, *, yt_video_id="renderclip1", start_sec=0.0, end_sec=3.0):
    video = _make_video(session, channel, yt_video_id=yt_video_id, status=VideoStatus.ANALYZED)
    candidate = ClipCandidate(
        video_id=video.id,
        start_sec=start_sec,
        end_sec=end_sec,
        score=90,
        status=CandidateStatus.APPROVED,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _touch_local_source(settings, yt_video_id: str) -> Path:
    """Place a (fake, since render_clip is monkeypatched in these tests)
    local source file where the fix-12 local-source mechanism looks for it."""
    local_dir = settings.sources_dir / "local"
    local_dir.mkdir(parents=True, exist_ok=True)
    path = local_dir / f"{yt_video_id}.mp4"
    path.write_bytes(b"fake-source")
    return path


def _fake_render_clip(monkeypatch):
    def _fake(source, output, *args, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-rendered-clip")
        return output

    monkeypatch.setattr(stages.media, "render_clip", _fake)
    monkeypatch.setattr(stages.media, "probe_duration", lambda path: 3.0)


def test_handle_render_clip_uses_local_source_without_downloading(db, settings, monkeypatch):
    with db() as session:
        channel = _make_channel(session)
        candidate = _make_render_candidate(session, channel)
        video = candidate.video
        _touch_local_source(settings, video.yt_video_id)
        _fake_render_clip(monkeypatch)

        def _boom_download(*args, **kwargs):
            raise AssertionError("download_section should not be called when a local source exists")

        monkeypatch.setattr(stages.media, "download_section", _boom_download)

        stages.handle_render_clip(session, {"candidate_id": candidate.id})
        session.flush()
        # handle_render_clip creates the Clip row via a direct FK insert
        # rather than through the `candidate.clip` relationship setter, so
        # this in-session object's cached (pre-render) view of it must be
        # dropped before re-reading.
        session.expire(candidate, ["clip"])

        clip = candidate.clip
        assert clip.status == ClipStatus.RENDERED
        assert Path(clip.file_path).exists()


def test_handle_render_clip_failure_persists_failed_status_and_raises(db, settings, monkeypatch):
    """A crashing render must not leave the clip silently stuck at RENDERING
    -- the handler commits RENDERING before the long-running work (fix: long
    transaction) and must survive the worker's session_scope() rollback on
    the re-raise (fix: FAILED state rolled back)."""
    with db() as session:
        channel = _make_channel(session)
        candidate = _make_render_candidate(session, channel)
        video = candidate.video
        _touch_local_source(settings, video.yt_video_id)

        def _boom(*args, **kwargs):
            raise RuntimeError("ffmpeg exploded")

        monkeypatch.setattr(stages.media, "render_clip", _boom)

        candidate_id = candidate.id
        with pytest.raises(RuntimeError, match="ffmpeg exploded"):
            stages.handle_render_clip(session, {"candidate_id": candidate_id})

    # Re-open a fresh session to prove the FAILED write actually committed.
    with db() as session:
        clip = session.get(ClipCandidate, candidate_id).clip
        assert clip.status == ClipStatus.FAILED
        assert "ffmpeg exploded" in clip.error


def test_handle_render_clip_skips_route_already_targeted_by_adhoc_post(db, settings, monkeypatch):
    with db() as session:
        channel = _make_channel(session)
        account = Account(platform=Platform.LOCAL, name="acct1")
        session.add(account)
        session.flush()
        session.add(Route(channel_id=channel.id, account_id=account.id))
        session.flush()

        candidate = _make_render_candidate(session, channel)
        video = candidate.video
        _touch_local_source(settings, video.yt_video_id)
        _fake_render_clip(monkeypatch)

        clip = Clip(candidate_id=candidate.id, status=ClipStatus.QUEUED)
        session.add(clip)
        session.flush()
        adhoc_post = Post(clip_id=clip.id, account_id=account.id, status=PostStatus.PENDING)
        session.add(adhoc_post)
        session.flush()

        stages.handle_render_clip(session, {"candidate_id": candidate.id})
        session.flush()

        posts = session.query(Post).filter(Post.clip_id == clip.id).all()
        # Only the pre-existing ad-hoc post -- no duplicate route-based post
        # created for the same account.
        assert len(posts) == 1
        assert posts[0].id == adhoc_post.id

        publish_jobs = session.query(Job).filter(Job.type == JobType.PUBLISH_POST).all()
        assert len(publish_jobs) == 1
        assert publish_jobs[0].payload == {"post_id": adhoc_post.id}


def test_publish_clip_to_accounts_skips_account_already_targeted_via_route(db):
    with db() as session:
        channel = _make_channel(session)
        account = Account(platform=Platform.LOCAL, name="acct1")
        session.add(account)
        session.flush()
        route = Route(channel_id=channel.id, account_id=account.id)
        session.add(route)
        session.flush()

        candidate = _make_render_candidate(session, channel)
        clip = Clip(candidate_id=candidate.id, status=ClipStatus.RENDERED, file_path="/tmp/x.mp4")
        session.add(clip)
        session.flush()
        session.add(Post(clip_id=clip.id, route_id=route.id, status=PostStatus.PUBLISHED))
        session.flush()

        posts = stages.publish_clip_to_accounts(session, clip, [account.id])

        assert posts == []
        assert session.query(Post).filter(Post.clip_id == clip.id).count() == 1


def test_publish_clip_to_accounts_enqueues_even_if_clip_not_rendered(db):
    """Ad-hoc posts must be enqueued unconditionally (not gated on
    clip.status == RENDERED) so the render-handler's tail scan racing the
    approving transaction can't leave the post stranded PENDING forever."""
    with db() as session:
        channel = _make_channel(session)
        account = Account(platform=Platform.LOCAL, name="acct1")
        session.add(account)
        session.flush()

        candidate = _make_render_candidate(session, channel)
        clip = Clip(candidate_id=candidate.id, status=ClipStatus.QUEUED)
        session.add(clip)
        session.flush()

        posts = stages.publish_clip_to_accounts(session, clip, [account.id])
        session.flush()

        assert len(posts) == 1
        publish_jobs = session.query(Job).filter(Job.type == JobType.PUBLISH_POST).all()
        assert len(publish_jobs) == 1
        assert publish_jobs[0].payload == {"post_id": posts[0].id}


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


def test_handle_publish_post_raises_when_clip_not_rendered(db, tmp_path):
    """publish_clip_to_accounts now enqueues PUBLISH_POST unconditionally, so
    this job can be claimed before the render commits; it must back off via
    a raise instead of silently no-op'ing (which would strand the post
    PENDING forever)."""
    with db() as session:
        post = _make_publish_fixture(session, tmp_path)
        post.clip.status = ClipStatus.QUEUED
        session.flush()

        with pytest.raises(RuntimeError, match="not rendered yet"):
            stages.handle_publish_post(session, {"post_id": post.id})

        refreshed = session.get(Post, post.id)
        assert refreshed.status == PostStatus.PENDING


def test_handle_publish_post_retry_dead_end_fails_post_after_max_attempts(db, tmp_path, monkeypatch, settings):
    """A publish job that exhausts its retries used to leave the job FAILED
    but the post stuck PENDING with no error -- the API's retry endpoint
    only accepts FAILED posts, so a human could never recover it. Post
    attempts must independently drive the post to FAILED once they reach
    job_max_attempts, and the job should complete (not raise) at that point."""
    from clipfactory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    try:
        with db() as session:
            post = _make_publish_fixture(session, tmp_path)

            class _FlakyPublisher:
                platform = "local"

                def publish(self, clip_path, metadata, credentials):
                    raise PublishError("temporary outage", retryable=True)

            monkeypatch.setattr(stages.publish, "get_publisher", lambda platform: _FlakyPublisher())

            # First failure: attempts=1 < job_max_attempts=2 -> requeue + raise.
            with pytest.raises(PublishError):
                stages.handle_publish_post(session, {"post_id": post.id})
            session.flush()
            refreshed = session.get(Post, post.id)
            assert refreshed.status == PostStatus.PENDING
            assert refreshed.attempts == 1

            # Second failure: attempts=2 >= job_max_attempts=2 -> FAILED, no raise.
            stages.handle_publish_post(session, {"post_id": post.id})
            session.flush()
            refreshed = session.get(Post, post.id)
            assert refreshed.status == PostStatus.FAILED
            assert refreshed.attempts == 2
            assert refreshed.error == "temporary outage"
    finally:
        get_settings.cache_clear()


def test_safe_format_survives_attribute_error_on_field_access():
    """'{title.upper}'-style attribute/index access goes through
    Formatter.get_field, not get_value -- a plain str title has no such
    attribute, and that AttributeError used to propagate out of vformat()
    and crash the publish job entirely."""
    result = stages._safe_format("hello {title.nonexistent_attr}!", title="world")
    assert result == "hello !"

    # A template with no bad fields still round-trips normally.
    assert stages._safe_format("{title} - {hook}", title="A", hook="B") == "A - B"


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

        local_dir = settings.sources_dir / "local"
        local_dir.mkdir(parents=True, exist_ok=True)
        source = local_dir / "tmp_vid.mp4"
        media.make_test_video(source, duration=15.0)
        # Simulate a locally-sourced video so handle_render_clip skips the downloader.
        video.yt_video_id = "localvid1"
        source_renamed = local_dir / "localvid1.mp4"
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
