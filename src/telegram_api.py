"""Raw Telegram Bot API client over the shared aiohttp session.

Every public coroutine returns the Bot API answer as a dict and never raises
for API or network errors::

    {"ok": True, "result": ...}
    {"ok": False, "error_code": 400, "description": "Bad Request: ...", "parameters": {...}}
    {"ok": False, "description": "network error: ..."}   # no error_code

Retries (bounded by ``config.TELEGRAM_MAX_RETRIES``):

* 429 Too Many Requests: after ``parameters.retry_after`` seconds (requests
  asking to wait longer than :data:`MAX_RETRY_AFTER` are returned to the
  caller, which can defer the job instead of holding a worker slot).
* Connection failures before the request was sent (DNS, refused, connect
  timeout): for every method.
* Ambiguous failures (read timeout, disconnect, HTTP 5xx): only for
  idempotent methods (edit/delete/reaction/webhook...). ``send*`` calls are
  not repeated, because Telegram may already have posted the message and a
  retry would post it twice.

Timeouts: ``config.TELEGRAM_API_TIMEOUT`` for normal calls,
``config.TELEGRAM_UPLOAD_TIMEOUT`` for media (uploads and URL fetches).

The bot token is part of every request URL: it is never logged; use
:func:`redact` for anything that may contain it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BufferedReader
from typing import Any

import aiohttp

from . import config, http_client

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_REACTIONS",
    "DEFAULT_API_BASE",
    "RedactTokenFilter",
    "api_base",
    "call",
    "delete_message",
    "delete_webhook",
    "edit_message_text",
    "get_me",
    "get_webhook_info",
    "install_log_redaction",
    "normalize_reaction",
    "redact",
    "send_audio",
    "send_document",
    "send_media",
    "send_media_group",
    "send_message",
    "send_photo",
    "send_video",
    "set_message_reaction",
    "set_webhook",
]

DEFAULT_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096  # UTF-16 code units
MAX_CAPTION_LENGTH = 1024
MAX_MEDIA_GROUP = 10
MAX_RETRY_AFTER = 60.0  # longer flood waits are returned to the caller
_MAX_BACKOFF = 8.0

# Emoji accepted by setMessageReaction (Bot API ReactionTypeEmoji), space
# separated; escapes keep the zero-width joiners of composed emoji visible.
_ALLOWED_REACTIONS_TEXT = (
    "\u2764 \U0001f44d \U0001f44e \U0001f525 \U0001f970 \U0001f44f "
    "\U0001f601 \U0001f914 \U0001f92f \U0001f631 \U0001f92c \U0001f622 "
    "\U0001f389 \U0001f929 \U0001f92e \U0001f4a9 \U0001f64f \U0001f44c "
    "\U0001f54a \U0001f921 \U0001f971 \U0001f974 \U0001f60d \U0001f433 "
    "\u2764\u200d\U0001f525 \U0001f31a \U0001f32d \U0001f4af \U0001f923 "
    "\u26a1 \U0001f34c \U0001f3c6 \U0001f494 \U0001f928 \U0001f610 "
    "\U0001f353 \U0001f37e \U0001f48b \U0001f595 \U0001f608 \U0001f634 "
    "\U0001f62d \U0001f913 \U0001f47b \U0001f468\u200d\U0001f4bb "
    "\U0001f440 \U0001f383 \U0001f648 \U0001f607 \U0001f628 \U0001f91d "
    "\u270d \U0001f917 \U0001fae1 \U0001f385 \U0001f384 \u2603 "
    "\U0001f485 \U0001f92a \U0001f5ff \U0001f192 \U0001f498 \U0001f649 "
    "\U0001f984 \U0001f618 \U0001f48a \U0001f64a \U0001f60e \U0001f47e "
    "\U0001f937\u200d\u2642 \U0001f937 \U0001f937\u200d\u2640 "
    "\U0001f621"
)
ALLOWED_REACTIONS = frozenset(_ALLOWED_REACTIONS_TEXT.split())

_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
_REDACTED = "<redacted>"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def redact(text: object, token: str | None = None) -> str:
    """``text`` with bot tokens (``123456:ABC...``) replaced by ``<redacted>``.

    ``token`` is also removed verbatim, even when it does not look like a
    real token (tests, misconfiguration).
    """
    value = "" if text is None else str(text)
    if token:
        value = value.replace(token, _REDACTED)
    return _TOKEN_RE.sub(_REDACTED, value)


class RedactTokenFilter(logging.Filter):
    """Logging filter that removes bot tokens from messages and tracebacks.

    Attach it to *handlers* (``handler.addFilter(RedactTokenFilter())``) so it
    also sees records of third-party loggers (aiohttp, aiogram).
    """

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True  # malformed record: let the handler report it
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction(target: logging.Logger | None = None) -> None:
    """Add :class:`RedactTokenFilter` to every handler of ``target`` (root)."""
    target = target or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactTokenFilter) for f in handler.filters):
            handler.addFilter(RedactTokenFilter())


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def api_base() -> str:
    """Bot API base URL (``config.TELEGRAM_API_BASE`` for a local Bot API server)."""
    base = getattr(config, "TELEGRAM_API_BASE", None) or DEFAULT_API_BASE
    return str(base).rstrip("/")


def _api_timeout() -> float:
    return float(getattr(config, "TELEGRAM_API_TIMEOUT", 60.0))


def _upload_timeout() -> float:
    return float(getattr(config, "TELEGRAM_UPLOAD_TIMEOUT", 300.0))


def _max_retries() -> int:
    return max(0, int(getattr(config, "TELEGRAM_MAX_RETRIES", 3)))


def _reaction_disable_seconds() -> float:
    return float(getattr(config, "REACTION_DISABLE_SECONDS", 300))


# ---------------------------------------------------------------------------
# Request core
# ---------------------------------------------------------------------------
_IDEMPOTENT_METHODS = frozenset(
    {
        "getMe",
        "getWebhookInfo",
        "setWebhook",
        "deleteWebhook",
        "editMessageText",
        "editMessageCaption",
        "deleteMessage",
        "setMessageReaction",
        "getChat",
        "getFile",
    }
)
_RETRY_HTTP_STATUSES = frozenset({500, 502, 503, 504})
_sleep = asyncio.sleep  # patched by tests


@dataclass(frozen=True)
class _Upload:
    field: str
    path: str
    filename: str
    content_type: str


@dataclass(frozen=True)
class _Outcome:
    result: dict[str, Any]
    retry: bool = False
    delay: float | None = None  # None: exponential backoff


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping | list | tuple):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _build_form(
    params: Mapping[str, Any], uploads: Sequence[_Upload]
) -> tuple[aiohttp.FormData, list[BufferedReader]]:
    """Multipart body; opened files must be closed by the caller."""
    form = aiohttp.FormData()
    for name, value in params.items():
        form.add_field(name, _form_value(value))
    handles: list[BufferedReader] = []
    try:
        for upload in uploads:
            handle = open(upload.path, "rb")  # noqa: SIM115 - closed by the caller
            handles.append(handle)
            form.add_field(
                upload.field,
                handle,
                filename=upload.filename,
                content_type=upload.content_type,
            )
    except OSError:
        for handle in handles:
            handle.close()
        raise
    return form, handles


async def _read_response(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        data = await resp.json(content_type=None)
    except (ValueError, UnicodeDecodeError):  # JSONDecodeError is a ValueError
        data = None
    if isinstance(data, dict) and "ok" in data:
        data["ok"] = data.get("ok") is True
        if not data["ok"]:
            data.setdefault("error_code", resp.status)
            data.setdefault("description", f"HTTP {resp.status}")
        return data
    return {
        "ok": False,
        "error_code": resp.status,
        "description": f"HTTP {resp.status}: unexpected response from Telegram",
    }


async def _post(
    token: str,
    method: str,
    params: Mapping[str, Any],
    form: aiohttp.FormData | None,
    timeout: float,
) -> dict[str, Any]:
    url = f"{api_base()}/bot{token}/{method}"
    session = await http_client.get_session()
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    if form is None:
        async with session.post(url, json=dict(params), timeout=client_timeout) as resp:
            return await _read_response(resp)
    async with session.post(url, data=form, timeout=client_timeout) as resp:
        return await _read_response(resp)


def _network_failure(exc: BaseException, token: str) -> dict[str, Any]:
    detail = str(exc) or "no details"
    return {
        "ok": False,
        "description": redact(f"network error: {type(exc).__name__}: {detail}", token),
    }


def _retry_after(result: Mapping[str, Any]) -> float | None:
    parameters = result.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    try:
        return max(0.0, float(parameters["retry_after"]))
    except (KeyError, TypeError, ValueError):
        return None


def _classify(method: str, result: dict[str, Any]) -> _Outcome:
    if result.get("ok"):
        return _Outcome(result)
    code = result.get("error_code")
    if code == 429:
        delay = _retry_after(result)
        if delay is not None and delay > MAX_RETRY_AFTER:
            return _Outcome(result)
        return _Outcome(result, retry=True, delay=delay)
    if code in _RETRY_HTTP_STATUSES:
        return _Outcome(result, retry=method in _IDEMPOTENT_METHODS)
    return _Outcome(result)


async def _attempt(
    token: str,
    method: str,
    params: Mapping[str, Any],
    uploads: Sequence[_Upload],
    timeout: float,
) -> _Outcome:
    form: aiohttp.FormData | None = None
    handles: list[BufferedReader] = []
    if uploads:
        try:
            form, handles = _build_form(params, uploads)
        except OSError as exc:
            return _Outcome(
                {"ok": False, "description": f"cannot read file: {exc.strerror or exc}"}
            )
    try:
        result = await _post(token, method, params, form, timeout)
    except aiohttp.ClientSSLError as exc:
        return _Outcome(_network_failure(exc, token))
    except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
        # The request never left this host: always safe to retry.
        return _Outcome(_network_failure(exc, token), retry=True)
    except (
        TimeoutError,
        aiohttp.ClientConnectionError,
        aiohttp.ClientPayloadError,
    ) as exc:
        # The request may have been processed: retry idempotent calls only.
        return _Outcome(
            _network_failure(exc, token), retry=method in _IDEMPOTENT_METHODS
        )
    except aiohttp.ClientError as exc:
        return _Outcome(_network_failure(exc, token))
    except (TypeError, ValueError) as exc:
        # Parameters that cannot be serialized: a caller bug, never retried.
        logger.warning("Telegram %s: invalid parameters", method, exc_info=True)
        return _Outcome(
            {"ok": False, "description": redact(f"invalid parameters: {exc}", token)}
        )
    finally:
        for handle in handles:
            handle.close()
    return _classify(method, result)


def _log_failure(method: str, result: Mapping[str, Any], token: str) -> None:
    code = result.get("error_code")
    description = redact(result.get("description"), token)
    if code in (401, 404):
        logger.error(
            "Telegram %s failed (%s): %s - check BOT_TOKEN/TELEGRAM_API_BASE",
            method,
            code,
            description,
        )
    elif not isinstance(code, int) or code == 429 or code >= 500:
        logger.warning("Telegram %s failed (%s): %s", method, code, description)
    else:
        logger.info("Telegram %s failed (%s): %s", method, code, description)


async def call(
    token: str,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    files: Sequence[_Upload] = (),
    timeout: float | None = None,
) -> dict[str, Any]:
    """Call a Bot API ``method``; ``None`` params are omitted. Never raises."""
    payload = {k: v for k, v in (params or {}).items() if v is not None}
    if timeout is None:
        timeout = _upload_timeout() if files else _api_timeout()
    retries = _max_retries()
    attempt = 0
    while True:
        outcome = await _attempt(token, method, payload, files, timeout)
        result = outcome.result
        if result.get("ok"):
            return result
        if not outcome.retry or attempt >= retries:
            _log_failure(method, result, token)
            return result
        delay = outcome.delay
        if delay is None:
            delay = min(2.0**attempt, _MAX_BACKOFF)
        attempt += 1
        logger.info(
            "Telegram %s failed (%s): %s; retry %d/%d in %.1fs",
            method,
            result.get("error_code"),
            redact(result.get("description"), token),
            attempt,
            retries,
            delay,
        )
        await _sleep(delay)


# ---------------------------------------------------------------------------
# Helpers for parameters
# ---------------------------------------------------------------------------
def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _truncate(text: str | None, limit: int) -> str | None:
    """Cut ``text`` to ``limit`` UTF-16 code units (Telegram's unit)."""
    if text is None or _utf16_len(text) <= limit:
        return text
    out: list[str] = []
    units = 0
    for ch in text:
        size = 2 if ord(ch) > 0xFFFF else 1
        if units + size > limit - 1:
            break
        out.append(ch)
        units += size
    return "".join(out) + "\u2026"


def _reply_parameters(message_id: Any) -> dict[str, Any] | None:
    """Reply that still sends when the original message was deleted."""
    if message_id is None:
        return None
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        logger.debug("Ignoring invalid reply_to_message_id %r", message_id)
        return None
    return {"message_id": mid, "allow_sending_without_reply": True}


def _is_remote(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _content_type(path: str, default: str) -> str:
    guessed, _encoding = mimetypes.guess_type(path)
    return guessed or default


def _local_upload(field: str, path: str, default_type: str) -> _Upload | None:
    if not os.path.isfile(path):
        return None
    return _Upload(
        field, path, os.path.basename(path), _content_type(path, default_type)
    )


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        number = _to_int(value)
        if number is not None and number >= 0:
            return number
    return None


def _sample_aspect_ratio(value: Any) -> float | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    num, _, den = value.partition(":")
    try:
        ratio = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return None
    return ratio if ratio > 0 else None


def _dims_from_meta(
    meta: Mapping[str, Any],
) -> tuple[int | None, int | None, int | None]:
    """(duration, width, height) from legacy ffprobe/yt-dlp metadata dicts.

    Coded dimensions are turned into display dimensions: swapped for 90/270
    degree rotations and scaled by a non-square sample aspect ratio.
    """
    fmt = meta.get("format")
    fmt_duration = fmt.get("duration") if isinstance(fmt, Mapping) else None
    duration = _first_int(meta.get("duration"), fmt_duration)
    if meta.get("display_width") and meta.get("display_height"):
        return (
            duration,
            _first_int(meta["display_width"]),
            _first_int(meta["display_height"]),
        )
    width = _first_int(meta.get("video_width"), meta.get("width"))
    height = _first_int(meta.get("video_height"), meta.get("height"))
    if width and height:
        sar = _sample_aspect_ratio(meta.get("video_sample_aspect_ratio"))
        if sar and abs(sar - 1.0) > 0.01:
            width = max(1, round(width * sar))
        rotation = _to_int(meta.get("video_rotation"))
        if rotation is None:
            rotation = _to_int(meta.get("rotation"))
        if rotation is not None and abs(rotation) % 180 == 90:
            width, height = height, width
    return duration, width, height


def _missing_file(path: str) -> dict[str, Any]:
    logger.warning("Cannot send %s: file not found", os.path.basename(path))
    return {"ok": False, "description": f"file not found: {os.path.basename(path)}"}


async def _send_file(
    token: str,
    method: str,
    field: str,
    chat_id: int | str,
    source: str,
    params: Mapping[str, Any],
    *,
    default_type: str,
    thumbnail: str | None = None,
) -> dict[str, Any]:
    """Send a local file (multipart) or a URL Telegram fetches itself."""
    payload: dict[str, Any] = {"chat_id": chat_id, **params}
    uploads: list[_Upload] = []
    if _is_remote(source):
        payload[field] = source
    else:
        upload = _local_upload(field, source, default_type)
        if upload is None:
            return _missing_file(source)
        uploads.append(upload)
    if thumbnail:
        thumb = _local_upload("thumbnail_file", thumbnail, "image/jpeg")
        if thumb is None:
            logger.debug("Thumbnail %s not found; sending without it", thumbnail)
        else:
            uploads.append(thumb)
            payload["thumbnail"] = "attach://thumbnail_file"
    return await call(token, method, payload, files=uploads, timeout=_upload_timeout())


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
async def get_me(token: str) -> dict[str, Any]:
    return await call(token, "getMe")


async def send_message(
    token: str,
    chat_id: int | str,
    text: str,
    reply_to_message_id: int | None = None,
    *,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = True,
    disable_notification: bool | None = None,
) -> dict[str, Any]:
    """sendMessage; replies never fail when the original message was deleted."""
    params = {
        "chat_id": chat_id,
        "text": _truncate(text, MAX_MESSAGE_LENGTH),
        "parse_mode": parse_mode,
        "reply_parameters": _reply_parameters(reply_to_message_id),
        "link_preview_options": (
            {"is_disabled": True} if disable_web_page_preview else None
        ),
        "disable_notification": disable_notification,
    }
    return await call(token, "sendMessage", params)


async def edit_message_text(
    token: str,
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = True,
) -> dict[str, Any]:
    """editMessageText; an unchanged text counts as success."""
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": _truncate(text, MAX_MESSAGE_LENGTH),
        "parse_mode": parse_mode,
        "link_preview_options": (
            {"is_disabled": True} if disable_web_page_preview else None
        ),
    }
    result = await call(token, "editMessageText", params)
    description = str(result.get("description") or "").lower()
    if not result.get("ok") and "message is not modified" in description:
        return {"ok": True, "result": True, "description": result.get("description")}
    return result


async def delete_message(
    token: str, chat_id: int | str, message_id: int
) -> dict[str, Any]:
    return await call(
        token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id}
    )


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
async def send_video(
    token: str,
    chat_id: int | str,
    video: str,
    *,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    duration: float | None = None,
    width: int | None = None,
    height: int | None = None,
    thumbnail: str | None = None,
    supports_streaming: bool = True,
    parse_mode: str | None = None,
    meta: Mapping[str, Any] | None = None,
    thumbnail_path: str | None = None,
) -> dict[str, Any]:
    """sendVideo for a local path (upload) or an http(s) URL (server-side fetch).

    ``meta`` and ``thumbnail_path`` are the legacy spellings used by the old
    worker: dimensions found in ``meta`` are used when not given explicitly.
    """
    if isinstance(meta, Mapping):
        meta_duration, meta_width, meta_height = _dims_from_meta(meta)
        duration = duration if duration is not None else meta_duration
        width = width if width is not None else meta_width
        height = height if height is not None else meta_height
    params = {
        "caption": _truncate(caption, MAX_CAPTION_LENGTH),
        "parse_mode": parse_mode,
        "reply_parameters": _reply_parameters(reply_to_message_id),
        "duration": _first_int(duration),
        "width": _first_int(width),
        "height": _first_int(height),
        "supports_streaming": True if supports_streaming else None,
    }
    return await _send_file(
        token,
        "sendVideo",
        "video",
        chat_id,
        video,
        params,
        default_type="video/mp4",
        thumbnail=thumbnail or thumbnail_path,
    )


async def send_audio(
    token: str,
    chat_id: int | str,
    audio: str,
    *,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    duration: float | None = None,
    title: str | None = None,
    performer: str | None = None,
    thumbnail: str | None = None,
    parse_mode: str | None = None,
) -> dict[str, Any]:
    """sendAudio (shown in the music player; MP3/M4A)."""
    params = {
        "caption": _truncate(caption, MAX_CAPTION_LENGTH),
        "parse_mode": parse_mode,
        "reply_parameters": _reply_parameters(reply_to_message_id),
        "duration": _first_int(duration),
        "title": title,
        "performer": performer,
    }
    return await _send_file(
        token,
        "sendAudio",
        "audio",
        chat_id,
        audio,
        params,
        default_type="audio/mpeg",
        thumbnail=thumbnail,
    )


async def send_document(
    token: str,
    chat_id: int | str,
    document: str,
    *,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    thumbnail: str | None = None,
    parse_mode: str | None = None,
    thumbnail_path: str | None = None,
) -> dict[str, Any]:
    params = {
        "caption": _truncate(caption, MAX_CAPTION_LENGTH),
        "parse_mode": parse_mode,
        "reply_parameters": _reply_parameters(reply_to_message_id),
    }
    return await _send_file(
        token,
        "sendDocument",
        "document",
        chat_id,
        document,
        params,
        default_type="application/octet-stream",
        thumbnail=thumbnail or thumbnail_path,
    )


async def send_photo(
    token: str,
    chat_id: int | str,
    photo: str,
    *,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = None,
) -> dict[str, Any]:
    params = {
        "caption": _truncate(caption, MAX_CAPTION_LENGTH),
        "parse_mode": parse_mode,
        "reply_parameters": _reply_parameters(reply_to_message_id),
    }
    return await _send_file(
        token, "sendPhoto", "photo", chat_id, photo, params, default_type="image/jpeg"
    )


_MEDIA_TYPES = ("photo", "video", "audio", "document")
_DEFAULT_TYPES = {
    "photo": "image/jpeg",
    "video": "video/mp4",
    "audio": "audio/mpeg",
    "document": "application/octet-stream",
}


def _group_error(description: str) -> dict[str, Any]:
    logger.warning("Invalid media group: %s", description)
    return {"ok": False, "description": description}


def _validate_group(items: Sequence[Mapping[str, Any]]) -> str | None:
    if not items:
        return "media group is empty"
    if len(items) > MAX_MEDIA_GROUP:
        return f"media group has {len(items)} items (max {MAX_MEDIA_GROUP})"
    kinds = {item.get("type") for item in items}
    unknown = kinds - set(_MEDIA_TYPES)
    if unknown:
        return f"unsupported media type(s): {sorted(map(str, unknown))}"
    for exclusive in ("audio", "document"):
        if exclusive in kinds and kinds != {exclusive}:
            return f"{exclusive} items can only be grouped with {exclusive} items"
    return None


def _input_media(
    index: int, item: Mapping[str, Any], uploads: list[_Upload]
) -> dict[str, Any] | str:
    """InputMedia JSON for one group item (an error string when invalid)."""
    kind = str(item["type"])
    source = item.get("path") or item.get("url")
    if not isinstance(source, str) or not source:
        return f"item {index} has no path or url"
    media: dict[str, Any] = {"type": kind}
    if _is_remote(source):
        media["media"] = source
    else:
        upload = _local_upload(f"file{index}", source, _DEFAULT_TYPES[kind])
        if upload is None:
            return f"file not found: {os.path.basename(source)}"
        uploads.append(upload)
        media["media"] = f"attach://file{index}"
    caption = _truncate(item.get("caption"), MAX_CAPTION_LENGTH)
    if caption:
        media["caption"] = caption
        if item.get("parse_mode"):
            media["parse_mode"] = item["parse_mode"]
    if kind in ("video", "audio") and (duration := _first_int(item.get("duration"))):
        media["duration"] = duration
    if kind == "video":
        for name in ("width", "height"):
            if (value := _first_int(item.get(name))) is not None:
                media[name] = value
        media["supports_streaming"] = bool(item.get("supports_streaming", True))
    if kind == "audio":
        for name in ("title", "performer"):
            if item.get(name):
                media[name] = str(item[name])
    thumbnail = item.get("thumbnail")
    if kind != "photo" and isinstance(thumbnail, str) and thumbnail:
        thumb = _local_upload(f"thumb{index}", thumbnail, "image/jpeg")
        if thumb is not None:
            uploads.append(thumb)
            media["thumbnail"] = f"attach://thumb{index}"
    return media


async def _send_single(
    token: str,
    chat_id: int | str,
    item: Mapping[str, Any],
    reply_to_message_id: int | None,
) -> dict[str, Any]:
    kind = item["type"]
    source = str(item.get("path") or item.get("url") or "")
    common = {
        "caption": item.get("caption"),
        "reply_to_message_id": reply_to_message_id,
        "parse_mode": item.get("parse_mode"),
    }
    if kind == "video":
        result = await send_video(
            token,
            chat_id,
            source,
            duration=item.get("duration"),
            width=item.get("width"),
            height=item.get("height"),
            thumbnail=item.get("thumbnail"),
            supports_streaming=bool(item.get("supports_streaming", True)),
            **common,
        )
    elif kind == "audio":
        result = await send_audio(
            token,
            chat_id,
            source,
            duration=item.get("duration"),
            title=item.get("title"),
            performer=item.get("performer"),
            thumbnail=item.get("thumbnail"),
            **common,
        )
    elif kind == "photo":
        result = await send_photo(token, chat_id, source, **common)
    else:
        result = await send_document(
            token, chat_id, source, thumbnail=item.get("thumbnail"), **common
        )
    if result.get("ok") and not isinstance(result.get("result"), list):
        result = {**result, "result": [result.get("result")]}
    return result


async def send_media_group(
    token: str,
    chat_id: int | str,
    media: Sequence[Mapping[str, Any]],
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    """sendMediaGroup (album) of up to 10 items.

    Items: ``{"type": "video"|"audio"|"document"|"photo", "path"|"url",
    "caption"?, "duration"?, "width"?, "height"?, "thumbnail"?, "title"?,
    "performer"?}``. Local files are uploaded with ``attach://`` names. Audio
    and documents can only be grouped with items of the same type. A single
    item is sent with the matching ``send*`` method; ``result`` is always a
    list of messages.
    """
    items = list(media)
    error = _validate_group(items)
    if error is not None:
        return _group_error(error)
    if len(items) == 1:
        return await _send_single(token, chat_id, items[0], reply_to_message_id)
    uploads: list[_Upload] = []
    input_media: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        entry = _input_media(index, item, uploads)
        if isinstance(entry, str):
            return _group_error(entry)
        input_media.append(entry)
    params = {
        "chat_id": chat_id,
        "media": input_media,
        "reply_parameters": _reply_parameters(reply_to_message_id),
    }
    return await call(
        token, "sendMediaGroup", params, files=uploads, timeout=_upload_timeout()
    )


_VIDEO_EXTS = frozenset({".mp4", ".m4v", ".mov"})
_AUDIO_EXTS = frozenset(
    {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav"}
)
_PHOTO_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _media_kind(path: str, meta: Mapping[str, Any] | None) -> str:
    ext = os.path.splitext(path)[1].lower()
    has_video = meta.get("has_video") if isinstance(meta, Mapping) else None
    has_audio = meta.get("has_audio") if isinstance(meta, Mapping) else None
    container = (
        str(meta.get("format") or "").lower() if isinstance(meta, Mapping) else ""
    )
    if ext in _VIDEO_EXTS or (has_video and "mp4" in container):
        if has_video is False:
            return "audio" if has_audio else "document"
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _PHOTO_EXTS:
        return "photo"
    return "document"


async def send_media(
    token: str,
    chat_id: int | str,
    file_path: str,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a local file with the best matching method (video/audio/photo/document)."""
    kind = _media_kind(file_path, meta)
    if kind == "video":
        return await send_video(
            token,
            chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            meta=meta,
        )
    if kind == "audio":
        duration = _dims_from_meta(meta)[0] if isinstance(meta, Mapping) else None
        return await send_audio(
            token,
            chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            duration=duration,
        )
    if kind == "photo":
        return await send_photo(
            token,
            chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
    return await send_document(
        token,
        chat_id,
        file_path,
        caption=caption,
        reply_to_message_id=reply_to_message_id,
    )


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------
# (chat_id, emoji) -> monotonic deadline; emoji None = every reaction.
_reaction_disabled_until: dict[tuple[int | str, str | None], float] = {}
_REACTION_EMOJI_ERRORS = ("reaction_invalid",)
_REACTION_CHAT_ERRORS = (
    "reactions are disabled",
    "reactions aren't allowed",
    "reactions not allowed",
    "reaction_not_allowed",
    "chat_admin_required",
    "not enough rights",
)


def normalize_reaction(emoji: str | None) -> str | None:
    """The Bot API spelling of ``emoji`` (no VS16), or ``None`` if not allowed."""
    if not isinstance(emoji, str):
        return None
    candidate = emoji.strip().replace("\ufe0f", "")
    return candidate if candidate in ALLOWED_REACTIONS else None


def _reactions_suppressed(chat_id: int | str, emoji: str | None) -> bool:
    now = time.monotonic()
    for key in ((chat_id, None), (chat_id, emoji)):
        deadline = _reaction_disabled_until.get(key)
        if deadline is None:
            continue
        if deadline > now:
            return True
        _reaction_disabled_until.pop(key, None)
    return False


def _suppress_reactions(
    chat_id: int | str, emoji: str | None, description: str
) -> None:
    seconds = _reaction_disable_seconds()
    if seconds <= 0:
        return
    _reaction_disabled_until[(chat_id, emoji)] = time.monotonic() + seconds
    logger.info(
        "Reactions %s disabled for chat %s for %.0fs: %s",
        "" if emoji is None else emoji,
        chat_id,
        seconds,
        description,
    )


async def set_message_reaction(
    token: str,
    chat_id: int | str,
    message_id: int,
    emoji: str | None = None,
    remove: bool = False,
    *,
    is_big: bool = False,
) -> dict[str, Any]:
    """Set the bot's reaction on a message (``None``/``remove`` clears it).

    The emoji must be in :data:`ALLOWED_REACTIONS` (no request otherwise).
    When Telegram refuses reactions in a chat they are not attempted again
    there for ``config.REACTION_DISABLE_SECONDS`` (per emoji for
    REACTION_INVALID, for every emoji when reactions are not allowed at all).
    """
    reaction: list[dict[str, str]] = []
    normalized: str | None = None
    if emoji is not None and not remove:
        normalized = normalize_reaction(emoji)
        if normalized is None:
            logger.warning("Refusing reaction %r: not an allowed reaction emoji", emoji)
            return {"ok": False, "description": f"reaction {emoji!r} is not allowed"}
        reaction = [{"type": "emoji", "emoji": normalized}]
    if _reactions_suppressed(chat_id, normalized):
        return {
            "ok": False,
            "description": "reactions temporarily disabled for this chat",
        }
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": reaction,
        "is_big": True if is_big else None,
    }
    result = await call(token, "setMessageReaction", params)
    if not result.get("ok") and result.get("error_code") in (400, 403):
        description = str(result.get("description") or "")
        lowered = description.lower()
        if any(marker in lowered for marker in _REACTION_CHAT_ERRORS):
            _suppress_reactions(chat_id, None, description)
        elif any(marker in lowered for marker in _REACTION_EMOJI_ERRORS):
            _suppress_reactions(chat_id, normalized, description)
    return result


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
async def set_webhook(
    token: str,
    url: str,
    allowed_updates: Sequence[str] | None = None,
    *,
    secret_token: str | None = None,
    drop_pending_updates: bool = False,
    max_connections: int | None = None,
) -> dict[str, Any]:
    """setWebhook; Telegram sends ``secret_token`` in X-Telegram-Bot-Api-Secret-Token."""
    params = {
        "url": url,
        "secret_token": secret_token,
        "allowed_updates": (
            list(allowed_updates) if allowed_updates is not None else None
        ),
        "drop_pending_updates": True if drop_pending_updates else None,
        "max_connections": max_connections,
    }
    return await call(token, "setWebhook", params)


async def delete_webhook(
    token: str, drop_pending_updates: bool = False
) -> dict[str, Any]:
    params = {"drop_pending_updates": True if drop_pending_updates else None}
    return await call(token, "deleteWebhook", params)


async def get_webhook_info(token: str) -> dict[str, Any]:
    return await call(token, "getWebhookInfo")
