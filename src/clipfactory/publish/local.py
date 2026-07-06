"""Local export publisher (module: publish).

Copies the rendered clip into a folder on disk together with a JSON sidecar
carrying its metadata. Requires no credentials or network access, so it is
the default account type used to exercise the whole pipeline end to end.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from clipfactory.config import get_settings
from clipfactory.publish.base import PublishResult, dry_run_guard, register
from clipfactory.schemas import PostMetadata

logger = logging.getLogger(__name__)


@register("local")
class LocalExportPublisher:
    platform = "local"

    def publish(self, clip_path: Path, metadata: PostMetadata, credentials: dict) -> PublishResult:
        dry = dry_run_guard(self.platform, metadata)
        if dry is not None:
            return dry

        settings = get_settings()
        export_dir = Path(credentials.get("export_dir") or settings.export_dir)
        account_name = credentials.get("name") or "default"
        target_dir = export_dir / account_name
        target_dir.mkdir(parents=True, exist_ok=True)

        dest = _unique_destination(target_dir, clip_path)
        shutil.copy2(clip_path, dest)

        sidecar = dest.with_suffix(".json")
        sidecar.write_text(
            json.dumps(
                {
                    "title": metadata.title,
                    "description": metadata.description,
                    "hashtags": metadata.hashtags,
                    "exported_at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        logger.info("LocalExportPublisher: exported %s -> %s", clip_path, dest)
        return PublishResult(external_id=dest.name, external_url=dest.resolve().as_uri())


def _unique_destination(target_dir: Path, clip_path: Path) -> Path:
    """Timestamped, collision-free destination path for `clip_path` in `target_dir`."""
    stem = clip_path.stem
    suffix = clip_path.suffix
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    candidate = target_dir / f"{stem}_{timestamp}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = target_dir / f"{stem}_{timestamp}_{counter}{suffix}"
        counter += 1
    return candidate
