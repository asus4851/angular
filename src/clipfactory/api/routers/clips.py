"""Clip listing + ad-hoc publish to accounts (outside of channel routes)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import ClipOut, ClipPublishRequest
from clipfactory.models import Clip, ClipStatus, JobType

router = APIRouter(prefix="/api/clips", tags=["clips"])


def _to_out(clip: Clip) -> ClipOut:
    candidate = clip.candidate
    return ClipOut(
        id=clip.id,
        candidate_title=candidate.title if candidate else "",
        status=clip.status,
        duration_sec=clip.duration_sec,
        has_posts=bool(clip.posts),
    )


@router.get("", response_model=list[ClipOut])
def list_clips(db: Session = Depends(get_db)) -> list[ClipOut]:
    clips = list(db.scalars(select(Clip).order_by(Clip.created_at.desc())))
    return [_to_out(c) for c in clips]


@router.post("/{clip_id}/publish")
def publish_clip(clip_id: int, payload: ClipPublishRequest, db: Session = Depends(get_db)) -> dict:
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.status == ClipStatus.FAILED:
        raise HTTPException(status_code=409, detail="Clip failed to render and cannot be published")

    from clipfactory.pipeline import publish_clip_to_accounts

    posts = publish_clip_to_accounts(db, clip, payload.account_ids)
    db.flush()
    return {"post_ids": [p.id for p in posts]}


@router.post("/{clip_id}/render")
def render_clip(clip_id: int, db: Session = Depends(get_db)) -> dict:
    """Requeue a failed render: reset the clip to QUEUED and re-enqueue
    RENDER_CLIP for its (already-approved) candidate."""
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip.status != ClipStatus.FAILED:
        raise HTTPException(status_code=409, detail="Only failed clips can be re-rendered")

    clip.status = ClipStatus.QUEUED
    clip.error = ""

    from clipfactory.pipeline.queue import enqueue

    enqueue(db, JobType.RENDER_CLIP, {"candidate_id": clip.candidate_id})
    db.flush()
    db.refresh(clip)
    return {"clip_id": clip.id, "status": clip.status.value}
