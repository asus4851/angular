"""Publisher protocol, registry, and shared helpers (module: publish).

Every concrete publisher (local export, YouTube, Instagram, TikTok) implements
the `Publisher` protocol and registers itself with `@register(platform)`.
`get_publisher()` is the single entry point the pipeline uses; it lazily
imports the concrete module so that e.g. importing `clipfactory.publish` never
pulls in `googleapiclient` or `httpx` unless that platform is actually used.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from clipfactory.config import get_settings
from clipfactory.models import Platform
from clipfactory.schemas import PostMetadata, PublishResult

logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Raised when a publisher fails to post a clip.

    `retryable` tells the worker whether re-attempting later can help:
    - False: auth/validation errors (bad credentials, missing config, 4xx
      other than 429) — retrying with the same input will fail again.
    - True (default): network errors, timeouts, 429/5xx — transient, worth
      retrying with exponential backoff.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Publisher(Protocol):
    """Module contract: a rendered clip + metadata in, a live post out."""

    platform: str

    def publish(self, clip_path: Path, metadata: PostMetadata, credentials: dict) -> PublishResult: ...


_REGISTRY: dict[str, type[Publisher]] = {}


def register(platform: str):
    """Class decorator registering a Publisher implementation under `platform`."""

    def _wrap(cls: type[Publisher]) -> type[Publisher]:
        _REGISTRY[platform] = cls
        return cls

    return _wrap


def get_publisher(platform: str | Platform) -> Publisher:
    """Instantiate the publisher for `platform`.

    Imports the concrete submodule lazily (inside this function) so that
    unrelated heavy/optional dependencies (google api client, httpx-based
    social APIs) are not imported until a publisher for that platform is
    actually requested.
    """
    name = platform.value if isinstance(platform, Platform) else str(platform)

    if name not in _REGISTRY:
        # Trigger registration by importing the matching submodule.
        if name == Platform.LOCAL.value:
            import clipfactory.publish.local  # noqa: F401
        elif name == Platform.YOUTUBE.value:
            import clipfactory.publish.youtube  # noqa: F401
        elif name == Platform.INSTAGRAM.value:
            import clipfactory.publish.instagram  # noqa: F401
        elif name == Platform.TIKTOK.value:
            import clipfactory.publish.tiktok  # noqa: F401
        else:
            raise PublishError(f"Unknown publishing platform: {name!r}", retryable=False)

    if name not in _REGISTRY:
        # Submodule imported but didn't register itself under this name (shouldn't happen).
        raise PublishError(f"Unknown publishing platform: {name!r}", retryable=False)

    return _REGISTRY[name]()


def dry_run_guard(platform: str, metadata: PostMetadata) -> PublishResult | None:
    """If DRY_RUN is enabled, log the intended action and short-circuit.

    Concrete network publishers call this first, before touching any
    credentials or making HTTP requests, so `DRY_RUN=true` guarantees no
    outbound calls regardless of which platform is configured.
    """
    if get_settings().dry_run:
        logger.info("[DRY RUN] would publish %r to %s", metadata.title, platform)
        return PublishResult(external_id="dry-run", external_url="")
    return None


def compose_description(metadata: PostMetadata) -> str:
    """Join description + hashtags (as "#tag") into the final post body."""
    parts = []
    if metadata.description:
        parts.append(metadata.description)
    tags = " ".join(f"#{tag.lstrip('#')}" for tag in metadata.hashtags if tag.strip())
    if tags:
        parts.append(tags)
    return "\n\n".join(parts)


def raise_for_http_status(response, service: str) -> None:
    """Shared 4xx/5xx -> PublishError mapping for httpx-based publishers.

    429 and 5xx are treated as transient (retryable); other 4xx are treated
    as permanent client errors (bad token, bad payload, etc.).
    """
    if response.status_code < 400:
        return
    retryable = response.status_code == 429 or response.status_code >= 500
    raise PublishError(
        f"{service} API error {response.status_code}: {response.text[:300]}", retryable=retryable
    )
