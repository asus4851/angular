"""Video listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import VideoOut
from clipfactory.models import ClipCandidate, Video, VideoStatus

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("", response_model=list[VideoOut])
def list_videos(
    channel_id: int | None = None,
    status: VideoStatus | None = None,
    limit: int = Query(default=50, le=500),
    db: Session = Depends(get_db),
) -> list[VideoOut]:
    stmt = select(Video)
    if channel_id is not None:
        stmt = stmt.where(Video.channel_id == channel_id)
    if status is not None:
        stmt = stmt.where(Video.status == status)
    stmt = stmt.order_by(Video.discovered_at.desc()).limit(limit)
    videos = list(db.scalars(stmt))

    counts: dict[int, int] = {}
    if videos:
        video_ids = [v.id for v in videos]
        rows = db.execute(
            select(ClipCandidate.video_id, func.count(ClipCandidate.id))
            .where(ClipCandidate.video_id.in_(video_ids))
            .group_by(ClipCandidate.video_id)
        ).all()
        counts = dict(rows)

    result = []
    for v in videos:
        out = VideoOut.model_validate(v, from_attributes=True)
        out.candidate_count = counts.get(v.id, 0)
        result.append(out)
    return result
