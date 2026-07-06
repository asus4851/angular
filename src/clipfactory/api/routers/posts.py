"""Post listing + retry for failed publishes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import PostOut
from clipfactory.models import JobType, Post, PostStatus

router = APIRouter(prefix="/api/posts", tags=["posts"])


def _to_out(post: Post) -> PostOut:
    account = post.route.account
    return PostOut(
        id=post.id,
        clip_id=post.clip_id,
        route_id=post.route_id,
        account_platform=account.platform,
        account_name=account.name,
        status=post.status,
        external_id=post.external_id,
        external_url=post.external_url,
        error=post.error,
        attempts=post.attempts,
        created_at=post.created_at,
        published_at=post.published_at,
    )


@router.get("", response_model=list[PostOut])
def list_posts(
    status: PostStatus | None = None,
    limit: int = Query(default=50, le=500),
    db: Session = Depends(get_db),
) -> list[PostOut]:
    stmt = select(Post)
    if status is not None:
        stmt = stmt.where(Post.status == status)
    stmt = stmt.order_by(Post.created_at.desc()).limit(limit)
    posts = list(db.scalars(stmt))
    return [_to_out(p) for p in posts]


@router.post("/{post_id}/retry", response_model=PostOut)
def retry(post_id: int, db: Session = Depends(get_db)) -> PostOut:
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.status != PostStatus.FAILED:
        raise HTTPException(status_code=409, detail="Only failed posts can be retried")

    post.status = PostStatus.PENDING
    post.error = ""

    from clipfactory.pipeline.queue import enqueue

    enqueue(db, JobType.PUBLISH_POST, {"post_id": post.id})
    db.flush()
    db.refresh(post)
    return _to_out(post)
