"""Admin GUI of social-media-reuploader (``uvicorn src.gui:app``).

The GUI runs as a separate process (its own pod) and shares state with the
bot ONLY through the SQLite database:

* everything shown is read from the DB (:mod:`src.gui_queries`), including the
  worker's liveness, running jobs and effective limits, which the worker
  publishes as a heartbeat row (``db.get_worker_status()``);
* every admin action is a DB write (``db.cancel_queued``,
  ``db.requeue_requests``, ``db.reset_chat_limit``, ``db.prune``, ...);
* live updates come from one background task that tails the DB event log and
  broadcasts each event once to the WebSocket clients (:mod:`src.ws_broadcast`).

Authentication (:mod:`src.gui_auth`): ``ADMIN_TOKEN`` (bearer header or
``POST /login``) and/or OAuth/OIDC. Admin rights are re-derived on every
request. Unsafe cookie-authenticated requests must be same-origin (CSRF).

DB-bound handlers are plain ``def`` (FastAPI runs them in its threadpool), so
the event loop serving WebSockets and ``/health`` is never blocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import re
import sqlite3
import string
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

import anyio
import fastapi
import httpx
from authlib.common.errors import AuthlibBaseError
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from joserfc.errors import JoseError
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from . import __version__, config, db, gui_auth, gui_queries, ws_broadcast
from .gui_auth import AuthError, AuthMethod, AuthSettings, Principal

logger = logging.getLogger(__name__)

SRC_DIR = Path(__file__).resolve().parent
STATIC_DIR = SRC_DIR / "static"
TEMPLATES_DIR = SRC_DIR / "templates"

# Pinned CDN assets (static package files, so Subresource Integrity is stable).
BOOTSTRAP_CSS_URL = (
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css"
)
BOOTSTRAP_CSS_SRI = (
    "sha384-T3c6CoIi6uLrA9TneNEoa7RxnatzjcDSCmG1MXxSR1GAsXEV/Dwwykc2MPK8M2HN"
)
CHART_JS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.js"
CHART_JS_SRI = "sha384-FcQlsUOd0TJjROrBxhJdUhXTUgNJQxTMcxZe6nHbaEfFL1zjQ+bq/uRoBQxb0KMo"
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        f"script-src 'self' {CHART_JS_URL}",
        f"style-src 'self' {BOOTSTRAP_CSS_URL}",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"

MAX_PAGE_SIZE = 200
MAX_LOGIN_BODY = 4096
MAX_REQUEUE_IDS = 1000
FILTER_REQUEUE_LIMIT = 500
DEFAULT_REQUEUE_MAX_AGE = 86400.0
# db.prune() deletes rows older than N days: ~0.1 ms means "everything so far".
CLEAR_UP_TO_NOW_DAYS = 1e-9
WS_RECHECK_SECONDS = 30.0
WS_CLOSE_GOING_AWAY = 1001
WS_CLOSE_POLICY = 1008
WS_CLOSE_INTERNAL = 1011
WS_CLOSE_TRY_AGAIN = 1013
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
_STATUS_RE = re.compile(r"^[a-z_]{1,32}$")


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------
@dataclass
class GuiState:
    settings: AuthSettings
    oauth: Any  # authlib OAuth registry (or a test double)
    hub: ws_broadcast.Hub
    tailer: ws_broadcast.EventTailer
    limiter: gui_auth.FailureLimiter
    index_html: str
    asset_version: str
    ws_recheck_seconds: float
    requeue_max_age: float

    @property
    def oauth_client(self) -> Any | None:
        return gui_auth.get_oauth_client(self.oauth)


def _state(conn: HTTPConnection) -> GuiState:
    return conn.app.state.gui


def _resolve(conn: HTTPConnection) -> Principal:
    """The admin check (raises ``AuthError``); sync, may query the DB."""
    state = _state(conn)
    principal = gui_auth.resolve_principal(
        conn,
        state.settings,
        oauth_enabled=state.oauth_client is not None,
        limiter=state.limiter,
    )
    if not principal.authenticated:
        _drop_stale_session(conn)
    return principal


def _drop_stale_session(conn: HTTPConnection) -> None:
    """Forget an invalid/expired identity (ours or the old GUI's) in the cookie.

    authlib's in-flight OAuth state (``_state_*`` keys) is kept.
    """
    session = conn.scope.get("session")
    if not isinstance(session, dict):
        return
    if not any(key in session for key in ("v", "is_admin", "user")):
        return
    for key in [key for key in session if not key.startswith("_state_")]:
        session.pop(key, None)


def current_principal(request: Request) -> Principal:
    """FastAPI dependency (threadpool): the caller, possibly anonymous."""
    try:
        return _resolve(request)
    except AuthError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=exc.headers) from exc


async def require_admin(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.authenticated:
        raise HTTPException(
            401, "Autenticazione richiesta", headers={"WWW-Authenticate": "Bearer"}
        )
    if not principal.is_admin:
        raise HTTPException(403, "Privilegi di amministratore richiesti")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


# ---------------------------------------------------------------------------
# HTML pages (no Jinja: tiny string templates, every value escaped)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _template(name: str) -> string.Template:
    return string.Template((TEMPLATES_DIR / name).read_text(encoding="utf-8"))


def _page(state: GuiState, name: str, status_code: int, **values: str) -> HTMLResponse:
    common = {
        "bootstrap_css": BOOTSTRAP_CSS_URL,
        "bootstrap_sri": BOOTSTRAP_CSS_SRI,
        "asset_version": state.asset_version,
    }
    body = _template(name).substitute(common | values)
    return HTMLResponse(body, status_code=status_code)


def error_page(
    conn: HTTPConnection, status_code: int, title: str, message: str
) -> HTMLResponse:
    state = _state(conn)
    token_login = ""
    if state.settings.token_enabled:
        token_login = (
            '<a class="btn btn-outline-primary" href="login?method=token">'
            "Accedi con il token</a>"
        )
    return _page(
        state,
        "error.html",
        status_code,
        title=html.escape(title),
        message=html.escape(message),
        token_login=token_login,
    )


def login_form(
    conn: HTTPConnection, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    block = ""
    if error:
        block = (
            f'<div class="alert alert-danger" role="alert">{html.escape(error)}</div>'
        )
    return _page(_state(conn), "login.html", status_code, error_block=block)


def _wants_json(request: Request) -> bool:
    content_type = request.headers.get("content-type", "").lower()
    accept = request.headers.get("accept", "").lower()
    return "application/json" in content_type or (
        "application/json" in accept and "text/html" not in accept
    )


# ---------------------------------------------------------------------------
# Public routes: shell, health, auth
# ---------------------------------------------------------------------------
public = APIRouter()


@public.get("/", include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    return HTMLResponse(_state(request).index_html)


@public.get("/health")
def health(request: Request) -> JSONResponse:
    """Readiness: the DB answers. Also reports the worker heartbeat state."""
    state = _state(request)
    try:
        gui_queries.ping()
        worker = gui_queries.load_worker()
    except sqlite3.Error as exc:
        logger.warning("Health check failed: database error: %s", exc)
        return JSONResponse({"ok": False, "db": "error"}, status_code=503)
    body: dict[str, Any] = {"ok": True, "db": "ok", "worker": worker["state"]}
    if getattr(config, "HEALTH_DEBUG", False):
        body.update(
            {
                "version": __version__,
                "worker_age_seconds": worker["age_seconds"],
                "ws_clients": state.hub.client_count,
                "last_event_id": state.tailer.last_id,
                "tailer_running": state.tailer.running,
            }
        )
    return JSONResponse(body)


@public.get("/config")
async def public_config(request: Request) -> dict[str, Any]:
    """Login methods, needed by the browser before authenticating."""
    state = _state(request)
    return {
        "oauth_configured": state.oauth_client is not None,
        "admin_token_set": state.settings.token_enabled,
    }


@public.get("/api/me")
def api_me(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict[str, Any]:
    return principal.public()


def _oauth_timeout(state: GuiState) -> float:
    oauth = state.settings.oauth
    return oauth.timeout if oauth else gui_auth.DEFAULT_OAUTH_TIMEOUT


def callback_url(conn: HTTPConnection, settings: AuthSettings) -> str:
    if settings.oauth and settings.oauth.redirect_uri:
        return settings.oauth.redirect_uri
    return gui_auth.external_base_url(conn) + "/auth"


@public.get("/login", include_in_schema=False)
async def login_page(
    request: Request, method: Annotated[str | None, Query(max_length=16)] = None
) -> Response:
    """Start the OAuth flow, or show the admin-token form.

    ``?method=token`` shows the token form even when OAuth is configured (no
    JavaScript, or the identity provider is down).
    """
    state = _state(request)
    if method == "token":
        if state.settings.token_enabled:
            return login_form(request)
        return error_page(
            request,
            400,
            "Accesso con token non disponibile",
            "ADMIN_TOKEN non è configurato: accedi con il provider di identità.",
        )
    client = state.oauth_client
    if client is not None:
        try:
            async with asyncio.timeout(_oauth_timeout(state) * 2):
                return await client.authorize_redirect(
                    request, callback_url(request, state.settings)
                )
        except (
            AuthlibBaseError,
            httpx.HTTPError,
            TimeoutError,
            RuntimeError,
            ValueError,
        ) as exc:
            logger.warning("Cannot start the OAuth login: %r", exc)
            alternative = (
                " o accedi con il token amministratore"
                if state.settings.token_enabled
                else ""
            )
            return error_page(
                request,
                502,
                "Accesso non disponibile",
                "Il provider di identità non risponde o è configurato male."
                f" Riprova più tardi{alternative}.",
            )
    if state.settings.token_enabled:
        return login_form(request)
    raise HTTPException(400, "Nessun metodo di accesso configurato")


async def _read_login_token(request: Request) -> str | None:
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_LOGIN_BODY:
            raise HTTPException(413, "Richiesta troppo grande")
        chunks.append(chunk)
    body = b"".join(chunks)
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    content_type = content_type.strip().lower()
    token: Any = None
    if content_type == "application/json":
        try:
            data = json.loads(body or b"null")
        except ValueError:
            return None
        token = data.get("token") if isinstance(data, dict) else None
    elif content_type in ("application/x-www-form-urlencoded", ""):
        values = parse_qs(body.decode("utf-8", "replace")).get("token")
        token = values[0] if values else None
    return token.strip() if isinstance(token, str) else None


def _login_failure(
    request: Request,
    status_code: int,
    detail: str,
    *,
    as_json: bool,
    headers: Mapping[str, str] | None = None,
) -> Response:
    if as_json:
        return JSONResponse(
            {"detail": detail}, status_code=status_code, headers=headers
        )
    response = login_form(request, error=detail, status_code=status_code)
    if headers:
        response.headers.update(headers)
    return response


@public.post("/login", include_in_schema=False)
async def login_submit(request: Request) -> Response:
    """Admin-token login: creates a session (JSON for fetch, redirect for forms)."""
    state = _state(request)
    as_json = _wants_json(request)
    if not state.settings.token_enabled:
        return _login_failure(
            request, 400, "Accesso con token non configurato", as_json=as_json
        )
    key = gui_auth.client_key(request)
    retry = state.limiter.retry_after(key)
    if retry is not None:
        seconds = max(1, int(retry + 0.999))
        return _login_failure(
            request,
            429,
            f"Troppi tentativi falliti: riprova tra {seconds} secondi",
            as_json=as_json,
            headers={"Retry-After": str(seconds)},
        )
    token = await _read_login_token(request)
    expected = state.settings.admin_token or ""
    if not token or not gui_auth.tokens_match(token, expected):
        state.limiter.record_failure(key)
        logger.warning("Failed admin-token login from %s", key)
        return _login_failure(request, 401, "Token non valido", as_json=as_json)
    state.limiter.reset(key)
    gui_auth.start_token_session(request.session, state.settings)
    logger.info("Admin-token login from %s", key)
    plain_http = gui_auth.external_base_url(request).startswith("http://")
    if state.settings.cookie_secure and plain_http:
        logger.warning(
            "Admin-token login from %s over plain HTTP with SESSION_COOKIE_SECURE=true:"
            " the browser will not store the session cookie (serve the GUI over HTTPS"
            " or check X-Forwarded-Proto behind a proxy)",
            key,
        )
    if as_json:
        principal = Principal(method=AuthMethod.TOKEN, is_admin=True)
        return JSONResponse({"ok": True, "me": principal.public()})
    return RedirectResponse("./", status_code=303)


@public.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    request.session.clear()
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse("./", status_code=303)


class LoginFailure(Exception):
    """An OAuth callback problem shown to the user as an error page."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def _has_userinfo_endpoint(client: Any) -> bool:
    load = getattr(client, "load_server_metadata", None)
    if load is None:
        return False
    metadata = await load()
    return bool(isinstance(metadata, Mapping) and metadata.get("userinfo_endpoint"))


async def _oauth_claims(
    client: Any, token: Mapping[str, Any], settings: AuthSettings
) -> dict[str, Any]:
    """Identity claims: verified ID-token claims, completed by userinfo.

    Userinfo is fetched through the provider client (timeout, status check)
    when OAUTH_USERINFO_URL is set or when there is no ID token; its ``sub``
    must match the ID token's.
    """
    claims = dict(token.get("userinfo") or {})
    wants_userinfo = bool(settings.oauth and settings.oauth.userinfo_url)
    if not wants_userinfo and not claims:
        wants_userinfo = await _has_userinfo_endpoint(client)
    if wants_userinfo and hasattr(client, "userinfo"):
        raw = await client.userinfo(token=token)
        if not isinstance(raw, Mapping):
            raise LoginFailure(502, "Il provider ha restituito un profilo non valido.")
        info = dict(raw)
        sub, info_sub = claims.get("sub"), info.get("sub")
        if sub is not None and info_sub is not None and str(sub) != str(info_sub):
            raise LoginFailure(
                400, "Il profilo restituito dal provider non corrisponde all'ID token."
            )
        info.update(claims)  # ID-token claims win
        claims = info
    if not claims:
        legacy = token.get("user") or token.get("id_token_claims")
        claims = dict(legacy) if isinstance(legacy, Mapping) else {}
    return claims


@public.get("/auth", include_in_schema=False)
async def oauth_callback(request: Request) -> Response:
    """OAuth/OIDC redirect target: verify, then start a fresh session."""
    state = _state(request)
    client = state.oauth_client
    if client is None:
        return error_page(
            request, 400, "Accesso non disponibile", "OAuth non è configurato."
        )
    try:
        async with asyncio.timeout(_oauth_timeout(state) * 2):
            token = await client.authorize_access_token(request)
            claims = await _oauth_claims(client, token, state.settings)
    except LoginFailure as exc:
        return error_page(request, exc.status_code, "Accesso non riuscito", exc.message)
    except (AuthlibBaseError, JoseError) as exc:
        logger.warning("OAuth callback refused: %r", exc)
        return error_page(
            request,
            400,
            "Accesso non riuscito",
            "Il provider ha rifiutato l'accesso o la risposta non è valida"
            " (sessione scaduta?). Riprova.",
        )
    except TimeoutError:
        logger.warning("OAuth callback timed out")
        return error_page(
            request,
            504,
            "Accesso non riuscito",
            "Il provider non ha risposto in tempo.",
        )
    except httpx.HTTPError as exc:
        logger.warning("OAuth provider request failed: %r", exc)
        return error_page(
            request,
            502,
            "Accesso non riuscito",
            "Impossibile contattare il provider di identità o risposta di errore.",
        )
    except RuntimeError as exc:  # e.g. 'Missing "jwks_uri" in metadata'
        logger.error("OAuth/OIDC is misconfigured: %s", exc)
        return error_page(
            request,
            500,
            "Configurazione OAuth incompleta",
            "Impossibile verificare l'ID token: imposta OAUTH_JWKS_URI oppure"
            " OAUTH_SERVER_METADATA_URL.",
        )
    except ValueError as exc:
        logger.warning("Invalid OAuth provider response: %r", exc)
        return error_page(
            request, 502, "Accesso non riuscito", "Risposta del provider non valida."
        )
    identity = gui_auth.OAuthIdentity.from_claims(claims)
    if identity is None:
        return error_page(
            request,
            400,
            "Accesso non riuscito",
            "Il provider non ha restituito un'identità utilizzabile.",
        )
    gui_auth.start_oauth_session(request.session, identity, state.settings)
    logger.info(
        "OAuth login: %s", identity.email or identity.username or identity.subject
    )
    _warn_unusable_email(identity, state.settings)
    return RedirectResponse("./", status_code=302)


def _warn_unusable_email(
    identity: gui_auth.OAuthIdentity, settings: AuthSettings
) -> None:
    """Tell the operator why an OAuth e-mail will not map to its DB role."""
    if not identity.email or settings.email_trusted(identity.email_verified):
        return
    if identity.email_verified is False:
        logger.warning(
            "OAuth login %s: the provider marks the e-mail as unverified,"
            " so it does not map to a DB user role",
            identity.email,
        )
        return
    logger.warning(
        "OAuth login %s: the provider sent no email_verified claim, so the e-mail"
        " does not map to a DB user role; set OAUTH_ASSUME_EMAIL_VERIFIED=true if"
        " the provider only returns verified addresses (e.g. GitHub)",
        identity.email,
    )


# ---------------------------------------------------------------------------
# WebSocket: live updates
# ---------------------------------------------------------------------------
@public.websocket("/ws/updates")
async def ws_updates(websocket: WebSocket) -> None:
    """Authenticated (before accept) stream of event-log batches."""
    state = _state(websocket)
    principal = await _ws_principal(websocket)
    if principal is None or not principal.is_admin:
        await websocket.close(code=WS_CLOSE_POLICY)
        return
    if principal.method is not AuthMethod.BEARER and not gui_auth.origin_allowed(
        websocket.headers, state.settings
    ):
        logger.warning(
            "Refused a cross-origin WebSocket (Origin=%r)",
            websocket.headers.get("origin"),
        )
        await websocket.close(code=WS_CLOSE_POLICY)
        return
    await websocket.accept()
    subscription = state.hub.subscribe()
    try:
        await websocket.send_json(
            {
                "kind": "hello",
                "last_event_id": state.tailer.last_id,
                "server_time": db.now_iso(),
                "version": __version__,
            }
        )
        code = await _serve_ws(websocket, subscription, state)
    finally:
        state.hub.unsubscribe(subscription)
    await _close_ws(websocket, code)


async def _ws_principal(websocket: WebSocket) -> Principal | None:
    try:
        return await db.aio(_resolve, websocket)
    except AuthError:
        return None
    except sqlite3.Error as exc:
        logger.warning("WebSocket auth failed: database error: %s", exc)
        return None


async def _ws_receive(websocket: WebSocket) -> None:
    """Read (and ignore) client frames so a disconnect is noticed at once."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _ws_send(
    websocket: WebSocket, subscription: ws_broadcast.Subscription
) -> None:
    while True:
        message = await subscription.get()
        if message is None:
            return
        await websocket.send_json(message)


async def _ws_watch(websocket: WebSocket, state: GuiState) -> None:
    """Re-check authorization periodically; return when it is gone."""
    while True:
        await asyncio.sleep(state.ws_recheck_seconds)
        try:
            principal = await db.aio(_resolve, websocket)
        except AuthError:
            return
        except sqlite3.Error as exc:
            logger.warning("WebSocket re-authorization skipped: %s", exc)
            continue
        if not principal.is_admin:
            return


async def _serve_ws(
    websocket: WebSocket, subscription: ws_broadcast.Subscription, state: GuiState
) -> int | None:
    """Run receiver, sender and watcher until one ends; the close code to send.

    An anyio task group (not bare asyncio tasks) so that cancellation from the
    server (shutdown) or the client propagates through the cancel scopes.
    """
    finished: list[str] = []
    async with anyio.create_task_group() as group:

        async def run(
            name: str, job: Callable[..., Awaitable[None]], *args: Any
        ) -> None:
            try:
                await job(*args)
            except (WebSocketDisconnect, OSError, RuntimeError):
                name = "gone"  # the client went away while we were talking to it
            except Exception:  # connection boundary: log it and close with 1011
                logger.exception("Live-update connection failed")
                name = "error"
            finished.append(name)
            group.cancel_scope.cancel()

        group.start_soon(run, "receiver", _ws_receive, websocket)
        group.start_soon(run, "sender", _ws_send, websocket, subscription)
        group.start_soon(run, "watcher", _ws_watch, websocket, state)
    reason = finished[0] if finished else "gone"
    if reason == "sender":
        return WS_CLOSE_TRY_AGAIN if subscription.dropped else WS_CLOSE_GOING_AWAY
    return {"watcher": WS_CLOSE_POLICY, "error": WS_CLOSE_INTERNAL}.get(reason)


async def _close_ws(websocket: WebSocket, code: int | None) -> None:
    if code is None:
        return
    if (
        websocket.application_state is WebSocketState.CONNECTED
        and websocket.client_state is WebSocketState.CONNECTED
    ):
        with contextlib.suppress(RuntimeError, OSError):
            await websocket.close(code=code)


# ---------------------------------------------------------------------------
# Admin API (every route requires an administrator)
# ---------------------------------------------------------------------------
api = APIRouter(dependencies=[Depends(require_admin)])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


# SQLite integers are 64-bit: binding a larger id raises OverflowError (a 500),
# so every id, chat id and offset that reaches SQL is range-checked (422).
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
DbInt = Annotated[int, Field(ge=INT64_MIN, le=INT64_MAX)]
DbIntQuery = Annotated[int | None, Query(ge=INT64_MIN, le=INT64_MAX)]
DbIdPath = Annotated[int, fastapi.Path(ge=INT64_MIN, le=INT64_MAX)]
OffsetQuery = Annotated[int, Query(ge=0, le=INT64_MAX)]


class RequeueBody(_Body):
    ids: list[DbInt] | None = Field(default=None, max_length=MAX_REQUEUE_IDS)
    status: str | None = Field(default=None, max_length=32)
    q: str | None = Field(default=None, max_length=200)
    chat_id: DbInt | None = None


class CancelBody(_Body):
    chat_id: DbInt | None = None


class UnlimitBody(_Body):
    chat_id: DbInt
    requeue: bool = False


class UnlimitAllBody(_Body):
    requeue: bool = False


class UserCreateBody(_Body):
    email: str = Field(max_length=320)
    username: str | None = Field(default=None, max_length=128)
    role: str = Field(default="user", max_length=16)


class UserRoleBody(_Body):
    role: str = Field(max_length=16)


def _statuses(raw: str | None) -> list[str] | None:
    """``status`` query value (comma-separated) -> list, or None for all."""
    if raw is None:
        return None
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not values or "all" in values:
        return None
    if len(values) > 12 or not all(_STATUS_RE.match(item) for item in values):
        raise HTTPException(400, "Filtro di stato non valido")
    return values


def _query_text(raw: str | None) -> str | None:
    text = (raw or "").strip()
    return text or None


@api.get("/api/requests")
@api.get("/requests", include_in_schema=False)
def list_requests(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: OffsetQuery = 0,
    status: Annotated[str | None, Query(max_length=400)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    chat_id: DbIntQuery = None,
) -> dict[str, Any]:
    """Requests, newest first; ``q`` searches URL, description and chat id."""
    return gui_queries.requests_page(
        limit=limit,
        offset=offset,
        status=_statuses(status),
        q=_query_text(q),
        chat_id=chat_id,
    )


@api.get("/api/requests/{request_id}")
@api.get("/requests/{request_id}", include_in_schema=False)
def request_detail(request_id: DbIdPath) -> dict[str, Any]:
    detail = gui_queries.request_detail(request_id)
    if detail is None:
        raise HTTPException(404, "Richiesta non trovata")
    return detail


@api.post("/api/requests/requeue")
def requeue_requests(body: RequeueBody, principal: AdminPrincipal) -> dict[str, Any]:
    """Requeue failed/refused/cancelled requests by id, or by filter."""
    if body.ids:
        ids = list(dict.fromkeys(body.ids))
    elif body.status:
        status = body.status.strip().lower()
        if status not in db.REQUEUEABLE_STATUSES:
            raise HTTPException(400, "Questo stato non può essere rimesso in coda")
        rows = db.list_requests(
            FILTER_REQUEUE_LIMIT,
            0,
            status=status,
            chat_id=body.chat_id,
            q=_query_text(body.q),
        )
        ids = [int(row["id"]) for row in rows]
    else:
        raise HTTPException(400, "Indica gli id oppure un filtro di stato")
    requeued = db.requeue_requests(ids, actor=principal.actor) if ids else []
    done = set(requeued)
    logger.info("%s requeued %d of %d request(s)", principal.actor, len(done), len(ids))
    return {
        "ok": True,
        "requested": len(ids),
        "requeued": requeued,
        "skipped": [request_id for request_id in ids if request_id not in done],
    }


@api.get("/api/queue")
def queue(limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50) -> dict[str, Any]:
    return gui_queries.queue_view(limit)


@api.post("/api/queue/cancel")
def cancel_queue(
    principal: AdminPrincipal, body: CancelBody | None = None
) -> dict[str, Any]:
    """Cancel every queued (and deferred) request, or those of one chat."""
    chat_id = body.chat_id if body else None
    ids = db.cancel_queued(chat_id=chat_id, actor=principal.actor)
    logger.info(
        "%s cancelled %d queued request(s) (chat %s)",
        principal.actor,
        len(ids),
        chat_id,
    )
    return {"ok": True, "chat_id": chat_id, "cancelled": len(ids), "ids": ids}


@api.get("/api/worker")
def worker_status() -> dict[str, Any]:
    return gui_queries.load_worker()


@api.get("/api/rate_limits")
def rate_limits(request: Request) -> dict[str, Any]:
    view = gui_queries.rate_limit_view()
    # How old a refusal may be for "Sblocca e rimetti in coda" to requeue it.
    view["requeue_max_age_seconds"] = _state(request).requeue_max_age
    return view


def _requeue_refused(state: GuiState, *, chat_id: int | None, actor: str) -> list[int]:
    """Requeue the latest refused request per link (recent ones only)."""
    since = gui_queries.iso(time.time() - state.requeue_max_age) or db.now_iso()
    ids = gui_queries.latest_refused_per_link(since_iso=since, chat_id=chat_id)
    return db.requeue_requests(ids, actor=actor) if ids else []


@api.post("/api/rate_limits/unlimit")
def unlimit_chat(
    body: UnlimitBody, request: Request, principal: AdminPrincipal
) -> dict[str, Any]:
    """Reset the chat's rate-limit window; optionally requeue its refusals."""
    db.reset_chat_limit(body.chat_id, actor=principal.actor)
    requeued = []
    if body.requeue:
        requeued = _requeue_refused(
            _state(request), chat_id=body.chat_id, actor=principal.actor
        )
    logger.info(
        "%s unlimited chat %s (requeued %d)",
        principal.actor,
        body.chat_id,
        len(requeued),
    )
    return {"ok": True, "chat_id": body.chat_id, "requeued": requeued}


@api.post("/api/rate_limits/unlimit_all")
def unlimit_all(
    request: Request, principal: AdminPrincipal, body: UnlimitAllBody | None = None
) -> dict[str, Any]:
    count = db.reset_all_chat_limits(actor=principal.actor)
    requeued = []
    if body is not None and body.requeue:
        requeued = _requeue_refused(
            _state(request), chat_id=None, actor=principal.actor
        )
    logger.info(
        "%s unlimited %d chat(s) (requeued %d)", principal.actor, count, len(requeued)
    )
    return {"ok": True, "chats": count, "requeued": requeued}


@api.get("/api/stats")
@api.get("/stats", include_in_schema=False)
def stats() -> dict[str, Any]:
    return gui_queries.request_stats()


@api.get("/api/aggregates")
def aggregates(
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    top: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    return gui_queries.aggregates(hours=hours, top=top)


@api.get("/api/events")
def events(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: OffsetQuery = 0,
    event_type: Annotated[str | None, Query(alias="type", max_length=64)] = None,
    chat_id: DbIntQuery = None,
    request_id: DbIntQuery = None,
) -> dict[str, Any]:
    """The event log (newest first)."""
    return gui_queries.list_events(
        limit=limit,
        offset=offset,
        event_type=_query_text(event_type),
        chat_id=chat_id,
        request_id=request_id,
    )


@api.get("/api/config")
def runtime_config(request: Request) -> dict[str, Any]:
    """GUI settings, effective limits and this process's configuration."""
    state = _state(request)
    settings = state.settings
    worker = gui_queries.load_worker()
    limits = gui_queries.effective_limits(worker)
    summary = getattr(config, "summary", None)
    validate = getattr(config, "validate", None)
    return {
        "version": __version__,
        "auth": {
            "token": settings.token_enabled,
            "oauth": state.oauth_client is not None,
            "oauth_admin_groups": sorted(settings.admin_groups),
            "oauth_assume_email_verified": settings.assume_email_verified,
            "oauth_redirect_uri": (
                settings.oauth.redirect_uri if settings.oauth else None
            ),
            "session_cookie_secure": settings.cookie_secure,
            "session_max_age_seconds": settings.session_max_age,
            "secret_key_ephemeral": settings.secret_key_ephemeral,
            "trusted_origins": sorted(settings.trusted_origins),
        },
        "gui": {
            "db_poll_seconds": state.tailer.poll_interval,
            "requeue_max_age_seconds": state.requeue_max_age,
            "history_keep_seconds": gui_queries.history_keep_seconds(limits),
            "ws_clients": state.hub.client_count,
            "last_event_id": state.tailer.last_id,
        },
        "worker": worker,
        "limits": limits,
        "settings": summary() if callable(summary) else {},
        "warnings": [*settings.warnings, *(validate() if callable(validate) else [])],
    }


@api.post("/api/history/clear")
def clear_history(principal: AdminPrincipal) -> dict[str, Any]:
    """Delete finished requests and the event log.

    Active requests are kept, and so are the accepted ones (done, failed)
    still inside the worker's largest rate-limit or duplicate window: the rate
    limiter and the duplicate check read them, so deleting them would reset
    every chat's quota. ``db.prune`` applies exactly that protection.
    """
    keep = gui_queries.history_keep_seconds(
        gui_queries.effective_limits(gui_queries.load_worker())
    )
    pruned = db.prune(
        updates_days=CLEAR_UP_TO_NOW_DAYS,
        requests_days=CLEAR_UP_TO_NOW_DAYS,
        keep_accepted_seconds=keep,
    )
    counts = {key: pruned[key] for key in ("requests", "request_events", "updates")}
    db.add_event(
        db.EventType.HISTORY_CLEARED, actor=principal.actor, kept_seconds=keep, **counts
    )
    logger.warning(
        "%s cleared the history (accepted requests of the last %.0f s kept): %s",
        principal.actor,
        keep,
        counts,
    )
    return {"ok": True, "deleted": counts, "kept_seconds": keep}


# Users -----------------------------------------------------------------------
def _valid_role(raw: str) -> str:
    role = raw.strip().lower()
    if role not in db.ROLES:
        raise HTTPException(400, "Ruolo non valido: usa 'admin' oppure 'user'")
    return role


class LastAdminError(Exception):
    """The change would leave no DB user with role admin."""


def _write_user_keeping_an_admin(user_id: int, role: str | None) -> bool:
    """Set the user's ``role`` (None = delete), never removing the last admin.

    False when there is no such user; :class:`LastAdminError` when the user
    is the only admin. The check and the change run in one ``BEGIN
    IMMEDIATE`` transaction, so two concurrent demotions cannot both pass the
    check (``db.set_user_role`` / ``db.delete_user`` have no guarded form).
    """
    with db.write() as conn:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return False
        if row["role"] == "admin" and role != "admin":
            (others,) = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND id <> ?",
                (user_id,),
            ).fetchone()
            if not others:
                raise LastAdminError
        if role is None:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        else:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    return True


def _change_user(state: GuiState, user_id: int, role: str | None) -> None:
    """Set the role of (None: delete) a user; 404 if missing, 409 if last admin.

    Without ADMIN_TOKEN and OAUTH_ADMIN_GROUPS only DB admins can administer
    the GUI, so the last one is never demoted or deleted.
    """
    if state.settings.token_enabled or state.settings.admin_groups:
        found = (
            db.delete_user(user_id) if role is None else db.set_user_role(user_id, role)
        )
    else:
        try:
            found = _write_user_keeping_an_admin(user_id, role)
        except LastAdminError as exc:
            raise HTTPException(
                409,
                "È l'ultimo amministratore: aggiungine un altro prima di rimuoverlo"
                " (oppure configura ADMIN_TOKEN o OAUTH_ADMIN_GROUPS)",
            ) from exc
    if not found:
        raise HTTPException(404, "Utente non trovato")


@api.get("/api/users")
def list_users(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    offset: OffsetQuery = 0,
) -> dict[str, Any]:
    rows = db.list_users(limit=limit, offset=offset)
    return {
        "items": [gui_queries.user_view(row) for row in rows],
        "total": gui_queries.count_users(),
        "admins": gui_queries.count_admins(),
        "offset": offset,
        "limit": limit,
    }


@api.post("/api/users", status_code=201)
def create_user(body: UserCreateBody, principal: AdminPrincipal) -> dict[str, Any]:
    email = body.email.strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Indirizzo e-mail non valido")
    role = _valid_role(body.role)
    username = _query_text(body.username)
    if db.get_user_by_email(email) is not None:
        raise HTTPException(409, "Esiste già un utente con questa e-mail")
    try:
        user_id = db.add_user(username=username, email=email, role=role)
    except ValueError as exc:  # the role is valid: a concurrent duplicate
        raise HTTPException(409, "Esiste già un utente con questa e-mail") from exc
    logger.info("%s created user %s with role %s", principal.actor, email, role)
    user = gui_queries.get_user(user_id)
    if user is None:  # deleted in the meantime
        raise HTTPException(404, "Utente non trovato")
    return user


@api.put("/api/users/{user_id}/role")
def set_user_role(
    user_id: DbIdPath, body: UserRoleBody, request: Request, principal: AdminPrincipal
) -> dict[str, Any]:
    role = _valid_role(body.role)
    _change_user(_state(request), user_id, role)
    logger.info("%s set the role of user %s to %s", principal.actor, user_id, role)
    user = gui_queries.get_user(user_id)
    if user is None:  # deleted in the meantime
        raise HTTPException(404, "Utente non trovato")
    return user


@api.delete("/api/users/{user_id}")
def delete_user(
    user_id: DbIdPath, request: Request, principal: AdminPrincipal
) -> dict[str, Any]:
    _change_user(_state(request), user_id, None)
    logger.info("%s deleted user %s", principal.actor, user_id)
    return {"ok": True, "id": user_id}


# ---------------------------------------------------------------------------
# Middleware, errors, lifespan
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware:
    """CSP and hardening headers on every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        static = str(scope.get("path", "")).startswith("/static/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "same-origin")
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                if not static:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _database_error(request: Request, exc: Exception) -> JSONResponse:
    busy = isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()
    logger.warning("Database error on %s %s: %s", request.method, request.url.path, exc)
    detail = "Database occupato: riprova tra poco" if busy else "Errore del database"
    return JSONResponse(
        {"detail": detail}, status_code=503, headers={"Retry-After": "2"}
    )


_TOKEN_QUERY_RE = re.compile(r"(?i)(token=)[^&\s\"']*")


def _redact_tokens(value: Any) -> Any:
    if isinstance(value, str) and "token=" in value.lower():
        return _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", value)
    return value


class _UvicornLogFilter(logging.Filter):
    """Redact ``token=`` query values (old bookmarks and scripts) in every
    string argument; optionally hide the /health probe lines.

    ``uvicorn.access`` logs ``(client, method, path, ...)``; ``uvicorn.error``
    logs WebSocket handshakes as ``'%s - "WebSocket %s" 403'`` with
    ``(client, path)``.
    """

    def __init__(self, *, hide_health: bool) -> None:
        super().__init__()
        self.hide_health = hide_health

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, Mapping):
            record.args = {key: _redact_tokens(value) for key, value in args.items()}
        elif args:
            path = args[2] if len(args) >= 3 else None
            if (
                self.hide_health
                and isinstance(path, str)
                and path.split("?", 1)[0] == "/health"
            ):
                return False
            record.args = tuple(_redact_tokens(arg) for arg in args)
        elif isinstance(record.msg, str):  # no arguments: the text is final
            record.msg = _redact_tokens(record.msg)
        return True


_LOG_FILTERS = {
    "uvicorn.access": _UvicornLogFilter(
        hide_health=not getattr(config, "HEALTH_DEBUG", False)
    ),
    "uvicorn.error": _UvicornLogFilter(hide_health=False),
}


def _configure_logging() -> None:
    for name, log_filter in _LOG_FILTERS.items():
        uvicorn_logger = logging.getLogger(name)
        if log_filter not in uvicorn_logger.filters:
            uvicorn_logger.addFilter(log_filter)
    root = logging.getLogger()
    if not root.handlers:  # plain uvicorn: give our loggers a handler
        logging.basicConfig(
            level=getattr(config, "LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: GuiState = app.state.gui
    _configure_logging()
    for warning in state.settings.warnings:
        logger.warning("GUI configuration: %s", warning)
    if not state.settings.token_enabled and state.oauth_client is None:
        raise RuntimeError(
            "The admin GUI needs ADMIN_TOKEN or a complete OAuth configuration"
            " (OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET and OAUTH_SERVER_METADATA_URL"
            " or OAUTH_AUTHORIZE_URL + OAUTH_TOKEN_URL)"
        )
    await db.aio(db.init_db)
    await state.tailer.start()
    logger.info(
        "Admin GUI v%s started (token login: %s, OAuth: %s)",
        __version__,
        state.settings.token_enabled,
        state.oauth_client is not None,
    )
    try:
        yield
    finally:
        await state.tailer.stop()
        state.hub.close_all()


def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("app.js", "app.css", "index.html"):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


def create_app(
    settings: AuthSettings | None = None,
    *,
    oauth_registry: Any = None,
    poll_interval: float | None = None,
    ws_recheck_seconds: float = WS_RECHECK_SECONDS,
) -> FastAPI:
    """Build the GUI application (settings default to the environment)."""
    settings = settings or AuthSettings.from_env()
    registry = (
        oauth_registry
        if oauth_registry is not None
        else gui_auth.build_oauth_registry(settings)
    )
    heartbeat = float(getattr(config, "WORKER_HEARTBEAT_SECONDS", 5.0))
    hub = ws_broadcast.Hub()
    tailer = ws_broadcast.EventTailer(
        hub,
        poll_interval=(
            poll_interval
            if poll_interval is not None
            else float(getattr(config, "GUI_DB_POLL_SECONDS", 1.0))
        ),
        status_interval=max(1.0, min(5.0, heartbeat)),
        status_source=gui_queries.load_worker,
        shape_event=gui_queries.event_view,
    )
    asset_version = _asset_version()
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    state = GuiState(
        settings=settings,
        oauth=registry,
        hub=hub,
        tailer=tailer,
        limiter=gui_auth.FailureLimiter(),
        index_html=index_html.replace(ASSET_VERSION_PLACEHOLDER, asset_version),
        asset_version=asset_version,
        ws_recheck_seconds=ws_recheck_seconds,
        requeue_max_age=float(
            getattr(config, "GUI_REQUEUE_MAX_AGE_SECONDS", DEFAULT_REQUEUE_MAX_AGE)
        ),
    )
    application = FastAPI(
        title="Social media reuploader - Admin GUI",
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        middleware=[
            Middleware(SecurityHeadersMiddleware),
            Middleware(gui_auth.CSRFMiddleware, settings=settings),
            Middleware(
                SessionMiddleware,
                secret_key=settings.secret_key,
                session_cookie=gui_auth.SESSION_COOKIE_NAME,
                max_age=settings.session_max_age,
                same_site="lax",
                https_only=settings.cookie_secure,
            ),
        ],
    )
    application.state.gui = state
    application.add_exception_handler(sqlite3.Error, _database_error)
    application.include_router(public)
    application.include_router(api)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application


app = create_app()
# The OAuth registry of ``app`` (tests may inject a provider client on it).
oauth = app.state.gui.oauth
