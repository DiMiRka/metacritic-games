from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.crawler.models import CrawlPhase

ListingFetcher = Callable[[CrawlPhase, int], Awaitable[list[str]]]
DoneFilter = Callable[[list[str]], Awaitable[set[str]]]


@dataclass(frozen=True)
class Cursor:
    phase: CrawlPhase
    next_page: int = 1


async def collect_batch(
    cursor: Cursor,
    fetch: ListingFetcher,
    already_done: DoneFilter,
    batch_size: int,
    max_browse_pages: int = 10,
) -> tuple[list[str], Cursor]:
    """Набирает до batch_size слагов, ещё не обработанных сегодня, и сдвигает курсор

    Страница считается пройденной, только если с неё забрали все свежие игры, иначе
    следующий проход начнёт с неё же и доберёт остаток
    """
    picked: list[str] = []
    browse_pages = 0

    while len(picked) < batch_size:
        if cursor.phase == CrawlPhase.BROWSE:
            if browse_pages >= max_browse_pages:
                break
            browse_pages += 1
        slugs = await fetch(cursor.phase, cursor.next_page)
        if not slugs and cursor.phase == CrawlPhase.BROWSE:
            break

        done = await already_done(slugs) if slugs else set()
        fresh = [s for s in slugs if s not in done and s not in picked]
        room = batch_size - len(picked)
        picked.extend(fresh[:room])

        if len(fresh) <= room:
            if cursor.phase == CrawlPhase.NEW_RELEASES:
                cursor = Cursor(CrawlPhase.BROWSE, 1)
            else:
                cursor = Cursor(CrawlPhase.BROWSE, cursor.next_page + 1)

    return picked, cursor
