from app.core.config import settings
from app.web.middleware.ratelimit import RateLimiter


def test_sliding_window() -> None:
    limiter = RateLimiter("t", lambda: 3, window_seconds=60)

    assert [limiter.hit("a", now=t) for t in (0, 10, 20)] == [None, None, None]
    assert limiter.hit("a", now=30) == 30
    assert limiter.hit("b", now=30) is None
    assert limiter.hit("a", now=60.5) is None
    assert limiter.hit("a", now=61) is not None


def test_prune_drops_idle_keys(monkeypatch) -> None:
    monkeypatch.setattr("app.web.middleware.ratelimit.MAX_KEYS", 2)
    limiter = RateLimiter("t", lambda: 5, window_seconds=10)
    for key in "abc":
        limiter.hit(key, now=0)
    limiter.hit("d", now=100)
    assert set(limiter._hits) == {"d"}


async def test_catalog_limited_per_ip(make_client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_catalog_per_minute", 2)
    async with make_client(ip="10.0.0.1") as client:
        statuses = [(await client.get("/")).status_code for _ in range(3)]
        blocked = await client.get("/", headers={"Accept": "text/html"})
        health = await client.get("/health")
    async with make_client(ip="10.0.0.2") as other:
        other_status = (await other.get("/")).status_code

    assert statuses == [200, 200, 429]
    assert blocked.status_code == 429
    assert "Слишком много запросов" in blocked.text
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert health.status_code == 200
    assert other_status == 200


async def test_api_and_login_limited(make_client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_api_per_minute", 1)
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 1)
    async with make_client() as client:
        api = [(await client.get("/api/games")).status_code for _ in range(2)]
        login = [
            (await client.post("/login", data={"login": "x", "password": "y"})).status_code
            for _ in range(2)
        ]
        detail = (await client.get("/api/games")).json()["detail"]
    assert api == [200, 429]
    assert login == [401, 429]
    assert detail.startswith("Слишком много")
