import io
from types import SimpleNamespace

import pytest

from app.youtube.errors import NoAudioFormatsError, NoSpeechError
from app.youtube.transcript import (
    TranscriptFetcher,
    YtdlpLog,
    join_segments,
    json3_text,
    pick_caption_track,
)


def track(url):
    return [{"ext": "vtt", "url": url + ".vtt"}, {"ext": "json3", "url": url}]


def test_json3_text_drops_tags_and_joins() -> None:
    data = {
        "events": [
            {"segs": [{"utf8": "every single "}, {"utf8": "time"}]},
            {"tStartMs": 5},
            {"segs": [{"utf8": "[Music]"}]},
            {"segs": [{"utf8": "\nFrom Software"}]},
        ]
    }
    assert json3_text(data) == "every single time From Software"


def test_caption_track_prefers_manual_then_original_auto() -> None:
    manual = {
        "language": "en",
        "subtitles": {"en": track("m")},
        "automatic_captions": {"en-orig": track("a")},
    }
    auto = {"language": "en", "automatic_captions": {"ru": track("tr"), "en-orig": track("a")}}
    translated_only = {"language": "en", "automatic_captions": {"ru": track("tr")}}
    assert pick_caption_track(manual) == ("en", "m")
    assert pick_caption_track(auto) == ("en", "a")
    assert pick_caption_track(translated_only) is None


def seg(start, text, logprob=-0.3, no_speech=0.1):
    return SimpleNamespace(start=start, text=text, avg_logprob=logprob, no_speech_prob=no_speech)


def test_join_segments_filters_hallucinations_and_stops_at_limit() -> None:
    segments = [
        seg(0, " Welcome back to the channel"),
        seg(5, " Welcome back to the channel"),
        seg(10, " Thanks for watching!"),
        seg(15, " ммм", logprob=-1.5),
        seg(20, " Music", no_speech=0.9),
        seg(25, " This boss is brutal"),
        seg(9999, " after the limit"),
    ]
    assert (
        join_segments(segments, max_seconds=600)
        == "Welcome back to the channel This boss is brutal"
    )


def test_auto_dubbed_video_uses_original_language_track() -> None:
    dubbed = {
        "language": "en-US",
        "automatic_captions": {
            "ar-orig": track("ar"),
            "en-orig": track("en"),
            "ru-orig": track("ru"),
        },
    }
    single = {"language": None, "automatic_captions": {"ja-orig": track("ja"), "en": track("tr")}}
    ambiguous = {
        "language": None,
        "automatic_captions": {"ar-orig": track("ar"), "en-orig": track("en")},
    }
    assert pick_caption_track(dubbed) == ("en", "en")
    assert pick_caption_track(single) == ("ja", "ja")
    assert pick_caption_track(ambiguous) is None


def fetcher() -> TranscriptFetcher:
    return TranscriptFetcher({}, whisper=None, mode="auto", max_audio_minutes=10)


def test_only_storyboards_is_youtube_refusal_with_ytdlp_reason(tmp_path) -> None:
    log = YtdlpLog()
    log.warning("[youtube] n challenge solving failed: Some formats may be missing")
    info = {"formats": [{"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"}]}

    with pytest.raises(NoAudioFormatsError, match="n challenge solving failed"):
        fetcher()._audio_prefix(None, info, str(tmp_path / "a.m4a"), log)
    with pytest.raises(NoAudioFormatsError, match=r"ни одной аудиодорожки$"):
        fetcher()._audio_prefix(None, {}, str(tmp_path / "a.m4a"), YtdlpLog())


def test_audio_without_m4a_is_video_specific(tmp_path) -> None:
    info = {"formats": [{"ext": "webm", "vcodec": "none", "acodec": "opus", "url": "u"}]}
    with pytest.raises(NoSpeechError):
        fetcher()._audio_prefix(None, info, str(tmp_path / "a.m4a"), YtdlpLog())


class RangeServer:
    """Отдаёт файл по заголовку Range, на запросе номер fail_on обрывает соединение"""

    def __init__(self, data: bytes, fail_on: int | None = None) -> None:
        self.data = data
        self.fail_on = fail_on
        self.ranges: list[str] = []

    def urlopen(self, request):
        from yt_dlp.networking.exceptions import TransportError

        rng = request.headers["Range"]
        self.ranges.append(rng)
        if len(self.ranges) == self.fail_on:
            raise TransportError("Read timed out.")
        start, end = map(int, rng.removeprefix("bytes=").split("-"))
        return io.BytesIO(self.data[start : end + 1])


def audio_info(size: int) -> dict:
    # abr 0.008 — байт в секунду: предел 10 минут * 60 * 1.2 = 720 байт, минимум 180
    fmt = {"ext": "m4a", "vcodec": "none", "acodec": "mp4a", "url": "u", "abr": 0.008,
           "filesize": size, "downloader_options": {"http_chunk_size": 100}}  # fmt: skip
    return {"formats": [fmt]}


def test_audio_is_fetched_in_chunks_up_to_limit(tmp_path) -> None:
    data = bytes(range(256)) * 4
    path = tmp_path / "a.m4a"
    short = RangeServer(data[:250])
    fetcher()._audio_prefix(short, audio_info(250), str(path), YtdlpLog())
    assert short.ranges == ["bytes=0-99", "bytes=100-199", "bytes=200-249"]
    assert path.read_bytes() == data[:250]

    long = RangeServer(data)
    fetcher()._audio_prefix(long, audio_info(len(data)), str(path), YtdlpLog())
    assert long.ranges[-1] == "bytes=700-719"
    assert path.read_bytes() == data[:720]


def test_broken_download_keeps_prefix_if_long_enough(tmp_path) -> None:
    from yt_dlp.networking.exceptions import TransportError

    path = tmp_path / "a.m4a"
    fetcher()._audio_prefix(
        RangeServer(bytes(1000), fail_on=3), audio_info(1000), str(path), YtdlpLog()
    )
    assert path.stat().st_size == 200
    with pytest.raises(TransportError):
        fetcher()._audio_prefix(
            RangeServer(bytes(1000), fail_on=2), audio_info(1000), str(path), YtdlpLog()
        )


def test_ytdlp_log_keeps_last_warnings() -> None:
    log = YtdlpLog()
    for i in range(5):
        log.warning(f"w{i}")
    log.debug("noise")
    log.warning("ffmpeg not found. The downloaded format may not be the best available")
    assert list(log.warnings) == ["w2", "w3", "w4"]
