"""Ошибки YouTube в терминах очереди: что повторять, а что сразу считать окончательным"""

from app.core.errors import PermanentError, RetryableError

# YouTube просит подтвердить, что запрос не от бота: частая история для адресов дата-центров.
# Только «не бот»: «Sign in to confirm your age» — это возрастное ограничение одного ролика
BOT_CHECK_MARKERS = ("not a bot", "confirm you're not", "confirm you’re not", "confirm that you")
# Проверяются раньше бот-проверки: недоступен именно этот ролик, остальные можно пробовать
UNAVAILABLE_MARKERS = (
    "video unavailable",
    "private video",
    "has been removed",
    "members-only",
    "age-restricted",
    "confirm your age",
    "inappropriate for some users",
)


class YouTubeBlockedError(RetryableError):
    """YouTube не отдаёт данные этому адресу: нужна пауза, cookies или другой IP"""


class YouTubeApiConfigError(RetryableError):
    """Ключ YouTube Data API не принят: неверный, ограничен другим IP или API не включён.
    Повторяемая, чтобы задачи дождались исправления, а автомат youtube открывается сразу"""


class YouTubeApiError(RetryableError):
    """Прочие ошибки API, без URL запроса: в нём ключ"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class VideoUnavailableError(PermanentError):
    pass


class NoSpeechError(PermanentError):
    """Ни субтитров, ни распознаваемой речи в начале ролика"""


def translate_ytdlp_error(exc: Exception) -> Exception:
    text = str(exc).lower()
    if any(marker in text for marker in UNAVAILABLE_MARKERS):
        return VideoUnavailableError(str(exc)[:300])
    if any(marker in text for marker in BOT_CHECK_MARKERS):
        return YouTubeBlockedError(str(exc)[:300])
    return RetryableError(f"yt-dlp: {str(exc)[:300]}")
