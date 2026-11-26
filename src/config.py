"""Runtime configuration read from environment variables.

Every setting is parsed exactly once, at import time, with *tolerant* parsing:
an invalid value never crashes the process (e.g. Helm rendering an integer as
``5.24288e+07``); it falls back to the default and the problem is recorded in
:data:`CONFIG_WARNINGS`, which the entrypoints log at startup.

This module has no side effects (it does not create directories or touch the
filesystem). Tests can override any setting with ``monkeypatch.setattr``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os

logger = logging.getLogger(__name__)

# Problems found while parsing the environment. Logged by the entrypoints.
CONFIG_WARNINGS: list[str] = []

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}

MIB = 1024 * 1024


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip()


def env_str(name: str, default: str | None = None) -> str | None:
    """Return the stripped value of ``name``; empty strings become ``default``."""
    value = _raw(name)
    if value is None or value == "":
        return default
    return value


def env_bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None or value == "":
        return default
    low = value.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    CONFIG_WARNINGS.append(f"{name}={value!r} is not a boolean; using {default}")
    return default


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Parse an integer, accepting float notation such as ``5.24288e+07``."""
    value = _raw(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        try:
            as_float = float(value)
        except ValueError:
            CONFIG_WARNINGS.append(f"{name}={value!r} is not a number; using {default}")
            return default
        if not math.isfinite(as_float):
            CONFIG_WARNINGS.append(f"{name}={value!r} is not finite; using {default}")
            return default
        parsed = int(as_float)
    if minimum is not None and parsed < minimum:
        CONFIG_WARNINGS.append(f"{name}={parsed} is below {minimum}; using {minimum}")
        parsed = minimum
    if maximum is not None and parsed > maximum:
        CONFIG_WARNINGS.append(f"{name}={parsed} is above {maximum}; using {maximum}")
        parsed = maximum
    return parsed


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = _raw(name)
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError:
        CONFIG_WARNINGS.append(f"{name}={value!r} is not a number; using {default}")
        return default
    if not math.isfinite(parsed):
        CONFIG_WARNINGS.append(f"{name}={value!r} is not finite; using {default}")
        return default
    if minimum is not None and parsed < minimum:
        CONFIG_WARNINGS.append(f"{name}={parsed} is below {minimum}; using {minimum}")
        parsed = minimum
    if maximum is not None and parsed > maximum:
        CONFIG_WARNINGS.append(f"{name}={parsed} is above {maximum}; using {maximum}")
        parsed = maximum
    return parsed


def env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Comma-separated list; blanks are dropped, order and case preserved."""
    value = _raw(name)
    if value is None or value == "":
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_rate_limits(raw: str | None, default: tuple[tuple[int, int], ...]):
    """Parse ``"1/10,3/60,5/3600"`` into ``((1, 10), (3, 60), (5, 3600))``.

    Each entry is ``<max requests>/<window seconds>``. Suffixes ``s``, ``m``,
    ``h`` and ``d`` are accepted on the window (``20/30d``). An empty string
    disables rate limiting; an invalid string keeps the default.
    """
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return ()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    limits: list[tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            count_s, window_s = part.split("/", 1)
            window_s = window_s.strip().lower()
            mult = 1
            if window_s and window_s[-1] in units:
                mult = units[window_s[-1]]
                window_s = window_s[:-1]
            count = int(count_s.strip())
            window = int(float(window_s) * mult)
            if count < 1 or window < 1:
                raise ValueError(part)
            limits.append((count, window))
        except ValueError:
            CONFIG_WARNINGS.append(
                f"RATE_LIMITS entry {part!r} is invalid (expected N/SECONDS); "
                "keeping the default limits"
            )
            return default
    return tuple(sorted(limits, key=lambda item: item[1]))


def parse_headers(raw: str | None) -> dict[str, str]:
    """Parse ``'Header: Value|Other: Value'`` into a dict."""
    headers: dict[str, str] = {}
    if not raw:
        return headers
    for part in raw.split("|"):
        if ":" not in part:
            if part.strip():
                CONFIG_WARNINGS.append(f"YTDLP_HEADERS entry {part!r} has no ':'")
            continue
        key, value = part.split(":", 1)
        if key.strip():
            headers[key.strip()] = value.strip()
    return headers


# ---------------------------------------------------------------------------
# Core / Telegram
# ---------------------------------------------------------------------------
BOT_TOKEN: str | None = env_str("BOT_TOKEN")

# "polling" (default) or "webhook". Webhook mode additionally needs WEBHOOK_URL;
# without it the bot falls back to polling (and logs a warning).
MODE: str = (env_str("MODE", "polling") or "polling").lower()
if MODE not in ("polling", "webhook"):
    CONFIG_WARNINGS.append(
        f"MODE={MODE!r} is not 'polling' or 'webhook'; using polling"
    )
    MODE = "polling"

# Public HTTPS URL Telegram should POST updates to (webhook mode). Legacy
# values containing "{token}" or the token itself are still accepted.
WEBHOOK_URL: str | None = env_str("WEBHOOK_URL")
# Path served by the bot for webhook updates (the public URL must route here).
WEBHOOK_PATH: str = env_str("WEBHOOK_PATH", "/webhook") or "/webhook"
# Secret sent by Telegram in X-Telegram-Bot-Api-Secret-Token. When unset it is
# derived deterministically from BOT_TOKEN (stable across restarts/replicas).
WEBHOOK_SECRET: str | None = env_str("WEBHOOK_SECRET")


def webhook_secret() -> str | None:
    """Return the webhook secret token (1-256 chars of [A-Za-z0-9_-])."""
    if WEBHOOK_SECRET:
        return WEBHOOK_SECRET
    if not BOT_TOKEN:
        return None
    return hashlib.sha256(f"webhook:{BOT_TOKEN}".encode()).hexdigest()[:48]


HOST: str = env_str("HOST", "0.0.0.0") or "0.0.0.0"
PORT: int = env_int("PORT", 8080, minimum=1, maximum=65535)
WORKERS: int = env_int("WORKERS", 2, minimum=1, maximum=32)

# Bot API cloud limit for uploads is 50 MB (raise it only with a local Bot API server).
TELEGRAM_MAX_FILE_SIZE: int = env_int("TELEGRAM_MAX_FILE_SIZE", 50 * MIB, minimum=MIB)
# Telegram servers fetch media by URL only up to 20 MB (photos 5 MB).
TELEGRAM_URL_FETCH_MAX_SIZE: int = env_int(
    "TELEGRAM_URL_FETCH_MAX_SIZE", 20 * MIB, minimum=0
)
# Try letting Telegram fetch small progressive files server-side before
# downloading them locally (saves bandwidth/CPU when it works).
TELEGRAM_SERVER_FETCH: bool = env_bool("TELEGRAM_SERVER_FETCH", True)
TELEGRAM_API_TIMEOUT: float = env_float("TELEGRAM_API_TIMEOUT", 60.0, minimum=1.0)
TELEGRAM_UPLOAD_TIMEOUT: float = env_float(
    "TELEGRAM_UPLOAD_TIMEOUT", 300.0, minimum=1.0
)
# Max automatic retries on Telegram 429 (flood wait) / transient network errors.
TELEGRAM_MAX_RETRIES: int = env_int("TELEGRAM_MAX_RETRIES", 3, minimum=0, maximum=10)
# Seconds reactions stay disabled for a chat after Telegram refuses them.
REACTION_DISABLE_SECONDS: int = env_int("REACTION_DISABLE_SECONDS", 300, minimum=0)
# Base URL of the Bot API (set it to a local Bot API server to upload up to 2 GB).
TELEGRAM_API_BASE: str | None = env_str("TELEGRAM_API_BASE")

# ---------------------------------------------------------------------------
# Logging / health
# ---------------------------------------------------------------------------
LOG_LEVEL: str = (env_str("LOG_LEVEL", "INFO") or "INFO").upper()
LOG_TO_FILE: bool = env_bool("LOG_TO_FILE", False)
LOG_FILE: str = env_str("LOG_FILE", os.path.join("data", "telegram_downloader.log"))
LOG_MAX_BYTES: int = env_int("LOG_MAX_BYTES", 5 * MIB, minimum=1024)
LOG_BACKUP_COUNT: int = env_int("LOG_BACKUP_COUNT", 5, minimum=0)
# Expose diagnostic details on /health (keep false in production).
HEALTH_DEBUG: bool = env_bool("HEALTH_DEBUG", False)

# ---------------------------------------------------------------------------
# Download (yt-dlp)
# ---------------------------------------------------------------------------
TMP_DIR: str = (
    env_str("TMP_DIR", "/tmp/telegram_downloader") or "/tmp/telegram_downloader"
)
KEEP_DOWNLOADED_FILES: bool = env_bool("KEEP_DOWNLOADED_FILES", False)
# Only run a plain yt-dlp download and upload its output verbatim (no remux,
# no transcode). Useful for debugging.
SIMPLE_YTDLP_ONLY: bool = env_bool("SIMPLE_YTDLP_ONLY", False)

EXTRACT_TIMEOUT: int = env_int("EXTRACT_TIMEOUT", 60, minimum=5)
DOWNLOAD_TIMEOUT: int = env_int("DOWNLOAD_TIMEOUT", 300, minimum=10)
# Hard cap for a raw download (before compression to TELEGRAM_MAX_FILE_SIZE).
MAX_DOWNLOAD_BYTES: int = env_int("MAX_DOWNLOAD_BYTES", 500 * MIB, minimum=MIB)
# Refuse media longer than this many seconds (0 = no limit).
MAX_DURATION_SECONDS: int = env_int("MAX_DURATION_SECONDS", 3600, minimum=0)
# Max media items delivered for a multi-item post (carousel, multi-video post).
MAX_ITEMS_PER_LINK: int = env_int("MAX_ITEMS_PER_LINK", 10, minimum=1, maximum=10)

# yt-dlp format selection. Defaults prefer Telegram-friendly H.264/AAC MP4 that
# fits the upload limit, falling back to the best available streams (which the
# worker then converts/compresses).
YTDLP_FORMAT: str | None = env_str("YTDLP_FORMAT")
YTDLP_FORMAT_SORT: str | None = env_str("YTDLP_FORMAT_SORT")
YTDLP_USER_AGENT: str | None = env_str("YTDLP_USER_AGENT")
YTDLP_COOKIES_PATH: str | None = env_str("YTDLP_COOKIES_PATH") or env_str(
    "YTDLP_COOKIES"
)
YTDLP_COOKIES = YTDLP_COOKIES_PATH  # backwards-compatible alias
YTDLP_COOKIES_FROM_BROWSER: str | None = env_str("YTDLP_COOKIES_FROM_BROWSER")
# Extra HTTP headers for yt-dlp, format 'Header: Value|Other: Value'.
YTDLP_HEADERS: str | None = env_str("YTDLP_HEADERS")
YTDLP_HEADERS_DICT: dict[str, str] = parse_headers(YTDLP_HEADERS)
# Optional impersonation target passed to --impersonate (e.g. "chrome").
YTDLP_IMPERSONATE: str | None = env_str("YTDLP_IMPERSONATE")
# Optional proxy for yt-dlp (e.g. "socks5://host:1080").
YTDLP_PROXY: str | None = env_str("YTDLP_PROXY")

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
# Restrict accepted providers (yt-dlp extractor names such as "youtube",
# "twitter", "reddit", or site domains such as "vimeo.com"). Empty = every
# provider supported by yt-dlp (the generic extractor is never used).
ALLOWED_PROVIDERS: tuple[str, ...] = env_list("ALLOWED_PROVIDERS")
BLOCKED_PROVIDERS: tuple[str, ...] = env_list("BLOCKED_PROVIDERS")
# Accept profile/channel/playlist/album links (the first MAX_ITEMS_PER_LINK
# items are posted). Off by default: one link = one post.
ALLOW_COLLECTION_LINKS: bool = env_bool("ALLOW_COLLECTION_LINKS", False)
# When to answer links the bot cannot handle: "private" (default), "always",
# "never".
UNSUPPORTED_LINK_REPLY: str = (
    env_str("UNSUPPORTED_LINK_REPLY", "private") or "private"
).lower()
if UNSUPPORTED_LINK_REPLY not in ("private", "always", "never"):
    CONFIG_WARNINGS.append(
        f"UNSUPPORTED_LINK_REPLY={UNSUPPORTED_LINK_REPLY!r} is invalid; using 'private'"
    )
    UNSUPPORTED_LINK_REPLY = "private"

# ---------------------------------------------------------------------------
# Media processing (ffmpeg)
# ---------------------------------------------------------------------------
WORKER_GENERATE_THUMBNAIL: bool = env_bool("WORKER_GENERATE_THUMBNAIL", True)
TRANSCODE_PRESET: str = env_str("TRANSCODE_PRESET", "veryfast") or "veryfast"
TRANSCODE_TIMEOUT: int = env_int("TRANSCODE_TIMEOUT", 900, minimum=30)
# Max concurrent ffmpeg transcodes (CPU heavy).
TRANSCODE_CONCURRENCY: int = env_int("TRANSCODE_CONCURRENCY", 1, minimum=1, maximum=8)
# Longest side (pixels) used when a size-targeted re-encode needs downscaling.
TRANSCODE_MAX_SIDE: int = env_int("TRANSCODE_MAX_SIDE", 1280, minimum=240)

# ---------------------------------------------------------------------------
# Queue, rate limits, duplicates
# ---------------------------------------------------------------------------
# Max queued + running requests per chat; further links are refused (only
# the new link — the chat is not blocked).
MAX_PENDING_PER_CHAT: int = env_int("MAX_PENDING_PER_CHAT", 3, minimum=1)
# Sliding windows per chat, "<max>/<seconds>" comma-separated. Empty disables.
_DEFAULT_RATE_LIMITS = ((1, 10), (3, 60), (5, 3600), (20, 30 * 86400))
RATE_LIMITS: tuple[tuple[int, int], ...] = parse_rate_limits(
    _raw("RATE_LIMITS"), _DEFAULT_RATE_LIMITS
)
# If the next free slot is at most this far away the link is accepted and
# processed automatically later; otherwise it is refused with a retry hint.
RATE_LIMIT_MAX_DEFER_SECONDS: int = env_int(
    "RATE_LIMIT_MAX_DEFER_SECONDS", 120, minimum=0
)
# Same chat + same (normalized) URL within this window is a duplicate.
DUPLICATE_WINDOW_SECONDS: int = env_int("DUPLICATE_WINDOW_SECONDS", 90 * 60, minimum=0)
DUPLICATE_DEFAULT = 90 * 60  # backwards-compatible name
# Minimum seconds between two rate-limit / duplicate notices to the same chat.
NOTIFY_RATE_THROTTLE_SECONDS: int = env_int(
    "NOTIFY_RATE_THROTTLE_SECONDS", 60, minimum=0
)
NOTIFY_DUPLICATE_THROTTLE_SECONDS: int = env_int(
    "NOTIFY_DUPLICATE_THROTTLE_SECONDS", 10 * 60, minimum=0
)

# On startup, keep processing requests persisted as 'queued' (true) or mark
# them 'aborted' (false).
WORKER_REHYDRATE_ON_START: bool = env_bool("WORKER_REHYDRATE_ON_START", True)
# How often the worker polls the DB queue for work queued by other processes
# (e.g. "requeue" from the admin GUI). Legacy name: WORKER_PERIODIC_REHYDRATE_SECONDS.
WORKER_POLL_SECONDS: float = env_float(
    "WORKER_POLL_SECONDS",
    env_float("WORKER_PERIODIC_REHYDRATE_SECONDS", 2.0, minimum=0.2),
    minimum=0.2,
)
WORKER_PERIODIC_REHYDRATE_SECONDS = WORKER_POLL_SECONDS  # backwards-compatible
# How often the worker publishes its heartbeat/status row for the GUI.
WORKER_HEARTBEAT_SECONDS: float = env_float(
    "WORKER_HEARTBEAT_SECONDS", 5.0, minimum=1.0
)
# Seconds to wait for running jobs on shutdown before cancelling them.
WORKER_SHUTDOWN_TIMEOUT: int = env_int("WORKER_SHUTDOWN_TIMEOUT", 30, minimum=0)
# A request is retried at most this many times after crashes/restarts.
WORKER_MAX_ATTEMPTS: int = env_int("WORKER_MAX_ATTEMPTS", 3, minimum=1)

# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
# Delete event-log rows (table `updates`) older than this many days (0 = keep).
UPDATES_RETENTION_DAYS: int = env_int("UPDATES_RETENTION_DAYS", 14, minimum=0)
# Delete finished requests and their events older than this (0 = keep).
REQUESTS_RETENTION_DAYS: int = env_int("REQUESTS_RETENTION_DAYS", 0, minimum=0)

# ---------------------------------------------------------------------------
# Admin GUI
# ---------------------------------------------------------------------------
# Poll interval used by the GUI to tail the DB event log.
GUI_DB_POLL_SECONDS: float = env_float("GUI_DB_POLL_SECONDS", 1.0, minimum=0.2)


def validate() -> list[str]:
    """Return human-readable problems with the effective configuration."""
    problems = list(CONFIG_WARNINGS)
    if MODE == "webhook" and not WEBHOOK_URL:
        problems.append(
            "MODE=webhook but WEBHOOK_URL is not set: falling back to polling"
        )
    if YTDLP_COOKIES_FROM_BROWSER:
        problems.append(
            "YTDLP_COOKIES_FROM_BROWSER needs a browser profile inside the container; "
            "prefer YTDLP_COOKIES_PATH with an exported cookies.txt"
        )
    if YTDLP_COOKIES_PATH and not os.path.isfile(YTDLP_COOKIES_PATH):
        problems.append(f"YTDLP_COOKIES_PATH={YTDLP_COOKIES_PATH!r} does not exist")
    if TELEGRAM_MAX_FILE_SIZE > 50 * MIB and not TELEGRAM_API_BASE:
        problems.append(
            "TELEGRAM_MAX_FILE_SIZE is above 50 MB: this only works with a local "
            "Bot API server"
        )
    return problems


def summary() -> dict:
    """Non-secret settings, for startup logs and the admin GUI."""
    return {
        "mode": MODE,
        "webhook_configured": bool(WEBHOOK_URL),
        "workers": WORKERS,
        "telegram_max_file_size": TELEGRAM_MAX_FILE_SIZE,
        "telegram_server_fetch": TELEGRAM_SERVER_FETCH,
        "max_download_bytes": MAX_DOWNLOAD_BYTES,
        "max_duration_seconds": MAX_DURATION_SECONDS,
        "max_items_per_link": MAX_ITEMS_PER_LINK,
        "max_pending_per_chat": MAX_PENDING_PER_CHAT,
        "rate_limits": [list(item) for item in RATE_LIMITS],
        "rate_limit_max_defer_seconds": RATE_LIMIT_MAX_DEFER_SECONDS,
        "duplicate_window_seconds": DUPLICATE_WINDOW_SECONDS,
        "allowed_providers": list(ALLOWED_PROVIDERS),
        "blocked_providers": list(BLOCKED_PROVIDERS),
        "allow_collection_links": ALLOW_COLLECTION_LINKS,
        "local_bot_api": bool(TELEGRAM_API_BASE),
        "unsupported_link_reply": UNSUPPORTED_LINK_REPLY,
        "generate_thumbnail": WORKER_GENERATE_THUMBNAIL,
        "simple_ytdlp_only": SIMPLE_YTDLP_ONLY,
        "keep_downloaded_files": KEEP_DOWNLOADED_FILES,
        "transcode_concurrency": TRANSCODE_CONCURRENCY,
        "worker_poll_seconds": WORKER_POLL_SECONDS,
        "cookies_configured": bool(YTDLP_COOKIES_PATH or YTDLP_COOKIES_FROM_BROWSER),
        "proxy_configured": bool(YTDLP_PROXY),
    }
