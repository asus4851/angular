"""Tests for the HTTP API + dashboard (module: api)."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from clipfactory.config import get_settings
from clipfactory.models import (
    Account,
    CandidateStatus,
    Channel,
    ClipCandidate,
    Job,
    JobType,
    Platform,
    Route,
    Video,
    VideoStatus,
)
from clipfactory.schemas import ChannelInfo

SAMPLE_CHANNEL_ID = "UC" + "a" * 22


def _ensure_pipeline_stub(monkeypatch):
    """Provide a minimal `clipfactory.pipeline` if the real one isn't ready yet.

    The pipeline module is being built by a parallel workstream; if it already
    exposes `approve_candidate` and `pipeline.queue.enqueue`, we leave it alone
    so these tests exercise the real implementation.
    """
    has_approve = False
    has_enqueue = False
    try:
        import clipfactory.pipeline as _pl

        has_approve = hasattr(_pl, "approve_candidate")
    except ImportError:
        pass
    try:
        import clipfactory.pipeline.queue as _q

        has_enqueue = hasattr(_q, "enqueue")
    except ImportError:
        pass

    if has_approve and has_enqueue:
        return

    from clipfactory.models import Clip, ClipStatus

    def _enqueue(session, job_type, payload):
        job = Job(type=job_type, payload=payload)
        session.add(job)
        session.flush()
        return job

    def _approve_candidate(session, candidate):
        candidate.status = CandidateStatus.APPROVED
        clip = Clip(candidate_id=candidate.id, status=ClipStatus.QUEUED)
        session.add(clip)
        session.flush()
        _enqueue(session, JobType.RENDER_CLIP, {"candidate_id": candidate.id})
        return clip

    queue_module = types.ModuleType("clipfactory.pipeline.queue")
    queue_module.enqueue = _enqueue

    pipeline_module = types.ModuleType("clipfactory.pipeline")
    pipeline_module.approve_candidate = _approve_candidate
    pipeline_module.queue = queue_module

    monkeypatch.setitem(sys.modules, "clipfactory.pipeline", pipeline_module)
    monkeypatch.setitem(sys.modules, "clipfactory.pipeline.queue", queue_module)


@pytest.fixture()
def app(db, monkeypatch):
    _ensure_pipeline_stub(monkeypatch)
    from clipfactory.api.main import create_app

    return create_app()


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_create_channel_and_list(client, monkeypatch):
    monkeypatch.setattr(
        "clipfactory.ingest.youtube.resolve_channel",
        lambda url: ChannelInfo(yt_channel_id=SAMPLE_CHANNEL_ID, title="Test Channel", url=url),
    )

    res = client.post("/api/channels", json={"url": "https://youtube.com/@test", "min_score": 70})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["yt_channel_id"] == SAMPLE_CHANNEL_ID
    assert body["min_score"] == 70

    res = client.get("/api/channels")
    assert res.status_code == 200
    assert len(res.json()) == 1


def test_create_channel_ingest_error_returns_422(client, monkeypatch):
    from clipfactory.ingest.youtube import IngestError

    def _raise(url):
        raise IngestError("boom")

    monkeypatch.setattr("clipfactory.ingest.youtube.resolve_channel", _raise)

    res = client.post("/api/channels", json={"url": "https://youtube.com/@bad"})
    assert res.status_code == 422


def test_patch_channel_toggle(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C", enabled=True)
        session.add(channel)
        session.flush()
        channel_id = channel.id

    res = client.patch(f"/api/channels/{channel_id}", json={"enabled": False})
    assert res.status_code == 200
    assert res.json()["enabled"] is False


def test_poll_channel_enqueues_job(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        channel_id = channel.id

    res = client.post(f"/api/channels/{channel_id}/poll")
    assert res.status_code == 202, res.text

    with db() as session:
        jobs = session.query(Job).filter(Job.type == JobType.POLL_CHANNEL).all()
        assert len(jobs) == 1
        assert jobs[0].payload == {"channel_id": channel_id}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def test_create_account_hides_credentials(client):
    res = client.post(
        "/api/accounts",
        json={"platform": "youtube", "name": "acc1", "credentials": {"refresh_token": "SUPER-SECRET-TOKEN"}},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["has_credentials"] is True
    assert "SUPER-SECRET-TOKEN" not in res.text
    assert "credentials" not in body

    res = client.get("/api/accounts")
    assert res.status_code == 200
    assert "SUPER-SECRET-TOKEN" not in res.text
    accounts = res.json()
    assert len(accounts) == 1
    assert accounts[0]["has_credentials"] is True


def test_create_account_without_secret_key_returns_422(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "")
    res = client.post(
        "/api/accounts", json={"platform": "local", "name": "acc-nosecret", "credentials": {"a": "b"}}
    )
    assert res.status_code == 422


def test_delete_account(client, db):
    with db() as session:
        account = Account(platform=Platform.LOCAL, name="to-delete")
        session.add(account)
        session.flush()
        account_id = account.id

    res = client.delete(f"/api/accounts/{account_id}")
    assert res.status_code == 204

    res = client.get("/api/accounts")
    assert all(a["id"] != account_id for a in res.json())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_create_route_and_duplicate_conflict(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        account = Account(platform=Platform.LOCAL, name="acc")
        session.add_all([channel, account])
        session.flush()
        channel_id, account_id = channel.id, account.id

    res = client.post("/api/routes", json={"channel_id": channel_id, "account_id": account_id})
    assert res.status_code == 201, res.text

    res = client.post("/api/routes", json={"channel_id": channel_id, "account_id": account_id})
    assert res.status_code == 409

    res = client.get("/api/routes")
    assert len(res.json()) == 1
    assert res.json()[0]["account_name"] == "acc"


# ---------------------------------------------------------------------------
# Candidates / moderation
# ---------------------------------------------------------------------------


def test_approve_candidate_sets_status_and_enqueues_render(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id="vid1", status=VideoStatus.ANALYZED)
        session.add(video)
        session.flush()
        candidate = ClipCandidate(video_id=video.id, start_sec=1.0, end_sec=10.0, score=80, title="Moment")
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id

    res = client.post(f"/api/candidates/{candidate_id}/approve")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "approved"

    with db() as session:
        candidate = session.get(ClipCandidate, candidate_id)
        assert candidate.status == CandidateStatus.APPROVED
        render_jobs = session.query(Job).filter(Job.type == JobType.RENDER_CLIP).all()
        assert len(render_jobs) == 1


def test_reject_candidate(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        video = Video(channel_id=channel.id, yt_video_id="vid2", status=VideoStatus.ANALYZED)
        session.add(video)
        session.flush()
        candidate = ClipCandidate(video_id=video.id, start_sec=1.0, end_sec=10.0, score=40)
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id

    res = client.post(f"/api/candidates/{candidate_id}/reject")
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


def test_media_serves_legit_file(client, settings):
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    (settings.clips_dir / "clip1.mp4").write_bytes(b"fake-mp4")

    res = client.get("/media/clips/file/clip1.mp4")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize(
    "path",
    [
        "/media/clips/file/..%2Fsecret",
        "/media/clips/file/..",
        "/media/clips/file/%2e%2e%2fsecret",
    ],
)
def test_media_path_traversal_rejected(client, settings, path):
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    secret = settings.clips_dir.parent / "secret"
    secret.write_text("do-not-serve-me")

    res = client.get(path)
    assert res.status_code in (400, 404)
    assert res.status_code != 200


def test_media_missing_file_404(client, settings):
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    res = client.get("/media/clips/file/does-not-exist.mp4")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# API key middleware
# ---------------------------------------------------------------------------


def test_api_key_middleware_protects_api_and_dashboard(db, monkeypatch):
    _ensure_pipeline_stub(monkeypatch)
    monkeypatch.setenv("API_KEY", "s3cr3t")
    get_settings.cache_clear()

    from clipfactory.api.main import create_app

    app = create_app()
    client = TestClient(app)

    res = client.get("/api/channels")
    assert res.status_code == 401
    assert res.json()["detail"]

    res = client.get("/api/channels", headers={"X-API-Key": "s3cr3t"})
    assert res.status_code == 200

    # Dashboard pages are now protected too (bare header check, no cookie yet).
    res = client.get("/")
    assert res.status_code == 401
    assert "text/html" in res.headers["content-type"]
    assert "key=" in res.text

    get_settings.cache_clear()


def test_api_key_public_media_file_route_stays_open(db, monkeypatch, settings):
    _ensure_pipeline_stub(monkeypatch)
    monkeypatch.setenv("API_KEY", "s3cr3t")
    get_settings.cache_clear()

    from clipfactory.api.main import create_app

    app = create_app()
    client = TestClient(app)

    # No key at all: 404 (missing file), never 401 -- Instagram's servers fetch
    # this URL directly and can't send our API key.
    res = client.get("/media/clips/file/does-not-exist.mp4")
    assert res.status_code == 404

    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    (settings.clips_dir / "clip1.mp4").write_bytes(b"fake-mp4")
    res = client.get("/media/clips/file/clip1.mp4")
    assert res.status_code == 200

    get_settings.cache_clear()


def test_media_clip_by_id_requires_api_key(db, monkeypatch):
    _ensure_pipeline_stub(monkeypatch)
    monkeypatch.setenv("API_KEY", "s3cr3t")
    get_settings.cache_clear()

    from clipfactory.api.main import create_app

    app = create_app()
    client = TestClient(app)

    # Unlike /media/clips/file/*, the by-id route is a dashboard-facing
    # convenience endpoint and must require the key like everything else.
    res = client.get("/media/clips/1")
    assert res.status_code == 401

    res = client.get("/media/clips/1", headers={"X-API-Key": "s3cr3t"})
    assert res.status_code == 404  # no such clip, but past the auth gate

    get_settings.cache_clear()


def test_api_key_query_param_sets_cookie_for_subsequent_requests(db, monkeypatch):
    _ensure_pipeline_stub(monkeypatch)
    monkeypatch.setenv("API_KEY", "s3cr3t")
    get_settings.cache_clear()

    from clipfactory.api.main import create_app

    app = create_app()
    client = TestClient(app)

    res = client.get("/?key=s3cr3t")
    assert res.status_code == 200
    assert client.cookies.get("cf_key") == "s3cr3t"

    # A fresh client (no cookie) is still rejected.
    other_client = TestClient(app)
    res = other_client.get("/")
    assert res.status_code == 401

    # The original client's cookie jar now authenticates it automatically,
    # exactly like the dashboard's same-origin fetch() calls would be.
    res = client.get("/")
    assert res.status_code == 200
    res = client.get("/api/channels")
    assert res.status_code == 200

    # A wrong key must not set the cookie.
    bad_client = TestClient(app)
    res = bad_client.get("/?key=nope")
    assert res.status_code == 401
    assert bad_client.cookies.get("cf_key") is None

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_shape(client, db):
    with db() as session:
        channel = Channel(yt_channel_id=SAMPLE_CHANNEL_ID, title="C")
        session.add(channel)
        session.flush()
        session.add(Video(channel_id=channel.id, yt_video_id="v1", status=VideoStatus.NEW))

    res = client.get("/api/stats")
    assert res.status_code == 200
    body = res.json()
    for key in ("videos", "candidates", "clips", "posts", "jobs", "recent_failed_jobs"):
        assert key in body
    assert body["videos"].get("new") == 1


# ---------------------------------------------------------------------------
# Dashboard pages render
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/", "/channels", "/accounts", "/routes", "/moderation", "/posts"]
)
def test_dashboard_pages_render(client, path):
    res = client.get(path)
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
