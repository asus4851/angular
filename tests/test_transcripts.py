"""Tests for clipfactory.transcripts.youtube."""

from __future__ import annotations

import pytest

from clipfactory.schemas import TranscriptSegment
from clipfactory.transcripts import youtube as transcripts_youtube
from clipfactory.transcripts.youtube import (
    NoTranscriptAvailable,
    fetch_transcript,
    segments_from_json,
    segments_to_json,
)


def test_segments_json_roundtrip():
    segments = [
        TranscriptSegment(start=0.0, end=1.5, text="hello"),
        TranscriptSegment(start=1.5, end=3.0, text="world"),
    ]

    data = segments_to_json(segments)
    assert data == [
        {"start": 0.0, "end": 1.5, "text": "hello"},
        {"start": 1.5, "end": 3.0, "text": "world"},
    ]

    restored = segments_from_json(data)
    assert restored == segments


class _FakeSnippet:
    def __init__(self, text, start, duration):
        self.text = text
        self.start = start
        self.duration = duration


class _FakeFetchedTranscript:
    def __init__(self, language_code, snippets):
        self.language_code = language_code
        self._snippets = snippets

    def __iter__(self):
        return iter(self._snippets)


class _FakeTranscript:
    def __init__(self, language_code, is_generated, snippets):
        self.language_code = language_code
        self.is_generated = is_generated
        self._snippets = snippets

    def fetch(self):
        return _FakeFetchedTranscript(self.language_code, self._snippets)


class _FakeTranscriptList:
    def __init__(self, transcripts):
        self._transcripts = transcripts

    def __iter__(self):
        return iter(self._transcripts)

    def find_transcript(self, language_codes):
        from youtube_transcript_api._errors import NoTranscriptFound

        for lang in language_codes:
            for t in self._transcripts:
                if t.language_code == lang:
                    return t
        raise NoTranscriptFound("vid", language_codes, self)


class _FakeYouTubeTranscriptApi:
    transcripts: list = []

    def list(self, video_id):
        return _FakeTranscriptList(self.transcripts)


def test_fetch_transcript_primary_path(monkeypatch):
    import youtube_transcript_api

    snippets = [
        _FakeSnippet("Hello there", 0.0, 2.0),
        _FakeSnippet("[Music]", 2.0, 1.0),
        _FakeSnippet("  ", 3.0, 1.0),
        _FakeSnippet("General Kenobi", 4.0, 2.0),
    ]
    fake_api = _FakeYouTubeTranscriptApi()
    fake_api.transcripts = [_FakeTranscript("en", False, snippets)]

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", lambda: fake_api)

    language, segments = fetch_transcript("abc123", ["en"])

    assert language == "en"
    assert [seg.text for seg in segments] == ["Hello there", "General Kenobi"]
    assert segments[0].start == 0.0
    assert segments[0].end == 2.0
    assert segments[1].start == 4.0
    assert segments[1].end == 6.0


def test_fetch_transcript_primary_path_falls_back_to_any_language(monkeypatch):
    import youtube_transcript_api

    snippets = [_FakeSnippet("Bonjour le monde", 0.0, 3.0)]
    fake_api = _FakeYouTubeTranscriptApi()
    fake_api.transcripts = [_FakeTranscript("fr", False, snippets)]

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", lambda: fake_api)

    language, segments = fetch_transcript("abc123", ["de"])

    assert language == "fr"
    assert segments[0].text == "Bonjour le monde"


def test_fetch_transcript_falls_back_and_raises_when_everything_empty(monkeypatch):
    monkeypatch.setattr(
        transcripts_youtube, "_fetch_via_transcript_api", lambda video_id, langs: None
    )
    monkeypatch.setattr(
        transcripts_youtube, "_fetch_via_yt_dlp", lambda video_id, langs: None
    )

    with pytest.raises(NoTranscriptAvailable):
        fetch_transcript("abc123", ["en"])
