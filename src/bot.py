"""Bot process entrypoint: ``python -m src`` (also ``python -m src.bot``).

One code path for both ways of receiving updates (``config.MODE``):

* ``polling`` (default): ``deleteWebhook`` first (a leftover webhook makes
  every getUpdates fail with 409 Conflict), then aiogram long polling with an
  explicit ``allowed_updates`` list.
* ``webhook`` (only when ``WEBHOOK_URL`` is set, otherwise the bot logs a
  warning and polls): aiogram ``SimpleRequestHandler`` on
  ``config.WEBHOOK_PATH`` with a secret token, registered with
  ``setWebhook`` at startup. ``python -m src.main`` starts this mode.

Both modes serve /health, /ready and /metrics on ``config.HOST:config.PORT``
(see :mod:`src.http_server`) and use the same handlers (:mod:`src.handlers`).

Startup: logging -> configuration problems and summary -> ``db.init_db()`` ->
HTTP server -> getMe (token check) -> ``providers.warm_up()`` (in a thread)
-> ``WorkerPool.start()`` -> receive updates.

Shutdown on SIGTERM/SIGINT (a second signal forces it): stop receiving
updates -> wait for running update handlers -> ``WorkerPool.stop()`` -> close
the aiogram and shared HTTP client sessions -> stop the HTTP server.

Exit codes: 0 after a normal shutdown, 1 on runtime failures, 2 on
configuration errors (missing or rejected ``BOT_TOKEN``, invalid webhook
secret). The bot token and the webhook URL are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import TelegramObject
from aiogram.utils.token import TokenValidationError
from aiohttp import web

from . import (
    __version__,
    config,
    db,
    handlers,
    http_client,
    http_server,
    providers,
    telegram_api,
    telegram_client,
)
from .handlers import ALLOWED_UPDATES
from .worker import WorkerPool

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_UPDATES",
    "EXIT_CONFIG",
    "EXIT_FAILURE",
    "EXIT_OK",
    "BotApp",
    "StartupError",
    "UpdateTracker",
    "WebhookSettings",
    "create_dispatcher",
    "main",
    "run",
    "select_mode",
    "setup_logging",
    "webhook_settings",
    "webhook_target",
]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

POLLING_TIMEOUT = 10  # getUpdates long-poll seconds
HANDLER_CONCURRENCY = 32  # update handlers running at the same time (polling)
HANDLER_DRAIN_SECONDS = 10.0  # wait for running handlers at shutdown
POLLING_STOP_SECONDS = 15.0
TRACKER_KEY = "update_tracker"
MISSING_TOKEN_MESSAGE = (
    "BOT_TOKEN is not set: create a bot with @BotFather and start the bot with "
    "the BOT_TOKEN environment variable set to its token"
)
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Handlers installed by setup_logging (replaced, never duplicated, on a new call).
_installed_handlers: list[logging.Handler] = []


class StartupError(Exception):
    """The bot cannot start; ``exit_code`` is the process exit status."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class UpdateTracker(BaseMiddleware):
    """Outer update middleware counting running handlers (graceful drain)."""

    def __init__(self) -> None:
        self.active = 0
        self.handled = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        self.active += 1
        self._idle.clear()
        try:
            return await handler(event, data)
        finally:
            self.active -= 1
            self.handled += 1
            if self.active == 0:
                self._idle.set()

    async def wait_idle(self, seconds: float) -> bool:
        """Wait until no handler runs; False if still busy after ``seconds``."""
        if self._idle.is_set():
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), seconds)
        except TimeoutError:
            return False
        return True


def create_dispatcher(
    pool: handlers.SubmitPool, *, bot_info: dict[str, Any] | None = None
) -> Dispatcher:
    """The aiogram dispatcher used in both modes.

    Handlers get ``pool`` (the :class:`~src.worker.WorkerPool`) and
    ``bot_info`` (the getMe result, set at startup) from the workflow data.
    """
    dispatcher = Dispatcher(disable_fsm=True, pool=pool, bot_info=bot_info)
    tracker = UpdateTracker()
    dispatcher.update.outer_middleware(tracker)
    dispatcher[TRACKER_KEY] = tracker
    dispatcher.include_router(handlers.create_router())
    return dispatcher


# ---------------------------------------------------------------------------
# Logging and configuration
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    """Configure the root logger from config (idempotent) and redact tokens.

    ``LOG_LEVEL``, optional rotating file (``LOG_TO_FILE``, ``LOG_FILE``,
    ``LOG_MAX_BYTES``, ``LOG_BACKUP_COUNT``). Calling it again replaces the
    handlers it installed before instead of adding duplicates.
    """
    root = logging.getLogger()
    while _installed_handlers:
        old = _installed_handlers.pop()
        root.removeHandler(old)
        old.close()

    problems: list[str] = []
    level_name = str(getattr(config, "LOG_LEVEL", "INFO") or "INFO").upper()
    level = logging.getLevelNamesMapping().get(level_name)
    if level is None:
        problems.append(f"LOG_LEVEL={level_name!r} is not a log level; using INFO")
        level = logging.INFO

    new_handlers: list[logging.Handler] = [logging.StreamHandler()]
    if getattr(config, "LOG_TO_FILE", False):
        path = str(getattr(config, "LOG_FILE", "") or "telegram_downloader.log")
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            new_handlers.append(
                RotatingFileHandler(
                    path,
                    maxBytes=int(getattr(config, "LOG_MAX_BYTES", 5 * 1024 * 1024)),
                    backupCount=int(getattr(config, "LOG_BACKUP_COUNT", 5)),
                    encoding="utf-8",
                )
            )
        except OSError as exc:
            problems.append(f"Cannot write the log file ({exc}); logging to stderr")

    formatter = logging.Formatter(_LOG_FORMAT)
    for handler in new_handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
        _installed_handlers.append(handler)
    root.setLevel(level)
    # aiogram logs one INFO line per update: keep that for DEBUG.
    logging.getLogger("aiogram.event").setLevel(
        logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    )
    telegram_api.install_log_redaction()
    for problem in problems:
        logger.warning(problem)


def log_startup(mode: str) -> None:
    """Banner, every configuration problem and the non-secret settings."""
    logger.info(
        "Social Media Reuploader v%s starting (mode %s, pid %s)",
        __version__,
        mode,
        os.getpid(),
    )
    for problem in config.validate():
        logger.warning("Configuration: %s", problem)
    logger.info(
        "Configuration: %s", json.dumps(config.summary(), sort_keys=True, default=str)
    )


def select_mode(requested: str | None = None) -> str:
    """``"webhook"`` only when asked for and ``WEBHOOK_URL`` is set."""
    configured = str(getattr(config, "MODE", "polling") or "polling").lower()
    mode = (requested or configured).lower()
    if mode not in ("polling", "webhook"):
        logger.warning("Unknown mode %r: using polling", mode)
        return "polling"
    if mode == "webhook" and not getattr(config, "WEBHOOK_URL", None):
        if configured != "webhook":  # config.validate() reports the other case
            logger.warning("Webhook mode needs WEBHOOK_URL: falling back to polling")
        return "polling"
    return mode


@dataclass(frozen=True)
class WebhookSettings:
    url: str  # registered with Telegram (never logged)
    path: str  # served locally
    secret: str
    token_in_url: bool  # legacy WEBHOOK_URL containing the bot token


def webhook_target(url: str, token: str, path: str) -> tuple[str, bool]:
    """``(URL to register, contains the token)`` for a ``WEBHOOK_URL``.

    Legacy values with ``{token}`` or the token itself are kept (the
    deprecated ``/webhook/{token}`` route serves them); a URL without a path
    gets ``path`` appended; anything else is used as it is.
    """
    if "{token}" in url:
        return url.replace("{token}", token), True
    if token in url:
        return url, True
    parts = urlsplit(url)
    if parts.path in ("", "/"):
        return urlunsplit(parts._replace(path=path)), False
    return url, False


def _webhook_path() -> str:
    path = str(getattr(config, "WEBHOOK_PATH", "/webhook") or "/webhook").strip()
    return path if path.startswith("/") else "/" + path


def webhook_settings(token: str) -> WebhookSettings:
    """Validated webhook settings (StartupError with exit code 2 if invalid)."""
    url = getattr(config, "WEBHOOK_URL", None)
    if not url:
        raise StartupError("WEBHOOK_URL is not set", EXIT_CONFIG)
    secret = config.webhook_secret()
    if not secret or not _SECRET_RE.fullmatch(secret):
        raise StartupError(
            "WEBHOOK_SECRET must be 1-256 characters among A-Z, a-z, 0-9, '_' "
            "and '-'",
            EXIT_CONFIG,
        )
    path = _webhook_path()
    if "{" in path or "}" in path:
        raise StartupError(
            "WEBHOOK_PATH must be a fixed path such as /webhook (no {placeholders})",
            EXIT_CONFIG,
        )
    target, token_in_url = webhook_target(url, token, path)
    if token_in_url:
        logger.warning(
            "WEBHOOK_URL contains the bot token, which ends up in proxy and "
            "access logs. This is deprecated: set WEBHOOK_URL to the public URL "
            "of WEBHOOK_PATH; requests are authenticated with a secret token."
        )
    elif urlsplit(target).path.rstrip("/") != path.rstrip("/"):
        logger.info(
            "The path of WEBHOOK_URL differs from WEBHOOK_PATH: the reverse "
            "proxy must forward webhook requests to WEBHOOK_PATH"
        )
    return WebhookSettings(target, path, secret, token_in_url)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
BotFactory = Callable[[str], Bot]
PoolFactory = Callable[[str], Any]


class BotApp:
    """One bot process: HTTP server, worker pool and update receiver."""

    def __init__(
        self,
        token: str,
        *,
        mode: str = "polling",
        host: str | None = None,
        port: int | None = None,
        bot_factory: BotFactory | None = None,
        pool_factory: PoolFactory | None = None,
    ) -> None:
        self.token = token
        self.mode = mode
        self.host = host or config.HOST
        self.port = config.PORT if port is None else port
        self.state = http_server.HealthState(mode=mode)
        self.exit_code = EXIT_OK
        self._bot_factory = bot_factory or telegram_client.get_bot
        self._pool_factory = pool_factory or WorkerPool
        self._stop = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None
        self._signals: list[signal.Signals] = []
        self._bot: Bot | None = None
        self._pool: Any = None
        self._pool_start_attempted = False
        self._dispatcher: Dispatcher | None = None
        self._runner: web.AppRunner | None = None
        self._webhook: WebhookSettings | None = None
        self._polling_task: asyncio.Task[None] | None = None
        self._shutdown_started = False

    # -- public -------------------------------------------------------------
    @property
    def dispatcher(self) -> Dispatcher | None:
        return self._dispatcher

    @property
    def http_port(self) -> int | None:
        """The bound HTTP port (useful with ``port=0``)."""
        if self._runner is None or not self._runner.addresses:
            return None
        return int(self._runner.addresses[0][1])

    def request_stop(self) -> None:
        """Start a graceful shutdown (what SIGTERM does)."""
        self._stop.set()

    async def run(self) -> int:
        """Start, serve until stopped, shut down; returns the exit code."""
        self._task = asyncio.current_task()
        self._install_signal_handlers()
        try:
            await self._start()
            if not self._stop.is_set():
                await self._serve()
        except StartupError as exc:
            logger.error("%s", exc)
            self.exit_code = exc.exit_code
        except Exception:
            logger.exception("The bot failed")
            self.exit_code = EXIT_FAILURE
        finally:
            try:
                await self._shutdown()
            finally:
                self._remove_signal_handlers()
        return self.exit_code

    # -- startup ------------------------------------------------------------
    async def _start(self) -> None:
        if self.mode == "webhook":
            self._webhook = webhook_settings(self.token)
        try:
            bot = self._bot = self._bot_factory(self.token)
        except TokenValidationError as exc:
            raise StartupError(
                "BOT_TOKEN is not a valid bot token", EXIT_CONFIG
            ) from exc
        await db.aio(db.init_db)
        if self.mode == "polling":
            self.state.polling = http_server.PollingMonitor()
            bot.session.middleware(self.state.polling)
        self._pool = self._pool_factory(self.token)
        dispatcher = self._dispatcher = create_dispatcher(self._pool)

        app = http_server.create_app(self.state)
        if self._webhook is not None:
            http_server.add_webhook_routes(
                app,
                dispatcher=dispatcher,
                bot=bot,
                state=self.state,
                path=self._webhook.path,
                secret_token=self._webhook.secret,
                token=self.token,
            )
        await self._start_http(app)

        dispatcher["bot_info"] = await self._get_me()
        if self._stop.is_set():
            return
        await asyncio.to_thread(providers.warm_up)
        if self._stop.is_set():
            return
        self._pool_start_attempted = True
        await self._pool.start()
        self.state.pool_started = True
        if self._stop.is_set():
            return
        if self.mode == "webhook":
            await self._start_webhook()
        else:
            await self._start_polling()

    async def _start_http(self, app: web.Application) -> None:
        try:
            self._runner = await http_server.start_server(app, self.host, self.port)
        except OSError as exc:
            raise StartupError(
                f"Cannot listen on {self.host}:{self.port}: {exc}"
            ) from exc
        endpoints = "/health, /ready, /metrics"
        if self.mode == "webhook":
            endpoints += " and the webhook"
        logger.info(
            "HTTP server listening on %s:%s (%s)", self.host, self.http_port, endpoints
        )

    async def _get_me(self) -> dict[str, Any]:
        result = await telegram_api.get_me(self.token)
        description = telegram_api.redact(result.get("description"), self.token)
        if not result.get("ok"):
            if result.get("error_code") in (401, 404):
                raise StartupError(
                    f"Telegram rejected BOT_TOKEN ({description}): check the token "
                    "given by @BotFather",
                    EXIT_CONFIG,
                )
            raise StartupError(f"Cannot reach the Telegram Bot API: {description}")
        me = result.get("result") or {}
        logger.info("Authorized as @%s (id %s)", me.get("username"), me.get("id"))
        if not me.get("can_read_all_group_messages"):
            logger.warning(
                "Group privacy mode is enabled: in groups the bot only sees links "
                "when it is an administrator. Disable it with /setprivacy in "
                "@BotFather to work as a plain member."
            )
        return me

    async def _start_polling(self) -> None:
        result = await telegram_api.delete_webhook(self.token)
        if not result.get("ok"):
            logger.warning(
                "deleteWebhook failed (%s): if a webhook is still set, polling "
                "fails with 409 Conflict",
                telegram_api.redact(result.get("description"), self.token),
            )
        if self.state.polling is not None:
            self.state.polling.mark_started()
        if self._dispatcher is None or self._bot is None:  # pragma: no cover
            raise StartupError("internal error: dispatcher not created")
        self._polling_task = asyncio.create_task(
            self._dispatcher.start_polling(
                self._bot,
                polling_timeout=POLLING_TIMEOUT,
                handle_as_tasks=True,
                allowed_updates=list(ALLOWED_UPDATES),
                handle_signals=False,
                close_bot_session=False,
                tasks_concurrency_limit=HANDLER_CONCURRENCY,
            ),
            name="telegram-polling",
        )
        self.state.polling_task = self._polling_task
        logger.info("Receiving updates with long polling")

    async def _start_webhook(self) -> None:
        settings = self._webhook
        if settings is None:  # pragma: no cover
            raise StartupError("internal error: webhook settings missing")
        self.state.accepting_updates = True
        result = await telegram_api.set_webhook(
            self.token,
            settings.url,
            secret_token=settings.secret,
            allowed_updates=list(ALLOWED_UPDATES),
        )
        if not result.get("ok"):
            raise StartupError(
                "Telegram refused the webhook: "
                + telegram_api.redact(result.get("description"), self.token)
            )
        self.state.webhook_registered = True
        logger.info("Webhook registered with Telegram; receiving updates")

    # -- running ------------------------------------------------------------
    async def _serve(self) -> None:
        stop_waiter = asyncio.create_task(self._stop.wait(), name="stop-waiter")
        waiters: set[asyncio.Task[Any]] = {stop_waiter}
        if self._polling_task is not None:
            waiters.add(self._polling_task)
        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_waiter.cancel()
        if self._polling_task is not None and self._polling_task in done:
            self._polling_ended(self._polling_task)

    def _polling_ended(self, task: asyncio.Task[Any]) -> None:
        self.exit_code = EXIT_FAILURE
        if task.cancelled():
            logger.error("Polling was cancelled unexpectedly")
            return
        exc = task.exception()
        if exc is None:
            logger.error("Polling stopped unexpectedly")
        elif isinstance(exc, TelegramUnauthorizedError):
            logger.error("Telegram rejected BOT_TOKEN while polling")
            self.exit_code = EXIT_CONFIG
        else:
            logger.error("Polling failed", exc_info=exc)

    # -- shutdown -----------------------------------------------------------
    async def _shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.state.shutting_down = True
        logger.info("Shutting down")
        await self._stop_receiving()
        await self._drain_handlers()
        await self._stop_pool()
        await self._close_clients()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Shutdown complete (exit code %s)", self.exit_code)

    async def _stop_receiving(self) -> None:
        self.state.accepting_updates = False  # webhook routes answer 503
        task = self._polling_task
        if task is None or task.done() or self._dispatcher is None:
            return
        try:
            await asyncio.wait_for(
                self._dispatcher.stop_polling(), POLLING_STOP_SECONDS
            )
        except (RuntimeError, TimeoutError):
            task.cancel()  # not started yet, or stuck
        await asyncio.wait({task}, timeout=POLLING_STOP_SECONDS)
        if not task.done():
            logger.warning("Polling did not stop in %.0fs", POLLING_STOP_SECONDS)
        elif not task.cancelled() and (exc := task.exception()) is not None:
            logger.warning("Polling ended with an error", exc_info=exc)

    async def _drain_handlers(self) -> None:
        if self._dispatcher is None:
            return
        tracker = self._dispatcher.get(TRACKER_KEY)
        if not isinstance(tracker, UpdateTracker):
            return
        await asyncio.sleep(0)  # let update tasks that were just created start
        if not await tracker.wait_idle(HANDLER_DRAIN_SECONDS):
            logger.warning(
                "%d update handler(s) still running after %.0fs",
                tracker.active,
                HANDLER_DRAIN_SECONDS,
            )

    async def _stop_pool(self) -> None:
        if self._pool is None or not self._pool_start_attempted:
            return
        try:
            await self._pool.stop()
        except Exception:
            logger.exception("Error while stopping the worker pool")
        self.state.pool_started = False

    async def _close_clients(self) -> None:
        if self._bot is not None:
            try:
                await self._bot.session.close()
            except (aiohttp.ClientError, OSError, RuntimeError):
                logger.warning("Error while closing the bot session", exc_info=True)
        await telegram_client.close_all_bots()
        await http_client.close_session()

    # -- signals ------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                logger.debug("Cannot install a handler for %s", sig.name)
                continue
            self._signals.append(sig)

    def _remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        while self._signals:
            loop.remove_signal_handler(self._signals.pop())

    def _on_signal(self, sig: signal.Signals) -> None:
        if not self._stop.is_set():
            logger.info("Received %s: shutting down gracefully", sig.name)
            self._stop.set()
            return
        logger.warning("Received %s again: forcing the shutdown", sig.name)
        if self._task is not None:
            self._task.cancel()


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------
async def run(mode: str | None = None) -> int:
    """Run the bot until SIGTERM/SIGINT; returns the process exit code."""
    selected = select_mode(mode)
    log_startup(selected)
    token = getattr(config, "BOT_TOKEN", None)
    if not token:
        logger.error(MISSING_TOKEN_MESSAGE)
        return EXIT_CONFIG
    return await BotApp(token, mode=selected).run()


def main(mode: str | None = None) -> int:
    """Process entrypoint (``python -m src``); ``mode`` overrides config.MODE."""
    setup_logging()
    try:
        return asyncio.run(run(mode))
    except KeyboardInterrupt:  # Ctrl+C before the signal handlers exist
        return 130
    except asyncio.CancelledError:  # second signal: forced shutdown
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
