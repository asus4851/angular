"""YouTube Shorts publisher (module: publish).

Uses the YouTube Data API v3 (`videos.insert`) via a resumable upload. Heavy
Google client libraries are imported lazily inside `publish()` so that
importing this module (or `clipfactory.publish`) never requires them unless a
YouTube account is actually used.
"""

from __future__ import annotations

import logging
from pathlib import Path

from clipfactory.publish.base import (
    PublishError,
    PublishResult,
    compose_description,
    dry_run_guard,
    redact,
    register,
)
from clipfactory.schemas import PostMetadata

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = ("client_id", "client_secret", "refresh_token")


@register("youtube")
class YouTubeShortsPublisher:
    platform = "youtube"

    def publish(self, clip_path: Path, metadata: PostMetadata, credentials: dict) -> PublishResult:
        dry = dry_run_guard(self.platform, metadata)
        if dry is not None:
            return dry

        missing = [key for key in _REQUIRED_KEYS if not credentials.get(key)]
        if missing:
            raise PublishError(
                f"YouTube credentials missing required keys: {', '.join(missing)}", retryable=False
            )

        # Lazy: keep google-api-python-client / google-auth out of the import path
        # for accounts/tests that never touch YouTube.
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        creds = Credentials(
            None,
            refresh_token=credentials["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
        )
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

        body = {
            "snippet": {
                "title": metadata.title[:100],
                "description": compose_description(metadata),
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": credentials.get("privacy_status", "public"),
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(clip_path), chunksize=-1, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        try:
            response = None
            while response is None:
                _status, response = request.next_chunk()
            video_id = response["id"]
        except HttpError as exc:
            status_code = exc.resp.status if exc.resp is not None else None
            # Non-retryable for any 4xx except 408 (timeout) and 429 (rate limit),
            # which — like 5xx and network errors — are transient and worth a retry.
            non_retryable = status_code is not None and 400 <= status_code < 500 and status_code not in (408, 429)
            retryable = not non_retryable
            secrets = [credentials.get("refresh_token", ""), credentials.get("client_secret", "")]
            raise PublishError(redact(f"YouTube upload failed: {exc}", secrets), retryable=retryable) from exc

        logger.info("YouTubeShortsPublisher: uploaded video %s", video_id)
        return PublishResult(external_id=video_id, external_url=f"https://youtube.com/shorts/{video_id}")
