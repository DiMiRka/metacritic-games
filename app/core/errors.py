"""Базовые классы ошибок: по ним очередь задач решает, повторять задачу или нет"""

from datetime import datetime


class PermanentError(Exception):
    """Повтор не поможет: игры нет, ответ не прошёл проверку, модель отказалась отвечать"""


class RetryableError(Exception):
    """Временный сбой, задачу стоит повторить позже"""


class DeferredError(RetryableError):
    """Работа невозможна до известного момента, например до сброса суточной квоты.
    Задача ждёт этого момента в очереди, попытка не тратится"""

    def __init__(self, message: str, until: datetime) -> None:
        super().__init__(message)
        self.until = until
