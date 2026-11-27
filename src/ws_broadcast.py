"""Live updates for the admin GUI: one DB event-log tailer, many WebSocket clients.

The bot and the GUI are separate processes; the only channel between them is
the event log in the database (``db.add_event`` / ``list_events_after``). The
GUI runs ONE :class:`EventTailer` task (started by the FastAPI lifespan) that
starts at ``db.latest_event_id()`` (no history replay), reads new events in
order and hands each batch to the :class:`Hub` exactly once. The hub fans the
messages out to bounded per-client queues; a client that falls behind is
dropped (its socket is closed and the browser reconnects and resyncs) instead
of growing memory without limit.

Messages sent to clients:

* ``{"kind": "events", "events": [...], "last_id": N}`` - a batch of event-log
  items in id order (each item is ``{"id", "created_at", "type", ...}``);
* ``{"kind": "worker", "worker": {...}}`` - worker heartbeat summary, sent
  when its state or heartbeat changes;
* ``{"kind": "hello", ...}`` - sent by the GUI right after the handshake.

All Hub methods must be called from the event loop thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import db

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 64
DEFAULT_BATCH_SIZE = 500
MAX_BACKOFF_SECONDS = 30.0
# After this many empty polls the tailer checks whether the log was reset.
_RESET_CHECK_EVERY = 30


class Subscription:
    """One client's bounded message queue."""

    __slots__ = ("_queue", "closed", "dropped")

    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize)
        self.closed = False
        self.dropped = False

    def offer(self, message: dict[str, Any]) -> bool:
        """Queue ``message`` without waiting; on overflow drop the client."""
        if self.closed:
            return False
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped = True
            self.close()
            return False
        return True

    def close(self) -> None:
        """Discard pending messages and wake the reader with the end marker."""
        if self.closed and not self._queue.empty():
            return
        self.closed = True
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    async def get(self) -> dict[str, Any] | None:
        """Next message, or None once the subscription is closed."""
        if self.closed and self._queue.empty():
            return None
        return await self._queue.get()

    def pending(self) -> int:
        return self._queue.qsize()


class Hub:
    """Fan-out of messages to the connected WebSocket clients."""

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.queue_size = max(1, int(queue_size))
        self._subscriptions: set[Subscription] = set()

    @property
    def client_count(self) -> int:
        return len(self._subscriptions)

    def subscribe(self) -> Subscription:
        subscription = Subscription(self.queue_size)
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscriptions.discard(subscription)
        if not subscription.closed:
            subscription.close()

    def publish(self, message: dict[str, Any]) -> int:
        """Offer ``message`` to every client; returns how many accepted it."""
        delivered = 0
        for subscription in list(self._subscriptions):
            if subscription.offer(message):
                delivered += 1
            elif subscription.dropped:
                logger.warning("Dropping a slow live-update client (queue full)")
                self._subscriptions.discard(subscription)
        return delivered

    def close_all(self) -> None:
        for subscription in list(self._subscriptions):
            subscription.close()
        self._subscriptions.clear()


EventSource = Callable[[int, int], list[dict[str, Any]]]
CursorSource = Callable[[], int]
StatusSource = Callable[[], Mapping[str, Any] | None]
EventShaper = Callable[[Mapping[str, Any]], dict[str, Any]]


class EventTailer:
    """Tail the DB event log and publish each new event exactly once."""

    def __init__(
        self,
        hub: Hub,
        *,
        poll_interval: float = 1.0,
        batch_size: int = DEFAULT_BATCH_SIZE,
        status_interval: float = 5.0,
        status_source: StatusSource | None = None,
        shape_event: EventShaper | None = None,
        list_events_after: EventSource = db.list_events_after,
        latest_event_id: CursorSource = db.latest_event_id,
    ) -> None:
        self.hub = hub
        self.poll_interval = max(0.05, float(poll_interval))
        self.batch_size = max(1, int(batch_size))
        self.status_interval = max(0.1, float(status_interval))
        self._status_source = status_source
        self._shape = shape_event or dict
        self._list_events_after = list_events_after
        self._latest_event_id = latest_event_id
        self.last_id: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None  # created in the running loop
        self._status_key: tuple[Any, ...] | None = None
        self._next_status_at = 0.0
        self._empty_polls = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the background task (idempotent). Must run inside the loop.

        The cursor is read here when possible, so every event written after
        start() returns is delivered; on a DB error the task keeps retrying.
        """
        if self.running:
            return
        self._wake = asyncio.Event()
        if self.last_id is None:
            try:
                self.last_id = int(await asyncio.to_thread(self._latest_event_id))
            except sqlite3.Error:
                logger.warning(
                    "Cannot read the event log cursor yet; retrying in the background",
                    exc_info=True,
                )
        self._task = asyncio.create_task(self.run(), name="gui-event-tailer")
        self._task.add_done_callback(_log_task_end)

    async def stop(self) -> None:
        """Cancel the task; a later start() re-reads the cursor."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.last_id = None
        self._status_key = None
        self._next_status_at = 0.0

    def wake(self) -> None:
        """Poll now instead of waiting for the next interval."""
        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
        await self.init_cursor()
        backoff = self.poll_interval
        while True:
            try:
                published = await self.poll_once()
                await self.publish_status()
            except sqlite3.Error:
                logger.warning(
                    "Event log poll failed; retrying in %.1fs", backoff, exc_info=True
                )
            except Exception:  # task-loop boundary: keep live updates alive
                logger.exception("Unexpected error in the event tailer")
            else:
                backoff = self.poll_interval
                if published < self.batch_size:
                    await self._sleep(self.poll_interval)
                continue
            await self._sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def init_cursor(self) -> int:
        """Start at the newest event (retrying on DB errors, never from 0)."""
        if self.last_id is not None:
            return self.last_id
        delay = self.poll_interval
        while True:
            try:
                self.last_id = int(await asyncio.to_thread(self._latest_event_id))
            except sqlite3.Error:
                logger.warning(
                    "Cannot read the event log cursor; retrying in %.1fs",
                    delay,
                    exc_info=True,
                )
                await self._sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            return self.last_id

    async def poll_once(self) -> int:
        """Publish the events after the cursor (one batch); returns their count."""
        if self.last_id is None:
            await self.init_cursor()
        last = int(self.last_id or 0)
        items = await asyncio.to_thread(self._list_events_after, last, self.batch_size)
        if not items:
            await self._check_reset(last)
            return 0
        self._empty_polls = 0
        events = [self._shape(item) for item in items]
        self.last_id = int(items[-1]["id"])
        self.hub.publish({"kind": "events", "events": events, "last_id": self.last_id})
        return len(items)

    async def publish_status(self, *, force: bool = False) -> bool:
        """Publish the worker summary when it changed.

        Checked at most every ``status_interval`` (``force`` skips the wait,
        never the change check).
        """
        if self._status_source is None:
            return False
        now = time.monotonic()
        if not force and now < self._next_status_at:
            return False
        self._next_status_at = now + self.status_interval
        status = await asyncio.to_thread(self._status_source)
        if status is None:
            return False
        key = (status.get("state"), status.get("heartbeat_at"))
        if key == self._status_key:
            return False
        self._status_key = key
        self.hub.publish({"kind": "worker", "worker": dict(status)})
        return True

    async def _check_reset(self, last: int) -> None:
        """Follow a log whose ids went backwards (e.g. the DB file was replaced)."""
        self._empty_polls += 1
        if self._empty_polls < _RESET_CHECK_EVERY:
            return
        self._empty_polls = 0
        newest = int(await asyncio.to_thread(self._latest_event_id))
        if newest < last:
            logger.warning(
                "Event log ids went back from %d to %d; following the new log",
                last,
                newest,
            )
            self.last_id = newest

    async def _sleep(self, seconds: float) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)


def _log_task_end(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("The event tailer stopped", exc_info=error)
