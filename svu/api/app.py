"""The API role's application: /health, the VirtualCrypto webhook and the dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from svu.api import dashboard, webhook
from svu.api.discord_oauth import CALLBACK_PATH, DiscordOAuth
from svu.api.state import ApiState, api_state
from svu.config import Settings
from svu.crypto import SecretBox
from svu.db.engine import create_engine
from svu.vc import VCClient

STATIC_DIR = Path(__file__).resolve().parent / "static"

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' https://cdn.discordapp.com; style-src 'self'; script-src 'none'; frame-ancestors 'none'; form-action 'self' https://discord.com",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


def build_state(settings: Settings) -> ApiState:
    settings.require("public_base_url", "secret_key", "discord_client_id", "discord_client_secret", "vc_client_id", "vc_client_secret")
    assert settings.public_base_url is not None and settings.discord_client_id is not None
    return ApiState(
        settings=settings,
        engine=create_engine(settings.database_url),
        vc=VCClient(settings.vc_base_url, settings.secret("vc_client_id"), settings.secret("vc_client_secret")),
        box=SecretBox(settings.secret("secret_key")),
        oauth=DiscordOAuth(settings.discord_client_id, settings.secret("discord_client_secret"), settings.public_base_url + CALLBACK_PATH),
    )


def create_app(state: ApiState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await state.vc.close()
            await state.oauth.close()
            await state.engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.svu = state

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path not in ("/health",) and not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        """200 when the database answers, 503 otherwise; compose uses it as the API's healthcheck."""
        try:
            async with api_state(request).engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    app.include_router(webhook.router)
    app.include_router(dashboard.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    dashboard.install_handlers(app)
    return app
