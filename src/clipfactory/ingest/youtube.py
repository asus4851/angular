"""Channel discovery: resolve a channel URL, poll its RSS feed for new videos.

No YouTube Data API key is used (see docs/ARCHITECTURE.md §5): the RSS feed
covers the ~15 most recent uploads, and yt-dlp resolves arbitrary channel URLs
to a stable `UC...` channel id without needing credentials.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from clipfactory.models import Channel, Video, VideoStatus
from clipfactory.schemas import ChannelInfo, VideoInfo

logger = logging.getLogger(__name__)

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

_ATOM_NS = "http://www.w3.org/2005/Atom"
_YT_NS = "http://www.youtube.com/xml/schemas/2015"
_MEDIA_NS = "http://search.yahoo.com/mrss/"
_NAMESPACES = {"atom": _ATOM_NS, "yt": _YT_NS, "media": _MEDIA_NS}

_RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


class IngestError(RuntimeError):
    pass


def resolve_channel(url_or_id: str) -> ChannelInfo:
    """Resolve any channel reference (URL, handle, or bare id) to a ChannelInfo."""
    candidate = url_or_id.strip()
    if _CHANNEL_ID_RE.match(candidate):
        return ChannelInfo(
            yt_channel_id=candidate,
            url=f"https://www.youtube.com/channel/{candidate}",
        )

    url = candidate if candidate.startswith(("http://", "https://")) else f"https://www.youtube.com/{candidate.lstrip('/')}"

    try:
        import yt_dlp

        opts = {
            "extract_flat": True,
            "playlist_items": "0",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt_dlp raises its own DownloadError subclasses
        raise IngestError(f"Failed to resolve channel from {url_or_id!r}: {exc}") from exc

    channel_id = info.get("channel_id") or info.get("id") if isinstance(info, dict) else None
    if not channel_id or not _CHANNEL_ID_RE.match(channel_id):
        raise IngestError(f"Could not determine channel id for {url_or_id!r}")

    title = info.get("channel") or info.get("title") or info.get("uploader") or ""
    channel_url = info.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}"
    return ChannelInfo(yt_channel_id=channel_id, title=title, url=channel_url)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def fetch_recent_videos(yt_channel_id: str) -> list[VideoInfo]:
    """Fetch and parse the channel's RSS feed, newest first."""
    url = _RSS_URL.format(channel_id=yt_channel_id)
    try:
        response = httpx.get(url, timeout=30)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise IngestError(f"Failed to fetch RSS feed for channel {yt_channel_id!r}: {exc}") from exc

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise IngestError(f"Failed to parse RSS feed for channel {yt_channel_id!r}: {exc}") from exc

    videos: list[VideoInfo] = []
    for entry in root.findall("atom:entry", _NAMESPACES):
        video_id_el = entry.find("yt:videoId", _NAMESPACES)
        if video_id_el is None or not video_id_el.text:
            continue
        title_el = entry.find("atom:title", _NAMESPACES)
        link_el = entry.find("atom:link", _NAMESPACES)
        published_el = entry.find("atom:published", _NAMESPACES)

        published_at = None
        if published_el is not None and published_el.text:
            try:
                published_at = _to_naive_utc(datetime.fromisoformat(published_el.text))
            except ValueError:
                published_at = None

        videos.append(
            VideoInfo(
                yt_video_id=video_id_el.text,
                title=title_el.text if title_el is not None and title_el.text else "",
                url=link_el.get("href", "") if link_el is not None else "",
                published_at=published_at,
            )
        )

    videos.sort(key=lambda v: v.published_at or datetime.min, reverse=True)
    return videos


def discover_new_videos(session: Session, channel: Channel) -> list[Video]:
    """Fetch new videos for a channel and persist them (caller commits)."""
    infos = fetch_recent_videos(channel.yt_channel_id)

    existing_ids = {
        row[0]
        for row in session.query(Video.yt_video_id).filter(Video.channel_id == channel.id).all()
    }

    new_videos: list[Video] = []
    for info in infos:
        if info.yt_video_id in existing_ids:
            continue
        if info.published_at is not None and channel.created_at is not None and info.published_at < channel.created_at:
            continue
        video = Video(
            channel_id=channel.id,
            yt_video_id=info.yt_video_id,
            title=info.title,
            duration_sec=info.duration_sec,
            published_at=info.published_at,
            status=VideoStatus.NEW,
        )
        session.add(video)
        new_videos.append(video)

    from clipfactory.models import utcnow

    channel.last_checked_at = utcnow()
    logger.info("Channel %s: discovered %d new video(s)", channel.yt_channel_id, len(new_videos))
    return new_videos
