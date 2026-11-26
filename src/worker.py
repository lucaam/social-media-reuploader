"""Worker pool: accepts links and processes the DB-backed request queue.

The ``requests`` table IS the queue (there is no in-memory copy of it):

* :meth:`WorkerPool.submit` decides, serialized per chat, what happens to a
  new link — ignored (same message already handled), ``duplicate`` (same link
  in the chat within ``DUPLICATE_WINDOW_SECONDS``), refused by the rate
  limiter (``rate_limited`` / pending cap) or accepted (``queued``, with
  ``not_before`` when deferred) — and stores one row for it. Refusals never
  consume quota and never block the chat for other links.
* The dispatcher claims due rows atomically (``db.claim_next_request``) up to
  ``WORKERS`` at a time. It wakes on :meth:`WorkerPool.wake`, when a job ends,
  when a deferred row becomes due and every ``WORKER_POLL_SECONDS``, so rows
  requeued by another process (the admin GUI) are picked up too.
* Each claimed row is processed by :mod:`src.pipeline`.
* A heartbeat task publishes the pool status (``db.set_worker_status``) for
  the GUI; a maintenance task prunes old history daily and removes stale work
  directories from ``TMP_DIR``.
* A job outcome the database refused (``pipeline.Settlement``) and the row of
  a job that crashed are settled in the background, so no row stays
  ``processing``/``sending`` (holding a pending slot of its chat) until the
  next start.

Lifecycle: ``WorkerPool(token)`` starts nothing; ``await pool.start()``
recovers requests interrupted by a previous process and starts the tasks;
``await pool.stop()`` stops claiming, waits for running jobs up to
``WORKER_SHUTDOWN_TIMEOUT`` seconds, then cancels them (their rows go back to
the queue, see :mod:`src.pipeline`). Everything runs on one event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import shutil
import sqlite3
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Collection, Coroutine, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from . import (
    __version__,
    config,
    db,
    messages,
    metrics,
    pipeline,
    providers,
    telegram_api,
)
from .providers import ProviderMatch
from .ratelimit import Decision, Outcome, RateLimiter

logger = logging.getLogger(__name__)

# Maintenance: first run shortly after start, then hourly; prune once a day.
MAINTENANCE_FIRST_RUN_SECONDS = 60.0
MAINTENANCE_INTERVAL_SECONDS = 3600.0
PRUNE_INTERVAL_SECONDS = 86400.0
# A work directory no running job owns is removed once it is this old (it
# belongs to a job that crashed). With KEEP_DOWNLOADED_FILES they are kept
# for TMP_RETENTION_HOURS instead.
STALE_WORKDIR_SECONDS = 3600.0
DEFAULT_TMP_RETENTION_HOURS = 24.0
# stop(): how long cancelled jobs get to put their rows back, and how long
# pending user notices may still be sent.
CANCEL_GRACE_SECONDS = 15.0
NOTICE_GRACE_SECONDS = 2.0
# Deferred-link notices are sent once per message; remember this many (and
# the last pending-cap notice of this many chats).
DEFERRED_NOTICE_MEMORY = 1024
# Timers for deferred rows fire slightly after their not_before.
WAKE_DELAY_EPSILON = 0.05
# Outcomes the database refused are written again after this many seconds,
# doubling up to the maximum, and once more when the pool stops.
SETTLE_RETRY_SECONDS = 5.0
SETTLE_MAX_RETRY_SECONDS = 120.0

_DEFAULTS: dict[str, Any] = {
    "WORKERS": 2,
    "WORKER_POLL_SECONDS": 2.0,
    "WORKER_HEARTBEAT_SECONDS": 5.0,
    "WORKER_SHUTDOWN_TIMEOUT": 30,
    "WORKER_MAX_ATTEMPTS": 3,
    "WORKER_REHYDRATE_ON_START": True,
    "DUPLICATE_WINDOW_SECONDS": 90 * 60,
    "NOTIFY_RATE_THROTTLE_SECONDS": 60,
    "NOTIFY_DUPLICATE_THROTTLE_SECONDS": 600,
    "UPDATES_RETENTION_DAYS": 14,
    "REQUESTS_RETENTION_DAYS": 0,
    "TMP_RETENTION_HOURS": DEFAULT_TMP_RETENTION_HOURS,
    "MODE": "polling",
}


def _setting(name: str) -> Any:
    """``config.<name>`` read at call time (tests patch config), or the default."""
    value = getattr(config, name, None)
    return _DEFAULTS.get(name) if value is None else value


def _float_setting(name: str) -> float:
    try:
        return float(_setting(name))
    except (TypeError, ValueError):
        return float(_DEFAULTS[name])


def _effective_mode() -> str:
    """``webhook`` only when it is configured with a URL (like the entrypoint)."""
    mode = str(_setting("MODE") or "polling").lower()
    if mode == "webhook" and getattr(config, "WEBHOOK_URL", None):
        return "webhook"
    return "polling"


def _is_private(chat_type: str | None, chat_id: int) -> bool:
    if chat_type:
        return chat_type == "private"
    return chat_id > 0  # user ids are positive, group/channel ids negative


class SubmitStatus(StrEnum):
    QUEUED = "queued"  # accepted, processed as soon as a slot is free
    DEFERRED = "deferred"  # accepted, processed after delay_seconds (rate limits)
    DUPLICATE = "duplicate"  # same link in this chat within the duplicate window
    RATE_LIMITED = "rate_limited"  # refused; retry after delay_seconds
    PENDING_CAP = "pending_cap"  # refused: too many links of the chat pending
    UNSUPPORTED = "unsupported"  # no provider can download this link
    IGNORED = "ignored"  # this message + link was already handled (edit/redelivery)
    CLOSED = "closed"  # the pool is stopping or stopped


@dataclass(frozen=True)
class SubmitResult:
    status: SubmitStatus
    request_id: int | None = None
    delay_seconds: float = 0.0
    # RATE_LIMITED/DEFERRED: the (count, seconds) limit responsible (additive).
    window: tuple[int, int] | None = None

    @property
    def accepted(self) -> bool:
        return self.status in (SubmitStatus.QUEUED, SubmitStatus.DEFERRED)


@dataclass
class _ChatLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@dataclass
class _Job:
    state: pipeline.JobState
    task: asyncio.Task[pipeline.JobResult]


def sweep_workdirs(root: str, *, keep: Collection[str], older_than: float) -> int:
    """Remove ``<root>/smr-*`` directories older than ``older_than`` seconds.

    Directories in ``keep`` (running jobs) and anything not created by the
    pipeline are never touched. Returns how many were removed.
    """
    removed = 0
    now = time.time()
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return 0
    for entry in entries:
        if not entry.name.startswith(pipeline.WORKDIR_PREFIX) or entry.path in keep:
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            age = now - entry.stat(follow_symlinks=False).st_mtime
        except FileNotFoundError:
            continue
        if age < older_than:
            continue
        try:
            shutil.rmtree(entry.path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Cannot remove %s: %s", entry.path, exc.strerror or exc)
            continue
        removed += 1
    return removed


def _make_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)


def recover_crashed(request_id: int, *, max_attempts: int) -> bool:
    """Settle the row of a job that crashed, like a restart would (blocking).

    ``processing`` goes back to the queue (``failed``/``interrupted`` once
    ``max_attempts`` are used); ``sending`` becomes ``failed``/
    ``interrupted_while_sending``: the upload may have happened, it is never
    resent. See :func:`src.db.recover_interrupted`.
    """
    row = db.get_request(request_id)
    if row is None:
        return False
    if row["status"] == db.Status.SENDING:
        return db.mark_failed(
            request_id,
            error_code="interrupted_while_sending",
            error_message="the job crashed during the upload; not resent",
        )
    if row["status"] != db.Status.PROCESSING:
        return False
    attempts = int(row["attempts"] or 0)
    if attempts >= max_attempts:
        return db.mark_failed(
            request_id,
            error_code="interrupted",
            error_message=f"the job crashed (attempt {attempts}); giving up",
        )
    retry_at = time.time() + pipeline.retry_delay(attempts)
    return db.defer_request(
        request_id, db.iso_from_ts(retry_at), reason="the job crashed"
    )


class WorkerPool:
    """Accepts links (:meth:`submit`) and processes the queue (see module doc)."""

    def __init__(self, token: str, workers: int | None = None) -> None:
        if workers is None:
            workers = int(_setting("WORKERS"))
        self.token = token
        self.workers = max(1, int(workers))
        self.limiter = RateLimiter()
        self._chat_locks: dict[int, _ChatLock] = {}
        self._jobs: dict[int, _Job] = {}
        # Outcomes still to be written, by request id (see _settle_loop).
        self._unsettled: dict[int, pipeline.Settlement] = {}
        self._settler: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._timers: set[asyncio.TimerHandle] = set()
        self._deferred_notices: OrderedDict[tuple[int, int], None] = OrderedDict()
        # chat id -> monotonic time of its last pending-cap notice
        self._pending_notices: OrderedDict[int, float] = OrderedDict()
        self._dispatcher: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._maintenance: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._wake_event: asyncio.Event | None = None
        self._halt_event: asyncio.Event | None = None  # stops the heartbeat
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._closing = False
        self._stopped = False
        self._started_at: str | None = None
        self._last_prune: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._started and not self._stopped

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def running(self) -> int:
        """Jobs running in this process."""
        return len(self._jobs)

    async def start(self) -> None:
        """Recover interrupted requests, then start dispatching (idempotent)."""
        if self._closing:
            raise RuntimeError("this WorkerPool was stopped; create a new one")
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()
        self._halt_event = asyncio.Event()
        self._started_at = db.now_iso()
        try:
            await self._prepare_start()
        except BaseException:
            self._started = False
            raise
        if self._closing:
            return  # stop() was called while starting: start nothing
        self._dispatcher = self._spawn(self._dispatch_loop(), "worker-dispatcher")
        self._heartbeat = self._spawn(self._heartbeat_loop(), "worker-heartbeat")
        self._maintenance = self._spawn(self._maintenance_loop(), "worker-maintenance")
        logger.info(
            "Worker pool started: %d slot(s), poll every %.1fs, mode %s",
            self.workers,
            _float_setting("WORKER_POLL_SECONDS"),
            _effective_mode(),
        )

    async def _prepare_start(self) -> None:
        await db.aio(db.init_db)
        # Rows the last process was working on carry its 👀 reaction.
        interrupted = await db.aio(
            db.list_requests,
            db.MAX_PAGE_SIZE,
            status=(db.Status.PROCESSING, db.Status.SENDING),
        )
        counts = await db.aio(
            db.recover_interrupted,
            rehydrate=bool(_setting("WORKER_REHYDRATE_ON_START")),
            max_attempts=int(_setting("WORKER_MAX_ATTEMPTS")),
        )
        if any(counts.values()):
            logger.info("Recovered requests interrupted by the last run: %s", counts)
        await self._clear_stale_reactions(interrupted)
        root = str(pipeline.setting("TMP_DIR"))
        try:
            await asyncio.to_thread(_make_dir, root)
        except OSError as exc:
            logger.error("Cannot create TMP_DIR %s: %s", root, exc.strerror or exc)
            return
        await self._sweep_workdirs(startup=True)

    async def _clear_stale_reactions(self, rows: list[dict[str, Any]]) -> None:
        """Remove the 👀 of interrupted rows the recovery settled for good
        (failed or aborted): nothing would ever replace it. Requeued rows
        keep it; they are processed again."""
        for row in rows:
            current = await db.aio(db.get_request, int(row["id"]))
            if current is None or current["status"] not in db.TERMINAL_STATUSES:
                continue
            chat_id, message_id = current["chat_id"], current["original_message_id"]
            if isinstance(chat_id, int) and isinstance(message_id, int):
                self._spawn(
                    self._clear_reaction(chat_id, message_id), f"unreact-{chat_id}"
                )

    async def _clear_reaction(self, chat_id: int, message_id: int) -> None:
        result = await telegram_api.set_message_reaction(
            self.token, chat_id, message_id, None
        )
        if not result.get("ok"):
            logger.info(
                "Cannot remove the reaction on message %s of chat %s: %s",
                message_id,
                chat_id,
                telegram_api.redact(result.get("description"), self.token),
            )

    async def stop(
        self,
        timeout: float | None = None,  # noqa: ASYNC109, RUF100
    ) -> None:
        """Stop claiming, wait for running jobs, then cancel them (idempotent).

        ``timeout`` defaults to ``config.WORKER_SHUTDOWN_TIMEOUT``. Cancelled
        jobs put their rows back in the queue (or mark an interrupted upload
        failed), so the next start resumes them.
        """
        if self._stopped:
            return
        if self._stop_task is None:
            self._stop_task = asyncio.get_running_loop().create_task(
                self._shutdown(timeout), name="worker-stop"
            )
        await asyncio.shield(self._stop_task)

    shutdown = stop  # backwards-compatible name

    async def _shutdown(self, timeout: float | None) -> None:
        if timeout is None:
            timeout = _float_setting("WORKER_SHUTDOWN_TIMEOUT")
        timeout = max(0.0, float(timeout))
        self._closing = True
        for handle in self._timers:
            handle.cancel()
        self._timers.clear()
        if self._wake_event is not None:
            self._wake_event.set()
        if self._dispatcher is not None:
            await self._finish(self._dispatcher, max(timeout, 1.0))
        await self._drain_jobs(timeout)
        if self._maintenance is not None:
            self._maintenance.cancel()
        if self._halt_event is not None:
            # Let the heartbeat finish its current write: a cancelled task
            # would leave the write running in its thread, racing "stopped".
            self._halt_event.set()
        await self._finish_notices()
        # The halt event also asks the settler for its last attempt.
        background = [
            t
            for t in (self._heartbeat, self._maintenance, self._settler)
            if t is not None
        ]
        if background:
            _done, pending = await asyncio.wait(
                background, timeout=CANCEL_GRACE_SECONDS
            )
            for task in pending:
                task.cancel()
        if self._started:
            await self._publish_status(state="stopped")
        self._stopped = True
        logger.info("Worker pool stopped")

    async def _finish(self, task: asyncio.Task[Any], timeout: float) -> None:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            await asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS)

    async def _drain_jobs(self, timeout: float) -> None:
        tasks = [job.task for job in self._jobs.values()]
        if not tasks:
            return
        logger.info("Waiting up to %.0fs for %d running job(s)", timeout, len(tasks))
        pending: set[asyncio.Task[Any]] = set(tasks)
        if timeout > 0:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
        if not pending:
            return
        logger.warning(
            "Cancelling %d job(s) still running after %.0fs: %s",
            len(pending),
            timeout,
            sorted(job.state.request_id for job in self._jobs.values()),
        )
        for task in pending:
            task.cancel()
        _done, still = await asyncio.wait(pending, timeout=CANCEL_GRACE_SECONDS)
        if still:
            logger.error("%d job(s) did not stop after cancellation", len(still))

    async def _finish_notices(self) -> None:
        own = (self._dispatcher, self._heartbeat, self._maintenance, self._settler)
        notices = [task for task in self._tasks if task not in own]
        if not notices:
            return
        _done, pending = await asyncio.wait(notices, timeout=NOTICE_GRACE_SECONDS)
        for task in pending:
            task.cancel()

    # ------------------------------------------------------------------
    # Tasks and wake-ups
    # ------------------------------------------------------------------
    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Task %s failed", task.get_name(), exc_info=exc)

    def wake(self) -> None:
        """Make the dispatcher look for due requests now (thread-safe)."""
        event, loop = self._wake_event, self._loop
        if event is None or loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            event.set()
        else:
            loop.call_soon_threadsafe(event.set)

    def _wake_at(self, when: float) -> None:
        """Wake the dispatcher at epoch ``when`` (a deferred row becomes due)."""
        loop = self._loop
        if loop is None or self._closing:
            return
        delay = max(0.0, when - time.time()) + WAKE_DELAY_EPSILON
        handles: list[asyncio.TimerHandle] = []

        def fire() -> None:
            self._timers.discard(handles[0])
            self.wake()

        handle = loop.call_later(delay, fire)
        handles.append(handle)
        self._timers.add(handle)

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def _chat_lock(self, chat_id: int) -> AsyncIterator[None]:
        """Serialize submits of one chat (check + create must not interleave)."""
        entry = self._chat_locks.get(chat_id)
        if entry is None:
            entry = self._chat_locks[chat_id] = _ChatLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._chat_locks.get(chat_id) is entry:
                del self._chat_locks[chat_id]

    async def submit(
        self,
        chat_id: int,
        url: str,
        *,
        chat_type: str | None = None,
        user_id: int | None = None,
        original_message_id: int | None = None,
        description: str | None = None,
        provider: ProviderMatch | None = None,
        edited_message_date: float | None = None,
    ) -> SubmitResult:
        """Record a link of a chat and decide its fate (see the module doc).

        ``provider`` is the :class:`ProviderMatch` of ``url`` when the caller
        already has it (otherwise it is looked up). ``edited_message_date``:
        for an edited message, the epoch date of the original message; edits
        of messages older than the duplicate window are ignored (their rows
        may have been pruned). Private chats get a short Italian notice for
        refusals and deferrals (throttled); groups and channels never do.
        """
        result = await self._submit(
            chat_id,
            url,
            chat_type=chat_type,
            user_id=user_id,
            original_message_id=original_message_id,
            description=description,
            provider=provider,
            edited_message_date=edited_message_date,
        )
        metrics.record_submit(result.status)
        logger.info(
            "Link from chat %s (message %s): %s%s",
            chat_id,
            original_message_id,
            result.status,
            f" (request {result.request_id})" if result.request_id else "",
        )
        return result

    async def _submit(
        self,
        chat_id: int,
        url: str,
        *,
        chat_type: str | None,
        user_id: int | None,
        original_message_id: int | None,
        description: str | None,
        provider: ProviderMatch | None,
        edited_message_date: float | None,
    ) -> SubmitResult:
        if self._closing:
            return SubmitResult(SubmitStatus.CLOSED)
        match = provider or await providers.match_provider_async(url)
        if match is None:
            return SubmitResult(SubmitStatus.UNSUPPORTED)
        window = _float_setting("DUPLICATE_WINDOW_SECONDS")
        if self._stale_edit(edited_message_date, window):
            return SubmitResult(SubmitStatus.IGNORED)
        fields: dict[str, Any] = {
            "normalized_url": match.normalized_url,
            "provider": match.key,
            "chat_type": chat_type,
            "user_id": user_id,
            "description": (description or "").strip() or None,
            "original_message_id": original_message_id,
        }
        async with self._chat_lock(chat_id):
            now = time.time()
            existing = await db.aio(
                db.find_request_for_message,
                chat_id,
                original_message_id,
                match.normalized_url,
            )
            if existing is not None:
                return SubmitResult(SubmitStatus.IGNORED, int(existing["id"]))
            earlier = None
            if window > 0:
                earlier = await db.aio(
                    db.find_recent_duplicate,
                    chat_id,
                    match.normalized_url,
                    db.iso_from_ts(now - window),
                )
            if earlier is not None:
                result = await self._record_duplicate(chat_id, match, earlier, fields)
            else:
                decision = await db.aio(self.limiter.check, chat_id, now)
                result = await self._apply_decision(
                    chat_id, match, decision, now, fields
                )
        self._after_submit(result, chat_id, chat_type, original_message_id, earlier)
        return result

    @staticmethod
    def _stale_edit(message_date: float | None, window: float) -> bool:
        """An edit of a message older than the duplicate window (0 = no limit)."""
        if message_date is None or window <= 0:
            return False
        return time.time() - float(message_date) > window

    async def _record_duplicate(
        self,
        chat_id: int,
        match: ProviderMatch,
        earlier: Mapping[str, Any],
        fields: Mapping[str, Any],
    ) -> SubmitResult:
        request_id = await db.aio(
            db.create_request,
            chat_id,
            match.url,
            status=db.Status.DUPLICATE,
            error_code="duplicate",
            error_message=f"same link as request {earlier['id']}",
            **fields,
        )
        return SubmitResult(SubmitStatus.DUPLICATE, request_id)

    async def _apply_decision(
        self,
        chat_id: int,
        match: ProviderMatch,
        decision: Decision,
        now: float,
        fields: Mapping[str, Any],
    ) -> SubmitResult:
        delay = max(0.0, float(decision.delay_seconds))
        if decision.outcome is Outcome.ACCEPT:
            request_id = await db.aio(db.create_request, chat_id, match.url, **fields)
            return SubmitResult(SubmitStatus.QUEUED, request_id)
        if decision.outcome is Outcome.DEFER:
            request_id = await db.aio(
                db.create_request,
                chat_id,
                match.url,
                not_before=db.iso_from_ts(now + delay),
                **fields,
            )
            return SubmitResult(
                SubmitStatus.DEFERRED, request_id, delay, decision.window
            )
        if decision.outcome is Outcome.RATE_LIMITED:
            limit = f" by the {decision.window} limit" if decision.window else ""
            request_id = await db.aio(
                db.create_request,
                chat_id,
                match.url,
                status=db.Status.RATE_LIMITED,
                error_code="rate_limited",
                error_message=f"rate limited{limit}; next slot in {delay:.0f}s",
                **fields,
            )
            await db.aio(
                db.set_chat_blocked_until, chat_id, db.iso_from_ts(now + delay)
            )
            return SubmitResult(
                SubmitStatus.RATE_LIMITED, request_id, delay, decision.window
            )
        request_id = await db.aio(
            db.create_request,
            chat_id,
            match.url,
            status=db.Status.RATE_LIMITED,
            error_code="pending_cap",
            error_message=f"{self.limiter.max_pending} link(s) of the chat already pending",
            **fields,
        )
        return SubmitResult(SubmitStatus.PENDING_CAP, request_id)

    def _after_submit(
        self,
        result: SubmitResult,
        chat_id: int,
        chat_type: str | None,
        message_id: int | None,
        earlier: Mapping[str, Any] | None,
    ) -> None:
        status = result.status
        if status is SubmitStatus.QUEUED:
            self.wake()
        elif status is SubmitStatus.DEFERRED:
            self._wake_at(time.time() + result.delay_seconds)
        if not _is_private(chat_type, chat_id) or self._closing:
            return
        if status is SubmitStatus.DEFERRED:
            if self._first_deferral_of(chat_id, message_id):
                self._notice(
                    chat_id, messages.deferred(result.delay_seconds), message_id
                )
        elif status is SubmitStatus.RATE_LIMITED:
            self._notice(
                chat_id,
                messages.rate_limited(result.delay_seconds, result.window),
                message_id,
                kind="rate",
            )
        elif status is SubmitStatus.PENDING_CAP:
            if self._pending_notice_due(chat_id):
                self._notice(
                    chat_id, messages.pending_cap(self.limiter.max_pending), message_id
                )
        elif status is SubmitStatus.DUPLICATE and earlier is not None:
            in_progress = earlier.get("status") in db.ACTIVE_STATUSES
            reply_to = earlier.get("sent_message_id") or message_id
            self._notice(
                chat_id,
                messages.duplicate(in_progress=in_progress),
                reply_to,
                kind="duplicate",
            )

    def _first_deferral_of(self, chat_id: int, message_id: int | None) -> bool:
        if message_id is None:
            return True
        key = (chat_id, message_id)
        if key in self._deferred_notices:
            return False
        self._deferred_notices[key] = None
        while len(self._deferred_notices) > DEFERRED_NOTICE_MEMORY:
            self._deferred_notices.popitem(last=False)
        return True

    def _pending_notice_due(self, chat_id: int) -> bool:
        """Pending-cap notices are throttled on their own (in memory: the db
        throttle only knows "rate" and "duplicate"). Sharing "rate" let one
        silence a later rate-limit notice, the only place that says when the
        user may send links again."""
        now = time.monotonic()
        last = self._pending_notices.get(chat_id)
        if last is not None and now - last < _float_setting(
            "NOTIFY_RATE_THROTTLE_SECONDS"
        ):
            return False
        self._pending_notices[chat_id] = now
        self._pending_notices.move_to_end(chat_id)
        while len(self._pending_notices) > DEFERRED_NOTICE_MEMORY:
            self._pending_notices.popitem(last=False)
        return True

    def _notice(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None,
        *,
        kind: str | None = None,
    ) -> None:
        """Send a short notice in the background (throttled by ``kind``)."""
        self._spawn(
            self._send_notice(chat_id, text, reply_to, kind),
            f"notice-{chat_id}",
        )

    async def _send_notice(
        self, chat_id: int, text: str, reply_to: int | None, kind: str | None
    ) -> None:
        if kind is not None:
            throttle = _float_setting(
                "NOTIFY_RATE_THROTTLE_SECONDS"
                if kind == "rate"
                else "NOTIFY_DUPLICATE_THROTTLE_SECONDS"
            )
            if not await db.aio(db.should_notify, chat_id, kind, throttle):
                return
        result = await telegram_api.send_message(
            self.token, chat_id, text, reply_to_message_id=reply_to
        )
        if not result.get("ok"):
            logger.info(
                "Cannot notify chat %s: %s",
                chat_id,
                telegram_api.redact(result.get("description"), self.token),
            )

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------
    async def _dispatch_loop(self) -> None:
        event = self._wake_event
        if event is None:
            raise RuntimeError("the dispatcher runs only after start()")
        while not self._closing:
            event.clear()
            try:
                await self._fill_slots()
            except sqlite3.Error:
                logger.warning("Cannot claim requests; retrying", exc_info=True)
            except Exception:  # task boundary: keep dispatching
                logger.exception("Dispatcher error; retrying")
            if self._closing:
                break
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_float_setting("WORKER_POLL_SECONDS")):
                    await event.wait()

    async def _fill_slots(self) -> None:
        while not self._closing and len(self._jobs) < self.workers:
            row = await db.aio(db.claim_next_request)
            if row is None:
                return
            if self._closing:
                # Claimed while stopping: give it back untouched.
                await db.aio(
                    db.defer_request, row["id"], db.now_iso(), reason="worker stopping"
                )
                return
            self._start_job(row)

    def _start_job(self, row: Mapping[str, Any]) -> None:
        state = pipeline.JobState.from_row(row)
        task = asyncio.get_running_loop().create_task(
            self._run_job(row, state), name=f"request-{state.request_id}"
        )
        self._jobs[state.request_id] = _Job(state, task)
        task.add_done_callback(lambda done: self._job_finished(state, done))

    async def _run_job(
        self, row: Mapping[str, Any], state: pipeline.JobState
    ) -> pipeline.JobResult:
        result = await pipeline.process(self.token, row, state)
        if result.settlement is not None:
            self._settle_later(result.settlement)
        if result.retry_at is not None:
            self._wake_at(result.retry_at)
        metrics.record_job(
            result.outcome,
            time.monotonic() - state.started_monotonic,
            error_code=result.error_code,
            error_phase=result.error_phase,
            method=result.method,
            sent=result.sent,
            bytes_sent=result.bytes_sent,
        )
        return result

    def _job_finished(
        self, state: pipeline.JobState, task: asyncio.Task[pipeline.JobResult]
    ) -> None:
        job = self._jobs.get(state.request_id)
        if job is not None and job.task is task:
            del self._jobs[state.request_id]
        self.wake()
        elapsed = time.monotonic() - state.started_monotonic
        if task.cancelled():
            metrics.record_job("interrupted", elapsed)
            return
        exc = task.exception()
        if exc is not None:
            metrics.record_job("crashed", elapsed)
            logger.error("Request %s: job crashed", state.request_id, exc_info=exc)
            # Its row is still processing/sending: settle it as a restart would.
            recover = functools.partial(
                recover_crashed,
                state.request_id,
                max_attempts=int(_setting("WORKER_MAX_ATTEMPTS")),
            )
            self._settle_later(
                pipeline.Settlement(state.request_id, "interrupted", recover)
            )

    # ------------------------------------------------------------------
    # Settling outcomes the database refused
    # ------------------------------------------------------------------
    def _settle_later(self, settlement: pipeline.Settlement) -> None:
        """Write ``settlement`` in the background, retrying until it works."""
        self._unsettled[settlement.request_id] = settlement
        if self._settler is None or self._settler.done():
            self._settler = self._spawn(self._settle_loop(), "worker-settle")

    async def _settle_loop(self) -> None:
        halt = self._halt_event
        if halt is None:
            raise RuntimeError("the settler runs only after start()")
        delay = SETTLE_RETRY_SECONDS
        try:
            while self._unsettled:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(delay):
                        await halt.wait()  # stopping: one last attempt now
                await self._settle_pending()
                if halt.is_set():
                    break
                delay = min(2 * delay, SETTLE_MAX_RETRY_SECONDS)
        finally:
            for request_id, settlement in self._unsettled.items():
                logger.error(
                    "Request %s: its %s outcome may not be recorded; the next "
                    "start settles the row as interrupted",
                    request_id,
                    settlement.outcome,
                )

    async def _settle_pending(self) -> None:
        for request_id, settlement in list(self._unsettled.items()):
            try:
                recorded = await db.aio(settlement.write)
            except sqlite3.Error as exc:
                logger.warning(
                    "Request %s: cannot record its %s outcome yet: %s",
                    request_id,
                    settlement.outcome,
                    exc,
                )
                continue
            if self._unsettled.get(request_id) is settlement:
                del self._unsettled[request_id]
            logger.info(
                "Request %s: %s outcome recorded%s",
                request_id,
                settlement.outcome,
                "" if recorded else " (the row was no longer active)",
            )
            self.wake()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------
    def status_payload(
        self, counts: Mapping[str, int] | None = None, *, state: str | None = None
    ) -> dict[str, Any]:
        """The worker status published for the GUI (``db.set_worker_status``)."""
        counts = counts or {}
        if state is None:
            state = "stopping" if self._closing else "running"
        return {
            "pid": os.getpid(),
            "version": __version__,
            "started_at": self._started_at,
            "state": state,
            "mode": _effective_mode(),
            "workers": self.workers,
            "running": [job.state.as_status() for job in self._jobs.values()],
            "queued": int(counts.get("queued", 0)),
            "deferred": int(counts.get("deferred", 0)),
            "rate_limits": [list(limit) for limit in self.limiter.limits],
            "rate_limit_max_defer_seconds": self.limiter.max_defer,
            "max_pending_per_chat": self.limiter.max_pending,
            "duplicate_window_seconds": int(_float_setting("DUPLICATE_WINDOW_SECONDS")),
            "max_attempts": int(_setting("WORKER_MAX_ATTEMPTS")),
            "poll_seconds": _float_setting("WORKER_POLL_SECONDS"),
            "heartbeat_seconds": _float_setting("WORKER_HEARTBEAT_SECONDS"),
        }

    async def _publish_status(self, *, state: str | None = None) -> None:
        try:
            snapshot = await db.aio(db.queue_snapshot, 0)
            counts = snapshot["counts"]
            await db.aio(db.set_worker_status, self.status_payload(counts, state=state))
        except sqlite3.Error:
            logger.warning("Cannot publish the worker status", exc_info=True)
            return
        metrics.set_queue_depth(
            queued=int(counts.get("queued", 0)),
            deferred=int(counts.get("deferred", 0)),
            running=len(self._jobs),
        )

    async def _heartbeat_loop(self) -> None:
        halt = self._halt_event
        if halt is None:
            raise RuntimeError("the heartbeat runs only after start()")
        while not halt.is_set():
            try:
                await self._publish_status()
            except Exception:  # task boundary: keep publishing
                logger.exception("Heartbeat error")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_float_setting("WORKER_HEARTBEAT_SECONDS")):
                    await halt.wait()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(MAINTENANCE_FIRST_RUN_SECONDS)
        while True:
            try:
                await self.run_maintenance()
            except Exception:  # task boundary: try again next time
                logger.exception("Maintenance error")
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)

    async def run_maintenance(self, *, force_prune: bool = False) -> dict[str, Any]:
        """Sweep stale work directories; prune the history once a day."""
        removed = await self._sweep_workdirs(startup=False)
        pruned: dict[str, int] | None = None
        now = time.monotonic()
        due = (
            self._last_prune is None or now - self._last_prune >= PRUNE_INTERVAL_SECONDS
        )
        if force_prune or due:
            try:
                pruned = await db.aio(
                    db.prune,
                    updates_days=_float_setting("UPDATES_RETENTION_DAYS"),
                    requests_days=_float_setting("REQUESTS_RETENTION_DAYS"),
                )
            except sqlite3.Error:
                logger.warning("Cannot prune the history", exc_info=True)
            else:
                self._last_prune = now
                if any(pruned.values()):
                    logger.info("Pruned history: %s", pruned)
        return {"workdirs_removed": removed, "pruned": pruned}

    async def _sweep_workdirs(self, *, startup: bool) -> int:
        root = str(pipeline.setting("TMP_DIR"))
        keep = {job.state.workdir for job in self._jobs.values() if job.state.workdir}
        if pipeline.setting("KEEP_DOWNLOADED_FILES"):
            older_than = _float_setting("TMP_RETENTION_HOURS") * 3600
        else:
            # At start no job of a previous process can still be running.
            older_than = 0.0 if startup else STALE_WORKDIR_SECONDS
        try:
            removed = await asyncio.to_thread(
                sweep_workdirs, root, keep=keep, older_than=older_than
            )
        except OSError as exc:
            logger.warning("Cannot sweep %s: %s", root, exc.strerror or exc)
            return 0
        if removed:
            logger.info("Removed %d stale work director(ies) from %s", removed, root)
        return removed
