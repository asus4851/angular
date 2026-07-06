"""End-to-end integration test: the fully offline `clipfactory demo` pipeline.

Exercises real ffmpeg (synthetic source + render) and the real HeuristicAnalyzer
and LocalExportPublisher -- no network access or API keys required.
"""

from __future__ import annotations

import json

import pytest

from clipfactory.models import Clip, ClipStatus, Post, PostStatus


@pytest.mark.integration
def test_run_demo_produces_published_clip(db, settings):
    from clipfactory.cli import run_demo

    result = run_demo()

    assert result["jobs_processed"] >= 3  # analyze -> render -> publish
    assert result["exported_files"], "expected at least one exported mp4"

    export_dir = result["export_dir"]
    mp4s = sorted(export_dir.rglob("*.mp4"))
    jsons = sorted(export_dir.rglob("*.json"))
    assert len(mp4s) >= 1
    assert len(jsons) >= 1
    assert mp4s[0].stat().st_size > 0

    sidecar = json.loads(jsons[0].read_text(encoding="utf-8"))
    assert sidecar["title"]

    with db() as session:
        clips = session.query(Clip).all()
        assert len(clips) >= 1
        assert all(c.status == ClipStatus.RENDERED for c in clips)
        for clip in clips:
            assert clip.duration_sec is not None
            assert clip.duration_sec > 0

        posts = session.query(Post).all()
        assert len(posts) >= 1
        assert all(p.status == PostStatus.PUBLISHED for p in posts)


@pytest.mark.integration
def test_run_demo_is_idempotent_about_setup_rows(db, settings):
    """Calling run_demo twice should reuse the demo channel/account/route/video."""
    from clipfactory.cli import run_demo
    from clipfactory.models import Account, Channel, Route, Video

    run_demo()
    with db() as session:
        channel_count = session.query(Channel).count()
        account_count = session.query(Account).count()
        route_count = session.query(Route).count()
        video_count = session.query(Video).count()

    run_demo()
    with db() as session:
        assert session.query(Channel).count() == channel_count
        assert session.query(Account).count() == account_count
        assert session.query(Route).count() == route_count
        assert session.query(Video).count() == video_count


@pytest.mark.integration
def test_demo_cli_command_runs(db, settings):
    from typer.testing import CliRunner

    from clipfactory.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["demo"])

    assert result.exit_code == 0, result.output
    assert "Demo complete" in result.output
    assert "Exported clip(s)" in result.output
