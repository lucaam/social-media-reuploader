"""End to end: the bot process against a local fake Bot API server.

Nothing of aiogram, telegram_api, telegram_client or http_client is faked:
``config.TELEGRAM_API_BASE`` points them at an aiohttp server on loopback
that answers like Telegram. Only the worker pool is a fake (it records
submit() calls and publishes a heartbeat like the real one).
"""

import asyncio
import json
import logging
import os
import time

import aiohttp
import pytest
from aiohttp import web

from src import bot as bot_module
from src import config, db, handlers, http_server, providers
from src.bot import BotApp

TOKEN = "123456789:" + "E" * 35
YT = "https://youtu.be/dQw4w9WgXcQ"
UNSUPPORTED = "https://example.com/video/1"


class FakePool:
    def __init__(self):
        self.calls = []
        self.stopped = False

    async def start(self):
        await db.aio(db.set_worker_status, {"pid": os.getpid(), "running": []})

    async def stop(self):
        self.stopped = True

    async def submit(self, chat_id, url, **kwargs):
        self.calls.append((chat_id, url, kwargs))
        return type("Result", (), {"status": "queued"})()


class FakeBotAPI:
    """Answers Bot API calls on ``POST /bot<token>/<method>``."""

    def __init__(self, updates=()):
        self.updates = list(updates)
        self.calls = []

    async def handle(self, request):
        if request.match_info["token"] != TOKEN:
            return web.json_response(
                {"ok": False, "error_code": 401, "description": "Unauthorized"},
                status=401,
            )
        method = request.match_info["method"]
        if request.content_type == "application/json":
            params = await request.json()
        else:  # aiogram sends form fields, complex values as JSON strings
            params = dict(await request.post())
        self.calls.append((method, params))
        if method == "getMe":
            # BotFullInfo (Bot API 10.5): aiogram's polling parses it strictly
            result = {
                "id": 123456789,
                "is_bot": True,
                "first_name": "Test",
                "username": "t_bot",
                "can_join_groups": True,
                "can_read_all_group_messages": True,
                "supports_inline_queries": False,
                "supports_guest_queries": False,
                "supports_join_request_queries": False,
                "needs_inline_query_location": False,
                "can_connect_to_business": False,
                "has_main_web_app": False,
                "has_topics_enabled": False,
                "allows_users_to_create_topics": False,
                "has_topic_sidebar": False,
                "can_manage_bots": False,
            }
        elif method == "getUpdates":
            if self.updates:
                result, self.updates = self.updates, []
            else:
                await asyncio.sleep(0.05)  # a (very) short long poll
                result = []
        else:
            result = True
        return web.json_response({"ok": True, "result": result})

    def methods(self):
        return [method for method, _ in self.calls]

    def params(self, method):
        return [params for name, params in self.calls if name == method]


def message_update(update_id, text, chat_id=77):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 100 + update_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 5, "is_bot": False, "first_name": "Ada"},
            "text": text,
            "entities": [{"type": "url", "offset": 0, "length": len(text)}],
        },
    }


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "requests.db"))
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "WEBHOOK_URL", None)
    monkeypatch.setattr(config, "WEBHOOK_PATH", "/webhook")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", None)
    monkeypatch.setattr(config, "HEALTH_DEBUG", False)
    monkeypatch.setattr(config, "UNSUPPORTED_LINK_REPLY", "private")
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 5400)
    monkeypatch.setattr(config, "TELEGRAM_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ())
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ())
    monkeypatch.setattr(providers, "supported_sites_text", lambda: "YouTube")


async def start_fake_api(api):
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", api.handle)
    runner = web.AppRunner(app, access_log=None)  # its paths contain the token
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


async def wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def http(method, url, **kwargs):
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, url, **kwargs) as resp,
    ):
        return resp.status, await resp.text()


def test_polling_end_to_end(monkeypatch, caplog):
    api = FakeBotAPI(updates=[message_update(1, YT), message_update(2, UNSUPPORTED)])
    pool = FakePool()

    async def scenario():
        runner, base = await start_fake_api(api)
        monkeypatch.setattr(config, "TELEGRAM_API_BASE", base)
        app = BotApp(
            TOKEN, mode="polling", host="127.0.0.1", port=0, pool_factory=lambda t: pool
        )
        task = asyncio.create_task(app.run())
        try:
            await wait_for(lambda: pool.calls and "setMessageReaction" in api.methods())
            await wait_for(lambda: api.methods().count("getUpdates") >= 2)
            assert await http("GET", f"http://127.0.0.1:{app.http_port}/ready") == (
                200,
                "ready",
            )
            app.request_stop()
            return await task
        finally:
            await runner.cleanup()

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(scenario()) == bot_module.EXIT_OK

    methods = api.methods()
    assert methods[:2] == ["getMe", "deleteWebhook"]  # token check, then webhook off
    assert methods.index("deleteWebhook") < methods.index("getUpdates")
    first_poll = api.params("getUpdates")[0]
    assert json.loads(first_poll["allowed_updates"]) == list(handlers.ALLOWED_UPDATES)
    assert api.params("getUpdates")[1]["offset"] == "3"  # both updates confirmed

    assert [(c[0], c[1]) for c in pool.calls] == [(77, YT)]
    assert pool.calls[0][2]["original_message_id"] == 101
    assert pool.stopped

    reply = api.params("sendMessage")[0]
    assert reply["chat_id"] == 77
    assert reply["text"] == "Link non supportato. Supporto YouTube."
    assert reply["reply_parameters"] == {
        "message_id": 102,
        "allow_sending_without_reply": True,
    }
    reaction = api.params("setMessageReaction")[0]
    assert reaction["reaction"] == [{"type": "emoji", "emoji": "\U0001f44e"}]
    assert TOKEN not in caplog.text


def test_webhook_end_to_end(monkeypatch, caplog):
    api = FakeBotAPI()
    pool = FakePool()
    monkeypatch.setattr(config, "WEBHOOK_URL", "https://bot.example.com")
    secret = config.webhook_secret()

    async def scenario():
        runner, base = await start_fake_api(api)
        monkeypatch.setattr(config, "TELEGRAM_API_BASE", base)
        app = BotApp(
            TOKEN, mode="webhook", host="127.0.0.1", port=0, pool_factory=lambda t: pool
        )
        task = asyncio.create_task(app.run())
        try:
            await wait_for(lambda: app.state.webhook_registered)
            url = f"http://127.0.0.1:{app.http_port}/webhook"
            body = json.dumps(message_update(1, YT))
            assert (await http("POST", url, data=body))[0] == 401
            headers = {http_server.SECRET_HEADER: secret}
            assert (await http("POST", url, data=body, headers=headers))[0] == 200
            await wait_for(lambda: pool.calls)
            ready = await http("GET", f"http://127.0.0.1:{app.http_port}/ready")
            assert ready == (200, "ready")
            app.request_stop()
            return await task
        finally:
            await runner.cleanup()

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(scenario()) == bot_module.EXIT_OK

    assert api.methods() == ["getMe", "setWebhook"]  # never polls in webhook mode
    registered = api.params("setWebhook")[0]
    assert registered == {
        "url": "https://bot.example.com/webhook",
        "secret_token": secret,
        "allowed_updates": list(handlers.ALLOWED_UPDATES),
    }
    assert [(c[0], c[1]) for c in pool.calls] == [(77, YT)]
    for hidden in (TOKEN, secret, "bot.example.com"):
        assert hidden not in caplog.text
