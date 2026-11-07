"""Ошибки YouTube в терминах очереди: что повторять, а что сразу считать окончательным"""

from app.core.errors import PermanentError, RetryableError

# YouTube просит подтвердить, что запрос не от бота: частая история для адресов дата-центров
BOT_CHECK_MARKERS = ("confirm you", "not a bot", "sign in to confirm")
UNAVAILABLE_MARKERS = (
    "video unavailable",
    "private video",
    "has been removed",
    "members-only",
    "age-restricted",
    "sign in to confirm your age",
)


class YouTubeBlockedError(RetryableError):
    """YouTube не отдаёт данные этому адресу: нужна пауза, cookies или другой IP"""


class VideoUnavailableError(PermanentError):
    pass


class NoSpeechError(PermanentError):
    """Ни субтитров, ни распознаваемой речи в начале ролика"""


def translate_ytdlp_error(exc: Exception) -> Exception:
    text = str(exc).lower()
    if any(marker in text for marker in BOT_CHECK_MARKERS):
        return YouTubeBlockedError(str(exc)[:300])
    if any(marker in text for marker in UNAVAILABLE_MARKERS):
        return VideoUnavailableError(str(exc)[:300])
    return RetryableError(f"yt-dlp: {str(exc)[:300]}")
