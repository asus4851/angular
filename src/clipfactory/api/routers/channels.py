"""Channel CRUD + manual poll trigger."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import ChannelCreate, ChannelOut, ChannelUpdate
from clipfactory.models import Channel

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
