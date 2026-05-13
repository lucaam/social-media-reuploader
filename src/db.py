import datetime
import os
import sqlite3

DB_PATH = os.environ.get(
    "REQUESTS_DB", os.path.join(os.getcwd(), "data", "requests.db")
)


def init_db():
    dirpath = os.path.dirname(DB_PATH)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            url TEXT,
            status TEXT,
            created_at TEXT
        )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw TEXT,
            created_at TEXT
        )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            email TEXT,
            role TEXT,
            created_at TEXT
        )""")
    # processed messages dedup table
    conn.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            created_at TEXT
        )""")
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
    conn.execute("""CREATE TABLE IF NOT EXISTS request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            event_type TEXT,
            details TEXT,
            duration_seconds REAL,
            created_at TEXT
        )""")
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_events_request_id ON request_events(request_id)"
        )
    except Exception:
        pass
    # ensure requests has a description column for future use
    cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
    if "description" not in cols:
        try:
            conn.execute("ALTER TABLE requests ADD COLUMN description TEXT")
        except Exception:
            # ignore if cannot alter (older sqlite, etc.)
            pass
    conn.commit()
    conn.close()


def add_request(
    chat_id: int,
    url: str,
    status: str = "pending",
    description: str = None,
    original_message_id: int = None,
) -> int:
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM requests")
    r = cur.fetchone()[0]
    conn.close()
    return r


def update_request_status(request_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
    conn.commit()
    conn.close()


def mark_request_started(request_id: int):
    now = datetime.datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE requests SET original_size = ? WHERE id = ?",
        (original_size, request_id),
    )
    conn.commit()
    conn.close()


def add_request_event(
    request_id: int,
    event_type: str,
    details: str = None,
    duration_seconds: float = None,
) -> int:
    conn = sqlite3.connect(DB_PATH)
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
    return rowid


def claim_request_for_sending(request_id: int) -> bool:
    """Atomically claim a request for sending. Returns True if claimed, False if already sent/claimed."""
    conn = sqlite3.connect(DB_PATH)
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
    return ok


def claim_request_for_processing(request_id: int) -> bool:
    """Atomically claim a request for processing (download/compress). Returns True if claimed."""
    now = datetime.datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
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
    return ok


def get_request_events(request_id: int, limit: int = 50, offset: int = 0):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, event_type, details, duration_seconds, created_at FROM request_events WHERE request_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
        (request_id, limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def add_update(raw: str) -> int:
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, raw, created_at FROM updates ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def is_message_processed(chat_id: int, message_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
        (chat_id, message_id),
    )
    r = cur.fetchone()
    conn.close()
    return bool(r)


def mark_message_processed(chat_id: int, message_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO processed_messages (chat_id, message_id, created_at) VALUES (?, ?, ?)",
        (chat_id, message_id, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def count_updates() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM updates")
    r = cur.fetchone()[0]
    conn.close()
    return r


def add_user(username: str = None, email: str = None, role: str = "user") -> int:
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, username, email, role, created_at FROM users ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_user_by_email(email: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, username, email, role, created_at FROM users WHERE email = ? LIMIT 1",
        (email,),
    )
    r = cur.fetchone()
    conn.close()
    return r


def set_user_role(user_id: int, role: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    conn.close()


def delete_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
