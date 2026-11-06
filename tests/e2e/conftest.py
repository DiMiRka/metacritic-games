"""Браузерные сценарии: настоящий uvicorn с фабрикой приложения на тестовой базе и Chromium.
Запуск: pytest -m e2e, браузер ставится один раз: playwright install chromium
"""

import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from tests.conftest import _test_db_url

playwright_api = pytest.importorskip("playwright.async_api")

SECRET = "e2e-" + "0" * 60


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def server(db_engine) -> Iterator[str]:
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": _test_db_url().render_as_string(hide_password=False),
        "SECRET_KEY": SECRET,
        "ENV": "dev",
        "LOG_FORMAT": "text",
        "LOG_LEVEL": "WARNING",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.web.app:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                raise RuntimeError("uvicorn завершился при запуске")
            time.sleep(0.2)
        else:
            raise RuntimeError("uvicorn не ответил на /health")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="session")
async def browser() -> AsyncIterator:
    async with playwright_api.async_playwright() as p:
        try:
            instance = await p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium не установлен: playwright install chromium ({exc})")
        yield instance
        await instance.close()


@pytest.fixture
async def page(browser, server) -> AsyncIterator:
    context = await browser.new_context(base_url=server, locale="ru-RU")
    page = await context.new_page()
    page.set_default_timeout(10_000)
    yield page
    await context.close()
