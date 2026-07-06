"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from clipfactory.db import get_session_factory


def get_db() -> Iterator[Session]:
    """Yield one session per request; commit on success, rollback on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
