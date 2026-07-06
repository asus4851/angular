"""Shared test fixtures: isolated settings + fresh in-tmp SQLite per test."""

from __future__ import annotations

import pytest

import clipfactory.db as db_module
from clipfactory.config import Settings, get_settings


@pytest.fixture()
def settings(tmp_path, monkeypatch) -> Settings:
    """Fresh Settings pointing at a temp dir/db; patches the global cache."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    db_module.reset_engine()
    s = get_settings()
    s.ensure_dirs()
    yield s
    get_settings.cache_clear()
    db_module.reset_engine()


@pytest.fixture()
def db(settings):
    """Initialized database bound to the temp settings."""
    from clipfactory.db import init_db, session_scope

    init_db()
    yield session_scope
