"""Process one claimed request: fetch the media, prepare it, deliver it.

:class:`src.worker.WorkerPool` claims a ``queued`` row (it becomes
``processing``) and runs :meth:`RequestProcessor.run` for it::

    👀 -> fetch_info -> [Telegram URL fetch] -> download -> prepare_for_telegram
       -> thumbnail -> mark_sending -> send -> mark_done -> 🏆 -> cleanup

Every attempt ends with exactly one outcome (:class:`Outcome`):

* ``done``: delivered, ``mark_done`` (sizes, sent message id, media type).
* ``retry``: a retryable error (``DownloadError.retryable``, or a Telegram
  flood wait seen before anything was uploaded: at start or on the URL
  fetch) with attempts left: ``defer_request`` with exponential backoff (at
  least the flood wait). A flood wait during a claimed upload is waited out
  in place (bounded), because a ``sending`` row cannot go back to the queue.
* ``failed``: ``mark_failed(error_code)``; the user gets 🍌 plus, in private
  chats, the Italian explanation (groups only get a text for ``too_large``,
  which carries the original link, or when the chat refuses the 🍌).
* ``skipped``: ``mark_sending`` refused (the row was cancelled, deleted or
  already handled): nothing is sent.
* interrupted (the task is cancelled by a shutdown): the row goes back to the
  queue, or to ``failed``/``interrupted_while_sending`` when an upload may
  already have reached Telegram (``failed``/``interrupted`` when the upload
  was claimed but nothing was sent: a claimed row cannot be requeued); the
  👀 of a request that is over is removed; the ``CancelledError`` propagates.

Outcome writes are retried on database errors; one the database keeps
refusing is returned as ``JobResult.settlement`` and written later by the
worker pool (the user already got the outcome), so no row stays active.

Nothing is ever posted twice on purpose: local uploads are claimed with
``mark_sending`` first (a refusal means: do not send); the Telegram URL fetch
runs while the row is still ``processing`` (so a refusal can fall back to a
download or be retried) but counts as in flight; ambiguous failures
(timeouts, disconnects, 5xx: Telegram may have posted) are never retried; and
once something was delivered no error can send the request back to the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from . import config, db, downloader, media, messages, telegram_api
from .downloader import DownloadedFile, DownloadError, MediaInfo, MediaItem, NoMedia

logger = logging.getLogger(__name__)

REACTION_WORKING = "👀"
REACTION_DONE = "🏆"
REACTION_FAILED = "🍌"

# Work directories are created as <TMP_DIR>/smr-<request id>-<random>.
WORKDIR_PREFIX = "smr-"
MAX_ALBUM_ITEMS = telegram_api.MAX_MEDIA_GROUP

# Retry backoff for retryable errors: base * 2 ** (attempt - 1), capped.
DEFAULT_RETRY_BACKOFF_SECONDS = 30.0
MAX_RETRY_BACKOFF_SECONDS = 15 * 60.0
# Uploads claimed with mark_sending cannot go back to the queue: a flood wait
# (429) or a connection failure that certainly did not reach Telegram is
# retried in place, at most this many extra times and waiting at most this long.
UPLOAD_EXTRA_ATTEMPTS = 1
MAX_UPLOAD_WAIT_SECONDS = 300.0
UPLOAD_RETRY_DELAY_SECONDS = 15.0
# Best-effort Telegram calls made while a job is being cancelled.
CANCEL_CLEANUP_SECONDS = 5.0
# Outcome writes (mark_done, defer_request, mark_failed) are retried on
# database errors, waiting attempt * backoff seconds in between; when they
# keep failing the worker pool records the outcome later (JobResult.settlement).
OUTCOME_WRITE_ATTEMPTS = 3
OUTCOME_WRITE_BACKOFF_SECONDS = 1.0

_DEFAULTS: dict[str, Any] = {
    "TMP_DIR": "/tmp/telegram_downloader",
    "KEEP_DOWNLOADED_FILES": False,
    "TELEGRAM_MAX_FILE_SIZE": 50 * 1024 * 1024,
    "TELEGRAM_URL_FETCH_MAX_SIZE": 20 * 1024 * 1024,
    "TELEGRAM_SERVER_FETCH": True,
    "WORKER_GENERATE_THUMBNAIL": True,
    "WORKER_MAX_ATTEMPTS": 3,
    "WORKER_RETRY_BACKOFF_SECONDS": DEFAULT_RETRY_BACKOFF_SECONDS,
    "MAX_DURATION_SECONDS": 3600,
}


def setting(name: str) -> Any:
    """``config.<name>`` read at call time (tests patch config), or the default."""
    value = getattr(config, name, None)
    return _DEFAULTS.get(name) if value is None else value


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
class Stage(StrEnum):
    """What a running job is doing (shown in the worker heartbeat)."""

    STARTING = "starting"
    EXTRACTING = "extracting"
    SERVER_FETCH = "server_fetch"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    SENDING = "sending"
    FINISHING = "finishing"


class Outcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    RETRY = "retry"
    SKIPPED = "skipped"


class Phase(StrEnum):
    """Where an error happened (metrics: download vs upload failures)."""

    DOWNLOAD = "download"
    SEND = "send"


# Stages whose errors happen on Telegram's side (Phase.SEND).
_SEND_STAGES = frozenset({Stage.SERVER_FETCH, Stage.SENDING, Stage.FINISHING})
# messages.failure(step=...): where a too_large refusal happened, by stage.
_FAILURE_STEPS: Mapping[Stage, str] = {
    Stage.CONVERTING: "convert",
    Stage.SERVER_FETCH: "send",
    Stage.SENDING: "send",
    Stage.FINISHING: "send",
}


@dataclass
class JobState:
    """Live, in-memory view of one running request (heartbeat payload)."""

    request_id: int
    chat_id: int | None
    url: str | None
    provider: str | None
    started_at: str = field(default_factory=db.now_iso)
    started_monotonic: float = field(default_factory=time.monotonic)
    stage: Stage = Stage.STARTING
    workdir: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> JobState:
        return cls(
            request_id=int(row["id"]),
            chat_id=row.get("chat_id"),
            url=row.get("url"),
            provider=row.get("provider"),
        )

    def as_status(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "chat_id": self.chat_id,
            "url": self.url,
            "provider": self.provider,
            "stage": str(self.stage),
            "started_at": self.started_at,
        }


@dataclass(frozen=True)
class Request:
    """The fields of a claimed ``requests`` row the pipeline needs."""

    id: int
    chat_id: int
    url: str
    chat_type: str | None
    original_message_id: int | None
    description: str | None
    provider: str | None
    attempts: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Request:
        """ValueError when the row cannot be processed (legacy/garbage rows)."""
        chat_id, url = row.get("chat_id"), row.get("url")
        if not isinstance(chat_id, int) or not isinstance(url, str) or not url:
            raise ValueError(f"request {row.get('id')} has no chat id or url")
        message_id = row.get("original_message_id")
        return cls(
            id=int(row["id"]),
            chat_id=chat_id,
            url=url,
            chat_type=row.get("chat_type"),
            original_message_id=message_id if isinstance(message_id, int) else None,
            description=row.get("description"),
            provider=row.get("provider"),
            attempts=int(row.get("attempts") or 0),
        )

    @property
    def is_private(self) -> bool:
        """Private chat; rows without chat_type: Telegram user ids are > 0."""
        if self.chat_type:
            return self.chat_type == "private"
        return self.chat_id > 0


@dataclass(frozen=True)
class Settlement:
    """A job outcome the database did not accept (it kept raising sqlite3.Error).

    The row is still ``processing``/``sending``, and nothing but this worker
    changes such rows, so :class:`src.worker.WorkerPool` runs ``write`` (a
    blocking db call) again later instead of leaving the row active, holding
    a pending slot of the chat, until the next start.
    """

    request_id: int
    outcome: str  # what ``write`` records: "done", "deferred", "failed", ...
    write: Callable[[], object] = field(repr=False, compare=False)


@dataclass(frozen=True)
class JobResult:
    outcome: Outcome
    error_code: str | None = None
    error_phase: Phase | None = None
    retry_at: float | None = None  # epoch seconds (RETRY)
    method: str | None = None  # "local" | "server_fetch" (DONE)
    media_type: str | None = None
    sent: Mapping[str, int] = field(default_factory=dict)  # media type -> files
    bytes_sent: int = 0
    settlement: Settlement | None = None  # the outcome still to be recorded


@dataclass(frozen=True)
class PreparedItem:
    """One downloaded file made Telegram-ready."""

    file: DownloadedFile
    prepared: media.PreparedMedia
    kind: str  # "video" | "audio"
    thumbnail: str | None = None

    @property
    def size(self) -> int:
        return int(self.prepared.probe.size)

    @property
    def original_size(self) -> int:
        return int(self.prepared.original_size or self.file.size)

    def group_entry(self, caption: str | None) -> dict[str, Any]:
        """Item of ``telegram_api.send_media_group``."""
        probe = self.prepared.probe
        entry: dict[str, Any] = {
            "type": self.kind,
            "path": self.prepared.path,
            "duration": probe.duration,
        }
        if caption:
            entry["caption"] = caption
        if self.kind == "video":
            entry["width"] = probe.display_width
            entry["height"] = probe.display_height
        else:
            entry["title"] = self.file.item.title
            entry["performer"] = self.file.item.uploader
        if self.thumbnail:
            entry["thumbnail"] = self.thumbnail
        return entry


@dataclass(frozen=True)
class Delivery:
    message_id: int | None
    media_type: str | None
    sent: Mapping[str, int]
    bytes_sent: int
    original_size: int | None
    final_size: int | None
    compressed: bool | None

    def row_fields(self) -> dict[str, Any]:
        """``db.mark_done`` keywords (None keeps the row's value)."""
        return {
            "sent_message_id": self.message_id,
            "media_type": self.media_type,
            "original_size": self.original_size,
            "final_size": self.final_size,
            "compressed": self.compressed,
        }


# Something was posted but the job did not get to describe it (it failed or
# was cancelled in between): the row is marked done without details.
UNKNOWN_DELIVERY = Delivery(None, None, {}, 0, None, None, None)


# ---------------------------------------------------------------------------
# Errors raised by the pipeline itself
# ---------------------------------------------------------------------------
class SlideshowNotSupported(NoMedia):
    """A TikTok photo post: only its background music is downloadable."""

    slideshow = True


class SendFailed(DownloadError):
    """Telegram did not accept the upload.

    ``not_sent`` is True when the message certainly was not posted (Telegram
    answered with an error, or the connection never reached it); False when
    the outcome is unknown (timeout, disconnect, 5xx), which is never retried.
    """

    code = "send_failed"

    def __init__(
        self,
        detail: str = "",
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        not_sent: bool = True,
        error_code: int | None = None,
    ) -> None:
        super().__init__(detail, retryable=retryable)
        self.retry_after = retry_after
        self.not_sent = not_sent
        self.error_code = error_code


class SendForbidden(SendFailed):
    """The bot cannot post in the chat (blocked by the user, kicked, no rights)."""

    code = "forbidden"


class ConversionTimeout(DownloadError):
    """``TRANSCODE_TIMEOUT`` ran out while converting (media's DownloadTimeout).

    Not a download timeout: another attempt would download the file again
    and run the same encode. Retryable only when other jobs were converting
    at the same time (see :data:`_converting`).
    """

    code = "conversion_timeout"


_CONNECT_FAILURES = ("ClientConnector", "ConnectionTimeoutError")
_LOCAL_SEND_FAILURES = (
    "cannot read file",
    "file not found",
    "invalid parameters",
    "media group",
    "unsupported media type",
    "items can only be grouped",
    "has no path or url",
)


def send_failure(result: Mapping[str, Any]) -> DownloadError:
    """The typed error for a failed ``telegram_api`` send result."""
    status = result.get("error_code")
    detail = telegram_api.redact(str(result.get("description") or "unknown error"))
    if status == 429:
        parameters = result.get("parameters")
        retry_after = None
        if isinstance(parameters, Mapping):
            with contextlib.suppress(TypeError, ValueError):
                retry_after = float(parameters.get("retry_after"))
        return SendFailed(
            detail, retryable=True, retry_after=retry_after, error_code=429
        )
    if status == 413:
        return downloader.TooLarge(f"Telegram refused the upload: {detail}")
    if status == 403:
        return SendForbidden(detail, error_code=403)
    if isinstance(status, int) and status >= 500:
        return SendFailed(detail, not_sent=False, error_code=status)
    if isinstance(status, int):
        return SendFailed(detail, error_code=status)
    if any(marker in detail for marker in _CONNECT_FAILURES):
        return SendFailed(detail, retryable=True)
    if any(marker in detail.lower() for marker in _LOCAL_SEND_FAILURES):
        return SendFailed(detail)
    return SendFailed(detail, not_sent=False)


def _messages_of(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = result.get("result")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return [value] if isinstance(value, Mapping) else []


def _first_message_id(result: Mapping[str, Any]) -> int | None:
    for message in _messages_of(result):
        message_id = message.get("message_id")
        if isinstance(message_id, int):
            return message_id
    return None


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Seconds before the next attempt after ``attempt`` (1-based) failed."""
    try:
        base = float(setting("WORKER_RETRY_BACKOFF_SECONDS"))
    except (TypeError, ValueError):
        base = DEFAULT_RETRY_BACKOFF_SECONDS
    exponent = min(max(attempt, 1) - 1, 16)
    delay = min(max(base, 0.0) * 2**exponent, MAX_RETRY_BACKOFF_SECONDS)
    return max(delay, retry_after or 0.0)


def album_groups(items: Sequence[PreparedItem]) -> list[list[PreparedItem]]:
    """Consecutive items of the same kind, at most 10 per album."""
    groups: list[list[PreparedItem]] = []
    for item in items:
        if (
            groups
            and groups[-1][0].kind == item.kind
            and len(groups[-1]) < MAX_ALBUM_ITEMS
        ):
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _is_youtube(key: str | None) -> bool:
    return bool(key) and str(key).lower().startswith("youtube")


def _is_tiktok(key: str | None) -> bool:
    return bool(key) and str(key).lower().startswith("tiktok")


def is_tiktok_slideshow(info: MediaInfo, provider: str | None = None) -> bool:
    """TikTok photo posts come back from yt-dlp as audio-only items (the music)."""
    if not (_is_tiktok(info.extractor_key) or _is_tiktok(provider)):
        return False
    return bool(info.items) and all(item.kind == "audio" for item in info.items)


def common_kind(kinds: Iterable[str]) -> str | None:
    """The one media kind of ``kinds`` ("video"/"audio"), None if mixed or empty."""
    found = set(kinds)
    return found.pop() if len(found) == 1 else None


async def _record(
    request_id: int, write: Callable[[], object], what: str
) -> bool | None:
    """Run an outcome write, retried on database errors (OUTCOME_WRITE_*).

    True/False: written / refused (the row was no longer active); None: the
    database kept failing, so the caller hands a :class:`Settlement` back.
    """
    for attempt in range(1, OUTCOME_WRITE_ATTEMPTS + 1):
        try:
            return bool(await db.aio(write))
        except sqlite3.Error:
            if attempt == OUTCOME_WRITE_ATTEMPTS:
                logger.exception(
                    "Request %s: cannot record %s; the worker retries it later",
                    request_id,
                    what,
                )
                return None
            logger.warning(
                "Request %s: cannot record %s (attempt %d); retrying",
                request_id,
                what,
                attempt,
                exc_info=True,
            )
            await asyncio.sleep(attempt * OUTCOME_WRITE_BACKOFF_SECONDS)
    return None


def _settlement(
    request_id: int, outcome: str, write: Callable[[], object], recorded: bool | None
) -> Settlement | None:
    """The Settlement of a write that :func:`_record` could not run (None)."""
    return Settlement(request_id, outcome, write) if recorded is None else None


def _make_workdir(root: str, prefix: str) -> str:
    os.makedirs(root, mode=0o700, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=root)  # private (0700)


def _remove_tree(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(path)


# ---------------------------------------------------------------------------
# User feedback (reactions + fallback status message)
# ---------------------------------------------------------------------------
class Feedback:
    """Reactions on the user's message, or a status message when they fail.

    👀 while working (fallback: an "In lavorazione 👀" reply), 🏆 on success,
    🍌 on failure; when the chat refuses the final emoji the 👀 is removed.
    Every call is best effort: telegram_api never raises.
    """

    def __init__(self, token: str, request: Request) -> None:
        self._token = token
        self._request = request
        self.status_message_id: int | None = None
        self._reacted = False  # our reaction is on the user's message

    async def react(self, emoji: str | None) -> bool:
        message_id = self._request.original_message_id
        if message_id is None:
            return False
        result = await telegram_api.set_message_reaction(
            self._token, self._request.chat_id, message_id, emoji
        )
        ok = bool(result.get("ok"))
        if ok:
            self._reacted = emoji is not None
        return ok

    async def clear_reaction(self) -> None:
        """Remove our reaction (👀), if we set one."""
        if self._reacted:
            await self.react(None)

    async def finish(self, emoji: str) -> bool:
        """The final reaction (🏆/🍌). When the chat refuses that emoji (groups
        can restrict reactions to a set without it), our 👀 is removed so the
        message does not look in progress forever."""
        if await self.react(emoji):
            return True
        await self.clear_reaction()
        return False

    async def _send_reply(self, text: str) -> dict[str, Any]:
        result = await telegram_api.send_message(
            self._token,
            self._request.chat_id,
            text,
            reply_to_message_id=self._request.original_message_id,
        )
        if not result.get("ok"):
            logger.info(
                "Request %s: cannot send a message to chat %s: %s",
                self._request.id,
                self._request.chat_id,
                telegram_api.redact(result.get("description"), self._token),
            )
        return result

    async def reply(self, text: str) -> int | None:
        """Reply to the user's message (sent even if it was deleted); its id."""
        result = await self._send_reply(text)
        return _first_message_id(result) if result.get("ok") else None

    async def working(self) -> float | None:
        """👀, or the status message when reactions fail.

        Returns Telegram's flood wait (seconds) when both were refused with
        429: nothing can be posted in this chat for a while.
        """
        if await self.react(REACTION_WORKING):
            return None
        result = await self._send_reply(messages.PROCESSING)
        if result.get("ok"):
            self.status_message_id = _first_message_id(result)
            return None
        failure = send_failure(result)
        if failure.retryable and getattr(failure, "not_sent", False):
            return getattr(failure, "retry_after", None)
        return None

    async def say(self, text: str) -> None:
        """Final text: turn the status message into it, or send a reply."""
        if self.status_message_id is not None:
            result = await telegram_api.edit_message_text(
                self._token, self._request.chat_id, self.status_message_id, text
            )
            if result.get("ok"):
                self.status_message_id = None  # it now carries the outcome: keep it
                return
            await self.clear_status()
        await self.reply(text)

    async def clear_status(self) -> None:
        message_id, self.status_message_id = self.status_message_id, None
        if message_id is not None:
            await telegram_api.delete_message(
                self._token, self._request.chat_id, message_id
            )


# ---------------------------------------------------------------------------
# The processor
# ---------------------------------------------------------------------------
# Jobs converting files right now. They share media's encoder slots
# (TRANSCODE_CONCURRENCY) and media counts the wait for a slot in
# TRANSCODE_TIMEOUT, so a conversion timeout is worth a retry only for a job
# that did not have the encoder to itself.
_converting: set[RequestProcessor] = set()


async def process(token: str, row: Mapping[str, Any], state: JobState) -> JobResult:
    """Process one claimed (``processing``) row to its outcome."""
    try:
        request = Request.from_row(row)
    except ValueError as exc:
        request_id = int(row["id"])
        logger.error("Request %s cannot be processed: %s", request_id, exc)
        write = functools.partial(
            db.mark_failed,
            request_id,
            error_code="invalid_request",
            error_message=str(exc),
        )
        recorded = await _record(request_id, write, "the failure")
        return JobResult(
            Outcome.FAILED,
            error_code="invalid_request",
            settlement=_settlement(request_id, "failed", write, recorded),
        )
    return await RequestProcessor(token, request, state).run()


class RequestProcessor:
    """Runs one claimed request to its outcome (see the module docstring)."""

    def __init__(self, token: str, request: Request, state: JobState) -> None:
        self.token = token
        self.state = state
        self.request = request
        self.feedback = Feedback(token, request)
        self._claim_started = False  # mark_sending was called (it may still land)
        self._sending = False  # mark_sending succeeded: the row is 'sending'
        self._send_in_flight = False  # a send (URL fetch or upload) may reach Telegram
        self._delivered = False  # something was posted: never retry or fail it
        self._delivery: Delivery | None = None  # what was posted, once known
        self._method = "local"  # how the media reaches Telegram
        self._kind: str | None = None  # "video"/"audio" (None: not known yet/mixed)
        self._encoder_shared = False  # another job converted at the same time

    @property
    def maybe_posted(self) -> bool:
        """An upload was claimed or is in flight: Telegram may have the post."""
        return self._sending or self._send_in_flight or self._delivered

    # -- entry point ---------------------------------------------------------
    async def run(self) -> JobResult:
        request = self.request
        logger.info(
            "Request %s: processing %s for chat %s (attempt %s)",
            request.id,
            downloader.redact(request.url),
            request.chat_id,
            request.attempts,
        )
        try:
            flood_wait = await self.feedback.working()
            if flood_wait:
                raise SendFailed(
                    f"Telegram flood control: retry after {flood_wait:.0f}s",
                    retryable=True,
                    retry_after=flood_wait,
                    error_code=429,
                )
            return await self._process()
        except asyncio.CancelledError:
            await self._interrupted()
            raise
        except DownloadError as err:
            return await self._failed(err)
        except sqlite3.Error as exc:
            logger.warning("Request %s: database error", request.id, exc_info=True)
            return await self._failed(
                DownloadError(f"database error: {exc}", retryable=True)
            )
        except Exception as exc:  # job boundary: a bug must not strand the row
            logger.exception("Request %s: unexpected error", request.id)
            return await self._failed(
                DownloadError(f"internal error: {type(exc).__name__}: {exc}")
            )
        finally:
            await self._cleanup()

    # -- steps -----------------------------------------------------------------
    def _stage(self, stage: Stage) -> None:
        self.state.stage = stage

    async def _process(self) -> JobResult:
        workdir = await self._workdir()
        self._stage(Stage.EXTRACTING)
        info = await downloader.fetch_info(self.request.url, workdir)
        if is_tiktok_slideshow(info, self.request.provider):
            raise SlideshowNotSupported(
                "TikTok photo slideshow: only the background music is downloadable"
            )
        self._kind = common_kind(entry.kind for entry in info.items)
        item = self._server_fetch_item(info)
        if item is not None:
            result = await self._server_fetch(item)
            if result is not None:
                return result
        self._stage(Stage.DOWNLOADING)
        files = await downloader.download(info, workdir)
        self._stage(Stage.CONVERTING)
        items = await self._prepare_all(files, workdir)
        self._kind = common_kind(ready.kind for ready in items)
        # Its thread commits even if the job is cancelled meanwhile.
        self._claim_started = True
        if not await db.aio(db.mark_sending, self.request.id):
            return await self._skipped("mark_sending refused")
        self._sending = True
        self._stage(Stage.SENDING)
        delivery = await self._deliver(items)
        return await self._done(delivery, method="local")

    async def _workdir(self) -> str:
        root = str(setting("TMP_DIR"))
        prefix = f"{WORKDIR_PREFIX}{self.request.id}-"
        try:
            path = await asyncio.to_thread(_make_workdir, root, prefix)
        except OSError as exc:
            raise DownloadError(
                f"cannot create a work directory in {root}: {exc.strerror or exc}",
                retryable=True,
            ) from exc
        self.state.workdir = path
        return path

    # -- Telegram server-side URL fetch ---------------------------------------
    def _server_fetch_item(self, info: MediaInfo) -> MediaItem | None:
        """The single progressive item Telegram can fetch by URL, if any."""
        if not setting("TELEGRAM_SERVER_FETCH") or len(info.items) != 1:
            return None
        item = info.items[0]
        if item.kind != "video" or not item.direct_url or item.filesize is None:
            return None
        if _is_youtube(info.extractor_key) or _is_youtube(self.request.provider):
            return None  # googlevideo URLs are bound to the bot's IP
        limit = min(
            int(setting("TELEGRAM_URL_FETCH_MAX_SIZE")),
            int(setting("TELEGRAM_MAX_FILE_SIZE")),
        )
        return item if 0 < item.filesize <= limit else None

    async def _server_fetch(self, item: MediaItem) -> JobResult | None:
        """Let Telegram fetch the URL itself; None = fall back to a download.

        The row stays ``processing`` (so a refusal can still fall back or be
        retried); the send counts as in flight for the cancellation handler.
        """
        row = await db.aio(db.get_request, self.request.id)
        if row is None or row.get("status") != db.Status.PROCESSING:
            return await self._skipped("the request is no longer processing")
        self._stage(Stage.SERVER_FETCH)
        self._method = "server_fetch"
        caption = messages.caption(self.request.description)
        # Cleared only when Telegram answered: if the send is cancelled or
        # raises, the post may exist and the request must not be retried.
        self._send_in_flight = True
        result = await telegram_api.send_video(
            self.token,
            self.request.chat_id,
            str(item.direct_url),
            caption=caption,
            reply_to_message_id=self.request.original_message_id,
            duration=item.duration,
            width=item.width,
            height=item.height,
        )
        self._send_in_flight = False
        if result.get("ok"):
            self._delivered = True
            size = int(item.filesize or 0)
            delivery = Delivery(
                message_id=_first_message_id(result),
                media_type="video",
                sent={"video": 1},
                bytes_sent=size,
                original_size=size,
                final_size=size,
                compressed=False,
            )
            return await self._done(delivery, method="server_fetch")
        failure = send_failure(result)
        not_sent = getattr(failure, "not_sent", True)
        if not not_sent or isinstance(failure, SendForbidden) or failure.retryable:
            # Unknown outcome (never post a second copy), the bot cannot post
            # here at all, or a flood wait: a local upload would fare no better.
            raise failure
        logger.info(
            "Request %s: Telegram could not fetch the URL (%s); downloading it",
            self.request.id,
            failure.detail,
        )
        self._method = "local"
        return None

    # -- local path ------------------------------------------------------------
    async def _prepare_all(
        self, files: Sequence[DownloadedFile], workdir: str
    ) -> list[PreparedItem]:
        max_bytes = int(setting("TELEGRAM_MAX_FILE_SIZE"))
        ready: list[PreparedItem] = []
        errors: list[DownloadError] = []
        _converting.add(self)
        if len(_converting) > 1:
            for job in _converting:
                job._encoder_shared = True
        try:
            for downloaded in files:
                try:
                    ready.append(await self._prepare(downloaded, workdir, max_bytes))
                except DownloadError as err:
                    if len(files) == 1:
                        raise
                    logger.warning(
                        "Request %s: item %s skipped: [%s] %s",
                        self.request.id,
                        downloaded.item.index,
                        err.code,
                        err.detail,
                    )
                    errors.append(err)
        finally:
            _converting.discard(self)
        if not ready:
            raise errors[0] if errors else NoMedia("nothing to send")
        return ready

    async def _prepare(
        self, downloaded: DownloadedFile, workdir: str, max_bytes: int
    ) -> PreparedItem:
        try:
            prepared = await media.prepare_for_telegram(
                downloaded.path, workdir, max_bytes=max_bytes
            )
        except downloader.DownloadTimeout as err:
            # Alone, the encode itself was too slow: a retry would repeat it.
            raise ConversionTimeout(
                err.detail,
                retryable=self._encoder_shared,
                stderr_tail=err.stderr_tail,
            ) from err
        probe = prepared.probe
        if probe.has_video:
            kind = "video"
        elif probe.has_audio and downloaded.item.kind != "video":
            kind = "audio"
        elif probe.has_audio:
            raise NoMedia("only an audio track was obtained for a video item")
        else:
            raise NoMedia("the downloaded file has no audio or video stream")
        thumbnail = None
        if kind == "video" and setting("WORKER_GENERATE_THUMBNAIL"):
            thumbnail = await media.make_thumbnail(
                prepared.path, workdir, duration=probe.duration
            )
        return PreparedItem(downloaded, prepared, kind, thumbnail)

    def _caption(self, items: Sequence[PreparedItem]) -> str | None:
        compressed = [item for item in items if item.prepared.compressed]
        note = None
        if compressed:
            note = messages.compression_note(
                sum(item.original_size for item in compressed),
                sum(item.size for item in compressed),
                kind=common_kind(item.kind for item in compressed),
                count=len(compressed),
            )
        return messages.caption(self.request.description, note)

    async def _deliver(self, items: Sequence[PreparedItem]) -> Delivery:
        caption = self._caption(items)
        if len(items) == 1:
            message_id, media_type = await self._send_single(items[0], caption)
            sent_items = list(items)
            sent: dict[str, int] = {media_type: 1}
        else:
            message_id, sent_items = await self._send_albums(items, caption)
            media_type = "album"
            sent = {}
            for item in sent_items:
                sent[item.kind] = sent.get(item.kind, 0) + 1
        return Delivery(
            message_id=message_id,
            media_type=media_type,
            sent=sent,
            bytes_sent=sum(item.size for item in sent_items),
            original_size=sum(item.original_size for item in sent_items),
            final_size=sum(item.size for item in sent_items),
            compressed=any(item.prepared.compressed for item in sent_items),
        )

    async def _upload(
        self, send: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        """Run one claimed upload; retry it in place only when it surely failed."""
        attempt = 0
        while True:
            self._send_in_flight = True  # cleared only when Telegram answered
            result = await send()
            self._send_in_flight = False
            if result.get("ok"):
                self._delivered = True
                return result
            failure = send_failure(result)
            wait = getattr(failure, "retry_after", None) or (
                UPLOAD_RETRY_DELAY_SECONDS * 2**attempt
            )
            if (
                attempt >= UPLOAD_EXTRA_ATTEMPTS
                or not failure.retryable
                or not getattr(failure, "not_sent", False)
                or wait > MAX_UPLOAD_WAIT_SECONDS
            ):
                raise failure
            attempt += 1
            logger.info(
                "Request %s: upload refused (%s); retrying in %.0fs",
                self.request.id,
                failure.detail,
                wait,
            )
            await asyncio.sleep(wait)

    async def _send_single(
        self, item: PreparedItem, caption: str | None
    ) -> tuple[int | None, str]:
        probe = item.prepared.probe
        reply_to = self.request.original_message_id
        if item.kind == "audio":
            result = await self._upload(
                lambda: telegram_api.send_audio(
                    self.token,
                    self.request.chat_id,
                    item.prepared.path,
                    caption=caption,
                    reply_to_message_id=reply_to,
                    duration=probe.duration,
                    title=item.file.item.title,
                    performer=item.file.item.uploader,
                )
            )
            return _first_message_id(result), "audio"
        try:
            result = await self._upload(
                lambda: telegram_api.send_video(
                    self.token,
                    self.request.chat_id,
                    item.prepared.path,
                    caption=caption,
                    reply_to_message_id=reply_to,
                    duration=probe.duration,
                    width=probe.display_width,
                    height=probe.display_height,
                    thumbnail=item.thumbnail,
                )
            )
            return _first_message_id(result), "video"
        except SendFailed as failure:
            if failure.error_code != 400:
                raise
            logger.info(
                "Request %s: sendVideo refused (%s); sending it as a document",
                self.request.id,
                failure.detail,
            )
        result = await self._upload(
            lambda: telegram_api.send_document(
                self.token,
                self.request.chat_id,
                item.prepared.path,
                caption=caption,
                reply_to_message_id=reply_to,
                thumbnail=item.thumbnail,
            )
        )
        return _first_message_id(result), "document"

    async def _send_albums(
        self, items: Sequence[PreparedItem], caption: str | None
    ) -> tuple[int | None, list[PreparedItem]]:
        first_id: int | None = None
        sent: list[PreparedItem] = []
        for number, group in enumerate(album_groups(items)):
            group_caption = caption if number == 0 else None
            entries = [
                item.group_entry(group_caption if index == 0 else None)
                for index, item in enumerate(group)
            ]
            try:
                result = await self._upload(
                    lambda entries=entries: telegram_api.send_media_group(
                        self.token,
                        self.request.chat_id,
                        entries,
                        reply_to_message_id=self.request.original_message_id,
                    )
                )
            except DownloadError as err:
                if not sent:
                    raise
                logger.warning(
                    "Request %s: album part %d not sent ([%s] %s); %d item(s) delivered",
                    self.request.id,
                    number + 1,
                    err.code,
                    err.detail,
                    len(sent),
                )
                break
            if first_id is None:
                first_id = _first_message_id(result)
            sent.extend(group)
        return first_id, sent

    # -- outcomes --------------------------------------------------------------
    async def _done(self, delivery: Delivery, *, method: str) -> JobResult:
        """Record the delivery; the post exists, so it is never retried."""
        self._stage(Stage.FINISHING)
        self._delivery = delivery
        write = functools.partial(
            db.mark_done, self.request.id, **delivery.row_fields()
        )
        recorded = await _record(self.request.id, write, "the delivery")
        if recorded is False:
            logger.warning(
                "Request %s: delivered, but the row was no longer active",
                self.request.id,
            )
        await self.feedback.finish(REACTION_DONE)
        await self.feedback.clear_status()
        logger.info(
            "Request %s: delivered %s (%s, %s, message %s)",
            self.request.id,
            delivery.media_type,
            method,
            messages.size_mb(delivery.final_size),
            delivery.message_id,
        )
        return JobResult(
            Outcome.DONE,
            method=method,
            media_type=delivery.media_type,
            sent=dict(delivery.sent),
            bytes_sent=delivery.bytes_sent,
            settlement=_settlement(self.request.id, "done", write, recorded),
        )

    async def _skipped(self, reason: str) -> JobResult:
        logger.warning("Request %s: not sent: %s", self.request.id, reason)
        await self.feedback.react(None)
        await self.feedback.clear_status()
        return JobResult(Outcome.SKIPPED)

    def _phase(self, err: DownloadError) -> Phase:
        """Where the attempt failed: on Telegram (the URL fetch, the upload, or
        a flood wait before any work) or while getting the media. By stage,
        not by error type: a Telegram 413 is a TooLarge raised by the upload."""
        if isinstance(err, SendFailed) or self.state.stage in _SEND_STAGES:
            return Phase.SEND
        return Phase.DOWNLOAD

    async def _failed(self, err: DownloadError) -> JobResult:
        """The attempt failed: retry it later or fail the request.

        Never raises sqlite3.Error: an outcome the database does not accept
        is handed back as a :class:`Settlement`.
        """
        request = self.request
        if self._delivered:
            # Something was posted: never retry or fail it, whatever went wrong
            # afterwards; it is recorded as done.
            logger.error(
                "Request %s: error after the delivery ([%s] %s)",
                request.id,
                err.code,
                err.detail,
            )
            return await self._done(
                self._delivery or UNKNOWN_DELIVERY, method=self._method
            )
        phase = self._phase(err)
        max_attempts = int(setting("WORKER_MAX_ATTEMPTS"))
        if err.retryable and not self.maybe_posted and request.attempts < max_attempts:
            delay = retry_delay(request.attempts, getattr(err, "retry_after", None))
            retry_at = time.time() + delay
            write = functools.partial(
                db.defer_request,
                request.id,
                db.iso_from_ts(retry_at),
                reason=f"{err.code}: {err.detail}"[:500],
            )
            deferred = await _record(request.id, write, "the retry")
            if deferred is not False:
                logger.info(
                    "Request %s: [%s] %s; retry %d/%d in %.0fs",
                    request.id,
                    err.code,
                    err.detail,
                    request.attempts + 1,
                    max_attempts,
                    delay,
                )
                await self.feedback.clear_status()
                return JobResult(
                    Outcome.RETRY,
                    error_code=err.code,
                    error_phase=phase,
                    retry_at=retry_at,
                    settlement=_settlement(request.id, "deferred", write, deferred),
                )
        logger.warning(
            "Request %s failed: [%s] %s", request.id, err.code, err.detail or err.code
        )
        if err.stderr_tail:
            logger.debug(
                "Request %s yt-dlp stderr tail:\n%s", request.id, err.stderr_tail
            )
        write = functools.partial(
            db.mark_failed,
            request.id,
            error_code=err.code,
            error_message=err.detail or err.code,
        )
        failed = await _record(request.id, write, "the failure")
        if failed is False:
            return await self._skipped("the request was no longer active")
        await self._report_failure(err)
        return JobResult(
            Outcome.FAILED,
            error_code=err.code,
            error_phase=phase,
            settlement=_settlement(request.id, "failed", write, failed),
        )

    async def _report_failure(self, err: DownloadError) -> None:
        """🍌, plus the explanation in private chats (groups: too_large only,
        or when the chat refuses the 🍌)."""
        if isinstance(err, SendForbidden):
            return  # the bot cannot post in this chat at all
        text = messages.failure(
            err.code,
            url=self.request.url,
            max_duration_seconds=setting("MAX_DURATION_SECONDS"),
            slideshow=bool(getattr(err, "slideshow", False)),
            kind=self._kind,
            step=_FAILURE_STEPS.get(self.state.stage, "download"),
        )
        reacted = await self.feedback.finish(REACTION_FAILED)
        explain = self.request.is_private or err.code == downloader.TooLarge.code
        if explain or not reacted:
            # Without the 🍌 (reactions refused, or restricted to a set
            # without it) the text is the only trace of the failure; a status
            # message becomes the explanation instead of disappearing.
            await self.feedback.say(text)
        else:
            await self.feedback.clear_status()

    async def _interrupted(self) -> None:
        """The job is being cancelled (shutdown): leave the row recoverable."""
        request = self.request
        over = self.maybe_posted  # if nothing can be recorded: never retried
        try:
            outcome, over = await self._record_interruption()
        except sqlite3.Error:
            outcome = "not recorded"
            logger.warning(
                "Request %s: cannot record the interruption; recovery at the next "
                "start will settle it",
                request.id,
                exc_info=True,
            )
        logger.info(
            "Request %s interrupted during %s (%s)",
            request.id,
            self.state.stage,
            outcome,
        )
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(CANCEL_CLEANUP_SECONDS):
                if self._delivered:
                    await self.feedback.finish(REACTION_DONE)
                elif over:
                    await self.feedback.clear_reaction()  # no 👀 on a dead request
                await self.feedback.clear_status()

    async def _record_interruption(self) -> tuple[str, bool]:
        """Settle the row of a cancelled job: (log text, is the request over)."""
        request_id = self.request.id
        if self._delivered:
            delivery = self._delivery or UNKNOWN_DELIVERY
            await db.aio(db.mark_done, request_id, **delivery.row_fields())
            return "delivered", True
        if self._send_in_flight:
            await db.aio(
                db.mark_failed,
                request_id,
                error_code="interrupted_while_sending",
                error_message="worker stopped during the upload; not resent",
            )
            return "upload outcome unknown; not resent", True
        if not self._sending and await db.aio(
            db.defer_request, request_id, db.now_iso(), reason="worker stopped"
        ):
            return "back in the queue", False
        # Not 'processing': the upload was claimed (mark_sending may also have
        # landed while the job was being cancelled), yet nothing was sent. A
        # 'sending' row cannot go back to the queue: fail it saying so (an
        # admin can requeue it) instead of leaving it to the next start, which
        # would call it interrupted while uploading.
        if (self._sending or self._claim_started) and await db.aio(
            db.mark_failed,
            request_id,
            error_code="interrupted",
            error_message="worker stopped before anything was uploaded; not resent",
        ):
            return "nothing uploaded, but a claimed upload cannot be requeued", True
        return "not requeued: the row was no longer processing", False

    async def _cleanup(self) -> None:
        path = self.state.workdir
        if not path:
            return
        if setting("KEEP_DOWNLOADED_FILES"):
            logger.info("Request %s: keeping %s", self.request.id, path)
            return
        try:
            await asyncio.to_thread(_remove_tree, path)
        except OSError as exc:
            logger.warning("Cannot remove %s: %s", path, exc.strerror or exc)
