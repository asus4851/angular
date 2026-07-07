"""Instagram Reels publisher (module: publish).

Uses the Meta Graph API's container flow: create a media container pointing
at a *publicly reachable* video URL, poll until Instagram has finished
downloading/processing it, then publish the container.

Contract with the API module: Instagram's servers fetch the video themselves
(no direct upload endpoint), so the clip must be reachable at
`{PUBLIC_BASE_URL}/media/clips/file/{clip_path.name}` for as long as this
call runs. Serving clip files under that path is the responsibility of the
`clipfactory.api.routers` media router (see docs/PUBLISHERS.md).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from clipfactory.config import get_settings
from clipfactory.publish.base import (
    PublishError,
    PublishResult,
    compose_description,
    dry_run_guard,
    raise_for_http_status,
    redact,
    register,
)
from clipfactory.schemas import PostMetadata

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.facebook.com/v21.0"
_POLL_INTERVAL_SEC = 5
_POLL_TIMEOUT_SEC = 300
_REQUIRED_KEYS = ("access_token", "ig_user_id")


@register("instagram")
class InstagramReelsPublisher:
    platform = "instagram"

    def publish(self, clip_path: Path, metadata: PostMetadata, credentials: dict) -> PublishResult:
        dry = dry_run_guard(self.platform, metadata)
        if dry is not None:
            return dry

        missing = [key for key in _REQUIRED_KEYS if not credentials.get(key)]
        if missing:
            raise PublishError(
                f"Instagram credentials missing required keys: {', '.join(missing)}", retryable=False
            )

        settings = get_settings()
        if not settings.public_base_url:
            raise PublishError(
                "Instagram publishing requires PUBLIC_BASE_URL to be set: Instagram's servers "
                "fetch the video from a public URL rather than accepting a direct upload.",
                retryable=False,
            )

        access_token = credentials["access_token"]
        ig_user_id = credentials["ig_user_id"]
        # See module docstring: the clip must be served at this path by the API module.
        video_url = f"{settings.public_base_url.rstrip('/')}/media/clips/file/{clip_path.name}"
        caption = f"{metadata.title}\n\n{compose_description(metadata)}".strip()[:2200]

        try:
            with httpx.Client(timeout=60) as client:
                creation_id = self._create_container(client, ig_user_id, video_url, caption, access_token)
                self._wait_until_finished(client, creation_id, access_token)
                media_id = self._publish_container(client, ig_user_id, creation_id, access_token)
                permalink = self._fetch_permalink(client, media_id, access_token)
        except httpx.HTTPError as exc:
            raise PublishError(
                redact(f"Instagram publish failed: {exc}", [access_token]), retryable=True
            ) from exc

        logger.info("InstagramReelsPublisher: published reel %s", media_id)
        return PublishResult(external_id=media_id, external_url=permalink)

    def _create_container(
        self, client: httpx.Client, ig_user_id: str, video_url: str, caption: str, access_token: str
    ) -> str:
        resp = client.post(
            f"{_GRAPH_BASE}/{ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": access_token,
            },
        )
        raise_for_http_status(resp, "Instagram", secrets=[access_token])
        return resp.json()["id"]

    def _wait_until_finished(self, client: httpx.Client, creation_id: str, access_token: str) -> None:
        deadline = time.monotonic() + _POLL_TIMEOUT_SEC
        status_code = None
        while time.monotonic() < deadline:
            resp = client.get(
                f"{_GRAPH_BASE}/{creation_id}",
                params={"fields": "status_code", "access_token": access_token},
            )
            raise_for_http_status(resp, "Instagram", secrets=[access_token])
            status_code = resp.json().get("status_code")
            if status_code == "FINISHED":
                return
            if status_code == "ERROR":
                # A deterministic rejection by Instagram (bad/corrupt video, disallowed
                # content, etc.) — retrying the exact same container will fail again.
                raise PublishError(f"Instagram failed to process media {creation_id}", retryable=False)
            time.sleep(_POLL_INTERVAL_SEC)
        raise PublishError(
            f"Instagram media {creation_id} did not finish processing within "
            f"{_POLL_TIMEOUT_SEC}s (last status: {status_code})",
            retryable=True,
        )

    def _publish_container(self, client: httpx.Client, ig_user_id: str, creation_id: str, access_token: str) -> str:
        resp = client.post(
            f"{_GRAPH_BASE}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": access_token},
        )
        raise_for_http_status(resp, "Instagram", secrets=[access_token])
        return resp.json()["id"]

    def _fetch_permalink(self, client: httpx.Client, media_id: str, access_token: str) -> str:
        # Best-effort: a missing permalink should not fail an otherwise successful publish.
        try:
            resp = client.get(
                f"{_GRAPH_BASE}/{media_id}",
                params={"fields": "permalink", "access_token": access_token},
            )
            if resp.status_code == 200:
                return resp.json().get("permalink", "")
        except httpx.HTTPError:
            logger.warning("InstagramReelsPublisher: could not fetch permalink for %s", media_id, exc_info=True)
        return ""
