"""Read model of the admin GUI: read-only queries and JSON shaping.

Everything the GUI displays comes from the shared SQLite database: the public
:mod:`src.db` functions plus the read-only SQL below (always through
``with db.connect() as conn``). Nothing here writes. All functions are
synchronous; the GUI calls them from FastAPI's threadpool.

Every timestamp returned is ISO 8601 with an explicit UTC offset (see
:func:`iso`), so browsers parse it correctly in any time zone.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from . import config, db

logger = logging.getLogger(__name__)

REFUSED_STATUSES: tuple[str, ...] = (db.Status.RATE_LIMITED, db.Status.DUPLICATE)
RETRYABLE_STATUSES: tuple[str, ...] = (db.Status.FAILED, db.Status.RATE_LIMITED)
ONLINE_FACTOR = 3.0  # heartbeat older than ~3 intervals = worker offline
MIN_STALE_SECONDS = 10.0
MAX_TEXT = 300
MAX_RAW = 2000
_DURATION_BUCKETS: tuple[tuple[float | None, str], ...] = (
    (5, "< 5 s"),
    (15, "5-15 s"),
    (30, "15-30 s"),
    (60, "30-60 s"),
    (120, "1-2 min"),
    (300, "2-5 min"),
    (None, "≥ 5 min"),
)


# ---------------------------------------------------------------------------
# Shaping helpers
# ---------------------------------------------------------------------------
def iso(value: Any) -> str | None:
    """Canonical UTC ISO 8601 string (``+00:00``) for a timestamp value.

    Accepts ISO strings (naive = UTC, ``Z`` or any offset) and epoch seconds.
    Unparseable strings are returned unchanged; anything else gives None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        try:
            return db.iso_from_ts(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _iso_from_dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _int(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


@lru_cache(maxsize=512)
def provider_name(key: str | None) -> str | None:
    """Display name of a provider key ("Youtube" -> "YouTube")."""
    if not key:
        return None
    try:
        from . import providers

        return providers.provider_display_name(key) or key
    except Exception:  # display only: never break a page for a name
        logger.warning("Cannot resolve the display name of %r", key, exc_info=True)
        return key


def request_view(
    row: Mapping[str, Any], *, now_iso: str | None = None, detail: bool = False
) -> dict[str, Any]:
    """JSON shape of one ``requests`` row."""
    status = row.get("status")
    not_before = iso(row.get("not_before"))
    compressed = row.get("compressed")
    limit = 20000 if detail else MAX_TEXT
    return {
        "id": row.get("id"),
        "chat_id": row.get("chat_id"),
        "chat_type": row.get("chat_type"),
        "user_id": row.get("user_id"),
        "url": row.get("url"),
        "normalized_url": row.get("normalized_url"),
        "provider": row.get("provider"),
        "provider_name": provider_name(row.get("provider")),
        "status": status,
        "deferred": bool(
            status == db.Status.QUEUED
            and not_before
            and now_iso is not None
            and not_before > now_iso
        ),
        "created_at": iso(row.get("created_at")),
        "updated_at": iso(row.get("updated_at")),
        "not_before": not_before,
        "attempts": row.get("attempts"),
        "error_code": row.get("error_code"),
        "error_message": _clip(row.get("error_message"), limit),
        "original_message_id": row.get("original_message_id"),
        "sent_message_id": row.get("sent_message_id"),
        "media_type": row.get("media_type"),
        "original_size": row.get("original_size"),
        "final_size": row.get("final_size"),
        "compressed": None if compressed is None else bool(compressed),
        "processing_started_at": iso(row.get("processing_started_at")),
        "processing_finished_at": iso(row.get("processing_finished_at")),
        "processing_duration_seconds": row.get("processing_duration_seconds"),
        "description": _clip(row.get("description"), limit),
        "requeueable": status in db.REQUEUEABLE_STATUSES,
        "cancellable": status == db.Status.QUEUED,
    }


def request_event_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """JSON shape of one ``request_events`` row."""
    return {
        "id": row.get("id"),
        "type": row.get("event_type"),
        "details": row.get("details"),
        "duration_seconds": row.get("duration_seconds"),
        "created_at": iso(row.get("created_at")),
    }


def user_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "username": row.get("username"),
        "email": row.get("email"),
        "role": row.get("role"),
        "created_at": iso(row.get("created_at")),
    }


def session_view(
    row: Mapping[str, Any], *, current_id: int | None = None
) -> dict[str, Any]:
    """An admin GUI session (``db.list_gui_sessions`` row) for the browser."""
    who = row.get("email") or row.get("username") or row.get("subject")
    if row.get("auth") == "token":
        who = "Token amministratore"
    return {
        "id": row.get("id"),
        "auth": row.get("auth"),
        "who": who,
        "name": row.get("name"),
        "groups": list(row.get("groups") or ()),
        "client": row.get("client"),
        "user_agent": row.get("user_agent"),
        "created_at": iso(row.get("created_at")),
        "last_seen_at": iso(row.get("last_seen_at")),
        "expires_at": iso(row.get("expires_at")),
        "current": current_id is not None and row.get("id") == current_id,
    }


def event_view(event: Mapping[str, Any]) -> dict[str, Any]:
    """An event-log item (as from ``db.list_events_after``) for the browser."""
    view = {key: _clip(value, MAX_RAW) for key, value in event.items()}
    view["created_at"] = iso(event.get("created_at"))
    return view


def decode_event_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """``updates`` row -> ``{"id", "created_at", "type", ...payload}``.

    Same shape as ``db.list_events_after``: ``id``/``created_at`` are the
    row's own; rows that are not JSON events are ``update_created`` + raw.
    """
    raw = row["raw"]
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        event: dict[str, Any] = {
            "id": row["id"],
            "created_at": row["created_at"],
            "type": payload["type"],
        }
        event.update(
            (key, value)
            for key, value in payload.items()
            if key not in ("id", "created_at", "type")
        )
    else:
        event = {
            "id": row["id"],
            "created_at": row["created_at"],
            "type": db.EventType.UPDATE_CREATED.value,
            "raw": raw,
        }
    return event_view(event)


# ---------------------------------------------------------------------------
# Worker heartbeat
# ---------------------------------------------------------------------------
def heartbeat_interval(status: Mapping[str, Any] | None) -> float:
    reported = _number(status.get("heartbeat_seconds")) if status else None
    if reported and reported > 0:
        return reported
    return float(getattr(config, "WORKER_HEARTBEAT_SECONDS", 5.0))


def _rate_limits(value: Any) -> list[dict[str, int]] | None:
    if not isinstance(value, list | tuple):
        return None
    limits = []
    for item in value:
        if isinstance(item, Mapping):
            count = _int(item.get("count"))
            seconds = _int(item.get("seconds", item.get("window")))
        elif isinstance(item, list | tuple) and len(item) == 2:
            count, seconds = _int(item[0]), _int(item[1])
        else:
            continue
        if count and seconds:
            limits.append({"count": count, "seconds": seconds})
    return limits


def _running_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    return {
        "request_id": _int(item.get("request_id")),
        "chat_id": _int(item.get("chat_id")),
        "url": item.get("url") if isinstance(item.get("url"), str) else None,
        "stage": item.get("stage") if isinstance(item.get("stage"), str) else None,
        "started_at": iso(item.get("started_at")),
    }


def worker_summary(status: Mapping[str, Any] | None) -> dict[str, Any]:
    """Worker liveness from ``db.get_worker_status()``.

    ``state`` is ``unknown`` (never seen: is the DB shared with the bot?),
    ``online``, ``offline`` (heartbeat older than ~3 intervals) or
    ``stopped`` (the worker said so).
    """
    stale_after = max(ONLINE_FACTOR * heartbeat_interval(status), MIN_STALE_SECONDS)
    if status is None:
        return {
            "state": "unknown",
            "online": False,
            "heartbeat_at": None,
            "age_seconds": None,
            "stale_after_seconds": stale_after,
            "running": [],
            "limits": None,
        }
    age = _number(status.get("age_seconds"))
    reported = str(status.get("state") or "").lower()
    online = age is not None and age <= stale_after and reported not in ("stopped",)
    state = "online" if online else ("stopped" if reported == "stopped" else "offline")
    running = [
        view
        for view in map(_running_item, status.get("running") or ())
        if view is not None
    ]
    limits = None
    rate_limits = _rate_limits(status.get("rate_limits"))
    if rate_limits is not None:
        limits = {
            "rate_limits": rate_limits,
            "max_pending_per_chat": _int(status.get("max_pending_per_chat")),
            "duplicate_window_seconds": _int(status.get("duplicate_window_seconds")),
            "max_defer_seconds": _int(status.get("rate_limit_max_defer_seconds")),
        }
    return {
        "state": state,
        "online": online,
        "heartbeat_at": iso(status.get("heartbeat_at")),
        "age_seconds": None if age is None else round(age, 1),
        "stale_after_seconds": stale_after,
        "pid": _int(status.get("pid")),
        "version": _clip(status.get("version"), 64),
        "started_at": iso(status.get("started_at")),
        "mode": _clip(status.get("mode"), 32),
        "workers": _int(status.get("workers")),
        "queued": _int(status.get("queued")),
        "deferred": _int(status.get("deferred")),
        "running": running,
        "limits": limits,
    }


def effective_limits(worker: Mapping[str, Any]) -> dict[str, Any]:
    """Limits reported by the worker (last heartbeat), else this process's config."""
    if worker.get("limits"):
        limits = dict(worker["limits"])
        limits["source"] = "worker" if worker.get("online") else "worker_last_seen"
        return limits
    return {
        "rate_limits": [
            {"count": count, "seconds": seconds}
            for count, seconds in getattr(config, "RATE_LIMITS", ())
        ],
        "max_pending_per_chat": getattr(config, "MAX_PENDING_PER_CHAT", None),
        "duplicate_window_seconds": getattr(config, "DUPLICATE_WINDOW_SECONDS", None),
        "max_defer_seconds": getattr(config, "RATE_LIMIT_MAX_DEFER_SECONDS", None),
        "source": "gui_config",
    }


def history_keep_seconds(limits: Mapping[str, Any]) -> float:
    """How long accepted requests must survive a history clear.

    The largest rate-limit window or the duplicate window of ``limits`` (see
    :func:`effective_limits`), whichever is longer: the rate limiter
    (``db.accepted_times``) and the duplicate check
    (``db.find_recent_duplicate``) read those rows.
    """
    windows = [
        _number(item.get("seconds")) or 0.0
        for item in limits.get("rate_limits") or ()
        if isinstance(item, Mapping)
    ]
    windows.append(_number(limits.get("duplicate_window_seconds")) or 0.0)
    return max(0.0, *windows)


def load_worker() -> dict[str, Any]:
    return worker_summary(db.get_worker_status())


# ---------------------------------------------------------------------------
# Read-only SQL
# ---------------------------------------------------------------------------
def _marks(count: int) -> str:
    return ", ".join("?" * count)


def ping() -> None:
    """Raise ``sqlite3.Error`` when the database cannot be read."""
    with db.connect() as conn:
        conn.execute("SELECT 1 FROM requests LIMIT 1").fetchall()


def request_stats(now: datetime | None = None) -> dict[str, Any]:
    """Dashboard totals in one pass over ``requests``."""
    current = now or datetime.now(UTC)
    now_iso = _iso_from_dt(current)
    since = _iso_from_dt(current - timedelta(hours=24))
    refused = _marks(len(REFUSED_STATUSES))
    active = _marks(len(db.ACTIVE_STATUSES))
    sql = f"""SELECT
        COUNT(*) AS total,
        COALESCE(SUM(status = ?), 0) AS done,
        COALESCE(SUM(status = ?), 0) AS failed,
        COALESCE(SUM(status IN ({active})), 0) AS active,
        COALESCE(SUM(status = ? AND (not_before IS NULL OR not_before <= ?)), 0)
            AS queued,
        COALESCE(SUM(status = ? AND not_before > ?), 0) AS deferred,
        COALESCE(SUM(status IN (?, ?)), 0) AS running,
        COALESCE(SUM(status = ?), 0) AS rate_limited,
        COALESCE(SUM(status = ?), 0) AS duplicate,
        COALESCE(SUM(status = ?), 0) AS cancelled,
        COALESCE(SUM(status = ?), 0) AS aborted,
        COALESCE(SUM(created_at >= ?), 0) AS day_total,
        COALESCE(SUM(created_at >= ? AND status = ?), 0) AS day_done,
        COALESCE(SUM(created_at >= ? AND status = ?), 0) AS day_failed,
        COALESCE(SUM(created_at >= ? AND status IN ({refused})), 0) AS day_refused,
        COALESCE(SUM(original_size), 0) AS downloaded_bytes,
        COALESCE(SUM(CASE WHEN status = ? THEN final_size END), 0) AS uploaded_bytes,
        COALESCE(SUM(status = ? AND compressed = 1), 0) AS compressed,
        AVG(CASE WHEN status = ? THEN processing_duration_seconds END) AS avg_seconds,
        AVG(CASE WHEN status = ? THEN original_size END) AS avg_original,
        AVG(CASE WHEN status = ? THEN final_size END) AS avg_final
        FROM requests"""
    S = db.Status
    params = (
        S.DONE,
        S.FAILED,
        *db.ACTIVE_STATUSES,
        S.QUEUED,
        now_iso,
        S.QUEUED,
        now_iso,
        S.PROCESSING,
        S.SENDING,
        S.RATE_LIMITED,
        S.DUPLICATE,
        S.CANCELLED,
        S.ABORTED,
        since,
        since,
        S.DONE,
        since,
        S.FAILED,
        since,
        *REFUSED_STATUSES,
        S.DONE,
        S.DONE,
        S.DONE,
        S.DONE,
        S.DONE,
    )
    with db.connect() as conn:
        row = conn.execute(sql, params).fetchone()
        limited = conn.execute(
            "SELECT COUNT(*) FROM chat_limits WHERE blocked_until > ?", (now_iso,)
        ).fetchone()[0]
    finished = row["done"] + row["failed"]
    return {
        "generated_at": now_iso,
        "totals": {
            key: int(row[key])
            for key in (
                "total",
                "done",
                "failed",
                "active",
                "queued",
                "deferred",
                "running",
                "rate_limited",
                "duplicate",
                "cancelled",
                "aborted",
            )
        },
        "last_24h": {
            "total": int(row["day_total"]),
            "done": int(row["day_done"]),
            "failed": int(row["day_failed"]),
            "refused": int(row["day_refused"]),
        },
        "bytes": {
            "downloaded": int(row["downloaded_bytes"]),
            "uploaded": int(row["uploaded_bytes"]),
        },
        "averages": {
            "processing_seconds": _number(row["avg_seconds"]),
            "original_size": _number(row["avg_original"]),
            "final_size": _number(row["avg_final"]),
        },
        "compressed": int(row["compressed"]),
        "success_rate": (row["done"] / finished) if finished else None,
        "limited_chats": int(limited),
    }


def status_counts() -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM requests GROUP BY status"
            " ORDER BY n DESC, status"
        ).fetchall()
    return {(row["status"] or "unknown"): int(row["n"]) for row in rows}


def top_chats(limit: int = 10) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT chat_id, COUNT(*) AS n, MAX(chat_type) AS chat_type
               FROM requests WHERE chat_id IS NOT NULL
               GROUP BY chat_id ORDER BY n DESC, chat_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "chat_id": row["chat_id"],
            "count": int(row["n"]),
            "chat_type": row["chat_type"],
        }
        for row in rows
    ]


def provider_counts(limit: int = 10) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT provider, COUNT(*) AS n FROM requests
               GROUP BY provider ORDER BY n DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "provider": row["provider"],
            "name": provider_name(row["provider"]),
            "count": int(row["n"]),
        }
        for row in rows
    ]


def duration_histogram() -> dict[str, list[Any]]:
    """Processing time of completed requests, in fixed buckets."""
    cases = " ".join(
        f"WHEN d < {float(bound)} THEN {index}"
        for index, (bound, _label) in enumerate(_DURATION_BUCKETS)
        if bound is not None
    )
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT CASE {cases} ELSE {len(_DURATION_BUCKETS) - 1} END AS bucket,
                       COUNT(*) AS n
                FROM (SELECT processing_duration_seconds AS d FROM requests
                      WHERE status = ? AND processing_duration_seconds IS NOT NULL)
                GROUP BY bucket""",
            (db.Status.DONE,),
        ).fetchall()
    counts = [0] * len(_DURATION_BUCKETS)
    for row in rows:
        counts[int(row["bucket"])] = int(row["n"])
    return {"labels": [label for _bound, label in _DURATION_BUCKETS], "counts": counts}


def hourly_series(hours: int = 24, *, now: datetime | None = None) -> dict[str, Any]:
    """Requests per hour (UTC buckets) with their outcome, oldest first."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    end = current.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours - 1)
    refused = _marks(len(REFUSED_STATUSES))
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT substr(created_at, 1, 13) AS bucket, COUNT(*) AS total,
                       COALESCE(SUM(status = ?), 0) AS done,
                       COALESCE(SUM(status = ?), 0) AS failed,
                       COALESCE(SUM(status IN ({refused})), 0) AS refused
                FROM requests WHERE created_at >= ? GROUP BY bucket""",
            (db.Status.DONE, db.Status.FAILED, *REFUSED_STATUSES, _iso_from_dt(start)),
        ).fetchall()
    by_bucket = {row["bucket"]: row for row in rows}
    points = []
    for offset in range(hours):
        moment = start + timedelta(hours=offset)
        row = by_bucket.get(moment.strftime("%Y-%m-%dT%H"))
        points.append(
            {
                "t": _iso_from_dt(moment),
                "total": int(row["total"]) if row else 0,
                "done": int(row["done"]) if row else 0,
                "failed": int(row["failed"]) if row else 0,
                "refused": int(row["refused"]) if row else 0,
            }
        )
    return {"bucket_seconds": 3600, "start": _iso_from_dt(start), "points": points}


def recent_refused(limit: int = 50) -> list[dict[str, Any]]:
    now_iso = db.now_iso()
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM requests WHERE status IN ({_marks(len(REFUSED_STATUSES))})
                ORDER BY id DESC LIMIT ?""",
            (*REFUSED_STATUSES, limit),
        ).fetchall()
    return [request_view(dict(row), now_iso=now_iso) for row in rows]


def refused_summary(
    chat_ids: Sequence[int], since_iso: str
) -> dict[int, dict[str, Any]]:
    """Per chat: rate-limited refusals since ``since_iso`` and the latest one."""
    ids = list(dict.fromkeys(chat_ids))[:500]
    if not ids:
        return {}
    with db.connect() as conn:
        counts = conn.execute(
            f"""SELECT chat_id, COUNT(*) AS n, MAX(id) AS latest_id FROM requests
                WHERE status = ? AND created_at >= ? AND chat_id IN ({_marks(len(ids))})
                GROUP BY chat_id""",
            (db.Status.RATE_LIMITED, since_iso, *ids),
        ).fetchall()
        latest_ids = [row["latest_id"] for row in counts]
        latest = {}
        if latest_ids:
            for row in conn.execute(
                f"""SELECT id, url, created_at, error_code FROM requests
                    WHERE id IN ({_marks(len(latest_ids))})""",
                latest_ids,
            ):
                latest[row["id"]] = {
                    "id": row["id"],
                    "url": row["url"],
                    "created_at": iso(row["created_at"]),
                    "error_code": row["error_code"],
                }
    return {
        row["chat_id"]: {
            "refused": int(row["n"]),
            "latest": latest.get(row["latest_id"]),
        }
        for row in counts
    }


def latest_refused_per_link(
    *, since_iso: str, chat_id: int | None = None, limit: int = 500
) -> list[int]:
    """Ids of rate-limited requests that are still the latest row for their link.

    Per (chat, link) only the newest row counts, and only when it is a
    ``rate_limited`` refusal created since ``since_iso``: a link the user sent
    again later (whatever happened to it) is never requeued.
    """
    sql = """SELECT r.id FROM requests r
             WHERE r.status = ? AND r.created_at >= ?"""
    params: list[Any] = [db.Status.RATE_LIMITED, since_iso]
    if chat_id is not None:
        sql += " AND r.chat_id = ?"
        params.append(chat_id)
    sql += """ AND NOT EXISTS (
                 SELECT 1 FROM requests n
                 WHERE n.chat_id = r.chat_id AND n.id > r.id
                   AND COALESCE(n.normalized_url, n.url) = COALESCE(r.normalized_url, r.url))
             ORDER BY r.id LIMIT ?"""
    params.append(limit)
    with db.connect() as conn:
        return [int(row[0]) for row in conn.execute(sql, params)]


_EVENT_TYPE_SQL = "CASE WHEN json_valid(raw) THEN json_extract(raw, '$.type') END"


def _event_filters(
    event_type: str | None, chat_id: int | None, request_id: int | None
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if event_type == db.EventType.UPDATE_CREATED:
        clauses.append(
            "COALESCE(CASE WHEN json_valid(raw) THEN json_type(raw, '$.type') END, '')"
            " != 'text'"
        )
    elif event_type:
        clauses.append(f"{_EVENT_TYPE_SQL} = ?")
        params.append(event_type)
    for key, value in (("chat_id", chat_id), ("request_id", request_id)):
        if value is not None:
            clauses.append(
                f"CASE WHEN json_valid(raw) THEN json_extract(raw, '$.{key}') END = ?"
            )
            params.append(value)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def list_events(
    *,
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
    chat_id: int | None = None,
    request_id: int | None = None,
) -> dict[str, Any]:
    """The event log, newest first, with optional filters."""
    where, params = _event_filters(event_type, chat_id, request_id)
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM updates{where}", params).fetchone()[
            0
        ]
        rows = conn.execute(
            f"SELECT id, raw, created_at FROM updates{where}"
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "items": [decode_event_row(row) for row in rows],
    }


def count_users() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def count_admins() -> int:
    with db.connect() as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[
                0
            ]
        )


def get_user(user_id: int) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, username, email, role, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return None if row is None else user_view(dict(row))


# ---------------------------------------------------------------------------
# Composite views
# ---------------------------------------------------------------------------
def requests_page(
    *,
    limit: int,
    offset: int,
    status: str | Sequence[str] | None = None,
    q: str | None = None,
    chat_id: int | None = None,
) -> dict[str, Any]:
    total = db.count_requests(status=status, chat_id=chat_id, q=q)
    rows = db.list_requests(limit, offset, status=status, chat_id=chat_id, q=q)
    now_iso = db.now_iso()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [request_view(row, now_iso=now_iso) for row in rows],
    }


def request_detail(
    request_id: int, *, events_limit: int = 200
) -> dict[str, Any] | None:
    row = db.get_request(request_id)
    if row is None:
        return None
    view = request_view(row, now_iso=db.now_iso(), detail=True)
    view["events"] = [
        request_event_view(event)
        for event in db.get_request_events(request_id, limit=events_limit)
    ]
    return view


def _views(rows: Iterable[Mapping[str, Any]], now_iso: str) -> list[dict[str, Any]]:
    return [request_view(row, now_iso=now_iso) for row in rows]


def queue_view(limit: int = 50) -> dict[str, Any]:
    """Queued / deferred / running requests plus the worker's own view."""
    snapshot = db.queue_snapshot(limit)
    worker = load_worker()
    now_iso = snapshot["generated_at"]
    stages = {
        item["request_id"]: item for item in worker["running"] if item["request_id"]
    }
    running = []
    for row in snapshot["running"]:
        view = request_view(row, now_iso=now_iso)
        info = stages.get(row["id"])
        view["stage"] = info["stage"] if info else None
        view["stale"] = not worker["online"]
        running.append(view)
    retryable = db.list_requests(20, 0, status=list(RETRYABLE_STATUSES))
    return {
        "generated_at": iso(now_iso),
        "counts": snapshot["counts"],
        "queued": _views(snapshot["queued"], now_iso),
        "deferred": _views(snapshot["deferred"], now_iso),
        "running": running,
        "retryable": _views(retryable, now_iso),
        "worker": worker,
    }


def rate_limit_view(*, recent_limit: int = 50) -> dict[str, Any]:
    """Currently limited chats (``chat_limits.blocked_until`` in the future)."""
    now = datetime.now(UTC)
    now_iso = _iso_from_dt(now)
    worker = load_worker()
    limited = db.list_chat_limits(only_blocked=True, now_iso=now_iso)
    summary = refused_summary(
        [row["chat_id"] for row in limited], _iso_from_dt(now - timedelta(hours=24))
    )
    items = []
    for row in limited[:500]:
        info = summary.get(row["chat_id"], {})
        items.append(
            {
                "chat_id": row["chat_id"],
                "blocked_until": iso(row.get("blocked_until")),
                "remaining_seconds": round(float(row.get("remaining_seconds") or 0), 1),
                "reset_at": iso(row.get("reset_at")),
                "last_rate_notice_at": iso(row.get("last_rate_notice_at")),
                "refused_24h": info.get("refused", 0),
                "latest_refused": info.get("latest"),
            }
        )
    return {
        "generated_at": now_iso,
        "limits": effective_limits(worker),
        "limited": items,
        "recent_refused": recent_refused(recent_limit),
        "worker": worker,
    }


def aggregates(*, hours: int = 24, top: int = 10) -> dict[str, Any]:
    return {
        "status_counts": status_counts(),
        "top_chats": top_chats(top),
        "providers": provider_counts(top),
        "duration_histogram": duration_histogram(),
        "timeseries": hourly_series(hours),
    }
