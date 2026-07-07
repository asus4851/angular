"""Tests for wave-3 API features: video import by URL, channel catalog/import,
re-analyze, and ad-hoc publish targets (module: api)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clipfactory.models import (
    Account,
    Channel,
    Clip,
    ClipCandidate,
    ClipStatus,
    Job,
    JobType,
    Platform,
    Post,
    Video,
    VideoStatus,
)
from clipfactory.schemas import ChannelInfo, VideoInfo

SAMPLE_CHANNEL_ID = "UC" + "b" * 22
SAMPLE_VIDEO_ID = "abcdefghijk"
OTHER_VIDEO_ID = "zyxwvutsrqp"


@pytest.fixture()
def app(db, monkeypatch):
    from tests.test_api import _ensure_pipeline_stub

    _ensure_pipeline_stub(monkeypatch)
    from clipfactory.api.main import create_app

    return create_app()


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Import video by URL
# ---------------------------------------------------------------------------


def test_import_video_by_url_creates_video_and_disabled_channel(client, db, monkeypatch):
    video_info = VideoInfo(yt_video_id=SAMPLE_VIDEO_ID, title="My Video", duration_sec=123.0)
    channel_info = ChannelInfo(yt_channel_id=SAMPLE_CHANNEL_ID, title="My Channel", url="https://youtube.com/c")
    monkeypatch.setattr(
        "clipfactory.ingest.youtube.fetch_video_info", lambda url: (video_info, channel_info)
    )

    res = client.post(
        "/api/videos",
        json={"url": f"https://youtube.com/watch?v={SAMPLE_VIDEO_ID}", "max_clips": 5, "min_score": 70},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["title"] == "My Video"
    assert body["duration_sec"] == 123.0
    assert body["channel_title"] == "My Channel"
    assert body["already_imported"] is False

    with db() as session:
        channel = session.query(Channel).filter(Channel.yt_channel_id == SAMPLE_CHANNEL_ID).one()
        assert channel.enabled is False
        video = session.query(Video).filter(Video.yt_video_id == SAMPLE_VIDEO_ID).one()
        assert video.status == VideoStatus.NEW
        assert video.channel_id == channel.id

        jobs = session.query(Job).filter(Job.type == JobType.FETCH_TRANSCRIPT).all()
        assert len(jobs) == 1
        assert jobs[0].payload["video_id"] == video.id
        assert jobs[0].payload["analysis_overrides"] == {"max_clips": 5, "min_score": 70}

    # Second call with the same video -> already_imported, no duplicate.
    res2 = client.post("/api/videos", json={"url": f"https://youtube.com/watch?v={SAMPLE_VIDEO_ID}"})
    assert res2.status_code == 200, res2.text
    assert res2.json()["already_imported"] is True

    with db() as session:
        assert session.query(Video).filter(Video.yt_video_id == SAMPLE_VIDEO_ID).count() == 1
        assert session.query(Job).filter(Job.type == JobType.FETCH_TRANSCRIPT).count() == 1


def test_import_video_ingest_error_returns_422(client, monkeypatch):
    from clipfactory.ingest.youtube import IngestError

    def _raise(url):
        raise IngestError("boom")

    monkeypatch.setattr("clipfactory.ingest.youtube.fetch_video_info", _raise)

    res = client.post("/api/videos", json={"url": "https://youtube.com/watch?v=bad"})
    assert res.status_code == 422


def test_import_video_reuses_existing_channel(client, db, monkeypatch):
    with db() as session:
        session.add(Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="Existing", enabled=True))

    video_info = VideoInfo(yt_video_id=SAMPLE_VIDEO_ID, title="Vid")
    channel_info = ChannelInfo(yt_channel_id=SAMPLE_CHANNEL_ID, title="Existing", url="")
    monkeypatch.setattr(
        "clipfactory.ingest.youtube.fetch_video_info", lambda url: (video_info, channel_info)
    )

    res = client.post("/api/videos", json={"url": "https://youtube.com/watch?v=x"})
    assert res.status_code == 201, res.text

    with db() as session:
        assert session.query(Channel).filter(Channel.yt_channel_id == SAMPLE_CHANNEL_ID).count() == 1
        channel = session.query(Channel).filter(Channel.yt_channel_id == SAMPLE_CHANNEL_ID).one()
        assert channel.enabled is True  # untouched, not re-created disabled


# ---------------------------------------------------------------------------
# Channel catalog
# ---------------------------------------------------------------------------


def test_channel_catalog_annotates_imported_flag(client, db, monkeypatch):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        channel_id = channel.id
        session.add(Video(channel_id=channel_id, yt_video_id=SAMPLE_VIDEO_ID, title="Already here"))

    infos = [
        VideoInfo(yt_video_id=SAMPLE_VIDEO_ID, title="Already here", duration_sec=60.0),
        VideoInfo(yt_video_id=OTHER_VIDEO_ID, title="New one", duration_sec=90.0),
    ]
    monkeypatch.setattr(
        "clipfactory.ingest.youtube.list_channel_videos", lambda yt_channel_id, limit=30: infos
    )

    res = client.get(f"/api/channels/{channel_id}/catalog")
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body) == 2
    by_id = {item["yt_video_id"]: item for item in body}
    assert by_id[SAMPLE_VIDEO_ID]["imported"] is True
    assert by_id[SAMPLE_VIDEO_ID]["video_id"] is not None
    assert by_id[OTHER_VIDEO_ID]["imported"] is False
    assert by_id[OTHER_VIDEO_ID]["video_id"] is None


def test_channel_catalog_ingest_error_returns_502(client, db, monkeypatch):
    from clipfactory.ingest.youtube import IngestError

    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        channel_id = channel.id

    def _raise(yt_channel_id, limit=30):
        raise IngestError("network down")

    monkeypatch.setattr("clipfactory.ingest.youtube.list_channel_videos", _raise)

    res = client.get(f"/api/channels/{channel_id}/catalog")
    assert res.status_code == 502


# ---------------------------------------------------------------------------
# Channel import
# ---------------------------------------------------------------------------


def test_channel_import_creates_video_and_job(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        channel_id = channel.id

    res = client.post(
        f"/api/channels/{channel_id}/import",
        json={"yt_video_id": SAMPLE_VIDEO_ID, "title": "Imported title", "max_clips": 2},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["already_imported"] is False

    with db() as session:
        video = session.query(Video).filter(Video.yt_video_id == SAMPLE_VIDEO_ID).one()
        assert video.status == VideoStatus.NEW
        assert video.title == "Imported title"
        jobs = session.query(Job).filter(Job.type == JobType.FETCH_TRANSCRIPT).all()
        assert len(jobs) == 1
        assert jobs[0].payload["analysis_overrides"] == {"max_clips": 2}


def test_channel_import_skipped_video_resets_to_new(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        channel_id = channel.id
        session.add(
            Video(
                channel_id=channel_id,
                yt_video_id=SAMPLE_VIDEO_ID,
                status=VideoStatus.SKIPPED,
                error="no transcript",
            )
        )

    res = client.post(f"/api/channels/{channel_id}/import", json={"yt_video_id": SAMPLE_VIDEO_ID})
    assert res.status_code == 201, res.text
    assert res.json()["already_imported"] is False

    with db() as session:
        video = session.query(Video).filter(Video.yt_video_id == SAMPLE_VIDEO_ID).one()
        assert video.status == VideoStatus.NEW
        assert video.error == ""
        assert session.query(Job).filter(Job.type == JobType.FETCH_TRANSCRIPT).count() == 1


def test_channel_import_already_imported_no_duplicate_job(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        channel_id = channel.id
        session.add(Video(channel_id=channel_id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.ANALYZED))

    res = client.post(f"/api/channels/{channel_id}/import", json={"yt_video_id": SAMPLE_VIDEO_ID})
    assert res.status_code == 200, res.text
    assert res.json()["already_imported"] is True

    with db() as session:
        assert session.query(Job).filter(Job.type == JobType.FETCH_TRANSCRIPT).count() == 0


# ---------------------------------------------------------------------------
# Re-analyze
# ---------------------------------------------------------------------------


def test_reanalyze_transcribed_video_enqueues_job(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.TRANSCRIBED)
        session.add(video)
        session.flush()
        video_id = video.id

    res = client.post(f"/api/videos/{video_id}/analyze", json={"max_clips": 4, "language": "uk"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "transcribed"

    with db() as session:
        jobs = session.query(Job).filter(Job.type == JobType.ANALYZE_VIDEO).all()
        assert len(jobs) == 1
        assert jobs[0].payload["video_id"] == video_id
        assert jobs[0].payload["overrides"] == {"max_clips": 4, "language": "uk"}


def test_reanalyze_new_video_returns_409(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.NEW)
        session.add(video)
        session.flush()
        video_id = video.id

    res = client.post(f"/api/videos/{video_id}/analyze", json={})
    assert res.status_code == 409


def test_reanalyze_analyzed_video_resets_status_and_enqueues(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.ANALYZED)
        session.add(video)
        session.flush()
        video_id = video.id
        session.add(ClipCandidate(video_id=video_id, start_sec=0, end_sec=5, score=50, title="existing"))

    res = client.post(f"/api/videos/{video_id}/analyze", json={"min_score": 80})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "transcribed"
    assert body["candidate_count"] == 1  # existing candidates stay

    with db() as session:
        video = session.get(Video, video_id)
        assert video.status == VideoStatus.TRANSCRIBED
        # Existing candidate is untouched.
        assert session.query(ClipCandidate).filter(ClipCandidate.video_id == video_id).count() == 1
        jobs = session.query(Job).filter(Job.type == JobType.ANALYZE_VIDEO).all()
        assert len(jobs) == 1
        assert jobs[0].payload["overrides"] == {"min_score": 80}


# ---------------------------------------------------------------------------
# Ad-hoc publish targets
# ---------------------------------------------------------------------------


def test_approve_with_account_ids_creates_adhoc_posts(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        account = Account(platform=Platform.LOCAL, name="adhoc-acc")
        session.add_all([channel, account])
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.ANALYZED)
        session.add(video)
        session.flush()
        candidate = ClipCandidate(video_id=video.id, start_sec=1.0, end_sec=10.0, score=90, title="Moment")
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
        account_id = account.id

    res = client.post(f"/api/candidates/{candidate_id}/approve", json={"account_ids": [account_id]})
    assert res.status_code == 200, res.text

    with db() as session:
        posts = session.query(Post).filter(Post.account_id == account_id).all()
        assert len(posts) == 1
        assert posts[0].route_id is None
        assert posts[0].account_id == account_id


def test_approve_without_body_still_works(client, db):
    """Existing callers that POST with no JSON body must keep working."""
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.ANALYZED)
        session.add(video)
        session.flush()
        candidate = ClipCandidate(video_id=video.id, start_sec=1.0, end_sec=10.0, score=90, title="Moment")
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id

    res = client.post(f"/api/candidates/{candidate_id}/approve")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "approved"


def test_clips_list_and_publish_rendered_clip(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        account = Account(platform=Platform.LOCAL, name="pub-acc")
        session.add_all([channel, account])
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.ANALYZED)
        session.add(video)
        session.flush()
        candidate = ClipCandidate(
            video_id=video.id, start_sec=1.0, end_sec=10.0, score=90, title="Rendered moment"
        )
        session.add(candidate)
        session.flush()
        clip = Clip(
            candidate_id=candidate.id,
            status=ClipStatus.RENDERED,
            file_path="/tmp/does-not-need-to-exist.mp4",
            duration_sec=9.0,
        )
        session.add(clip)
        session.flush()
        clip_id = clip.id
        account_id = account.id

    res = client.get("/api/clips")
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body) == 1
    assert body[0]["id"] == clip_id
    assert body[0]["candidate_title"] == "Rendered moment"
    assert body[0]["status"] == "rendered"
    assert body[0]["has_posts"] is False

    res = client.post(f"/api/clips/{clip_id}/publish", json={"account_ids": [account_id]})
    assert res.status_code == 200, res.text
    assert len(res.json()["post_ids"]) == 1

    with db() as session:
        posts = session.query(Post).filter(Post.clip_id == clip_id).all()
        assert len(posts) == 1
        assert posts[0].account_id == account_id
        jobs = session.query(Job).filter(Job.type == JobType.PUBLISH_POST).all()
        assert len(jobs) == 1


def test_publish_failed_clip_returns_409(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        account = Account(platform=Platform.LOCAL, name="acc2")
        session.add_all([channel, account])
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.ANALYZED)
        session.add(video)
        session.flush()
        candidate = ClipCandidate(video_id=video.id, start_sec=1.0, end_sec=10.0, score=90)
        session.add(candidate)
        session.flush()
        clip = Clip(candidate_id=candidate.id, status=ClipStatus.FAILED)
        session.add(clip)
        session.flush()
        clip_id = clip.id
        account_id = account.id

    res = client.post(f"/api/clips/{clip_id}/publish", json={"account_ids": [account_id]})
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# Dashboard pages render
# ---------------------------------------------------------------------------


def test_videos_page_renders(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        session.add(Video(channel_id=channel.id, yt_video_id=SAMPLE_VIDEO_ID, status=VideoStatus.NEW))

    res = client.get("/videos")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_channel_videos_page_renders(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        channel_id = channel.id

    res = client.get(f"/channels/{channel_id}/videos")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_channel_videos_page_404_for_missing_channel(client):
    res = client.get("/channels/9999/videos")
    assert res.status_code == 404
