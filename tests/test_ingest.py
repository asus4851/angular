"""Tests for clipfactory.ingest.youtube."""

from __future__ import annotations

from datetime import datetime

import pytest

from clipfactory.ingest.youtube import (
    IngestError,
    discover_new_videos,
    fetch_recent_videos,
    resolve_channel,
)
from clipfactory.models import Channel, Video, VideoStatus

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
