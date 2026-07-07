"""Tests for clipfactory.ingest.youtube."""

from __future__ import annotations

from datetime import datetime

import pytest

from clipfactory.ingest.youtube import (
    IngestError,
    discover_new_videos,
    fetch_recent_videos,
    fetch_video_info,
    list_channel_videos,
    parse_video_id,
    resolve_channel,
)
from clipfactory.models import Channel, Video, VideoStatus

SAMPLE_VIDEO_ID = "dQw4w9WgXcQ"

SAMPLE_CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <id>yt:channel:UC_x5XG1OV2P6uZZ5FSM9Ttw</id>
  <title>Sample Channel</title>
  <entry>
    <id>yt:video:vid_newer</id>
    <yt:videoId>vid_newer</yt:videoId>
    <yt:channelId>UC_x5XG1OV2P6uZZ5FSM9Ttw</yt:channelId>
    <title>Newer video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=vid_newer"/>
    <published>2026-07-01T12:00:00+00:00</published>
    <media:group>
      <media:title>Newer video</media:title>
    </media:group>
  </entry>
  <entry>
    <id>yt:video:vid_older</id>
    <yt:videoId>vid_older</yt:videoId>
    <yt:channelId>UC_x5XG1OV2P6uZZ5FSM9Ttw</yt:channelId>
    <title>Older video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=vid_older"/>
    <published>2026-06-01T08:30:00+00:00</published>
    <media:group>
      <media:title>Older video</media:title>
    </media:group>
  </entry>
</feed>
"""


def test_resolve_channel_bare_id_no_network():
    info = resolve_channel(SAMPLE_CHANNEL_ID)
    assert info.yt_channel_id == SAMPLE_CHANNEL_ID
    assert info.url == f"https://www.youtube.com/channel/{SAMPLE_CHANNEL_ID}"


def test_resolve_channel_bare_id_strips_whitespace():
    info = resolve_channel(f"  {SAMPLE_CHANNEL_ID}  ")
    assert info.yt_channel_id == SAMPLE_CHANNEL_ID


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_fetch_recent_videos_parses_rss(monkeypatch):
    def fake_get(url, timeout=None):
        assert SAMPLE_CHANNEL_ID in url
        return _FakeResponse(RSS_SAMPLE)

    monkeypatch.setattr("clipfactory.ingest.youtube.httpx.get", fake_get)

    videos = fetch_recent_videos(SAMPLE_CHANNEL_ID)

    assert [v.yt_video_id for v in videos] == ["vid_newer", "vid_older"]
    assert videos[0].title == "Newer video"
    assert videos[0].url == "https://www.youtube.com/watch?v=vid_newer"
    assert videos[0].published_at == datetime(2026, 7, 1, 12, 0, 0)
    assert videos[0].published_at.tzinfo is None


def test_fetch_recent_videos_raises_ingest_error_on_http_failure(monkeypatch):
    import httpx

    def fake_get(url, timeout=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("clipfactory.ingest.youtube.httpx.get", fake_get)

    with pytest.raises(IngestError):
        fetch_recent_videos(SAMPLE_CHANNEL_ID)


def test_discover_new_videos_skips_existing_and_old(monkeypatch, db):
    from clipfactory.schemas import VideoInfo

    with db() as session:
        channel = Channel(
            yt_channel_id=SAMPLE_CHANNEL_ID,
            title="Sample Channel",
            created_at=datetime(2026, 6, 15, 0, 0, 0),
        )
        session.add(channel)
        session.flush()

        existing = Video(
            channel_id=channel.id,
            yt_video_id="vid_existing",
            status=VideoStatus.NEW,
        )
        session.add(existing)
        session.flush()

        fake_infos = [
            VideoInfo(
                yt_video_id="vid_existing",
                title="Already known",
                published_at=datetime(2026, 7, 1),
            ),
            VideoInfo(
                yt_video_id="vid_too_old",
                title="Before channel was added",
                published_at=datetime(2026, 6, 1),
            ),
            VideoInfo(
                yt_video_id="vid_new",
                title="Brand new",
                published_at=datetime(2026, 7, 2),
            ),
            VideoInfo(
                yt_video_id="vid_no_date",
                title="No publish date",
                published_at=None,
            ),
        ]
        monkeypatch.setattr(
            "clipfactory.ingest.youtube.fetch_recent_videos",
            lambda channel_id: fake_infos,
        )

        new_videos = discover_new_videos(session, channel)
        session.commit()

        assert {v.yt_video_id for v in new_videos} == {"vid_new", "vid_no_date"}
        assert channel.last_checked_at is not None

    with db() as session:
        all_ids = {row[0] for row in session.query(Video.yt_video_id).all()}
        assert all_ids == {"vid_existing", "vid_new", "vid_no_date"}


# --- parse_video_id -------------------------------------------------------


@pytest.mark.parametrize(
    "url_or_id",
    [
        f"https://www.youtube.com/watch?v={SAMPLE_VIDEO_ID}",
        f"https://www.youtube.com/watch?v={SAMPLE_VIDEO_ID}&t=30s",
        f"https://youtu.be/{SAMPLE_VIDEO_ID}",
        f"https://www.youtube.com/shorts/{SAMPLE_VIDEO_ID}",
        f"https://www.youtube.com/live/{SAMPLE_VIDEO_ID}",
        f"https://www.youtube.com/embed/{SAMPLE_VIDEO_ID}",
        SAMPLE_VIDEO_ID,
    ],
)
def test_parse_video_id_accepts_known_forms(url_or_id):
    assert parse_video_id(url_or_id) == SAMPLE_VIDEO_ID


@pytest.mark.parametrize(
    "garbage",
    [
        "not a url",
        "https://example.com/watch?v=short",
        "https://www.youtube.com/watch",
        "",
        "too-long-to-be-a-video-id",
    ],
)
def test_parse_video_id_returns_none_for_garbage(garbage):
    assert parse_video_id(garbage) is None


# --- fetch_video_info -------------------------------------------------------


class _FakeYoutubeDL:
    """Minimal stand-in for yt_dlp.YoutubeDL used across ingest tests."""

    captured_opts: list[dict] | None = None
    result: dict | None = None
    exc: Exception | None = None

    def __init__(self, opts):
        if self.__class__.captured_opts is not None:
            self.__class__.captured_opts.append(opts)
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        if self.__class__.exc is not None:
            raise self.__class__.exc
        return self.__class__.result


def _install_fake_ydl(monkeypatch, result=None, exc=None, captured_opts=None):
    fake_cls = type(
        "_FakeYoutubeDLInstance",
        (_FakeYoutubeDL,),
        {"result": result, "exc": exc, "captured_opts": captured_opts},
    )
    monkeypatch.setattr("yt_dlp.YoutubeDL", fake_cls)
    return fake_cls


def test_fetch_video_info_via_upload_date(monkeypatch):
    info = {
        "id": SAMPLE_VIDEO_ID,
        "title": "Test Video",
        "webpage_url": f"https://www.youtube.com/watch?v={SAMPLE_VIDEO_ID}",
        "duration": 212.0,
        "channel_id": SAMPLE_CHANNEL_ID,
        "channel": "Sample Channel",
        "channel_url": f"https://www.youtube.com/channel/{SAMPLE_CHANNEL_ID}",
        "upload_date": "20260615",
    }
    _install_fake_ydl(monkeypatch, result=info)

    video, channel = fetch_video_info(f"https://www.youtube.com/watch?v={SAMPLE_VIDEO_ID}")

    assert video.yt_video_id == SAMPLE_VIDEO_ID
    assert video.title == "Test Video"
    assert video.duration_sec == 212.0
    assert video.published_at == datetime(2026, 6, 15)
    assert channel.yt_channel_id == SAMPLE_CHANNEL_ID
    assert channel.title == "Sample Channel"
    assert channel.url == f"https://www.youtube.com/channel/{SAMPLE_CHANNEL_ID}"


def test_fetch_video_info_via_timestamp(monkeypatch):
    info = {
        "id": SAMPLE_VIDEO_ID,
        "title": "Test Video",
        "webpage_url": f"https://www.youtube.com/watch?v={SAMPLE_VIDEO_ID}",
        "duration": 212.0,
        "channel_id": SAMPLE_CHANNEL_ID,
        "channel": "Sample Channel",
        "timestamp": 1750000000,  # 2025-06-15T14:13:20Z
    }
    _install_fake_ydl(monkeypatch, result=info)

    video, channel = fetch_video_info(SAMPLE_VIDEO_ID)

    assert video.published_at is not None
    assert video.published_at.tzinfo is None
    assert video.published_at == datetime.utcfromtimestamp(1750000000)
    assert channel.yt_channel_id == SAMPLE_CHANNEL_ID


def test_fetch_video_info_missing_channel_id_raises(monkeypatch):
    info = {
        "id": SAMPLE_VIDEO_ID,
        "title": "Test Video",
    }
    _install_fake_ydl(monkeypatch, result=info)

    with pytest.raises(IngestError):
        fetch_video_info(SAMPLE_VIDEO_ID)


def test_fetch_video_info_wraps_extraction_failure(monkeypatch):
    _install_fake_ydl(monkeypatch, exc=RuntimeError("network down"))

    with pytest.raises(IngestError):
        fetch_video_info(SAMPLE_VIDEO_ID)


# --- list_channel_videos -----------------------------------------------------


def test_list_channel_videos_filters_missing_id_and_respects_limit(monkeypatch):
    entries = [
        {"id": "vid_one", "title": "One", "duration": 10.0, "url": "https://www.youtube.com/watch?v=vid_one"},
        {"id": None, "title": "No id, should be skipped"},
        {"title": "Missing id key entirely"},
        {"id": "vid_two", "title": "Two"},
    ]
    captured_opts: list[dict] = []
    _install_fake_ydl(monkeypatch, result={"entries": entries}, captured_opts=captured_opts)

    videos = list_channel_videos(SAMPLE_CHANNEL_ID, limit=7)

    assert [v.yt_video_id for v in videos] == ["vid_one", "vid_two"]
    assert videos[0].title == "One"
    assert videos[0].duration_sec == 10.0
    assert videos[0].published_at is None
    assert videos[1].url == "https://www.youtube.com/watch?v=vid_two"

    assert len(captured_opts) == 1
    assert captured_opts[0]["playlistend"] == 7
    assert captured_opts[0]["extract_flat"] == "in_playlist"


def test_list_channel_videos_wraps_extraction_failure(monkeypatch):
    _install_fake_ydl(monkeypatch, exc=RuntimeError("boom"))

    with pytest.raises(IngestError):
        list_channel_videos(SAMPLE_CHANNEL_ID)
