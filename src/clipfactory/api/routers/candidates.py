"""Clip candidate listing + moderation (approve/reject)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import ApproveRequest, CandidateOut
from clipfactory.models import CandidateStatus, ClipCandidate, Video

router = APIRouter(prefix="/api/candidates", tags=["candidates"])


def _to_out(candidate: ClipCandidate, video_title: str) -> CandidateOut:
    clip = candidate.clip
    return CandidateOut(
        id=candidate.id,
        video_id=candidate.video_id,
        video_title=video_title,
        start_sec=candidate.start_sec,
        end_sec=candidate.end_sec,
        score=candidate.score,
        title=candidate.title,
        hook=candidate.hook,
        description=candidate.description,
        hashtags=candidate.hashtags,
        reason=candidate.reason,
        status=candidate.status,
        created_at=candidate.created_at,
        clip_id=clip.id if clip else None,
        clip_status=clip.status if clip else None,
    )


@router.get("", response_model=list[CandidateOut])
def list_candidates(
    status: CandidateStatus | None = None,
    video_id: int | None = None,
    limit: int = Query(default=50, le=500),
    db: Session = Depends(get_db),
) -> list[CandidateOut]:
    stmt = select(ClipCandidate)
    if status is not None:
        stmt = stmt.where(ClipCandidate.status == status)
    if video_id is not None:
        stmt = stmt.where(ClipCandidate.video_id == video_id)
    stmt = stmt.order_by(ClipCandidate.created_at.desc()).limit(limit)
    candidates = list(db.scalars(stmt))

    video_titles: dict[int, str] = {}
    if candidates:
        video_ids = {c.video_id for c in candidates}
        rows = db.execute(select(Video.id, Video.title).where(Video.id.in_(video_ids))).all()
        video_titles = dict(rows)

    return [_to_out(c, video_titles.get(c.video_id, "")) for c in candidates]


@router.post("/{candidate_id}/approve", response_model=CandidateOut)
def approve(candidate_id: int, payload: ApproveRequest | None = None, db: Session = Depends(get_db)) -> CandidateOut:
    candidate = db.get(ClipCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")

    from clipfactory.pipeline import approve_candidate

    account_ids = payload.account_ids if payload else None
    approve_candidate(db, candidate, account_ids)
    db.flush()
    db.refresh(candidate)
    video = db.get(Video, candidate.video_id)
    return _to_out(candidate, video.title if video else "")


@router.post("/{candidate_id}/reject", response_model=CandidateOut)
def reject(candidate_id: int, db: Session = Depends(get_db)) -> CandidateOut:
    candidate = db.get(ClipCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")

    candidate.status = CandidateStatus.REJECTED
    db.flush()
    db.refresh(candidate)
    video = db.get(Video, candidate.video_id)
    return _to_out(candidate, video.title if video else "")
