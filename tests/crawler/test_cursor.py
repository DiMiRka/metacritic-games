from app.crawler.cursor import Cursor, collect_batch
from app.crawler.models import CrawlPhase

NR = CrawlPhase.NEW_RELEASES
BROWSE = CrawlPhase.BROWSE


def page(prefix: str, n: int = 24) -> list[str]:
    return [f"{prefix}-{i}" for i in range(n)]


class Site:
    def __init__(self, new_releases: list[str], browse: dict[int, list[str]], done=()) -> None:
        self.new_releases = new_releases
        self.browse = browse
        self.done = set(done)
        self.calls: list[tuple[CrawlPhase, int]] = []

    async def fetch(self, phase: CrawlPhase, page_no: int) -> list[str]:
        self.calls.append((phase, page_no))
        return self.new_releases if phase == NR else self.browse.get(page_no, [])

    async def already_done(self, slugs: list[str]) -> set[str]:
        return self.done & set(slugs)


async def test_new_day_takes_new_releases_then_browse() -> None:
    site = Site(page("nr", 5), {1: page("p1"), 2: page("p2")})
    picked, cursor = await collect_batch(Cursor(NR), site.fetch, site.already_done, 20)

    assert picked == page("nr", 5) + page("p1")[:15]
    assert cursor == Cursor(BROWSE, 1)


async def test_partial_page_is_finished_next_pass() -> None:
    site = Site(page("nr", 5), {1: page("p1"), 2: page("p2")})
    site.done = set(page("nr", 5) + page("p1")[:15])

    picked, cursor = await collect_batch(Cursor(BROWSE, 1), site.fetch, site.already_done, 20)

    assert picked == page("p1")[15:] + page("p2")[:11]
    assert cursor == Cursor(BROWSE, 2)


async def test_new_releases_with_more_than_batch_stays_in_phase() -> None:
    site = Site(page("nr", 25), {1: page("p1")})
    picked, cursor = await collect_batch(Cursor(NR), site.fetch, site.already_done, 20)

    assert picked == page("nr", 20)
    assert cursor == Cursor(NR)
    assert site.calls == [(NR, 1)]


async def test_processed_today_are_skipped() -> None:
    site = Site(page("nr", 4), {1: page("p1")}, done={"nr-1", "p1-0", "p1-2"})
    picked, _ = await collect_batch(Cursor(NR), site.fetch, site.already_done, 6)

    assert picked == ["nr-0", "nr-2", "nr-3", "p1-1", "p1-3", "p1-4"]


async def test_exact_fit_advances_cursor() -> None:
    site = Site([], {1: page("p1", 20), 2: page("p2")})
    picked, cursor = await collect_batch(Cursor(BROWSE, 1), site.fetch, site.already_done, 20)

    assert picked == page("p1", 20)
    assert cursor == Cursor(BROWSE, 2)


async def test_end_of_catalog_stops() -> None:
    site = Site(["a"], {1: ["b", "c"]})
    picked, cursor = await collect_batch(Cursor(NR), site.fetch, site.already_done, 20)

    assert picked == ["a", "b", "c"]
    assert cursor == Cursor(BROWSE, 2)
    assert site.calls[-1] == (BROWSE, 2)


async def test_browse_pages_capped_per_pass() -> None:
    browse = {n: page(f"p{n}") for n in range(1, 30)}
    site = Site([], browse, done={s for slugs in browse.values() for s in slugs})

    picked, cursor = await collect_batch(
        Cursor(BROWSE, 1), site.fetch, site.already_done, 20, max_browse_pages=3
    )

    assert picked == []
    assert cursor == Cursor(BROWSE, 4)
