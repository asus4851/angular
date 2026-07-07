"""Channel CRUD + manual poll trigger + video catalog browsing/import."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import (
    ChannelCatalogItem,
    ChannelCreate,
    ChannelImportOut,
    ChannelImportRequest,
    ChannelOut,
    ChannelUpdate,
)
from clipfactory.models import Channel, Video, VideoStatus

router = APIRouter(prefix="/api/channels", tags=["channels"])


@router.get("", response_model=list[ChannelOut])
def list_channels(db: Session = Depends(get_db)) -> list[Channel]:
    return list(db.scalars(select(Channel).order_by(Channel.created_at.desc())))


@router.post("", response_model=ChannelOut, status_code=201)
def create_channel(payload: ChannelCreate, db: Session = Depends(get_db)) -> Channel:
    from clipfactory.ingest.youtube import IngestError, resolve_channel

    try:
        info = resolve_channel(payload.url)
    except IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = db.scalar(select(Channel).where(Channel.yt_channel_id == info.yt_channel_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Channel already exists")

    channel = Channel(
        yt_channel_id=info.yt_channel_id,
        title=info.title,
        url=info.url,
        auto_approve=payload.auto_approve,
        check_interval_min=payload.check_interval_min,
        max_clips_per_video=payload.max_clips_per_video,
        min_score=payload.min_score,
        language=payload.language,
    )
    db.add(channel)
    db.flush()
    db.refresh(channel)
    return channel


@router.patch("/{channel_id}", response_model=ChannelOut)
def update_channel(channel_id: int, payload: ChannelUpdate, db: Session = Depends(get_db)) -> Channel:
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(channel, field, value)
    db.flush()
    db.refresh(channel)
    return channel


@router.delete("/{channel_id}", status_code=204)
def delete_channel(channel_id: int, db: Session = Depends(get_db)) -> None:
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    db.delete(channel)


@router.post("/{channel_id}/poll", status_code=202)
def poll_channel(channel_id: int, db: Session = Depends(get_db)) -> dict:
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    from clipfactory.models import JobType
    from clipfactory.pipeline.queue import enqueue

    job = enqueue(db, JobType.POLL_CHANNEL, {"channel_id": channel.id})
    db.flush()
    return {"job_id": job.id}


@router.get("/{channel_id}/catalog", response_model=list[ChannelCatalogItem])
def channel_catalog(
    channel_id: int, limit: int = Query(default=30, le=100), db: Session = Depends(get_db)
) -> list[ChannelCatalogItem]:
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    from clipfactory.ingest.youtube import IngestError, list_channel_videos

    try:
        infos = list_channel_videos(channel.yt_channel_id, limit=limit)
    except IngestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    yt_ids = [info.yt_video_id for info in infos]
    existing: dict[str, int] = {}
    if yt_ids:
        rows = db.execute(select(Video.yt_video_id, Video.id).where(Video.yt_video_id.in_(yt_ids))).all()
        existing = dict(rows)

    return [
        ChannelCatalogItem(
            yt_video_id=info.yt_video_id,
            title=info.title,
            duration_sec=info.duration_sec,
            imported=info.yt_video_id in existing,
            video_id=existing.get(info.yt_video_id),
        )
        for info in infos
    ]


@router.post("/{channel_id}/import", response_model=ChannelImportOut)
def channel_import(
    channel_id: int, payload: ChannelImportRequest, response: Response, db: Session = Depends(get_db)
) -> ChannelImportOut:
    channel = db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    from clipfactory.models import JobType
    from clipfactory.pipeline.queue import enqueue

    video = db.scalar(select(Video).where(Video.yt_video_id == payload.yt_video_id))
    already_imported = False
    needs_enqueue = True

    if video is None:
        video = Video(
            channel_id=channel_id,
            yt_video_id=payload.yt_video_id,
            title=payload.title or "",
            status=VideoStatus.NEW,
        )
        db.add(video)
        db.flush()
    elif video.status in (VideoStatus.SKIPPED, VideoStatus.FAILED):
        video.status = VideoStatus.NEW
        video.error = ""
    else:
        already_imported = True
        needs_enqueue = False

    if needs_enqueue:
        job_payload: dict = {"video_id": video.id}
        overrides: dict = {}
        if payload.max_clips is not None:
            overrides["max_clips"] = payload.max_clips
        if payload.min_score is not None:
            overrides["min_score"] = payload.min_score
        if overrides:
            job_payload["analysis_overrides"] = overrides
        enqueue(db, JobType.FETCH_TRANSCRIPT, job_payload)
        response.status_code = 201

    db.flush()
    db.refresh(video)
    return ChannelImportOut(video_id=video.id, already_imported=already_imported)
