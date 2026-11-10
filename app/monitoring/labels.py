"""Человекочитаемые подписи для монитора: роли воркеров и автоматы внешних сервисов"""

from dataclasses import dataclass

# Пулы воркера (WORKER_POOLS) и что в них делается
POOL_LABELS = {
    "metacritic": "обход Metacritic",
    "summarize": "резюме, переводы и заключения LLM",
    "embed": "эмбеддинги",
    "letsplay": "поиск и расшифровка летсплеев",
}
ALL_POOLS = "all"

BREAKER_LABELS = {
    "metacritic": ("Metacritic", "обход и карточки игр"),
    "llm": ("LLM", "резюме отзывов, переводы описаний, заключения по летсплеям"),
    "youtube": ("YouTube", "поиск летсплеев и расшифровка речи"),
}
# Какие задачи стоят за каждым автоматом. Повторяет POLICY_BY_KIND из crawler.breakers:
# тот модуль тянет библиотеки LLM, которых нет в образе web. Совпадение проверяет тест
BREAKER_KINDS = {
    "metacritic": ("collect", "scrape_game"),
    "llm": ("summarize", "letsplay_conclude", "translate"),
    "youtube": ("letsplay",),
}
# Настройка порога: сколько сбоев подряд приостанавливают сервис
BREAKER_THRESHOLDS = {
    "metacritic": "breaker_metacritic_threshold",
    "llm": "breaker_llm_threshold",
    "youtube": "breaker_metacritic_threshold",
}


@dataclass(frozen=True)
class WorkerName:
    role: str | None
    title: str
    duties: str
    host: str


def describe_worker(identity: str) -> WorkerName:
    """letsplay@14e5b4ae2b44:1 → «Воркер летсплеев», что он делает и контейнер.
    Записи старого формата (просто hostname:pid) остаются без роли"""
    if "@" not in identity:
        return WorkerName(None, identity, "", identity)
    role, host = identity.split("@", 1)
    pools = [p for p in role.split(",") if p]
    if role == ALL_POOLS:
        title, duties = "Воркер", "все виды задач"
    elif pools == ["letsplay"]:
        title, duties = "Воркер летсплеев", POOL_LABELS["letsplay"]
    else:
        title = "Основной воркер" if "metacritic" in pools else "Воркер"
        duties = ", ".join(POOL_LABELS.get(p, p) for p in pools)
    if host.endswith(":crawl"):
        title += " (команда crawl)"
    return WorkerName(role, title, duties, host.split(":")[0])
