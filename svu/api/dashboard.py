"""The web dashboard: Discord login, guild reward settings for members who can manage the guild, personal settings, and the bot administrators' page."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from svu import guild_admin, render
from svu.api.discord_oauth import CALLBACK_PATH, OAuthError, can_manage
from svu.api.state import api_state
from svu.claims import start_operation
from svu.core import audit, grants, guilds, operations, payouts, pool, sessions, users, webhooks
from svu.core.sessions import Session
from svu.db.tables import REWARD_TYPES

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
templates.env.filters["amount"] = render.amount

COOKIE = "svu_session"
# The user's guild list and permissions are fetched from Discord again when the cached copy is older than this.
GUILD_CACHE = timedelta(seconds=60)
AMOUNT_MAX = 2**63 - 1

REWARD_LABELS = {"message": "メッセージの送信", "server_join": "サーバーへの参加"}
STATUS_LABELS = {
    "creating": "請求を作成中",
    "awaiting_claim": "承認待ち",
    "paying": "支払い中",
    "done": "完了",
    "partial": "一部失敗",
    "failed": "失敗",
    "denied": "拒否",
    "canceled": "取り消し",
    "expired": "期限切れ",
    "held": "保留",
    "pending": "送金待ち",
    "unknown": "結果不明",
}
PURPOSE_LABELS = {"rain": "エアドロップ", "send": "送金", "reward": "報酬", "refund": "返金", "withdraw": "引き出し"}
ENTRY_LABELS = {"import": "移行", "deposit": "入金", "reward": "報酬", "reward_release": "報酬の取り消し", "withdraw": "引き出し", "withdraw_release": "引き出しの取り消し"}
templates.env.globals.update(REWARD_LABELS=REWARD_LABELS, STATUS_LABELS=STATUS_LABELS, PURPOSE_LABELS=PURPOSE_LABELS, ENTRY_LABELS=ENTRY_LABELS, KIND_LABELS=render.KIND_LABELS)


class LoginRequired(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


class Forbidden(Exception):
    """The viewer may not see or change what the request names; answered 404 so it reveals nothing."""


class BadForm(Exception):
    """A form failed its CSRF or origin check."""


@dataclass(frozen=True)
class Viewer:
    session: Session
    is_admin: bool
    manageable: dict[int, dict[str, Any]]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_next(path: str | None) -> str:
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


async def _session(request: Request) -> Session | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    async with api_state(request).engine.connect() as conn:
        return await sessions.get(conn, token, utcnow())


async def viewer(request: Request) -> Viewer:
    """The logged-in viewer with the guilds they may manage, refreshing the guild list from Discord when it is stale."""
    state = api_state(request)
    session = await _session(request)
    if session is None:
        raise LoginRequired(request.url.path)
    guild_list = session.guilds
    if guild_list is None or session.guilds_fetched_at is None or utcnow() - session.guilds_fetched_at > GUILD_CACHE:
        try:
            guild_list = await state.oauth.guilds(state.box.decrypt(session.discord_token_enc))
        except OAuthError as exc:
            if exc.unauthorized:
                async with state.engine.begin() as conn:
                    await sessions.end(conn, session.token_digest)
                raise LoginRequired(request.url.path) from exc
            if guild_list is None:
                raise
        else:
            async with state.engine.begin() as conn:
                await sessions.store_guilds(conn, session.token_digest, guild_list)
    manageable = {int(g["id"]): g for g in guild_list or [] if can_manage(g)}
    return Viewer(session=session, is_admin=session.user_id in state.settings.admin_ids, manageable=manageable)


ViewerDep = Annotated[Viewer, Depends(viewer)]


async def check_form(request: Request, who: Viewer, csrf_token: str) -> None:
    """Accept a form only with the session's CSRF token and, when the browser names one, this site's origin."""
    origin = request.headers.get("origin")
    if origin is not None and origin != api_state(request).settings.public_base_url:
        raise BadForm
    if not hmac.compare_digest(csrf_token.encode(), who.session.csrf_token.encode()):
        raise BadForm


async def _page(request: Request, who: Viewer | None, template: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
    flash = None
    if who is not None:
        async with api_state(request).engine.begin() as conn:
            flash = await sessions.pop_flash(conn, who.session.token_digest)
    return templates.TemplateResponse(request, template, {"viewer": who, "flash": flash, "csrf": who.session.csrf_token if who else "", **context}, status_code=status_code)


async def _redirect(request: Request, who: Viewer, path: str, level: str | None = None, message: str | None = None) -> RedirectResponse:
    if level is not None and message is not None:
        async with api_state(request).engine.begin() as conn:
            await sessions.set_flash(conn, who.session.token_digest, level, message)
    return RedirectResponse(path, status_code=303)


async def _guild(request: Request, who: Viewer, guild_id: int) -> guilds.BotGuild:
    """The guild when the viewer may manage it and the bot is in it; Forbidden otherwise."""
    if guild_id not in who.manageable:
        raise Forbidden
    async with api_state(request).engine.connect() as conn:
        present = await guilds.bot_guild_map(conn)
    if guild_id not in present:
        raise Forbidden
    return present[guild_id]


# Login.


@router.get("/login")
async def login(request: Request, next: str | None = None) -> RedirectResponse:
    state = api_state(request)
    async with state.engine.begin() as conn:
        oauth_state = await sessions.create_state(conn, _safe_next(next))
    return RedirectResponse(state.oauth.authorize_url(oauth_state), status_code=303)


@router.get(CALLBACK_PATH)
async def callback(request: Request, code: str | None = None, state: str | None = None) -> Response:
    api = api_state(request)
    if not code or not state:
        return await _page(request, None, "error.html", message="ログインが取り消されました。", status_code=400)
    async with api.engine.begin() as conn:
        next_path = await sessions.consume_state(conn, state, utcnow())
    if next_path is None:
        return await _page(request, None, "error.html", message="ログインの有効期限が切れました。もう一度ログインしてください。", status_code=400)
    try:
        token = await api.oauth.exchange(code)
        user = await api.oauth.user(token)
    except OAuthError:
        return await _page(request, None, "error.html", message="Discordでのログインに失敗しました。もう一度お試しください。", status_code=502)
    async with api.engine.begin() as conn:
        session_token = await sessions.create(conn, user_id=user.id, username=user.username, avatar=user.avatar, discord_token_enc=api.box.encrypt(token), now=utcnow())
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(COOKIE, session_token, max_age=int(sessions.SESSION_LIFETIME.total_seconds()), httponly=True, secure=True, samesite="lax", path="/")
    return response


@router.post("/logout")
async def logout(request: Request, who: ViewerDep, csrf_token: Annotated[str, Form()]) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    async with api_state(request).engine.begin() as conn:
        await sessions.end(conn, who.session.token_digest)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


# Pages.


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    if await _session(request) is None:
        return await _page(request, None, "landing.html")
    who = await viewer(request)
    async with api_state(request).engine.connect() as conn:
        present = await guilds.bot_guild_map(conn)
    managed = [present[g] for g in who.manageable if g in present]
    managed.sort(key=lambda g: g.name)
    return await _page(request, who, "index.html", guilds=managed)


@router.get("/guilds/{guild_id}", response_class=HTMLResponse)
async def guild_page(request: Request, guild_id: int, who: ViewerDep) -> HTMLResponse:
    guild = await _guild(request, who, guild_id)
    async with api_state(request).engine.connect() as conn:
        context: dict[str, Any] = {
            "guild": guild,
            "settings": await guilds.settings(conn, guild_id),
            "balances": await pool.balances(conn, guild_id),
            "rules": await guilds.rules(conn, guild_id),
            "grant": await grants.active_grant(conn, guild_id),
            "grant_status": await grants.grant_status(conn, guild_id),
            "pending_request": await grants.pending_request(conn, guild_id),
            "operations": await operations.recent(conn, guild_id=guild_id, limit=20),
            "payouts": await payouts.recent(conn, guild_id=guild_id, limit=20),
            "entries": await pool.entries(conn, guild_id, limit=20),
            "reward_types": REWARD_TYPES,
        }
    return await _page(request, who, "guild.html", **context)


@router.post("/guilds/{guild_id}/rules")
async def guild_rule_set(
    request: Request,
    guild_id: int,
    who: ViewerDep,
    csrf_token: Annotated[str, Form()],
    reward_type: Annotated[str, Form()],
    amount: Annotated[int, Form(ge=1, le=AMOUNT_MAX)],
    cooldown_seconds: Annotated[int, Form(ge=0, le=guilds.MAX_COOLDOWN_SECONDS)],
) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    await _guild(request, who, guild_id)
    path = f"/guilds/{guild_id}"
    try:
        await guild_admin.set_rule(api_state(request).engine, guild_id, reward_type, amount, cooldown_seconds, who.session.user_id)
    except guild_admin.AdminError as exc:
        return await _redirect(request, who, path, "error", str(exc))
    return await _redirect(request, who, path, "success", f"{REWARD_LABELS.get(reward_type, reward_type)} の報酬を保存しました。")


@router.post("/guilds/{guild_id}/rules/delete")
async def guild_rule_delete(request: Request, guild_id: int, who: ViewerDep, csrf_token: Annotated[str, Form()], reward_type: Annotated[str, Form()]) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    await _guild(request, who, guild_id)
    await guild_admin.delete_rule(api_state(request).engine, guild_id, reward_type, who.session.user_id)
    return await _redirect(request, who, f"/guilds/{guild_id}", "success", f"{REWARD_LABELS.get(reward_type, reward_type)} の報酬を削除しました。")


@router.post("/guilds/{guild_id}/unit")
async def guild_unit(request: Request, guild_id: int, who: ViewerDep, csrf_token: Annotated[str, Form()], unit: Annotated[str, Form(min_length=1, max_length=64)]) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    await _guild(request, who, guild_id)
    state = api_state(request)
    path = f"/guilds/{guild_id}"
    try:
        await guild_admin.set_reward_unit(state.engine, state.vc, guild_id, unit.strip(), who.session.user_id)
    except guild_admin.AdminError as exc:
        return await _redirect(request, who, path, "error", str(exc))
    return await _redirect(request, who, path, "success", f"報酬の通貨を {unit.strip()} にしました。")


@router.post("/guilds/{guild_id}/source")
async def guild_source(request: Request, guild_id: int, who: ViewerDep, csrf_token: Annotated[str, Form()], source: Annotated[str, Form()]) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    await _guild(request, who, guild_id)
    state = api_state(request)
    path = f"/guilds/{guild_id}"
    if source == "wallet":
        await guild_admin.use_wallet(state.engine, guild_id, who.session.user_id)
        return await _redirect(request, who, path, "success", "報酬はBotのウォレットのプールから支払われます。")
    if source != "issue":
        return await _redirect(request, who, path, "error", "支払い元が正しくありません。")
    try:
        request_info = await guild_admin.use_issue(state.engine, state.vc, state.box, guild_id, who.session.user_id)
    except guild_admin.AdminError as exc:
        return await _redirect(request, who, path, "error", str(exc))
    if request_info.user_code is None:
        return await _redirect(request, who, path, "success", f"報酬は発行枠から {request_info.unit} で支払われます。")
    return await _redirect(request, who, path, "info", f"サーバーでVirtualCryptoの /grant approve code:{request_info.user_code} を実行して発行を許可してください。")


@router.post("/guilds/{guild_id}/deposit")
async def guild_deposit(
    request: Request,
    guild_id: int,
    who: ViewerDep,
    csrf_token: Annotated[str, Form()],
    unit: Annotated[str, Form(min_length=1, max_length=64)],
    amount: Annotated[int, Form(ge=1, le=AMOUNT_MAX)],
) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    await _guild(request, who, guild_id)
    state = api_state(request)
    op = await start_operation(
        state.engine,
        state.vc,
        kind="deposit",
        requester_id=who.session.user_id,
        unit=unit.strip(),
        deposit_amount=amount,
        guild_id=guild_id,
        timeout_seconds=state.settings.claim_timeout_seconds,
    )
    path = f"/guilds/{guild_id}"
    if op.status == "failed":
        return await _redirect(request, who, path, "error", op.error or "請求を作成できませんでした。")
    if op.claim_id is None:
        return await _redirect(request, who, path, "info", "請求の作成結果を確認しています。結果はDMでお知らせします。")
    return await _redirect(request, who, path, "info", f"請求 {op.claim_id} を発行しました。VirtualCryptoで /claim approve id:{op.claim_id} を実行してください。")


@router.post("/guilds/{guild_id}/withdraw")
async def guild_withdraw(
    request: Request,
    guild_id: int,
    who: ViewerDep,
    csrf_token: Annotated[str, Form()],
    unit: Annotated[str, Form(min_length=1, max_length=64)],
    amount: Annotated[int, Form(ge=1, le=AMOUNT_MAX)],
) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    await _guild(request, who, guild_id)
    path = f"/guilds/{guild_id}"
    try:
        await guild_admin.withdraw(api_state(request).engine, guild_id, unit, amount, who.session.user_id)
    except guild_admin.AdminError as exc:
        return await _redirect(request, who, path, "error", str(exc))
    return await _redirect(request, who, path, "success", f"{render.amount(amount, unit)} をあなたのウォレットに送金します。")


@router.get("/me", response_class=HTMLResponse)
async def me(request: Request, who: ViewerDep) -> HTMLResponse:
    async with api_state(request).engine.connect() as conn:
        receive_dm = await users.receives_dm(conn, who.session.user_id)
        mine = await operations.recent(conn, requester_id=who.session.user_id, limit=20)
    return await _page(request, who, "me.html", receive_dm=receive_dm, operations=mine)


@router.post("/me/dm")
async def me_dm(request: Request, who: ViewerDep, csrf_token: Annotated[str, Form()], enabled: Annotated[str, Form()]) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    value = enabled == "on"
    async with api_state(request).engine.begin() as conn:
        await users.set_receive_dm(conn, who.session.user_id, value)
    return await _redirect(request, who, "/me", "success", "/send のメッセージをDMで受け取ります。" if value else "/send のメッセージを受け取りません。")


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, who: ViewerDep) -> HTMLResponse:
    if not who.is_admin:
        raise Forbidden
    async with api_state(request).engine.connect() as conn:
        app_state = await webhooks.app_state(conn)
        attention = await payouts.needing_attention(conn)
    return await _page(request, who, "admin.html", app_state=app_state, attention=attention)


@router.post("/admin/payouts/{payout_id}/resolve")
async def admin_resolve(request: Request, payout_id: uuid.UUID, who: ViewerDep, csrf_token: Annotated[str, Form()], outcome: Annotated[str, Form()]) -> RedirectResponse:
    await check_form(request, who, csrf_token)
    if not who.is_admin or outcome not in ("paid", "not_paid"):
        raise Forbidden
    async with api_state(request).engine.begin() as conn:
        resolved = await payouts.resolve_unknown(conn, payout_id, outcome)
        if resolved:
            await audit.record(conn, "payout.resolve", actor_id=who.session.user_id, payout_id=str(payout_id), outcome=outcome)
    if not resolved:
        return await _redirect(request, who, "/admin", "error", "その支払いは結果不明の状態ではありません。")
    return await _redirect(request, who, "/admin", "success", "支払いの結果を記録しました。")


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse(f"/login?next={_safe_next(exc.next_path)}", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden) -> HTMLResponse:
        return templates.TemplateResponse(request, "error.html", {"viewer": None, "flash": None, "csrf": "", "message": "ページが見つかりません。"}, status_code=404)

    @app.exception_handler(BadForm)
    async def _bad_form(request: Request, exc: BadForm) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "error.html", {"viewer": None, "flash": None, "csrf": "", "message": "フォームの有効期限が切れました。ページを開き直してください。"}, status_code=400
        )

    @app.exception_handler(OAuthError)
    async def _oauth(request: Request, exc: OAuthError) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "error.html", {"viewer": None, "flash": None, "csrf": "", "message": "Discordに接続できませんでした。しばらくしてからもう一度お試しください。"}, status_code=502
        )
