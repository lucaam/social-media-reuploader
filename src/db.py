"""SQLite persistence shared by the bot process and the admin GUI process.

The database is the single source of truth for the request queue, the request
state machine, per-chat rate-limit state, the worker heartbeat and the
cross-process event log (table ``updates``).

Rules followed by every function in this module:

* Each call opens its own short-lived connection through :func:`connect` and
  closes it before returning: connections are never shared between threads
  and nothing leaks when a statement fails.
* Connections use WAL, ``synchronous=NORMAL``, foreign keys and a busy timeout
  of :data:`BUSY_TIMEOUT_SECONDS`. They run in autocommit mode; every write is
  an explicit ``BEGIN IMMEDIATE`` transaction (:func:`write`), so
  read-modify-write sequences are atomic across threads and processes.
* State transitions are conditional (``UPDATE ... WHERE id = ? AND status IN
  (...)``), return ``bool`` and append their event rows in the same
  transaction. This module never talks to websockets: the GUI tails the event
  log with :func:`list_events_after`.
* Errors are raised, never swallowed. There is no silent in-memory fallback;
  ``REQUESTS_DB=":memory:"`` is an explicit, test-only mode.
* Everything is synchronous: async code must call these functions through
  :func:`aio` so the event loop is never blocked.
* Timestamps are fixed-width UTC ISO-8601 strings with an explicit ``+00:00``
  offset (:func:`now_iso`), so string order is chronological order. (Event-log
  rows written by the old code keep their naive UTC timestamps until
  :func:`prune` deletes them: the migration does not rewrite that table.)

Never call another function of this module while holding a write transaction
(``with write() as conn:``): the nested call opens a second connection and
waits for the lock the caller holds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from . import config

logger = logging.getLogger(__name__)

# Version written to ``PRAGMA user_version`` once every migration has run.
SCHEMA_VERSION = 3
# Seconds a connection waits for a lock held by another connection before
# raising ``sqlite3.OperationalError: database is locked``. Read on every
# connect, so tests may lower it.
BUSY_TIMEOUT_SECONDS = 10.0
# Seconds a process waits for a schema migration that another process is
# running (bot and GUI start together after an upgrade) before giving up.
MIGRATION_TIMEOUT_SECONDS = 600.0
MEMORY_PATH = ":memory:"
MAX_PAGE_SIZE = 1000
MAX_EVENTS_PAGE = 5000
MAX_ERROR_MESSAGE_LENGTH = 2000
# processed_messages is a legacy dedupe table; rows older than this are pruned.
PROCESSED_MESSAGES_RETENTION_SECONDS = 2 * 86400
# Used by prune() when config.RATE_LIMITS is not defined.
_FALLBACK_RATE_WINDOW_SECONDS = 30 * 86400
_SQL_CHUNK = 500
_DELETE_BATCH = 500


class Status(StrEnum):
    """Request statuses stored in ``requests.status``."""

    QUEUED = "queued"  # waiting; not_before in the future means deferred
    PROCESSING = "processing"
    SENDING = "sending"
    DONE = "done"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"  # refused at submit
    DUPLICATE = "duplicate"  # refused at submit
    CANCELLED = "cancelled"  # by an admin
    ABORTED = "aborted"  # startup with rehydrate disabled
    PENDING = "pending"  # legacy, displayed as-is
    UNLIMITED = "unlimited"  # legacy, displayed as-is


# Requests that consume rate-limit quota.
ACCEPTED_STATUSES: tuple[str, ...] = (
    Status.QUEUED,
    Status.PROCESSING,
    Status.SENDING,
    Status.DONE,
    Status.FAILED,
)
# Requests that count for the per-chat pending cap (and are never deleted).
ACTIVE_STATUSES: tuple[str, ...] = (Status.QUEUED, Status.PROCESSING, Status.SENDING)
# A new link equal to one of these is a duplicate: an active one whatever its
# age, a done one delivered within the duplicate window.
DUPLICATE_STATUSES: tuple[str, ...] = (
    Status.QUEUED,
    Status.PROCESSING,
    Status.SENDING,
    Status.DONE,
)
# Statuses an admin may put back in the queue.
REQUEUEABLE_STATUSES: tuple[str, ...] = (
    Status.RATE_LIMITED,
    Status.FAILED,
    Status.CANCELLED,
    Status.ABORTED,
    Status.DUPLICATE,
    Status.UNLIMITED,
    Status.PENDING,
)
TERMINAL_STATUSES: tuple[str, ...] = tuple(
    status for status in Status if status not in ACTIVE_STATUSES
)
ROLES = frozenset({"admin", "user"})


class EventType(StrEnum):
    """``type`` values of the rows written to the event log (table ``updates``)."""

    REQUEST_CREATED = "request_created"
    REQUEST_STATUS = "request_status"
    REQUEST_UPDATED = "request_updated"
    REQUEST_EVENT = "request_event"
    QUEUE_CLEARED = "queue_cleared"
    RATE_LIMIT_CHANGED = "rate_limit_changed"
    CHAT_UNLIMITED = "chat_unlimited"
    CHAT_UNLIMITED_ALL = "chat_unlimited_all"
    HISTORY_CLEARED = "history_cleared"
    HISTORY_PRUNED = "history_pruned"
    UPDATE_CREATED = "update_created"  # legacy row whose raw text is not an event


# Columns that callers may change with set_request_fields().
_SETTABLE_FIELDS = frozenset(
    {
        "original_size",
        "final_size",
        "compressed",
        "provider",
        "media_type",
        "error_code",
        "error_message",
        "sent_message_id",
    }
)
_NOTICE_COLUMNS = {
    "rate": "last_rate_notice_at",
    "duplicate": "last_duplicate_notice_at",
}


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def _format_dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _utc_now() -> str:
    return _format_dt(datetime.now(UTC))


def now_iso() -> str:
    """Current time as ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00`` (fixed width, UTC)."""
    return _utc_now()


def iso_from_ts(ts: float) -> str:
    """Epoch seconds -> canonical UTC ISO string (see :func:`now_iso`)."""
    return _format_dt(datetime.fromtimestamp(ts, UTC))


def _parse_iso(value: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"expected an ISO-8601 string, got {type(value).__name__}")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        # Legacy rows were written with datetime.utcnow(): naive means UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def ts_from_iso(s: str) -> float:
    """ISO-8601 string -> epoch seconds.

    Accepts the canonical format, a ``Z`` suffix, any UTC offset and legacy
    naive values (interpreted as UTC). Raises ``ValueError`` on garbage.
    """
    return _parse_iso(s).timestamp()


def _canonical_iso(value: str | datetime) -> str:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return _format_dt(aware)
    return _format_dt(_parse_iso(value))


def _optional_iso(value: str | datetime | None) -> str | None:
    return None if value is None else _canonical_iso(value)


def _normalize_ts_value(value: Any) -> Any:
    """SQL function used by the migration: canonicalize one timestamp cell."""
    if not isinstance(value, str):
        return value
    try:
        return _canonical_iso(value)
    except ValueError:
        return value  # left untouched; the migration logs how many


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _marks(count: int) -> str:
    return ", ".join("?" * count)


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _page(limit: int, offset: int) -> tuple[int, int]:
    return min(max(int(limit), 0), MAX_PAGE_SIZE), max(int(offset), 0)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _truncate(value: Any, limit: int = MAX_ERROR_MESSAGE_LENGTH) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bool_int(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _validate_status(status: str) -> str:
    try:
        return Status(status).value
    except ValueError:
        raise ValueError(f"unknown request status {status!r}") from None


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"invalid role {role!r}; expected 'admin' or 'user'")
    return str(role)


def _normalize_email(email: Any) -> str | None:
    if email is None:
        return None
    return str(email).strip().casefold() or None


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
_memory_lock = threading.RLock()
_memory_conn: sqlite3.Connection | None = None
_wal_ready: set[str] = set()
_newer_schema_warned: set[int] = set()


def _db_path() -> str:
    """Database path, read from ``REQUESTS_DB`` on every call.

    Defaults to ``<cwd>/data/requests.db``; an empty value counts as unset
    (``sqlite3.connect("")`` would silently open a throw-away database).
    """
    path = os.environ.get("REQUESTS_DB", "").strip()
    return path or os.path.join(os.getcwd(), "data", "requests.db")


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is None:
        return "locked" in str(exc)
    return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _enable_wal(conn: sqlite3.Connection) -> str:
    """``PRAGMA journal_mode = WAL``; returns the resulting mode.

    Switching a rollback-journal database to WAL needs an exclusive lock.
    When several connections switch at once (bot and GUI starting together
    after an upgrade) SQLite returns SQLITE_BUSY immediately, without calling
    the busy handler, to avoid a deadlock: retry until the busy timeout.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    pause = 0.005
    while True:
        try:
            return str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
        time.sleep(pause)
        pause = min(pause * 2, 0.1)


def _ensure_wal(conn: sqlite3.Connection, path: str) -> None:
    if path in _wal_ready:
        return
    mode = _enable_wal(conn)
    if mode == "wal":
        _wal_ready.add(path)
    else:
        logger.warning("Could not enable WAL for %s (journal_mode=%s)", path, mode)


def _open_file(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    except sqlite3.Error as exc:
        raise sqlite3.OperationalError(f"cannot open database {path!r}: {exc}") from exc
    try:
        _configure(conn)
        _ensure_wal(conn, path)
        _ensure_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _memory_connection() -> sqlite3.Connection:
    """The process-wide in-memory connection (caller holds ``_memory_lock``)."""
    global _memory_conn
    if _memory_conn is None:
        conn = sqlite3.connect(
            MEMORY_PATH, check_same_thread=False, isolation_level=None
        )
        try:
            _configure(conn)
            _ensure_schema(conn)
        except BaseException:
            conn.close()
            raise
        _memory_conn = conn
    return _memory_conn


def _close_memory_db() -> None:
    """Drop the shared ``:memory:`` database (tests only)."""
    global _memory_conn
    with _memory_lock:
        if _memory_conn is not None:
            _memory_conn.close()
            _memory_conn = None


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a configured connection and close it on exit.

    Autocommit mode (``isolation_level=None``), ``sqlite3.Row`` rows, WAL,
    ``synchronous=NORMAL``, ``foreign_keys=ON``, busy timeout. The schema is
    created/migrated on first use of a database file. ``REQUESTS_DB=":memory:"``
    yields one shared, lock-protected connection (tests only). A database
    that cannot be opened raises ``sqlite3.OperationalError``.
    """
    path = _db_path()
    if path == MEMORY_PATH:
        with _memory_lock:
            conn = _memory_connection()
            try:
                yield conn
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        return
    conn = _open_file(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on any exception)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """``with db.write() as conn:`` = :func:`connect` + :func:`transaction`."""
    with connect() as conn, transaction(conn):
        yield conn


@contextmanager
def _read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run several SELECTs against one consistent snapshot."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")


def init_db() -> None:
    """Create the schema and run pending migrations (idempotent).

    Safe to call concurrently from several threads and processes: migrations
    run in one ``BEGIN IMMEDIATE`` transaction and re-check
    ``PRAGMA user_version`` under the lock; a caller that finds a migration in
    progress waits for it (up to :data:`MIGRATION_TIMEOUT_SECONDS`). Raises
    when the database cannot be opened or migrated.
    """
    with connect():
        pass


async def aio[**P, R](fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run a synchronous db function in a worker thread (never block the loop)."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Schema and migrations
# ---------------------------------------------------------------------------
_LEGACY_TABLES = (
    """CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        url TEXT,
        status TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        email TEXT,
        role TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS processed_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        message_id INTEGER,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS request_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER,
        event_type TEXT,
        details TEXT,
        duration_seconds REAL,
        created_at TEXT
    )""",
)
_LEGACY_REQUEST_COLUMNS = {
    "original_message_id": "INTEGER",
    "original_size": "INTEGER",
    "final_size": "INTEGER",
    "compressed": "INTEGER",
    "processing_started_at": "TEXT",
    "processing_finished_at": "TEXT",
    "processing_duration_seconds": "REAL",
    "description": "TEXT",
}
_LEGACY_EVENT_COLUMNS = {
    "event_type": "TEXT",
    "details": "TEXT",
    "duration_seconds": "REAL",
    "created_at": "TEXT",
}
_NEW_REQUEST_COLUMNS = {
    "chat_type": "TEXT",
    "user_id": "INTEGER",
    "normalized_url": "TEXT",
    "provider": "TEXT",
    "not_before": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "error_code": "TEXT",
    "error_message": "TEXT",
    "sent_message_id": "INTEGER",
    "media_type": "TEXT",
    "updated_at": "TEXT",
}
_NEW_TABLES = (
    """CREATE TABLE IF NOT EXISTS chat_limits (
        chat_id INTEGER PRIMARY KEY,
        reset_at TEXT,
        blocked_until TEXT,
        last_rate_notice_at TEXT,
        last_duplicate_notice_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS worker_status (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        heartbeat_at TEXT,
        payload TEXT
    )""",
)
# name -> (unique, table, columns, WHERE clause or "")
_NEW_INDEXES: dict[str, tuple[bool, str, str, str]] = {
    "idx_requests_status_not_before": (False, "requests", "status, not_before, id", ""),
    "idx_requests_chat_created": (False, "requests", "chat_id, created_at", ""),
    "idx_requests_chat_norm_url_created": (
        False,
        "requests",
        "chat_id, normalized_url, created_at",
        "",
    ),
    "idx_requests_chat_message": (
        False,
        "requests",
        "chat_id, original_message_id",
        "",
    ),
    "idx_users_email_norm": (True, "users", "email_norm", "email_norm IS NOT NULL"),
}
# The event log (``updates``) is left out on purpose: it can be the biggest
# legacy table, prune() deletes it by age soon after start (naive UTC strings
# still compare correctly enough for that) and the GUI parses naive values as
# UTC. Rewriting it would only make the migration longer and the WAL bigger.
_TIMESTAMP_COLUMNS = {
    "requests": ("created_at", "processing_started_at", "processing_finished_at"),
    "request_events": ("created_at",),
    "users": ("created_at",),
    "processed_messages": ("created_at",),
}
_CANONICAL_TS_GLOB = "????-??-??T??:??:??.??????+00:00"


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _add_missing_columns(
    conn: sqlite3.Connection, table: str, columns: Mapping[str, str]
) -> None:
    existing = _table_columns(conn, table)
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _migration_1_legacy_baseline(conn: sqlite3.Connection) -> None:
    """Bring any database written by the old code (v0.1 - v0.8) to its last shape."""
    for statement in _LEGACY_TABLES:
        conn.execute(statement)
    _add_missing_columns(conn, "requests", _LEGACY_REQUEST_COLUMNS)
    _add_missing_columns(conn, "request_events", _LEGACY_EVENT_COLUMNS)
    if not _index_exists(conn, "idx_processed_messages_chat_message"):
        # Old databases may hold duplicates that made the unique index fail.
        conn.execute("""DELETE FROM processed_messages
               WHERE chat_id IS NOT NULL AND message_id IS NOT NULL
                 AND id NOT IN (SELECT MIN(id) FROM processed_messages
                                GROUP BY chat_id, message_id)""")
        conn.execute(
            "CREATE UNIQUE INDEX idx_processed_messages_chat_message"
            " ON processed_messages(chat_id, message_id)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_request_events_request_id"
        " ON request_events(request_id)"
    )


def _normalize_timestamps(conn: sqlite3.Connection) -> None:
    conn.create_function("smr_normalize_ts", 1, _normalize_ts_value, deterministic=True)
    for table, columns in _TIMESTAMP_COLUMNS.items():
        for column in columns:
            # Only literal identifiers from _TIMESTAMP_COLUMNS reach this SQL.
            not_canonical = f"{column} IS NOT NULL AND {column} NOT GLOB ?"
            conn.execute(
                f"UPDATE {table} SET {column} = smr_normalize_ts({column})"
                f" WHERE {not_canonical}",
                (_CANONICAL_TS_GLOB,),
            )
            left = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {not_canonical}",
                (_CANONICAL_TS_GLOB,),
            ).fetchone()[0]
            if left:
                logger.warning(
                    "%d value(s) of %s.%s are not valid timestamps; left unchanged",
                    left,
                    table,
                    column,
                )


def _migrate_users(conn: sqlite3.Connection) -> None:
    """Fill email_norm, coerce roles and merge case-insensitive duplicates.

    Only the exact legacy role 'admin' granted admin rights, so every other
    value becomes 'user'. Duplicate emails keep the least privileged row (then
    the oldest one), so the merge never grants admin to anybody.
    """
    rows = conn.execute("SELECT id, email, role FROM users ORDER BY id").fetchall()
    keep: dict[str, tuple[int, str]] = {}
    drop: list[int] = []
    changes: list[tuple[str | None, str, int]] = []
    coerced = 0
    for row in rows:
        role = "admin" if row["role"] == "admin" else "user"
        coerced += role != row["role"]
        email_norm = _normalize_email(row["email"])
        changes.append((email_norm, role, row["id"]))
        if email_norm is None:
            continue
        kept = keep.get(email_norm)
        if kept is None:
            keep[email_norm] = (row["id"], role)
        elif kept[1] == "admin" and role == "user":
            drop.append(kept[0])
            keep[email_norm] = (row["id"], role)
        else:
            drop.append(row["id"])
    conn.executemany("UPDATE users SET email_norm = ?, role = ? WHERE id = ?", changes)
    for chunk in _chunks(drop, _SQL_CHUNK):
        conn.execute(f"DELETE FROM users WHERE id IN ({_marks(len(chunk))})", chunk)
    if coerced:
        logger.warning(
            "Set role 'user' on %d user row(s) with an invalid role", coerced
        )
    if drop:
        logger.warning(
            "Merged %d user row(s) with a duplicate (case-insensitive) email;"
            " the least privileged role was kept",
            len(drop),
        )


def _migration_2_queue_and_limits(conn: sqlite3.Connection) -> None:
    """DB-backed queue, rate-limit state, worker heartbeat, unique emails."""
    _add_missing_columns(conn, "requests", _NEW_REQUEST_COLUMNS)
    _add_missing_columns(conn, "users", {"email_norm": "TEXT"})
    for statement in _NEW_TABLES:
        conn.execute(statement)
    _normalize_timestamps(conn)
    conn.execute("""UPDATE requests
           SET updated_at = COALESCE(processing_finished_at, processing_started_at,
                                     created_at)
           WHERE updated_at IS NULL""")
    _migrate_users(conn)
    for name, (unique, table, columns, where) in _NEW_INDEXES.items():
        kind = "UNIQUE INDEX" if unique else "INDEX"
        partial = f" WHERE {where}" if where else ""
        # Every identifier comes from the _NEW_INDEXES literal above.
        conn.execute(
            f"CREATE {kind} IF NOT EXISTS {name} ON {table}({columns}){partial}"
        )


# How the rate-limit slot was derived before schema version 3 stored it. It
# moved to the retry time once a retried request was claimed again, and it
# could not be indexed. Only used to fill slot_at for existing rows.
_LEGACY_SLOT_SQL = """CASE
    WHEN processing_started_at IS NOT NULL
         AND processing_started_at < COALESCE(not_before, created_at)
    THEN processing_started_at
    ELSE COALESCE(not_before, created_at)
END"""


def _migration_3_rate_slots(conn: sqlite3.Connection) -> None:
    """Stored rate-limit slot (``requests.slot_at``) and a per-chat index.

    ``slot_at`` is when a request counts in its chat's rate-limit windows. It
    is set at submission (a requeue is a new submission) and never moved by
    claims or retries. The ``(chat_id, status, slot_at)`` index keeps the
    per-submit reads (pending cap, rate windows, active copies) proportional
    to the rows they return, not to the chat's whole history.
    """
    _add_missing_columns(conn, "requests", {"slot_at": "TEXT"})
    conn.execute(
        f"UPDATE requests SET slot_at = {_LEGACY_SLOT_SQL} WHERE slot_at IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_requests_chat_status_slot"
        " ON requests(chat_id, status, slot_at)"
    )


_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_1_legacy_baseline),
    (2, _migration_2_queue_and_limits),
    (3, _migration_3_rate_slots),
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    version = _user_version(conn)
    if version < SCHEMA_VERSION:
        _migrate(conn)
    elif version > SCHEMA_VERSION and version not in _newer_schema_warned:
        _newer_schema_warned.add(version)
        logger.warning(
            "Database schema version %d is newer than this code (%d)",
            version,
            SCHEMA_VERSION,
        )


def _set_busy_timeout(conn: sqlite3.Connection, seconds: float) -> None:
    conn.execute(f"PRAGMA busy_timeout = {max(0, int(seconds * 1000))}")


def _migrate(conn: sqlite3.Connection) -> None:
    # journal_mode cannot change inside a transaction; it is a no-op (returns
    # "memory") for in-memory databases.
    _enable_wal(conn)
    # Another process may be running this migration right now and a big legacy
    # database takes longer than the normal busy timeout: wait for its lock
    # (the re-check below then finds the schema up to date) instead of failing.
    _set_busy_timeout(conn, MIGRATION_TIMEOUT_SECONDS)
    try:
        with transaction(conn):
            version = _user_version(conn)  # re-check under the write lock
            if version >= SCHEMA_VERSION:
                return
            for target, migration in _MIGRATIONS:
                if version < target:
                    logger.info("Migrating database schema to version %d", target)
                    migration(conn)
                    version = target
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    finally:
        _set_busy_timeout(conn, BUSY_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Internal write helpers (they run inside the caller's transaction)
# ---------------------------------------------------------------------------
def _append_event(
    conn: sqlite3.Connection,
    event_type: str,
    payload: Mapping[str, Any],
    created_at: str,
) -> int:
    body: dict[str, Any] = {"type": str(event_type)}
    body.update((key, value) for key, value in payload.items() if key != "type")
    cur = conn.execute(
        "INSERT INTO updates (raw, created_at) VALUES (?, ?)",
        (_json(body), created_at),
    )
    return int(cur.lastrowid)


def _insert_request_event(
    conn: sqlite3.Connection,
    request_id: int,
    event_type: str,
    details: str | None,
    duration_seconds: float | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO request_events
               (request_id, event_type, details, duration_seconds, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (request_id, event_type, details, duration_seconds, created_at),
    )
    return int(cur.lastrowid)


def _fetch_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()


def _insert_request(
    conn: sqlite3.Connection,
    *,
    now: str,
    chat_id: int,
    url: str,
    status: str,
    normalized_url: str | None = None,
    provider: str | None = None,
    chat_type: str | None = None,
    user_id: int | None = None,
    description: str | None = None,
    original_message_id: int | None = None,
    not_before: str | datetime | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    status = _validate_status(status)
    not_before = _optional_iso(not_before)
    cur = conn.execute(
        """INSERT INTO requests (chat_id, url, status, created_at, updated_at,
               normalized_url, provider, chat_type, user_id, description,
               original_message_id, not_before, slot_at, error_code, error_message,
               attempts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            chat_id,
            url,
            status,
            now,
            now,
            normalized_url,
            provider,
            chat_type,
            user_id,
            description,
            original_message_id,
            not_before,
            not_before or now,  # slot_at: deferred at submit = counted then
            error_code,
            _truncate(error_message),
        ),
    )
    request_id = int(cur.lastrowid)
    _append_event(
        conn,
        EventType.REQUEST_CREATED,
        {
            "id": request_id,
            "request_id": request_id,
            "chat_id": chat_id,
            "url": url,
            "status": status,
            "not_before": not_before,
        },
        now,
    )
    return request_id


def _apply_transition(
    conn: sqlite3.Connection,
    before: Mapping[str, Any] | None,
    *,
    allowed_from: Collection[str],
    to_status: str,
    now: str,
    assignments: Mapping[str, Any] | None = None,
    increment_attempts: bool = False,
    history: str | None = None,
    details: str | None = None,
    duration: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> bool:
    """Conditionally move one request to ``to_status`` and record it.

    ``assignments`` keys are column names chosen by this module, never by
    callers. ``history`` names the request_events row to append (None = none).
    """
    if before is None or before["status"] not in allowed_from:
        return False
    allowed = tuple(str(status) for status in allowed_from)
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [str(to_status), now]
    for column, value in (assignments or {}).items():
        sets.append(f"{column} = ?")
        params.append(value)
    if increment_attempts:
        sets.append("attempts = attempts + 1")
    cur = conn.execute(
        f"UPDATE requests SET {', '.join(sets)}"
        f" WHERE id = ? AND status IN ({_marks(len(allowed))})",
        (*params, before["id"], *allowed),
    )
    if cur.rowcount != 1:
        return False
    request_id = int(before["id"])
    payload: dict[str, Any] = {
        "id": request_id,
        "request_id": request_id,
        "chat_id": before["chat_id"],
        "status": str(to_status),
        "previous": before["status"],
    }
    if extra:
        payload.update(extra)
    _append_event(conn, EventType.REQUEST_STATUS, payload, now)
    if history:
        _insert_request_event(conn, request_id, history, details, duration, now)
    return True


def _update_fields(
    conn: sqlite3.Connection, request_id: int, values: Mapping[str, Any], now: str
) -> bool:
    """Set non-status columns (names chosen by this module) + one event."""
    sets = ", ".join(f"{column} = ?" for column in values)
    rows = conn.execute(
        f"UPDATE requests SET {sets}, updated_at = ? WHERE id = ? RETURNING chat_id",
        (*values.values(), now, request_id),
    ).fetchall()
    if not rows:
        return False
    _append_event(
        conn,
        EventType.REQUEST_UPDATED,
        {"id": request_id, "request_id": request_id, "chat_id": rows[0][0], **values},
        now,
    )
    return True


def _duration(before: Mapping[str, Any] | None, now: str) -> float | None:
    started = before["processing_started_at"] if before is not None else None
    if not started:
        return None
    try:
        return max(0.0, ts_from_iso(now) - ts_from_iso(started))
    except ValueError:
        logger.warning("Request %s has an invalid processing_started_at", before["id"])
        return None


def _claim_row(
    conn: sqlite3.Connection, before: sqlite3.Row | None, now: str
) -> dict[str, Any] | None:
    if before is None:
        return None
    attempt = int(before["attempts"] or 0) + 1
    claimed = _apply_transition(
        conn,
        before,
        allowed_from=(Status.QUEUED,),
        to_status=Status.PROCESSING,
        now=now,
        assignments={
            "processing_started_at": now,
            "processing_finished_at": None,
            "processing_duration_seconds": None,
        },
        increment_attempts=True,
        history=Status.PROCESSING,
        details=f"attempt {attempt}",
        extra={"attempts": attempt},
    )
    return _dict(_fetch_request(conn, before["id"])) if claimed else None


# When a done request was posted. mark_done() always sets
# processing_finished_at; legacy rows may lack it.
_DELIVERED_AT_SQL = (
    "COALESCE(processing_finished_at, processing_started_at, created_at)"
)


def _has_active_duplicate(conn: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
    found = conn.execute(
        f"""SELECT 1 FROM requests
            WHERE chat_id = ? AND id != ? AND status IN ({_marks(len(ACTIVE_STATUSES))})
              AND ((normalized_url IS NOT NULL AND normalized_url = ?) OR url = ?)
            LIMIT 1""",
        (
            row["chat_id"],
            row["id"],
            *ACTIVE_STATUSES,
            row["normalized_url"],
            row["url"],
        ),
    ).fetchone()
    return found is not None


def _duplicate_window_seconds() -> float:
    return max(0.0, float(getattr(config, "DUPLICATE_WINDOW_SECONDS", 0) or 0))


def _has_delivered_copy(
    conn: sqlite3.Connection, row: Mapping[str, Any], now: float
) -> bool:
    """The chat got the row's link posted after the row was submitted, or
    less than the duplicate window before (the row was a duplicate of it)."""
    try:
        submitted = min(ts_from_iso(row["created_at"]), now)
        since = iso_from_ts(submitted - _duplicate_window_seconds())
    except (TypeError, ValueError):
        since = ""  # unknown submission time: any delivered copy counts
    link = row["normalized_url"] or row["url"]
    # Both lookups use (chat_id, normalized_url); see find_recent_duplicate.
    found = conn.execute(
        f"""SELECT 1 FROM requests
            WHERE chat_id = ? AND normalized_url = ? AND +status = ?
              AND {_DELIVERED_AT_SQL} >= ?
            UNION ALL
            SELECT 1 FROM requests
            WHERE chat_id = ? AND normalized_url IS NULL AND url IN (?, ?)
              AND +status = ? AND {_DELIVERED_AT_SQL} >= ?
            LIMIT 1""",
        (
            *(row["chat_id"], link, Status.DONE, since),
            *(row["chat_id"], link, row["url"], Status.DONE, since),
        ),
    ).fetchone()
    return found is not None


# ---------------------------------------------------------------------------
# Requests / queue
# ---------------------------------------------------------------------------
def create_request(
    chat_id: int,
    url: str,
    *,
    status: str = Status.QUEUED,
    normalized_url: str | None = None,
    provider: str | None = None,
    chat_type: str | None = None,
    user_id: int | None = None,
    description: str | None = None,
    original_message_id: int | None = None,
    not_before: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    """Insert a request (plus its ``request_created`` event) and return its id.

    ``status`` must be a :class:`Status` value; refusals are stored directly as
    ``rate_limited`` / ``duplicate`` with ``error_code``/``error_message``.
    ``not_before`` (ISO) defers a ``queued`` request.
    """
    now = _utc_now()
    with write() as conn:
        return _insert_request(
            conn,
            now=now,
            chat_id=chat_id,
            url=url,
            status=status,
            normalized_url=normalized_url,
            provider=provider,
            chat_type=chat_type,
            user_id=user_id,
            description=description,
            original_message_id=original_message_id,
            not_before=not_before,
            error_code=error_code,
            error_message=error_message,
        )


def find_request_for_message(
    chat_id: int, original_message_id: int | None, normalized_url: str
) -> dict[str, Any] | None:
    """Latest request (any status) for this message and link, or None.

    Matches ``normalized_url`` or, for legacy rows without it, ``url``. Used to
    ignore redelivered updates and edits that did not change the link.
    """
    if original_message_id is None:
        return None
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM requests
               WHERE chat_id = ? AND original_message_id = ?
                 AND (normalized_url = ? OR (normalized_url IS NULL AND url = ?))
               ORDER BY id DESC LIMIT 1""",
            (chat_id, original_message_id, normalized_url, normalized_url),
        ).fetchone()
    return _dict(row)


def find_recent_duplicate(
    chat_id: int, normalized_url: str, since_iso: str
) -> dict[str, Any] | None:
    """Latest request of the chat that makes a new copy of the link a duplicate.

    That is a queued/processing/sending request whatever its age (it is going
    to be posted; requeued rows, retries and a backlog keep their old
    created_at), or a done one delivered (``processing_finished_at``) since
    ``since_iso``. Refusals (rate_limited, duplicate), failures and
    cancellations never make a later link a duplicate. Matches
    ``normalized_url`` or, for legacy rows without it, ``url``.
    """
    since = _canonical_iso(since_iso)
    active = _marks(len(ACTIVE_STATUSES))
    with connect() as conn:
        # One index lookup per kind of copy (an OR over all of them would read
        # the chat's whole history): active copies on (chat_id, status),
        # delivered ones on (chat_id, normalized_url) - the unary + keeps the
        # planner off the status index, which would read every done row of
        # the chat. Legacy rows are only looked up among those created since.
        row = conn.execute(
            f"""SELECT * FROM (
                    SELECT * FROM requests
                    WHERE chat_id = ? AND status IN ({active})
                      AND (normalized_url = ? OR (normalized_url IS NULL AND url = ?))
                    UNION ALL
                    SELECT * FROM requests
                    WHERE chat_id = ? AND normalized_url = ? AND +status = ?
                      AND {_DELIVERED_AT_SQL} >= ?
                    UNION ALL
                    SELECT * FROM requests
                    WHERE chat_id = ? AND normalized_url IS NULL AND url = ?
                      AND created_at >= ? AND +status = ?)
                ORDER BY id DESC LIMIT 1""",
            (
                *(chat_id, *ACTIVE_STATUSES, normalized_url, normalized_url),
                *(chat_id, normalized_url, Status.DONE, since),
                *(chat_id, normalized_url, since, Status.DONE),
            ),
        ).fetchone()
    return _dict(row)


def claim_next_request(now_iso: str | None = None) -> dict[str, Any] | None:
    """Atomically claim the next due ``queued`` request (``processing``).

    Picks the oldest row with ``not_before`` unset or due (ordered by
    ``COALESCE(not_before, created_at), id``), sets ``processing_started_at``,
    increments ``attempts`` and returns the updated row, or None.
    """
    now = _canonical_iso(now_iso) if now_iso is not None else _utc_now()
    with write() as conn:
        before = conn.execute(
            """SELECT * FROM requests
               WHERE status = ? AND (not_before IS NULL OR not_before <= ?)
               ORDER BY COALESCE(not_before, created_at), id LIMIT 1""",
            (Status.QUEUED, now),
        ).fetchone()
        return _claim_row(conn, before, now)


def claim_request(
    request_id: int, *, now_iso: str | None = None
) -> dict[str, Any] | None:
    """Like :func:`claim_next_request` for one id (queued and due only)."""
    now = _canonical_iso(now_iso) if now_iso is not None else _utc_now()
    with write() as conn:
        before = conn.execute(
            """SELECT * FROM requests
               WHERE id = ? AND status = ? AND (not_before IS NULL OR not_before <= ?)""",
            (request_id, Status.QUEUED, now),
        ).fetchone()
        return _claim_row(conn, before, now)


def mark_sending(request_id: int) -> bool:
    """``processing`` -> ``sending`` (claim the upload). False otherwise."""
    now = _utc_now()
    with write() as conn:
        return _apply_transition(
            conn,
            _fetch_request(conn, request_id),
            allowed_from=(Status.PROCESSING,),
            to_status=Status.SENDING,
            now=now,
            history=Status.SENDING,
        )


def mark_done(
    request_id: int,
    *,
    sent_message_id: int | None = None,
    media_type: str | None = None,
    original_size: int | None = None,
    final_size: int | None = None,
    compressed: bool | None = None,
) -> bool:
    """``processing``/``sending`` -> ``done``; records sizes and the duration.

    Fields passed as None keep their current value. Clears error fields.
    """
    now = _utc_now()
    with write() as conn:
        before = _fetch_request(conn, request_id)
        duration = _duration(before, now)
        assignments: dict[str, Any] = {
            "processing_finished_at": now,
            "processing_duration_seconds": duration,
            "error_code": None,
            "error_message": None,
        }
        optional = {
            "sent_message_id": sent_message_id,
            "media_type": media_type,
            "original_size": original_size,
            "final_size": final_size,
            "compressed": _bool_int(compressed),
        }
        assignments.update({k: v for k, v in optional.items() if v is not None})
        return _apply_transition(
            conn,
            before,
            allowed_from=(Status.PROCESSING, Status.SENDING),
            to_status=Status.DONE,
            now=now,
            assignments=assignments,
            history=Status.DONE,
            details=media_type,
            duration=duration,
            extra={"sent_message_id": sent_message_id, "media_type": media_type},
        )


def mark_failed(
    request_id: int, *, error_code: str | None, error_message: str | None = None
) -> bool:
    """``queued``/``processing``/``sending`` -> ``failed`` with an error code."""
    now = _utc_now()
    message = _truncate(error_message)
    with write() as conn:
        before = _fetch_request(conn, request_id)
        assignments: dict[str, Any] = {
            "error_code": error_code,
            "error_message": message,
        }
        duration = _duration(before, now)
        if before is not None and before["processing_started_at"]:
            assignments["processing_finished_at"] = now
            assignments["processing_duration_seconds"] = duration
        details = f"{error_code}: {message}" if message else error_code
        return _apply_transition(
            conn,
            before,
            allowed_from=ACTIVE_STATUSES,
            to_status=Status.FAILED,
            now=now,
            assignments=assignments,
            history=Status.FAILED,
            details=details,
            duration=duration,
            extra={"error_code": error_code},
        )


def defer_request(
    request_id: int, not_before_iso: str, *, reason: str | None = None
) -> bool:
    """``processing`` -> ``queued`` with ``not_before`` (retry later)."""
    now = _utc_now()
    not_before = _canonical_iso(not_before_iso)
    details = f"until {not_before}" + (f": {reason}" if reason else "")
    with write() as conn:
        return _apply_transition(
            conn,
            _fetch_request(conn, request_id),
            allowed_from=(Status.PROCESSING,),
            to_status=Status.QUEUED,
            now=now,
            assignments={"not_before": not_before},
            history="deferred",
            details=details,
            extra={"not_before": not_before, "reason": reason},
        )


def release_sending(
    request_id: int, not_before_iso: str, *, reason: str | None = None
) -> bool:
    """``sending`` -> ``queued`` with ``not_before``: give back a claimed upload.

    Only for uploads that certainly did not reach Telegram (a flood wait, a
    connection that never got to it, the worker stopping before it sent
    anything): the request is retried later instead of failing. Keeps the
    rate-limit slot like :func:`defer_request`.
    """
    now = _utc_now()
    not_before = _canonical_iso(not_before_iso)
    details = f"until {not_before}" + (f": {reason}" if reason else "")
    with write() as conn:
        return _apply_transition(
            conn,
            _fetch_request(conn, request_id),
            allowed_from=(Status.SENDING,),
            to_status=Status.QUEUED,
            now=now,
            assignments={"not_before": not_before},
            history="deferred",
            details=details,
            extra={"not_before": not_before, "reason": reason, "released": True},
        )


def unclaim_sending(request_id: int, *, reason: str | None = None) -> bool:
    """``sending`` -> ``processing``: give back the upload claim in the same attempt.

    For a send that Telegram refused without posting anything (it could not
    fetch the URL itself): the worker goes on with a local download and claims
    the upload again with :func:`mark_sending`.
    """
    now = _utc_now()
    with write() as conn:
        return _apply_transition(
            conn,
            _fetch_request(conn, request_id),
            allowed_from=(Status.SENDING,),
            to_status=Status.PROCESSING,
            now=now,
            history="unclaimed",
            details=reason,
            extra={"reason": reason},
        )


def set_request_fields(request_id: int, **fields: Any) -> None:
    """Update whitelisted non-status columns (see ``_SETTABLE_FIELDS``).

    Raises ValueError for any other column (including ``status``).
    """
    if not fields:
        return
    unknown = sorted(set(fields) - _SETTABLE_FIELDS)
    if unknown:
        raise ValueError(f"cannot set request field(s): {', '.join(unknown)}")
    values = dict(fields)
    if "compressed" in values:
        values["compressed"] = _bool_int(values["compressed"])
    if "error_message" in values:
        values["error_message"] = _truncate(values["error_message"])
    now = _utc_now()
    with write() as conn:
        _update_fields(conn, request_id, values, now)


def recover_interrupted(*, rehydrate: bool, max_attempts: int) -> dict[str, int]:
    """Worker startup: settle requests left behind by a previous process.

    ``processing`` -> ``queued`` (``failed``/``interrupted`` once attempts >=
    max_attempts; ``aborted`` when not rehydrating); ``sending`` -> ``failed``
    (``interrupted_while_sending``: the upload may have happened, never resend
    automatically); when not rehydrating, ``queued`` -> ``aborted``.
    Assumes no other worker process is alive.
    """
    now = _utc_now()
    counts = {"requeued": 0, "failed": 0, "failed_sending": 0, "aborted": 0}
    with write() as conn:
        processing = conn.execute(
            "SELECT * FROM requests WHERE status = ? ORDER BY id", (Status.PROCESSING,)
        ).fetchall()
        for row in processing:
            attempts = int(row["attempts"] or 0)
            if attempts >= max_attempts:
                outcome, target = "failed", Status.FAILED
                assignments: dict[str, Any] = {
                    "error_code": "interrupted",
                    "error_message": f"interrupted {attempts} time(s); giving up",
                    "processing_finished_at": now,
                }
            elif rehydrate:
                outcome, target = "requeued", Status.QUEUED
                assignments = {"not_before": None}
            else:
                outcome, target = "aborted", Status.ABORTED
                assignments = {"error_code": "interrupted"}
            counts[outcome] += _apply_transition(
                conn,
                row,
                allowed_from=(Status.PROCESSING,),
                to_status=target,
                now=now,
                assignments=assignments,
                history="interrupted",
                details=f"processing interrupted (attempt {attempts}) -> {target}",
            )
        sending = conn.execute(
            "SELECT * FROM requests WHERE status = ? ORDER BY id", (Status.SENDING,)
        ).fetchall()
        for row in sending:
            counts["failed_sending"] += _apply_transition(
                conn,
                row,
                allowed_from=(Status.SENDING,),
                to_status=Status.FAILED,
                now=now,
                assignments={
                    "error_code": "interrupted_while_sending",
                    "error_message": "interrupted while uploading; not resent",
                    "processing_finished_at": now,
                },
                history="interrupted",
                details="sending interrupted -> failed",
            )
        if not rehydrate:
            queued = conn.execute(
                "SELECT * FROM requests WHERE status = ? ORDER BY id", (Status.QUEUED,)
            ).fetchall()
            for row in queued:
                counts["aborted"] += _apply_transition(
                    conn,
                    row,
                    allowed_from=(Status.QUEUED,),
                    to_status=Status.ABORTED,
                    now=now,
                    history=Status.ABORTED,
                    details="aborted at startup (rehydrate disabled)",
                )
    return counts


def cancel_queued(*, chat_id: int | None = None, actor: str | None = None) -> list[int]:
    """``queued`` (incl. deferred) -> ``cancelled``, in one transaction.

    Appends one ``request_status`` event per row and one ``queue_cleared``
    event. Returns the cancelled ids.
    """
    now = _utc_now()
    sql = "SELECT * FROM requests WHERE status = ?"
    params: list[Any] = [Status.QUEUED]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    details = f"cancelled by {actor}" if actor else "cancelled"
    cancelled: list[int] = []
    with write() as conn:
        for row in conn.execute(sql + " ORDER BY id", params).fetchall():
            if _apply_transition(
                conn,
                row,
                allowed_from=(Status.QUEUED,),
                to_status=Status.CANCELLED,
                now=now,
                history=Status.CANCELLED,
                details=details,
                extra={"actor": actor},
            ):
                cancelled.append(int(row["id"]))
        _append_event(
            conn,
            EventType.QUEUE_CLEARED,
            {
                "chat_id": chat_id,
                "count": len(cancelled),
                "ids": cancelled,
                "actor": actor,
            },
            now,
        )
    return cancelled


def requeue_requests(ids: Iterable[int], *, actor: str | None = None) -> list[int]:
    """Put refused/failed/cancelled/aborted requests back in the queue.

    Statuses accepted: see :data:`REQUEUEABLE_STATUSES`. A requeue counts as
    a new submission: the row becomes ``queued`` with ``not_before`` = now (it
    waits behind the rows already queued instead of jumping ahead with its
    old ``created_at``), a fresh rate-limit slot (now; like any row, it is not
    counted when the chat's limits were reset after it was created, as the
    GUI does before requeueing a chat's refusals), ``attempts`` 0 and no
    error. A row is skipped when the chat already has an active request for
    the same link (this also keeps at most one copy per link within one
    call), or got the link posted after the row was submitted or less than
    ``DUPLICATE_WINDOW_SECONDS`` before it: so a requeue never posts a link
    twice. Returns the ids actually requeued.
    """
    wanted = list(dict.fromkeys(int(request_id) for request_id in ids))
    current = time.time()
    now = iso_from_ts(current)
    requeued: list[int] = []
    with write() as conn:
        rows: dict[int, sqlite3.Row] = {}
        for chunk in _chunks(wanted, _SQL_CHUNK):
            for row in conn.execute(
                f"SELECT * FROM requests WHERE id IN ({_marks(len(chunk))})", chunk
            ):
                rows[int(row["id"])] = row
        for request_id in wanted:
            row = rows.get(request_id)
            if row is None or row["status"] not in REQUEUEABLE_STATUSES:
                continue
            if _has_active_duplicate(conn, row) or _has_delivered_copy(
                conn, row, current
            ):
                continue
            details = f"requeued from {row['status']}"
            if actor:
                details += f" by {actor}"
            if _apply_transition(
                conn,
                row,
                allowed_from=REQUEUEABLE_STATUSES,
                to_status=Status.QUEUED,
                now=now,
                assignments={
                    "not_before": now,
                    "slot_at": now,
                    "attempts": 0,
                    "error_code": None,
                    "error_message": None,
                },
                history="requeued",
                details=details,
                extra={"actor": actor},
            ):
                requeued.append(request_id)
    return requeued


def get_request(request_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return _dict(_fetch_request(conn, request_id))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _request_filters(
    status: str | Sequence[str] | None, chat_id: int | None, q: str | None
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        statuses = [status] if isinstance(status, str) else list(status)
        if statuses:
            clauses.append(f"status IN ({_marks(len(statuses))})")
            params.extend(str(item) for item in statuses)
        else:
            clauses.append("0")
    if chat_id is not None:
        clauses.append("chat_id = ?")
        params.append(chat_id)
    if q is not None and q.strip():
        pattern = f"%{_escape_like(q.strip())}%"
        clauses.append(
            "(url LIKE ? ESCAPE '\\' OR normalized_url LIKE ? ESCAPE '\\'"
            " OR description LIKE ? ESCAPE '\\'"
            " OR CAST(chat_id AS TEXT) LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 4)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def list_requests(
    limit: int = 50,
    offset: int = 0,
    *,
    status: str | Sequence[str] | None = None,
    chat_id: int | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """Requests newest first (full rows as dicts).

    ``status`` is one status or a sequence of statuses; ``q`` is a
    case-insensitive (ASCII) substring of url, normalized_url, description or
    chat id. ``limit`` is clamped to 0..MAX_PAGE_SIZE, ``offset`` to >= 0.
    """
    where, params = _request_filters(status, chat_id, q)
    page, skip = _page(limit, offset)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM requests{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, page, skip),
        ).fetchall()
    return _dicts(rows)


def count_requests(
    *,
    status: str | Sequence[str] | None = None,
    chat_id: int | None = None,
    q: str | None = None,
) -> int:
    """Number of requests matching the :func:`list_requests` filters."""
    where, params = _request_filters(status, chat_id, q)
    with connect() as conn:
        return int(
            conn.execute(f"SELECT COUNT(*) FROM requests{where}", params).fetchone()[0]
        )


def count_active_for_chat(chat_id: int) -> int:
    """Queued + processing + sending requests of the chat (pending cap)."""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) FROM requests
                WHERE chat_id = ? AND status IN ({_marks(len(ACTIVE_STATUSES))})""",
            (chat_id, *ACTIVE_STATUSES),
        ).fetchone()
    return int(row[0])


def accepted_times(chat_id: int, since_iso: str) -> list[float]:
    """Epoch slots (sorted) of the chat's accepted requests since ``since_iso``.

    Accepted = :data:`ACCEPTED_STATUSES`. Rows created before
    ``chat_limits.reset_at`` are ignored. The slot (``requests.slot_at``) is
    fixed at submission: ``COALESCE(not_before, created_at)``, so requests
    deferred at submit count at their ``not_before``. Claims and retries
    (:func:`defer_request`) never move it; :func:`requeue_requests` gives the
    row a new one (the requeue time).
    """
    since = _canonical_iso(since_iso)
    with connect() as conn:
        # One index range per status on (chat_id, status, slot_at): only the
        # window is read. The unary + makes reset_at a plain filter, so the
        # planner never prefers a created_at range over the whole history.
        rows = conn.execute(
            f"""SELECT slot_at FROM requests
                WHERE chat_id = ?
                  AND status IN ({_marks(len(ACCEPTED_STATUSES))})
                  AND slot_at >= ?
                  AND +created_at >= COALESCE(
                      (SELECT reset_at FROM chat_limits WHERE chat_id = ?), '')
                ORDER BY slot_at""",
            (chat_id, *ACCEPTED_STATUSES, since, chat_id),
        ).fetchall()
    times: list[float] = []
    for row in rows:
        try:
            times.append(ts_from_iso(row[0]))
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring an invalid timestamp %r of chat %s", row[0], chat_id
            )
    return times


def queue_snapshot(limit: int = 50) -> dict[str, Any]:
    """``{"queued", "deferred", "running", "counts", "generated_at"}``.

    queued: due ``queued`` rows in claim order; deferred: ``queued`` rows with
    a future ``not_before``; running: ``processing`` + ``sending`` rows.
    ``counts`` has queued/deferred/processing/sending/running totals (not
    limited). One consistent snapshot.
    """
    now = _utc_now()
    page = min(max(int(limit), 0), MAX_PAGE_SIZE)
    with connect() as conn, _read_snapshot(conn):
        queued = conn.execute(
            """SELECT * FROM requests
               WHERE status = ? AND (not_before IS NULL OR not_before <= ?)
               ORDER BY COALESCE(not_before, created_at), id LIMIT ?""",
            (Status.QUEUED, now, page),
        ).fetchall()
        deferred = conn.execute(
            """SELECT * FROM requests WHERE status = ? AND not_before > ?
               ORDER BY not_before, id LIMIT ?""",
            (Status.QUEUED, now, page),
        ).fetchall()
        running = conn.execute(
            """SELECT * FROM requests WHERE status IN (?, ?)
               ORDER BY processing_started_at, id LIMIT ?""",
            (Status.PROCESSING, Status.SENDING, page),
        ).fetchall()
        totals = conn.execute(
            """SELECT
                 COALESCE(SUM(status = ? AND (not_before IS NULL OR not_before <= ?)), 0),
                 COALESCE(SUM(status = ? AND not_before > ?), 0),
                 COALESCE(SUM(status = ?), 0),
                 COALESCE(SUM(status = ?), 0)
               FROM requests WHERE status IN (?, ?, ?)""",
            (
                Status.QUEUED,
                now,
                Status.QUEUED,
                now,
                Status.PROCESSING,
                Status.SENDING,
                *ACTIVE_STATUSES,
            ),
        ).fetchone()
    counts = {
        "queued": int(totals[0]),
        "deferred": int(totals[1]),
        "processing": int(totals[2]),
        "sending": int(totals[3]),
    }
    counts["running"] = counts["processing"] + counts["sending"]
    return {
        "queued": _dicts(queued),
        "deferred": _dicts(deferred),
        "running": _dicts(running),
        "counts": counts,
        "generated_at": now,
    }


# ---------------------------------------------------------------------------
# Request events (per-request history)
# ---------------------------------------------------------------------------
def add_request_event(
    request_id: int,
    event_type: str,
    details: str | Mapping[str, Any] | None = None,
    duration_seconds: float | None = None,
) -> int:
    """Append a request_events row (+ one ``request_event`` log row); its id."""
    now = _utc_now()
    text = details if details is None or isinstance(details, str) else _json(details)
    with write() as conn:
        event_id = _insert_request_event(
            conn, request_id, event_type, text, duration_seconds, now
        )
        owner = conn.execute(
            "SELECT chat_id FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        _append_event(
            conn,
            EventType.REQUEST_EVENT,
            {
                "request_id": request_id,
                "request_event_id": event_id,
                "chat_id": owner[0] if owner is not None else None,
                "event_type": event_type,
                "duration_seconds": duration_seconds,
            },
            now,
        )
    return event_id


_EVENT_COLUMNS = "id, request_id, event_type, details, duration_seconds, created_at"


def get_request_events(
    request_id: int, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """The request's events, NEWEST first (so a limit keeps the latest ones)."""
    page, skip = _page(limit, offset)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT {_EVENT_COLUMNS} FROM request_events WHERE request_id = ?
                ORDER BY id DESC LIMIT ? OFFSET ?""",
            (request_id, page, skip),
        ).fetchall()
    return _dicts(rows)


def get_request_events_bulk(
    request_ids: Iterable[int], *, limit_per_request: int = 50
) -> dict[int, list[dict[str, Any]]]:
    """``{request_id: [events newest first]}`` for many requests in few queries.

    Every requested id is a key (empty list when it has no events); at most
    ``limit_per_request`` events per request.
    """
    ids = list(dict.fromkeys(int(request_id) for request_id in request_ids))
    result: dict[int, list[dict[str, Any]]] = {request_id: [] for request_id in ids}
    per_request = min(max(int(limit_per_request), 0), MAX_PAGE_SIZE)
    if not ids or per_request == 0:
        return result
    with connect() as conn:
        for chunk in _chunks(ids, _SQL_CHUNK):
            rows = conn.execute(
                f"""SELECT {_EVENT_COLUMNS} FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY request_id ORDER BY id DESC) AS position
                        FROM request_events WHERE request_id IN ({_marks(len(chunk))}))
                    WHERE position <= ? ORDER BY request_id, id DESC""",
                (*chunk, per_request),
            ).fetchall()
            for row in rows:
                result[int(row["request_id"])].append(dict(row))
    return result


# ---------------------------------------------------------------------------
# Event log (table `updates`): the only channel between bot and GUI
# ---------------------------------------------------------------------------
def add_event(event_type: str, /, **payload: Any) -> int:
    """Append ``{"type": event_type, **payload}`` to the event log; its id."""
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("event_type must be a non-empty string")
    now = _utc_now()
    with write() as conn:
        return _append_event(conn, event_type, payload, now)


def _decode_event(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["raw"]
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        event = {
            "id": row["id"],
            "created_at": row["created_at"],
            "type": payload["type"],
        }
        event.update(
            (key, value)
            for key, value in payload.items()
            if key not in ("id", "created_at", "type")
        )
        return event
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "type": EventType.UPDATE_CREATED.value,
        "raw": raw,
    }


def list_events_after(last_id: int, limit: int = 500) -> list[dict[str, Any]]:
    """Events with id > last_id, oldest first.

    Each item is ``{"id", "created_at", "type", ...payload}`` where ``id`` and
    ``created_at`` are the event row's own (tail with ``last_id = items[-1]["id"]``);
    request events carry the request id as ``request_id``. Rows that are not
    JSON events (legacy) come back as ``{"type": "update_created", "raw": ...}``.
    """
    page = min(max(int(limit), 0), MAX_EVENTS_PAGE)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, raw, created_at FROM updates WHERE id > ? ORDER BY id LIMIT ?",
            (int(last_id), page),
        ).fetchall()
    return [_decode_event(row) for row in rows]


def latest_event_id() -> int:
    """Id of the newest event (0 when the log is empty)."""
    with connect() as conn:
        return int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM updates").fetchone()[0]
        )


# ---------------------------------------------------------------------------
# Chat limits
# ---------------------------------------------------------------------------
def get_chat_limit(chat_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return _dict(
            conn.execute(
                "SELECT * FROM chat_limits WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        )


def set_chat_blocked_until(chat_id: int, until_iso: str | None) -> None:
    """Store (or clear with None) until when the chat is shown as rate limited.

    Informational only (GUI "currently limited"): the rate limiter never reads
    it, so a chat is never blocked for other links.
    """
    until = _optional_iso(until_iso)
    now = _utc_now()
    with write() as conn:
        conn.execute(
            """INSERT INTO chat_limits (chat_id, blocked_until, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   blocked_until = excluded.blocked_until,
                   updated_at = excluded.updated_at""",
            (chat_id, until, now),
        )
        _append_event(
            conn,
            EventType.RATE_LIMIT_CHANGED,
            {"chat_id": chat_id, "blocked_until": until},
            now,
        )


def reset_chat_limit(chat_id: int, *, actor: str | None = None) -> None:
    """Forget the chat's rate-limit history (reset_at = now, unblock)."""
    now = _utc_now()
    with write() as conn:
        conn.execute(
            """INSERT INTO chat_limits (chat_id, reset_at, blocked_until, updated_at)
               VALUES (?, ?, NULL, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   reset_at = excluded.reset_at,
                   blocked_until = NULL,
                   updated_at = excluded.updated_at""",
            (chat_id, now, now),
        )
        _append_event(
            conn,
            EventType.CHAT_UNLIMITED,
            {"chat_id": chat_id, "actor": actor, "reset_at": now},
            now,
        )


def reset_all_chat_limits(*, actor: str | None = None) -> int:
    """:func:`reset_chat_limit` for every chat with accepted requests or a
    limit row, in one transaction; returns the number of chats reset."""
    now = _utc_now()
    with write() as conn:
        conn.execute(
            f"""INSERT INTO chat_limits (chat_id, updated_at)
                SELECT DISTINCT chat_id, ? FROM requests
                WHERE chat_id IS NOT NULL
                  AND status IN ({_marks(len(ACCEPTED_STATUSES))})
                ON CONFLICT(chat_id) DO NOTHING""",
            (now, *ACCEPTED_STATUSES),
        )
        count = conn.execute(
            "UPDATE chat_limits SET reset_at = ?, blocked_until = NULL, updated_at = ?",
            (now, now),
        ).rowcount
        _append_event(
            conn,
            EventType.CHAT_UNLIMITED_ALL,
            {"count": count, "actor": actor, "reset_at": now},
            now,
        )
    return int(count)


def list_chat_limits(
    *, only_blocked: bool = False, now_iso: str | None = None
) -> list[dict[str, Any]]:
    """chat_limits rows (blocked first) plus ``blocked`` / ``remaining_seconds``."""
    now = _canonical_iso(now_iso) if now_iso is not None else _utc_now()
    sql = "SELECT * FROM chat_limits"
    params: tuple[Any, ...] = ()
    if only_blocked:
        sql += " WHERE blocked_until IS NOT NULL AND blocked_until > ?"
        params = (now,)
    sql += " ORDER BY blocked_until IS NULL, blocked_until DESC, chat_id"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    now_ts = ts_from_iso(now)
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        until = item["blocked_until"]
        blocked = bool(until) and until > now
        item["blocked"] = blocked
        item["remaining_seconds"] = (
            max(0.0, ts_from_iso(until) - now_ts) if blocked else 0.0
        )
        result.append(item)
    return result


def should_notify(chat_id: int, kind: str, throttle_seconds: float) -> bool:
    """Atomically check-and-stamp the chat's last ``kind`` notice.

    ``kind`` is "rate" or "duplicate". Returns True (and records now) when no
    notice of that kind was sent in the last ``throttle_seconds``.
    """
    column = _NOTICE_COLUMNS.get(kind)
    if column is None:
        raise ValueError(
            f"unknown notice kind {kind!r}; expected 'rate' or 'duplicate'"
        )
    current = time.time()
    now = iso_from_ts(current)
    threshold = iso_from_ts(current - max(0.0, float(throttle_seconds)))
    with write() as conn:
        # column comes from _NOTICE_COLUMNS, never from the caller.
        cur = conn.execute(
            f"""INSERT INTO chat_limits (chat_id, {column}, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    {column} = excluded.{column},
                    updated_at = excluded.updated_at
                WHERE chat_limits.{column} IS NULL OR chat_limits.{column} <= ?""",
            (chat_id, now, now, threshold),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Worker status (heartbeat, one row)
# ---------------------------------------------------------------------------
def set_worker_status(payload: Mapping[str, Any]) -> None:
    """Upsert the worker heartbeat row (heartbeat_at = now)."""
    body = _json(dict(payload))
    with write() as conn:
        conn.execute(
            """INSERT INTO worker_status (id, heartbeat_at, payload) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   heartbeat_at = excluded.heartbeat_at,
                   payload = excluded.payload""",
            (_utc_now(), body),
        )


def get_worker_status() -> dict[str, Any] | None:
    """``{"heartbeat_at", "age_seconds", **payload}`` or None if never written."""
    with connect() as conn:
        row = conn.execute(
            "SELECT heartbeat_at, payload FROM worker_status WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"]) if row["payload"] else {}
    status: dict[str, Any] = (
        payload if isinstance(payload, dict) else {"payload": payload}
    )
    heartbeat = row["heartbeat_at"]
    status["heartbeat_at"] = heartbeat
    status["age_seconds"] = (
        max(0.0, time.time() - ts_from_iso(heartbeat)) if heartbeat else None
    )
    return status


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
_USER_COLUMNS = "id, username, email, role, created_at"


def add_user(
    username: str | None = None, email: str | None = None, role: str = "user"
) -> int:
    """Create a user. ValueError for an invalid role or an already used email
    (compared case-insensitively, surrounding spaces ignored)."""
    role = _validate_role(role)
    email_norm = _normalize_email(email)
    stored_email = email.strip() if isinstance(email, str) and email.strip() else None
    with write() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO users (username, email, email_norm, role, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (username, stored_email, email_norm, role, _utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"a user with email {email!r} already exists") from exc
        return int(cur.lastrowid)


def get_user_by_email(email: str | None) -> dict[str, Any] | None:
    """The user whose email matches case-insensitively, or None."""
    email_norm = _normalize_email(email)
    if email_norm is None:
        return None
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE email_norm = ?", (email_norm,)
        ).fetchone()
    return _dict(row)


def list_users(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    page, skip = _page(limit, offset)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users ORDER BY id DESC LIMIT ? OFFSET ?",
            (page, skip),
        ).fetchall()
    return _dicts(rows)


def set_user_role(user_id: int, role: str) -> bool:
    """Change a user's role (ValueError if invalid); False if no such user."""
    role = _validate_role(role)
    with write() as conn:
        cur = conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    with write() as conn:
        return conn.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
def _max_rate_window_seconds() -> float:
    limits = getattr(config, "RATE_LIMITS", None)
    if limits is None:
        return float(_FALLBACK_RATE_WINDOW_SECONDS)
    return float(max((int(window) for _count, window in limits), default=0))


def _delete_ids(
    conn: sqlite3.Connection, table: str, column: str, ids: Sequence[int]
) -> int:
    # table/column are literals chosen by this module.
    cur = conn.execute(
        f"DELETE FROM {table} WHERE {column} IN ({_marks(len(ids))})", ids
    )
    return int(cur.rowcount)


def _prune_batches(select_sql: str, params: Sequence[Any], table: str) -> int:
    """Delete the rows ``select_sql`` returns, in short transactions."""
    total = 0
    while True:
        with write() as conn:
            ids = [row[0] for row in conn.execute(select_sql, (*params, _DELETE_BATCH))]
            if ids:
                total += _delete_ids(conn, table, "id", ids)
        if len(ids) < _DELETE_BATCH:
            return total


def prune(
    *,
    updates_days: float,
    requests_days: float,
    keep_accepted_seconds: float | None = None,
) -> dict[str, int]:
    """Retention. 0 (or less) days = keep forever.

    Deletes event-log rows older than ``updates_days``; terminal requests (and
    their events) created more than ``requests_days`` ago; processed_messages
    older than two days; orphan request events. Never deletes ACTIVE requests,
    nor accepted requests still inside the largest rate-limit window
    (``keep_accepted_seconds``, default: from ``config.RATE_LIMITS``), nor
    done ones delivered within ``DUPLICATE_WINDOW_SECONDS``, so retention
    never weakens rate limiting or duplicate detection. Works in short
    batches.
    """
    current = time.time()
    counts = {"updates": 0, "requests": 0, "request_events": 0, "processed_messages": 0}
    if updates_days and updates_days > 0:
        counts["updates"] = _prune_batches(
            "SELECT id FROM updates WHERE created_at < ? ORDER BY id LIMIT ?",
            (iso_from_ts(current - float(updates_days) * 86400),),
            "updates",
        )
    if requests_days and requests_days > 0:
        protect = (
            _max_rate_window_seconds()
            if keep_accepted_seconds is None
            else max(0.0, float(keep_accepted_seconds))
        )
        removed, events = _prune_requests(
            iso_from_ts(current - float(requests_days) * 86400),
            iso_from_ts(current - protect),
            iso_from_ts(current - _duplicate_window_seconds()),
        )
        counts["requests"] = removed
        counts["request_events"] += events
    counts["processed_messages"] = _prune_batches(
        "SELECT id FROM processed_messages WHERE created_at < ? ORDER BY id LIMIT ?",
        (iso_from_ts(current - PROCESSED_MESSAGES_RETENTION_SECONDS),),
        "processed_messages",
    )
    counts["request_events"] += _prune_batches(
        """SELECT e.id FROM request_events e LEFT JOIN requests r ON r.id = e.request_id
           WHERE r.id IS NULL ORDER BY e.id LIMIT ?""",
        (),
        "request_events",
    )
    if any(counts.values()):
        add_event(EventType.HISTORY_PRUNED, **counts)
    return counts


def _expendable_requests(
    cutoff: str, accepted_cutoff: str, delivered_cutoff: str
) -> tuple[str, tuple[Any, ...]]:
    """WHERE clause (+ params): terminal requests created before ``cutoff``
    that neither the rate limiter nor duplicate detection needs any more.

    Needed: accepted requests whose slot is since ``accepted_cutoff`` (the
    largest rate-limit window) and done ones delivered since
    ``delivered_cutoff`` (the duplicate window).
    """
    active = _marks(len(ACTIVE_STATUSES))
    accepted = _marks(len(ACCEPTED_STATUSES))
    # NULL-safe: legacy rows without a status are terminal and never protected.
    where = f"""created_at < ?
          AND (status IS NULL OR status NOT IN ({active}))
          AND NOT (status IS NOT NULL AND status IN ({accepted})
                   AND COALESCE(slot_at, created_at) >= ?)
          AND NOT (status IS NOT NULL AND status = ?
                   AND {_DELIVERED_AT_SQL} >= ?)"""
    params = (
        cutoff,
        *ACTIVE_STATUSES,
        *ACCEPTED_STATUSES,
        accepted_cutoff,
        Status.DONE,
        delivered_cutoff,
    )
    return where, params


def _prune_requests(
    cutoff: str, accepted_cutoff: str, delivered_cutoff: str
) -> tuple[int, int]:
    where, params = _expendable_requests(cutoff, accepted_cutoff, delivered_cutoff)
    select_sql = f"SELECT id FROM requests WHERE {where} ORDER BY id LIMIT ?"
    params = (*params, _DELETE_BATCH)
    removed = events = 0
    while True:
        with write() as conn:
            ids = [row[0] for row in conn.execute(select_sql, params)]
            if ids:
                events += _delete_ids(conn, "request_events", "request_id", ids)
                removed += _delete_ids(conn, "requests", "id", ids)
        if len(ids) < _DELETE_BATCH:
            return removed, events


def clear_history(
    *, actor: str | None = None, keep_accepted_seconds: float | None = None
) -> dict[str, int]:
    """Admin wipe: finished requests (+ their events) and the event log.

    The rows the bot still relies on are kept, so the chats' rate limits and
    duplicate detection are unchanged: accepted requests inside the largest
    rate-limit window (``keep_accepted_seconds``, default: from
    ``config.RATE_LIMITS``; the GUI passes the worker's effective limits),
    done ones delivered within ``DUPLICATE_WINDOW_SECONDS`` and every request
    created within it (edits of those messages are still honoured and are
    matched to their request), like :func:`prune`. Their number is returned
    as ``requests_kept``. Never touches queued/processing/sending requests
    (nor their events), chat_limits, users, worker_status or
    processed_messages. One transaction, followed by exactly one
    ``history_cleared`` event. Ids keep increasing.
    """
    current = time.time()
    now = iso_from_ts(current)
    recent = iso_from_ts(current - _duplicate_window_seconds())
    keep = (
        _max_rate_window_seconds()
        if keep_accepted_seconds is None
        else max(0.0, float(keep_accepted_seconds))
    )
    where, params = _expendable_requests(recent, iso_from_ts(current - keep), recent)
    active = _marks(len(ACTIVE_STATUSES))
    with write() as conn:
        requests = conn.execute(f"DELETE FROM requests WHERE {where}", params).rowcount
        events = conn.execute(
            """DELETE FROM request_events WHERE NOT EXISTS (
                   SELECT 1 FROM requests r WHERE r.id = request_events.request_id)"""
        ).rowcount
        kept = conn.execute(
            f"""SELECT COUNT(*) FROM requests
                WHERE status IS NULL OR status NOT IN ({active})""",
            ACTIVE_STATUSES,
        ).fetchone()[0]
        updates = conn.execute("DELETE FROM updates").rowcount
        counts = {
            "requests": requests,
            "request_events": events,
            "updates": updates,
            "requests_kept": int(kept),
        }
        _append_event(
            conn,
            EventType.HISTORY_CLEARED,
            {"actor": actor, "kept_seconds": keep, **counts},
            now,
        )
    return counts
