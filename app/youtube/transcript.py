"""Речь блогера текстом: субтитры ролика на языке оригинала, а без них распознавание Whisper.

Субтитры, в том числе автоматические, — это та же расшифровка речи, только уже готовая:
быстро и без нагрузки на процессор. Whisper слушает только начало ролика
(LETSPLAY_MAX_AUDIO_MINUTES): этого хватает, чтобы понять впечатление, и распознавание
остаётся в пределах минут на 2 vCPU. Аудио берётся HTTP-запросом с Range без ffmpeg:
дорожки YouTube в m4a фрагментированные, и начало файла декодируется само по себе
"""

import asyncio
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger
from prometheus_client import Counter

from app.youtube.errors import NoSpeechError, translate_ytdlp_error

TranscriptSource = Literal["captions", "whisper"]
TRANSCRIPTS = Counter(
    "letsplay_transcripts_total", "Расшифровки летсплеев по источнику", ["source"]
)
MIN_TRANSCRIPT_CHARS = 200
# Фразы, которые Whisper выдумывает на тишине и музыке
HALLUCINATIONS = (
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subscribe to my channel",
    "продолжение следует",
    "спасибо за просмотр",
    "субтитры сделал",
    "субтитры создавал",
)


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    source: TranscriptSource


def clean_caption_text(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def json3_text(data: dict[str, Any]) -> str:
    """Текст субтитров YouTube в формате json3"""
    parts = (
        "".join(seg.get("utf8", "") for seg in event.get("segs", []))
        for event in data.get("events", [])
        if event.get("segs")
    )
    return clean_caption_text(" ".join(parts))


def pick_caption_track(info: dict[str, Any]) -> tuple[str, str] | None:
    """(язык, url json3): ручные субтитры языка ролика, иначе автоматические на языке
    оригинала. Переводы на другие языки не берём: это уже не слова блогера"""
    language = info.get("language") or ""
    # en-US в метаданных ролика и en у дорожки субтитров — один язык
    languages = [code for code in dict.fromkeys([language, language.split("-")[0]]) if code]
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    originals = [key for key in auto if key.endswith("-orig")]
    options: list[tuple[dict, str]] = [(manual, code) for code in languages if code in manual]
    options += [(auto, f"{code}-orig") for code in languages if f"{code}-orig" in auto]
    # С автодубляжом у ролика десятки дорожек «-orig» на всех языках: брать любую можно,
    # только если она единственная, иначе это перевод, а не речь блогера
    if len(originals) == 1:
        options.append((auto, originals[0]))
    options += [(auto, code) for code in languages if code in auto]
    for tracks, key in options:
        url = next((t["url"] for t in tracks[key] if t.get("ext") == "json3"), None)
        if url:
            return key.removesuffix("-orig"), url
    return None


def is_hallucination(text: str) -> bool:
    lowered = text.lower().strip(" .!")
    return not lowered or any(phrase in lowered for phrase in HALLUCINATIONS)


def join_segments(segments, max_seconds: float) -> str:
    """Сегменты Whisper до max_seconds: без выдуманных фраз, неуверенных кусков и повторов"""
    kept: list[str] = []
    for segment in segments:
        if segment.start > max_seconds:
            break
        text = segment.text.strip()
        if segment.avg_logprob < -1.0 or segment.no_speech_prob > 0.8 or is_hallucination(text):
            continue
        if kept and kept[-1] == text:
            continue
        kept.append(text)
    return " ".join(kept)


class WhisperTranscriber:
    """faster-whisper на CPU: модель грузится при первом ролике и остаётся в памяти"""

    def __init__(self, model: str, threads: int, cache_dir: str) -> None:
        self.model_name = model
        self.threads = threads
        self.cache_dir = cache_dir
        self._model = None
        self._lock = threading.Lock()

    def transcribe(self, path: str, max_seconds: float) -> tuple[str, str | None]:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=self.threads,
                    download_root=self.cache_dir,
                )
            segments, info = self._model.transcribe(
                path,
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                compression_ratio_threshold=2.4,
            )
            return join_segments(segments, max_seconds), info.language


class TranscriptFetcher:
    def __init__(
        self,
        ytdlp_options: dict,
        whisper: WhisperTranscriber,
        *,
        mode: Literal["auto", "captions", "whisper"],
        max_audio_minutes: int,
    ) -> None:
        self.options = ytdlp_options
        self.whisper = whisper
        self.mode = mode
        self.max_audio_minutes = max_audio_minutes

    def _info(self, ydl, url: str) -> dict[str, Any]:
        import yt_dlp

        try:
            return ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise translate_ytdlp_error(exc) from exc

    def _captions(self, ydl, info: dict[str, Any]) -> Transcript | None:
        track = pick_caption_track(info)
        if track is None:
            return None
        language, url = track
        with ydl.urlopen(url) as response:
            text = json3_text(json.loads(response.read()))
        return Transcript(text, language, "captions") if len(text) >= MIN_TRANSCRIPT_CHARS else None

    def _audio_prefix(self, ydl, info: dict[str, Any], path: str) -> None:
        from yt_dlp.networking.common import Request

        audio = [
            f
            for f in info.get("formats") or []
            if f.get("vcodec") == "none" and f.get("ext") == "m4a" and f.get("url")
        ]
        if not audio:
            raise NoSpeechError("у ролика нет аудиодорожки m4a")
        # При автодубляже дорожек много: исходную yt-dlp помечает высшим language_preference
        best = max(f.get("language_preference") or -1 for f in audio)
        original = [f for f in audio if (f.get("language_preference") or -1) == best]
        fmt = min(original, key=lambda f: f.get("abr") or 1000)
        # Запас 20% на заголовки и колебания битрейта
        limit = int(self.max_audio_minutes * 60 * (fmt.get("abr") or 128) * 1000 / 8 * 1.2)
        headers = {**(fmt.get("http_headers") or {}), "Range": f"bytes=0-{limit}"}
        with ydl.urlopen(Request(fmt["url"], headers=headers)) as response, open(path, "wb") as out:
            while chunk := response.read(1 << 16):
                out.write(chunk)

    def _whisper(self, ydl, info: dict[str, Any]) -> Transcript | None:
        fd, path = tempfile.mkstemp(suffix=".m4a")
        os.close(fd)
        try:
            self._audio_prefix(ydl, info, path)
            text, language = self.whisper.transcribe(path, self.max_audio_minutes * 60)
        finally:
            os.unlink(path)
        return Transcript(text, language, "whisper") if len(text) >= MIN_TRANSCRIPT_CHARS else None

    def _fetch_sync(self, url: str) -> Transcript:
        import yt_dlp

        with yt_dlp.YoutubeDL({**self.options, "skip_download": True}) as ydl:
            info = self._info(ydl, url)
            transcript = None
            if self.mode in ("auto", "captions"):
                transcript = self._captions(ydl, info)
            if transcript is None and self.mode in ("auto", "whisper"):
                logger.info("субтитров нет, распознаю речь Whisper: {}", url)
                transcript = self._whisper(ydl, info)
        if transcript is None:
            raise NoSpeechError("ни субтитров, ни распознаваемой речи в начале ролика")
        TRANSCRIPTS.labels(transcript.source).inc()
        return transcript

    async def fetch(self, url: str) -> Transcript:
        return await asyncio.to_thread(self._fetch_sync, url)
