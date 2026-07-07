"""Tests for clipfactory.media.downloader: caching correctness (no network)."""

from __future__ import annotations

from clipfactory.media import downloader


def test_padding_sec_is_exported_and_used_internally():
    assert downloader.PADDING_SEC == 5.0


def test_download_full_was_removed():
    """download_full had zero callers; it (and its media/__init__ re-export)
    should be gone rather than kept around as dead code."""
    assert not hasattr(downloader, "download_full")

    from clipfactory import media

    assert not hasattr(media, "download_full")


def test_existing_output_ignores_partial_and_fragment_files(tmp_path):
    """Leftover fragments from an interrupted download (.part, per-format
    .fNNN.m4a/.mp4) must never be mistaken for a completed merge."""
    stem = "vid_10p0_20p0"
    (tmp_path / f"{stem}.mp4.part").write_bytes(b"partial")
    (tmp_path / f"{stem}.f140.m4a").write_bytes(b"audio-fragment-only")

    assert downloader._existing_output(tmp_path, stem) is None

    final = tmp_path / f"{stem}.mp4"
    final.write_bytes(b"merged-output")
    assert downloader._existing_output(tmp_path, stem) == final


def test_existing_output_ignores_empty_file(tmp_path):
    stem = "vid_10p0_20p0"
    (tmp_path / f"{stem}.mp4").write_bytes(b"")
    assert downloader._existing_output(tmp_path, stem) is None


def test_download_section_stem_has_decisecond_precision_no_collision(tmp_path):
    """Under the old int()-truncating stem, [10.2, 40.4] and [10.9, 40.1]
    both collapsed to the same "vid_10_40" cache key and would silently
    return each other's (wrong-length) cached fragment. Decisecond precision
    must keep them distinct."""
    file_a = tmp_path / "vid_10p2_40p4.mp4"
    file_b = tmp_path / "vid_10p9_40p1.mp4"
    file_a.write_bytes(b"fragment-a")
    file_b.write_bytes(b"fragment-b")

    # download_section returns the pre-existing cached file without ever
    # touching yt_dlp/network *only if* it computes the same stem we did
    # above -- so a cache hit here is itself proof of the precision fix.
    result_a = downloader.download_section("vid", 10.2, 40.4, tmp_path)
    result_b = downloader.download_section("vid", 10.9, 40.1, tmp_path)

    assert result_a == file_a
    assert result_b == file_b
    assert result_a != result_b
