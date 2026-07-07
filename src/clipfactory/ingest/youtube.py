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
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy.orm import Session

from clipfactory.models import Channel, Video, VideoStatus
from clipfactory.schemas import ChannelInfo, VideoInfo

logger = logging.getLogger(__name__)

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

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


def _ydl_opts(**overrides: object) -> dict:
    """Base options for a quiet, no-download YoutubeDL instance."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    opts.update(overrides)
    return opts


def parse_video_id(url_or_id: str) -> str | None:
    """Extract an 11-char YouTube video id from any known URL form, or a bare id."""
    candidate = url_or_id.strip()
    if _VIDEO_ID_RE.match(candidate):
        return candidate

    parsed = urlparse(candidate if "//" in candidate else f"//{candidate}")
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if "youtu.be" in host:
        vid = path.strip("/").split("/")[0]
        return vid if _VIDEO_ID_RE.match(vid) else None

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        if path == "/watch":
            vid = parse_qs(parsed.query).get("v", [None])[0]
            return vid if vid and _VIDEO_ID_RE.match(vid) else None
        for prefix in ("/shorts/", "/live/", "/embed/"):
            if path.startswith(prefix):
                vid = path[len(prefix):].strip("/").split("/")[0]
                return vid if _VIDEO_ID_RE.match(vid) else None

    return None


def fetch_video_info(url_or_id: str) -> tuple[VideoInfo, ChannelInfo]:
    """Resolve a single video (any URL form or bare id) plus its owning channel."""
    candidate = url_or_id.strip()
    video_id = parse_video_id(candidate)
    if video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
    else:
        url = candidate

    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt_dlp raises its own DownloadError subclasses
        raise IngestError(f"Failed to fetch video info from {url_or_id!r}: {exc}") from exc

    if not isinstance(info, dict):
        raise IngestError(f"Failed to fetch video info from {url_or_id!r}: no data returned")

    channel_id = info.get("channel_id")
    if not channel_id:
        raise IngestError(f"Could not determine channel id for video {url_or_id!r}")

    published_at = None
    timestamp = info.get("timestamp")
    if timestamp is not None:
        try:
            published_at = _to_naive_utc(datetime.fromtimestamp(timestamp, tz=UTC))
        except (OverflowError, OSError, ValueError):
            published_at = None
    else:
        upload_date = info.get("upload_date")
        if upload_date:
            try:
                published_at = datetime.strptime(upload_date, "%Y%m%d")
            except ValueError:
                published_at = None

    video_info = VideoInfo(
        yt_video_id=info.get("id") or video_id or "",
        title=info.get("title") or "",
        url=info.get("webpage_url") or url,
        duration_sec=info.get("duration"),
        published_at=published_at,
    )
    channel_info = ChannelInfo(
        yt_channel_id=channel_id,
        title=info.get("channel") or info.get("uploader") or "",
        url=info.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}",
    )
    return video_info, channel_info


def list_channel_videos(yt_channel_id: str, limit: int = 30) -> list[VideoInfo]:
    """List up to `limit` videos on a channel's Videos tab (flat, no per-video metadata)."""
    url = f"https://www.youtube.com/channel/{yt_channel_id}/videos"
    try:
        import yt_dlp

        opts = _ydl_opts(extract_flat="in_playlist", playlistend=limit)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt_dlp raises its own DownloadError subclasses
        raise IngestError(f"Failed to list videos for channel {yt_channel_id!r}: {exc}") from exc

    entries = (info or {}).get("entries") or []
    videos: list[VideoInfo] = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        video_id = entry["id"]
        videos.append(
            VideoInfo(
                yt_video_id=video_id,
                title=entry.get("title") or "",
                url=entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                duration_sec=entry.get("duration"),
                published_at=None,
            )
        )
    return videos


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
