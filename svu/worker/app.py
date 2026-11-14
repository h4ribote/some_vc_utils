"""The worker role: one active instance at a time, guarded by a PostgreSQL advisory lock, running every periodic job until SIGTERM."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from svu.config import Settings
from svu.crypto import SecretBox
from svu.db.engine import create_engine
from svu.discord_rest import DiscordRest
from svu.signals import stop_signals
from svu.vc import VCClient
from svu.worker.jobs import Worker
from svu.worker.scheduler import Job, run_jobs

log = logging.getLogger("svu.worker")

# The advisory lock key that makes the worker a singleton; a second worker waits until the first stops.
WORKER_LOCK_KEY = 0x5356_5557
LOCK_RETRY_SECONDS = 10.0


def build_jobs(worker: Worker) -> list[Job]:
    return [
        Job("payouts", 1.0, worker.payouts_tick),
        Job("webhooks", 1.0, worker.webhooks_tick),
        Job("messages", 1.0, worker.messages_tick),
        Job("expire_claims", 10.0, worker.expire_tick),
        Job("reconcile_claims", 30.0, worker.reconcile_tick),
        Job("grant_requests", 3.0, worker.grant_requests_tick),
        Job("refresh_grants", 60.0, worker.refresh_grants_tick),
        Job("vc_sync", 300.0, worker.vc_sync_tick),
        Job("backup", 60.0, worker.backup_tick),
        Job("prune", 3600.0, worker.prune_tick),
    ]


async def acquire_singleton(engine: AsyncEngine, stop: asyncio.Event) -> AsyncConnection | None:
    """Hold the worker advisory lock on a dedicated connection; None when stop was set before it could be taken."""
    while not stop.is_set():
        conn = await engine.connect()
        try:
            if await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK_KEY}):
                await conn.commit()
                return conn
        except Exception:
            await conn.close()
            raise
        await conn.close()
        log.info("another worker holds the lock; waiting")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=LOCK_RETRY_SECONDS)
    return None


async def _run(settings: Settings) -> int:
    settings.require("discord_token", "vc_client_id", "vc_client_secret", "secret_key")
    stop = asyncio.Event()
    engine = create_engine(settings.database_url)
    vc = VCClient(settings.vc_base_url, settings.secret("vc_client_id"), settings.secret("vc_client_secret"))
    discord = DiscordRest(settings.secret("discord_token"))
    try:
        with stop_signals(stop.set):
            lock_conn = await acquire_singleton(engine, stop)
            if lock_conn is None:
                return 0
            try:
                worker = Worker(settings=settings, engine=engine, vc=vc, discord=discord, box=SecretBox(settings.secret("secret_key")))
                log.info("worker started")
                await run_jobs(build_jobs(worker), stop)
            finally:
                await lock_conn.close()
    finally:
        await vc.close()
        await discord.close()
        await engine.dispose()
    log.info("worker stopped")
    return 0


def run_worker(settings: Settings) -> int:
    return asyncio.run(_run(settings))
