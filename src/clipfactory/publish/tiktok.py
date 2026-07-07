"""TikTok publisher (module: publish).

Uses TikTok's Content Posting API `FILE_UPLOAD` source: initialize an upload
(single chunk for small files, otherwise chunked PUTs with `Content-Range`),
then push the video bytes to the returned `upload_url`.

Note: unaudited TikTok developer apps can only post with `privacy_level`
`SELF_ONLY` (private, visible only to the posting account) — see
docs/PUBLISHERS.md for how to request the Content Posting API audit needed
to unlock public posting.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from clipfactory.publish.base import (
    PublishError,
    PublishResult,
    dry_run_guard,
    format_hashtags,
    raise_for_http_status,
    redact,
    register,
)
from clipfactory.schemas import PostMetadata

logger = logging.getLogger(__name__)

_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_SINGLE_CHUNK_MAX = 64 * 1024 * 1024  # files at or under this size are sent as one chunk
_CHUNK_SIZE = 10 * 1024 * 1024


@register("tiktok")
class TikTokPublisher:
    platform = "tiktok"

    def publish(self, clip_path: Path, metadata: PostMetadata, credentials: dict) -> PublishResult:
        dry = dry_run_guard(self.platform, metadata)
        if dry is not None:
            return dry

        access_token = credentials.get("access_token")
        if not access_token:
            raise PublishError("TikTok credentials missing required key: access_token", retryable=False)

        size = clip_path.stat().st_size
        chunk_size, total_chunks = self._chunking(size)
        title = self._compose_title(metadata)
        headers = {"Authorization": f"Bearer {access_token}"}

        payload = {
            "post_info": {
                "title": title,
                "privacy_level": credentials.get("privacy_level", "SELF_ONLY"),
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunks,
            },
        }

        try:
            with httpx.Client(timeout=60) as client, clip_path.open("rb") as fh:
                init_resp = client.post(_INIT_URL, json=payload, headers=headers)
                raise_for_http_status(init_resp, "TikTok", secrets=[access_token])
                init_data = init_resp.json()["data"]
                publish_id = init_data["publish_id"]
                upload_url = init_data["upload_url"]

                for chunk_index in range(total_chunks):
                    start = chunk_index * chunk_size
                    # The final chunk absorbs whatever remainder floor division left
                    # behind, per TikTok's FILE_UPLOAD chunking rules (see `_chunking`).
                    end = size - 1 if chunk_index == total_chunks - 1 else start + chunk_size - 1
                    fh.seek(start)
                    chunk = fh.read(end - start + 1)
                    put_resp = client.put(
                        upload_url,
                        content=chunk,
                        headers={
                            "Content-Range": f"bytes {start}-{end}/{size}",
                            "Content-Type": "video/mp4",
                        },
                    )
                    raise_for_http_status(put_resp, "TikTok", secrets=[access_token])
        except httpx.HTTPError as exc:
            raise PublishError(redact(f"TikTok publish failed: {exc}", [access_token]), retryable=True) from exc

        logger.info("TikTokPublisher: uploaded video, publish_id=%s", publish_id)
        return PublishResult(external_id=publish_id, external_url="")

    @staticmethod
    def _chunking(size: int) -> tuple[int, int]:
        """TikTok FILE_UPLOAD chunking: non-final chunks are exactly `chunk_size`,
        the final chunk absorbs the remainder (so it's always >= `chunk_size`,
        comfortably over TikTok's 5MB-minimum-final-chunk rule for multi-chunk
        uploads)."""
        if size <= _SINGLE_CHUNK_MAX:
            return size, 1
        total_chunks = max(size // _CHUNK_SIZE, 1)
        return _CHUNK_SIZE, total_chunks

    @staticmethod
    def _compose_title(metadata: PostMetadata) -> str:
        tags = format_hashtags(metadata.hashtags)
        title = f"{metadata.title} {tags}".strip() if tags else metadata.title
        return title[:2200]
