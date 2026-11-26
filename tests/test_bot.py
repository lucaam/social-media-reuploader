"""Bot lifecycle (src.bot): modes, startup/shutdown order, exit codes, logging.

Everything external is faked: the aiogram session (getMe / getUpdates), the
raw Bot API helpers (telegram_api), the worker pool and providers.warm_up.
The HTTP server really listens on a loopback port.
"""

import asyncio
import json
import logging
import os
import signal
import socket
import time

import aiohttp
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import Update, User

from src import bot as bot_module
from src import (
    config,
    db,
    http_client,
    http_server,
    providers,
    telegram_api,
    telegram_client,
)
from src.bot import BotApp, StartupError

TOKEN = "123456789:" + "C" * 35
WEBHOOK_BASE = "https://bot.example.com"
YT = "https://youtu.be/dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakePool:
    """WorkerPool stand-in; start() publishes a heartbeat like the worker."""

    def __init__(self, events, *, submit_delay=0.0, stop_delay=0.0):
        self.events = events
        self.submit_delay = submit_delay
        self.stop_delay = stop_delay
        self.calls = []

    async def start(self):
        self.events.append("pool.start")
        await db.aio(db.set_worker_status, {"pid": os.getpid(), "running": []})

    async def stop(self):
        self.events.append("pool.stop.begin")
        await asyncio.sleep(self.stop_delay)
        self.events.append("pool.stop")

    async def submit(self, chat_id, url, **kwargs):
        self.events.append("submit.start")
        self.calls.append((chat_id, url, kwargs))
        await asyncio.sleep(self.submit_delay)
        self.events.append("submit.end")
        return type("Result", (), {"status": "queued"})()


class FakeSession(BaseSession):
    """aiogram session answering getMe and getUpdates without network."""

    def __init__(self, events, updates=(), me_error=None):
        super().__init__()
        self.events = events
        self.updates = list(updates)
        self.me_error = me_error
        self.methods = []

    async def make_request(self, bot, method, **kwargs):
        self.methods.append(type(method).__name__)
        if isinstance(method, GetMe):
            if self.me_error is not None:
                raise self.me_error
            return User(id=bot.id, is_bot=True, first_name="Test", username="t_bot")
        if isinstance(method, GetUpdates):
            assert method.allowed_updates == list(bot_module.ALLOWED_UPDATES)
            if "getUpdates" not in self.events:
                self.events.append("getUpdates")
            if self.updates:
                batch, self.updates = self.updates, []
                return [Update.model_validate(u, context={"bot": bot}) for u in batch]
            await asyncio.sleep(0.02)  # long poll without news
            return []
        raise AssertionError(f"unexpected Bot API call {type(method).__name__}")

    async def close(self):
        self.events.append("bot.session.close")

    async def stream_content(self, *args, **kwargs):
        for chunk in ():
            yield chunk


class RunnerProxy:
    """Records when the HTTP server stops."""

    def __init__(self, runner, events):
        self._runner, self._events = runner, events

    @property
    def addresses(self):
        return self._runner.addresses

    async def cleanup(self):
        self._events.append("http.stop")
        await self._runner.cleanup()


def raw_update(update_id=1, chat_id=77):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10 + update_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 5, "is_bot": False, "first_name": "Ada"},
            "text": YT,
            "entities": [{"type": "url", "offset": 0, "length": len(YT)}],
        },
    }


@pytest.fixture
def events():
    return []


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path, events):
    """Settings and recorders shared by every test."""
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "requests.db"))
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "MODE", "polling")
    monkeypatch.setattr(config, "WEBHOOK_URL", None)
    monkeypatch.setattr(config, "WEBHOOK_PATH", "/webhook")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", None)
    monkeypatch.setattr(config, "HEALTH_DEBUG", False)
    monkeypatch.setattr(config, "UNSUPPORTED_LINK_REPLY", "private")
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 5400)

    real_init_db = db.init_db

    def init_db():
        events.append("init_db")
        real_init_db()

    monkeypatch.setattr(db, "init_db", init_db)
    monkeypatch.setattr(providers, "warm_up", lambda: events.append("warm_up"))

    real_start_server = http_server.start_server

    async def start_server(app, host, port):
        runner = await real_start_server(app, host, port)
        events.append("http.start")
        return RunnerProxy(runner, events)

    monkeypatch.setattr(http_server, "start_server", start_server)

    async def close_all_bots():
        events.append("close_all_bots")

    async def close_session():
        events.append("http_client.close")

    monkeypatch.setattr(telegram_client, "close_all_bots", close_all_bots)
    monkeypatch.setattr(http_client, "close_session", close_session)

    real_create = bot_module.create_dispatcher

    def create_dispatcher(pool, **kwargs):
        dispatcher = real_create(pool, **kwargs)

        async def polling_stopped():
            events.append("polling.stopped")

        dispatcher.shutdown.register(polling_stopped)
        return dispatcher

    monkeypatch.setattr(bot_module, "create_dispatcher", create_dispatcher)


@pytest.fixture(autouse=True)
def telegram(monkeypatch, events):
    """Fake raw Bot API helpers; tests can change the answers."""
    answers = {
        "get_me": {
            "ok": True,
            "result": {
                "id": 123456789,
                "is_bot": True,
                "username": "t_bot",
                "can_read_all_group_messages": False,
            },
        },
        "delete_webhook": {"ok": True, "result": True},
        "set_webhook": {"ok": True, "result": True},
        "set_webhook_calls": [],
    }

    async def get_me(token):
        assert token == TOKEN
        events.append("get_me")
        return answers["get_me"]

    async def delete_webhook(token, drop_pending_updates=False):
        assert drop_pending_updates is False  # never lose pending updates
        events.append("delete_webhook")
        return answers["delete_webhook"]

    async def set_webhook(token, url, allowed_updates=None, **kwargs):
        events.append("set_webhook")
        answers["set_webhook_calls"].append(
            {"url": url, "allowed_updates": allowed_updates, **kwargs}
        )
        return answers["set_webhook"]

    async def send_message(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(telegram_api, "get_me", get_me)
    monkeypatch.setattr(telegram_api, "delete_webhook", delete_webhook)
    monkeypatch.setattr(telegram_api, "set_webhook", set_webhook)
    monkeypatch.setattr(telegram_api, "send_message", send_message)
    monkeypatch.setattr(telegram_api, "set_message_reaction", send_message)
    return answers


def make_app(events, *, mode="polling", pool=None, session=None, port=0):
    pool = pool if pool is not None else FakePool(events)
    session = session if session is not None else FakeSession(events)
    app = BotApp(
        TOKEN,
        mode=mode,
        host="127.0.0.1",
        port=port,
        bot_factory=lambda token: Bot(token, session=session),
        pool_factory=lambda token: pool,
    )
    return app, pool, session


async def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def http_get(port, path):
    async with (
        aiohttp.ClientSession() as session,
        session.get(f"http://127.0.0.1:{port}{path}") as resp,
    ):
        return resp.status, await resp.text()


SHUTDOWN_TAIL = [
    "pool.stop.begin",
    "pool.stop",
    "bot.session.close",
    "close_all_bots",
    "http_client.close",
    "http.stop",
]


# ---------------------------------------------------------------------------
# Polling mode
# ---------------------------------------------------------------------------
def test_polling_lifecycle_order(events, caplog):
    session = FakeSession(events, updates=[raw_update()])
    pool = FakePool(events, submit_delay=0.3)
    app, pool, _ = make_app(events, pool=pool, session=session)

    async def scenario():
        task = asyncio.create_task(app.run())
        await wait_for(lambda: "submit.start" in events)
        assert await http_get(app.http_port, "/ready") == (200, "ready")
        events.append("stop")
        app.request_stop()
        await wait_for(lambda: app.state.shutting_down)
        port = app.http_port
        # The HTTP server is the last thing to stop.
        assert await http_get(port, "/health") == (200, "ok")
        assert (await http_get(port, "/ready"))[0] == 503
        return await task

    with caplog.at_level(logging.INFO):
        assert asyncio.run(scenario()) == bot_module.EXIT_OK

    startup = ["init_db", "http.start", "get_me", "warm_up", "pool.start"]
    assert events[:6] == [*startup, "delete_webhook"]
    assert events.index("delete_webhook") < events.index("getUpdates")
    stop = events.index("stop")
    assert events[stop:] == ["stop", "polling.stopped", "submit.end", *SHUTDOWN_TAIL]
    assert pool.calls[0][:2] == (77, YT)
    assert session.methods.count("GetUpdates") >= 1
    assert TOKEN not in caplog.text


def test_drain_timeout_does_not_block_the_shutdown(events, caplog, monkeypatch):
    monkeypatch.setattr(bot_module, "HANDLER_DRAIN_SECONDS", 0.05)
    session = FakeSession(events, updates=[raw_update()])
    app, _, _ = make_app(
        events, pool=FakePool(events, submit_delay=1.0), session=session
    )

    async def scenario():
        task = asyncio.create_task(app.run())
        await wait_for(lambda: "submit.start" in events)
        app.request_stop()
        return await task

    with caplog.at_level(logging.WARNING, logger="src.bot"):
        assert asyncio.run(scenario()) == bot_module.EXIT_OK
    assert "1 update handler(s) still running" in caplog.text
    # The pool was stopped while the slow handler was still running.
    assert "pool.stop" in events
    assert "submit.end" not in events[: events.index("pool.stop")]


def test_delete_webhook_failure_is_only_a_warning(events, telegram, caplog):
    telegram["delete_webhook"] = {"ok": False, "description": "network error"}
    app, _, _ = make_app(events)

    async def scenario():
        task = asyncio.create_task(app.run())
        await wait_for(lambda: "getUpdates" in events)
        app.request_stop()
        return await task

    with caplog.at_level(logging.WARNING, logger="src.bot"):
        assert asyncio.run(scenario()) == bot_module.EXIT_OK
    assert "deleteWebhook failed" in caplog.text


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TelegramUnauthorizedError(method=GetMe(), message="Unauthorized"), 2),
        (RuntimeError("polling exploded"), 1),
    ],
)
def test_polling_failure_ends_the_process(events, caplog, error, code):
    app, _, _ = make_app(events, session=FakeSession(events, me_error=error))
    with caplog.at_level(logging.ERROR, logger="src.bot"):
        assert asyncio.run(app.run()) == code
    assert "pool.stop" in events and events[-1] == "http.stop"
    assert "Polling" in caplog.text or "polling" in caplog.text


# ---------------------------------------------------------------------------
# Webhook mode
# ---------------------------------------------------------------------------
def test_webhook_lifecycle(events, telegram, caplog, monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_URL", WEBHOOK_BASE)
    pool = FakePool(events, submit_delay=0.3)
    app, pool, session = make_app(events, mode="webhook", pool=pool)
    secret = config.webhook_secret()

    async def post(port, path, payload, secret_header=None):
        headers = {http_server.SECRET_HEADER: secret_header} if secret_header else {}
        async with (
            aiohttp.ClientSession() as client,
            client.post(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload),
                headers=headers,
            ) as resp,
        ):
            return resp.status

    async def scenario():
        task = asyncio.create_task(app.run())
        await wait_for(lambda: app.state.webhook_registered)
        port = app.http_port
        assert await http_get(port, "/ready") == (200, "ready")
        assert await post(port, "/webhook", raw_update(1)) == 401
        assert await post(port, "/webhook", raw_update(1), secret) == 200
        await wait_for(lambda: "submit.start" in events)
        events.append("stop")
        app.request_stop()
        await wait_for(lambda: app.state.shutting_down)
        assert await post(port, "/webhook", raw_update(2), secret) == 503
        assert await post(port, f"/webhook/{TOKEN}", raw_update(3)) == 503
        return await task

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(scenario()) == bot_module.EXIT_OK

    startup = ["init_db", "http.start", "get_me", "warm_up", "pool.start"]
    assert events[:6] == [*startup, "set_webhook"]
    stop = events.index("stop")
    assert events[stop:] == ["stop", "submit.end", *SHUTDOWN_TAIL]
    assert [c[1] for c in pool.calls] == [YT]
    assert "getUpdates" not in events and session.methods == []
    assert telegram["set_webhook_calls"] == [
        {
            "url": WEBHOOK_BASE + "/webhook",
            "allowed_updates": list(bot_module.ALLOWED_UPDATES),
            "secret_token": secret,
        }
    ]
    for secret_text in (TOKEN, "bot.example.com", secret):
        assert secret_text not in caplog.text


def test_legacy_webhook_url_keeps_working(events, telegram, caplog, monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_URL", WEBHOOK_BASE + "/webhook/{token}")
    app, pool, _ = make_app(events, mode="webhook")

    async def scenario():
        task = asyncio.create_task(app.run())
        await wait_for(lambda: app.state.webhook_registered)
        async with (
            aiohttp.ClientSession() as client,
            client.post(
                f"http://127.0.0.1:{app.http_port}/webhook/{TOKEN}",
                data=json.dumps(raw_update()),
            ) as resp,
        ):
            assert resp.status == 200
        await wait_for(lambda: pool.calls)
        app.request_stop()
        return await task

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(scenario()) == bot_module.EXIT_OK
    assert telegram["set_webhook_calls"][0]["url"] == f"{WEBHOOK_BASE}/webhook/{TOKEN}"
    assert "WEBHOOK_URL contains the bot token" in caplog.text
    assert TOKEN not in caplog.text


def test_set_webhook_failure_exits_1_after_a_clean_shutdown(
    events, telegram, caplog, monkeypatch
):
    monkeypatch.setattr(config, "WEBHOOK_URL", WEBHOOK_BASE)
    telegram["set_webhook"] = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: bad webhook: HTTPS url must be provided",
    }
    app, _, _ = make_app(events, mode="webhook")
    with caplog.at_level(logging.ERROR, logger="src.bot"):
        assert asyncio.run(app.run()) == bot_module.EXIT_FAILURE
    assert "Telegram refused the webhook: Bad Request" in caplog.text
    assert events[-len(SHUTDOWN_TAIL) :] == SHUTDOWN_TAIL


def test_invalid_webhook_secret_is_a_configuration_error(events, monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_URL", WEBHOOK_BASE)
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "not allowed!")
    app, _, _ = make_app(events, mode="webhook")
    assert asyncio.run(app.run()) == bot_module.EXIT_CONFIG
    assert "init_db" not in events and "pool.start" not in events


# ---------------------------------------------------------------------------
# Startup failures and signals
# ---------------------------------------------------------------------------
def test_rejected_token_exits_2_without_starting_the_pool(events, telegram, caplog):
    telegram["get_me"] = {"ok": False, "error_code": 401, "description": "Unauthorized"}
    app, _, _ = make_app(events)
    with caplog.at_level(logging.ERROR, logger="src.bot"):
        assert asyncio.run(app.run()) == bot_module.EXIT_CONFIG
    assert "Telegram rejected BOT_TOKEN (Unauthorized)" in caplog.text
    assert "pool.start" not in events and "pool.stop.begin" not in events
    assert events[-1] == "http.stop"


def test_unreachable_bot_api_exits_1(events, telegram):
    telegram["get_me"] = {"ok": False, "description": "network error: timeout"}
    app, _, _ = make_app(events)
    assert asyncio.run(app.run()) == bot_module.EXIT_FAILURE


def test_malformed_token_is_a_configuration_error(events):
    app = BotApp("not-a-token", host="127.0.0.1", port=0)
    assert asyncio.run(app.run()) == bot_module.EXIT_CONFIG
    assert "init_db" not in events


def test_busy_port_exits_1(events, caplog):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        app, _, _ = make_app(events, port=busy.getsockname()[1])
        with caplog.at_level(logging.ERROR, logger="src.bot"):
            assert asyncio.run(app.run()) == bot_module.EXIT_FAILURE
    assert "Cannot listen on 127.0.0.1" in caplog.text
    assert "pool.start" not in events


def test_stop_requested_during_startup(events):
    app, _, _ = make_app(events)

    async def scenario():
        app.request_stop()
        return await app.run()

    assert asyncio.run(scenario()) == bot_module.EXIT_OK
    assert "pool.start" not in events and events[-1] == "http.stop"


def test_unexpected_errors_exit_1(events, caplog):
    class BrokenPool(FakePool):
        async def start(self):
            raise RuntimeError("cannot recover the queue")

    app, _, _ = make_app(events, pool=BrokenPool(events))
    with caplog.at_level(logging.ERROR, logger="src.bot"):
        assert asyncio.run(app.run()) == bot_module.EXIT_FAILURE
    assert "The bot failed" in caplog.text
    assert "pool.stop" in events  # start was attempted: stop it


def test_sigterm_triggers_a_graceful_shutdown(events):
    app, _, _ = make_app(events)

    async def scenario():
        task = asyncio.create_task(app.run())
        await wait_for(lambda: "getUpdates" in events)
        # Never send the signal unless the bot installed its handler.
        assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, None)
        os.kill(os.getpid(), signal.SIGTERM)
        return await task

    assert asyncio.run(scenario()) == bot_module.EXIT_OK
    assert events[-len(SHUTDOWN_TAIL) :] == SHUTDOWN_TAIL
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL  # handler removed


def test_second_signal_forces_the_shutdown(events):
    app, _, _ = make_app(events, pool=FakePool(events, stop_delay=30))

    async def scenario():
        task = asyncio.create_task(app.run())
        await wait_for(lambda: "getUpdates" in events)
        app._on_signal(signal.SIGTERM)
        await wait_for(lambda: "pool.stop.begin" in events)
        app._on_signal(signal.SIGINT)
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if app._runner is not None:
                await app._runner.cleanup()

    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started < 10
    assert "pool.stop" not in events


# ---------------------------------------------------------------------------
# Mode selection and webhook settings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("configured", "url", "requested", "expected"),
    [
        ("polling", None, None, "polling"),
        ("webhook", None, None, "polling"),  # no WEBHOOK_URL: poll
        ("webhook", WEBHOOK_BASE, None, "webhook"),
        ("polling", WEBHOOK_BASE, None, "polling"),  # a URL alone changes nothing
        ("polling", None, "webhook", "polling"),
        ("polling", WEBHOOK_BASE, "webhook", "webhook"),
        ("bogus", WEBHOOK_BASE, None, "polling"),
    ],
)
def test_select_mode(monkeypatch, configured, url, requested, expected):
    monkeypatch.setattr(config, "MODE", configured)
    monkeypatch.setattr(config, "WEBHOOK_URL", url)
    assert bot_module.select_mode(requested) == expected


def test_run_falls_back_to_polling_without_webhook_url(monkeypatch, caplog):
    created = []

    class Recorder:
        def __init__(self, token, *, mode):
            created.append((token, mode))

        async def run(self):
            return 0

    monkeypatch.setattr(bot_module, "BotApp", Recorder)
    with caplog.at_level(logging.WARNING, logger="src.bot"):
        assert asyncio.run(bot_module.run("webhook")) == 0
    assert created == [(TOKEN, "polling")]
    assert "falling back to polling" in caplog.text


def test_missing_token_exits_2(monkeypatch, caplog):
    monkeypatch.setattr(config, "BOT_TOKEN", None)
    with caplog.at_level(logging.INFO, logger="src.bot"):
        assert asyncio.run(bot_module.run()) == bot_module.EXIT_CONFIG
    assert "BOT_TOKEN is not set" in caplog.text
    assert "Configuration: {" in caplog.text  # summary logged first


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WEBHOOK_BASE, (WEBHOOK_BASE + "/hook", False)),
        (WEBHOOK_BASE + "/", (WEBHOOK_BASE + "/hook", False)),
        (WEBHOOK_BASE + "/tg/updates", (WEBHOOK_BASE + "/tg/updates", False)),
        (WEBHOOK_BASE + "/x?a=1", (WEBHOOK_BASE + "/x?a=1", False)),
        (WEBHOOK_BASE + "/webhook/{token}", (f"{WEBHOOK_BASE}/webhook/{TOKEN}", True)),
        (f"{WEBHOOK_BASE}/webhook/{TOKEN}", (f"{WEBHOOK_BASE}/webhook/{TOKEN}", True)),
    ],
)
def test_webhook_target(url, expected):
    assert bot_module.webhook_target(url, TOKEN, "/hook") == expected


def test_webhook_settings(monkeypatch, caplog):
    monkeypatch.setattr(config, "WEBHOOK_URL", "https://bot.example.com/other")
    monkeypatch.setattr(config, "WEBHOOK_PATH", "hook")
    with caplog.at_level(logging.INFO, logger="src.bot"):
        settings = bot_module.webhook_settings(TOKEN)
    assert settings.path == "/hook"
    assert settings.url == "https://bot.example.com/other"
    assert len(settings.secret) == 48 and settings.secret == config.webhook_secret()
    assert settings.token_in_url is False
    assert "reverse proxy must forward" in caplog.text
    assert "bot.example.com" not in caplog.text


@pytest.mark.parametrize(
    ("secret", "path"),
    [
        ("with space", "/webhook"),
        ("x" * 257, "/webhook"),
        ("àccento", "/webhook"),
        (None, "/webhook/{token}"),
    ],
)
def test_webhook_settings_reject_invalid_values(monkeypatch, secret, path):
    monkeypatch.setattr(config, "WEBHOOK_URL", WEBHOOK_BASE)
    monkeypatch.setattr(config, "WEBHOOK_SECRET", secret)
    monkeypatch.setattr(config, "WEBHOOK_PATH", path)
    with pytest.raises(StartupError) as raised:
        bot_module.webhook_settings(TOKEN)
    assert raised.value.exit_code == bot_module.EXIT_CONFIG


# ---------------------------------------------------------------------------
# UpdateTracker
# ---------------------------------------------------------------------------
def test_update_tracker_waits_for_running_handlers():
    tracker = bot_module.UpdateTracker()

    async def handler(event, data):
        await asyncio.sleep(0.1)
        return "done"

    async def go():
        assert await tracker.wait_idle(0)
        running = asyncio.create_task(tracker(handler, object(), {}))
        await asyncio.sleep(0)
        assert tracker.active == 1
        assert not await tracker.wait_idle(0.01)
        assert await tracker.wait_idle(1)
        assert await running == "done"
        assert (tracker.active, tracker.handled) == (0, 1)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    aiogram_event = logging.getLogger("aiogram.event")
    saved_aiogram = aiogram_event.level
    yield
    for handler in root.handlers[:]:
        if handler not in saved[0]:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved[1])
    aiogram_event.setLevel(saved_aiogram)
    bot_module._installed_handlers.clear()


def own_handlers():
    return [
        h for h in logging.getLogger().handlers if h in bot_module._installed_handlers
    ]


def test_setup_logging_is_idempotent_and_redacts(
    monkeypatch, tmp_path, restore_logging
):
    log_file = tmp_path / "logs" / "bot.log"
    monkeypatch.setattr(config, "LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(config, "LOG_TO_FILE", True)
    monkeypatch.setattr(config, "LOG_FILE", str(log_file))
    bot_module.setup_logging()
    bot_module.setup_logging()

    handlers = own_handlers()
    assert len(handlers) == 2  # stderr + file, not duplicated
    assert (
        sum(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers) == 1
    )
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("aiogram.event").level == logging.DEBUG
    logging.getLogger("src.test").warning("calling https://x/bot%s/getMe", TOKEN)
    for handler in handlers:
        handler.flush()
    content = log_file.read_text()
    assert "calling https://x/bot<redacted>/getMe" in content
    assert TOKEN not in content


def test_setup_logging_defaults(monkeypatch, restore_logging):
    monkeypatch.setattr(config, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(config, "LOG_TO_FILE", False)
    bot_module.setup_logging()
    assert len(own_handlers()) == 1
    assert logging.getLogger().level == logging.INFO
    # aiogram's per-update INFO line is noise at INFO level.
    assert logging.getLogger("aiogram.event").level == logging.WARNING


def test_setup_logging_problems_are_reported(
    monkeypatch, tmp_path, restore_logging, caplog
):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(config, "LOG_LEVEL", "LOUD")
    monkeypatch.setattr(config, "LOG_TO_FILE", True)
    monkeypatch.setattr(config, "LOG_FILE", str(blocker / "bot.log"))
    with caplog.at_level(logging.WARNING, logger="src.bot"):
        bot_module.setup_logging()
    assert len(own_handlers()) == 1
    assert logging.getLogger().level == logging.INFO
    assert "LOG_LEVEL='LOUD' is not a log level" in caplog.text
    assert "Cannot write the log file" in caplog.text


def test_main_without_token_returns_2(monkeypatch, restore_logging):
    monkeypatch.setattr(config, "BOT_TOKEN", None)
    monkeypatch.setattr(config, "LOG_TO_FILE", False)
    assert bot_module.main() == bot_module.EXIT_CONFIG
