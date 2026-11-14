"""Periodic jobs in one event loop: each job runs in its own loop, a failing run is logged and retried at the next tick without stopping the others."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger("svu.worker.scheduler")

SHUTDOWN_DRAIN_SECONDS = 10.0


@dataclass(frozen=True)
class Job:
    """tick runs every interval_seconds, measured from the end of the previous run."""

    name: str
    interval_seconds: float
    tick: Callable[[], Awaitable[object]]


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _run_job(job: Job, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await job.tick()
        except Exception:
            log.exception("job failed; it runs again at its next tick", extra={"job": job.name})
        await _sleep(stop, job.interval_seconds)


async def run_jobs(jobs: Sequence[Job], stop: asyncio.Event, drain_seconds: float = SHUTDOWN_DRAIN_SECONDS) -> None:
    """Run every job until stop is set, then give running ticks drain_seconds to finish before cancelling them."""
    tasks = [asyncio.create_task(_run_job(job, stop), name=f"job:{job.name}") for job in jobs]
    try:
        await stop.wait()
    finally:
        _, pending = await asyncio.wait(tasks, timeout=drain_seconds)
        for task in pending:
            log.warning("cancelling a job still running at shutdown", extra={"job": task.get_name()})
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
