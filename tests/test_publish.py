"""Tests for clipfactory.publish (fully offline: HTTP and Google clients are mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from clipfactory.config import get_settings
from clipfactory.models import Platform
from clipfactory.publish import PublishError, get_publisher
from clipfactory.schemas import PostMetadata


def _make_clip(tmp_path: Path, name: str = "clip.mp4", content: bytes = b"fake-mp4-bytes") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _metadata() -> PostMetadata:
    return PostMetadata(title="Дуже цікавий момент", description="Опис моменту", hashtags=["shorts", "#viral"])


def _boom_client(*args, **kwargs):
    raise AssertionError("httpx.Client should not be constructed (no network expected here)")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_get_publisher_returns_correct_class_for_each_platform():
    from clipfactory.publish.instagram import InstagramReelsPublisher
    from clipfactory.publish.local import LocalExportPublisher
    from clipfactory.publish.tiktok import TikTokPublisher
    from clipfactory.publish.youtube import YouTubeShortsPublisher

    assert isinstance(get_publisher("local"), LocalExportPublisher)
    assert isinstance(get_publisher(Platform.LOCAL), LocalExportPublisher)
    assert isinstance(get_publisher("youtube"), YouTubeShortsPublisher)
    assert isinstance(get_publisher(Platform.YOUTUBE), YouTubeShortsPublisher)
    assert isinstance(get_publisher("instagram"), InstagramReelsPublisher)
    assert isinstance(get_publisher("tiktok"), TikTokPublisher)


def test_get_publisher_unknown_platform_raises_non_retryable():
    with pytest.raises(PublishError) as exc_info:
        get_publisher("myspace")
    assert exc_info.value.retryable is False


# ---------------------------------------------------------------------------
# Local export
# ---------------------------------------------------------------------------


def test_local_publish_writes_file_and_sidecar(settings, tmp_path):
    clip = _make_clip(tmp_path)
    publisher = get_publisher("local")

    result = publisher.publish(clip, _metadata(), {})

    export_dir = settings.export_dir / "default"
    files = list(export_dir.glob("clip_*.mp4"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"fake-mp4-bytes"

    sidecar = files[0].with_suffix(".json")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["title"] == "Дуже цікавий момент"
    assert data["hashtags"] == ["shorts", "#viral"]
    assert "exported_at" in data

    assert result.external_id == files[0].name
    assert result.external_url.startswith("file://")


def test_local_publish_collision_creates_two_files(settings, tmp_path):
    clip = _make_clip(tmp_path)
    publisher = get_publisher("local")

    publisher.publish(clip, _metadata(), {})
    publisher.publish(clip, _metadata(), {})

    export_dir = settings.export_dir / "default"
    files = sorted(export_dir.glob("clip_*.mp4"))
    assert len(files) == 2
    # Sidecars follow suit, one per exported file.
    sidecars = sorted(export_dir.glob("clip_*.json"))
    assert len(sidecars) == 2


def test_local_publish_custom_account_name_and_export_dir(settings, tmp_path):
    clip = _make_clip(tmp_path)
    custom_dir = tmp_path / "custom-exports"
    publisher = get_publisher("local")

    result = publisher.publish(clip, _metadata(), {"export_dir": str(custom_dir), "name": "acc1"})

    files = list((custom_dir / "acc1").glob("clip_*.mp4"))
    assert len(files) == 1
    assert result.external_id == files[0].name


# ---------------------------------------------------------------------------
# Dry run: no network publisher should ever touch httpx/google when DRY_RUN=true
# ---------------------------------------------------------------------------


@pytest.fixture()
def dry_run_settings(settings, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_dry_run_youtube_no_network(dry_run_settings, monkeypatch, tmp_path):
    clip = _make_clip(tmp_path)
    publisher = get_publisher("youtube")

    result = publisher.publish(clip, _metadata(), {})

    assert result.external_id == "dry-run"
    assert result.external_url == ""


def test_dry_run_instagram_no_network(dry_run_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(httpx, "Client", _boom_client)
    clip = _make_clip(tmp_path)
    publisher = get_publisher("instagram")

    result = publisher.publish(clip, _metadata(), {})

    assert result.external_id == "dry-run"
    assert result.external_url == ""


def test_dry_run_tiktok_no_network(dry_run_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(httpx, "Client", _boom_client)
    clip = _make_clip(tmp_path)
    publisher = get_publisher("tiktok")

    result = publisher.publish(clip, _metadata(), {})

    assert result.external_id == "dry-run"
    assert result.external_url == ""


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


def test_youtube_missing_credentials_raises_non_retryable(settings, tmp_path):
    clip = _make_clip(tmp_path)
    publisher = get_publisher("youtube")

    with pytest.raises(PublishError) as exc_info:
        publisher.publish(clip, _metadata(), {"client_id": "x"})  # missing client_secret, refresh_token

    assert exc_info.value.retryable is False
    message = str(exc_info.value)
    assert "client_secret" in message
    assert "refresh_token" in message


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------


def test_instagram_missing_public_base_url_raises_non_retryable(settings, tmp_path):
    clip = _make_clip(tmp_path)
    publisher = get_publisher("instagram")

    with pytest.raises(PublishError) as exc_info:
        publisher.publish(clip, _metadata(), {"access_token": "t", "ig_user_id": "1"})

    assert exc_info.value.retryable is False
    assert "PUBLIC_BASE_URL" in str(exc_info.value)


def test_instagram_happy_path(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    get_settings.cache_clear()

    import clipfactory.publish.instagram as instagram_module

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v21.0/999/media" and request.method == "POST":
            assert "example.com%2Fmedia%2Fclips%2Ffile%2Fclip.mp4" in request.content.decode()
            return httpx.Response(200, json={"id": "creation-1"})
        if request.url.path == "/v21.0/creation-1" and request.method == "GET":
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if request.url.path == "/v21.0/999/media_publish" and request.method == "POST":
            return httpx.Response(200, json={"id": "media-1"})
        if request.url.path == "/v21.0/media-1" and request.method == "GET":
            return httpx.Response(200, json={"permalink": "https://instagram.com/reel/xyz"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(instagram_module, "_POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(instagram_module.httpx, "Client", fake_client)

    clip = _make_clip(tmp_path)
    publisher = get_publisher("instagram")
    result = publisher.publish(clip, _metadata(), {"access_token": "tok", "ig_user_id": "999"})

    assert result.external_id == "media-1"
    assert result.external_url == "https://instagram.com/reel/xyz"
    assert "POST /v21.0/999/media" in calls
    assert "POST /v21.0/999/media_publish" in calls


def test_instagram_error_status_is_retryable(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    get_settings.cache_clear()

    import clipfactory.publish.instagram as instagram_module

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v21.0/999/media":
            return httpx.Response(200, json={"id": "creation-1"})
        if request.url.path == "/v21.0/creation-1":
            return httpx.Response(200, json={"status_code": "ERROR"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(instagram_module, "_POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(instagram_module.httpx, "Client", fake_client)

    clip = _make_clip(tmp_path)
    publisher = get_publisher("instagram")

    with pytest.raises(PublishError) as exc_info:
        publisher.publish(clip, _metadata(), {"access_token": "tok", "ig_user_id": "999"})

    assert exc_info.value.retryable is True


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------


def test_tiktok_happy_path_single_chunk(settings, monkeypatch, tmp_path):
    import clipfactory.publish.tiktok as tiktok_module

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init/"):
            assert request.headers["authorization"] == "Bearer tok"
            return httpx.Response(
                200,
                json={"data": {"publish_id": "pub-1", "upload_url": "https://upload.tiktokapis.com/upload"}},
            )
        if request.url.host == "upload.tiktokapis.com":
            assert request.method == "PUT"
            assert request.headers["content-range"] == "bytes 0-999/1000"
            return httpx.Response(201)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(tiktok_module.httpx, "Client", fake_client)

    clip = _make_clip(tmp_path, content=b"x" * 1000)
    publisher = get_publisher("tiktok")
    result = publisher.publish(clip, _metadata(), {"access_token": "tok"})

    assert result.external_id == "pub-1"
    assert result.external_url == ""


def test_tiktok_missing_access_token_raises_non_retryable(settings, tmp_path):
    clip = _make_clip(tmp_path)
    publisher = get_publisher("tiktok")

    with pytest.raises(PublishError) as exc_info:
        publisher.publish(clip, _metadata(), {})

    assert exc_info.value.retryable is False


def test_tiktok_server_error_is_retryable(settings, monkeypatch, tmp_path):
    import clipfactory.publish.tiktok as tiktok_module

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream hiccup")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(tiktok_module.httpx, "Client", fake_client)

    clip = _make_clip(tmp_path, content=b"x" * 10)
    publisher = get_publisher("tiktok")

    with pytest.raises(PublishError) as exc_info:
        publisher.publish(clip, _metadata(), {"access_token": "tok"})

    assert exc_info.value.retryable is True
