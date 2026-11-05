"""Исполнитель задач одной группы видов со своим лимитом параллельности"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs.errors import classify
from app.jobs.models import JobStatus
from app.jobs.queue import ClaimedJob, claim, complete, extend_lease, fail, release

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Finish:
    """Итог попытки: done, queued (будет повтор) или dead"""

    status: str
    result: Any = None
    error: BaseException | None = None
    duration_ms: int = 0

    @property
    def error_text(self) -> str | None:
        return f"{type(self.error).__name__}: {self.error}"[:1000] if self.error else None


JobHandler = Callable[[ClaimedJob], Awaitable[Any]]
# Вызывается в той же транзакции, где меняется статус задачи
FinishHook = Callable[[AsyncSession, ClaimedJob, Finish], Awaitable[None]]
# Перед выполнением: время, до которого задачу отложить без траты попытки, или None
Gate = Callable[[ClaimedJob], Awaitable[datetime | None]]
# После завершения, вне транзакции: успех или ошибка этапа, например для автомата
Outcome = Callable[[ClaimedJob, BaseException | None], Awaitable[None]]


@dataclass(frozen=True)
class JobType:
    handler: JobHandler
    on_finish: FinishHook | None = None


class JobPool:
    def __init__(
        self,
        name: str,
        types: dict[str, JobType],
        concurrency: int,
        session_factory: async_sessionmaker[AsyncSession],
        worker: str,
        lease: float,
        poll: float,
        gate: Gate | None = None,
        on_outcome: Outcome | None = None,
    ) -> None:
        self.name = name
        self.types = types
        self.concurrency = concurrency
        self.session_factory = session_factory
        self.worker = worker
        self.lease = lease
        self.poll = poll
        self.gate = gate
        self.on_outcome = on_outcome
        self.wake = asyncio.Event()
        self.active: dict[asyncio.Task, ClaimedJob] = {}

    @property
    def kinds(self) -> list[str]:
        return list(self.types)

    def held(self) -> list[str]:
        return [f"{job.kind}#{job.id}" for job in self.active.values()]

    async def run(self, stop: asyncio.Event) -> None:
        """Берёт задачи, пока не попросят остановиться; текущие доделывает drain"""
        while not stop.is_set():
            # Сбрасываем до выборки: NOTIFY, пришедший во время запроса, не потеряется
            self.wake.clear()
            free = self.concurrency - len(self.active)
            claimed = await self._claim(free) if free > 0 else []
            for job in claimed:
                task = asyncio.create_task(self._execute(job), name=f"job-{job.id}")
                self.active[task] = job
                task.add_done_callback(self._forget)
            await self._sleep(stop)

    def _forget(self, task: asyncio.Task) -> None:
        self.active.pop(task, None)
        self.wake.set()

    async def _sleep(self, stop: asyncio.Event) -> None:
        waiters = [asyncio.create_task(self.wake.wait()), asyncio.create_task(stop.wait())]
        try:
            await asyncio.wait(waiters, timeout=self.poll, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def _claim(self, limit: int) -> list[ClaimedJob]:
        try:
            async with self.session_factory() as session, session.begin():
                return await claim(session, self.worker, self.kinds, limit, self.lease)
        except Exception:
            log.exception("pool %s: не удалось взять задачи", self.name)
            return []

    async def _renew(self, job: ClaimedJob) -> None:
        while True:
            await asyncio.sleep(self.lease / 3)
            try:
                async with self.session_factory() as session, session.begin():
                    if not await extend_lease(session, job.id, self.worker, self.lease):
                        log.warning("job %s: аренда потеряна", job.id)
                        return
            except Exception:
                log.exception("job %s: не удалось продлить аренду", job.id)

    async def _execute(self, job: ClaimedJob) -> None:
        if self.gate and (until := await self.gate(job)):
            async with self.session_factory() as session, session.begin():
                await release(session, job.id, self.worker, run_after=until)
            return

        renew = asyncio.create_task(self._renew(job))
        started = time.monotonic()
        result, error = None, None
        try:
            result = await self.types[job.kind].handler(job)
        except Exception as exc:
            error = exc
            log.warning("job %s %s: %r", job.id, job.kind, exc)
        finally:
            renew.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew
        duration_ms = int((time.monotonic() - started) * 1000)
        await self._finish(job, result, error, duration_ms)
        if self.on_outcome:
            try:
                await self.on_outcome(job, error)
            except Exception:
                log.exception("job %s: ошибка в on_outcome", job.id)

    async def _finish(
        self, job: ClaimedJob, result: Any, error: Exception | None, duration_ms: int
    ) -> None:
        hook = self.types[job.kind].on_finish
        try:
            async with self.session_factory() as session, session.begin():
                if error is None:
                    status = (
                        JobStatus.DONE if await complete(session, job.id, self.worker) else None
                    )
                else:
                    classified = classify(error)
                    text = f"{classified.reason}: {error}"
                    status = await fail(session, job, self.worker, text, classified.retryable)
                if status is None:
                    log.warning("job %s: аренду забрал другой воркер, итог отброшен", job.id)
                    return
                if hook:
                    await hook(session, job, Finish(status, result, error, duration_ms))
        except Exception:
            # Задача останется running, сборщик вернёт её в очередь по истечении аренды
            log.exception("job %s: не удалось записать итог", job.id)

    async def drain(self, grace: float) -> None:
        """Ждёт текущие задачи, оставшиеся отменяет и возвращает в очередь без траты попытки"""
        if not self.active:
            return
        jobs = dict(self.active)
        _, pending = await asyncio.wait(list(jobs), timeout=grace)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if not pending:
            return
        async with self.session_factory() as session, session.begin():
            for task in pending:
                await release(session, jobs[task].id, self.worker)
        log.info("pool %s: возвращено в очередь %d задач", self.name, len(pending))
