"""Tests for src/db.py: connections, migrations, state machine, events, limits."""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "data" / "requests.db"
    monkeypatch.setenv("REQUESTS_DB", str(path))
    db.init_db()
    return path


@pytest.fixture
def fast_busy(monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.3)


def raw_conn(path, timeout=10.0):
    """An independent connection (e.g. another process holding a lock)."""
    conn = sqlite3.connect(
        str(path), timeout=timeout, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def ago(seconds):
    return db.iso_from_ts(time.time() - seconds)


def make(chat_id=1, url="https://example.com/v/1", **kwargs):
    kwargs.setdefault("normalized_url", url)
    return db.create_request(chat_id, url, **kwargs)


def force(request_id, **columns):
    """Put a row in an arbitrary state, bypassing the state machine.

    Forcing ``created_at`` means "submitted then", so the rate-limit slot
    follows it (``COALESCE(not_before, created_at)``) unless ``slot_at`` is
    given too.
    """
    sets = [f"{name} = ?" for name in columns]
    params = list(columns.values())
    if "created_at" in columns and "slot_at" not in columns:
        sets.append("slot_at = COALESCE(not_before, ?)")
        params.append(columns["created_at"])
    with db.write() as conn:
        conn.execute(
            f"UPDATE requests SET {', '.join(sets)} WHERE id = ?", (*params, request_id)
        )


def events(kind=None):
    items = db.list_events_after(0, limit=5000)
    return [item for item in items if kind is None or item["type"] == kind]


def status_changes(request_id):
    return [
        (item["previous"], item["status"])
        for item in events("request_status")
        if item["request_id"] == request_id
    ]


def history(request_id):
    return [item["event_type"] for item in reversed(db.get_request_events(request_id))]


def status_of(request_id):
    return db.get_request(request_id)["status"]


def processing(chat_id=1, url="https://example.com/v/p", **kwargs):
    request_id = make(chat_id, url, **kwargs)
    assert db.claim_request(request_id) is not None
    return request_id


def processed_message_ids(chat_id):
    """Message ids of the chat in the legacy processed_messages table."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT message_id FROM processed_messages WHERE chat_id = ?"
            " ORDER BY message_id",
            (chat_id,),
        ).fetchall()
    return [row["message_id"] for row in rows]


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------
def test_now_iso_is_fixed_width_utc():
    value = db.now_iso()
    assert ISO_RE.match(value)
    assert abs(db.ts_from_iso(value) - time.time()) < 5


def test_iso_from_ts_and_back():
    assert db.iso_from_ts(0) == "1970-01-01T00:00:00.000000+00:00"
    assert db.iso_from_ts(1.5) == "1970-01-01T00:00:01.500000+00:00"
    ts = 1795795200.123456
    assert db.iso_from_ts(db.ts_from_iso(db.iso_from_ts(ts))) == db.iso_from_ts(ts)


def test_ts_from_iso_accepts_legacy_naive_z_and_offsets():
    expected = datetime(2026, 11, 26, 14, 0, tzinfo=UTC).timestamp()
    assert db.ts_from_iso("2026-11-26T14:00:00") == expected  # legacy naive = UTC
    assert db.ts_from_iso("2026-11-26T14:00:00Z") == expected
    assert db.ts_from_iso("2026-11-26T14:00:00z") == expected
    assert db.ts_from_iso("2026-11-26T16:00:00+02:00") == expected
    assert db.ts_from_iso("2026-11-26 14:00:00") == expected
    assert db.ts_from_iso("2026-11-26T14:00:00.000000+00:00") == expected
    with pytest.raises(ValueError):
        db.ts_from_iso("yesterday")
    with pytest.raises(ValueError):
        db.ts_from_iso("")


def test_canonical_strings_sort_chronologically():
    base = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    stamps = [base + offset for offset in (0, 0.000001, 0.5, 1, 59.999999, 3600, 1e6)]
    strings = [db.iso_from_ts(ts) for ts in stamps]
    assert sorted(strings) == strings
    assert len({len(s) for s in strings}) == 1


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------
def test_db_path_is_read_from_the_environment_on_every_call(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "a.db"))
    assert db._db_path() == str(tmp_path / "a.db")
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "b.db"))
    assert db._db_path() == str(tmp_path / "b.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REQUESTS_DB")
    assert db._db_path() == os.path.join(str(tmp_path), "data", "requests.db")
    monkeypatch.setenv("REQUESTS_DB", "  ")  # empty never means a temp database
    assert db._db_path() == os.path.join(str(tmp_path), "data", "requests.db")


def test_connect_configures_the_connection_and_closes_it(db_file):
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert conn.row_factory is sqlite3.Row
        assert conn.isolation_level is None
        assert not conn.in_transaction
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_closes_the_connection_when_the_body_raises(db_file):
    with pytest.raises(RuntimeError), db.connect() as conn:
        raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_no_connection_leaks_even_on_errors(db_file):
    def open_db_fds():
        found = []
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            if target.startswith(str(db_file)):
                found.append(target)
        return found

    request_id = make()
    db.add_user(email="a@example.com")
    for _ in range(20):
        db.claim_next_request()
        db.mark_sending(request_id)
        db.mark_done(request_id)
        db.list_requests(status="done", q="example")
        with pytest.raises(ValueError):
            db.add_user(email="A@example.com")
        with pytest.raises(sqlite3.OperationalError), db.connect() as conn:
            conn.execute("SELECT * FROM no_such_table")
        with pytest.raises(RuntimeError), db.write():
            raise RuntimeError("rollback")
    assert open_db_fds() == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions"
)
def test_unopenable_database_raises_instead_of_falling_back(tmp_path, monkeypatch):
    locked_dir = tmp_path / "ro"
    locked_dir.mkdir()
    locked_dir.chmod(0o555)
    try:
        monkeypatch.setenv("REQUESTS_DB", str(locked_dir / "requests.db"))
        with pytest.raises(sqlite3.OperationalError, match="cannot open database"):
            db.init_db()
        with pytest.raises(sqlite3.OperationalError):
            db.create_request(1, "https://example.com/x")
        with pytest.raises(sqlite3.OperationalError):
            db.list_requests()
        assert db._memory_conn is None  # no silent in-memory database
    finally:
        locked_dir.chmod(0o755)


def test_write_commits_or_rolls_back(db_file):
    with db.write() as conn:
        conn.execute("INSERT INTO updates (raw, created_at) VALUES ('a', ?)", (ago(0),))
    with pytest.raises(RuntimeError), db.write() as conn:
        conn.execute("INSERT INTO updates (raw, created_at) VALUES ('b', ?)", (ago(0),))
        raise RuntimeError("abort")
    assert [item["raw"] for item in events()] == ["a"]


def test_write_takes_the_write_lock_at_begin(db_file):
    other = raw_conn(db_file, timeout=0)
    try:
        with db.write(), pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")  # free again after COMMIT
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_transaction_helper_on_an_explicit_connection(db_file):
    with db.connect() as conn:
        with db.transaction(conn):
            assert conn.in_transaction
            conn.execute(
                "INSERT INTO updates (raw, created_at) VALUES ('t', ?)", (ago(0),)
            )
        assert not conn.in_transaction
    assert len(events()) == 1


def test_memory_database_is_shared_and_lock_protected(monkeypatch):
    monkeypatch.setenv("REQUESTS_DB", ":memory:")
    db._close_memory_db()
    try:
        db.init_db()
        first = make(url="https://example.com/m/0")
        assert db.get_request(first)["status"] == "queued"

        def worker(offset):
            for index in range(25):
                make(chat_id=offset, url=f"https://example.com/m/{offset}/{index}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert db.count_requests() == 101
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 101
        assert db.count_requests() == 101  # leaving connect() keeps the shared db
        with pytest.raises(RuntimeError), db.write() as conn:
            conn.execute("DELETE FROM requests")
            raise RuntimeError("rollback")
        assert db.count_requests() == 101
    finally:
        db._close_memory_db()


def test_aio_runs_in_a_worker_thread_and_propagates_errors(db_file):
    main_thread = threading.get_ident()

    async def scenario():
        thread_id = await db.aio(threading.get_ident)
        request_id = await db.aio(make, url="https://example.com/aio")
        row = await db.aio(db.get_request, request_id)
        with pytest.raises(ValueError):
            await db.aio(db.set_request_fields, request_id, status="done")
        return thread_id, row

    thread_id, row = asyncio.run(scenario())
    assert thread_id != main_thread
    assert row["url"] == "https://example.com/aio"


def test_db_module_never_broadcasts_to_websockets(db_file, monkeypatch):
    source = Path(db.__file__).read_text()
    assert "ws_broadcast" not in source
    assert "run_coroutine_threadsafe" not in source
    calls = []
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda *a: calls.append(a))
    request_id = make()
    db.claim_request(request_id)
    db.mark_done(request_id)
    db.add_request_event(request_id, "compress")
    db.clear_history()
    assert calls == []


# ---------------------------------------------------------------------------
# schema & migrations
# ---------------------------------------------------------------------------
LEGACY_DDL = (
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
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_messages_chat_message"
        " ON processed_messages(chat_id, message_id)"
    ),
    "ALTER TABLE requests ADD COLUMN original_message_id INTEGER",
    "ALTER TABLE requests ADD COLUMN original_size INTEGER",
    "ALTER TABLE requests ADD COLUMN final_size INTEGER",
    "ALTER TABLE requests ADD COLUMN compressed INTEGER",
    "ALTER TABLE requests ADD COLUMN processing_started_at TEXT",
    "ALTER TABLE requests ADD COLUMN processing_finished_at TEXT",
    "ALTER TABLE requests ADD COLUMN processing_duration_seconds REAL",
    """CREATE TABLE IF NOT EXISTS request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            event_type TEXT,
            details TEXT,
            duration_seconds REAL,
            created_at TEXT
        )""",
    (
        "CREATE INDEX IF NOT EXISTS idx_request_events_request_id"
        " ON request_events(request_id)"
    ),
    "ALTER TABLE requests ADD COLUMN description TEXT",
)

# fmt: off
LEGACY_REQUESTS = [
    # id, chat_id, url, status, created_at, original_message_id, started, finished
    (1, 100, "https://youtu.be/a", "done", "2026-11-20T10:00:00.123456", 5,
     "2026-11-20T10:00:01.000001", "2026-11-20T10:00:31.5"),
    (2, 100, "https://youtu.be/b", "pending", "2026-11-21T08:30:00", 6, None, None),
    (3, 200, "https://x.com/u/status/1", "unlimited", "2026-11-22T09:00:00.5", 7,
     None, None),
    (4, 200, "https://x.com/u/status/2", "queued", "2026-11-23T11:11:11.111111", 8,
     None, None),
    (5, 300, "https://vimeo.com/1", "processing", "2026-11-24T12:00:00", 9,
     "2026-11-24T12:00:05", None),
    (6, 300, "https://vimeo.com/2", "rate_limited", "2026-11-25T13:00:00", 10,
     None, None),
]
EXPECTED_REQUEST_COLUMNS = {
    "id", "chat_id", "url", "status", "created_at", "original_message_id",
    "original_size", "final_size", "compressed", "processing_started_at",
    "processing_finished_at", "processing_duration_seconds", "description",
    "chat_type", "user_id", "normalized_url", "provider", "not_before",
    "attempts", "error_code", "error_message", "sent_message_id",
    "media_type", "updated_at", "slot_at",
}
# fmt: on


def create_legacy_db(path):
    conn = sqlite3.connect(str(path))
    for statement in LEGACY_DDL:
        conn.execute(statement)
    conn.executemany(
        """INSERT INTO requests (id, chat_id, url, status, created_at,
               original_message_id, processing_started_at, processing_finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        LEGACY_REQUESTS,
    )
    conn.executemany(
        "INSERT INTO request_events (request_id, event_type, details, created_at)"
        " VALUES (?, ?, ?, ?)",
        [
            (1, "compress", "ok", "2026-11-20T10:00:20.000001"),
            (6, "rate_limited", "blocked", "2026-11-25T13:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO updates (raw, created_at) VALUES (?, ?)",
        [
            (json.dumps({"type": "request_created", "id": 1}), "2026-11-20T10:00:00"),
            (str({"chat_id": 100, "links": ["x"]}), "2026-11-20T10:00:00.25"),
        ],
    )
    conn.executemany(
        "INSERT INTO users (username, email, role, created_at) VALUES (?, ?, ?, ?)",
        [
            ("luca", "Luca@Example.com", "admin", "2026-01-01T00:00:00"),
            ("luca2", " luca@example.COM ", "user", "2026-01-02T00:00:00"),
            ("boss", "boss@example.com", "superadmin", "2026-01-03T00:00:00"),
            ("ops", "ops@example.com", "admin", "2026-01-04T00:00:00"),
            ("anon", None, None, "2026-01-05T00:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO processed_messages (chat_id, message_id, created_at)"
        " VALUES (?, ?, ?)",
        [(100, 5, "2026-11-20T10:00:00"), (100, 6, "2026-11-21T08:30:00")],
    )
    conn.commit()
    conn.close()


def dump(path):
    conn = raw_conn(path)
    try:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("requests", "request_events", "updates", "users")
        }
    finally:
        conn.close()


def test_migrates_a_database_created_by_the_old_code(tmp_path, monkeypatch):
    path = tmp_path / "requests.db"
    create_legacy_db(path)
    monkeypatch.setenv("REQUESTS_DB", str(path))

    db.init_db()
    after_first = dump(path)
    db.init_db()  # idempotent
    assert dump(path) == after_first

    conn = raw_conn(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
        assert columns >= EXPECTED_REQUEST_COLUMNS
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert "email_norm" in user_columns
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"chat_limits", "worker_status", "processed_messages"} <= tables
        indexes = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
            )
        }
        for name in (
            "idx_requests_status_not_before",
            "idx_requests_chat_created",
            "idx_requests_chat_norm_url_created",
            "idx_requests_chat_message",
            "idx_requests_chat_status_slot",
            "idx_users_email_norm",
            "idx_processed_messages_chat_message",
            "idx_request_events_request_id",
        ):
            assert name in indexes
        assert "status, not_before, id" in indexes["idx_requests_status_not_before"]
        assert "UNIQUE" in indexes["idx_users_email_norm"]

        rows = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM requests")}
        assert rows[1]["created_at"] == "2026-11-20T10:00:00.123456+00:00"
        assert rows[1]["processing_started_at"] == "2026-11-20T10:00:01.000001+00:00"
        assert rows[1]["processing_finished_at"] == "2026-11-20T10:00:31.500000+00:00"
        assert rows[2]["created_at"] == "2026-11-21T08:30:00.000000+00:00"
        assert rows[3]["created_at"] == "2026-11-22T09:00:00.500000+00:00"
        assert rows[2]["status"] == "pending"  # legacy statuses kept as-is
        assert rows[3]["status"] == "unlimited"
        for row in rows.values():
            assert ISO_RE.match(row["created_at"])
            assert ISO_RE.match(row["updated_at"])
            assert row["attempts"] == 0
            assert row["slot_at"] == row["created_at"]  # legacy rows: submission
        assert rows[1]["updated_at"] == rows[1]["processing_finished_at"]
        for table in ("request_events", "users", "processed_messages"):
            for (value,) in conn.execute(f"SELECT created_at FROM {table}"):
                assert ISO_RE.match(value), (table, value)
        # the legacy event log is not rewritten (prune deletes it by age)
        legacy_log = [row[0] for row in conn.execute("SELECT created_at FROM updates")]
        assert legacy_log == ["2026-11-20T10:00:00", "2026-11-20T10:00:00.25"]
    finally:
        conn.close()

    users = {user["username"]: user for user in db.list_users()}
    assert "luca" not in users  # duplicate emails merged: least privilege kept
    assert users["luca2"]["role"] == "user"
    assert users["boss"]["role"] == "user"  # invalid role coerced, never admin
    assert users["ops"]["role"] == "admin"
    assert users["anon"]["role"] == "user"
    assert db.get_user_by_email("LUCA@example.com")["username"] == "luca2"
    assert db.get_user_by_email("ops@EXAMPLE.com")["role"] == "admin"

    # the new API works on the migrated data
    claimed = db.claim_next_request()
    assert claimed["id"] == 4 and claimed["attempts"] == 1
    assert sorted(db.requeue_requests([2, 3, 6])) == [2, 3, 6]
    assert db.find_request_for_message(100, 5, "https://youtu.be/a")["id"] == 1
    legacy_events = db.list_events_after(0)
    assert legacy_events[0]["type"] == "request_created"
    assert legacy_events[1]["type"] == "update_created"
    assert legacy_events[1]["raw"].startswith("{'chat_id'")


def test_migrates_the_oldest_schema_with_duplicate_processed_messages(
    tmp_path, monkeypatch
):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(LEGACY_DDL[0])
    conn.execute(LEGACY_DDL[3])
    conn.execute(
        "INSERT INTO requests (chat_id, url, status, created_at)"
        " VALUES (1, 'https://a.b/c', 'done', '2025-01-01T00:00:00')"
    )
    conn.executemany(
        "INSERT INTO processed_messages (chat_id, message_id, created_at)"
        " VALUES (?, ?, ?)",
        [(1, 1, "2025-01-01T00:00:00")] * 3 + [(1, 2, "2025-01-01T00:00:00")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("REQUESTS_DB", str(path))
    db.init_db()
    check = raw_conn(path)
    try:
        assert (
            check.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0] == 2
        )
        with pytest.raises(sqlite3.IntegrityError):  # the unique index now exists
            check.execute(
                "INSERT INTO processed_messages (chat_id, message_id, created_at)"
                " VALUES (1, 1, '2025-01-02T00:00:00')"
            )
        names = {row[1] for row in check.execute("PRAGMA table_info(request_events)")}
        assert {"event_type", "details", "duration_seconds", "created_at"} <= names
    finally:
        check.close()
    assert processed_message_ids(1) == [1, 2]
    assert db.get_request(1)["created_at"] == "2025-01-01T00:00:00.000000+00:00"


def test_fresh_database_has_the_full_schema(db_file):
    with db.connect() as conn:
        assert db._user_version(conn) == db.SCHEMA_VERSION
        assert {"chat_type", "not_before", "attempts"} <= db._table_columns(
            conn, "requests"
        )
        assert db._index_exists(conn, "idx_requests_chat_message")
        assert db._index_exists(conn, "idx_requests_chat_status_slot")


def test_migration_3_stores_the_slot_existing_rows_already_had(db_file):
    plain = make(1, "https://a.b/plain")
    deferred = make(1, "https://a.b/deferred", not_before=ago(-60))
    retried = processing(1, "https://a.b/retried")
    first_start = db.get_request(retried)["processing_started_at"]
    db.defer_request(retried, ago(-900), reason="429")
    conn = raw_conn(db_file)  # back to a version 2 database
    try:
        conn.execute("DROP INDEX idx_requests_chat_status_slot")
        conn.execute("ALTER TABLE requests DROP COLUMN slot_at")
        conn.execute("PRAGMA user_version = 2")
    finally:
        conn.close()

    db.init_db()
    rows = {rid: db.get_request(rid) for rid in (plain, deferred, retried)}
    assert rows[plain]["slot_at"] == rows[plain]["created_at"]
    assert rows[deferred]["slot_at"] == rows[deferred]["not_before"]
    assert rows[retried]["slot_at"] == first_start  # the slot it had consumed
    with db.connect() as conn:
        assert db._user_version(conn) == db.SCHEMA_VERSION
        assert db._index_exists(conn, "idx_requests_chat_status_slot")


def test_failed_migration_is_rolled_back_and_raises(tmp_path, monkeypatch):
    path = tmp_path / "requests.db"
    create_legacy_db(path)
    monkeypatch.setenv("REQUESTS_DB", str(path))

    def broken(conn):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(db, "_migrate_users", broken)
    with pytest.raises(RuntimeError, match="disk on fire"):
        db.init_db()
    conn = raw_conn(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
        assert "not_before" not in columns  # DDL rolled back too
        created = conn.execute(
            "SELECT created_at FROM requests WHERE id = 1"
        ).fetchone()
        assert created[0] == "2026-11-20T10:00:00.123456"
    finally:
        conn.close()
    monkeypatch.undo()
    monkeypatch.setenv("REQUESTS_DB", str(path))
    db.init_db()
    assert db.get_request(1)["created_at"].endswith("+00:00")


def test_newer_schema_version_is_tolerated(db_file, caplog):
    conn = raw_conn(db_file)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 7}")
    conn.close()
    db.init_db()
    assert make() > 0
    assert any("newer than this code" in rec.getMessage() for rec in caplog.records)


class _FakeCursor:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return (self._value,)


class _FlakyWalConnection:
    """Reports SQLITE_BUSY like concurrent WAL switches do (no busy handler)."""

    def __init__(self, failures, error="database is locked"):
        self.failures = failures
        self.error = error
        self.calls = 0

    def execute(self, sql):
        assert sql == "PRAGMA journal_mode = WAL"
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError(self.error)
        return _FakeCursor("wal")


def test_enable_wal_retries_immediate_busy_errors(monkeypatch):
    conn = _FlakyWalConnection(failures=3)
    assert db._enable_wal(conn) == "wal"
    assert conn.calls == 4
    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db._enable_wal(_FlakyWalConnection(failures=10**6))
    other = _FlakyWalConnection(failures=1, error="disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db._enable_wal(other)
    assert other.calls == 1  # only lock contention is retried


def _init_db_from_threads(count):
    errors = []
    barrier = threading.Barrier(count)

    def run():
        barrier.wait()
        try:
            db.init_db()
        except Exception as exc:  # noqa: BLE001 - collected and asserted by callers
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_init_db_concurrently_from_threads(tmp_path, monkeypatch):
    for attempt in range(3):
        path = tmp_path / f"requests{attempt}.db"
        create_legacy_db(path)
        monkeypatch.setenv("REQUESTS_DB", str(path))
        assert _init_db_from_threads(8) == []
        assert db.get_request(2)["created_at"] == "2026-11-21T08:30:00.000000+00:00"
        assert len(db.list_users()) == 4
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "fresh" / "requests.db"))
    assert _init_db_from_threads(8) == []  # brand-new file
    assert make() > 0


def test_init_db_concurrently_from_processes(tmp_path):
    path = tmp_path / "requests.db"
    create_legacy_db(path)
    env = {**os.environ, "REQUESTS_DB": str(path), "PYTHONPATH": str(REPO_ROOT)}
    code = "from src import db; db.init_db(); print(db.get_request(1)['created_at'])"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            env=env,
            cwd=str(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    outputs = [proc.communicate(timeout=60) for proc in procs]
    for proc, (out, err) in zip(procs, outputs, strict=True):
        assert proc.returncode == 0, err
        assert out.strip() == "2026-11-20T10:00:00.123456+00:00"


SLOW_MIGRATION = """
import time
from src import db

original = db._migrate_users

def slow(conn):
    print("migrating", flush=True)
    time.sleep(1.5)
    original(conn)

db._migrate_users = slow
db.init_db()
print("done", flush=True)
"""


def test_init_db_waits_for_a_migration_running_in_another_process(
    tmp_path, monkeypatch
):
    """A migration longer than the busy timeout must not fail the other process."""
    path = tmp_path / "requests.db"
    create_legacy_db(path)
    env = {**os.environ, "REQUESTS_DB": str(path), "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-c", SLOW_MIGRATION],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The other process holds the write lock for 1.5 s from now on.
        assert proc.stdout.readline().strip() == "migrating"
        monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(db, "MIGRATION_TIMEOUT_SECONDS", 30.0, raising=False)
        monkeypatch.setenv("REQUESTS_DB", str(path))
        with db.connect() as conn:  # waits for that migration instead of failing
            version = db._user_version(conn)
            busy_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err
    assert out.strip() == "done"
    assert version == db.SCHEMA_VERSION
    assert busy_ms == 200  # the waiting connection is back to the normal timeout
    assert db.get_request(1)["created_at"] == "2026-11-20T10:00:00.123456+00:00"


def query_plans(monkeypatch, call):
    """EXPLAIN QUERY PLAN lines of every SELECT that ``call()`` really runs."""
    statements = []
    configure = db._configure

    def traced(conn):
        configure(conn)
        conn.set_trace_callback(statements.append)  # SQL with values inlined

    monkeypatch.setattr(db, "_configure", traced)
    try:
        call()
    finally:
        monkeypatch.setattr(db, "_configure", configure)
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert selects, "nothing was traced"
    with db.connect() as conn:
        return [
            row[3]
            for sql in selects
            for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")
        ]


def test_hot_queries_use_indexes(db_file, monkeypatch):
    """The per-submit and per-claim queries read index ranges, never a history."""
    make(1, "https://a.b/x", original_message_id=7)
    refused = db.create_request(1, "https://a.b/y", status="rate_limited")
    status_slot = "idx_requests_chat_status_slot"
    norm_url = "idx_requests_chat_norm_url_created"
    cases = {
        "pending cap": (
            lambda: db.count_active_for_chat(1),
            [f"USING COVERING INDEX {status_slot} (chat_id=? AND status=?)"],
        ),
        "rate windows": (
            lambda: db.accepted_times(1, ago(3600)),
            [f"USING INDEX {status_slot} (chat_id=? AND status=? AND slot_at>?)"],
        ),
        "message dedupe": (
            lambda: db.find_request_for_message(1, 7, "https://a.b/x"),
            ["idx_requests_chat_message (chat_id=? AND original_message_id=?)"],
        ),
        "duplicate": (
            lambda: db.find_recent_duplicate(1, "https://a.b/x", ago(5400)),
            [
                f"USING INDEX {status_slot} (chat_id=? AND status=?)",
                f"USING INDEX {norm_url} (chat_id=? AND normalized_url=?)",
                f"{norm_url} (chat_id=? AND normalized_url=? AND created_at>?)",
            ],
        ),
        "claim": (
            db.claim_next_request,
            ["USING INDEX idx_requests_status_not_before (status=?"],
        ),
        "requeue": (
            lambda: db.requeue_requests([refused]),
            [f"USING INDEX {status_slot} (chat_id=? AND status=?)"],
        ),
    }
    for name, (call, expected) in cases.items():
        lines = query_plans(monkeypatch, call)
        for wanted in expected:
            assert any(wanted in line for line in lines), (name, lines)
        for line in lines:
            assert not line.startswith("SCAN"), (name, lines)  # no full scans
            assert not line.endswith("(chat_id=?)"), (name, lines)  # whole chat


# ---------------------------------------------------------------------------
# requests: creation and lookups
# ---------------------------------------------------------------------------
def test_create_request_stores_fields_and_appends_an_event(db_file):
    request_id = db.create_request(
        -100123,
        "https://www.youtube.com/watch?v=x&si=1",
        normalized_url="https://youtube.com/watch?v=x",
        provider="Youtube",
        chat_type="supergroup",
        user_id=42,
        description="hi",
        original_message_id=7,
        not_before="2030-01-01T00:00:00Z",
    )
    row = db.get_request(request_id)
    assert isinstance(row, dict)
    assert row["status"] == "queued"
    assert row["chat_type"] == "supergroup" and row["user_id"] == 42
    assert row["provider"] == "Youtube" and row["attempts"] == 0
    assert row["not_before"] == "2030-01-01T00:00:00.000000+00:00"
    assert ISO_RE.match(row["created_at"]) and row["updated_at"] == row["created_at"]
    (created,) = events("request_created")
    assert created["request_id"] == request_id
    assert created["chat_id"] == -100123
    assert created["url"] == "https://www.youtube.com/watch?v=x&si=1"
    assert created["status"] == "queued"
    assert created["not_before"] == row["not_before"]
    with pytest.raises(ValueError):
        db.create_request(1, "https://a.b", status="bogus")


def test_refusals_are_stored_with_their_error(db_file):
    request_id = db.create_request(
        1,
        "https://a.b/c",
        status="rate_limited",
        error_code="rate_limited",
        error_message="x" * 5000,
    )
    row = db.get_request(request_id)
    assert row["status"] == "rate_limited" and row["error_code"] == "rate_limited"
    assert len(row["error_message"]) == db.MAX_ERROR_MESSAGE_LENGTH


def test_find_request_for_message(db_file):
    first = make(1, "https://a.b/1", original_message_id=10)
    make(1, "https://a.b/2", original_message_id=10)
    make(2, "https://a.b/1", original_message_id=10)
    force(first, status="done")
    found = db.find_request_for_message(1, 10, "https://a.b/1")
    assert found["id"] == first and found["status"] == "done"
    assert db.find_request_for_message(1, 11, "https://a.b/1") is None
    assert db.find_request_for_message(1, None, "https://a.b/1") is None
    # legacy rows have no normalized_url: they match by url
    legacy = db.create_request(1, "https://a.b/legacy", original_message_id=12)
    assert db.get_request(legacy)["normalized_url"] is None
    assert db.find_request_for_message(1, 12, "https://a.b/legacy")["id"] == legacy


@pytest.mark.parametrize(
    ("status", "is_duplicate"),
    [
        ("queued", True),
        ("processing", True),
        ("sending", True),
        ("done", True),
        ("failed", False),
        ("rate_limited", False),
        ("duplicate", False),
        ("cancelled", False),
        ("aborted", False),
    ],
)
def test_find_recent_duplicate_only_counts_real_requests(db_file, status, is_duplicate):
    request_id = make(5, "https://a.b/dup")
    force(request_id, status=status)
    found = db.find_recent_duplicate(5, "https://a.b/dup", ago(60))
    assert (found is not None) is is_duplicate
    assert db.find_recent_duplicate(6, "https://a.b/dup", ago(60)) is None


def test_find_recent_duplicate_window_and_latest_row(db_file):
    old = make(5, "https://a.b/w")
    force(old, created_at=ago(7200), status="done")
    assert db.find_recent_duplicate(5, "https://a.b/w", ago(5400)) is None
    assert db.find_recent_duplicate(5, "https://a.b/w", ago(9000))["id"] == old
    new = make(5, "https://a.b/w")
    assert db.find_recent_duplicate(5, "https://a.b/w", ago(9000))["id"] == new
    since_naive = (datetime.now(UTC) - timedelta(hours=3)).replace(tzinfo=None)
    assert db.find_recent_duplicate(5, "https://a.b/w", since_naive.isoformat())
    legacy = db.create_request(5, "https://a.b/legacy")  # no normalized_url
    assert db.find_recent_duplicate(5, "https://a.b/legacy", ago(60))["id"] == legacy


def test_find_recent_duplicate_sees_active_copies_whatever_their_age(db_file):
    window = ago(5400)
    requeued = db.create_request(
        1, "https://a.b/requeued", normalized_url="https://a.b/requeued"
    )
    force(requeued, status="rate_limited", created_at=ago(3 * 3600))
    db.reset_chat_limit(1, actor="admin")
    assert db.requeue_requests([requeued]) == [requeued]
    retried = make(1, "https://a.b/retried")
    force(retried, created_at=ago(2 * 3600))
    assert db.claim_request(retried)
    assert db.defer_request(retried, ago(-3000), reason="flood wait")
    backlog = make(1, "https://a.b/backlog")
    force(backlog, created_at=ago(100 * 60))
    for request_id, url in (
        (requeued, "https://a.b/requeued"),
        (retried, "https://a.b/retried"),
        (backlog, "https://a.b/backlog"),
    ):
        found = db.find_recent_duplicate(1, url, window)
        assert found is not None and found["id"] == request_id, url
        assert found["status"] == "queued"


def test_find_recent_duplicate_anchors_delivered_copies_on_delivery(db_file):
    window = ago(5400)
    late = processing(1, "https://a.b/late")
    force(late, created_at=ago(3 * 3600))  # waited long, posted a minute ago
    assert db.mark_done(late)
    assert db.find_recent_duplicate(1, "https://a.b/late", window)["id"] == late
    old = processing(1, "https://a.b/old")
    assert db.mark_done(old)
    force(old, created_at=ago(3 * 3600), processing_finished_at=ago(2 * 3600))
    assert db.find_recent_duplicate(1, "https://a.b/old", window) is None
    # legacy rows (no normalized_url) are looked up among recent ones only
    legacy = db.create_request(1, "https://a.b/legacy")
    force(legacy, status="done")
    assert db.find_recent_duplicate(1, "https://a.b/legacy", window)["id"] == legacy
    force(legacy, created_at=ago(2 * 3600))
    assert db.find_recent_duplicate(1, "https://a.b/legacy", window) is None


# ---------------------------------------------------------------------------
# queue claims and transitions
# ---------------------------------------------------------------------------
def test_claim_next_request_order_and_deferral(db_file):
    a = make(url="https://a.b/a")
    deferred = make(url="https://a.b/deferred", not_before=ago(-60))
    c = make(url="https://a.b/c")
    early = make(url="https://a.b/early", not_before=ago(3600))  # due long ago
    order = [db.claim_next_request()["id"] for _ in range(3)]
    assert order == [early, a, c]
    assert db.claim_next_request() is None  # the deferred one is not due
    later = db.iso_from_ts(time.time() + 120)
    claimed = db.claim_next_request(now_iso=later)
    assert claimed["id"] == deferred
    assert claimed["processing_started_at"] == later


def test_claim_sets_processing_fields_and_records_it(db_file):
    request_id = make()
    claimed = db.claim_next_request()
    assert claimed["id"] == request_id and claimed["status"] == "processing"
    assert claimed["attempts"] == 1
    assert ISO_RE.match(claimed["processing_started_at"])
    assert status_changes(request_id) == [("queued", "processing")]
    assert history(request_id) == ["processing"]
    assert db.claim_next_request() is None
    assert db.claim_request(request_id) is None  # never claimed twice


def test_claim_request_only_claims_due_queued_rows(db_file):
    future = make(url="https://a.b/f", not_before=ago(-300))
    assert db.claim_request(future) is None
    assert db.claim_request(future, now_iso=ago(-301))["id"] == future
    for status in ("done", "cancelled", "failed", "rate_limited", "pending"):
        request_id = make(url=f"https://a.b/{status}")
        force(request_id, status=status)
        assert db.claim_request(request_id) is None
        assert status_of(request_id) == status
    assert db.claim_request(999999) is None


def test_happy_path_transitions(db_file):
    request_id = processing()
    assert db.mark_sending(request_id) is True
    assert db.mark_sending(request_id) is False
    assert db.mark_done(
        request_id,
        sent_message_id=99,
        media_type="video",
        original_size=10,
        final_size=5,
        compressed=True,
    )
    row = db.get_request(request_id)
    assert row["status"] == "done" and row["sent_message_id"] == 99
    assert row["media_type"] == "video" and row["compressed"] == 1
    assert row["original_size"] == 10 and row["final_size"] == 5
    assert row["processing_duration_seconds"] >= 0
    assert ISO_RE.match(row["processing_finished_at"])
    assert status_changes(request_id) == [
        ("queued", "processing"),
        ("processing", "sending"),
        ("sending", "done"),
    ]
    assert history(request_id) == ["processing", "sending", "done"]
    assert db.mark_done(request_id) is False
    assert db.mark_failed(request_id, error_code="late") is False
    assert status_of(request_id) == "done"


def test_mark_done_from_processing_keeps_unset_fields(db_file):
    request_id = processing()
    db.set_request_fields(request_id, original_size=123, error_code="old")
    assert db.mark_done(request_id, final_size=50)
    row = db.get_request(request_id)
    assert row["original_size"] == 123 and row["final_size"] == 50
    assert row["error_code"] is None  # done clears stale errors


@pytest.mark.parametrize("status", ["queued", "processing", "sending"])
def test_mark_failed_from_active_statuses(db_file, status):
    request_id = make()
    force(request_id, status=status)
    assert db.mark_failed(request_id, error_code="timeout", error_message="slow")
    row = db.get_request(request_id)
    assert row["status"] == "failed" and row["error_code"] == "timeout"
    assert row["error_message"] == "slow"
    assert status_changes(request_id) == [(status, "failed")]
    (failed_event,) = [e for e in events("request_status") if e["status"] == "failed"]
    assert failed_event["error_code"] == "timeout"


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "rate_limited"])
def test_mark_failed_refuses_terminal_rows(db_file, status):
    request_id = make()
    force(request_id, status=status)
    assert db.mark_failed(request_id, error_code="x") is False
    assert status_of(request_id) == status
    assert status_changes(request_id) == []


def test_mark_failed_unknown_id(db_file):
    assert db.mark_failed(424242, error_code="x") is False


def test_defer_request_moves_back_to_the_queue(db_file):
    request_id = processing()
    until = db.iso_from_ts(time.time() + 600)
    assert db.defer_request(request_id, until, reason="flood wait")
    row = db.get_request(request_id)
    assert row["status"] == "queued" and row["not_before"] == until
    assert db.claim_next_request() is None
    reclaimed = db.claim_next_request(now_iso=db.iso_from_ts(time.time() + 601))
    assert reclaimed["id"] == request_id and reclaimed["attempts"] == 2
    assert history(request_id) == ["processing", "deferred", "processing"]
    assert db.defer_request(request_id, until) is True  # processing again
    force(request_id, status="done")
    assert db.defer_request(request_id, until) is False


def test_set_request_fields_is_whitelisted(db_file):
    request_id = make()
    db.set_request_fields(request_id, compressed=False, provider="Reddit")
    row = db.get_request(request_id)
    assert row["compressed"] == 0 and row["provider"] == "Reddit"
    (updated,) = events("request_updated")
    assert updated["request_id"] == request_id
    assert updated["provider"] == "Reddit" and updated["chat_id"] == 1
    for bad in ({"status": "done"}, {"attempts": 0}, {"id": 2}, {"url": "x"}):
        with pytest.raises(ValueError):
            db.set_request_fields(request_id, **bad)
    db.set_request_fields(request_id)  # nothing to do
    db.set_request_fields(31337, final_size=1)  # unknown id: no event, no error
    assert len(events("request_updated")) == 1


def test_recover_interrupted_with_rehydrate(db_file):
    queued = make(url="https://a.b/q")
    first_try = processing(url="https://a.b/p1")
    exhausted = processing(url="https://a.b/p3")
    force(exhausted, attempts=3)
    sending = processing(url="https://a.b/s")
    db.mark_sending(sending)
    done = processing(url="https://a.b/d")
    db.mark_done(done)
    counts = db.recover_interrupted(rehydrate=True, max_attempts=3)
    assert counts == {"requeued": 1, "failed": 1, "failed_sending": 1, "aborted": 0}
    assert status_of(queued) == "queued"
    assert status_of(first_try) == "queued"
    assert db.get_request(exhausted)["error_code"] == "interrupted"
    assert status_of(exhausted) == "failed"
    assert db.get_request(sending)["error_code"] == "interrupted_while_sending"
    assert status_of(done) == "done"
    # the requeued one is claimable again and keeps counting attempts
    claimed_ids = {db.claim_next_request()["id"] for _ in range(2)}
    assert claimed_ids == {queued, first_try}
    assert db.get_request(first_try)["attempts"] == 2


def test_recover_interrupted_without_rehydrate_aborts_queued_work(db_file):
    queued = make(url="https://a.b/q")
    deferred = make(url="https://a.b/def", not_before=ago(-600))
    interrupted = processing(url="https://a.b/p")
    counts = db.recover_interrupted(rehydrate=False, max_attempts=3)
    assert counts == {"requeued": 0, "failed": 0, "failed_sending": 0, "aborted": 3}
    assert {status_of(i) for i in (queued, deferred, interrupted)} == {"aborted"}
    assert db.claim_next_request(now_iso=ago(-3600)) is None


def test_cancel_queued_is_one_quick_transaction_with_events(db_file):
    chat1 = [make(1, f"https://a.b/1/{i}") for i in range(30)]
    chat1.append(make(1, "https://a.b/1/deferred", not_before=ago(-600)))
    chat2 = [make(2, f"https://a.b/2/{i}") for i in range(3)]
    running = processing(1, "https://a.b/1/running")
    started = time.monotonic()
    cancelled = db.cancel_queued(chat_id=1, actor="admin")
    assert time.monotonic() - started < 2
    assert cancelled == chat1
    assert {status_of(i) for i in chat1} == {"cancelled"}
    assert {status_of(i) for i in chat2} == {"queued"}
    assert status_of(running) == "processing"
    assert history(chat1[0]) == ["cancelled"]
    assert (
        len([e for e in events("request_status") if e["status"] == "cancelled"]) == 31
    )
    (cleared,) = events("queue_cleared")
    assert cleared["count"] == 31 and cleared["chat_id"] == 1
    assert cleared["actor"] == "admin" and cleared["ids"] == chat1
    assert db.cancel_queued() == chat2
    assert db.cancel_queued() == []


def test_cancelled_rows_are_never_sent(db_file):
    """F50: a cancelled row can be neither claimed nor claimed for sending."""
    request_id = make(url="https://a.b/c")
    assert db.cancel_queued() == [request_id]
    assert db.claim_next_request() is None
    assert db.claim_request(request_id) is None
    assert db.mark_sending(request_id) is False
    assert status_of(request_id) == "cancelled"


def test_requeue_requests_from_every_requeueable_status(db_file):
    ids = {}
    for status in db.REQUEUEABLE_STATUSES:
        request_id = make(url=f"https://a.b/{status}")
        force(
            request_id,
            status=str(status),
            attempts=3,
            error_code="boom",
            not_before=ago(-999),
        )
        ids[str(status)] = request_id
    blocked = {}
    for status in ("queued", "processing", "sending", "done"):
        request_id = make(url=f"https://a.b/keep/{status}")
        force(request_id, status=status)
        blocked[status] = request_id
    requeued = db.requeue_requests([*ids.values(), *blocked.values(), 999], actor="op")
    assert requeued == list(ids.values())
    for request_id in ids.values():
        row = db.get_request(request_id)
        assert row["status"] == "queued" and row["not_before"] is None
        assert row["attempts"] == 0 and row["error_code"] is None
        assert history(request_id) == ["requeued"]
    for status, request_id in blocked.items():
        assert status_of(request_id) == status


def test_requeue_never_queues_the_same_link_twice(db_file):
    first = db.create_request(1, "https://a.b/x", status="rate_limited")
    second = db.create_request(1, "https://a.b/x", status="rate_limited")
    other_chat = db.create_request(2, "https://a.b/x", status="rate_limited")
    assert db.requeue_requests([first, second, other_chat]) == [first, other_chat]
    assert status_of(second) == "rate_limited"
    assert db.requeue_requests([second]) == []  # first is still active
    db.claim_request(first)
    db.mark_done(first)
    assert db.requeue_requests([second]) == [second]


def test_list_and_count_requests_with_filters(db_file):
    a = db.create_request(-100555, "https://A.com/100%_off", description="Promo")
    b = db.create_request(7, "https://a.com/100x-off")
    c = db.create_request(7, "https://b.org/v", description="video PROMO")
    force(c, status="done")
    assert [row["id"] for row in db.list_requests()] == [c, b, a]
    assert [row["id"] for row in db.list_requests(2, 1)] == [b, a]  # positional
    assert [row["id"] for row in db.list_requests(status="queued")] == [b, a]
    assert [row["id"] for row in db.list_requests(status=["done"])] == [c]
    assert db.list_requests(status=[]) == []
    assert [row["id"] for row in db.list_requests(chat_id=7)] == [c, b]
    assert [row["id"] for row in db.list_requests(q="100%_")] == [a]  # escaped
    assert [row["id"] for row in db.list_requests(q="a.COM")] == [b, a]
    assert [row["id"] for row in db.list_requests(q="promo")] == [c, a]
    assert [row["id"] for row in db.list_requests(q="-1005")] == [a]
    assert db.list_requests(q="nothing-here") == []
    assert db.count_requests() == 3
    assert db.count_requests(status="queued", q="a.com") == 2
    assert db.count_requests(chat_id=7, q="promo") == 1
    assert db.list_requests(limit=-1) == []
    assert len(db.list_requests(limit=10**9, offset=-5)) == 3
    assert db._page(10**9, -5) == (db.MAX_PAGE_SIZE, 0)


def test_count_active_for_chat(db_file):
    for status in ("queued", "processing", "sending", "done", "failed", "duplicate"):
        force(make(1, f"https://a.b/{status}"), status=status)
    make(2, "https://a.b/other")
    assert db.count_active_for_chat(1) == 3
    assert db.count_active_for_chat(2) == 1
    assert db.count_active_for_chat(3) == 0


def test_accepted_times(db_file):
    now = time.time()
    rows = {}
    for status in ("queued", "processing", "sending", "done", "failed"):
        rows[status] = make(1, f"https://a.b/{status}")
    for status in ("rate_limited", "duplicate", "cancelled", "aborted"):
        rows[status] = make(1, f"https://a.b/{status}")
        force(rows[status], status=status)
    force(rows["done"], status="done", created_at=db.iso_from_ts(now - 100))
    force(rows["failed"], status="failed", created_at=db.iso_from_ts(now - 50))
    force(rows["processing"], status="processing")
    force(rows["sending"], status="sending", created_at=db.iso_from_ts(now - 5000))
    deferred = make(1, "https://a.b/deferred", not_before=db.iso_from_ts(now + 30))
    make(2, "https://a.b/other-chat")
    times = db.accepted_times(1, db.iso_from_ts(now - 3600))
    assert times == sorted(times)
    assert len(times) == 5  # done, failed, queued, processing, deferred
    assert times[0] == pytest.approx(now - 100, abs=1e-3)
    assert times[1] == pytest.approx(now - 50, abs=1e-3)
    assert times[-1] == pytest.approx(now + 30, abs=1e-3)
    assert db.get_request(deferred)["not_before"] == db.iso_from_ts(now + 30)
    assert len(db.accepted_times(1, db.iso_from_ts(now - 6000))) == 6


def test_accepted_times_honours_reset_at(db_file):
    old = make(1, "https://a.b/old")
    force(old, status="done", created_at=ago(30))
    assert len(db.accepted_times(1, ago(3600))) == 1
    db.reset_chat_limit(1, actor="admin")
    assert db.accepted_times(1, ago(3600)) == []
    make(1, "https://a.b/new")
    assert len(db.accepted_times(1, ago(3600))) == 1


def test_retry_deferral_keeps_the_original_rate_limit_slot(db_file):
    request_id = make(1, "https://a.b/retry")
    submitted = db.ts_from_iso(db.get_request(request_id)["created_at"])
    assert db.claim_request(request_id)
    first_retry = time.time() + 30
    db.defer_request(request_id, db.iso_from_ts(first_retry), reason="timeout")
    assert db.accepted_times(1, ago(60)) == [pytest.approx(submitted, abs=1)]
    # claimed again at the retry time, then a long flood wait, then done
    assert db.claim_request(request_id, now_iso=db.iso_from_ts(first_retry + 1))
    second_retry = first_retry + 1800
    db.defer_request(request_id, db.iso_from_ts(second_retry), reason="429")
    assert db.accepted_times(1, ago(60)) == [pytest.approx(submitted, abs=1)]
    assert db.claim_request(request_id, now_iso=db.iso_from_ts(second_retry + 1))
    assert db.accepted_times(1, ago(60)) == [pytest.approx(submitted, abs=1)]
    assert db.mark_done(request_id)
    assert db.accepted_times(1, ago(60)) == [pytest.approx(submitted, abs=1)]


def test_queue_snapshot(db_file):
    due = make(1, "https://a.b/due")
    later = make(1, "https://a.b/later", not_before=ago(-120))
    running = processing(2, "https://a.b/run")
    sending = processing(2, "https://a.b/send")
    db.mark_sending(sending)
    force(make(3, "https://a.b/done"), status="done")
    snapshot = db.queue_snapshot()
    assert [row["id"] for row in snapshot["queued"]] == [due]
    assert [row["id"] for row in snapshot["deferred"]] == [later]
    assert [row["id"] for row in snapshot["running"]] == [running, sending]
    assert snapshot["counts"] == {
        "queued": 1,
        "deferred": 1,
        "processing": 1,
        "sending": 1,
        "running": 2,
    }
    assert ISO_RE.match(snapshot["generated_at"])
    empty = db.queue_snapshot(limit=0)
    assert empty["queued"] == [] and empty["counts"]["running"] == 2


# ---------------------------------------------------------------------------
# request events and the event log
# ---------------------------------------------------------------------------
def test_request_events_newest_first(db_file):
    request_id = make()
    for index in range(5):
        db.add_request_event(request_id, f"step{index}", details=f"d{index}")
    db.add_request_event(request_id, "json", details={"a": 1}, duration_seconds=1.5)
    items = db.get_request_events(request_id, limit=3)
    assert [item["event_type"] for item in items] == ["json", "step4", "step3"]
    assert items[0]["details"] == '{"a":1}' and items[0]["duration_seconds"] == 1.5
    assert isinstance(items[0], dict)
    assert ISO_RE.match(items[0]["created_at"])
    assert [i["event_type"] for i in db.get_request_events(request_id, 2, 4)] == [
        "step1",
        "step0",
    ]
    logged = events("request_event")
    assert len(logged) == 6
    assert logged[-1]["request_id"] == request_id and logged[-1]["chat_id"] == 1
    assert logged[-1]["event_type"] == "json"
    assert logged[-1]["request_event_id"] == items[0]["id"]
    assert json.loads(json.dumps(items))  # plain JSON-serializable dicts


def test_request_events_bulk(db_file):
    ids = [make(url=f"https://a.b/{i}") for i in range(3)]
    for request_id in ids[:2]:
        for index in range(4):
            db.add_request_event(request_id, f"e{index}")
    bulk = db.get_request_events_bulk([*ids, ids[0], *range(10_000, 10_600)])
    assert set(bulk) == {*ids, *range(10_000, 10_600)}
    assert [e["event_type"] for e in bulk[ids[0]]] == ["e3", "e2", "e1", "e0"]
    assert bulk[ids[2]] == [] and bulk[10_000] == []
    capped = db.get_request_events_bulk(ids, limit_per_request=2)
    assert [e["event_type"] for e in capped[ids[1]]] == ["e3", "e2"]
    assert db.get_request_events_bulk([]) == {}


def test_event_log_round_trip(db_file):
    assert db.latest_event_id() == 0
    first = db.add_event("custom", a=1, event_type="payload-key", nested={"k": [1]})
    second = db.add_event("other", id=999, created_at="spoofed")
    assert second > first and db.latest_event_id() == second
    items = db.list_events_after(0)
    assert items[0] == {
        "id": first,
        "created_at": items[0]["created_at"],
        "type": "custom",
        "a": 1,
        "event_type": "payload-key",
        "nested": {"k": [1]},
    }
    assert ISO_RE.match(items[0]["created_at"])
    assert items[1]["id"] == second  # row metadata wins over payload keys
    assert items[1]["created_at"] != "spoofed"
    assert db.list_events_after(first) == [items[1]]
    assert db.list_events_after(second) == []
    assert len(db.list_events_after(0, limit=1)) == 1
    with pytest.raises(ValueError):
        db.add_event("")


def test_transition_events_carry_the_request_id(db_file):
    request_id = processing(9, "https://a.b/e")
    last = db.latest_event_id()
    db.mark_sending(request_id)
    (event,) = db.list_events_after(last)
    assert event["type"] == "request_status"
    assert event["id"] > last  # the event id, usable to keep tailing
    assert event["request_id"] == request_id and event["chat_id"] == 9
    assert event["status"] == "sending" and event["previous"] == "processing"


def test_legacy_event_log_rows_are_decoded(db_file):
    legacy_rows = [
        "not json at all",
        '{"update_id": 5, "message": {"text": "hi"}}',  # raw Telegram update
        '{"type": "db_cleared"}',
    ]
    with db.write() as conn:
        conn.executemany(
            "INSERT INTO updates (raw, created_at) VALUES (?, ?)",
            [(raw, db.now_iso()) for raw in legacy_rows],
        )
    decoded = db.list_events_after(0)
    assert [item["type"] for item in decoded] == [
        "update_created",
        "update_created",
        "db_cleared",
    ]
    assert [item["raw"] for item in decoded[:2]] == legacy_rows[:2]
    assert "raw" not in decoded[2]
    assert all(ISO_RE.match(item["created_at"]) for item in decoded)
    assert db.latest_event_id() == decoded[-1]["id"]


# ---------------------------------------------------------------------------
# chat limits and notices
# ---------------------------------------------------------------------------
def test_chat_limit_lifecycle(db_file):
    assert db.get_chat_limit(5) is None
    until = db.iso_from_ts(time.time() + 300)
    db.set_chat_blocked_until(5, until)
    db.set_chat_blocked_until(6, ago(10))  # already expired
    limit = db.get_chat_limit(5)
    assert limit["blocked_until"] == until and limit["reset_at"] is None
    blocked = db.list_chat_limits(only_blocked=True)
    assert [row["chat_id"] for row in blocked] == [5]
    assert blocked[0]["blocked"] is True
    assert 290 < blocked[0]["remaining_seconds"] <= 300
    everything = db.list_chat_limits()
    assert [row["chat_id"] for row in everything] == [5, 6]
    assert everything[1]["blocked"] is False and everything[1]["remaining_seconds"] == 0
    assert db.list_chat_limits(only_blocked=True, now_iso=ago(-400)) == []
    changed = [(e["chat_id"], e["blocked_until"]) for e in events("rate_limit_changed")]
    assert changed[:2] == [(5, until), (6, db.get_chat_limit(6)["blocked_until"])]

    db.reset_chat_limit(5, actor="admin")
    limit = db.get_chat_limit(5)
    assert limit["blocked_until"] is None and ISO_RE.match(limit["reset_at"])
    (unlimited,) = events("chat_unlimited")
    assert unlimited["chat_id"] == 5 and unlimited["actor"] == "admin"
    db.set_chat_blocked_until(5, None)
    assert db.get_chat_limit(5)["blocked_until"] is None


def test_reset_all_chat_limits(db_file):
    make(10, "https://a.b/1")
    force(make(11, "https://a.b/2"), status="done")
    db.create_request(12, "https://a.b/3", status="duplicate")  # refused only
    db.set_chat_blocked_until(13, ago(-60))
    assert db.reset_all_chat_limits(actor="admin") == 3
    for chat_id in (10, 11, 13):
        limit = db.get_chat_limit(chat_id)
        assert limit["reset_at"] is not None and limit["blocked_until"] is None
    assert db.get_chat_limit(12) is None
    (event,) = events("chat_unlimited_all")
    assert event["count"] == 3 and event["actor"] == "admin"
    assert db.accepted_times(10, ago(60)) == []


def test_should_notify_throttles_per_kind(db_file):
    assert db.should_notify(1, "rate", 60) is True
    assert db.should_notify(1, "rate", 60) is False
    assert db.should_notify(1, "duplicate", 60) is True
    assert db.should_notify(2, "rate", 60) is True
    assert db.should_notify(1, "rate", 0) is True
    with db.write() as conn:
        conn.execute(
            "UPDATE chat_limits SET last_rate_notice_at = ? WHERE chat_id = 1",
            (ago(61),),
        )
    assert db.should_notify(1, "rate", 60) is True
    assert ISO_RE.match(db.get_chat_limit(1)["last_rate_notice_at"])
    with pytest.raises(ValueError):
        db.should_notify(1, "spam", 60)


def test_should_notify_is_atomic_across_threads(db_file):
    results = []
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        results.append(db.should_notify(77, "duplicate", 600))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 7 + [True]


# ---------------------------------------------------------------------------
# worker status
# ---------------------------------------------------------------------------
def test_worker_status(db_file):
    assert db.get_worker_status() is None
    db.set_worker_status({"pid": 1, "running": [{"request_id": 3}], "age_seconds": -1})
    status = db.get_worker_status()
    assert status["pid"] == 1 and status["running"] == [{"request_id": 3}]
    assert ISO_RE.match(status["heartbeat_at"])
    assert 0 <= status["age_seconds"] < 5
    db.set_worker_status({"pid": 2})
    status = db.get_worker_status()
    assert status["pid"] == 2 and "running" not in status
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM worker_status").fetchone()[0] == 1
        conn.execute("UPDATE worker_status SET heartbeat_at = ?", (ago(120),))
    assert db.get_worker_status()["age_seconds"] == pytest.approx(120, abs=5)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
def test_users_unique_casefolded_email_and_validated_roles(db_file):
    admin = db.add_user(username="luca", email=" Luca@Example.COM ", role="admin")
    with pytest.raises(ValueError, match="already exists"):
        db.add_user(username="dup", email="luca@example.com")
    with pytest.raises(ValueError, match="invalid role"):
        db.add_user(email="x@example.com", role="superadmin")
    with pytest.raises(ValueError):
        db.add_user(email="y@example.com", role="Admin")
    nobody1 = db.add_user(username="svc1")
    nobody2 = db.add_user(username="svc2", email="   ")  # no email twice is fine
    user = db.get_user_by_email("LUCA@example.com")
    assert user["id"] == admin and user["email"] == "Luca@Example.COM"
    assert user["role"] == "admin"
    assert isinstance(user, dict) and "email_norm" not in user
    assert db.get_user_by_email("nobody@example.com") is None
    assert db.get_user_by_email(None) is None and db.get_user_by_email("") is None
    assert [u["id"] for u in db.list_users()] == [nobody2, nobody1, admin]
    assert db.list_users(1, 1)[0]["id"] == nobody1

    assert db.set_user_role(admin, "user") is True
    assert db.get_user_by_email("luca@example.com")["role"] == "user"
    with pytest.raises(ValueError):
        db.set_user_role(admin, "root")
    assert db.set_user_role(424242, "admin") is False
    assert db.delete_user(admin) is True
    assert db.delete_user(admin) is False
    assert db.get_user_by_email("luca@example.com") is None
    assert db.add_user(email="LUCA@example.com", role="admin") > admin


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------
def test_prune_respects_retention_and_active_rows(db_file):
    days = 86400
    old_done = make(1, "https://a.b/old-done")
    force(old_done, status="done", created_at=ago(40 * days))
    db.add_request_event(old_done, "compress")
    old_refused = db.create_request(1, "https://a.b/refused", status="rate_limited")
    force(old_refused, created_at=ago(40 * days))
    old_queued = make(1, "https://a.b/old-queued")
    force(old_queued, created_at=ago(40 * days))
    recent_done = make(1, "https://a.b/recent")
    force(recent_done, status="done", created_at=ago(5 * days))
    in_window = make(1, "https://a.b/in-window")
    force(in_window, status="failed", created_at=ago(20 * days))
    with db.write() as conn:
        conn.execute("UPDATE updates SET created_at = ?", (ago(30 * days),))
        conn.execute(
            "INSERT INTO processed_messages (chat_id, message_id, created_at)"
            " VALUES (1, 1, ?), (1, 2, ?)",
            (ago(3 * days), ago(60)),
        )
        conn.execute(
            "INSERT INTO request_events (request_id, event_type, created_at)"
            " VALUES (987654, 'orphan', ?)",
            (ago(0),),
        )
    fresh_event = db.add_event("fresh")

    counts = db.prune(
        updates_days=14, requests_days=10, keep_accepted_seconds=25 * days
    )
    assert counts["requests"] == 2  # old_done + old_refused
    assert counts["request_events"] == 2  # old_done's event + the orphan
    assert counts["processed_messages"] == 1
    assert counts["updates"] > 0
    assert db.get_request(old_done) is None and db.get_request(old_refused) is None
    for kept in (old_queued, recent_done, in_window):
        assert db.get_request(kept) is not None
    remaining = db.list_events_after(0)
    assert remaining[0]["id"] == fresh_event  # older event rows are gone
    assert remaining[-1]["type"] == "history_pruned"
    assert processed_message_ids(1) == [2]  # only the recent legacy row is kept

    assert db.prune(updates_days=0, requests_days=0)["requests"] == 0
    assert db.get_request(in_window) is not None


def test_prune_protects_rows_inside_the_rate_limit_window(db_file, monkeypatch):
    monkeypatch.setattr(db.config, "RATE_LIMITS", ((20, 30 * 86400),), raising=False)
    request_id = make(1, "https://a.b/x")
    force(request_id, status="done", created_at=ago(20 * 86400))
    legacy_null = make(1, "https://a.b/null-status")
    force(legacy_null, status=None, created_at=ago(20 * 86400))
    assert db.prune(updates_days=0, requests_days=7)["requests"] == 1  # NULL status
    assert db.get_request(legacy_null) is None
    assert db.get_request(request_id) is not None
    monkeypatch.setattr(db.config, "RATE_LIMITS", (), raising=False)
    assert db.prune(updates_days=0, requests_days=7)["requests"] == 1


def test_prune_deletes_in_batches(db_file, monkeypatch):
    monkeypatch.setattr(db, "_DELETE_BATCH", 7)
    with db.write() as conn:
        conn.executemany(
            "INSERT INTO updates (raw, created_at) VALUES ('x', ?)",
            [(ago(100 * 86400),)] * 30,
        )
    assert db.prune(updates_days=1, requests_days=0)["updates"] == 30


def test_clear_history_keeps_active_state(db_file):
    queued = make(1, "https://a.b/q")
    running = processing(1, "https://a.b/p")
    db.add_request_event(running, "download")
    done = processing(2, "https://a.b/d")
    db.mark_done(done)
    db.add_request_event(done, "compress")
    refused = db.create_request(2, "https://a.b/r", status="duplicate")
    legacy = db.create_request(3, "https://a.b/l", status="pending")  # legacy status
    db.add_user(email="admin@example.com", role="admin")
    db.set_chat_blocked_until(1, ago(-60))
    db.set_worker_status({"pid": 1})
    with db.write() as conn:
        conn.execute(
            "INSERT INTO processed_messages (chat_id, message_id, created_at)"
            " VALUES (1, 1, ?)",
            (ago(0),),
        )
    last_id = db.latest_event_id()

    counts = db.clear_history(actor="admin")
    assert counts["requests"] == 3  # done, refused, legacy pending
    assert counts["updates"] > 0
    for gone in (done, refused, legacy):
        assert db.get_request(gone) is None
    assert status_of(queued) == "queued" and status_of(running) == "processing"
    assert "download" in history(running)
    assert db.get_request_events(done) == []
    (cleared,) = db.list_events_after(0)
    assert cleared["type"] == "history_cleared" and cleared["actor"] == "admin"
    assert cleared["id"] > last_id  # event ids keep increasing
    assert db.get_user_by_email("admin@example.com")["role"] == "admin"
    assert db.get_chat_limit(1)["blocked_until"] is not None
    assert db.get_worker_status()["pid"] == 1
    assert processed_message_ids(1) == [1]
    # in-flight work can still finish normally
    assert db.mark_sending(running) and db.mark_done(running)
    assert db.create_request(2, "https://a.b/new") > legacy


# ---------------------------------------------------------------------------
# atomicity and concurrency
# ---------------------------------------------------------------------------
def test_a_failing_event_insert_rolls_the_transition_back(db_file):
    request_id = processing()
    before_events = len(events())
    with db.write() as conn:
        conn.execute(
            "CREATE TRIGGER broken_log BEFORE INSERT ON updates"
            " BEGIN SELECT RAISE(ABORT, 'event log broken'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="event log broken"):
        db.mark_sending(request_id)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_request(1, "https://a.b/never")
    with pytest.raises(sqlite3.IntegrityError):
        db.cancel_queued()
    assert status_of(request_id) == "processing"
    assert db.count_requests() == 1
    assert len(events()) == before_events
    with db.write() as conn:
        conn.execute("DROP TRIGGER broken_log")
    assert db.mark_sending(request_id) is True


def test_concurrent_claims_never_return_the_same_row(db_file):
    ids = [make(chat_id=i % 7, url=f"https://a.b/{i}") for i in range(120)]
    claimed = []
    errors = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            while (row := db.claim_next_request()) is not None:
                with lock:
                    claimed.append(row["id"])
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(claimed) == len(set(claimed)) == len(ids)
    assert sorted(claimed) == ids
    assert {db.get_request(i)["attempts"] for i in ids} == {1}
    assert len(events("request_status")) == len(ids)


def test_writers_wait_for_the_busy_timeout_then_raise(db_file, fast_busy):
    request_id = processing()
    holder = raw_conn(db_file)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.mark_failed(request_id, error_code="x")
        waited = time.monotonic() - started
        assert 0.25 <= waited < 5
        # readers are never blocked by a writer (WAL)
        assert status_of(request_id) == "processing"
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert status_of(request_id) == "processing"  # nothing half-written
    assert db.mark_failed(request_id, error_code="x") is True


def test_sending_claim_raises_on_lock_instead_of_returning_false(db_file, fast_busy):
    """F47: a DB error is never mistaken for 'already sent'."""
    request_id = processing(1, "https://a.b/l")
    holder = raw_conn(db_file)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.mark_sending(request_id)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.create_request(1, "https://a.b/other")
        # reads keep working while another process writes (WAL)
        found = db.find_recent_duplicate(1, "https://a.b/l", ago(60))
        assert found["status"] == "processing"
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert status_of(request_id) == "processing"
    assert db.mark_sending(request_id) is True


def test_waiting_writer_succeeds_when_the_lock_is_released(db_file):
    holder = raw_conn(db_file)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO updates (raw, created_at) VALUES ('held', ?)", (ago(0),)
    )
    released = threading.Event()

    def release_later():
        time.sleep(0.5)
        holder.execute("COMMIT")
        released.set()

    thread = threading.Thread(target=release_later)
    started = time.monotonic()
    thread.start()
    try:
        request_id = make(url="https://a.b/waited")  # waits, then writes
    finally:
        thread.join()
        holder.close()
    assert released.is_set()
    assert time.monotonic() - started >= 0.4
    assert db.get_request(request_id) is not None
    assert events()[0]["raw"] == "held"  # committed before the waiting writer


def test_rows_are_plain_dicts(db_file):
    db.add_user(username="u", email="u@example.com")
    request_id = make()
    db.add_request_event(request_id, "step")
    user = db.get_user_by_email("u@example.com")
    assert user == {
        "id": user["id"],
        "username": "u",
        "email": "u@example.com",
        "role": "user",
        "created_at": user["created_at"],
    }
    assert list(user) == ["id", "username", "email", "role", "created_at"]
    rows = [
        user,
        *db.list_users(),
        db.get_request(request_id),
        *db.get_request_events(request_id),
        *db.get_request_events_bulk([request_id])[request_id],
        *db.list_requests(),
    ]
    assert all(type(row) is dict for row in rows)
    assert json.loads(json.dumps(rows))[0]["email"] == "u@example.com"
    with pytest.raises(KeyError):
        user[0]  # no positional access: rows are keyed by column name
    with pytest.raises(KeyError):
        user["missing"]
