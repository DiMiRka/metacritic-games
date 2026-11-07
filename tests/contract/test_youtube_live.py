"""Контракт с настоящим YouTube: поиск летсплея, субтитры и распознавание речи Whisper.
Нужна сеть, запуск: RUN_LIVE=1 pytest -m live. Тест Whisper в первый раз скачивает модель
"""

import os

import pytest

from app.core.config import Settings
from app.youtube.client import YouTubeLetsplays

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="нужен RUN_LIVE=1 и сеть"),
]


def letsplays(**overrides) -> YouTubeLetsplays:
    return YouTubeLetsplays(Settings(timezone="UTC", **overrides))


async def test_most_popular_letsplay_and_captions() -> None:
    source = letsplays(letsplay_transcriber="captions")
    try:
        candidate = await source.find("Elden Ring")
        assert candidate is not None
        assert candidate.views and candidate.views > 100_000
        assert candidate.duration_seconds >= 600
        transcript = await source.transcript(candidate)
    finally:
        await source.aclose()
    assert transcript.source == "captions"
    assert len(transcript.text) > 2000
    assert transcript.language


async def test_whisper_transcribes_start_of_video() -> None:
    source = letsplays(letsplay_transcriber="whisper", letsplay_max_audio_minutes=1)
    try:
        candidate = await source.find("Elden Ring")
        transcript = await source.transcript(candidate)
    finally:
        await source.aclose()
    assert transcript.source == "whisper"
    assert len(transcript.text) > 200
    assert transcript.language == "en"
