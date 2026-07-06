"""Serves rendered clip files.

Public (no API key required): Instagram's Graph API downloads the video
directly from `{PUBLIC_BASE_URL}/media/clips/file/{filename}` (see
docs/PUBLISHERS.md), so this router must stay reachable without auth.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.config import get_settings
from clipfactory.models import Clip

router = APIRouter(prefix="/media/clips", tags=["media"])


def _is_safe_filename(name: str) -> bool:
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return True


@router.get("/file/{filename}")
def get_clip_file(filename: str) -> FileResponse:
    if not _is_safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    settings = get_settings()
    path = (settings.clips_dir / filename).resolve()
    clips_dir = settings.clips_dir.resolve()
    if clips_dir not in path.parents and path != clips_dir:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(path, media_type="video/mp4")


@router.get("/{clip_id}")
def get_clip_by_id(clip_id: int, db: Session = Depends(get_db)) -> FileResponse:
    clip = db.get(Clip, clip_id)
    if clip is None or not clip.file_path:
        raise HTTPException(status_code=404, detail="Clip not found")

    path = clip.file_path
    from pathlib import Path

    file_path = Path(path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path, media_type="video/mp4")
