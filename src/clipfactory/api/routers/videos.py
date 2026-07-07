"""Video listing, import-by-URL, and re-analyze."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import VideoAnalyzeRequest, VideoImportOut, VideoImportRequest, VideoOut
from clipfactory.models import Channel, ClipCandidate, Video, VideoStatus

router = APIRouter(prefix="/api/videos", tags=["videos"])


def _candidate_count(db: Session, video_id: int) -> int:
    return db.scalar(select(func.count()).select_from(ClipCandidate).where(ClipCandidate.video_id == video_id)) or 0


def _to_out(db: Session, video: Video) -> VideoOut:
    out = VideoOut.model_validate(video, from_attributes=True)
    out.candidate_count = _candidate_count(db, video.id)
    return out


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


def _overrides_from(max_clips: int | None, min_score: int | None, language: str | None) -> dict:
    overrides: dict = {}
    if max_clips is not None:
        overrides["max_clips"] = max_clips
    if min_score is not None:
        overrides["min_score"] = min_score
    if language is not None:
        overrides["language"] = language
    return overrides


@router.post("", response_model=VideoImportOut)
def import_video(payload: VideoImportRequest, response: Response, db: Session = Depends(get_db)) -> VideoImportOut:
    from clipfactory.ingest.youtube import IngestError, fetch_video_info

    try:
        video_info, channel_info = fetch_video_info(payload.url)
    except IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    channel = db.scalar(select(Channel).where(Channel.yt_channel_id == channel_info.yt_channel_id))
    if channel is None:
        channel = Channel(
            yt_channel_id=channel_info.yt_channel_id,
            title=channel_info.title,
            url=channel_info.url,
            enabled=False,
        )
        db.add(channel)
        db.flush()

    video = db.scalar(select(Video).where(Video.yt_video_id == video_info.yt_video_id))
    already_imported = video is not None
    if video is None:
        video = Video(
            channel_id=channel.id,
            yt_video_id=video_info.yt_video_id,
            title=video_info.title,
            duration_sec=video_info.duration_sec,
            published_at=video_info.published_at,
            status=VideoStatus.NEW,
        )
        db.add(video)
        db.flush()

        from clipfactory.models import JobType
        from clipfactory.pipeline.queue import enqueue

        job_payload: dict = {"video_id": video.id}
        overrides = _overrides_from(payload.max_clips, payload.min_score, payload.language)
        if overrides:
            job_payload["analysis_overrides"] = overrides
        enqueue(db, JobType.FETCH_TRANSCRIPT, job_payload)
        response.status_code = 201

    db.flush()
    db.refresh(video)
    return VideoImportOut(
        id=video.id,
        title=video.title,
        duration_sec=video.duration_sec,
        channel_title=channel.title,
        already_imported=already_imported,
    )


@router.post("/{video_id}/analyze", response_model=VideoOut)
def analyze_video(
    video_id: int, payload: VideoAnalyzeRequest | None = None, db: Session = Depends(get_db)
) -> VideoOut:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status not in (VideoStatus.TRANSCRIBED, VideoStatus.ANALYZED):
        raise HTTPException(status_code=409, detail="Video must be transcribed or analyzed to re-analyze")

    if video.status == VideoStatus.ANALYZED:
        video.status = VideoStatus.TRANSCRIBED

    from clipfactory.models import JobType
    from clipfactory.pipeline.queue import enqueue

    payload = payload or VideoAnalyzeRequest()
    job_payload: dict = {"video_id": video.id}
    overrides = _overrides_from(payload.max_clips, payload.min_score, payload.language)
    if overrides:
        job_payload["overrides"] = overrides
    enqueue(db, JobType.ANALYZE_VIDEO, job_payload)

    db.flush()
    db.refresh(video)
    return _to_out(db, video)
