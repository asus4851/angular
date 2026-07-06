"""Route CRUD: assigns a channel's clips to be published to an account."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import RouteCreate, RouteOut, RouteUpdate
from clipfactory.models import Account, Channel, Route

router = APIRouter(prefix="/api/routes", tags=["routes"])


def _to_out(route: Route) -> RouteOut:
    return RouteOut(
        id=route.id,
        channel_id=route.channel_id,
        account_id=route.account_id,
        channel_title=route.channel.title or route.channel.yt_channel_id,
        account_name=route.account.name,
        account_platform=route.account.platform,
        enabled=route.enabled,
        title_template=route.title_template,
        description_template=route.description_template,
        extra_hashtags=route.extra_hashtags,
        created_at=route.created_at,
    )


@router.get("", response_model=list[RouteOut])
def list_routes(db: Session = Depends(get_db)) -> list[RouteOut]:
    routes = db.scalars(select(Route).order_by(Route.created_at.desc()))
    return [_to_out(r) for r in routes]


@router.post("", response_model=RouteOut, status_code=201)
def create_route(payload: RouteCreate, db: Session = Depends(get_db)) -> RouteOut:
    if db.get(Channel, payload.channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if db.get(Account, payload.account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")

    route = Route(
        channel_id=payload.channel_id,
        account_id=payload.account_id,
        title_template=payload.title_template,
        description_template=payload.description_template,
        extra_hashtags=payload.extra_hashtags,
    )
    db.add(route)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Route for this channel+account already exists") from exc
    db.refresh(route)
    return _to_out(route)


@router.patch("/{route_id}", response_model=RouteOut)
def update_route(route_id: int, payload: RouteUpdate, db: Session = Depends(get_db)) -> RouteOut:
    route = db.get(Route, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(route, field, value)
    db.flush()
    db.refresh(route)
    return _to_out(route)


@router.delete("/{route_id}", status_code=204)
def delete_route(route_id: int, db: Session = Depends(get_db)) -> None:
    route = db.get(Route, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    db.delete(route)
