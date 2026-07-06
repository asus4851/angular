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
    raise_for_http_status,
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

        video_bytes = clip_path.read_bytes()
        size = len(video_bytes)
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
            with httpx.Client(timeout=60) as client:
                init_resp = client.post(_INIT_URL, json=payload, headers=headers)
                raise_for_http_status(init_resp, "TikTok")
                init_data = init_resp.json()["data"]
                publish_id = init_data["publish_id"]
                upload_url = init_data["upload_url"]

                for chunk_index in range(total_chunks):
                    start = chunk_index * chunk_size
                    end = min(start + chunk_size, size) - 1
                    chunk = video_bytes[start : end + 1]
                    put_resp = client.put(
                        upload_url,
                        content=chunk,
                        headers={
                            "Content-Range": f"bytes {start}-{end}/{size}",
                            "Content-Type": "video/mp4",
                        },
                    )
                    raise_for_http_status(put_resp, "TikTok")
        except httpx.HTTPError as exc:
            raise PublishError(f"TikTok publish failed: {exc}", retryable=True) from exc

        logger.info("TikTokPublisher: uploaded video, publish_id=%s", publish_id)
        return PublishResult(external_id=publish_id, external_url="")

    @staticmethod
    def _chunking(size: int) -> tuple[int, int]:
        if size <= _SINGLE_CHUNK_MAX:
            return size, 1
        total_chunks = (size + _CHUNK_SIZE - 1) // _CHUNK_SIZE
        return _CHUNK_SIZE, total_chunks

    @staticmethod
    def _compose_title(metadata: PostMetadata) -> str:
        tags = " ".join(f"#{tag.lstrip('#')}" for tag in metadata.hashtags if tag.strip())
        title = f"{metadata.title} {tags}".strip() if tags else metadata.title
        return title[:2200]
