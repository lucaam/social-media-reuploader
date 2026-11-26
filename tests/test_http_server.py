"""HTTP endpoints of the bot process (src.http_server).

The aiohttp app runs on a loopback test server; the webhook routes feed a
real aiogram dispatcher whose worker pool is a fake.
"""

import asyncio
import json
import logging
import sqlite3
import time
from urllib.parse import quote

import aiohttp
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.methods import GetMe, GetUpdates
from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import CONTENT_TYPE_LATEST

from src import config, db, http_server, metrics, providers, telegram_api
from src.bot import create_dispatcher
from src.http_server import HealthState, PollingMonitor, QuietAccessLogger

TOKEN = "123456789:" + "B" * 35
SECRET = "s3cret_token-for-tests"
YT = "https://youtu.be/dQw4w9WgXcQ"


class FakePool:
    def __init__(self):
        self.calls = []

    async def submit(self, chat_id, url, **kwargs):
        self.calls.append((chat_id, url, kwargs))
        return type("Result", (), {"status": "queued"})()


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "requests.db"))
    db.init_db()
    monkeypatch.setattr(config, "HEALTH_DEBUG", False)
    monkeypatch.setattr(config, "WORKER_HEARTBEAT_SECONDS", 5.0)
    monkeypatch.setattr(config, "WEBHOOK_PATH", "/webhook")
    monkeypatch.setattr(config, "UNSUPPORTED_LINK_REPLY", "private")
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 5400)


def heartbeat():
    db.set_worker_status({"pid": 1, "running": [{"request_id": 1}], "queued": 2})


def ready_state(mode="polling"):
    """A state that passes every in-memory check."""
    state = HealthState(mode=mode, pool_started=True)
    if mode == "webhook":
        state.webhook_registered = state.accepting_updates = True
    else:
        state.polling = PollingMonitor()
        state.polling.mark_started()
    return state


async def _forever():
    await asyncio.Event().wait()


def serve(state, scenario, *, app=None):
    """Run ``scenario(client)`` against the app on a loopback test server."""

    async def go():
        task = asyncio.create_task(_forever())
        if state.mode == "polling" and state.polling_task is None:
            state.polling_task = task
        server = TestServer(app or http_server.create_app(state))
        # Same access logger as http_server.start_server().
        await server.start_server(access_log_class=QuietAccessLogger)
        try:
            async with TestClient(server) as client:
                return await scenario(client)
        finally:
            task.cancel()

    return asyncio.run(go())


async def get(client, path):
    resp = await client.get(path)
    return resp.status, await resp.text()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health_is_cheap_and_always_ok(monkeypatch):
    def no_db(*args, **kwargs):
        raise AssertionError("liveness must not touch the database")

    monkeypatch.setattr(db, "get_worker_status", no_db)
    state = HealthState(mode="polling")  # nothing started yet

    async def scenario(client):
        head = await client.head("/health")
        return await get(client, "/health"), head.status

    (status, body), head_status = serve(state, scenario)
    assert (status, body, head_status) == (200, "ok", 200)


def test_health_debug_details(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_DEBUG", True)
    state = ready_state()

    async def scenario(client):
        resp = await client.get("/health")
        return resp.status, await resp.json()

    status, payload = serve(state, scenario)
    assert status == 200
    assert payload["status"] == "ok" and payload["mode"] == "polling"
    assert payload["pool_started"] is True and payload["receiving_updates"] is True
    assert TOKEN not in json.dumps(payload)


# ---------------------------------------------------------------------------
# /ready
# ---------------------------------------------------------------------------
def test_ready_when_everything_is_up():
    heartbeat()

    async def scenario(client):
        return [await get(client, p) for p in ("/ready", "/health?ready=1")]

    assert serve(ready_state(), scenario) == [(200, "ready"), (200, "ready")]


def test_ready_in_webhook_mode():
    heartbeat()
    assert serve(ready_state("webhook"), lambda c: get(c, "/ready"))[0] == 200


@pytest.mark.parametrize(
    "breaks",
    [
        "pool",
        "shutdown",
        "no_heartbeat",
        "polling_dead",
        "polling_stale",
        "webhook_unregistered",
    ],
)
def test_not_ready(breaks):
    mode = "webhook" if breaks == "webhook_unregistered" else "polling"
    state = ready_state(mode)
    if breaks != "no_heartbeat":
        heartbeat()
    if breaks == "pool":
        state.pool_started = False
    elif breaks == "shutdown":
        state.shutting_down = True
    elif breaks == "polling_stale":
        clock = Clock()
        state.polling = PollingMonitor(clock=clock)
        state.polling.mark_started()
        clock.now += http_server.POLLING_STALE_SECONDS + 1
    elif breaks == "webhook_unregistered":
        state.webhook_registered = False

    async def scenario(client):
        if breaks == "polling_dead":
            state.polling_task = asyncio.get_running_loop().create_future()
            state.polling_task.cancel()
        return [await get(client, p) for p in ("/ready", "/health?ready=yes")]

    assert serve(state, scenario) == [(503, "not ready")] * 2


def test_stale_worker_heartbeat_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        db, "get_worker_status", lambda: {"age_seconds": 16.0, "heartbeat_at": "x"}
    )
    assert serve(ready_state(), lambda c: get(c, "/ready"))[0] == 503
    monkeypatch.setattr(config, "WORKER_HEARTBEAT_SECONDS", 10.0)  # 3 x 10 s
    assert serve(ready_state(), lambda c: get(c, "/ready"))[0] == 200


def test_database_errors_make_the_bot_not_ready(monkeypatch):
    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "get_worker_status", locked)
    monkeypatch.setattr(config, "HEALTH_DEBUG", True)

    async def scenario(client):
        resp = await client.get("/ready")
        return resp.status, await resp.json()

    status, payload = serve(ready_state(), scenario)
    assert status == 503
    assert payload["checks"]["db"] == {"ok": False, "error": "OperationalError"}
    assert payload["checks"]["pool"]["ok"] is True


def test_slow_database_times_out(monkeypatch):
    monkeypatch.setattr(http_server, "DB_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(db, "get_worker_status", lambda: time.sleep(0.5))
    started = time.monotonic()
    assert serve(ready_state(), lambda c: get(c, "/ready"))[0] == 503
    assert time.monotonic() - started < 3


def test_ready_debug_payload_has_counts_but_no_contents(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_DEBUG", True)
    db.set_worker_status(
        {"running": [{"url": YT, "chat_id": 5}], "queued": 4, "deferred": 1}
    )

    async def scenario(client):
        resp = await client.get("/ready")
        return resp.status, await resp.json()

    status, payload = serve(ready_state(), scenario)
    assert status == 200 and payload["ready"] is True
    worker = payload["checks"]["worker"]
    assert (worker["running"], worker["queued"], worker["deferred"]) == (1, 4, 1)
    assert YT not in json.dumps(payload)
    assert payload["checks"]["updates"]["polling_alive"] is True


def test_readiness_transitions_are_logged_once(caplog):
    state = ready_state()

    async def scenario(client):
        await get(client, "/ready")
        await get(client, "/ready")
        heartbeat()
        await get(client, "/ready")
        await get(client, "/ready")

    with caplog.at_level(logging.INFO, logger="src.http_server"):
        serve(state, scenario)
    messages = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert messages == [
        ("INFO", "Bot is not ready yet: worker"),
        ("INFO", "Bot is ready"),
    ]


def test_losing_readiness_is_a_warning(caplog, monkeypatch):
    state = ready_state()
    heartbeat()

    async def scenario(client):
        await get(client, "/ready")
        state.pool_started = False
        await get(client, "/ready")

    with caplog.at_level(logging.INFO, logger="src.http_server"):
        serve(state, scenario)
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("INFO", "Bot is ready"),
        ("WARNING", "Bot is no longer ready: pool"),
    ]


# ---------------------------------------------------------------------------
# PollingMonitor
# ---------------------------------------------------------------------------
def test_polling_monitor_tracks_get_updates(caplog):
    clock = Clock()
    monitor = PollingMonitor(clock=clock, stale_after=25)
    bot = Bot(TOKEN)
    outcomes = iter(
        [
            ["u1", "u2"],
            TelegramConflictError(method=GetUpdates(), message="Conflict"),
            TelegramConflictError(method=GetUpdates(), message="Conflict"),
            TelegramNetworkError(method=GetUpdates(), message="timeout"),
            [],
        ]
    )

    async def make_request(bot, method):
        if isinstance(method, GetMe):
            return "me"
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def go():
        assert not monitor.healthy()  # polling not started
        monitor.mark_started()
        assert monitor.healthy()
        assert await monitor(make_request, bot, GetMe()) == "me"
        assert monitor.last_success is None  # other methods are not tracked
        assert await monitor(make_request, bot, GetUpdates()) == ["u1", "u2"]
        for _ in range(3):
            clock.now += 10
            with pytest.raises((TelegramConflictError, TelegramNetworkError)):
                await monitor(make_request, bot, GetUpdates())

    with caplog.at_level(logging.ERROR, logger="src.http_server"):
        asyncio.run(go())
    assert monitor.updates_received == 2
    assert monitor.consecutive_errors == 3
    assert monitor.last_error == "TelegramNetworkError"
    assert monitor.healthy() is False  # last success 30 s ago
    assert caplog.text.count("getUpdates conflict") == 1  # throttled
    asyncio.run(monitor(make_request, bot, GetUpdates()))
    assert monitor.healthy() and monitor.consecutive_errors == 0
    snapshot = monitor.snapshot()
    assert snapshot["last_success_age_seconds"] == 0
    assert snapshot["last_error"] == "TelegramNetworkError"


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------
def test_metrics_endpoint_serves_prometheus_text():
    async def scenario(client):
        resp = await client.get("/metrics")
        return resp.status, resp.headers["Content-Type"]

    status, content_type = serve(ready_state(), scenario)
    assert status == 200 and content_type.startswith("text/plain")


def test_metrics_fall_back_to_the_default_registry(monkeypatch):
    monkeypatch.delattr(metrics, "render", raising=False)
    body, content_type = asyncio.run(http_server.render_metrics())
    assert content_type == CONTENT_TYPE_LATEST
    assert b"python_info" in body


@pytest.mark.parametrize("kind", ["tuple", "bytes", "str", "async"])
def test_metrics_use_the_render_helper(monkeypatch, kind):
    custom = "text/plain; version=0.0.4; charset=utf-8"

    def render():
        if kind == "tuple":
            return b"x_total 1\n", custom
        return "x_total 1\n" if kind == "str" else b"x_total 1\n"

    async def render_async():
        return b"x_total 1\n", custom

    helper = render_async if kind == "async" else render
    monkeypatch.setattr(metrics, "render", helper, raising=False)
    body, content_type = asyncio.run(http_server.render_metrics())
    assert body == b"x_total 1\n"
    expected = custom if kind in ("tuple", "async") else CONTENT_TYPE_LATEST
    assert content_type == expected


# ---------------------------------------------------------------------------
# Webhook routes
# ---------------------------------------------------------------------------
@pytest.fixture
def telegram(monkeypatch):
    sent = []

    async def send_message(token, chat_id, text, reply_to_message_id=None, **kw):
        sent.append((chat_id, text))
        return {"ok": True}

    async def set_message_reaction(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(telegram_api, "send_message", send_message)
    monkeypatch.setattr(telegram_api, "set_message_reaction", set_message_reaction)
    monkeypatch.setattr(providers, "supported_sites_text", lambda: "YouTube")
    return sent


def raw_update(update_id=1, text=YT):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10 + update_id,
            "date": int(time.time()),
            "chat": {"id": 77, "type": "private"},
            "from": {"id": 5, "is_bot": False, "first_name": "Ada"},
            "text": text,
            "entities": [{"type": "url", "offset": 0, "length": len(text)}],
        },
    }


def webhook_app(state, pool):
    bot = Bot(TOKEN)
    dispatcher = create_dispatcher(pool)
    app = http_server.create_app(state)
    http_server.add_webhook_routes(
        app,
        dispatcher=dispatcher,
        bot=bot,
        state=state,
        path="/webhook",
        secret_token=SECRET,
        token=TOKEN,
    )
    return app


async def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def post_json(client, path, payload, secret=None):
    headers = {http_server.SECRET_HEADER: secret} if secret is not None else {}
    return client.post(path, data=json.dumps(payload), headers=headers)


def test_webhook_feeds_updates_with_the_secret_header(telegram):
    pool, state = FakePool(), ready_state("webhook")

    async def scenario(client):
        resp = await post_json(client, "/webhook", raw_update(), SECRET)
        await wait_for(lambda: pool.calls)
        return resp.status, await resp.json()

    status, body = serve(state, scenario, app=webhook_app(state, pool))
    assert (status, body) == (200, {})
    assert pool.calls[0][:2] == (77, YT)
    assert pool.calls[0][2]["original_message_id"] == 11


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x"])
def test_webhook_requires_the_secret(telegram, secret):
    pool, state = FakePool(), ready_state("webhook")

    async def scenario(client):
        resp = await post_json(client, "/webhook", raw_update(), secret)
        await asyncio.sleep(0.05)
        return resp.status

    assert serve(state, scenario, app=webhook_app(state, pool)) == 401
    assert pool.calls == []


async def raw_post(port, path, header_value: bytes):
    """POST with a raw secret header value (clients would refuse non-ASCII)."""
    body = json.dumps(raw_update()).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        b"POST " + path.encode() + b" HTTP/1.1\r\nHost: test\r\n"
        b"Content-Type: application/json\r\n"
        + http_server.SECRET_HEADER.encode()
        + b": "
        + header_value
        + b"\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    await writer.drain()
    status_line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return int(status_line.split()[1])


@pytest.mark.parametrize(
    "header_value",
    ["sécret".encode(), b"s\xe9cret", SECRET.encode() + "é".encode()],
)
def test_webhook_non_ascii_secret_is_unauthorized_not_a_crash(
    telegram, caplog, header_value
):
    pool, state = FakePool(), ready_state("webhook")

    async def scenario(client):
        status = await raw_post(client.port, "/webhook", header_value)
        await asyncio.sleep(0.05)
        return status

    with caplog.at_level(logging.ERROR):
        assert serve(state, scenario, app=webhook_app(state, pool)) == 401
    assert pool.calls == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    ("body", "status"),
    [("{not json", 400), ("[1, 2]", 400), ('{"message": {}}', 400)],
)
def test_webhook_rejects_invalid_bodies(telegram, body, status):
    pool, state = FakePool(), ready_state("webhook")

    async def scenario(client):
        headers = {http_server.SECRET_HEADER: SECRET}
        resp = await client.post("/webhook", data=body, headers=headers)
        return resp.status

    assert serve(state, scenario, app=webhook_app(state, pool)) == status


def test_webhook_answers_503_when_not_accepting_updates(telegram):
    pool, state = FakePool(), ready_state("webhook")
    state.accepting_updates = False

    async def scenario(client):
        resp = await post_json(client, "/webhook", raw_update(), SECRET)
        await asyncio.sleep(0.05)
        return resp.status, resp.headers.get("Retry-After")

    assert serve(state, scenario, app=webhook_app(state, pool)) == (503, "5")
    assert pool.calls == []


def test_legacy_token_route(telegram, caplog):
    pool, state = FakePool(), ready_state("webhook")

    async def scenario(client):
        wrong = await post_json(client, "/webhook/123:wrong", raw_update(1))
        ok = await post_json(client, f"/webhook/{TOKEN}", raw_update(2))
        encoded = await post_json(client, f"/webhook/{quote(TOKEN)}", raw_update(3))
        await wait_for(lambda: len(pool.calls) == 2)
        return wrong.status, ok.status, encoded.status

    with caplog.at_level(logging.WARNING, logger="src.http_server"):
        statuses = serve(state, scenario, app=webhook_app(state, pool))
    assert statuses == (403, 200, 200)
    assert sorted(call[2]["original_message_id"] for call in pool.calls) == [12, 13]
    assert caplog.text.count("deprecated /webhook/<token> route") == 1
    assert TOKEN not in caplog.text


def test_legacy_route_compares_in_constant_time(monkeypatch, telegram):
    compared = []
    real = http_server.hmac.compare_digest

    def spy(a, b):
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(http_server.hmac, "compare_digest", spy)
    pool, state = FakePool(), ready_state("webhook")
    serve(
        state,
        lambda c: post_json(c, "/webhook/nope", raw_update()),
        app=webhook_app(state, pool),
    )
    assert compared == [(b"nope", TOKEN.encode())]


# ---------------------------------------------------------------------------
# Access log
# ---------------------------------------------------------------------------
def test_access_log_hides_probes_queries_and_tokens(caplog, telegram):
    pool, state = FakePool(), ready_state("webhook")
    heartbeat()

    async def scenario(client):
        await client.get("/health")
        await client.get("/ready")
        await client.get("/metrics")
        await client.get("/missing?api_key=12345")
        await post_json(client, f"/webhook/{TOKEN}", raw_update(1))
        await post_json(client, "/webhook", raw_update(2), "bad")
        await post_json(client, "/webhook", raw_update(3), SECRET)
        await wait_for(lambda: len(pool.calls) == 2)

    with caplog.at_level(logging.INFO, logger="aiohttp.access"):
        serve(state, scenario, app=webhook_app(state, pool))
    lines = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert len(lines) == 2, lines
    assert '"GET /missing" 404' in lines[0]
    assert '"POST /webhook" 401' in lines[1]
    assert "12345" not in caplog.text and TOKEN not in caplog.text


def test_access_log_debug_level_shows_probes_with_redacted_paths(caplog, telegram):
    pool, state = FakePool(), ready_state("webhook")

    async def scenario(client):
        await client.get("/health")
        await post_json(client, f"/webhook/{TOKEN}", raw_update(1))
        await wait_for(lambda: pool.calls)

    with caplog.at_level(logging.DEBUG, logger="aiohttp.access"):
        serve(state, scenario, app=webhook_app(state, pool))
    lines = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert any('"GET /health" 200' in line for line in lines)
    assert any('"POST /webhook/<redacted>" 200' in line for line in lines)
    assert TOKEN not in caplog.text


def test_health_debug_logs_probes_at_info(monkeypatch, caplog):
    monkeypatch.setattr(config, "HEALTH_DEBUG", True)
    with caplog.at_level(logging.INFO, logger="aiohttp.access"):
        serve(ready_state(), lambda c: c.get("/health"))
    assert '"GET /health" 200' in caplog.text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/webhook/{TOKEN}", "/webhook/<redacted>"),
        ("/webhook/", "/webhook/"),
        ("/webhook", "/webhook"),
        (f"/bot{TOKEN}/x", "/bot<redacted>/x"),
        ("/health", "/health"),
    ],
)
def test_sanitize_path(path, expected):
    assert http_server.sanitize_path(path) == expected


# ---------------------------------------------------------------------------
# start_server
# ---------------------------------------------------------------------------
def test_start_server_listens_and_stops(caplog):
    state = HealthState()

    async def go():
        runner = await http_server.start_server(
            http_server.create_app(state), "127.0.0.1", 0
        )
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                assert (resp.status, await resp.text()) == (200, "ok")
            async with session.get(f"http://127.0.0.1:{port}/bot{TOKEN}?q=1") as resp:
                assert resp.status == 404
        await runner.cleanup()
        with pytest.raises(aiohttp.ClientConnectionError):
            async with aiohttp.ClientSession() as session:
                await session.get(f"http://127.0.0.1:{port}/health")

    with caplog.at_level(logging.INFO, logger="aiohttp.access"):
        asyncio.run(go())
    lines = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert len(lines) == 1  # the probe is not logged at INFO
    assert '"GET /bot<redacted>" 404' in lines[0]
    assert TOKEN not in caplog.text


def test_start_server_reports_a_busy_port():
    async def go():
        first = await http_server.start_server(
            http_server.create_app(HealthState()), "127.0.0.1", 0
        )
        port = first.addresses[0][1]
        try:
            with pytest.raises(OSError):
                await http_server.start_server(
                    http_server.create_app(HealthState()), "127.0.0.1", port
                )
        finally:
            await first.cleanup()

    asyncio.run(go())


def test_access_log_levels_follow_the_webhook_path(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_PATH", "tg/hook")  # normalized to /tg/hook
    access = QuietAccessLogger(logging.getLogger("aiohttp.access"), "")
    assert access._level("/tg/hook", 200) == logging.DEBUG
    assert access._level("/tg/hook", 401) == logging.INFO
    assert access._level("/webhook/<redacted>", 200) == logging.DEBUG
    assert access._level("/other", 200) == logging.INFO
    assert access._level("/ready", 503) == logging.DEBUG
