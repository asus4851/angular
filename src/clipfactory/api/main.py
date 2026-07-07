"""FastAPI application factory: wires routers, templates, lifespan and auth."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from clipfactory.api.routers import accounts, candidates, channels, clips, dashboard, media, posts, routes, videos
from clipfactory.config import get_settings
from clipfactory.db import init_db

API_KEY_HEADER = "X-API-Key"
API_KEY_QUERY_PARAM = "key"
API_KEY_COOKIE = "cf_key"
_COOKIE_MAX_AGE_SEC = 30 * 24 * 60 * 60  # 30 days

# The only path that must stay reachable with no key at all, key set or not:
# Instagram's Graph API fetches the clip file itself from this URL (see
# docs/PUBLISHERS.md / clipfactory.api.routers.media).
_PUBLIC_PREFIX = "/media/clips/file/"

_UNAUTHORIZED_HTML = (
    "<!doctype html><html><body style=\"font-family:sans-serif;padding:40px\">"
    "<h1>401</h1><p>Додайте ?key=ВАШ_КЛЮЧ до URL</p>"
    "</body></html>"
)


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
        if not settings.api_key:
            return await call_next(request)

        path = request.url.path
        if path.startswith(_PUBLIC_PREFIX):
            return await call_next(request)

        query_key = request.query_params.get(API_KEY_QUERY_PARAM)
        provided = (
            request.headers.get(API_KEY_HEADER)
            or request.cookies.get(API_KEY_COOKIE)
            or query_key
        )
        if provided != settings.api_key:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
            return HTMLResponse(status_code=401, content=_UNAUTHORIZED_HTML)

        response = await call_next(request)
        if query_key == settings.api_key:
            # One-time `?key=` visit remembers the key so the dashboard's own
            # same-origin fetch() calls (which never set X-API-Key) keep working.
            response.set_cookie(
                API_KEY_COOKIE,
                settings.api_key,
                httponly=True,
                samesite="lax",
                max_age=_COOKIE_MAX_AGE_SEC,
            )
        return response

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
