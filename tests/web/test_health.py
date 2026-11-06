from app.web.deps import get_session
from tests.conftest import app


async def test_health_checks_database(make_client) -> None:
    async with make_client() as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


class BrokenSession:
    async def execute(self, *args, **kwargs):
        raise ConnectionRefusedError("db down")


async def test_health_503_when_database_down(make_client) -> None:
    async def broken():
        yield BrokenSession()

    async with make_client() as client:
        app.dependency_overrides[get_session] = broken
        response = await client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "db": "error"}
