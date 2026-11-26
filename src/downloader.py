"""Download media with yt-dlp: one extraction per link, typed errors, bounded processes.

Typical use (see ``src/worker.py``)::

    info = await downloader.fetch_info(url, workdir)   # one `yt-dlp -J` extraction
    files = await downloader.download(info, workdir)   # `--load-info-json`, no re-extraction
    prepared = await media.prepare_for_telegram(files[0].path, workdir, max_bytes=...)

Design notes
------------
* yt-dlp always runs as a subprocess in its own session/process group, so a
  timeout or a cancellation kills the whole tree (yt-dlp plus its ffmpeg
  children) with ``os.killpg`` and the wait afterwards is bounded.
* stdout/stderr are captured with a bounded buffer; errors are classified from
  the *last* ``ERROR:`` line(s), never from the first bytes of stderr (which
  are usually warnings).
* Format selection is size aware: the default selector asks for H.264/AAC MP4
  that fits ``TELEGRAM_MAX_FILE_SIZE`` and falls back to inputs that
  :func:`src.media.prepare_for_telegram` converts. ``--max-filesize`` (set to
  ``MAX_DOWNLOAD_BYTES``) is only a safety net, plus an on-disk size guard for
  protocols where yt-dlp does not enforce it (HLS/DASH fragments).
* The configured cookies file is copied into the per-request workdir before
  every run: yt-dlp rewrites its ``--cookies`` file on exit and must never
  touch the operator's (often read-only) file.
* The info JSON from the extraction is saved in the workdir and every
  download uses it with ``--load-info-json`` (no second extraction). It may
  contain cookies: the workdir must stay private and the JSON is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import signal
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from . import config

logger = logging.getLogger(__name__)

MIB = 1024 * 1024

# ---------------------------------------------------------------------------
# Settings (read at call time so tests can monkeypatch ``src.config``)
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "TELEGRAM_MAX_FILE_SIZE": 50 * MIB,
    "MAX_DOWNLOAD_BYTES": 500 * MIB,
    "MAX_DURATION_SECONDS": 3600,
    "MAX_ITEMS_PER_LINK": 10,
    "EXTRACT_TIMEOUT": 60,
    "DOWNLOAD_TIMEOUT": 300,
}


def setting(name: str, default: Any = None) -> Any:
    """Return ``config.<name>``, falling back to the documented default."""
    if default is None:
        default = _DEFAULTS.get(name)
    value = getattr(config, name, None)
    return default if value is None else value


def _int_setting(name: str) -> int:
    try:
        return int(setting(name))
    except (TypeError, ValueError):
        return int(_DEFAULTS[name])


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

DETAIL_MAX_CHARS = 600
STDERR_TAIL_CHARS = 4000


class DownloadError(Exception):
    """Base class of every typed download/conversion error.

    ``code`` is stable and meant for the DB (``requests.error_code``) and for
    choosing the user-facing message. ``detail`` is the (redacted) technical
    reason, e.g. the last ``ERROR:`` line printed by yt-dlp. ``retryable``
    hints that trying again later may succeed (network trouble, site rate
    limits, timeouts).
    """

    code: ClassVar[str] = "failed"
    default_retryable: ClassVar[bool] = False

    def __init__(
        self,
        detail: str = "",
        *,
        retryable: bool | None = None,
        stderr_tail: str = "",
    ) -> None:
        self.detail = _clip(redact(detail).strip(), DETAIL_MAX_CHARS)
        self.retryable = self.default_retryable if retryable is None else retryable
        self.stderr_tail = redact(stderr_tail)[-STDERR_TAIL_CHARS:]
        super().__init__(self.detail or self.code)


class UnsupportedUrl(DownloadError):
    code = "unsupported"


class AuthRequired(DownloadError):
    """Login, private content, age gate, bot check: cookies are needed."""

    code = "auth_required"


class ContentUnavailable(DownloadError):
    """Removed, deleted, 404."""

    code = "unavailable"


class GeoBlocked(DownloadError):
    code = "geo_blocked"


class LiveStream(DownloadError):
    code = "live"


class TooLong(DownloadError):
    code = "too_long"


class TooLarge(DownloadError):
    code = "too_large"


class NoMedia(DownloadError):
    """Nothing downloadable: image-only posts, no video formats, empty result."""

    code = "no_media"


class DownloadTimeout(DownloadError):
    code = "timeout"
    default_retryable = True


class ConversionFailed(DownloadError):
    """ffmpeg/ffprobe failed or is missing (raised by :mod:`src.media`)."""

    code = "conversion_failed"


class ConversionTimeout(ConversionFailed):
    """The conversion exceeded ``TRANSCODE_TIMEOUT`` (raised by :mod:`src.media`).

    Not retryable, unlike :class:`DownloadTimeout`: the same input on the same
    CPU would be just as slow, and a retry downloads and converts everything
    again. The code stays ``conversion_failed`` (same user text and label).
    """


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}")
_URL_CREDENTIALS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]*@")
_URL_QUERY_RE = re.compile(r"(?i)\b(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]*")


def redact(text: str | None) -> str:
    """Remove bot tokens, URL credentials and URL query strings (signatures)."""
    if not text:
        return ""
    text = _BOT_TOKEN_RE.sub("<redacted-token>", text)
    text = _URL_CREDENTIALS_RE.sub(r"\1***@", text)
    return _URL_QUERY_RE.sub(r"\1?…", text)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

_MAX_FILESIZE_RE = re.compile(r"File is larger than max-filesize[^\n]*", re.IGNORECASE)
_LOG_PREFIXES = ("WARNING:", "ERROR:", "[", "DEBUG:", "Traceback", "Deleting ")

# (error class, retryable, needles). Order matters: the first match wins.
# ``DownloadError`` rows stop the search with a generic (non-content) error.
_RULES: tuple[tuple[type[DownloadError], bool, tuple[str, ...]], ...] = (
    (
        UnsupportedUrl,
        False,
        ("unsupported url", "no suitable extractor", "is not a valid url"),
    ),
    (
        # missing dependency / bad local configuration: not the content's fault
        DownloadError,
        False,
        (
            "impersonat",
            "javascript runtime",
            "js runtime",
            "cookies database",
            "failed to load cookies",
            "cookieloaderror",
            "ffmpeg not found",
            "ffmpeg is not installed",
            "ffprobe not found",
        ),
    ),
    (
        NoMedia,
        False,
        (
            "no video formats found",
            "there is no video in this post",
            "no video could be found",
            "no media found",
            "does not contain a video",
            "does not contain any video",
            "requested format is not available",
            "no formats found",
            "drm protected",
        ),
    ),
    (
        LiveStream,
        False,
        (
            "live event",
            "premieres in",
            "premiere will begin",
            "livestream",
            "live stream",
            "is live now",
            "is currently live",
            "is offline",
        ),
    ),
    (
        GeoBlocked,
        False,
        (
            "in your country",
            "from your location",
            "in your region",
            "geo restriction",
            "geo-restrict",
            "georestrict",
            "geo restricted",
            "geoblock",
        ),
    ),
    (
        AuthRequired,
        False,
        (
            "sign in",
            "signin",
            "log in",
            "login",
            "logged in",
            "logged-in",
            "private video",
            "is private",
            "account is private",
            "authenticat",
            "registered users",
            "members-only",
            "members only",
            "join this channel",
            "confirm your age",
            "age-restricted",
            "age restricted",
            "inappropriate for some users",
            "cookies",
            "password",
            "follow this account",
            "subscription",
        ),
    ),
    (
        DownloadError,
        True,
        (
            "http error 429",
            "too many requests",
            "rate-limit",
            "rate limit",
            "ratelimit",
            "try again later",
        ),
    ),
    (DownloadTimeout, True, ("timed out", "timeout")),
    (
        # transient server/network trouble (before "unavailable": HTTP 503 is
        # "Service Unavailable")
        DownloadError,
        True,
        (
            "http error 5",
            "connection reset",
            "connection refused",
            "connection aborted",
            "remote end closed",
            "temporary failure in name resolution",
            "name or service not known",
            "network is unreachable",
            "transporterror",
            "incompleteread",
            "ssl:",
        ),
    ),
    (
        ContentUnavailable,
        False,
        (
            "unavailable",
            "not available",
            "no longer available",
            "been removed",
            "removed by",
            "deleted",
            "does not exist",
            "doesn't exist",
            "not found",
            "http error 404",
            "http error 410",
            "terminated",
            "suspended",
            "copyright",
            "taken down",
        ),
    ),
)

_BOILERPLATE_RE = re.compile(
    r"[;.]?\s*(please report this issue on|confirm you are on the latest version).*$",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
    return " ".join(text.lower().split())


def error_messages(stderr: str) -> list[str]:
    """Return the ``ERROR:`` messages printed by yt-dlp, in order.

    Continuation lines (a message split over several lines) are joined; any
    other log line (``WARNING:``, ``[extractor] ...``) ends a message.
    """
    messages: list[list[str]] = []
    current: list[str] | None = None
    for raw_line in (stderr or "").splitlines():
        line = raw_line.strip()
        if line.startswith("ERROR:"):
            current = [line[len("ERROR:") :].strip()]
            messages.append(current)
        elif current is not None and line and not line.startswith(_LOG_PREFIXES):
            current.append(line)
        else:
            current = None
    joined = (_BOILERPLATE_RE.sub("", " ".join(parts)).strip() for parts in messages)
    return [message for message in joined if message]


def classify_message(message: str) -> tuple[type[DownloadError], bool]:
    """Map one yt-dlp error message to ``(error class, retryable)``."""
    text = _normalize(message)
    for cls, retryable, needles in _RULES:
        if any(needle in text for needle in needles):
            return cls, retryable
    return DownloadError, False


def classify_ytdlp_error(
    stderr: str,
    *,
    stdout: str | None = None,
    returncode: int | None = None,
) -> DownloadError:
    """Build the typed error for a failed yt-dlp run.

    The *last* ``ERROR:`` line decides; earlier ones are only used when the
    last one is generic. ``stdout`` is checked for yt-dlp's (non-error)
    "File is larger than max-filesize" notice.
    """
    tail = (stderr or "")[-STDERR_TAIL_CHARS:]
    if stdout:
        match = _MAX_FILESIZE_RE.search(stdout)
        if match:
            return TooLarge(match.group(0).strip(), stderr_tail=tail)
    messages = error_messages(stderr)
    for message in reversed(messages):
        cls, retryable = classify_message(message)
        if cls is not DownloadError or retryable:
            return cls(message, retryable=retryable, stderr_tail=tail)
    if messages:
        return DownloadError(messages[-1], stderr_tail=tail)
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if lines:
        cls, retryable = classify_message(lines[-1])
        return cls(lines[-1], retryable=retryable, stderr_tail=tail)
    status = "unknown" if returncode is None else str(returncode)
    return DownloadError(f"yt-dlp exited with status {status}", stderr_tail=tail)


# ---------------------------------------------------------------------------
# Subprocess runner (shared with src.media)
# ---------------------------------------------------------------------------

CAPTURE_LIMIT = 64 * 1024
KILL_WAIT_SECONDS = 5.0
READER_GRACE_SECONDS = 2.0
MONITOR_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


class _Capture:
    """Bounded capture keeping the beginning and the end of a stream."""

    def __init__(self, limit: int) -> None:
        self._head_limit = max(0, limit // 4)
        self._tail_limit = max(1, limit - self._head_limit)
        self._head = bytearray()
        self._tail = bytearray()
        self._dropped = 0

    def feed(self, data: bytes) -> None:
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        if not data:
            return
        self._tail += data
        excess = len(self._tail) - self._tail_limit
        if excess > 0:
            del self._tail[:excess]
            self._dropped += excess

    def text(self) -> str:
        head = self._head.decode("utf-8", errors="replace")
        tail = self._tail.decode("utf-8", errors="replace")
        if self._dropped:
            return f"{head}\n[... {self._dropped} bytes omitted ...]\n{tail}"
        return head + tail


async def _drain(stream: asyncio.StreamReader, sink: _Capture) -> None:
    while chunk := await stream.read(65536):
        sink.feed(chunk)


def _signal_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole process group of ``proc`` (its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    _signal_group(proc)
    try:
        async with asyncio.timeout(KILL_WAIT_SECONDS):
            await proc.wait()
    except TimeoutError:
        logger.warning(
            "process %s did not exit %ss after SIGKILL", proc.pid, KILL_WAIT_SECONDS
        )


async def _finish_readers(
    proc: asyncio.subprocess.Process, readers: list[asyncio.Task[None]]
) -> None:
    if not readers:
        return
    _, pending = await asyncio.wait(readers, timeout=READER_GRACE_SECONDS)
    if pending:
        # A straggler (e.g. a grandchild that outlived its parent) still holds
        # our pipes: the group still exists, kill it and stop reading.
        _signal_group(proc)
        _, pending = await asyncio.wait(pending, timeout=1.0)
        for task in pending:
            task.cancel()
    results = await asyncio.gather(*readers, return_exceptions=True)
    for result in results:
        if isinstance(result, OSError):
            logger.debug(
                "reading the output of process %s failed: %s", proc.pid, result
            )


async def _wait_monitored(
    proc: asyncio.subprocess.Process,
    monitor: Callable[[], None] | None,
    interval: float,
) -> int:
    if monitor is None:
        return await proc.wait()
    waiter = asyncio.ensure_future(proc.wait())
    try:
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=interval)
            if done:
                return waiter.result()
            monitor()
    finally:
        if not waiter.done():
            waiter.cancel()


async def run_command(
    argv: Sequence[str],
    *,
    time_limit: float | None,
    label: str,
    stdout_path: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    monitor: Callable[[], None] | None = None,
    monitor_interval: float = MONITOR_INTERVAL_SECONDS,
    capture_limit: int = CAPTURE_LIMIT,
) -> CommandResult:
    """Run ``argv`` in its own process group with a hard timeout.

    * stdout goes to ``stdout_path`` (created ``0600``) or to a bounded capture;
      stderr is always captured (bounded, head + tail).
    * On timeout, cancellation or when ``monitor()`` raises, the whole process
      group is SIGKILLed and reaped (bounded wait) before the exception
      propagates; a timeout raises :class:`DownloadTimeout`.
    * A non-zero exit status is *not* an exception: inspect ``returncode``.
    """
    stdout_fd: int | None = None
    if stdout_path is not None:
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if stdout_fd is None else stdout_fd,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
            cwd=cwd,
        )
    except OSError as exc:
        raise DownloadError(
            f"{label}: cannot start {argv[0]}: {exc.strerror or exc}"
        ) from exc
    finally:
        if stdout_fd is not None:
            os.close(stdout_fd)

    out_capture = _Capture(capture_limit)
    err_capture = _Capture(capture_limit)
    readers: list[asyncio.Task[None]] = []
    if proc.stderr is not None:
        readers.append(asyncio.create_task(_drain(proc.stderr, err_capture)))
    if proc.stdout is not None:
        readers.append(asyncio.create_task(_drain(proc.stdout, out_capture)))

    timed_out = False
    returncode = -1
    try:
        try:
            async with asyncio.timeout(time_limit):
                returncode = await _wait_monitored(proc, monitor, monitor_interval)
        except TimeoutError:
            timed_out = True
            await _terminate(proc)
        except BaseException:
            await _terminate(proc)
            raise
    finally:
        await _finish_readers(proc, readers)

    elapsed = time.monotonic() - started
    if timed_out:
        raise DownloadTimeout(
            f"{label} timed out after {time_limit:g}s (process group killed)",
            stderr_tail=err_capture.text(),
        )
    return CommandResult(returncode, out_capture.text(), err_capture.text(), elapsed)


# ---------------------------------------------------------------------------
# Format selection
# ---------------------------------------------------------------------------

# Sort: prefer the original audio language, the best resolution up to 1080p
# (smallest side, so portrait 1080x1920 counts as 1080p), SDR, direct HTTPS
# over HLS/DASH, then H.264 and AAC.
DEFAULT_FORMAT_SORT = "lang,res:1080,hdr:sdr,proto,vcodec:avc,acodec:aac"

# yt-dlp filters work per format, so a merged video+audio download is kept
# under a size limit by trying several video/audio splits, largest video first.
VIDEO_BUDGET_SHARES = (0.95, 0.9, 0.8, 0.7, 0.6)
FALLBACK_SHARES = (0.9, 0.75)
# Inputs for a size-targeted re-encode should not be much larger than the
# result: the output resolution is decided by the budget anyway.
COMPRESS_INPUT_FACTOR = 4
# A merged MP4 is slightly larger than its parts (sample tables): keep some
# headroom so that a selection "just under" the budget does not need a re-encode.
CONTAINER_HEADROOM_BYTES = 256 * 1024
CONTAINER_HEADROOM_RATIO = 0.005


def selection_budget(max_bytes: int) -> int:
    """Bytes the selected formats may use to end up <= ``max_bytes`` merged."""
    headroom = max(CONTAINER_HEADROOM_BYTES, int(max_bytes * CONTAINER_HEADROOM_RATIO))
    return max(1, max_bytes - headroom)


_H264_MP4 = "[vcodec~=?'(?i)^(avc|h\\.?264)'][ext=mp4]"
_AAC = "[acodec~='(?i)^(mp4a|aac|mp3)']"
_AAC_OR_UNKNOWN = "[acodec~=?'(?i)^(mp4a|aac|mp3)']"


def _size_filter(limit: float) -> str:
    value = max(1, int(limit))
    return f"[filesize<?{value}][filesize_approx<?{value}]"


def _within(limit: float, *, video_filter: str = "") -> list[str]:
    """Alternatives (merged splits, then progressive) whose size is <= limit."""
    alternatives = [
        f"bv*{video_filter}{_size_filter(limit * share)}+ba{_size_filter(limit * (1 - share))}"
        for share in FALLBACK_SHARES
    ]
    alternatives.append(f"b{video_filter}{_size_filter(limit)}")
    return alternatives


def build_format_spec(budget: int, max_download: int, max_side: int = 1280) -> str:
    """Default ``-f`` selector; yt-dlp uses the first alternative that matches.

    1. Telegram-ready, no conversion needed: H.264 (or unknown codec) MP4
       video + AAC/MP3 audio whose combined size fits ``budget`` (several
       video/audio splits), then progressive H.264 MP4 within ``budget`` (AAC
       or unknown audio, then any audio; silent-only posts land here too).
    2. Any codec within ``budget``: re-encoded (CRF, capped) by
       :func:`src.media.prepare_for_telegram`.
    3. Inputs for a size-targeted re-encode: longest side <= ``max_side``
       (``TRANSCODE_MAX_SIDE``) and at most ``COMPRESS_INPUT_FACTOR`` x
       ``budget``, then anything <= ``max_download`` (with that resolution
       cap first).
    4. Anything (``fetch_info`` refuses it when larger than ``max_download``).

    Within an alternative the sort (``-S``) decides: <= 1080p, H.264, AAC.
    Unknown sizes pass the filters (``<?``); with
    ``--compat-options manifest-filesize-approx`` yt-dlp estimates HLS/DASH
    sizes from their bitrate, so most of them are filtered too.
    """
    side = f"[width<=?{max_side}][height<=?{max_side}]"
    tiers: list[str] = []
    for share in VIDEO_BUDGET_SHARES:
        video = _size_filter(budget * share)
        audio = _size_filter(budget * (1 - share))
        tiers.append(f"bv*{_H264_MP4}{video}+ba{_AAC}{audio}")
    tiers.append(f"b{_H264_MP4}{_AAC_OR_UNKNOWN}{_size_filter(budget)}")
    tiers.append(f"b{_H264_MP4}{_size_filter(budget)}")
    tiers += _within(budget)
    tiers += _within(budget * COMPRESS_INPUT_FACTOR, video_filter=side)
    tiers += _within(max_download, video_filter=side)
    tiers += _within(max_download)
    tiers.append("bv*+ba/b")
    return "/".join(tiers)


def format_spec() -> str:
    """The effective ``-f`` value (``config.YTDLP_FORMAT`` overrides it)."""
    custom = setting("YTDLP_FORMAT", "")
    if custom:
        return str(custom)
    try:
        max_side = max(240, int(setting("TRANSCODE_MAX_SIDE", 1280)))
    except (TypeError, ValueError):
        max_side = 1280
    return build_format_spec(
        selection_budget(_int_setting("TELEGRAM_MAX_FILE_SIZE")),
        _int_setting("MAX_DOWNLOAD_BYTES"),
        max_side,
    )


def format_sort() -> str:
    """The effective ``-S`` value (``config.YTDLP_FORMAT_SORT`` overrides it)."""
    return str(setting("YTDLP_FORMAT_SORT", "") or DEFAULT_FORMAT_SORT)


def format_args(format_id: str | None = None) -> list[str]:
    """``-f``/``-S``/merge options; ``format_id`` pins an already chosen format."""
    spec = format_spec()
    if format_id:
        spec = f"{format_id}/{spec}"
    return [
        "--format",
        spec,
        "--format-sort",
        format_sort(),
        "--merge-output-format",
        "mp4",
        "--compat-options",
        "manifest-filesize-approx",
    ]


# ---------------------------------------------------------------------------
# yt-dlp command line
# ---------------------------------------------------------------------------


def ytdlp_command() -> list[str]:
    """The yt-dlp executable: the package installed for this interpreter."""
    return [sys.executable, "-m", "yt_dlp"]


def ytdlp_env() -> dict[str, str]:
    """Environment for yt-dlp: tools installed next to the interpreter
    (deno for YouTube, the yt-dlp script) are found even without an activated
    virtualenv."""
    env = dict(os.environ)
    bindir = os.path.dirname(sys.executable)
    path = env.get("PATH", "")
    if bindir and bindir not in path.split(os.pathsep):
        env["PATH"] = f"{bindir}{os.pathsep}{path}" if path else bindir
    return env


def _configured_headers() -> dict[str, str]:
    headers = getattr(config, "YTDLP_HEADERS_DICT", None)
    if isinstance(headers, dict) and headers:
        return {str(k): str(v) for k, v in headers.items()}
    raw = getattr(config, "YTDLP_HEADERS", None)
    parsed: dict[str, str] = {}
    if isinstance(raw, str):
        for part in raw.split("|"):
            key, sep, value = part.partition(":")
            if sep and key.strip():
                parsed[key.strip()] = value.strip()
    return parsed


def common_args(*, cookies_path: str | None = None) -> list[str]:
    """Options shared by every yt-dlp run (auth/impersonation included, always)."""
    args = [
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--no-update",
        "--no-mtime",
        "--ies",
        "default,-generic",
    ]
    user_agent = setting("YTDLP_USER_AGENT", "")
    if user_agent:
        args += ["--user-agent", str(user_agent)]
    for key, value in _configured_headers().items():
        args += ["--add-header", f"{key}:{value}"]
    impersonate = setting("YTDLP_IMPERSONATE", "")
    if impersonate:
        args += ["--impersonate", str(impersonate)]
    proxy = setting("YTDLP_PROXY", "")
    if proxy:
        args += ["--proxy", str(proxy)]
    if cookies_path:
        args += ["--cookies", cookies_path]
    from_browser = setting("YTDLP_COOKIES_FROM_BROWSER", "")
    if from_browser:
        args += ["--cookies-from-browser", str(from_browser)]
    return args


def build_fetch_argv(url: str, *, cookies_path: str | None = None) -> list[str]:
    """``yt-dlp -J`` for one link (playlists capped at ``MAX_ITEMS_PER_LINK``)."""
    max_items = max(1, _int_setting("MAX_ITEMS_PER_LINK"))
    return [
        *ytdlp_command(),
        *common_args(cookies_path=cookies_path),
        *format_args(),
        "--dump-single-json",
        "--playlist-items",
        f"1:{max_items}",
        "--",
        url,
    ]


def _escape_template(path: str) -> str:
    return path.replace("%", "%%")


def build_download_argv(
    info_json: str,
    *,
    output_template: str,
    paths_file: str,
    format_id: str | None = None,
    cookies_path: str | None = None,
) -> list[str]:
    """``yt-dlp --load-info-json`` for one item; the final path is written to
    ``paths_file`` after all post-processing (merge, fixups, move)."""
    return [
        *ytdlp_command(),
        *common_args(cookies_path=cookies_path),
        *format_args(format_id),
        "--max-filesize",
        str(_int_setting("MAX_DOWNLOAD_BYTES")),
        "--output",
        output_template,
        "--print-to-file",
        "after_move:filepath",
        _escape_template(paths_file),
        "--load-info-json",
        info_json,
    ]


_SENSITIVE_OPTIONS = {"--add-header", "--proxy", "--cookies-from-browser"}


def argv_for_log(argv: Sequence[str]) -> str:
    """Printable argv with header values/proxy/browser profile masked."""
    shown: list[str] = []
    mask_next = False
    for arg in argv:
        if mask_next:
            key, sep, _ = arg.partition(":")
            shown.append(
                f"{key}:***" if sep and not arg.startswith(("http", "socks")) else "***"
            )
            mask_next = False
            continue
        shown.append(arg)
        mask_next = arg in _SENSITIVE_OPTIONS
    return redact(" ".join(shown))


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


def _cookies_source() -> str | None:
    path = getattr(config, "YTDLP_COOKIES_PATH", None) or getattr(
        config, "YTDLP_COOKIES", None
    )
    return str(path) if path else None


def _copy_private(src: str, dst: str) -> None:
    with open(src, "rb") as inp:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            while chunk := inp.read(65536):
                out.write(chunk)


@contextlib.asynccontextmanager
async def private_cookies(workdir: str) -> AsyncIterator[str | None]:
    """Yield a private copy of the configured cookies file (or ``None``).

    yt-dlp rewrites its ``--cookies`` file when it exits; giving it a copy
    keeps the operator's file (often a read-only Secret mount) untouched and
    avoids concurrent runs corrupting each other. The copy is deleted after
    the run.
    """
    source = _cookies_source()
    copy_path: str | None = None
    if source:
        candidate = os.path.join(workdir, f".cookies-{uuid.uuid4().hex}.txt")
        try:
            await asyncio.to_thread(_copy_private, source, candidate)
            copy_path = candidate
        except OSError as exc:
            logger.warning(
                "Cannot copy the yt-dlp cookies file %r (%s); continuing without cookies",
                source,
                exc.strerror or exc,
            )
    try:
        yield copy_path
    finally:
        if copy_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(copy_path)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class MediaItem:
    """One downloadable entry of a link (a post may have several)."""

    index: int  # 1-based position in the post (also used by download(items=...))
    id: str
    title: str | None
    kind: str  # "video" | "audio" | "unknown"
    duration: float | None
    width: int | None
    height: int | None
    filesize: int | None  # exact/approx size of the selected format(s), None if unknown
    direct_url: str | None  # single progressive MP4 usable by Telegram's URL fetch
    direct_ext: str | None
    http_headers: dict[str, str]
    thumbnail: str | None
    uploader: str | None
    webpage_url: str | None
    # additive fields
    format_id: str | None = None
    ext: str | None = None
    vcodec: str | None = None
    acodec: str | None = None


@dataclass
class MediaInfo:
    url: str
    extractor_key: str
    title: str | None
    is_live: bool
    items: list[MediaItem]
    info_path: str  # yt-dlp info JSON in the workdir (used by download())
    raw: dict[str, Any]  # trimmed, secret-free metadata
    # additive fields
    entry_count: int = 1  # entries reported by the site (may exceed len(items))
    skipped: dict[int, str] = field(default_factory=dict)  # index -> error code

    @property
    def is_playlist(self) -> bool:
        return self.raw.get("_type") in ("playlist", "multi_video")


@dataclass
class DownloadedFile:
    path: str
    size: int
    item: MediaItem


INFO_JSON_NAME = "info.json"
_LIVE_STATUSES = {"is_live", "is_upcoming", "post_live"}
_AUDIO_EXTS = {"mp3", "m4a", "aac", "opus", "ogg", "oga", "wav", "flac", "weba", "mka"}
_VIDEO_EXTS = {"mp4", "m4v", "mov", "webm", "mkv", "flv", "ts", "3gp", "avi"}
_H264_RE = re.compile(r"(?i)^(avc|h\.?264)")
_DIRECT_AUDIO_RE = re.compile(r"(?i)^(mp4a|aac|mp3)")
_RAW_KEYS = (
    "_type",
    "id",
    "title",
    "fulltitle",
    "extractor",
    "extractor_key",
    "webpage_url",
    "original_url",
    "uploader",
    "uploader_id",
    "channel",
    "channel_id",
    "duration",
    "upload_date",
    "timestamp",
    "thumbnail",
    "view_count",
    "like_count",
    "live_status",
    "is_live",
    "playlist_count",
    "format_id",
    "ext",
    "width",
    "height",
    "vcodec",
    "acodec",
)


def item_prefix(index: int) -> str:
    return f"item-{index:02d}"


def _item_info_name(index: int) -> str:
    return f"info-{index:02d}.json"


def _paths_name(index: int) -> str:
    return f"paths-{index:02d}.txt"


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _selected_formats(entry: dict[str, Any]) -> list[dict[str, Any]]:
    requested = entry.get("requested_formats")
    if isinstance(requested, list) and requested:
        return [f for f in requested if isinstance(f, dict)]
    return [entry]


def _has_video(fmt: dict[str, Any]) -> bool | None:
    vcodec = fmt.get("vcodec")
    if vcodec == "none":
        return False
    if vcodec:
        return True
    if fmt.get("width") or fmt.get("height"):
        return True
    ext = str(fmt.get("ext") or "").lower()
    if ext in _AUDIO_EXTS:
        return False
    if ext in _VIDEO_EXTS:
        return True
    return None


def _has_audio(fmt: dict[str, Any]) -> bool | None:
    acodec = fmt.get("acodec")
    if acodec == "none":
        return False
    if acodec:
        return True
    return None


def _kind(formats: list[dict[str, Any]]) -> str:
    video = [_has_video(f) for f in formats]
    if any(v is True for v in video):
        return "video"
    if all(v is False for v in video) and any(
        _has_audio(f) is not False for f in formats
    ):
        return "audio"
    return "unknown"


def _format_size(fmt: dict[str, Any]) -> int | None:
    return _as_int(fmt.get("filesize")) or _as_int(fmt.get("filesize_approx"))


def _selected_size(formats: list[dict[str, Any]]) -> int | None:
    sizes = [_format_size(f) for f in formats]
    if not sizes or any(size is None for size in sizes):
        return None
    return sum(size for size in sizes if size is not None)


def _estimated_size(
    formats: list[dict[str, Any]], duration: float | None
) -> int | None:
    """Like ``_selected_size`` but falls back to bitrate x duration."""
    total = 0
    for fmt in formats:
        size = _format_size(fmt)
        if size is None:
            tbr = _as_float(fmt.get("tbr"))
            if tbr is None or duration is None:
                return None
            size = int(tbr * 1000 / 8 * duration)
        total += size
    return total


def _direct_url(formats: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """URL of a single progressive MP4 (video+audio) Telegram could fetch."""
    if len(formats) != 1:
        return None, None
    fmt = formats[0]
    url = fmt.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None, None
    if str(fmt.get("protocol") or "https") not in ("http", "https"):
        return None, None
    if str(fmt.get("ext") or "").lower() != "mp4" or fmt.get("cookies"):
        return None, None
    vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
    if vcodec == "none" or acodec == "none":
        return None, None
    if vcodec and not _H264_RE.match(str(vcodec)):
        return None, None
    if acodec and not _DIRECT_AUDIO_RE.match(str(acodec)):
        return None, None
    return url, "mp4"


def _clean_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    return {str(k): str(v) for k, v in headers.items() if str(k).lower() != "cookie"}


def _make_item(index: int, entry: dict[str, Any]) -> MediaItem:
    formats = _selected_formats(entry)
    video_formats = [f for f in formats if _has_video(f)]
    first = video_formats[0] if video_formats else formats[0]
    direct_url, direct_ext = _direct_url(formats)
    width = _as_int(entry.get("width")) or _as_int(first.get("width"))
    height = _as_int(entry.get("height")) or _as_int(first.get("height"))
    uploader = entry.get("uploader") or entry.get("channel") or entry.get("uploader_id")
    return MediaItem(
        index=index,
        id=str(entry.get("id") or index),
        title=entry.get("title") or None,
        kind=_kind(formats),
        duration=_as_float(entry.get("duration")),
        width=width or None,
        height=height or None,
        filesize=_selected_size(formats),
        direct_url=direct_url,
        direct_ext=direct_ext,
        http_headers=_clean_headers(first.get("http_headers")),
        thumbnail=entry.get("thumbnail") or None,
        uploader=str(uploader) if uploader else None,
        webpage_url=entry.get("webpage_url") or None,
        format_id=str(entry["format_id"]) if entry.get("format_id") else None,
        ext=entry.get("ext") or None,
        vcodec=entry.get("vcodec") or first.get("vcodec") or None,
        acodec=entry.get("acodec") or None,
    )


def _entry_problem(entry: dict[str, Any] | None) -> DownloadError | None:
    if not isinstance(entry, dict):
        return NoMedia("the site returned no downloadable media for this item")
    if entry.get("is_live") or entry.get("live_status") in _LIVE_STATUSES:
        status = entry.get("live_status") or "is_live"
        return LiveStream(f"live content cannot be downloaded (live_status={status})")
    if not (
        entry.get("format_id") or entry.get("requested_formats") or entry.get("url")
    ):
        return NoMedia("no downloadable video or audio formats")
    duration = _as_float(entry.get("duration"))
    max_duration = _int_setting("MAX_DURATION_SECONDS")
    if max_duration > 0 and duration is not None and duration > max_duration:
        return TooLong(f"duration {duration:.0f}s exceeds the {max_duration}s limit")
    estimate = _estimated_size(_selected_formats(entry), duration)
    max_download = _int_setting("MAX_DOWNLOAD_BYTES")
    if estimate is not None and estimate > max_download:
        return TooLarge(
            f"smallest suitable download is about {estimate / MIB:.0f} MiB "
            f"(limit {max_download / MIB:.0f} MiB)"
        )
    return None


def _flatten(data: dict[str, Any], depth: int = 0) -> Iterable[Any]:
    if data.get("_type") not in ("playlist", "multi_video") or depth > 2:
        yield data
        return
    for entry in data.get("entries") or []:
        if isinstance(entry, dict) and entry.get("_type") in (
            "playlist",
            "multi_video",
        ):
            yield from _flatten(entry, depth + 1)
        else:
            yield entry  # may be None: an entry yt-dlp failed to process


def _iter_entries(data: dict[str, Any]) -> Iterable[tuple[int, Any]]:
    """Yield ``(index, entry)`` in post order (1-based; nested playlists are
    flattened, failed entries keep their position)."""
    return enumerate(_flatten(data), start=1)


def _trim_raw(data: dict[str, Any], items: list[MediaItem]) -> dict[str, Any]:
    raw = {key: data[key] for key in _RAW_KEYS if data.get(key) is not None}
    description = data.get("description")
    if isinstance(description, str) and description:
        raw["description"] = _clip(description, 1000)
    raw["items"] = [
        {"index": it.index, "id": it.id, "kind": it.kind, "duration": it.duration}
        for it in items
    ]
    return raw


def _first_problem(problems: dict[int, DownloadError]) -> DownloadError:
    for cls in (LiveStream, TooLong, TooLarge, NoMedia):
        for problem in problems.values():
            if isinstance(problem, cls):
                return problem
    return next(iter(problems.values()), NoMedia("no downloadable media found"))


@dataclass
class _Extraction:
    media: MediaInfo
    problems: dict[int, DownloadError]


def _write_json(path: str, data: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # -J prints a single JSON document; be lenient with trailing noise
        first_line = text.splitlines()[0]
        try:
            return json.loads(first_line)
        except json.JSONDecodeError:
            logger.warning("yt-dlp wrote an unparsable info JSON (%d bytes)", len(text))
            return None


def _build_media_info(url: str, data: dict[str, Any], info_path: str) -> _Extraction:
    max_items = max(1, _int_setting("MAX_ITEMS_PER_LINK"))
    is_playlist = data.get("_type") in ("playlist", "multi_video")
    entries = list(_iter_entries(data))
    items: list[MediaItem] = []
    problems: dict[int, DownloadError] = {}
    selected = entries[:max_items]
    for index, entry in selected:
        problem = _entry_problem(entry)
        if problem is not None:
            problems[index] = problem
            continue
        items.append(_make_item(index, entry))
        if is_playlist:
            item_path = os.path.join(os.path.dirname(info_path), _item_info_name(index))
            _write_json(item_path, entry)
    entry_count = _as_int(data.get("playlist_count")) or len(entries) or 1
    media = MediaInfo(
        url=url,
        extractor_key=str(data.get("extractor_key") or data.get("ie_key") or ""),
        title=data.get("title") or (items[0].title if items else None),
        is_live=bool(data.get("is_live")) or data.get("live_status") in _LIVE_STATUSES,
        items=items,
        info_path=info_path,
        raw=_trim_raw(data, items),
        entry_count=max(entry_count, len(selected)),
        skipped={index: problem.code for index, problem in problems.items()},
    )
    return _Extraction(media, problems)


def _load_extraction(url: str, info_path: str) -> _Extraction | None:
    data = _read_json(info_path)
    if not isinstance(data, dict):
        return None
    return _build_media_info(url, data, info_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_info(
    url: str,
    workdir: str,
    *,
    timeout: float | None = None,  # noqa: ASYNC109, RUF100
) -> MediaInfo:
    """Extract the metadata of ``url`` with ONE yt-dlp run (``-J``).

    The info JSON is saved as ``<workdir>/info.json`` (plus ``info-NN.json``
    per item of multi-item posts) and reused by :func:`download`. Raises
    :class:`LiveStream`, :class:`TooLong`, :class:`TooLarge`, :class:`NoMedia`
    or the error classified from yt-dlp's stderr.
    """
    if timeout is None:
        timeout = float(setting("EXTRACT_TIMEOUT"))
    await asyncio.to_thread(os.makedirs, workdir, exist_ok=True)
    info_path = os.path.join(workdir, INFO_JSON_NAME)
    async with private_cookies(workdir) as cookies_path:
        argv = build_fetch_argv(url, cookies_path=cookies_path)
        logger.debug("yt-dlp extraction: %s", argv_for_log(argv))
        result = await run_command(
            argv,
            time_limit=timeout,
            label="yt-dlp extraction",
            stdout_path=info_path,
            env=ytdlp_env(),
        )
    extraction = await asyncio.to_thread(_load_extraction, url, info_path)
    failure = (
        classify_ytdlp_error(result.stderr, returncode=result.returncode)
        if result.returncode != 0
        else None
    )
    if extraction is None:
        if failure is not None:
            raise failure
        raise NoMedia("yt-dlp returned no metadata", stderr_tail=result.stderr)
    media = extraction.media
    if not media.items:
        if failure is not None:
            raise failure
        raise _first_problem(extraction.problems)
    if failure is not None:
        logger.warning(
            "yt-dlp reported errors for %s, continuing with %d item(s): %s",
            redact(url),
            len(media.items),
            failure.detail,
        )
    if media.skipped:
        logger.info("Skipped items of %s: %s", redact(url), media.skipped)
    logger.info(
        "Extracted %s [%s]: %d item(s) (%s) in %.1fs",
        redact(url),
        media.extractor_key,
        len(media.items),
        ", ".join(f"{it.kind}:{it.format_id}" for it in media.items),
        result.elapsed,
    )
    return media


def _select_items(info: MediaInfo, items: Sequence[int] | None) -> list[MediaItem]:
    if items is None:
        return list(info.items)
    wanted = set(items)
    unknown = wanted - {item.index for item in info.items}
    if unknown:
        raise ValueError(f"unknown item index(es): {sorted(unknown)}")
    return [item for item in info.items if item.index in wanted]


def _prepare_item_files(
    info: MediaInfo, item: MediaItem, workdir: str
) -> tuple[str, str]:
    """Return ``(info_json, paths_file)`` for ``item``; clears stale outputs."""
    os.makedirs(workdir, exist_ok=True)
    prefix = item_prefix(item.index) + "."
    with os.scandir(workdir) as entries:
        stale = [entry.path for entry in entries if entry.name.startswith(prefix)]
    for path in stale:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    paths_file = os.path.join(workdir, _paths_name(item.index))
    with contextlib.suppress(FileNotFoundError):
        os.unlink(paths_file)
    if not info.is_playlist:
        return info.info_path, paths_file
    item_json = os.path.join(
        os.path.dirname(info.info_path), _item_info_name(item.index)
    )
    if not os.path.exists(item_json):
        data = _read_json(info.info_path)
        if not isinstance(data, dict):
            raise NoMedia("the saved info JSON is missing or unreadable")
        entry = next((e for i, e in _iter_entries(data) if i == item.index), None)
        if not isinstance(entry, dict):
            raise NoMedia(f"item {item.index} is not in the saved info JSON")
        _write_json(item_json, entry)
    return item_json, paths_file


def _read_final_path(paths_file: str) -> str | None:
    try:
        with open(paths_file, encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip()]
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        if os.path.isfile(line) and os.path.getsize(line) > 0:
            return line
    return None


class _OutputSizeGuard:
    """Raises :class:`TooLarge` when an item's files exceed ``limit`` bytes.

    yt-dlp's ``--max-filesize`` only checks the HTTP Content-Length; this guard
    also covers HLS/DASH fragments and chunked responses. Merge/fixup temp
    files (``*.temp.*``) are not counted.
    """

    def __init__(self, directory: str, prefix: str, limit: int) -> None:
        self.directory = directory
        self.prefix = prefix
        self.limit = limit

    def total(self) -> int:
        total = 0
        with os.scandir(self.directory) as entries:
            for entry in entries:
                if not entry.name.startswith(self.prefix) or ".temp." in entry.name:
                    continue
                with contextlib.suppress(FileNotFoundError):
                    total += entry.stat(follow_symlinks=False).st_size
        return total

    def __call__(self) -> None:
        total = self.total()
        if total > self.limit:
            raise TooLarge(
                f"download exceeded {self.limit / MIB:.0f} MiB ({total / MIB:.0f} MiB written)"
            )


async def _download_item(
    info: MediaInfo,
    item: MediaItem,
    workdir: str,
    *,
    time_limit: float,
) -> DownloadedFile:
    info_json, paths_file = await asyncio.to_thread(
        _prepare_item_files, info, item, workdir
    )
    prefix = item_prefix(item.index)
    output_template = _escape_template(os.path.join(workdir, prefix)) + ".%(ext)s"
    max_download = _int_setting("MAX_DOWNLOAD_BYTES")
    guard = _OutputSizeGuard(workdir, prefix + ".", max_download)
    async with private_cookies(workdir) as cookies_path:
        argv = build_download_argv(
            info_json,
            output_template=output_template,
            paths_file=paths_file,
            format_id=item.format_id,
            cookies_path=cookies_path,
        )
        logger.debug("yt-dlp download: %s", argv_for_log(argv))
        result = await run_command(
            argv,
            time_limit=time_limit,
            label=f"yt-dlp download of item {item.index}",
            env=ytdlp_env(),
            monitor=guard,
        )
    final_path = await asyncio.to_thread(_read_final_path, paths_file)
    if final_path is None:
        if _MAX_FILESIZE_RE.search(result.stdout) or result.returncode != 0:
            raise classify_ytdlp_error(
                result.stderr, stdout=result.stdout, returncode=result.returncode
            )
        raise NoMedia(
            "yt-dlp finished without producing a file", stderr_tail=result.stderr
        )
    if result.returncode != 0:
        logger.warning(
            "yt-dlp exited with %s after producing %s: %s",
            result.returncode,
            final_path,
            classify_ytdlp_error(result.stderr).detail,
        )
    final_ext = os.path.splitext(final_path)[1].lstrip(".").lower()
    if item.kind == "video" and final_ext in _AUDIO_EXTS:
        # e.g. the video part was skipped and only the audio part survived
        raise NoMedia(f"only an audio file ({final_ext}) was produced for a video item")
    size = await asyncio.to_thread(os.path.getsize, final_path)
    if size > max_download:
        raise TooLarge(
            f"downloaded file is {size / MIB:.0f} MiB (limit {max_download / MIB:.0f})"
        )
    logger.info(
        "Downloaded item %d (%s) -> %s (%.1f MiB) in %.1fs",
        item.index,
        item.format_id,
        os.path.basename(final_path),
        size / MIB,
        result.elapsed,
    )
    return DownloadedFile(path=final_path, size=size, item=item)


async def download(
    info: MediaInfo,
    workdir: str,
    *,
    items: Sequence[int] | None = None,
    timeout: float | None = None,  # noqa: ASYNC109, RUF100
) -> list[DownloadedFile]:
    """Download the selected items of ``info`` from its saved info JSON.

    No re-extraction happens (``--load-info-json``). Returns the final files
    (after merge/fixups) in item order. ``items`` are :attr:`MediaItem.index`
    values (default: all). ``timeout`` bounds the whole call; when ``None``
    each item gets ``DOWNLOAD_TIMEOUT`` seconds.

    Raises :class:`TooLarge` when yt-dlp skipped a file because of
    ``--max-filesize`` (or the on-disk guard tripped), :class:`NoMedia` when
    nothing was produced. For multi-item posts, failed items are skipped
    (logged) as long as at least one file was produced; otherwise the first
    error is raised.
    """
    selected = _select_items(info, items)
    if not selected:
        raise NoMedia("no items to download")
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    per_item = float(setting("DOWNLOAD_TIMEOUT"))
    files: list[DownloadedFile] = []
    errors: list[DownloadError] = []
    for item in selected:
        remaining = per_item if deadline is None else deadline - loop.time()
        if remaining <= 0:
            errors.append(
                DownloadTimeout(f"no time left to download item {item.index}")
            )
            break
        try:
            files.append(
                await _download_item(info, item, workdir, time_limit=remaining)
            )
        except DownloadError as exc:
            if len(selected) == 1:
                raise
            logger.warning(
                "Item %d of %s failed: [%s] %s",
                item.index,
                redact(info.url),
                exc.code,
                exc.detail,
            )
            errors.append(exc)
    if not files:
        raise errors[0] if errors else NoMedia("nothing was downloaded")
    return files
