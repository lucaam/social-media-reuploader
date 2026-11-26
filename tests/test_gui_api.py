"""Admin GUI JSON API against a real temporary database."""

import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

from src import config, db, gui, gui_queries, ratelimit
from src.gui_auth import AuthSettings

TOKEN = "admin-token-0123456789abcdef"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
SAME = {"Origin": "http://testserver"}
ISO_OFFSET = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")


@pytest.fixture(autouse=True)
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "requests.db"
    monkeypatch.setenv("REQUESTS_DB", str(path))
    db.init_db()
    return path


def make_app(**overrides):
    values = {"secret_key": "k" * 40, "admin_token": TOKEN}
    values.update(overrides)
    return gui.create_app(AuthSettings(**values))


@pytest.fixture
def client():
    return TestClient(make_app(), headers=BEARER)


def iso_ago(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(timespec="microseconds")


def set_column(request_id, column, value):
    with db.write() as conn:
        conn.execute(
            f"UPDATE requests SET {column} = ? WHERE id = ?", (value, request_id)
        )


def make_done(chat_id, url, *, original=None, final=None, compressed=None, **kw):
    request_id = db.create_request(chat_id, url, **kw)
    assert db.claim_request(request_id)
    assert db.mark_done(
        request_id, original_size=original, final_size=final, compressed=compressed
    )
    return request_id


def make_failed(chat_id, url, code="unavailable", **kw):
    request_id = db.create_request(chat_id, url, **kw)
    assert db.claim_request(request_id)
    assert db.mark_failed(request_id, error_code=code, error_message="boom " * 10)
    return request_id


def refused(chat_id, url, normalized=None, status="rate_limited", **kw):
    code = "duplicate" if status == "duplicate" else "rate_limited"
    return db.create_request(
        chat_id,
        url,
        status=status,
        normalized_url=normalized or url,
        error_code=code,
        **kw,
    )


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def test_requests_pagination_search_and_filters(client):
    ids = [
        db.create_request(
            100 + i % 3,
            f"https://example.com/video/{i}",
            description="un gatto" if i % 5 == 0 else None,
        )
        for i in range(30)
    ]
    page = client.get("/api/requests", params={"limit": 10}).json()
    assert page["total"] == 30
    assert [item["id"] for item in page["items"]] == ids[::-1][:10]
    last = client.get("/api/requests", params={"limit": 10, "offset": 20}).json()
    assert [item["id"] for item in last["items"]] == ids[::-1][20:]

    def total(**params):
        return client.get("/api/requests", params=params).json()["total"]

    assert total(q="video/1") == 11
    assert total(q="  GATTO ") == 6
    assert total(q="101") == 10
    assert total(q="100%") == 0  # LIKE wildcards are escaped
    assert total(chat_id=102) == 10
    assert total(status="queued") == 30
    assert total(status="done") == 0
    assert total(status="done,queued") == 30
    assert total(status="all") == 30
    # chat 101 = i % 3 == 1; "video/1" matches 1, 10, 13, 16, 19
    assert total(status="queued", chat_id=101, q="video/1") == 5
    # the legacy path is an alias
    assert client.get("/requests").json()["total"] == 30
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"chat_id": "x"}):
        assert client.get("/api/requests", params=params).status_code == 422
    bad = client.get("/api/requests", params={"status": "done; DROP"})
    assert bad.status_code == 400
    assert bad.json() == {"detail": "Filtro di stato non valido"}


def test_request_items_shape_and_timestamps(client):
    done = make_done(
        5,
        "https://youtu.be/abc",
        original=2000,
        final=1000,
        compressed=True,
        provider="Youtube",
        normalized_url="https://youtube.com/watch?v=abc",
        chat_type="private",
    )
    deferred = db.create_request(5, "https://vimeo.com/1", not_before=iso_ago(hours=-1))
    legacy = db.create_request(5, "https://vimeo.com/2")
    set_column(legacy, "created_at", "2026-01-02T03:04:05")  # naive (legacy writer)
    items = {item["id"]: item for item in client.get("/api/requests").json()["items"]}
    item = items[done]
    assert item["status"] == "done"
    assert item["provider_name"] == "YouTube"
    assert item["chat_type"] == "private"
    assert item["original_size"] == 2000
    assert item["final_size"] == 1000
    assert item["compressed"] is True
    assert item["requeueable"] is False
    assert item["cancellable"] is False
    for key in (
        "created_at",
        "updated_at",
        "processing_started_at",
        "processing_finished_at",
    ):
        assert ISO_OFFSET.match(item[key]), (key, item[key])
    assert items[deferred]["deferred"] is True
    assert items[deferred]["cancellable"] is True
    assert ISO_OFFSET.match(items[deferred]["not_before"])
    assert items[legacy]["deferred"] is False
    assert items[legacy]["created_at"] == "2026-01-02T03:04:05.000000+00:00"
    assert "events" not in item  # no N+1: events only in the detail view


def test_request_detail_and_404(client):
    request_id = make_failed(9, "https://example.com/v")
    detail = client.get(f"/api/requests/{request_id}").json()
    assert detail["id"] == request_id
    assert detail["status"] == "failed"
    assert detail["error_code"] == "unavailable"
    assert detail["requeueable"] is True
    assert [event["type"] for event in detail["events"]] == ["failed", "processing"]
    assert all(ISO_OFFSET.match(event["created_at"]) for event in detail["events"])
    assert client.get(f"/requests/{request_id}").json()["id"] == request_id
    missing = client.get("/api/requests/999999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Richiesta non trovata"}


def test_requeue_by_ids(client):
    failed = make_failed(1, "https://example.com/a")
    done = make_done(1, "https://example.com/b")
    blocked = refused(1, "https://example.com/c")
    db.create_request(
        1, "https://example.com/c", normalized_url="https://example.com/c"
    )
    response = client.post(
        "/api/requests/requeue", json={"ids": [failed, done, blocked, 999, failed]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 4
    assert body["requeued"] == [failed]
    assert body["skipped"] == [done, blocked, 999]
    row = db.get_request(failed)
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert row["error_code"] is None


def test_requeue_by_filter(client):
    first = make_failed(1, "https://tiktok.com/@a/video/1")
    second = make_failed(2, "https://tiktok.com/@b/video/2")
    make_failed(3, "https://vimeo.com/3")
    make_done(4, "https://tiktok.com/@c/video/4")
    response = client.post(
        "/api/requests/requeue", json={"status": "failed", "q": "tiktok"}
    )
    assert sorted(response.json()["requeued"]) == [first, second]
    assert db.count_requests(status="failed") == 1
    assert (
        client.post("/api/requests/requeue", json={"status": "done"}).status_code == 400
    )
    assert client.post("/api/requests/requeue", json={}).status_code == 400
    too_many = client.post("/api/requests/requeue", json={"ids": list(range(1001))})
    assert too_many.status_code == 422
    unknown = client.post("/api/requests/requeue", json={"ids": [1], "x": 1})
    assert unknown.status_code == 422


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
def test_cancel_queue_per_chat_and_all(client):
    a1 = db.create_request(1, "https://example.com/1")
    a2 = db.create_request(1, "https://example.com/2")
    a3 = db.create_request(1, "https://example.com/3", not_before=iso_ago(hours=-2))
    b1 = db.create_request(2, "https://example.com/4")
    running = db.create_request(1, "https://example.com/5")
    assert db.claim_request(running)
    response = client.post("/api/queue/cancel", json={"chat_id": 1})
    assert response.json() == {
        "ok": True,
        "chat_id": 1,
        "cancelled": 3,
        "ids": [a1, a2, a3],
    }
    assert db.get_request(b1)["status"] == "queued"
    assert db.get_request(running)["status"] == "processing"
    everything = client.post("/api/queue/cancel")
    assert everything.json()["cancelled"] == 1
    assert db.get_request(b1)["status"] == "cancelled"
    log = client.get("/api/events", params={"type": "queue_cleared"}).json()
    assert log["total"] == 2
    assert {item["actor"] for item in log["items"]} == {"gui:bearer"}
    history = client.get(f"/api/requests/{a1}").json()["events"]
    assert history[0]["type"] == "cancelled"
    assert "gui:bearer" in history[0]["details"]


def test_queue_view_and_worker_states(client):
    queued = db.create_request(1, "https://example.com/1")
    deferred = db.create_request(
        1, "https://example.com/2", not_before=iso_ago(hours=-1)
    )
    running = db.create_request(2, "https://example.com/3")
    assert db.claim_request(running)
    make_failed(3, "https://example.com/4")

    view = client.get("/api/queue").json()
    assert view["worker"]["state"] == "unknown"
    assert view["counts"] == {
        "queued": 1,
        "deferred": 1,
        "processing": 1,
        "sending": 0,
        "running": 1,
    }
    assert [item["id"] for item in view["queued"]] == [queued]
    assert [item["id"] for item in view["deferred"]] == [deferred]
    assert view["running"][0]["id"] == running
    assert view["running"][0]["stale"] is True
    assert view["retryable"][0]["status"] == "failed"
    assert ISO_OFFSET.match(view["generated_at"])

    db.set_worker_status(
        {
            "pid": 42,
            "version": "1.2.3",
            "started_at": time.time() - 60,
            "workers": 2,
            "mode": "polling",
            "running": [
                {
                    "request_id": running,
                    "chat_id": 2,
                    "stage": "download",
                    "started_at": time.time(),
                }
            ],
            "rate_limits": [[1, 10], [3, 60]],
            "max_pending_per_chat": 3,
            "duplicate_window_seconds": 5400,
        }
    )
    online = client.get("/api/queue").json()
    worker = online["worker"]
    assert worker["state"] == "online"
    assert worker["online"] is True
    assert worker["pid"] == 42
    assert ISO_OFFSET.match(worker["started_at"])
    assert online["running"][0]["stage"] == "download"
    assert online["running"][0]["stale"] is False

    stale_after = worker["stale_after_seconds"]
    assert stale_after == max(3 * config.WORKER_HEARTBEAT_SECONDS, 10)
    with db.write() as conn:
        conn.execute(
            "UPDATE worker_status SET heartbeat_at = ?",
            (iso_ago(seconds=stale_after + 5),),
        )
    offline = client.get("/api/queue").json()
    assert offline["worker"]["state"] == "offline"
    assert offline["running"][0]["stale"] is True
    assert client.get("/api/worker").json()["state"] == "offline"


def test_worker_heartbeat_interval_and_stopped_state():
    status = {"age_seconds": 100.0, "heartbeat_seconds": 60}
    assert gui_queries.worker_summary(status)["state"] == "online"
    assert gui_queries.worker_summary({"age_seconds": 1, "state": "stopped"})[
        "state"
    ] == ("stopped")
    summary = gui_queries.worker_summary(
        {"age_seconds": 1, "rate_limits": [{"count": 2, "seconds": 30}, "junk", [0, 5]]}
    )
    assert summary["limits"]["rate_limits"] == [{"count": 2, "seconds": 30}]
    assert gui_queries.effective_limits(summary)["source"] == "worker"
    unknown = gui_queries.worker_summary(None)
    assert gui_queries.effective_limits(unknown)["source"] == "gui_config"


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------
def test_rate_limit_view(client):
    db.set_chat_blocked_until(7, iso_ago(minutes=-30))
    db.set_chat_blocked_until(8, iso_ago(minutes=5))  # expired
    old = refused(7, "https://example.com/old")
    set_column(old, "created_at", iso_ago(days=2))
    refused(7, "https://example.com/a")
    latest = refused(7, "https://example.com/b")
    dup = refused(9, "https://example.com/d", status="duplicate")
    view = client.get("/api/rate_limits").json()
    assert [row["chat_id"] for row in view["limited"]] == [7]
    row = view["limited"][0]
    assert row["refused_24h"] == 2
    assert row["latest_refused"]["id"] == latest
    assert 1700 < row["remaining_seconds"] <= 1800
    assert ISO_OFFSET.match(row["blocked_until"])
    assert [item["id"] for item in view["recent_refused"]] == [
        dup,
        latest,
        latest - 1,
        old,
    ]
    assert view["limits"]["source"] == "gui_config"
    assert view["limits"]["rate_limits"] == [
        {"count": c, "seconds": s} for c, s in config.RATE_LIMITS
    ]


def _unlimit_fixture():
    a1 = refused(10, "https://example.com/a?x=1", normalized="https://example.com/a")
    a2 = refused(10, "https://example.com/a", normalized="https://example.com/a")
    b1 = refused(10, "https://example.com/b")
    make_done(10, "https://example.com/b", normalized_url="https://example.com/b")
    c1 = refused(10, "https://example.com/c")
    set_column(c1, "created_at", iso_ago(days=3))
    d1 = refused(10, "https://example.com/d", status="duplicate")
    e1 = refused(11, "https://example.com/e")
    db.set_chat_blocked_until(10, iso_ago(minutes=-10))
    db.set_chat_blocked_until(11, iso_ago(minutes=-10))
    return {"a1": a1, "a2": a2, "b1": b1, "c1": c1, "d1": d1, "e1": e1}


def test_unlimit_chat_requeues_only_the_latest_refusal_per_link(client):
    rows = _unlimit_fixture()
    response = client.post(
        "/api/rate_limits/unlimit", json={"chat_id": 10, "requeue": True}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "chat_id": 10, "requeued": [rows["a2"]]}
    assert db.get_request(rows["a2"])["status"] == "queued"
    for key in ("a1", "b1", "c1", "e1"):
        assert db.get_request(rows[key])["status"] == "rate_limited", key
    assert db.get_request(rows["d1"])["status"] == "duplicate"
    limit = db.get_chat_limit(10)
    assert limit["blocked_until"] is None
    assert limit["reset_at"] is not None
    assert db.get_chat_limit(11)["blocked_until"] is not None
    events = client.get("/api/events", params={"type": "chat_unlimited"}).json()
    assert events["items"][0]["chat_id"] == 10
    assert client.get("/api/rate_limits").json()["limited"][0]["chat_id"] == 11


def test_unlimit_chat_without_requeue_only_resets(client):
    rows = _unlimit_fixture()
    response = client.post(
        "/api/rate_limits/unlimit", json={"chat_id": 10, "requeue": "false"}
    )
    assert response.json()["requeued"] == []
    assert db.get_request(rows["a2"])["status"] == "rate_limited"
    assert db.get_chat_limit(10)["blocked_until"] is None
    for body in ({}, {"chat_id": "abc"}, {"chat_id": 1, "extra": True}):
        assert client.post("/api/rate_limits/unlimit", json=body).status_code == 422


def test_unlimit_all(client):
    rows = _unlimit_fixture()
    response = client.post("/api/rate_limits/unlimit_all", json={"requeue": True})
    body = response.json()
    assert body["chats"] >= 2
    assert sorted(body["requeued"]) == sorted([rows["a2"], rows["e1"]])
    assert client.get("/api/rate_limits").json()["limited"] == []
    plain = client.post("/api/rate_limits/unlimit_all")
    assert plain.json()["requeued"] == []


def test_unlimit_requeue_respects_the_configured_max_age(client, monkeypatch):
    request_id = refused(12, "https://example.com/z")
    set_column(request_id, "created_at", iso_ago(hours=2))
    monkeypatch.setattr(config, "GUI_REQUEUE_MAX_AGE_SECONDS", 3600, raising=False)
    short = TestClient(make_app(), headers=BEARER)
    assert (
        short.post(
            "/api/rate_limits/unlimit", json={"chat_id": 12, "requeue": True}
        ).json()["requeued"]
        == []
    )
    assert client.post(
        "/api/rate_limits/unlimit", json={"chat_id": 12, "requeue": True}
    ).json()["requeued"] == [request_id]


# ---------------------------------------------------------------------------
# History, stats, aggregates, events
# ---------------------------------------------------------------------------
def age(request_id, seconds):
    """Move a request ``seconds`` into the past (creation and processing)."""
    stamp = db.iso_from_ts(time.time() - seconds)
    set_column(request_id, "created_at", stamp)
    set_column(request_id, "processing_started_at", stamp)
    return request_id


def test_clear_history_keeps_active_requests(client):
    age(make_done(1, "https://example.com/1"), 60 * 86400)
    age(make_failed(1, "https://example.com/2"), 60 * 86400)
    refused(1, "https://example.com/5")
    queued = db.create_request(1, "https://example.com/3")
    running = db.create_request(1, "https://example.com/4")
    assert db.claim_request(running)
    response = client.post("/api/history/clear")
    assert response.status_code == 200
    deleted = response.json()["deleted"]
    assert deleted["requests"] == 3
    assert deleted["updates"] > 0
    assert {row["id"] for row in db.list_requests()} == {queued, running}
    log = client.get("/api/events").json()
    assert [item["type"] for item in log["items"]] == [
        "history_cleared",
        "history_pruned",
    ]
    cleared = log["items"][0]
    assert cleared["actor"] == "gui:bearer"
    assert cleared["requests"] == 3


def test_clear_history_keeps_what_rate_limits_and_duplicate_checks_read(client):
    chat, latest_url = 42, "https://example.com/latest"
    db.set_worker_status(
        {"pid": 1, "rate_limits": [[20, 30 * 86400]], "duplicate_window_seconds": 5400}
    )
    old = age(make_done(chat, "https://example.com/old"), 40 * 86400)
    for day in range(18):  # 18 + 1 failed + 1 latest = the 20 links of 30 days
        url = f"https://example.com/v/{day}"
        age(make_done(chat, url, normalized_url=url), (day + 1) * 86400)
    age(make_failed(chat, "https://example.com/failed"), 3 * 3600)
    age(make_done(chat, latest_url, normalized_url=latest_url), 600)
    refusal = refused(chat, "https://example.com/refused")
    limiter = ratelimit.RateLimiter(limits=[(20, 30 * 86400)])
    since = db.iso_from_ts(time.time() - 5400)
    assert limiter.check(chat).outcome == ratelimit.Outcome.RATE_LIMITED
    assert db.find_recent_duplicate(chat, latest_url, since) is not None

    response = client.post("/api/history/clear")
    assert response.status_code == 200
    body = response.json()
    assert body["kept_seconds"] == 30 * 86400
    assert body["deleted"]["requests"] == 2
    assert db.get_request(old) is None
    assert db.get_request(refusal) is None
    # quota and duplicate memory survive the clear
    assert limiter.check(chat).outcome == ratelimit.Outcome.RATE_LIMITED
    assert db.find_recent_duplicate(chat, latest_url, since) is not None
    gui_config = client.get("/api/config").json()["gui"]
    assert gui_config["history_keep_seconds"] == 30 * 86400


def test_clear_history_protection_follows_the_worker_limits(client):
    # The worker's heartbeat, not this process's config, says what is enforced.
    db.set_worker_status(
        {"pid": 1, "rate_limits": [[3, 3600]], "duplicate_window_seconds": 600}
    )
    stale = age(make_done(5, "https://example.com/a"), 2 * 3600)
    recent = age(make_done(5, "https://example.com/b"), 30 * 60)
    body = client.post("/api/history/clear").json()
    assert body["kept_seconds"] == 3600
    assert db.get_request(stale) is None
    assert db.get_request(recent) is not None
    assert gui_queries.history_keep_seconds({}) == 0
    limits = {"rate_limits": [{"count": 1, "seconds": 10}]}
    assert gui_queries.history_keep_seconds(limits) == 10
    assert (
        gui_queries.history_keep_seconds(limits | {"duplicate_window_seconds": 90})
        == 90
    )


def test_stats(client):
    make_done(1, "https://example.com/1", original=100, final=50, compressed=True)
    make_done(1, "https://example.com/2", original=200, final=200, compressed=False)
    failed = make_failed(2, "https://example.com/3")
    db.set_request_fields(failed, original_size=300)
    old_done = make_done(2, "https://example.com/4", original=None, final=None)
    set_column(old_done, "created_at", iso_ago(days=2))
    refused(3, "https://example.com/5")
    refused(3, "https://example.com/6", status="duplicate")
    db.create_request(4, "https://example.com/7")
    db.create_request(4, "https://example.com/8", not_before=iso_ago(hours=-1))
    running = db.create_request(4, "https://example.com/9")
    assert db.claim_request(running)
    for request_id in (1, 2, old_done):
        set_column(request_id, "processing_duration_seconds", 10.0)
    db.set_chat_blocked_until(3, iso_ago(minutes=-5))

    stats = client.get("/api/stats").json()
    assert stats["totals"] == {
        "total": 9,
        "done": 3,
        "failed": 1,
        "active": 3,
        "queued": 1,
        "deferred": 1,
        "running": 1,
        "rate_limited": 1,
        "duplicate": 1,
        "cancelled": 0,
        "aborted": 0,
    }
    assert stats["last_24h"] == {"total": 8, "done": 2, "failed": 1, "refused": 2}
    assert stats["bytes"] == {"downloaded": 600, "uploaded": 250}
    assert stats["compressed"] == 1
    assert stats["success_rate"] == pytest.approx(0.75)
    assert stats["averages"]["processing_seconds"] == pytest.approx(10.0)
    assert stats["averages"]["original_size"] == pytest.approx(150.0)
    assert stats["limited_chats"] == 1
    assert client.get("/stats").json()["totals"]["total"] == 9


def test_stats_on_an_empty_database(client):
    stats = client.get("/api/stats").json()
    assert stats["totals"]["total"] == 0
    assert stats["success_rate"] is None
    assert stats["averages"] == {
        "processing_seconds": None,
        "original_size": None,
        "final_size": None,
    }


def test_aggregates(client):
    now_id = make_done(1, "https://youtu.be/x", provider="Youtube")
    set_column(now_id, "processing_duration_seconds", 3.0)
    slow = make_done(1, "https://youtu.be/y", provider="Youtube")
    set_column(slow, "processing_duration_seconds", 400.0)
    earlier = make_failed(2, "https://vimeo.com/1", provider="Vimeo")
    set_column(earlier, "created_at", iso_ago(hours=3))
    ancient = db.create_request(3, "https://example.com/old")
    set_column(ancient, "created_at", iso_ago(days=3))

    data = client.get("/api/aggregates").json()
    assert data["status_counts"] == {"done": 2, "failed": 1, "queued": 1}
    assert data["top_chats"][0] == {"chat_id": 1, "count": 2, "chat_type": None}
    names = {item["provider"]: item["name"] for item in data["providers"]}
    assert names["Youtube"] == "YouTube"
    assert names[None] is None
    hist = data["duration_histogram"]
    assert len(hist["labels"]) == len(hist["counts"])
    assert hist["counts"][0] == 1 and hist["counts"][-1] == 1
    series = data["timeseries"]
    points = series["points"]
    assert len(points) == 24
    assert series["bucket_seconds"] == 3600
    assert all(ISO_OFFSET.match(point["t"]) for point in points)
    created_hour = db.get_request(now_id)["created_at"][:13]
    bucket = next(p for p in points if p["t"][:13] == created_hour)
    assert bucket["total"] == 2 and bucket["done"] == 2
    three_hours = [p for p in points if p["failed"]]
    assert len(three_hours) == 1 and three_hours[0]["total"] == 1
    assert sum(point["total"] for point in points) == 3
    week = client.get("/api/aggregates", params={"hours": 168}).json()
    assert len(week["timeseries"]["points"]) == 168
    assert sum(point["total"] for point in week["timeseries"]["points"]) == 4
    for hours in (0, 169):
        assert client.get("/api/aggregates", params={"hours": hours}).status_code == 422


def test_event_log(client):
    first = db.create_request(1, "https://example.com/1")
    db.create_request(2, "https://example.com/2")
    with db.write() as conn:  # rows written by the old code are not JSON events
        conn.executemany(
            "INSERT INTO updates (raw, created_at) VALUES (?, ?)",
            [
                ("not json at all", db.now_iso()),
                ('{"update_id": 5, "message": {"text": "hi"}}', db.now_iso()),
            ],
        )
    db.add_event("custom_event", chat_id=1, note="x" * 5000)
    all_events = client.get("/api/events").json()
    assert all_events["total"] == 5
    items = all_events["items"]
    assert [item["id"] for item in items] == sorted(
        (item["id"] for item in items), reverse=True
    )
    assert items[0]["type"] == "custom_event"
    assert len(items[0]["note"]) <= gui_queries.MAX_RAW
    assert all(ISO_OFFSET.match(item["created_at"]) for item in items)

    def total(**params):
        return client.get("/api/events", params=params).json()["total"]

    assert total(type="request_created") == 2
    assert total(type="update_created") == 2
    assert total(chat_id=1) == 2
    assert total(request_id=first) == 1
    assert total(type="request_created", chat_id=2) == 1
    paged = client.get("/api/events", params={"limit": 2, "offset": 4}).json()
    assert len(paged["items"]) == 1
    legacy = client.get("/api/events", params={"type": "update_created"}).json()
    assert {item["raw"] for item in legacy["items"]} == {
        "not json at all",
        '{"update_id": 5, "message": {"text": "hi"}}',
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def test_users_crud(client):
    created = client.post(
        "/api/users", json={"email": " Admin@Example.com ", "role": "ADMIN"}
    )
    assert created.status_code == 201
    user = created.json()
    assert user["email"] == "Admin@Example.com"
    assert user["role"] == "admin"
    assert ISO_OFFSET.match(user["created_at"])
    duplicate = client.post("/api/users", json={"email": "admin@example.com"})
    assert duplicate.status_code == 409
    for body, status in (
        ({"email": "nope"}, 400),
        ({"email": "a@b.c", "role": "superuser"}, 400),
        ({}, 422),
        ({"email": "a@b.c", "extra": 1}, 422),
    ):
        assert client.post("/api/users", json=body).status_code == status, body
    other = client.post(
        "/api/users", json={"email": "user@example.com", "username": "u"}
    ).json()
    listing = client.get("/api/users").json()
    assert listing["total"] == 2
    assert listing["admins"] == 1
    assert {item["email"] for item in listing["items"]} == {
        "Admin@Example.com",
        "user@example.com",
    }
    promoted = client.put(f"/api/users/{other['id']}/role", json={"role": "admin"})
    assert promoted.json()["role"] == "admin"
    assert (
        client.put(f"/api/users/{other['id']}/role", json={"role": "x"}).status_code
        == 400
    )
    assert client.put("/api/users/999/role", json={"role": "user"}).status_code == 404
    assert client.delete("/api/users/999").status_code == 404
    assert client.delete(f"/api/users/{other['id']}").json() == {
        "ok": True,
        "id": other["id"],
    }
    # with ADMIN_TOKEN configured the last DB admin may be removed
    assert client.delete(f"/api/users/{user['id']}").status_code == 200
    assert client.get("/api/users").json()["total"] == 0


def test_the_last_admin_cannot_be_removed_without_another_way_in():
    db.add_user(email="boss@example.com", role="admin")

    class Provider:
        async def authorize_redirect(self, request, redirect_uri):
            return RedirectResponse("/auth")

        async def authorize_access_token(self, request):
            return {
                "access_token": "t",
                "userinfo": {"sub": "b", "email": "boss@example.com"},
            }

    app = gui.create_app(
        AuthSettings(secret_key="k" * 40),
        oauth_registry=SimpleNamespace(provider=Provider()),
    )
    client = TestClient(app, headers=SAME)
    client.get("/login")
    users = client.get("/api/users").json()["items"]
    boss = users[0]["id"]
    demote = client.put(f"/api/users/{boss}/role", json={"role": "user"})
    assert demote.status_code == 409
    assert "ultimo amministratore" in demote.json()["detail"]
    assert client.delete(f"/api/users/{boss}").status_code == 409
    second = client.post(
        "/api/users", json={"email": "second@example.com", "role": "admin"}
    )
    assert second.status_code == 201
    assert (
        client.put(f"/api/users/{boss}/role", json={"role": "user"}).status_code == 200
    )
    # the demoted user loses access on the very next request
    assert client.get("/api/users").status_code == 403


# ---------------------------------------------------------------------------
# Config, health, errors, security headers
# ---------------------------------------------------------------------------
def test_config_endpoints(client):
    anonymous = TestClient(make_app())
    assert anonymous.get("/config").json() == {
        "oauth_configured": False,
        "admin_token_set": True,
    }
    assert anonymous.get("/api/config").status_code == 401
    cfg = client.get("/api/config").json()
    assert cfg["auth"]["token"] is True
    assert cfg["auth"]["oauth"] is False
    assert cfg["auth"]["session_cookie_secure"] is False
    assert cfg["limits"]["source"] == "gui_config"
    assert cfg["settings"]["max_pending_per_chat"] == config.MAX_PENDING_PER_CHAT
    assert isinstance(cfg["warnings"], list)
    assert cfg["version"]


def test_health(client, monkeypatch):
    assert client.get("/health").json() == {"ok": True, "db": "ok", "worker": "unknown"}
    db.set_worker_status({"pid": 1})
    assert client.get("/health").json()["worker"] == "online"
    monkeypatch.setattr(config, "HEALTH_DEBUG", True)
    debug = client.get("/health").json()
    assert {"ws_clients", "last_event_id", "tailer_running", "version"} <= set(debug)

    def broken():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(gui_queries, "ping", broken)
    response = TestClient(make_app()).get("/health")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "db": "error"}


def test_database_errors_are_503_json(client, monkeypatch):
    def locked(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gui_queries, "requests_page", locked)
    response = client.get("/api/requests")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database occupato: riprova tra poco"}
    assert response.headers["retry-after"] == "2"


def test_db_failure_during_the_admin_check_is_503_not_forbidden(monkeypatch):
    class Provider:
        async def authorize_redirect(self, request, redirect_uri):
            return RedirectResponse("/auth")

        async def authorize_access_token(self, request):
            return {
                "access_token": "t",
                "userinfo": {"sub": "b", "email": "b@example.com"},
            }

    app = gui.create_app(
        AuthSettings(secret_key="k" * 40),
        oauth_registry=SimpleNamespace(provider=Provider()),
    )
    client = TestClient(app)
    client.get("/login")

    def broken(email):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "get_user_by_email", broken)
    response = client.get("/api/requests")
    assert response.status_code == 503
    assert response.json() == {"detail": "Errore del database"}


def test_security_headers(client):
    for path in ("/", "/api/me", "/login", "/health"):
        response = client.get(path)
        headers = response.headers
        assert headers["content-security-policy"] == gui.CONTENT_SECURITY_POLICY, path
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert headers["cache-control"] == "no-store"
    static = client.get("/static/app.js")
    assert static.status_code == 200
    assert "cache-control" not in static.headers
    assert static.headers["content-security-policy"] == gui.CONTENT_SECURITY_POLICY
    csp = gui.CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # no blanket scheme/host sources: only the two pinned CDN files
    assert re.search(r"\shttps:(;|\s|$)", csp) is None
    assert re.search(r"https://cdn\.jsdelivr\.net/?(;|\s|$)", csp) is None
    assert gui.CHART_JS_URL in csp and gui.BOOTSTRAP_CSS_URL in csp


def test_html_is_csp_friendly_and_cdn_assets_are_pinned(client):
    index = client.get("/").text
    assert gui.ASSET_VERSION_PLACEHOLDER not in index
    assert f"app.js?v={client.app.state.gui.asset_version}" in index
    assert f'integrity="{gui.CHART_JS_SRI}"' in index
    assert f'integrity="{gui.BOOTSTRAP_CSS_SRI}"' in index
    assert index.count('crossorigin="anonymous"') == 2
    pages = [index, client.get("/login").text]
    pages += [p.read_text() for p in Path(gui.TEMPLATES_DIR).glob("*.html")]
    for page in pages:
        assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page) is None  # no inline JS
        assert re.search(r"\sstyle=", page) is None
        assert re.search(r"<style", page) is None
        assert re.search(r"\son[a-z]+=", page) is None
    script = (Path(gui.STATIC_DIR) / "app.js").read_text()
    assert ".innerHTML" not in script
    assert "eval(" not in script


def test_docs_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


ADMIN_ROUTES = [
    ("get", "/api/requests", None),
    ("get", "/requests", None),
    ("get", "/api/requests/1", None),
    ("post", "/api/requests/requeue", {"ids": [1]}),
    ("get", "/api/queue", None),
    ("post", "/api/queue/cancel", {}),
    ("get", "/api/worker", None),
    ("get", "/api/rate_limits", None),
    ("post", "/api/rate_limits/unlimit", {"chat_id": 1}),
    ("post", "/api/rate_limits/unlimit_all", {}),
    ("get", "/api/stats", None),
    ("get", "/stats", None),
    ("get", "/api/aggregates", None),
    ("get", "/api/events", None),
    ("get", "/api/config", None),
    ("post", "/api/history/clear", None),
    ("get", "/api/users", None),
    ("post", "/api/users", {"email": "a@b.c"}),
    ("put", "/api/users/1/role", {"role": "user"}),
    ("delete", "/api/users/1", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ROUTES)
def test_admin_routes_require_an_administrator(method, path, body):
    db.create_request(1, "https://example.com/keep")

    class Provider:
        async def authorize_redirect(self, request, redirect_uri):
            return RedirectResponse("/auth")

        async def authorize_access_token(self, request):
            return {"access_token": "t", "userinfo": {"sub": "nobody"}}

    app = gui.create_app(
        AuthSettings(secret_key="k" * 40, admin_token=TOKEN),
        oauth_registry=SimpleNamespace(provider=Provider()),
    )
    anonymous = TestClient(app, headers=SAME)
    kwargs = {"json": body} if body is not None else {}
    response = getattr(anonymous, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json() == {"detail": "Autenticazione richiesta"}
    user = TestClient(app, headers=SAME)
    user.get("/login")
    forbidden = getattr(user, method)(path, **kwargs)
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Privilegi di amministratore richiesti"}
    assert db.count_requests() == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/queue/start"),
        ("post", "/api/clear_queue"),
        ("post", "/api/unlimit_chat"),
        ("post", "/api/unlimit_all"),
        ("post", "/api/db/clear"),
        ("get", "/api/updates"),
        ("post", "/api/session/grant_admin"),
        ("post", "/api/session/revoke_admin"),
    ],
)
def test_features_that_cannot_work_cross_process_are_gone(client, method, path):
    assert getattr(client, method)(path).status_code == 404


def test_gui_modules_never_touch_the_worker():
    for module in ("gui.py", "gui_auth.py", "gui_queries.py", "ws_broadcast.py"):
        source = (Path(gui.SRC_DIR) / module).read_text()
        assert "active_worker" not in source, module
        assert "import worker" not in source and "from .worker" not in source, module
        assert "src.worker" not in source, module
