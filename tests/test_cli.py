"""Tests for clipfactory.cli: channel_add defaults, demo channel setup."""

from __future__ import annotations

from typer.testing import CliRunner

from clipfactory.models import Channel

_VALID_CHANNEL_ID = "UC" + "a" * 22  # matches ingest_yt's channel-id regex, no network needed


def test_channel_add_defaults_interval_to_poll_interval_min_setting(db, settings, monkeypatch):
    """--interval used to default to a hardcoded 30, silently ignoring
    POLL_INTERVAL_MIN entirely. It must now fall back to the setting."""
    monkeypatch.setenv("POLL_INTERVAL_MIN", "45")
    from clipfactory.config import get_settings

    get_settings.cache_clear()
    try:
        from clipfactory.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["channel", "add", _VALID_CHANNEL_ID])
        assert result.exit_code == 0, result.output

        with db() as session:
            channel = session.query(Channel).filter(Channel.yt_channel_id == _VALID_CHANNEL_ID).one()
            assert channel.check_interval_min == 45
    finally:
        get_settings.cache_clear()


def test_channel_add_explicit_interval_overrides_setting(db, settings, monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL_MIN", "45")
    from clipfactory.config import get_settings

    get_settings.cache_clear()
    try:
        from clipfactory.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["channel", "add", _VALID_CHANNEL_ID, "--interval", "5"])
        assert result.exit_code == 0, result.output

        with db() as session:
            channel = session.query(Channel).filter(Channel.yt_channel_id == _VALID_CHANNEL_ID).one()
            assert channel.check_interval_min == 5
    finally:
        get_settings.cache_clear()


def test_demo_channel_created_disabled_so_scheduler_never_polls_it(db, settings):
    """The demo pipeline enqueues its ANALYZE_VIDEO job directly and never
    needs (or wants) the scheduler polling a synthetic channel id against
    real YouTube forever."""
    from clipfactory.cli import run_demo

    run_demo()

    with db() as session:
        channel = session.query(Channel).filter(Channel.title == "ClipFactory Demo Channel").one()
        assert channel.enabled is False


def test_cli_help_runs_without_error():
    from clipfactory.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
