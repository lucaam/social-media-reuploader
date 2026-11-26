"""HTTP endpoints of the bot process (aiohttp), served in both modes.

Routes on ``config.HOST:config.PORT``:

* ``GET /health`` - liveness. Cheap (no I/O): answers ``ok`` while the event
  loop runs. ``/health?ready=1`` is the same as ``/ready``.
* ``GET /ready`` - readiness. 200 only when the worker pool is started,
  updates are being received (polling loop alive with a recent successful
  getUpdates, or webhook registered), the database answers and the worker
  heartbeat is fresh; 503 otherwise and while shutting down.
* ``GET /metrics`` - Prometheus metrics (``src.metrics.render`` when the
  module provides it, else ``prometheus_client.generate_latest()``).

Webhook mode also serves (see :func:`add_webhook_routes`):

* ``POST config.WEBHOOK_PATH`` - aiogram ``SimpleRequestHandler``. Requests
  must carry the ``X-Telegram-Bot-Api-Secret-Token`` header registered with
  setWebhook (401 otherwise).
* ``POST /webhook/{token}`` - DEPRECATED, kept only for the transition from
  setups whose webhook URL contains the bot token. The token is compared in
  constant time. It will be removed in a future release.

``config.HEALTH_DEBUG`` makes the probes answer JSON with details (never
secrets, URLs or chat contents). Probe and successful webhook requests are
logged at DEBUG only and request paths are sanitized (no query strings, no
token) before they reach the access log.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramAPIError, TelegramConflictError
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger, BaseRequest, StreamResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import __version__, config, db, metrics, telegram_api

logger = logging.getLogger(__name__)

__all__ = [
    "LEGACY_WEBHOOK_ROUTE",
    "SECRET_HEADER",
    "HealthState",
    "PollingMonitor",
    "QuietAccessLogger",
    "add_webhook_routes",
    "create_app",
    "readiness",
    "render_metrics",
    "start_server",
]

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
LEGACY_WEBHOOK_ROUTE = "/webhook/{token}"
PROBE_PATHS = frozenset({"/health", "/ready", "/metrics"})
# getUpdates must have succeeded within this many seconds (or polling started
# less than this ago) for the bot to be ready.
POLLING_STALE_SECONDS = 90.0
# Minimum accepted age of the worker heartbeat (3 heartbeats otherwise).
MIN_HEARTBEAT_AGE_SECONDS = 15.0
DB_CHECK_SECONDS = 3.0
_CONFLICT_LOG_INTERVAL = 300.0
_TRUE_QUERY = frozenset({"1", "true", "yes", "on"})

Clock = Callable[[], float]
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# ---------------------------------------------------------------------------
# Runtime state shared with the bot lifecycle
# ---------------------------------------------------------------------------
class PollingMonitor(BaseRequestMiddleware):
    """aiogram request middleware recording the outcome of getUpdates calls.

    aiogram retries failed getUpdates forever (logging each failure), so a
    polling bot can be alive but deaf, e.g. when another process polls with
    the same token (409 Conflict). The readiness probe uses
    :meth:`healthy`.
    """

    def __init__(
        self,
        *,
        stale_after: float = POLLING_STALE_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        self.stale_after = stale_after
        self._clock = clock
        self.started_at: float | None = None
        self.last_success: float | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.consecutive_errors = 0
        self.updates_received = 0
        self._conflict_logged_at: float | None = None

    def mark_started(self) -> None:
        self.started_at = self._clock()

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Any:
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        try:
            result = await make_request(bot, method)
        except TelegramAPIError as exc:
            self._record_error(exc)
            raise
        self.last_success = self._clock()
        self.consecutive_errors = 0
        if isinstance(result, list):
            self.updates_received += len(result)
        return result

    def _record_error(self, exc: TelegramAPIError) -> None:
        now = self._clock()
        self.last_error = type(exc).__name__
        self.last_error_at = now
        self.consecutive_errors += 1
        if not isinstance(exc, TelegramConflictError):
            return
        logged = self._conflict_logged_at
        if logged is None or now - logged >= _CONFLICT_LOG_INTERVAL:
            self._conflict_logged_at = now
            logger.error(
                "getUpdates conflict: another process is receiving updates for "
                "this bot token (a second instance or a webhook). Run exactly one "
                "bot instance per token (Kubernetes: replicas 1, strategy Recreate)."
            )

    def healthy(self, now: float | None = None) -> bool:
        """getUpdates succeeded recently (or polling started recently)."""
        now = self._clock() if now is None else now
        reference = self.last_success
        if reference is None:
            reference = self.started_at
        return reference is not None and now - reference <= self.stale_after

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = self._clock() if now is None else now

        def age(value: float | None) -> float | None:
            return None if value is None else round(now - value, 1)

        return {
            "last_success_age_seconds": age(self.last_success),
            "last_error": self.last_error,
            "last_error_age_seconds": age(self.last_error_at),
            "consecutive_errors": self.consecutive_errors,
            "updates_received": self.updates_received,
        }


@dataclass
class HealthState:
    """What the bot process reports on /health and /ready."""

    mode: str = "polling"
    version: str = __version__
    started_at: float = field(default_factory=time.monotonic)
    pool_started: bool = False
    accepting_updates: bool = False  # webhook routes feed updates
    webhook_registered: bool = False
    shutting_down: bool = False
    polling: PollingMonitor | None = None
    polling_task: asyncio.Task[Any] | None = None
    last_ready: bool | None = None

    def uptime(self) -> float:
        return round(time.monotonic() - self.started_at, 1)

    def updates_check(self) -> dict[str, Any]:
        """Is the process receiving updates? (in memory, no I/O)."""
        if self.mode == "webhook":
            ok = self.webhook_registered and self.accepting_updates
            return {
                "ok": ok,
                "registered": self.webhook_registered,
                "accepting": self.accepting_updates,
            }
        task = self.polling_task
        alive = task is not None and not task.done()
        monitor = self.polling
        recent = monitor is not None and monitor.healthy()
        detail: dict[str, Any] = {"ok": alive and recent, "polling_alive": alive}
        if monitor is not None:
            detail.update(monitor.snapshot())
        return detail

    def note_readiness(self, ready: bool, failing: list[str]) -> None:
        """Log readiness transitions (probes themselves are not logged)."""
        previous, self.last_ready = self.last_ready, ready
        if ready == previous:
            return
        if ready:
            logger.info("Bot is ready")
        elif self.shutting_down:
            logger.info("Bot is not ready: shutting down")
        elif previous:
            logger.warning("Bot is no longer ready: %s", ", ".join(failing))
        else:
            logger.info("Bot is not ready yet: %s", ", ".join(failing))


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
def _heartbeat_max_age() -> float:
    interval = float(getattr(config, "WORKER_HEARTBEAT_SECONDS", 5.0) or 5.0)
    return max(3 * interval, MIN_HEARTBEAT_AGE_SECONDS)


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list | tuple | dict):
        return len(value)
    return None


async def _worker_checks() -> tuple[dict[str, Any], dict[str, Any]]:
    """(database check, worker heartbeat check)."""
    try:
        status = await asyncio.wait_for(db.aio(db.get_worker_status), DB_CHECK_SECONDS)
    except TimeoutError:
        return {"ok": False, "error": "timeout"}, {"ok": False}
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.debug("Readiness: database check failed", exc_info=True)
        return {"ok": False, "error": type(exc).__name__}, {"ok": False}
    if not status:
        return {"ok": True}, {"ok": False, "error": "no heartbeat"}
    age = status.get("age_seconds")
    max_age = _heartbeat_max_age()
    worker: dict[str, Any] = {
        "ok": isinstance(age, int | float) and age <= max_age,
        "heartbeat_age_seconds": (
            round(age, 1) if isinstance(age, int | float) else None
        ),
        "max_age_seconds": max_age,
    }
    for key in ("running", "queued", "deferred"):
        counted = _count(status.get(key))
        if counted is not None:
            worker[key] = counted
    return {"ok": True}, worker


async def readiness(state: HealthState) -> tuple[bool, dict[str, dict[str, Any]]]:
    """``(ready, checks)``; every check is a dict with at least ``ok``."""
    checks: dict[str, dict[str, Any]] = {
        "shutdown": {"ok": not state.shutting_down},
        "pool": {"ok": state.pool_started},
        "updates": state.updates_check(),
    }
    checks["db"], checks["worker"] = await _worker_checks()
    ready = all(check["ok"] for check in checks.values())
    state.note_readiness(ready, [name for name, c in checks.items() if not c["ok"]])
    return ready, checks


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
STATE_KEY = web.AppKey("health_state", HealthState)


def _debug() -> bool:
    return bool(getattr(config, "HEALTH_DEBUG", False))


async def _ready_response(state: HealthState) -> web.Response:
    ready, checks = await readiness(state)
    status = 200 if ready else 503
    if not _debug():
        return web.Response(status=status, text="ready" if ready else "not ready")
    payload = {
        "status": "ready" if ready else "not ready",
        "ready": ready,
        "mode": state.mode,
        "version": state.version,
        "uptime_seconds": state.uptime(),
        "checks": checks,
    }
    return web.json_response(payload, status=status)


async def health_view(request: web.Request) -> web.Response:
    """Liveness (``?ready=1``: readiness)."""
    state = request.app[STATE_KEY]
    if request.query.get("ready", "").lower() in _TRUE_QUERY:
        return await _ready_response(state)
    if not _debug():
        return web.Response(text="ok")
    payload = {
        "status": "ok",
        "mode": state.mode,
        "version": state.version,
        "uptime_seconds": state.uptime(),
        "pool_started": state.pool_started,
        "receiving_updates": state.updates_check()["ok"],
        "shutting_down": state.shutting_down,
    }
    return web.json_response(payload)


async def ready_view(request: web.Request) -> web.Response:
    return await _ready_response(request.app[STATE_KEY])


async def render_metrics() -> tuple[bytes, str]:
    """``(body, content type)`` of the Prometheus exposition."""
    render = getattr(metrics, "render", None)
    if render is None:
        return await asyncio.to_thread(generate_latest), CONTENT_TYPE_LATEST
    if inspect.iscoroutinefunction(render):
        result = await render()
    else:
        result = await asyncio.to_thread(render)
    body, content_type = (
        result if isinstance(result, tuple) else (result, CONTENT_TYPE_LATEST)
    )
    if isinstance(body, str):
        body = body.encode()
    return body, content_type


async def metrics_view(request: web.Request) -> web.Response:
    body, content_type = await render_metrics()
    return web.Response(body=body, headers={"Content-Type": content_type})


def create_app(state: HealthState) -> web.Application:
    """The aiohttp application with /health, /ready and /metrics."""
    app = web.Application()
    app[STATE_KEY] = state
    app.router.add_get("/health", health_view)
    app.router.add_get("/ready", ready_view)
    app.router.add_get("/metrics", metrics_view)
    return app


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def _invalid(text: str, status: int = 400) -> web.Response:
    return web.Response(status=status, text=text)


async def _feed(
    handler: SimpleRequestHandler, state: HealthState, request: web.Request
) -> web.StreamResponse:
    """Hand an authenticated request to aiogram (processed in background)."""
    if not state.accepting_updates:
        return _not_accepting()
    try:
        body = await request.json()
    except ValueError:
        return _invalid("invalid update")
    if not isinstance(body, dict) or "update_id" not in body:
        return _invalid("invalid update")
    if not state.accepting_updates:  # shutdown started while reading the body
        return _not_accepting()
    # No suspension from here until aiogram has created the background task
    # (the body is cached), so the shutdown drain always sees it.
    return await handler.handle(request)


def _not_accepting() -> web.Response:
    # Telegram retries later: the update reaches the next instance.
    return web.Response(
        status=503, text="not accepting updates", headers={"Retry-After": "5"}
    )


def _webhook_view(handler: SimpleRequestHandler, state: HealthState) -> Handler:
    async def view(request: web.Request) -> web.StreamResponse:
        supplied = request.headers.get(SECRET_HEADER, "")
        if not handler.verify_secret(supplied, handler.bot):
            return _invalid("unauthorized", 401)
        return await _feed(handler, state, request)

    return view


def _legacy_webhook_view(
    handler: SimpleRequestHandler, state: HealthState, token: str
) -> Handler:
    expected = token.encode()
    warned = False

    async def view(request: web.Request) -> web.StreamResponse:
        nonlocal warned
        supplied = request.match_info.get("token", "").encode()
        if not hmac.compare_digest(supplied, expected):
            return _invalid("forbidden", 403)
        if not warned:
            warned = True
            logger.warning(
                "Received an update on the deprecated /webhook/<token> route. Set "
                "WEBHOOK_URL to the public URL of WEBHOOK_PATH (without the bot "
                "token): Telegram then authenticates with the secret token header."
            )
        return await _feed(handler, state, request)

    return view


def add_webhook_routes(
    app: web.Application,
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    state: HealthState,
    path: str,
    secret_token: str,
    token: str,
) -> None:
    """Register ``POST path`` (secret header) and the legacy token route."""
    handler = SimpleRequestHandler(
        dispatcher=dispatcher, bot=bot, secret_token=secret_token
    )
    app.router.add_post(path, _webhook_view(handler, state))
    # The path token authenticates the legacy route (no secret header check).
    legacy = SimpleRequestHandler(dispatcher=dispatcher, bot=bot)
    app.router.add_post(
        LEGACY_WEBHOOK_ROUTE, _legacy_webhook_view(legacy, state, token)
    )


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def sanitize_path(path: str) -> str:
    """Request path safe for logs: no bot token, no legacy webhook secret."""
    if path.startswith("/webhook/") and path.rstrip("/") != "/webhook":
        return "/webhook/<redacted>"
    return telegram_api.redact(path)


class QuietAccessLogger(AbstractAccessLogger):
    """Access log without probe noise and without secrets.

    Probes (/health, /ready, /metrics) and successful webhook deliveries are
    logged at DEBUG (probes at INFO with ``HEALTH_DEBUG``); everything else
    at INFO. Query strings are never logged.
    """

    def _level(self, path: str, status: int) -> int:
        if path in PROBE_PATHS:
            return logging.INFO if _debug() else logging.DEBUG
        webhook_path = str(getattr(config, "WEBHOOK_PATH", "") or "/webhook")
        is_webhook = path in ("/" + webhook_path.lstrip("/"), "/webhook/<redacted>")
        if is_webhook and status < 400:
            return logging.DEBUG
        return logging.INFO

    def log(self, request: BaseRequest, response: StreamResponse, time: float) -> None:
        path = sanitize_path(request.path)
        level = self._level(path, response.status)
        if not self.logger.isEnabledFor(level):
            return
        self.logger.log(
            level,
            '%s "%s %s" %s %.3fs',
            request.remote,
            request.method,
            path,
            response.status,
            time,
        )

    @property
    def enabled(self) -> bool:
        return self.logger.isEnabledFor(logging.INFO)


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start serving ``app``; returns the runner (``await runner.cleanup()``)."""
    runner = web.AppRunner(
        app, access_log_class=QuietAccessLogger, shutdown_timeout=5.0
    )
    await runner.setup()
    try:
        await web.TCPSite(runner, host=host, port=port).start()
    except OSError:
        await runner.cleanup()
        raise
    return runner
