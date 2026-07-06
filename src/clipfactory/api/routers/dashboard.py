"""Server-rendered dashboard pages + small aggregate API endpoints (stats, poll-all)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import StatsOut
from clipfactory.models import (
    Account,
    Channel,
    Clip,
    ClipCandidate,
    Job,
    JobStatus,
    JobType,
    Post,
    Route,
    Video,
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

pages_router = APIRouter(tags=["dashboard"])
api_router = APIRouter(prefix="/api", tags=["dashboard-api"])


def _counts_by_status(db: Session, model, status_col) -> dict[str, int]:
    rows = db.execute(select(status_col, func.count()).group_by(status_col)).all()
    return {(status.value if hasattr(status, "value") else str(status)): count for status, count in rows}


@pages_router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    stats = {
        "channels": db.scalar(select(func.count()).select_from(Channel)) or 0,
        "videos": db.scalar(select(func.count()).select_from(Video)) or 0,
        "candidates_pending": db.scalar(
            select(func.count()).select_from(ClipCandidate).where(ClipCandidate.status == "pending")
        )
        or 0,
        "clips_rendered": db.scalar(select(func.count()).select_from(Clip).where(Clip.status == "rendered"))
        or 0,
        "posts_published": db.scalar(
            select(func.count()).select_from(Post).where(Post.status == "published")
        )
        or 0,
        "posts_failed": db.scalar(select(func.count()).select_from(Post).where(Post.status == "failed")) or 0,
    }
    recent_candidates = list(
        db.scalars(select(ClipCandidate).order_by(ClipCandidate.created_at.desc()).limit(5))
    )
    recent_posts = list(db.scalars(select(Post).order_by(Post.created_at.desc()).limit(5)))
    channels = list(db.scalars(select(Channel).order_by(Channel.created_at.desc())))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "stats": stats,
            "recent_candidates": recent_candidates,
            "recent_posts": recent_posts,
            "channels": channels,
        },
    )


@pages_router.get("/channels")
def channels_page(request: Request, db: Session = Depends(get_db)):
    channels = list(db.scalars(select(Channel).order_by(Channel.created_at.desc())))
    return templates.TemplateResponse(request, "channels.html", {"channels": channels})


@pages_router.get("/accounts")
def accounts_page(request: Request, db: Session = Depends(get_db)):
    accounts = list(db.scalars(select(Account).order_by(Account.created_at.desc())))
    return templates.TemplateResponse(request, "accounts.html", {"accounts": accounts})


@pages_router.get("/routes")
def routes_page(request: Request, db: Session = Depends(get_db)):
    routes = list(db.scalars(select(Route).order_by(Route.created_at.desc())))
    channels = list(db.scalars(select(Channel).order_by(Channel.title)))
    accounts = list(db.scalars(select(Account).order_by(Account.name)))
    return templates.TemplateResponse(
        request, "routes.html", {"routes": routes, "channels": channels, "accounts": accounts}
    )


@pages_router.get("/moderation")
def moderation_page(request: Request, db: Session = Depends(get_db)):
    candidates = list(
        db.scalars(
            select(ClipCandidate)
            .where(ClipCandidate.status == "pending")
            .order_by(ClipCandidate.created_at.desc())
        )
    )
    return templates.TemplateResponse(request, "moderation.html", {"candidates": candidates})


@pages_router.get("/posts")
def posts_page(request: Request, db: Session = Depends(get_db)):
    posts = list(db.scalars(select(Post).order_by(Post.created_at.desc())))
    return templates.TemplateResponse(request, "posts.html", {"posts": posts})


@api_router.get("/stats", response_model=StatsOut)
def stats(db: Session = Depends(get_db)) -> StatsOut:
    failed_jobs = list(
        db.scalars(
            select(Job).where(Job.status == JobStatus.FAILED).order_by(Job.updated_at.desc()).limit(10)
        )
    )
    return StatsOut(
        videos=_counts_by_status(db, Video, Video.status),
        candidates=_counts_by_status(db, ClipCandidate, ClipCandidate.status),
        clips=_counts_by_status(db, Clip, Clip.status),
        posts=_counts_by_status(db, Post, Post.status),
        jobs=_counts_by_status(db, Job, Job.status),
        recent_failed_jobs=[
            {"type": j.type.value, "error": j.last_error, "run_at": j.run_at.isoformat()} for j in failed_jobs
        ],
    )


@api_router.post("/poll-all", status_code=202)
def poll_all(db: Session = Depends(get_db)) -> dict:
    from clipfactory.pipeline.queue import enqueue

    channels = list(db.scalars(select(Channel).where(Channel.enabled.is_(True))))
    job_ids = [enqueue(db, JobType.POLL_CHANNEL, {"channel_id": c.id}).id for c in channels]
    db.flush()
    return {"enqueued": len(job_ids), "job_ids": job_ids}
