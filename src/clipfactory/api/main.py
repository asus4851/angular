"""FastAPI application factory: wires routers, templates, lifespan and auth."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from clipfactory.api.routers import accounts, candidates, channels, clips, dashboard, media, posts, routes, videos
from clipfactory.config import get_settings
from clipfactory.db import init_db

API_KEY_HEADER = "X-API-Key"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    """Build the ClipFactory FastAPI app (used by `cli.py` with uvicorn factory=True)."""
    app = FastAPI(title="ClipFactory", lifespan=_lifespan)

    settings = get_settings()

    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        if settings.api_key and request.url.path.startswith("/api/"):
            provided = request.headers.get(API_KEY_HEADER)
            if provided != settings.api_key:
                return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
        return await call_next(request)

    app.include_router(channels.router)
    app.include_router(accounts.router)
    app.include_router(routes.router)
    app.include_router(videos.router)
    app.include_router(candidates.router)
    app.include_router(clips.router)
    app.include_router(posts.router)
    app.include_router(media.router)
    app.include_router(dashboard.api_router)
    app.include_router(dashboard.pages_router)

    return app
