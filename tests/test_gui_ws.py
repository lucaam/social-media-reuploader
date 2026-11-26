"""Live updates: WebSocket auth, DB event tailing, hub and lifespan."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse
from starlette.websockets import WebSocketDisconnect

from src import db, gui, ws_broadcast
from src.gui_auth import AuthSettings

TOKEN = "admin-token-0123456789abcdef"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
SAME = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "requests.db"
    monkeypatch.setenv("REQUESTS_DB", str(path))
    db.init_db()
    return path


def make_app(**kwargs):
    settings = kwargs.pop("settings", None) or AuthSettings(
        secret_key="k" * 40, admin_token=TOKEN
    )
    kwargs.setdefault("poll_interval", 0.05)
    return gui.create_app(settings, **kwargs)


def recv(ws, timeout=5.0):
    """Next JSON message (raises WebSocketDisconnect on close), with a timeout."""

    async def _next():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    message = ws.portal.call(_next)
    ws._raise_on_close(message)
    return json.loads(message["text"])


def refused_code(client, url="/ws/updates", headers=None):
    """Close code of a WebSocket handshake that the server refuses."""
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(url, headers=headers or {}),
    ):
        pass
    return refused.value.code


class OAuthProvider:
    def __init__(self, email):
        self.email = email

    async def authorize_redirect(self, request, redirect_uri):
        return RedirectResponse("/auth")

    async def authorize_access_token(self, request):
        userinfo = {"sub": "u", "email": self.email, "email_verified": True}
        return {"access_token": "t", "userinfo": userinfo}


# ---------------------------------------------------------------------------
# Authentication before accept
# ---------------------------------------------------------------------------
def test_websocket_requires_authentication_before_accept():
    with TestClient(make_app()) as client:
        for headers in ({}, SAME, {"Authorization": "Bearer wrong"}):
            assert refused_code(client, headers=headers) == 1008
        assert refused_code(client, f"/ws/updates?token={TOKEN}") == 1008


def test_websocket_bearer_and_session_authentication():
    with TestClient(make_app()) as client:
        with client.websocket_connect("/ws/updates", headers=BEARER) as ws:
            hello = recv(ws)
            assert hello["kind"] == "hello"
            assert isinstance(hello["last_event_id"], int)
        assert (
            client.post("/login", json={"token": TOKEN}, headers=SAME).status_code
            == 200
        )
        with client.websocket_connect("/ws/updates", headers=SAME) as ws:
            assert recv(ws)["kind"] == "hello"
        # a cookie session must come from the GUI's own origin
        for headers in ({}, {"Origin": "https://evil.example.com"}):
            assert refused_code(client, headers=headers) == 1008


def test_websocket_rejects_non_admin_sessions_and_db_errors(monkeypatch):
    app = make_app(
        settings=AuthSettings(secret_key="k" * 40),
        oauth_registry=SimpleNamespace(provider=OAuthProvider("user@example.com")),
    )
    with TestClient(app) as client:
        client.get("/login")
        assert client.get("/api/me").json()["authenticated"] is True
        assert refused_code(client, headers=SAME) == 1008

        def broken(email):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db, "get_user_by_email", broken)
        assert refused_code(client, headers=SAME) == 1008


# ---------------------------------------------------------------------------
# Event tailing
# ---------------------------------------------------------------------------
def test_each_new_event_is_streamed_once_in_order_without_history_replay():
    old = db.create_request(1, "https://example.com/old")
    history_end = db.latest_event_id()
    app = make_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/updates", headers=BEARER) as ws:
            assert recv(ws)["kind"] == "hello"
            first = db.create_request(2, "https://example.com/new")
            assert db.claim_request(first)
            assert db.mark_done(first, sent_message_id=7, media_type="video")
            db.cancel_queued(chat_id=1, actor="test")
            wanted = list(range(history_end + 1, db.latest_event_id() + 1))
            events = []
            while not set(wanted) <= {event["id"] for event in events}:
                message = recv(ws)
                if message["kind"] == "events":
                    events.extend(message["events"])
                    assert message["last_id"] == message["events"][-1]["id"]
            ids = [event["id"] for event in events]
            assert ids == sorted(set(ids))  # exactly once, in order
            assert min(ids) > history_end  # nothing from before startup
            types = [event["type"] for event in events]
            assert types[0] == "request_created"
            statuses = [e["status"] for e in events if e["type"] == "request_status"]
            assert statuses[:3] == ["processing", "done", "cancelled"]
            cancelled = [e for e in events if e.get("request_id") == old]
            assert cancelled and cancelled[0]["actor"] == "test"
            assert all(event["created_at"].endswith("+00:00") for event in events)
        assert app.state.gui.tailer.last_id >= max(wanted)


def test_two_clients_receive_the_same_batches():
    with TestClient(make_app()) as client:
        with (
            client.websocket_connect("/ws/updates", headers=BEARER) as first,
            client.websocket_connect("/ws/updates", headers=BEARER) as second,
        ):
            recv(first)
            recv(second)
            assert client.app.state.gui.hub.client_count == 2
            request_id = db.create_request(3, "https://example.com/x")
            for ws in (first, second):
                message = recv(ws)
                while message["kind"] != "events":
                    message = recv(ws)
                assert message["events"][0]["request_id"] == request_id
        assert client.app.state.gui.hub.client_count == 0


def test_worker_status_changes_are_pushed():
    app = make_app()
    app.state.gui.tailer.status_interval = 0.1
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/updates", headers=BEARER) as ws,
    ):
        recv(ws)
        db.set_worker_status({"pid": 99, "running": []})
        message = recv(ws)
        while message["kind"] != "worker" or message["worker"]["state"] != "online":
            message = recv(ws)
        assert message["worker"]["pid"] == 99


def test_websocket_is_closed_when_admin_rights_are_revoked():
    uid = db.add_user(email="boss@example.com", role="admin")
    app = make_app(
        settings=AuthSettings(secret_key="k" * 40),
        oauth_registry=SimpleNamespace(provider=OAuthProvider("boss@example.com")),
        ws_recheck_seconds=0.05,
    )
    with TestClient(app) as client:
        client.get("/login")
        with client.websocket_connect("/ws/updates", headers=SAME) as ws:
            assert recv(ws)["kind"] == "hello"
            db.set_user_role(uid, "user")
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    recv(ws)
            assert closed.value.code == 1008


def test_slow_clients_are_dropped_and_shutdown_closes_sockets():
    app = make_app()
    hub = app.state.gui.hub
    with TestClient(app) as client:
        hub.queue_size = 2
        with client.websocket_connect("/ws/updates", headers=BEARER) as ws:
            assert recv(ws)["kind"] == "hello"

            def burst():
                for number in range(10):
                    hub.publish({"kind": "events", "events": [], "n": number})

            client.portal.call(burst)
            with pytest.raises(WebSocketDisconnect) as dropped:
                while True:
                    recv(ws)
            assert dropped.value.code == 1013
        hub.queue_size = 64
        with client.websocket_connect("/ws/updates", headers=BEARER) as ws:
            assert recv(ws)["kind"] == "hello"
            client.portal.call(hub.close_all)
            with pytest.raises(WebSocketDisconnect) as going_away:
                while True:
                    recv(ws)
            assert going_away.value.code == 1001


# ---------------------------------------------------------------------------
# Hub and tailer units
# ---------------------------------------------------------------------------
def test_hub_bounded_queues():
    async def scenario():
        hub = ws_broadcast.Hub(queue_size=2)
        slow = hub.subscribe()
        fast = hub.subscribe()
        assert hub.publish({"n": 1}) == 2
        assert await fast.get() == {"n": 1}
        assert hub.publish({"n": 2}) == 2
        assert await fast.get() == {"n": 2}
        assert hub.publish({"n": 3}) == 1  # slow overflows and is dropped
        assert slow.dropped and slow.closed
        assert hub.client_count == 1
        assert await slow.get() is None
        assert await slow.get() is None
        assert slow.offer({"n": 4}) is False
        assert fast.pending() == 1
        hub.close_all()  # shutdown: pending messages are discarded
        assert await fast.get() is None
        assert await fast.get() is None
        assert hub.client_count == 0
        unsubscribed = hub.subscribe()
        hub.unsubscribe(unsubscribed)
        assert unsubscribed.closed and hub.client_count == 0

    asyncio.run(scenario())


def fake_log(count):
    return [
        {"id": i, "created_at": "2026-01-01T00:00:00", "type": "t"}
        for i in range(1, count + 1)
    ]


def test_tailer_publishes_batches_after_the_cursor():
    events = fake_log(7)
    calls = []

    def list_after(last, limit):
        calls.append((last, limit))
        return [event for event in events if event["id"] > last][:limit]

    async def scenario():
        hub = ws_broadcast.Hub()
        sub = hub.subscribe()
        tailer = ws_broadcast.EventTailer(
            hub, batch_size=2, list_events_after=list_after, latest_event_id=lambda: 3
        )
        published = [await tailer.poll_once() for _ in range(4)]
        assert published == [2, 2, 0, 0]
        assert tailer.last_id == 7
        batches = [await sub.get(), await sub.get()]
        assert [[e["id"] for e in b["events"]] for b in batches] == [[4, 5], [6, 7]]
        assert sub.pending() == 0

    asyncio.run(scenario())
    assert calls[0] == (3, 2)


def test_tailer_cursor_retries_and_never_starts_from_zero():
    attempts = []

    def latest():
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return 41

    async def scenario():
        tailer = ws_broadcast.EventTailer(
            ws_broadcast.Hub(),
            poll_interval=0.01,
            list_events_after=lambda last, limit: [],
            latest_event_id=latest,
        )
        assert await tailer.init_cursor() == 41

    asyncio.run(scenario())
    assert len(attempts) == 3


def test_tailer_follows_a_reset_log():
    async def scenario():
        tailer = ws_broadcast.EventTailer(
            ws_broadcast.Hub(),
            list_events_after=lambda last, limit: [],
            latest_event_id=lambda: 5,
        )
        tailer.last_id = 100
        for _ in range(ws_broadcast._RESET_CHECK_EVERY):
            await tailer.poll_once()
        assert tailer.last_id == 5

    asyncio.run(scenario())


def test_tailer_loop_survives_errors_and_stops_cleanly():
    responses = [
        sqlite3.OperationalError("locked"),
        RuntimeError("bug"),
        fake_log(2),
    ]

    def list_after(last, limit):
        item = responses.pop(0) if responses else []
        if isinstance(item, Exception):
            raise item
        return [event for event in item if event["id"] > last]

    async def scenario():
        hub = ws_broadcast.Hub()
        sub = hub.subscribe()
        tailer = ws_broadcast.EventTailer(
            hub,
            poll_interval=0.01,
            list_events_after=list_after,
            latest_event_id=lambda: 0,
        )
        await tailer.start()
        assert tailer.running
        message = await asyncio.wait_for(sub.get(), timeout=5)
        assert [event["id"] for event in message["events"]] == [1, 2]
        await tailer.stop()
        assert not tailer.running
        assert tailer.last_id is None

    asyncio.run(scenario())


def test_tailer_publishes_worker_status_only_on_change():
    statuses = [
        {"state": "online", "heartbeat_at": "a"},
        {"state": "online", "heartbeat_at": "a"},
        {"state": "offline", "heartbeat_at": "a"},
    ]

    async def scenario():
        hub = ws_broadcast.Hub()
        sub = hub.subscribe()
        tailer = ws_broadcast.EventTailer(
            hub,
            status_interval=0.1,
            status_source=lambda: statuses.pop(0),
            list_events_after=lambda last, limit: [],
            latest_event_id=lambda: 0,
        )
        assert await tailer.publish_status(force=True) is True
        assert await tailer.publish_status() is False  # interval not elapsed
        assert await tailer.publish_status(force=True) is False  # unchanged
        assert await tailer.publish_status(force=True) is True
        messages = [await sub.get(), await sub.get()]
        assert [m["worker"]["state"] for m in messages] == ["online", "offline"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
def test_lifespan_starts_and_stops_the_tailer(tmp_path, monkeypatch):
    fresh = tmp_path / "sub" / "fresh.db"
    monkeypatch.setenv("REQUESTS_DB", str(fresh))
    app = make_app()
    tailer = app.state.gui.tailer
    assert not tailer.running
    with TestClient(app) as client:
        assert fresh.exists()  # init_db ran at startup
        assert tailer.running
        assert client.get("/health").json()["ok"] is True
    assert not tailer.running
    # the same app can be started again (new event loop)
    with TestClient(app):
        assert tailer.running
    assert not tailer.running


def test_lifespan_refuses_to_start_without_any_login_method():
    app = make_app(settings=AuthSettings(secret_key="k" * 40))
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"), TestClient(app):
        pass
    oauth_only = make_app(
        settings=AuthSettings(secret_key="k" * 40),
        oauth_registry=SimpleNamespace(provider=OAuthProvider("x@example.com")),
    )
    with TestClient(oauth_only):
        assert oauth_only.state.gui.tailer.running
