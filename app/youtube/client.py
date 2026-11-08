"""Летсплей игры на YouTube: самый популярный ролик и речь блогера в нём текстом"""

from typing import Protocol

import httpx

from app.core.config import Settings
from app.youtube.search import Candidate, SearchQuota, query_for, rank_candidates, search
from app.youtube.transcript import Transcript, TranscriptFetcher, WhisperTranscriber


class LetsplaySource(Protocol):
    async def find(self, game_title: str) -> list[Candidate]: ...

    async def transcript(self, candidate: Candidate) -> Transcript: ...


class YouTubeLetsplays:
    def __init__(self, config: Settings) -> None:
        self.api_key = config.youtube_api_key
        self.min_duration = config.letsplay_min_duration_minutes * 60
        options = {"quiet": True, "no_warnings": True, "noprogress": True}
        if config.youtube_cookies_file:
            # Cookies аккаунта помогают, когда YouTube просит подтвердить, что запрос не от бота
            options["cookiefile"] = config.youtube_cookies_file
        self.http = httpx.AsyncClient(timeout=30)
        self.quota = SearchQuota()
        self.transcripts = TranscriptFetcher(
            options,
            WhisperTranscriber(
                config.whisper_model,
                config.whisper_threads,
                f"{config.embedding_cache_dir}/whisper",
            ),
            mode=config.letsplay_transcriber,
            max_audio_minutes=config.letsplay_max_audio_minutes,
        )

    async def find(self, game_title: str) -> list[Candidate]:
        """Подходящие ролики от лучшего к худшему, пусто — летсплея нет"""
        found = await search(self.http, self.api_key, query_for(game_title), self.quota)
        return rank_candidates(found, game_title, self.min_duration)

    async def transcript(self, candidate: Candidate) -> Transcript:
        return await self.transcripts.fetch(candidate.url)

    async def aclose(self) -> None:
        await self.http.aclose()
