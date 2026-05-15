import datetime
import json
import os
import sqlite3
import threading


def _db_path() -> str:
    """Compute the DB path at runtime from the environment.

    Reading the env var on every call makes the code resilient to tests
    or CI that set `REQUESTS_DB` dynamically after import.
    """
    return os.environ.get(
        "REQUESTS_DB", os.path.join(os.getcwd(), "data", "requests.db")
    )


# Cached in-memory fallback connection (created lazily). If the on-disk DB
# cannot be opened we create a single shared in-memory connection so tests
# and CI see a coherent DB across calls.
_cached_memory_conn = None
_cached_memory_lock = threading.Lock()


class _NoCloseConn:
    """Proxy around a sqlite3.Connection whose .close() is a no-op so the
    shared cached connection isn't destroyed by individual callers.

    All other attributes and methods are delegated to the underlying
    connection object.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        # no-op: keep the shared connection alive
        return None


def _init_db_conn(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            url TEXT,
            status TEXT,
            created_at TEXT
        )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw TEXT,
            created_at TEXT
        )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            email TEXT,
            role TEXT,
            created_at TEXT
        )""")
    # processed messages dedup table
    cur.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            created_at TEXT
        )""")
    try:
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_messages_chat_message ON processed_messages(chat_id, message_id)"
        )
    except Exception:
        pass

    # ensure requests has optional telemetry/stat columns
    cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
    extra_cols = {
        "original_message_id": "INTEGER",
        "original_size": "INTEGER",
        "final_size": "INTEGER",
        "compressed": "INTEGER",
        "processing_started_at": "TEXT",
        "processing_finished_at": "TEXT",
        "processing_duration_seconds": "REAL",
    }
    for c, t in extra_cols.items():
        if c not in cols:
            try:
                conn.execute(f"ALTER TABLE requests ADD COLUMN {c} {t}")
            except Exception:
                pass

    # per-request event log (compression/redownload durations, etc.)
    cur.execute("""CREATE TABLE IF NOT EXISTS request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            event_type TEXT,
            details TEXT,
            duration_seconds REAL,
            created_at TEXT
        )""")
    try:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_events_request_id ON request_events(request_id)"
        )
    except Exception:
        pass

    # Migration: ensure request_events has the expected columns for older DBs
    try:
        existing = [r[1] for r in conn.execute("PRAGMA table_info(request_events)")]
        needed = {
            "event_type": "TEXT",
            "details": "TEXT",
            "duration_seconds": "REAL",
        }
        for col, col_type in needed.items():
            if col not in existing:
                try:
                    conn.execute(
                        f"ALTER TABLE request_events ADD COLUMN {col} {col_type}"
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # ensure requests has a description column for future use
    cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
    if "description" not in cols:
        try:
            conn.execute("ALTER TABLE requests ADD COLUMN description TEXT")
        except Exception:
            pass
    conn.commit()


def init_db():
    dbpath = _db_path()
    dirpath = os.path.dirname(dbpath)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    conn = sqlite3.connect(dbpath)
    _init_db_conn(conn)
    conn.close()


def _connect():
    """Return a sqlite3.Connection to the configured DB.

    If the DB cannot be opened, attempt to initialize it (create directory
    and schema). If that still fails, fall back to an in-memory DB with the
    required schema so callers don't crash in CI/test environments.
    """
    dbpath = _db_path()
    try:
        # Ensure parent directory exists before connecting.
        dirpath = os.path.dirname(dbpath)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)

        conn = sqlite3.connect(dbpath)
        # If the database file is empty or new, ensure schema is present so
        # callers can rely on tables existing.
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='requests'"
            )
            if not cur.fetchone():
                _init_db_conn(conn)
        except Exception:
            pass
        return conn
    except Exception:
        # try to initialize DB path and retry (covers races/permissions)
        try:
            init_db()
            conn = sqlite3.connect(dbpath)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='requests'"
                )
                if not cur.fetchone():
                    _init_db_conn(conn)
            except Exception:
                pass
            return conn
        except Exception:
            # fallback to a cached in-memory DB so multiple calls operate on
            # the same database instance during the process lifetime.
            global _cached_memory_conn
            with _cached_memory_lock:
                if _cached_memory_conn is None:
                    _cached_memory_conn = sqlite3.connect(
                        ":memory:", check_same_thread=False
                    )
                    _init_db_conn(_cached_memory_conn)
            return _NoCloseConn(_cached_memory_conn)


def add_request(
    chat_id: int,
    url: str,
    status: str = "pending",
    description: str = None,
    original_message_id: int = None,
) -> int:
    conn = _connect()
    cur = conn.cursor()
    # Deduplicate when original_message_id is provided: return existing request if present
    if original_message_id is not None:
        try:
            cur.execute(
                "SELECT id FROM requests WHERE chat_id = ? AND original_message_id = ? AND url = ? LIMIT 1",
                (chat_id, original_message_id, url),
            )
            r = cur.fetchone()
            if r:
                conn.close()
                return r[0]
        except Exception:
            pass

    cur.execute(
        "INSERT INTO requests (chat_id, url, status, created_at, description, original_message_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            url,
            status,
            datetime.datetime.utcnow().isoformat(),
            description,
            original_message_id,
        ),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    # persist an updates row so other processes (GUI) watching the DB
    # can detect the new request and broadcast it to connected clients.
    try:
        add_update(
            json.dumps(
                {
                    "type": "request_created",
                    "id": rowid,
                    "chat_id": chat_id,
                    "url": url,
                    "status": status,
                }
            )
        )
    except Exception:
        pass
    # Publish event to websocket broadcaster if available
    try:
        # import here to avoid circular imports at module load
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            # schedule broadcast in the app event loop
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {
                        "type": "request_created",
                        "id": rowid,
                        "chat_id": chat_id,
                        "url": url,
                        "status": status,
                    }
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass
    return rowid


def list_requests(limit: int = 100, offset: int = 0):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, chat_id, url, status, created_at, description,
                   original_message_id, original_size, final_size, compressed,
                   processing_started_at, processing_finished_at, processing_duration_seconds
                   FROM requests ORDER BY id DESC LIMIT ? OFFSET ?""",
        (limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def count_requests() -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM requests")
    r = cur.fetchone()[0]
    conn.close()
    return r


def update_request_status(request_id: int, status: str):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
    conn.commit()
    conn.close()
    # persist a lightweight update record so other processes (e.g. GUI)
    # watching the DB can detect and re-broadcast this change.
    try:
        add_update(
            json.dumps({"type": "request_status", "id": request_id, "status": status})
        )
    except Exception:
        pass
    # Broadcast status change to connected GUI clients if websocket loop present
    try:
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {"type": "request_status", "id": request_id, "status": status}
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass


def mark_request_started(request_id: int):
    now = datetime.datetime.utcnow().isoformat()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE requests SET processing_started_at = ?, status = ? WHERE id = ?",
        (now, "processing", request_id),
    )
    conn.commit()
    conn.close()
    try:
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {"type": "request_started", "id": request_id, "started_at": now}
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass


def mark_request_finished(
    request_id: int, final_size: int = None, compressed: bool = None
):
    now = datetime.datetime.utcnow().isoformat()
    conn = _connect()
    cur = conn.cursor()
    # compute duration if started_at present
    cur.execute(
        "SELECT processing_started_at FROM requests WHERE id = ?", (request_id,)
    )
    row = cur.fetchone()
    duration = None
    if row and row[0]:
        try:
            started = datetime.datetime.fromisoformat(row[0])
            finished = datetime.datetime.fromisoformat(now)
            duration = (finished - started).total_seconds()
        except Exception:
            duration = None

    cur.execute(
        "UPDATE requests SET processing_finished_at = ?, processing_duration_seconds = ?, final_size = ?, compressed = ? WHERE id = ?",
        (
            now,
            duration,
            final_size,
            1 if compressed else 0 if compressed is not None else None,
            request_id,
        ),
    )
    conn.commit()
    conn.close()
    try:
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {
                        "type": "request_finished",
                        "id": request_id,
                        "finished_at": now,
                        "duration": duration,
                    }
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass


def set_request_original_size(request_id: int, original_size: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE requests SET original_size = ? WHERE id = ?",
        (original_size, request_id),
    )
    conn.commit()
    conn.close()
    # Broadcast update so GUIs can refresh size column
    try:
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {
                        "type": "request_updated",
                        "id": request_id,
                        "original_size": original_size,
                    }
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass


def add_request_event(
    request_id: int,
    event_type: str,
    details: str = None,
    duration_seconds: float = None,
) -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO request_events (request_id, event_type, details, duration_seconds, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            request_id,
            event_type,
            details,
            duration_seconds,
            datetime.datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    try:
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {
                        "type": "request_event",
                        "id": rowid,
                        "request_id": request_id,
                        "event_type": event_type,
                        "duration_seconds": duration_seconds,
                    }
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass
    # persist to updates so cross-process GUIs pick up the event
    try:
        add_update(
            json.dumps(
                {
                    "type": "request_event",
                    "id": rowid,
                    "request_id": request_id,
                    "event_type": event_type,
                    "duration_seconds": duration_seconds,
                }
            )
        )
    except Exception:
        pass
    return rowid


def claim_request_for_sending(request_id: int) -> bool:
    """Atomically claim a request for sending. Returns True if claimed, False if already sent/claimed."""
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE requests SET status = ? WHERE id = ? AND (status IS NULL OR status NOT IN ('done','sending'))",
            ("sending", request_id),
        )
        conn.commit()
        ok = cur.rowcount > 0
    except Exception:
        ok = False
    conn.close()
    # notify GUIs that a request moved to 'sending' (claimed)
    try:
        from . import ws_broadcast

        if ok and getattr(ws_broadcast, "loop", None):
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {"type": "request_status", "id": request_id, "status": "sending"}
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass
    return ok


def claim_request_for_processing(request_id: int) -> bool:
    """Atomically claim a request for processing (download/compress). Returns True if claimed."""
    now = datetime.datetime.utcnow().isoformat()
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE requests SET status = ?, processing_started_at = ? WHERE id = ? AND (status IS NULL OR status NOT IN ('processing','sending','done'))",
            ("processing", now, request_id),
        )
        conn.commit()
        ok = cur.rowcount > 0
    except Exception:
        ok = False
    conn.close()
    # notify GUIs that a request was claimed for processing
    try:
        from . import ws_broadcast

        if ok and getattr(ws_broadcast, "loop", None):
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {"type": "request_status", "id": request_id, "status": "processing"}
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass
    return ok


def get_request_events(request_id: int, limit: int = 50, offset: int = 0):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, event_type, details, duration_seconds, created_at FROM request_events WHERE request_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
        (request_id, limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def add_update(raw: str) -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO updates (raw, created_at) VALUES (?, ?)",
        (raw, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    # publish event
    try:
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            import asyncio

            asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast(
                    {"type": "update_created", "id": rowid, "raw": raw}
                ),
                ws_broadcast.loop,
            )
    except Exception:
        pass
    return rowid


def list_updates(limit: int = 100, offset: int = 0):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, raw, created_at FROM updates ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def find_recent_request_by_chat_url(chat_id: int, url: str, since_seconds: int = 0):
    """Return the most recent request row (id, status, created_at) for the given
    chat_id and url which was created within the last `since_seconds`. Returns
    None if no such request exists.
    """
    try:
        cutoff = None
        if since_seconds and since_seconds > 0:
            cutoff_dt = datetime.datetime.utcnow() - datetime.timedelta(
                seconds=int(since_seconds)
            )
            cutoff = cutoff_dt.isoformat()
        conn = _connect()
        cur = conn.cursor()
        if cutoff:
            cur.execute(
                "SELECT id, status, created_at FROM requests WHERE chat_id = ? AND url = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (chat_id, url, cutoff),
            )
        else:
            cur.execute(
                "SELECT id, status, created_at FROM requests WHERE chat_id = ? AND url = ? ORDER BY created_at DESC LIMIT 1",
                (chat_id, url),
            )
        row = cur.fetchone()
        conn.close()
        return row
    except Exception:
        return None


def is_message_processed(chat_id: int, message_id: int) -> bool:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
        (chat_id, message_id),
    )
    r = cur.fetchone()
    conn.close()
    return bool(r)


def mark_message_processed(chat_id: int, message_id: int):
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO processed_messages (chat_id, message_id, created_at) VALUES (?, ?, ?)",
            (chat_id, message_id, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # already present (deduplicated at DB level) — ignore
        pass
    finally:
        conn.close()


def count_updates() -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM updates")
    r = cur.fetchone()[0]
    conn.close()
    return r


def add_user(username: str = None, email: str = None, role: str = "user") -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, email, role, created_at) VALUES (?, ?, ?, ?)",
        (username, email, role, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def list_users(limit: int = 100, offset: int = 0):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, username, email, role, created_at FROM users ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_user_by_email(email: str):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, username, email, role, created_at FROM users WHERE email = ? LIMIT 1",
        (email,),
    )
    r = cur.fetchone()
    conn.close()
    return r


def set_user_role(user_id: int, role: str):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    conn.close()


def delete_user(user_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def clear_history():
    """Clear historical request data (requests, events, updates, processed messages).

    This is intended for operator/debug use from the Admin GUI.
    """
    conn = _connect()
    cur = conn.cursor()
    success = False
    try:
        cur.execute("DELETE FROM request_events")
        cur.execute("DELETE FROM requests")
        cur.execute("DELETE FROM updates")
        cur.execute("DELETE FROM processed_messages")
        conn.commit()
        success = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # broadcast an event so connected GUIs can refresh (only on success)
    if success:
        try:
            from . import ws_broadcast

            if getattr(ws_broadcast, "loop", None):
                import asyncio

                asyncio.run_coroutine_threadsafe(
                    ws_broadcast.broadcast({"type": "db_cleared"}), ws_broadcast.loop
                )
        except Exception:
            pass
