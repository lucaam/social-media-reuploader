import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from asyncio.subprocess import PIPE
from typing import Optional

from . import config, db, downloader, metrics, telegram_api

# module-level pointer to the active WorkerPool instance (if any).
# WorkerPool.__init__ will assign itself to this so other modules (GUI)
# can retrieve the running pool for monitoring/debugging.
active_worker = None

logger = logging.getLogger(__name__)


def _mb(n: int | None) -> str:
    try:
        if n is None:
            return "? MB"
        return f"{n / (1024 * 1024):.1f} MB"
    except Exception:
        return "? MB"


class WorkerPool:
    def __init__(self, token: str, workers: int = 2):
        self.token = token
        self._sem = asyncio.Semaphore(workers)
        self._tasks = set()
        self._closing = False
        # queue for incoming requests (FIFO)
        self._queue: asyncio.Queue = asyncio.Queue()
        # ensure only one transcode runs at a time
        self._transcode_lock: asyncio.Lock = asyncio.Lock()
        # per-chat timestamps for rate limiting (chat_id -> [ts, ...])
        self._chat_timestamps: dict = {}
        # in-memory inflight enqueues to avoid race duplicates before DB write
        self._inflight_urls: set = set()
        # last time we warned a chat about rate limiting (chat_id -> ts)
        self._last_rate_warning: dict = {}
        # last time we warned a chat about duplicate submissions
        self._last_duplicate_warning: dict = {}
        # last time a chat was actively rate-limited (chat_id -> ts)
        self._last_rate_limited: dict = {}
        # next allowed timestamp per chat when rate-limited (chat_id -> unix_ts)
        self._last_rate_limited_next: dict = {}
        # default rate limits: (count, window_seconds)
        # stricter defaults: avoid bursts and long-running saturation
        self._rate_limits = [
            (1, 10),
            (3, 60),
            (5, 3600),
            (20, 30 * 24 * 3600),
        ]
        # notification throttles (seconds) read from config so operators can tune
        self._notify_rate_throttle = getattr(config, "NOTIFY_RATE_THROTTLE_SECONDS", 60)
        self._notify_duplicate_throttle = getattr(
            config, "NOTIFY_DUPLICATE_THROTTLE_SECONDS", 600
        )
        # per-chat pending cap: queued + running tasks limit (protects worker saturation)
        self._max_pending_per_chat = getattr(config, "MAX_PENDING_PER_CHAT", 1)
        # dedupe window in seconds for same chat+url (default 90 minutes)
        self._dedupe_window_seconds = getattr(
            config, "DUPLICATE_WINDOW_SECONDS", 90 * 60
        )
        # start dispatcher task if event loop is running
        try:
            loop = asyncio.get_running_loop()
            self._dispatch_task = loop.create_task(self._dispatch_loop())
            try:
                # Rehydrate persisted queued requests from DB into the in-memory
                # queue so that a worker restart continues processing where it
                # left off. Make this behavior configurable via
                # `config.WORKER_REHYDRATE_ON_START`.
                if getattr(config, "WORKER_REHYDRATE_ON_START", True):
                    loop.create_task(self._rehydrate_persisted_queue())
                else:
                    # Mark any persisted queued rows as 'aborted' so operators
                    # can inspect what was interrupted without requeuing.
                    try:
                        conn = db._connect()
                        cur = conn.cursor()
                        cur.execute("SELECT id FROM requests WHERE status = 'queued'")
                        rows = [r[0] for r in cur.fetchall()]
                        try:
                            conn.close()
                        except Exception:
                            pass
                    except Exception:
                        rows = []
                    aborted = 0
                    for rid in rows:
                        try:
                            db.update_request_status(rid, "aborted")
                            try:
                                db.add_request_event(
                                    rid,
                                    "aborted_on_startup",
                                    details=("aborted at startup because WORKER_REHYDRATE_ON_START=false"),
                                )
                            except Exception:
                                pass
                            aborted += 1
                        except Exception:
                            pass
                    try:
                        logger.info("Marked %s persisted queued requests as aborted on startup", aborted)
                    except Exception:
                        pass
            except Exception:
                pass
        except RuntimeError:
            # not in a running loop; dispatcher will be started later
            self._dispatch_task = None

        # optional periodic rehydration: if configured, start a background
        # task that periodically pulls persisted 'queued' rows from the DB
        # into the in-memory queue. This helps when the GUI or another
        # process requeues items and the worker runs in a separate process.
        try:
            interval = float(getattr(config, "WORKER_PERIODIC_REHYDRATE_SECONDS", 0) or 0)
            if interval and interval > 0:
                try:
                    loop.create_task(self._periodic_rehydrate_loop(interval))
                except Exception:
                    pass
                try:
                    # also start an updates poller so GUI/other processes can
                    # notify this worker about unlimit events across processes.
                    loop.create_task(self._db_updates_poller(interval))
                except Exception:
                    pass
        except Exception:
            pass

        # register active pool for external monitoring (GUI/debug)
        try:
            global active_worker
            active_worker = self
        except Exception:
            pass

    def enqueue(
        self,
        chat_id,
        url,
        description: Optional[str] = None,
        original_message_id: Optional[int] = None,
        chat_type: Optional[str] = None,
    ) -> bool:
        """Schedule processing of a link. Returns True if scheduled."""
        if self._closing:
            return False
        now = time.time()
        # quick in-process dedupe to avoid racing DB writes from concurrent handlers
        key = (chat_id, url)
        already_inflight = False
        try:
            if key in self._inflight_urls:
                already_inflight = True
            else:
                self._inflight_urls.add(key)
        except Exception:
            already_inflight = False

        if already_inflight:
            # Persist a small marker so operators can see duplicate attempts
            try:
                rid = db.add_request(
                    chat_id,
                    url,
                    status="duplicate",
                    description=description,
                    original_message_id=original_message_id,
                )
                try:
                    db.add_request_event(
                        rid,
                        "duplicate",
                        details=("duplicate (inflight) of recent enqueue for same url"),
                    )
                except Exception:
                    pass
            except Exception:
                pass
            try:
                last_dup = self._last_duplicate_warning.get(chat_id)
                if not last_dup or (now - last_dup) > int(
                    self._notify_duplicate_throttle or 600
                ):
                    try:
                        asyncio.create_task(
                            self._notify_duplicate(
                                chat_id,
                                original_message_id,
                                int(self._dedupe_window_seconds),
                            )
                        )
                    except Exception:
                        pass
                    try:
                        self._last_duplicate_warning[chat_id] = now
                    except Exception:
                        pass
            except Exception:
                pass
            return False

        # If this chat is currently rate-limited by an earlier attempt, block
        # new enqueues immediately (prevents sending a different link to
        # 'self-unblock'). Notify the user (throttled) and persist a
        # rate_limited marker for operator visibility.
        try:
            next_allowed = self._last_rate_limited_next.get(chat_id)
            if next_allowed and now < float(next_allowed):
                # persist a rate_limited request row for traceability
                try:
                    rid = db.add_request(
                        chat_id,
                        url,
                        status="rate_limited",
                        description=description,
                        original_message_id=original_message_id,
                    )
                    try:
                        db.add_request_event(
                            rid,
                            "rate_limited",
                            details=(f"enqueue blocked by active rate-limit; next_in={int(max(0, float(next_allowed) - now))}s"),
                        )
                    except Exception:
                        pass
                except Exception:
                    rid = None

                # Throttled notification to chat about active rate limit
                try:
                    last_warn = self._last_rate_warning.get(chat_id)
                    if not last_warn or (now - last_warn) > int(self._notify_rate_throttle or 60):
                        try:
                            asyncio.create_task(
                                self._notify_rate_limit(
                                    chat_id,
                                    original_message_id,
                                    int(max(0, float(next_allowed) - now)),
                                )
                            )
                        except Exception:
                            pass
                        try:
                            self._last_rate_warning[chat_id] = now
                        except Exception:
                            pass
                except Exception:
                    pass

                try:
                    self._inflight_urls.discard(key)
                except Exception:
                    pass
                return False
        except Exception:
            pass

        try:
            # 1) Duplicate suppression: do not download the same URL from the
            # same chat within the configured dedupe window (default 90m).
            try:
                recent = db.find_recent_request_by_chat_url(
                    chat_id, url, since_seconds=self._dedupe_window_seconds
                )
                if recent:
                    # If the most-recent matching request exists, examine its
                    # status. Treat prior 'rate_limited' as an active block and
                    # notify the user; treat prior 'duplicate' as a duplicate
                    # and notify accordingly. Only previously successful/queued
                    # requests count as true dedupe for the same URL.
                    try:
                        recent_status = recent[1] if len(recent) > 1 else None
                    except Exception:
                        recent_status = None

                    # If previously rate_limited, re-assert the block and notify.
                    if recent_status == "rate_limited":
                        try:
                            # compute remaining using in-memory next_allowed if present
                            next_allowed = self._last_rate_limited_next.get(chat_id)
                            if next_allowed and float(next_allowed) > now:
                                remaining = int(max(0, int(float(next_allowed) - now)))
                            else:
                                # fallback: infer from recent.created_at if available
                                try:
                                    from datetime import datetime

                                    created_at = None
                                    try:
                                        created_iso = recent[2]
                                        if created_iso:
                                            created_at = datetime.fromisoformat(created_iso)
                                    except Exception:
                                        created_at = None
                                    remaining = int(self._dedupe_window_seconds)
                                    if created_at is not None:
                                        try:
                                            now_dt = datetime.utcnow()
                                            elapsed = (now_dt - created_at).total_seconds()
                                            remaining = int(
                                                max(
                                                    0,
                                                    int(self._dedupe_window_seconds) - int(elapsed),
                                                )
                                            )
                                        except Exception:
                                            remaining = int(self._dedupe_window_seconds)
                                except Exception:
                                    remaining = int(self._dedupe_window_seconds)
                        except Exception:
                            remaining = int(self._dedupe_window_seconds)

                        # persist a small marker for operator visibility
                        try:
                            rid = db.add_request(
                                chat_id,
                                url,
                                status="rate_limited",
                                description=description,
                                original_message_id=original_message_id,
                            )
                            try:
                                db.add_request_event(
                                    rid,
                                    "rate_limited",
                                    details=(f"re-attempt blocked; prior rate_limited; next_in={remaining}s"),
                                )
                            except Exception:
                                pass
                        except Exception:
                            rid = None

                        try:
                            last_warn = self._last_rate_warning.get(chat_id)
                            if not last_warn or (now - last_warn) > int(self._notify_rate_throttle or 60):
                                try:
                                    asyncio.create_task(
                                        self._notify_rate_limit(chat_id, original_message_id, remaining)
                                    )
                                except Exception:
                                    pass
                                try:
                                    self._last_rate_warning[chat_id] = now
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        try:
                            self._inflight_urls.discard(key)
                        except Exception:
                            pass
                        return False

                    # Treat prior duplicate as duplicate: notify with remaining dedupe window
                    if recent_status == "duplicate":
                        try:
                            # Persist a marker so operators can see the attempt
                            rid = db.add_request(
                                chat_id,
                                url,
                                status="duplicate",
                                description=description,
                                original_message_id=original_message_id,
                            )
                            try:
                                db.add_request_event(
                                    rid,
                                    "duplicate",
                                    details=(
                                        f"duplicate of request {recent[0]} within {int(self._dedupe_window_seconds)}s"
                                    ),
                                )
                            except Exception:
                                pass
                        except Exception:
                            pass

                        # compute remaining seconds until dedupe window expires
                        try:
                            from datetime import datetime

                            created_at = None
                            try:
                                created_iso = recent[2]
                                if created_iso:
                                    created_at = datetime.fromisoformat(created_iso)
                            except Exception:
                                created_at = None
                            remaining = int(self._dedupe_window_seconds)
                            if created_at is not None:
                                try:
                                    now_dt = datetime.utcnow()
                                    elapsed = (now_dt - created_at).total_seconds()
                                    remaining = int(
                                        max(
                                            0,
                                            int(self._dedupe_window_seconds) - int(elapsed),
                                        )
                                    )
                                except Exception:
                                    remaining = int(self._dedupe_window_seconds)
                        except Exception:
                            remaining = int(self._dedupe_window_seconds)

                        # notify user about duplicate only if not recently warned (long throttle)
                        try:
                            last_dup = self._last_duplicate_warning.get(chat_id)
                            if not last_dup or (now - last_dup) > int(
                                self._notify_duplicate_throttle or 600
                            ):
                                try:
                                    asyncio.create_task(
                                        self._notify_duplicate(
                                            chat_id, original_message_id, remaining
                                        )
                                    )
                                except Exception:
                                    pass
                                try:
                                    self._last_duplicate_warning[chat_id] = now
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        # ensure inflight marker removed before returning
                        try:
                            self._inflight_urls.discard(key)
                        except Exception:
                            pass
                        return False

                    # else: prior successful/queued/processing requests are treated as duplicates
                    # fall through to duplicate handler below
                    # (we will handle below as before)
            except Exception:
                # if DB check fails, continue to next checks
                pass

            # ensure timestamp list exists and prune long-ago entries
            ts = self._chat_timestamps.setdefault(chat_id, [])
            try:
                cutoff = now - (30 * 24 * 3600)
                self._chat_timestamps[chat_id] = [t for t in ts if t >= cutoff]
                ts = self._chat_timestamps[chat_id]
            except Exception:
                ts = ts

            # 2) per-chat pending cap: count queued + running tasks for this chat
            try:
                queued_raw = list(getattr(self._queue, "_queue", []))
                queued_count = sum(
                    1 for it in queued_raw if (it or {}).get("chat_id") == chat_id
                )
            except Exception:
                queued_count = 0
            try:
                running_count = 0
                for t in list(getattr(self, "_tasks", set()) or set()):
                    try:
                        if getattr(t, "done", lambda: True)():
                            continue
                    except Exception:
                        pass
                    try:
                        itm = getattr(t, "_item", None)
                        if itm and itm.get("chat_id") == chat_id:
                            running_count += 1
                    except Exception:
                        pass
            except Exception:
                running_count = 0

            if (queued_count + running_count) >= int(self._max_pending_per_chat or 2):
                # block and persist a rate_limited marker
                try:
                    rid = db.add_request(
                        chat_id,
                        url,
                        status="rate_limited",
                        description=description,
                        original_message_id=original_message_id,
                    )
                    try:
                        db.add_request_event(
                            rid,
                            "rate_limited",
                            details=(
                                f"pending cap reached: queued={queued_count} running={running_count} limit={self._max_pending_per_chat}"
                            ),
                        )
                    except Exception:
                        pass
                except Exception:
                    rid = None
                try:
                    self._last_rate_limited[chat_id] = now
                    # temporary block for pending-cap cases (short backoff of 2 minutes)
                    self._last_rate_limited_next[chat_id] = now + 120
                except Exception:
                    pass
                # notify GUIs about this in-memory rate-limit change
                try:
                    from . import ws_broadcast

                    try:
                        ws_broadcast.publish_sync({
                            "type": "rate_limit_changed",
                            "chat_id": chat_id,
                            "next": self._last_rate_limited_next.get(chat_id),
                        })
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    logger.info(
                        "Enqueue blocked (pending cap) for chat=%s url=%s queued=%s running=%s limit=%s",
                        chat_id,
                        url,
                        queued_count,
                        running_count,
                        self._max_pending_per_chat,
                    )
                except Exception:
                    pass
                try:
                    # do not send immediate rate-limit messages at enqueue time
                    # to avoid false-positives; operators can inspect persisted
                    # 'rate_limited' rows in the GUI.
                    logger.info(
                        "Enqueue blocked (pending cap) for chat=%s url=%s queued=%s running=%s limit=%s",
                        chat_id,
                        url,
                        queued_count,
                        running_count,
                        self._max_pending_per_chat,
                    )
                    try:
                        self._last_rate_warning[chat_id] = now
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    self._inflight_urls.discard(key)
                except Exception:
                    pass
                # Do NOT schedule an automatic requeue for rate-limited items.
                # Persisted DB row remains with status='rate_limited' so operators
                # can inspect and decide whether to requeue via the Admin GUI.
                return False

            # Check rate limits at enqueue time and BLOCK if exceeded.
            exceeded = False
            delay_needed = 0
            try:
                # diagnostic logging: show recent timestamps and computed counts
                try:
                    recent_ts = list(ts)[-10:]
                    logger.debug(
                        "enqueue: chat=%s recent_ts=%s rate_limits=%s",
                        chat_id,
                        recent_ts,
                        getattr(self, "_rate_limits", []),
                    )
                except Exception:
                    pass
            except Exception:
                pass
            try:
                for limit, window in self._rate_limits:
                    cnt = sum(1 for t in ts if t >= now - window)
                    # if current count already meets/exceeds limit, block this new one
                    if cnt >= limit:
                        oldest = min(t for t in ts if t >= now - window)
                        d = (oldest + window) - now
                        if d > delay_needed:
                            delay_needed = d
                        exceeded = True
            except Exception:
                exceeded = False

            if exceeded:
                # Persist a "rate_limited" request record and log an event
                try:
                    rid = db.add_request(
                        chat_id,
                        url,
                        status="rate_limited",
                        description=description,
                        original_message_id=original_message_id,
                    )
                    try:
                        db.add_request_event(
                            rid,
                            "rate_limited",
                            details=f"limit exceeded; next_in={int(delay_needed)}s",
                        )
                    except Exception:
                        pass
                except Exception:
                    rid = None

                # record last-limited timestamp and optionally notify chat (best-effort)
                try:
                    self._last_rate_limited[chat_id] = now
                    # record the next allowed time based on computed delay_needed
                    try:
                        self._last_rate_limited_next[chat_id] = now + (
                            delay_needed if delay_needed and delay_needed > 0 else 120
                        )
                    except Exception:
                        self._last_rate_limited_next[chat_id] = now + 120
                except Exception:
                    pass

                # broadcast rate-limit change so GUI updates promptly
                try:
                    from . import ws_broadcast

                    try:
                        ws_broadcast.publish_sync({
                            "type": "rate_limit_changed",
                            "chat_id": chat_id,
                            "next": self._last_rate_limited_next.get(chat_id),
                        })
                    except Exception:
                        pass
                except Exception:
                    pass

                # throttle frequent warnings according to configured throttle
                try:
                    # avoid notifying users at enqueue time to reduce false
                    # positives; dispatch-time notifications remain.
                    try:
                        self._last_rate_warning[chat_id] = now
                    except Exception:
                        pass
                    logger.info(
                        "Enqueue blocked (rate limit) for chat=%s url=%s next_in=%s",
                        chat_id,
                        url,
                        int(delay_needed) if delay_needed else None,
                    )
                except Exception:
                    pass

                try:
                    self._inflight_urls.discard(key)
                except Exception:
                    pass
                # Do NOT schedule automatic requeue for rate-limited items.
                # Operators can requeue via the Admin GUI when appropriate.
                return False

            # not exceeded: record this enqueue timestamp and persist queued request
            item = {
                "chat_id": chat_id,
                "url": url,
                "description": description,
                "original_message_id": original_message_id,
                "chat_type": chat_type,
                "enqueued_at": now,
            }
            try:
                ts.append(now)
            except Exception:
                pass

            # persist a queued request in DB so external GUIs/processes can
            # observe pending items even when worker and GUI run in different
            # processes. db.add_request will deduplicate by original_message_id
            # if present and return the existing request id.
            try:
                rid = db.add_request(
                    chat_id,
                    url,
                    status="queued",
                    description=description,
                    original_message_id=original_message_id,
                )
                try:
                    item["request_id"] = rid
                except Exception:
                    pass
            except Exception:
                # non-fatal: continue even if DB write fails
                pass
            try:
                self._queue.put_nowait(item)
            except Exception:
                try:
                    self._inflight_urls.discard(key)
                except Exception:
                    pass
                return False
            # queued successfully: remove inflight marker so DB-backed dedupe works
            try:
                self._inflight_urls.discard(key)
            except Exception:
                pass
            return True
        except Exception:
            try:
                self._inflight_urls.discard(key)
            except Exception:
                pass
            return False

    async def shutdown(self, timeout: int | None = None):
        """Gracefully wait for currently running tasks to finish.

        New enqueues are rejected once shutdown starts. Waits up to `timeout`
        seconds (defaults to `config.WORKER_SHUTDOWN_TIMEOUT` or 30s) and then
        cancels remaining tasks.
        """
        self._closing = True
        # cancel dispatcher if running
        try:
            if getattr(self, "_dispatch_task", None):
                try:
                    self._dispatch_task.cancel()
                except Exception:
                    pass
                try:
                    await self._dispatch_task
                except Exception:
                    pass
        except Exception:
            pass
        if timeout is None:
            timeout = getattr(config, "WORKER_SHUTDOWN_TIMEOUT", 30)
        start = time.time()
        while self._tasks and (timeout is None or (time.time() - start) < timeout):
            await asyncio.sleep(0.1)
        # cancel remaining tasks
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True), timeout=5
            )
        except Exception:
            pass

    async def _periodic_rehydrate_loop(self, interval: float):
        """Background loop that periodically invokes the persisted-queue
        rehydration routine. Runs until the worker is closed.
        """
        try:
            while not self._closing:
                try:
                    await asyncio.sleep(float(interval))
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass
                try:
                    await self._rehydrate_persisted_queue()
                except Exception:
                    pass
        except Exception:
            pass

    def trigger_rehydrate(self):
        """Schedule an immediate rehydrate task on the running loop.

        This is useful for the GUI to call when items were requeued via
        the Admin UI and an immediate pickup is desired.
        """
        try:
            loop = asyncio.get_running_loop()
            try:
                loop.create_task(self._rehydrate_persisted_queue())
            except Exception:
                pass
        except Exception:
            # not running in an event loop (rare) — ignore
            pass

    async def _db_updates_poller(self, poll_interval: float = 5.0):
        """Poll the DB `updates` table and react to cross-process events.

        Currently supports: `unlimited` (with `chat_id`) and `unlimited_all`.
        When an `unlimited` event is observed, clear the in-memory rate-limit
        markers for the specified chat so persisted queued items can be
        processed immediately.
        """
        last_id = 0
        try:
            conn = db._connect()
            cur = conn.cursor()
            cur.execute("SELECT MAX(id) FROM updates")
            r = cur.fetchone()
            try:
                last_id = int(r[0]) if r and r[0] is not None else 0
            except Exception:
                last_id = 0
            try:
                conn.close()
            except Exception:
                pass
        except Exception:
            last_id = 0

        while not self._closing:
            try:
                await asyncio.sleep(float(poll_interval))
            except asyncio.CancelledError:
                break
            except Exception:
                pass

            try:
                conn = db._connect()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, raw FROM updates WHERE id > ? ORDER BY id ASC",
                    (last_id,),
                )
                rows = cur.fetchall()
                try:
                    conn.close()
                except Exception:
                    pass
            except Exception:
                rows = []

            for r in rows:
                try:
                    uid, raw = r
                except Exception:
                    continue
                try:
                    last_id = int(uid)
                except Exception:
                    pass
                parsed = None
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
                try:
                    if isinstance(parsed, dict) and parsed.get("type"):
                        t = parsed.get("type")
                        if t == "unlimited":
                            cid = parsed.get("chat_id")
                            try:
                                if cid is not None:
                                    self._last_rate_limited.pop(cid, None)
                                    self._last_rate_limited_next.pop(cid, None)
                                    self._last_rate_warning.pop(cid, None)
                                    logger.info("Cleared in-memory rate-limit for chat=%s via DB update", cid)
                                    # rehydrate queued items for this chat immediately
                                    try:
                                        asyncio.create_task(self._rehydrate_persisted_queue())
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        elif t == "unlimited_all":
                            try:
                                self._last_rate_limited.clear()
                                self._last_rate_limited_next.clear()
                                self._last_rate_warning.clear()
                                logger.info("Cleared all in-memory rate-limits via DB update")
                                try:
                                    asyncio.create_task(self._rehydrate_persisted_queue())
                                except Exception:
                                    pass
                            except Exception:
                                pass
                except Exception:
                    pass

    async def _delayed_requeue(self, item: dict, delay: float):
        try:
            if delay and delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        try:
            if not self._closing:
                # If this item references a persisted request row, update its
                # status to 'queued' so the dispatcher does not skip it.
                try:
                    rid = item.get("request_id") if isinstance(item, dict) else None
                except Exception:
                    rid = None
                if rid:
                    try:
                        db.update_request_status(rid, "queued")
                        try:
                            db.add_request_event(
                                rid,
                                "requeued",
                                details=f"requeued after {int(delay) if delay else 0}s",
                            )
                        except Exception:
                            pass
                    except Exception:
                        pass
                await self._queue.put(item)
        except Exception:
            pass

    async def _rehydrate_persisted_queue(self):
        """Load persisted requests with status='queued' from the DB into
        the in-memory queue at startup so restarts continue processing.
        """
        try:
            # small delay to allow other startup tasks to complete
            await asyncio.sleep(0.1)
        except Exception:
            pass
        try:
            conn = db._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, chat_id, url, description, original_message_id, created_at FROM requests WHERE status = 'queued' ORDER BY created_at ASC"
            )
            rows = cur.fetchall()
            try:
                conn.close()
            except Exception:
                pass
        except Exception:
            rows = []

        # compute existing request_ids already present in memory (queue/tasks)
        existing_rids = set()
        try:
            queued_raw = list(getattr(self._queue, "_queue", []))
            for it in queued_raw:
                try:
                    if isinstance(it, dict) and it.get("request_id"):
                        existing_rids.add(int(it.get("request_id")))
                except Exception:
                    pass
        except Exception:
            pass
        try:
            for t in list(getattr(self, "_tasks", set()) or set()):
                try:
                    itm = getattr(t, "_item", None)
                    if itm and itm.get("request_id"):
                        existing_rids.add(int(itm.get("request_id")))
                except Exception:
                    pass
        except Exception:
            pass

        requeued = 0
        now = time.time()
        cutoff = now - (30 * 24 * 3600)
        for r in rows or []:
            try:
                rid, chat_id, url, description, original_message_id, created_at = r
                try:
                    if int(rid) in existing_rids:
                        # already present in memory; skip to avoid duplicate enqueue
                        continue
                except Exception:
                    pass
                # build item dict similar to enqueue
                item = {
                    "chat_id": chat_id,
                    "url": url,
                    "description": description,
                    "original_message_id": original_message_id,
                    "chat_type": None,
                    "enqueued_at": now,
                    "request_id": rid,
                }
                # update in-memory timestamps for rate limiting conservatively
                try:
                    ts = self._chat_timestamps.setdefault(chat_id, [])
                    # only include recent timestamps
                    if now >= cutoff:
                        ts.append(now)
                except Exception:
                    pass
                try:
                    await self._queue.put(item)
                    requeued += 1
                except Exception:
                    pass
            except Exception:
                pass
        try:
            logger.info("Rehydrated %s persisted queued requests into memory", requeued)
        except Exception:
            pass
        try:
            from . import ws_broadcast

            try:
                ws_broadcast.publish_sync({"type": "queue_rehydrated", "count": requeued})
            except Exception:
                pass
        except Exception:
            pass

    async def _notify_rate_limit(
        self, chat_id: int, original_message_id: int | None, delay: float
    ):
        try:
            # Format a human-friendly message. Avoid showing 0 seconds.
            try:
                d = int(delay) if delay is not None else None
            except Exception:
                d = None
            try:
                if d is None or d <= 0:
                    msg = "Sei temporaneamente limitato: troppi link inviati. Riprova tra qualche minuto."
                elif d < 60:
                    msg = f"Sei temporaneamente limitato: riprova tra {d} secondi."
                else:
                    mins = int((d + 59) // 60)
                    msg = f"Sei temporaneamente limitato: riprova tra circa {mins} minuto{'' if mins==1 else 'i'}."
            except Exception:
                msg = "Sei temporaneamente limitato: riprova più tardi."
            try:
                await telegram_api.send_message(
                    self.token, chat_id, msg, reply_to_message_id=original_message_id
                )
            except Exception:
                logger.debug("Could not send rate limit warning to chat %s", chat_id)
        except Exception:
            pass

    async def _notify_duplicate(
        self,
        chat_id: int,
        original_message_id: int | None,
        delay_seconds: int | None = None,
    ):
        try:
            # Friendly Italian message describing remaining wait time
            try:
                if delay_seconds and delay_seconds > 0:
                    if delay_seconds >= 60:
                        mins = int((delay_seconds + 59) // 60)
                        msg = f"Hai già inviato questo link di recente. Riprova tra circa {mins} minuto{'' if mins==1 else 'i'}."
                    else:
                        msg = f"Hai già inviato questo link di recente; riprova tra {int(delay_seconds)} secondi."
                else:
                    msg = "Hai già inviato questo link di recente; riprova più tardi."
            except Exception:
                msg = "Hai già inviato questo link di recente; riprova più tardi."
            try:
                await telegram_api.send_message(
                    self.token, chat_id, msg, reply_to_message_id=original_message_id
                )
            except Exception:
                logger.debug("Could not send duplicate notice to chat %s", chat_id)
        except Exception:
            pass

    async def _dispatch_loop(self):
        """Background dispatcher that pulls items from the queue, enforces per-chat rate limits
        and starts worker tasks honoring the semaphore (max concurrent workers).
        """
        # use configured rate limits from the instance (set in __init__)
        limits = list(getattr(self, "_rate_limits", []))
        while not self._closing:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break
            if item is None:
                continue
            # If this item was previously persisted as rate_limited/duplicate,
            # skip processing to avoid resurrecting blocked attempts.
            try:
                rid = item.get("request_id") if isinstance(item, dict) else None
                if rid:
                    try:
                        conn = db._connect()
                        cur = conn.cursor()
                        cur.execute("SELECT status FROM requests WHERE id = ?", (rid,))
                        row = cur.fetchone()
                        conn.close()
                        if row and row[0] in ("rate_limited", "duplicate"):
                            # skip and do not requeue
                            continue
                    except Exception:
                        pass
            except Exception:
                pass
            chat_id = item.get("chat_id")
            now = time.time()

            # If this chat has an active rate-limit window, skip processing
            try:
                next_allowed = self._last_rate_limited_next.get(chat_id)
                if next_allowed and now < float(next_allowed):
                    # mark persisted request (if present) as rate_limited and skip
                    try:
                        rid = item.get("request_id") if isinstance(item, dict) else None
                        if rid:
                            try:
                                db.update_request_status(rid, "rate_limited")
                                db.add_request_event(
                                    rid,
                                    "rate_limited",
                                    details="skipped due to active rate-limit window",
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                    continue
            except Exception:
                pass

            # prune old timestamps for chat
            try:
                ts = self._chat_timestamps.setdefault(chat_id, [])
                cutoff = now - (30 * 24 * 3600)
                # keep timestamps newer than cutoff
                self._chat_timestamps[chat_id] = [t for t in ts if t >= cutoff]
                ts = self._chat_timestamps[chat_id]
            except Exception:
                ts = []

            # check limits
            exceeded = False
            delay_needed = 0
            try:
                for limit, window in limits:
                    cnt = sum(1 for t in ts if t >= now - window)
                    # ts already includes timestamps recorded at enqueue time,
                    # so allow up to `limit` entries in the window. Block only
                    # when the count exceeds the configured limit.
                    if cnt > limit:
                        # compute earliest time when next slot frees
                        oldest = min(t for t in ts if t >= now - window)
                        d = (oldest + window) - now
                        if d > delay_needed:
                            delay_needed = d
                        exceeded = True
                # if exceeded, warn (once per minute) and requeue after delay
                if exceeded and delay_needed > 0:
                    # Prefer any previously recorded next-allowed timestamp
                    # when computing the user-facing remaining seconds so that
                    # messages are consistent with what the dispatcher will honor.
                    try:
                        import math

                        next_allowed_ts = self._last_rate_limited_next.get(chat_id)
                        if next_allowed_ts and float(next_allowed_ts) > now:
                            remaining = int(max(0, math.ceil(float(next_allowed_ts) - now)))
                        else:
                            remaining = int(max(0, math.ceil(delay_needed)))
                    except Exception:
                        try:
                            remaining = int(max(0, int(delay_needed)))
                        except Exception:
                            remaining = int(delay_needed) if delay_needed else 0

                    # record a canonical next-allowed time so subsequent checks
                    # and client notifications observe the same window.
                    try:
                        self._last_rate_limited_next[chat_id] = now + (remaining if remaining and remaining > 0 else int(delay_needed))
                    except Exception:
                        pass

                    # broadcast rate-limit change so connected GUIs refresh
                    try:
                        from . import ws_broadcast

                        try:
                            ws_broadcast.publish_sync({
                                "type": "rate_limit_changed",
                                "chat_id": chat_id,
                                "next": self._last_rate_limited_next.get(chat_id),
                            })
                        except Exception:
                            pass
                    except Exception:
                        pass

                    last_warn = self._last_rate_warning.get(chat_id)
                    if not last_warn or (now - last_warn) > int(self._notify_rate_throttle or 60):
                        try:
                            # best-effort notify chat about throttling
                            if remaining and remaining > 0:
                                msg = (
                                    f"Rate limit: troppi link inviati. "
                                    f"Il tuo link sarà processato tra {remaining} secondi."
                                )
                            else:
                                msg = "Sei temporaneamente limitato: riprova tra qualche secondo."
                            await telegram_api.send_message(self.token, chat_id, msg)
                        except Exception:
                            logger.debug("Could not send rate limit warning to chat %s", chat_id)
                        try:
                            self._last_rate_warning[chat_id] = now
                        except Exception:
                            pass

                    # requeue with delay
                    try:
                        asyncio.create_task(self._delayed_requeue(item, delay_needed))
                    except Exception:
                        try:
                            await self._queue.put(item)
                        except Exception:
                            pass
                    continue
            except Exception:
                # if rate check fails, proceed to start task
                pass

            # timestamps for rate limiting are recorded at enqueue time
            # (we intentionally do not append here to avoid double-counting)

            # wait for a worker slot and start the processing task
            try:
                await self._sem.acquire()
            except asyncio.CancelledError:
                break

            async def _run_item(itm: dict):
                try:
                    await self._process(
                        itm.get("chat_id"),
                        itm.get("url"),
                        itm.get("description"),
                        itm.get("original_message_id"),
                        itm.get("chat_type"),
                    )
                finally:
                    try:
                        self._sem.release()
                    except Exception:
                        pass

            try:
                t = asyncio.create_task(_run_item(item))
                # attach the processed item to the task for monitoring/UI
                try:
                    setattr(t, "_item", item)
                except Exception:
                    pass
                self._tasks.add(t)
                t.add_done_callback(lambda fut: self._tasks.discard(fut))
            except Exception:
                try:
                    self._sem.release()
                except Exception:
                    pass
                logger.exception("Could not start worker task for item %s", item)

    async def _generate_thumbnail(
        self, ffmpeg_bin: str, src_path: str, dst_dir: str
    ) -> Optional[str]:
        base = os.path.splitext(os.path.basename(src_path))[0]
        thumb = os.path.join(dst_dir, f"{base}_thumb.jpg")
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            src_path,
            "-ss",
            "00:00:01.000",
            "-vframes",
            "1",
            "-vf",
            "scale=w=320:h=-2:force_original_aspect_ratio=decrease,setsar=1",
            "-q:v",
            "2",
            thumb,
        ]
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
            try:
                await asyncio.wait_for(p.communicate(), timeout=15)
            except asyncio.TimeoutError:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    await p.communicate()
                except Exception:
                    pass
                return None
            if p.returncode == 0 and os.path.exists(thumb):
                return thumb
        except Exception:
            logger.debug("thumbnail generation failed for %s", src_path)
        return None

    async def _transcode_to_baseline(
        self,
        src_path: str,
        dst_path: str,
        tmpdir: str,
        meta: dict | None = None,
        target_size: int | None = None,
    ) -> bool:
        """Ensure `dst_path` is an MP4 file compatible with Telegram.

        Strategy:
        - If possible, try a fast remux/copy into MP4 (`-c copy`).
        - If audio codec is incompatible (e.g. opus), copy video and transcode audio to AAC.
        - Otherwise fall back to full transcode to H.264 baseline profile.
        Logs ffmpeg stderr on failure to aid debugging.
        """
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            logger.debug("ffmpeg not available for transcode")
            return False

        # try to learn codecs from meta if available
        video_codec = None
        audio_codec = None
        try:
            if isinstance(meta, dict):
                video_codec = (meta.get("video_codec") or "").lower()
                audio_codec = (meta.get("audio_codec") or "").lower()
        except Exception:
            pass

        async def _run_cmd(cmd, timeout):
            # Run ffmpeg and stream stderr in real-time to logs so users can
            # observe progress. Also capture full stderr to return on failure.
            logger.info("running ffmpeg cmd: %s", " ".join(cmd))
            try:
                p = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
            except Exception as e:
                logger.exception("ffmpeg invocation failed: %s", e)
                return False, b"", b""

            full_err = bytearray()
            buffer = bytearray()

            # try to obtain duration from meta for progress percentage
            duration_seconds = None
            try:
                if isinstance(meta, dict):
                    duration_seconds = meta.get("duration")
                    if duration_seconds is None:
                        fmt = meta.get("format") or {}
                        if isinstance(fmt, dict):
                            try:
                                duration_seconds = float(fmt.get("duration"))
                            except Exception:
                                duration_seconds = None
            except Exception:
                duration_seconds = None

            async def _reader():
                nonlocal buffer, full_err
                try:
                    while True:
                        chunk = await p.stderr.read(1024)
                        if not chunk:
                            break
                        full_err.extend(chunk)
                        buffer.extend(chunk)
                        # find last newline/carriage return
                        last_sep = max(buffer.rfind(b"\n"), buffer.rfind(b"\r"))
                        if last_sep != -1:
                            part = bytes(buffer[: last_sep + 1])
                            try:
                                text = part.decode(errors="ignore")
                            except Exception:
                                text = ""
                            # compress multiple lines into a single concise log entry
                            lines = [
                                line.strip()
                                for line in re.split(r"[\r\n]+", text)
                                if line.strip()
                            ]
                            if lines:
                                single = " | ".join(lines)
                                # use DEBUG for verbose ffmpeg output; keep progress at INFO
                                logger.debug("ffmpeg: %s", single)
                                # try to parse last occurrence of time=VALUE to show percent
                                try:
                                    if duration_seconds:
                                        idx = single.rfind("time=")
                                        if idx != -1:
                                            token = single[idx + 5 :].split()[0]
                                            parts = token.split(":")
                                            secs = 0.0
                                            if len(parts) == 3:
                                                secs = (
                                                    float(parts[0]) * 3600
                                                    + float(parts[1]) * 60
                                                    + float(parts[2])
                                                )
                                            elif len(parts) == 2:
                                                secs = float(parts[0]) * 60 + float(
                                                    parts[1]
                                                )
                                            else:
                                                secs = float(parts[0])
                                            pct = min(
                                                100.0,
                                                max(
                                                    0.0,
                                                    (secs / float(duration_seconds))
                                                    * 100.0,
                                                ),
                                            )
                                            logger.info(
                                                "ffmpeg progress: time=%s (%.1f%%)",
                                                token,
                                                pct,
                                            )
                                except Exception:
                                    pass
                            # keep remainder after last_sep
                            buffer = bytearray(buffer[last_sep + 1 :])
                except Exception as e:
                    logger.exception("error reading ffmpeg stderr: %s", e)
                # log any leftover compressed into a single line
                if buffer:
                    try:
                        tail = bytes(buffer).decode(errors="ignore")
                        tail_lines = [
                            line.strip()
                            for line in re.split(r"[\r\n]+", tail)
                            if line.strip()
                        ]
                        if tail_lines:
                            logger.debug("ffmpeg: %s", " | ".join(tail_lines))
                    except Exception:
                        pass

            reader_task = asyncio.create_task(_reader())

            try:
                try:
                    await asyncio.wait_for(p.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    try:
                        p.kill()
                    except Exception:
                        pass
                    try:
                        await p.wait()
                    except Exception:
                        pass
                    logger.warning("ffmpeg timed out for %s", src_path)
                    # ensure reader finishes
                    try:
                        await reader_task
                    except Exception:
                        pass
                    return False, b"", bytes(full_err)
                # ensure reader finished reading remaining stderr
                try:
                    await reader_task
                except Exception:
                    pass
                # read remaining stdout
                try:
                    out = await p.stdout.read()
                except Exception:
                    out = b""
                return p.returncode == 0, out, bytes(full_err)
            except Exception as e:
                logger.exception("ffmpeg run failed: %s", e)
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    await reader_task
                except Exception:
                    pass
                return False, b"", bytes(full_err)

        # 1) attempt fast remux (copy) when codecs look compatible
        if video_codec == "h264" and (audio_codec in (None, "aac", "mp3")):
            cmd_copy = [
                ffmpeg_bin,
                "-y",
                "-i",
                src_path,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                dst_path,
            ]
            ok, out, err = await _run_cmd(cmd_copy, timeout=60)
            if ok and os.path.exists(dst_path):
                return True
            logger.debug(
                "fast remux failed: %s",
                (err.decode(errors="ignore")[:1000] if err else ""),
            )

        # 2) if video is h264, try copying video and transcode audio to aac
        if video_codec == "h264":
            cmd_audio = [
                ffmpeg_bin,
                "-y",
                "-i",
                src_path,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                dst_path,
            ]
            ok, out, err = await _run_cmd(cmd_audio, timeout=120)
            if ok and os.path.exists(dst_path):
                return True
            logger.debug(
                "copy-video+transcode-audio failed: %s",
                (err.decode(errors="ignore")[:1000] if err else ""),
            )

        # 3) full transcode to baseline
        # Compute an orientation-aware scale+pad filter when we have stream
        # dimensions available (keeps visual content and pads to Telegram-friendly sizes).
        vf_filter = "setsar=1"
        try:
            if isinstance(meta, dict):
                vw = meta.get("video_width")
                vh = meta.get("video_height")
                vrot = meta.get("video_rotation")
                try:
                    w = int(vw) if vw else None
                    h = int(vh) if vh else None
                except Exception:
                    w = None
                    h = None
                # account for rotation metadata swapping w/h
                try:
                    r = int(vrot) if vrot is not None else 0
                except Exception:
                    r = 0
                if r in (90, -270, 270, -90) and w is not None and h is not None:
                    w, h = h, w

                is_portrait = False
                try:
                    if w and h:
                        is_portrait = int(h) >= int(w)
                except Exception:
                    is_portrait = False

                if is_portrait:
                    pad_w = 720
                    pad_h = 1280
                else:
                    pad_w = 640
                    pad_h = 360

                max_w = pad_w
                max_h = pad_h

                if w and h:
                    scale_ratio = min(
                        1.0, float(max_w) / float(w), float(max_h) / float(h)
                    )
                    new_w = max(2, int((w * scale_ratio) // 2 * 2))
                    new_h = max(2, int((h * scale_ratio) // 2 * 2))
                    if new_w == w and new_h == h:
                        if new_w == pad_w and new_h == pad_h:
                            vf_filter = "setsar=1"
                        else:
                            pad_x = max(0, (pad_w - new_w) // 2)
                            pad_y = max(0, (pad_h - new_h) // 2)
                            vf_filter = (
                                f"setsar=1,pad={pad_w}:{pad_h}:{pad_x}:{pad_y}:black"
                            )
                    else:
                        pad_x = max(0, (pad_w - new_w) // 2)
                        pad_y = max(0, (pad_h - new_h) // 2)
                        vf_filter = f"scale={new_w}:{new_h},setsar=1,pad={pad_w}:{pad_h}:{pad_x}:{pad_y}:black"
                else:
                    # fallback: keep simple SAR reset
                    vf_filter = "setsar=1"
        except Exception:
            vf_filter = "setsar=1"

        # Optionally compute a target video bitrate to fit `target_size` bytes
        # given the media duration and a fixed audio bitrate. This tries to
        # produce an MP4 small enough for Telegram when we need to reduce
        # filesize.
        bitrate_args: list[str] = []
        try:
            if target_size and isinstance(meta, dict):
                dur = meta.get("duration")
                if dur is None:
                    # try nested format.duration
                    fmt = meta.get("format") or {}
                    try:
                        dur = (
                            float(fmt.get("duration"))
                            if isinstance(fmt, dict) and fmt.get("duration")
                            else None
                        )
                    except Exception:
                        dur = None
                if dur and dur > 0:
                    # Reserve a small headroom and subtract audio bitrate (128k)
                    audio_bps = 128000
                    total_target_bps = (float(target_size) * 8.0) / float(dur)
                    # headroom factor to account for container overhead
                    total_target_bps *= 0.95
                    video_bps = int(max(100000, total_target_bps - audio_bps))
                    video_k = max(100, int(video_bps / 1000))
                    maxrate_k = int(video_k * 1.5)
                    bufsize_k = int(video_k * 2)
                    bitrate_args = [
                        "-b:v",
                        f"{video_k}k",
                        "-maxrate",
                        f"{maxrate_k}k",
                        "-bufsize",
                        f"{bufsize_k}k",
                    ]
        except Exception:
            bitrate_args = []

        cmd_full = [
            ffmpeg_bin,
            "-y",
            "-i",
            src_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            vf_filter,
            *bitrate_args,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            dst_path,
        ]
        ok, out, err = await _run_cmd(cmd_full, timeout=300)
        if ok and os.path.exists(dst_path):
            return True
        # log ffmpeg stderr for debugging
        try:
            stderr_txt = err.decode(errors="ignore") if err else ""
        except Exception:
            stderr_txt = "<decoding error>"
        logger.warning(
            "Worker ffmpeg transcode failed for %s: %s", src_path, stderr_txt[:2000]
        )
        return False

    async def _edit_status(
        self, token: str, chat_id: int, status_msg_id: int | None, text: str
    ):
        """Helper to edit an existing status message or send a new one.

        Returns the message_id of the status message (int) or None.
        """
        try:
            if status_msg_id:
                try:
                    res = await telegram_api.edit_message_text(
                        token, chat_id, status_msg_id, text
                    )
                    if isinstance(res, dict) and res.get("ok"):
                        return status_msg_id
                except Exception:
                    pass
            # send a new status message
            try:
                res = await telegram_api.send_message(token, chat_id, text)
                if isinstance(res, dict) and res.get("ok"):
                    return res.get("result", {}).get("message_id")
            except Exception:
                pass
        except Exception:
            pass
        return None

    async def _process(
        self,
        chat_id,
        url,
        description: Optional[str] = None,
        original_message_id: Optional[int] = None,
        chat_type: Optional[str] = None,
    ):
        request_id = None
        tmpdir = tempfile.mkdtemp(dir=getattr(config, "TMP_DIR", None))
        # track which reaction we added on the original message (None, 'eyes', 'banana')
        reaction_current: Optional[str] = None
        status_msg_id = None
        try:
            try:
                request_id = db.add_request(
                    chat_id,
                    url,
                    status="pending",
                    description=description,
                    original_message_id=original_message_id,
                )
            except Exception:
                request_id = None
            # mark processing started early so telemetry records processing_started_at
            try:
                if request_id:
                    db.mark_request_started(request_id)
            except Exception:
                pass

            # Try to add an eyes reaction to the original message; fallback to status message
            if original_message_id:
                try:
                    r = await telegram_api.set_message_reaction(
                        self.token, chat_id, original_message_id, "👀"
                    )
                    if isinstance(r, dict):
                        ok = r.get("ok")
                    else:
                        ok = bool(r)
                    if ok:
                        reaction_current = "eyes"
                    else:
                        try:
                            status_res = await telegram_api.send_message(
                                self.token,
                                chat_id,
                                "In lavorazione 👀",
                                reply_to_message_id=original_message_id,
                            )
                            if isinstance(status_res, dict) and status_res.get("ok"):
                                status_msg_id = status_res.get("result", {}).get(
                                    "message_id"
                                )
                        except Exception:
                            logger.debug("Could not create status message")
                except Exception:
                    logger.debug(
                        "Could not add reaction; falling back to status message"
                    )
                    try:
                        status_res = await telegram_api.send_message(
                            self.token,
                            chat_id,
                            "In lavorazione 👀",
                            reply_to_message_id=original_message_id,
                        )
                        if isinstance(status_res, dict) and status_res.get("ok"):
                            status_msg_id = status_res.get("result", {}).get(
                                "message_id"
                            )
                    except Exception:
                        logger.debug("Could not create status message")

            # download
            try:
                file_path, meta = await downloader.download(
                    url,
                    tmpdir,
                    max_bytes=getattr(config, "TELEGRAM_MAX_FILE_SIZE", None),
                )

                # Ensure meta-derived flags are present (default to True)
                has_video = True
                has_audio = True
                if isinstance(meta, dict):
                    try:
                        if meta.get("has_video") is not None:
                            has_video = bool(meta.get("has_video"))
                        if meta.get("has_audio") is not None:
                            has_audio = bool(meta.get("has_audio"))
                    except Exception:
                        pass

                # If yt-dlp selected an audio-only format because we passed
                # a --max-filesize limit (common for Instagram reels larger
                # than Telegram's limit), try one redownload without the
                # filesize limit to let yt-dlp pick the best video stream.
                try:
                    if not getattr(config, "SIMPLE_YTDLP_ONLY", False):
                        if not has_video:
                            lowurl = (url or "").lower()
                            if "instagram.com" in lowurl or "reel" in lowurl:
                                logger.info(
                                    "No video stream detected; attempting redownload without size limit for %s",
                                    url,
                                )
                                try:
                                    file_path2, meta2 = await downloader.download(
                                        url, tmpdir, max_bytes=None
                                    )
                                    if isinstance(meta2, dict) and meta2.get(
                                        "has_video"
                                    ):
                                        logger.info(
                                            "Redownload succeeded and contains a video stream; switching to redownloaded file"
                                        )
                                        file_path = file_path2
                                        meta = meta2
                                        # reflect that we redownloaded (event)
                                        try:
                                            if request_id:
                                                db.add_request_event(
                                                    request_id,
                                                    "redownload",
                                                    details="redownload without filesize limit",
                                                    duration_seconds=None,
                                                )
                                        except Exception:
                                            logger.debug(
                                                "Could not record redownload event for request %s",
                                                request_id,
                                            )
                                    else:
                                        logger.debug(
                                            "Redownload did not produce a video stream"
                                        )
                                except Exception as e:
                                    logger.debug("Redownload attempt failed: %s", e)
                except Exception:
                    pass
            except Exception as e:
                # Special-case auth/impersonation and oversized file errors so we can notify the chat
                msg = str(e)
                low = msg.lower()
                auth_indicators = [
                    "you need to log in",
                    "you need to log in to access",
                    "login required",
                    "requiring login",
                    "requires login",
                    "no csrf token",
                    "this content isn't available",
                    "not available to everyone",
                    "authentication",
                    "cookies",
                    "impersonation",
                    "impersonate",
                ]
                is_auth = any(sub in low for sub in auth_indicators) or (
                    "login" in low and "tiktok" in (url or "").lower()
                )

                # If downloader reported the file is too large, send a friendly
                # fallback message with the original link instead of attempting
                # to upload as a document (we never send videos as documents).
                if "downloaded file too large" in low or "too large" in low:
                    try:
                        logger.info(
                            "File too large to send; posting fallback link to chat %s",
                            chat_id,
                        )
                        text = f"Couldn't send the video. Download it here: {url}"
                        await telegram_api.send_message(self.token, chat_id, text)
                    except Exception:
                        logger.debug(
                            "Failed to send fallback link message for large file"
                        )
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
                    return

                try:
                    if request_id:
                        db.add_request_event(request_id, "error", details=msg)
                except Exception:
                    pass

                try:
                    if is_auth and request_id:
                        try:
                            db.add_request_event(
                                request_id, "auth_required", details=msg
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

                if is_auth:
                    logger.warning(
                        "Auth/private content error processing %s: %s", url, msg
                    )
                    metrics.downloads_failed_total.inc()
                    try:
                        hint = "Contenuto privato o che richiede autenticazione: il bot al momento non riesce a scaricarlo."
                        if chat_type and chat_type.lower() in (
                            "group",
                            "supergroup",
                            "channel",
                        ):
                            if original_message_id:
                                try:
                                    try:
                                        radd = await telegram_api.set_message_reaction(
                                            self.token,
                                            chat_id,
                                            original_message_id,
                                            "🍌",
                                        )
                                        ok_radd = (
                                            (isinstance(radd, dict) and radd.get("ok"))
                                            if isinstance(radd, dict)
                                            else bool(radd)
                                        )
                                        if ok_radd:
                                            reaction_current = "banana"
                                        else:
                                            radd = None
                                    except Exception:
                                        radd = None
                                    if not radd:
                                        try:
                                            await telegram_api.send_message(
                                                self.token,
                                                chat_id,
                                                "🍌",
                                                reply_to_message_id=original_message_id,
                                            )
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            else:
                                try:
                                    await telegram_api.send_message(
                                        self.token,
                                        chat_id,
                                        "🍌",
                                        reply_to_message_id=original_message_id,
                                    )
                                except Exception:
                                    pass
                            try:
                                await telegram_api.send_message(
                                    self.token,
                                    chat_id,
                                    hint,
                                    reply_to_message_id=original_message_id,
                                )
                            except Exception:
                                pass
                        else:
                            # In private chats try to remove the eyes reaction (if we added it)
                            # then attempt to add a banana reaction; if reactions are not
                            # supported fall back to sending a banana emoji message.
                            if original_message_id:
                                try:
                                    try:
                                        radd = await telegram_api.set_message_reaction(
                                            self.token, chat_id, original_message_id, "🍌"
                                        )
                                        ok_radd = (
                                            (isinstance(radd, dict) and radd.get("ok"))
                                            if isinstance(radd, dict)
                                            else bool(radd)
                                        )
                                        if ok_radd:
                                            reaction_current = "banana"
                                        else:
                                            try:
                                                await telegram_api.send_message(
                                                    self.token,
                                                    chat_id,
                                                    "🍌",
                                                    reply_to_message_id=original_message_id,
                                                )
                                            except Exception:
                                                pass
                                    except Exception:
                                        try:
                                            await telegram_api.send_message(
                                                self.token,
                                                chat_id,
                                                "🍌",
                                                reply_to_message_id=original_message_id,
                                            )
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            try:
                                await telegram_api.send_message(
                                    self.token,
                                    chat_id,
                                    hint,
                                    reply_to_message_id=original_message_id,
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
                else:
                    logger.exception("Error during download: %s", e)
                    metrics.downloads_failed_total.inc()
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass

                return

            try:
                size = os.path.getsize(file_path)
            except Exception:
                size = None
            final_size = (
                meta.get("final_size") if isinstance(meta, dict) else None
            ) or size
            # Ensure meta reflects the best-known final size at this stage
            try:
                if isinstance(meta, dict):
                    meta["final_size"] = final_size
            except Exception:
                pass

            # persist original size in the DB for telemetry/debugging
            try:
                if request_id and isinstance(meta, dict):
                    orig = meta.get("original_size") or size
                    if orig is not None:
                        try:
                            db.set_request_original_size(request_id, int(orig))
                        except Exception:
                            logger.debug(
                                "Could not persist original_size to DB for request %s",
                                request_id,
                            )
            except Exception:
                pass
            if not has_video:
                logger.info(
                    "Downloaded file has no video stream — wrapper disabled, sending original file as-is: %s",
                    file_path,
                )

            # helper: send compression notice in private chats
            async def _maybe_notify_compression():
                try:
                    compressed = (
                        meta.get("compressed") if isinstance(meta, dict) else False
                    )
                    if (
                        compressed
                        and chat_type
                        and chat_type.lower() not in ("group", "supergroup", "channel")
                    ):
                        orig_mb = _mb(
                            meta.get("original_size")
                            if isinstance(meta, dict)
                            else None
                        )
                        final_mb = _mb(final_size)
                        try:
                            await telegram_api.send_message(
                                self.token,
                                chat_id,
                                f"Ho compresso il video: {orig_mb} -> {final_mb}",
                                reply_to_message_id=original_message_id,
                            )
                        except Exception:
                            logger.debug("Could not send compression notice")
                except Exception:
                    logger.debug("compression notify failed")

            # Final stats will be recorded after a successful upload

            # attempt to claim the request for sending to avoid duplicates
            claimed = True
            if request_id is not None:
                try:
                    claimed = db.claim_request_for_sending(request_id)
                except Exception:
                    claimed = True
            if not claimed:
                try:
                    status_msg_id = await self._edit_status(
                        self.token, chat_id, status_msg_id, "🏆 already processed"
                    )
                except Exception:
                    pass
                return

            ext = os.path.splitext(file_path)[1].lower().lstrip(".")
            # Thumbnail generation disabled by user request — skip entirely
            thumb_path = None
            logger.debug("Skipping thumbnail generation (disabled)")

            # Decide whether we must convert/remux for Telegram compatibility.
            # Prefer to preserve the original file whenever possible. Only
            # transcode when ffprobe-derived `meta` indicates incompatibility
            # (non-h264 video, non-aac audio, rotation, SAR/DAR issues, or
            # file too large).
            try:
                preferred_video = ("h264", "mpeg4")
                preferred_audio = ("aac", "mp3", "mp4a")
                has_video = meta.get("has_video") if isinstance(meta, dict) else True
                has_audio = meta.get("has_audio") if isinstance(meta, dict) else True
                video_codec = (
                    (meta.get("video_codec") or "").lower()
                    if isinstance(meta, dict)
                    else ""
                )
                audio_codec = (
                    (meta.get("audio_codec") or "").lower()
                    if isinstance(meta, dict)
                    else ""
                )
                fmt = (
                    (meta.get("format") or "").lower() if isinstance(meta, dict) else ""
                )
                container_ok = ("mp4" in fmt) or (ext == "mp4")
                size_ok = (
                    True
                    if final_size is None
                    else final_size
                    <= getattr(config, "TELEGRAM_MAX_FILE_SIZE", 50 * 1024 * 1024)
                )

                # Require a preferred video codec (H.264/MPEG4) when a video
                # stream is present. Even if the container is MP4, non-H.264
                # codecs (e.g. VP9) can fail to play inline in Telegram clients,
                # so prefer to transcode to H.264 when needed. Audio codec is
                # also validated when a video stream is present.
                codec_ok = True
                if has_video:
                    codec_ok = (video_codec in preferred_video) and (
                        (not has_audio) or (audio_codec in preferred_audio)
                    )

                logger.debug(
                    "telegram rules: has_video=%s has_audio=%s video_codec=%s audio_codec=%s format=%s size_ok=%s",
                    has_video,
                    has_audio,
                    video_codec,
                    audio_codec,
                    fmt,
                    size_ok,
                )

                need_conversion = not (
                    has_video and size_ok and codec_ok and container_ok
                )

                # Do not attempt to convert/remux audio-only files. The
                # wrapper/transcode logic expects a video stream and will
                # fail on pure-audio inputs (e.g. .m4a). If there's no
                # video stream, preserve original bytes and send as media.
                if not has_video:
                    logger.debug(
                        "Audio-only file detected; skipping conversion/transcode"
                    )
                    need_conversion = False

                if need_conversion:
                    ffmpeg_bin = shutil.which("ffmpeg")
                    if not ffmpeg_bin:
                        logger.warning(
                            "File requires conversion for Telegram but ffmpeg is not available: %s",
                            file_path,
                        )
                        try:
                            status_msg_id = await self._edit_status(
                                self.token,
                                chat_id,
                                status_msg_id,
                                "👀 conversion failed: ffmpeg missing",
                            )
                        except Exception:
                            pass
                        try:
                            if request_id:
                                db.update_request_status(request_id, "failed")
                        except Exception:
                            pass
                        return

                    # Use _transcode_to_baseline which first tries fast remux, then audio transcode, then full transcode.
                    try:
                        base = os.path.splitext(os.path.basename(file_path))[0]
                        trans_path = os.path.join(tmpdir, f"{base}_tg_transcoded.mp4")
                        max_allowed = getattr(
                            config, "TELEGRAM_MAX_FILE_SIZE", 50 * 1024 * 1024
                        )
                        # Prefer not to upsize: use the smaller of the original
                        # file size and the allowed Telegram max as the target.
                        try:
                            orig_sz = size or (
                                meta.get("original_size") if isinstance(meta, dict) else None
                            )
                        except Exception:
                            orig_sz = None
                        try:
                            if orig_sz and orig_sz < max_allowed:
                                target_sz = int(orig_sz)
                            else:
                                target_sz = int(max_allowed)
                        except Exception:
                            target_sz = int(max_allowed)
                        logger.debug(
                            "Transcode target: orig=%s max_allowed=%s target=%s",
                            orig_sz,
                            max_allowed,
                            target_sz,
                        )
                        # ensure only one transcode runs at a time
                        try:
                            async with self._transcode_lock:
                                ok = await self._transcode_to_baseline(
                                    file_path,
                                    trans_path,
                                    tmpdir,
                                    meta=meta,
                                    target_size=target_sz,
                                )
                        except Exception:
                            ok = False
                        if ok and os.path.exists(trans_path):
                            try:
                                file_path = trans_path
                                ext = "mp4"
                                size = os.path.getsize(file_path)
                                final_size = size
                                try:
                                    if isinstance(meta, dict):
                                        meta["compressed"] = True
                                        # Update meta to reflect the new final size
                                        try:
                                            meta["final_size"] = final_size
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                # We intentionally do not regenerate thumbnails here
                                # since thumbnailing is disabled per user request.
                            except Exception:
                                pass
                        else:
                            logger.warning(
                                "Worker ffmpeg transcode/remux failed for %s", file_path
                            )
                            try:
                                status_msg_id = await self._edit_status(
                                    self.token,
                                    chat_id,
                                    status_msg_id,
                                    "👀 conversion to mp4 failed",
                                )
                            except Exception:
                                pass
                            try:
                                if request_id:
                                    db.update_request_status(request_id, "failed")
                            except Exception:
                                pass
                            return
                    except Exception:
                        logger.exception("Worker transcode exception")
            except Exception:
                logger.debug(
                    "Could not evaluate Telegram compatibility rules; proceeding with original behavior"
                )

            # send media — prefer inline `sendVideo` when ffprobe `meta` says it's
            # compatible, otherwise send as a document. Fail only when the file
            # exceeds Telegram size limits.
            try:
                max_sz = getattr(config, "TELEGRAM_MAX_FILE_SIZE", 50 * 1024 * 1024)
                if size is not None and size <= max_sz:
                    # If this is audio-only, send via send_media() which will
                    # choose the appropriate API (document/audio) instead of
                    # attempting to send as a video.
                    try:
                        is_audio_only = False
                        if isinstance(meta, dict):
                            is_audio_only = not bool(meta.get("has_video"))
                    except Exception:
                        is_audio_only = False

                    if is_audio_only:
                        try:
                            res = await telegram_api.send_media(
                                self.token,
                                chat_id,
                                file_path,
                                caption=None,
                                reply_to_message_id=original_message_id,
                                meta=meta,
                            )
                        except Exception as e:
                            logger.exception("send_media (audio-only) failed: %s", e)
                            try:
                                status_msg_id = await self._edit_status(
                                    self.token,
                                    chat_id,
                                    status_msg_id,
                                    "👀 upload failed",
                                )
                            except Exception:
                                pass
                            try:
                                if request_id:
                                    db.update_request_status(request_id, "failed")
                            except Exception:
                                pass
                            res = {"ok": False}
                    else:
                        # decide if we can send as a Telegram video
                        try:
                            video_ok = False
                            if isinstance(meta, dict):
                                has_video = meta.get("has_video")
                                fmt = (meta.get("format") or "").lower()
                                # Be permissive: prefer to send mp4 inline even if codec
                                # isn't H.264. Some bots send VP9-in-mp4 successfully.
                                video_ok = bool(has_video) and ("mp4" in fmt)
                            else:
                                video_ok = ext == "mp4"
                        except Exception:
                            video_ok = ext == "mp4"

                        if video_ok:
                            try:
                                res = await telegram_api.send_video(
                                    self.token,
                                    chat_id,
                                    file_path,
                                    reply_to_message_id=original_message_id,
                                    thumbnail_path=thumb_path,
                                    meta=meta,
                                )
                            except Exception as e:
                                logger.exception("send_video failed: %s", e)
                                try:
                                    status_msg_id = await self._edit_status(
                                        self.token,
                                        chat_id,
                                        status_msg_id,
                                        "👀 upload failed",
                                    )
                                except Exception:
                                    pass
                                try:
                                    if request_id:
                                        db.update_request_status(request_id, "failed")
                                except Exception:
                                    pass
                                res = {"ok": False}
                        else:
                            # We do not send videos as documents. If the file is an
                            # MP4, attempt to upload inline anyway; otherwise send
                            # a fallback message with the original URL so users can
                            # download it themselves.
                            if ext == "mp4":
                                try:
                                    res = await telegram_api.send_video(
                                        self.token,
                                        chat_id,
                                        file_path,
                                        reply_to_message_id=original_message_id,
                                        thumbnail_path=thumb_path,
                                        meta=meta,
                                    )
                                except Exception as e:
                                    logger.exception(
                                        "send_video (fallback) failed: %s", e
                                    )
                                    try:
                                        await telegram_api.send_message(
                                            self.token,
                                            chat_id,
                                            f"Couldn't send the video. Download it here: {url}",
                                            reply_to_message_id=original_message_id,
                                        )
                                    except Exception:
                                        logger.debug(
                                            "Failed to send fallback link message after send_video error"
                                        )
                                    res = {"ok": False}
                            else:
                                try:
                                    await telegram_api.send_message(
                                        self.token,
                                        chat_id,
                                        f"Couldn't send the video. Download it here: {url}",
                                        reply_to_message_id=original_message_id,
                                    )
                                except Exception:
                                    logger.debug(
                                        "Failed to send fallback link message for non-mp4 file"
                                    )
                                res = {"ok": False}
                    # evaluate send result (only when upload was attempted)
                    try:
                        ok = isinstance(res, dict) and res.get("ok")
                    except Exception:
                        ok = False
                    if not ok:
                        logger.warning("send returned non-ok: %s", res)
                        try:
                            status_msg_id = await self._edit_status(
                                self.token,
                                chat_id,
                                status_msg_id,
                                "👀 upload failed",
                            )
                        except Exception:
                            pass
                        try:
                            if request_id:
                                db.update_request_status(request_id, "failed")
                        except Exception:
                            pass
                    else:
                        metrics.files_sent_total.inc()
                        try:
                            if request_id:
                                # record finished telemetry (final size and compressed flag)
                                try:
                                    compressed_val = False
                                    try:
                                        if (
                                            isinstance(meta, dict)
                                            and meta.get("compressed") is not None
                                        ):
                                            compressed_val = bool(
                                                meta.get("compressed")
                                            )
                                    except Exception:
                                        compressed_val = False
                                    db.mark_request_finished(
                                        request_id,
                                        final_size=final_size,
                                        compressed=compressed_val,
                                    )
                                except Exception:
                                    pass
                                db.update_request_status(request_id, "done")
                        except Exception:
                            pass
                        try:
                            await _maybe_notify_compression()
                        except Exception:
                            pass
                        try:
                            if reaction_current == "eyes" and original_message_id:
                                try:
                                    # replace processing eyes reaction with a success trophy
                                    await telegram_api.set_message_reaction(
                                        self.token,
                                        chat_id,
                                        original_message_id,
                                        "🏆",
                                    )
                                    reaction_current = "trophy"
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        if status_msg_id:
                            try:
                                await telegram_api.delete_message(
                                    self.token, chat_id, status_msg_id
                                )
                            except Exception:
                                pass
                else:
                    logger.warning("File too large (%s). Sending fallback link.", size)
                    metrics.files_too_large_total.inc()
                    try:
                        status_msg_id = await self._edit_status(
                            self.token,
                            chat_id,
                            status_msg_id,
                            f"👀 file troppo grande — {_mb(size)}",
                        )
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                auth_indicators = [
                    "you need to log in",
                    "you need to log in to access",
                    "login required",
                    "requiring login",
                    "requires login",
                    "no csrf token",
                    "this content isn't available",
                    "not available to everyone",
                    "authentication",
                    "cookies",
                    "impersonation",
                    "impersonate",
                ]
                is_auth = any(sub in low for sub in auth_indicators) or (
                    "login" in low and "tiktok" in (url or "").lower()
                )

                try:
                    if request_id:
                        db.add_request_event(request_id, "error", details=msg)
                except Exception:
                    pass

                try:
                    if is_auth and request_id:
                        try:
                            db.add_request_event(
                                request_id, "auth_required", details=msg
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

                if is_auth:
                    logger.warning(
                        "Auth/private content error processing %s: %s", url, msg
                    )
                    metrics.downloads_failed_total.inc()
                    try:
                        hint = "Contenuto privato o che richiede autenticazione: il bot al momento non riesce a scaricarlo."
                        if chat_type and chat_type.lower() in (
                            "group",
                            "supergroup",
                            "channel",
                        ):
                            if original_message_id:
                                try:
                                    try:
                                        await telegram_api.set_message_reaction(
                                            self.token,
                                            chat_id,
                                            original_message_id,
                                            "🍌",
                                        )
                                        reaction_current = "banana"
                                    except Exception:
                                        try:
                                            await telegram_api.send_message(
                                                self.token,
                                                chat_id,
                                                "🍌",
                                                reply_to_message_id=original_message_id,
                                            )
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            else:
                                try:
                                    await telegram_api.send_message(
                                        self.token,
                                        chat_id,
                                        "🍌",
                                        reply_to_message_id=original_message_id,
                                    )
                                except Exception:
                                    pass
                            try:
                                await telegram_api.send_message(
                                    self.token,
                                    chat_id,
                                    hint,
                                    reply_to_message_id=original_message_id,
                                )
                            except Exception:
                                pass
                        else:
                            if reaction_current == "eyes" and original_message_id:
                                try:
                                    # on generic error replace eyes with banana to indicate failure
                                    await telegram_api.set_message_reaction(
                                        self.token,
                                        chat_id,
                                        original_message_id,
                                        "🍌",
                                    )
                                    reaction_current = "banana"
                                except Exception:
                                    pass
                            try:
                                await telegram_api.send_message(
                                    self.token,
                                    chat_id,
                                    hint,
                                    reply_to_message_id=original_message_id,
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
                else:
                    logger.exception("Error processing link: %s", e)
                    metrics.downloads_failed_total.inc()
                    try:
                        if status_msg_id:
                            try:
                                await telegram_api.delete_message(
                                    self.token, chat_id, status_msg_id
                                )
                            except Exception:
                                try:
                                    await telegram_api.edit_message_text(
                                        self.token, chat_id, status_msg_id, ""
                                    )
                                except Exception:
                                    pass
                            if reaction_current == "eyes" and original_message_id:
                                try:
                                    # on generic error replace eyes with banana to indicate failure
                                    await telegram_api.set_message_reaction(
                                        self.token,
                                        chat_id,
                                        original_message_id,
                                        "🍌",
                                    )
                                    reaction_current = "banana"
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
            finally:
                try:
                    if getattr(config, "KEEP_DOWNLOADED_FILES", False):
                        logger.info(
                            "Preserving tmpdir per KEEP_DOWNLOADED_FILES: %s", tmpdir
                        )
                    else:
                        shutil.rmtree(tmpdir)
                except Exception:
                    pass

        except Exception as e:
            logger.exception("Error processing link %s: %s", url, e)
            metrics.downloads_failed_total.inc()
            try:
                if request_id:
                    db.add_request_event(request_id, "error", details=str(e))
                    db.update_request_status(request_id, "failed")
            except Exception:
                pass
        finally:
            try:
                # Ensure we always try to clear the eyes reaction and delete the status
                # message regardless of how the processing ended.
                try:
                    if reaction_current == "eyes" and original_message_id:
                        try:
                            # final cleanup: if still showing eyes, mark as failure
                            await telegram_api.set_message_reaction(
                                self.token,
                                chat_id,
                                original_message_id,
                                "🍌",
                            )
                            reaction_current = "banana"
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    if status_msg_id:
                        try:
                            await telegram_api.delete_message(
                                self.token, chat_id, status_msg_id
                            )
                        except Exception:
                            try:
                                await telegram_api.edit_message_text(
                                    self.token, chat_id, status_msg_id, ""
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
            finally:
                try:
                    if getattr(config, "KEEP_DOWNLOADED_FILES", False):
                        logger.info(
                            "Preserving tmpdir per KEEP_DOWNLOADED_FILES: %s", tmpdir
                        )
                    else:
                        shutil.rmtree(tmpdir)
                except Exception:
                    pass
