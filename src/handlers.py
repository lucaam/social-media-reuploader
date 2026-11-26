"""aiogram handlers of the bot: links in messages, /start, /help, group welcome.

The same :class:`aiogram.Router` serves polling and webhook mode (see
:func:`src.bot.create_dispatcher`). Handlers receive the worker pool from the
dispatcher's workflow data (``pool``) and never touch the database or the
network themselves, except for short Telegram replies.

Link pipeline for every message, edited message, channel post and edited
channel post::

    urls = providers.extract_urls(text or caption, entities or caption_entities)
    skip Telegram links (t.me, ...)
    check = await providers.check_link_async(url)
    supported   -> await pool.submit(chat_id, check.match.url, ..., provider=check.match)
    unsupported -> ONE Italian reply per message + a reaction, only where
                   config.UNSUPPORTED_LINK_REPLY allows it

Privacy: the text of a message is never logged at INFO level (only chat id,
message id and the number of links) and messages without links are ignored
without any database access.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from aiogram import Bot, F, Router
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command
from aiogram.types import ChatMemberUpdated, ErrorEvent, Message

from . import config, providers, telegram_api
from .providers import LinkCheck, ProviderMatch, Rejection

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_UPDATES",
    "REJECT_REACTION",
    "SubmitPool",
    "create_router",
    "handle_links",
    "help_text",
    "should_reply_unsupported",
    "unsupported_reply_text",
    "welcome_text",
]

# Update types the bot asks Telegram for (getUpdates and setWebhook).
ALLOWED_UPDATES: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
)
# Reaction added to a message whose links cannot be downloaded.
REJECT_REACTION = "\U0001f44e"  # 👎
PRIVATE = "private"
GROUP_TYPES = frozenset({"group", "supergroup"})
# Links considered per message (the rest is ignored); bounds the work a
# single message can cause.
DEFAULT_MAX_LINKS_PER_MESSAGE = 10
_MIB = 1024 * 1024


class SubmitPool(Protocol):
    """What the handlers need from :class:`src.worker.WorkerPool`."""

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
    ) -> Any: ...


# ---------------------------------------------------------------------------
# User facing texts (Italian)
# ---------------------------------------------------------------------------
# Most specific reason first: a reply lists one sentence per distinct reason.
_REASON_ORDER = (
    Rejection.COLLECTION,
    Rejection.BLOCKED,
    Rejection.BROKEN,
    Rejection.UNRESOLVED,
    Rejection.INVALID,
    Rejection.UNSUPPORTED,
)
_REASON_TEXT = {
    Rejection.COLLECTION: (
        "Questo è il link di un profilo, di un canale o di una playlist: "
        "mandami il link di un singolo video o post."
    ),
    Rejection.BLOCKED: (
        "I link di questo sito sono stati disattivati dall'amministratore del bot."
    ),
    Rejection.BROKEN: (
        "Al momento non riesco a scaricare da questo sito: il supporto è "
        "temporaneamente fuori servizio. Riprova più avanti."
    ),
    Rejection.UNRESOLVED: (
        "Non riesco ad aprire questo link di condivisione in questo momento: "
        "riprova tra qualche minuto oppure mandami il link diretto del video o "
        "del post."
    ),
    Rejection.INVALID: "Link non valido: controlla di averlo copiato per intero.",
    Rejection.UNSUPPORTED: "Link non supportato. Supporto {sites}.",
}


def unsupported_reply_text(reasons: Iterable[Rejection], sites: str) -> str:
    """The single reply for the refused links of one message.

    One sentence per distinct reason (most specific first); ``sites`` is
    :func:`src.providers.supported_sites_text`. Telegram links are not a reason.
    """
    distinct = set(reasons)
    lines = [
        _REASON_TEXT[reason].format(sites=sites)
        for reason in _REASON_ORDER
        if reason in distinct
    ]
    return "\n".join(lines) or _REASON_TEXT[Rejection.UNSUPPORTED].format(sites=sites)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def duration_text(seconds: float) -> str:
    """``5400`` -> ``"90 minuti"``, ``3600`` -> ``"1 ora"`` (Italian)."""
    seconds = max(0, round(seconds))
    if seconds and seconds % 86400 == 0:
        return _plural(seconds // 86400, "giorno", "giorni")
    if seconds >= 3600 and seconds % 3600 == 0:
        return _plural(seconds // 3600, "ora", "ore")
    if seconds >= 60 and seconds % 60 == 0:
        return _plural(seconds // 60, "minuto", "minuti")
    return _plural(seconds, "secondo", "secondi")


def _window_text(seconds: int) -> str:
    """``60`` -> ``"al minuto"``, ``10`` -> ``"ogni 10 secondi"``."""
    units = (
        (86400, "al giorno", "giorni"),
        (3600, "all'ora", "ore"),
        (60, "al minuto", "minuti"),
        (1, "al secondo", "secondi"),
    )
    for unit, single, plural in units:
        if seconds % unit == 0:
            count = seconds // unit
            return single if count == 1 else f"ogni {count} {plural}"
    return f"ogni {seconds} secondi"  # pragma: no cover - unit 1 always matches


def _join_italian(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " e " + items[-1]


def rate_limits_text(limits: Iterable[Sequence[int]]) -> str | None:
    """``((1, 10), (3, 60))`` -> ``"1 link ogni 10 secondi e 3 al minuto"``."""
    parts: list[str] = []
    for count, seconds in limits:
        window = _window_text(int(seconds))
        parts.append(f"{count} link {window}" if not parts else f"{count} {window}")
    return _join_italian(parts) or None


def help_text(sites: str) -> str:
    """Answer to /start and /help in private chats."""
    max_mb = max(1, getattr(config, "TELEGRAM_MAX_FILE_SIZE", 50 * _MIB) // _MIB)
    max_duration = int(getattr(config, "MAX_DURATION_SECONDS", 0) or 0)
    pending = int(getattr(config, "MAX_PENDING_PER_CHAT", 3) or 0)
    duplicate_window = int(getattr(config, "DUPLICATE_WINDOW_SECONDS", 0) or 0)
    limits = rate_limits_text(getattr(config, "RATE_LIMITS", ()) or ())

    cannot = ["dirette"]
    if max_duration > 0:
        cannot.append(f"video più lunghi di {duration_text(max_duration)}")
    cannot.append("contenuti privati o che richiedono l'accesso")
    lines = [
        (
            "Ciao! 👋 Mandami il link di un video o di un post e te lo rimando "
            "qui come file, così puoi guardarlo direttamente su Telegram."
        ),
        "",
        f"Siti supportati: {sites}.",
        "",
        "Come funziona",
        (
            "• Puoi mandare più link nello stesso messaggio, anche nella "
            "didascalia di una foto o di un video."
        ),
        f"• I file più grandi di {max_mb} MB vengono compressi prima dell'invio.",
        f"• Non posso scaricare {_join_italian(cannot)}.",
        "",
        "Limiti",
    ]
    if pending > 0:
        lines.append(
            f"• Al massimo {pending} link in lavorazione alla volta per ogni chat."
        )
    if limits:
        lines.append(f"• Al massimo {limits} per ogni chat.")
        lines.append(
            "• Se superi un limite di poco il link resta in coda e lo scarico appena "
            "possibile, altrimenti ti dico quando riprovare."
        )
    if duplicate_window > 0:
        lines.append(
            "• Lo stesso link mandato di nuovo nella stessa chat entro "
            f"{duration_text(duplicate_window)} non viene ripubblicato."
        )
    if lines[-1] == "Limiti":
        lines.append("• Nessun limite al numero di link.")
    lines += [
        "",
        "Gruppi",
        (
            "Aggiungimi a un gruppo: nei gruppi vedo i link solo se sono "
            "amministratore oppure se la modalità privacy del bot è disattivata."
        ),
        "",
        "Privacy",
        (
            "Per elaborare un link salvo l'ID della chat, il link e il testo del "
            "messaggio che lo accompagna (lo uso come didascalia). I messaggi "
            "senza link vengono ignorati."
        ),
    ]
    if not getattr(config, "KEEP_DOWNLOADED_FILES", False):
        lines[-1] += " I file scaricati vengono cancellati dopo l'invio."
    return "\n".join(lines)


def welcome_text(sites: str, *, needs_admin_hint: bool) -> str:
    """Message posted when the bot is added to a group or supergroup."""
    lines = [
        "Ciao! 👋 Grazie per avermi aggiunto.",
        (
            f"Quando qualcuno manda il link di un video ({sites}) lo scarico e "
            "lo pubblico qui. Agli altri messaggi non rispondo."
        ),
    ]
    if needs_admin_hint:
        lines.append(
            "Per vedere i link devo essere amministratore del gruppo (oppure la "
            "modalità privacy del bot deve essere disattivata)."
        )
    lines.append(
        "Non posso scaricare contenuti privati o che richiedono l'accesso. "
        "Scrivimi /help in privato per sapere come funziono."
    )
    return "\n".join(lines)


async def _sites_text() -> str:
    # Walks yt-dlp's extractor list (loads it on first use): keep it off the loop.
    return await asyncio.to_thread(providers.supported_sites_text)


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------
def should_reply_unsupported(chat_type: str | None) -> bool:
    """Whether refused links are answered in a chat of this type.

    ``config.UNSUPPORTED_LINK_REPLY``: ``"private"`` (default, private chats
    only), ``"always"`` or ``"never"``.
    """
    policy = str(getattr(config, "UNSUPPORTED_LINK_REPLY", "private")).lower()
    if policy == "always":
        return True
    if policy == "never":
        return False
    return chat_type == PRIVATE


def _max_links_per_message() -> int:
    value = getattr(config, "MAX_LINKS_PER_MESSAGE", DEFAULT_MAX_LINKS_PER_MESSAGE)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_LINKS_PER_MESSAGE


def _edit_too_old(message: Message, now: float | None = None) -> bool:
    """Edits are honoured only within ``config.DUPLICATE_WINDOW_SECONDS``.

    Older messages may have lost their request rows (history cleared or
    pruned), so an edit could otherwise post their links again. A window of
    0 (duplicate detection disabled) sets no limit, like the worker.
    """
    window = float(getattr(config, "DUPLICATE_WINDOW_SECONDS", 0) or 0)
    sent_at = message.date.timestamp() if message.date else None
    if sent_at is None or window <= 0:
        return False
    current = time.time() if now is None else now
    return current - sent_at > window


def _text_and_entities(message: Message) -> tuple[str | None, list[Any] | None]:
    if message.text is not None:
        return message.text, message.entities
    return message.caption, message.caption_entities


def _status_name(result: Any) -> str:
    status = getattr(result, "status", result)
    return str(getattr(status, "value", status))


# ---------------------------------------------------------------------------
# Link handling
# ---------------------------------------------------------------------------
async def handle_links(
    message: Message, bot: Bot, pool: SubmitPool, *, edited: bool = False
) -> None:
    """Submit the supported links of ``message`` and answer refused ones."""
    chat = message.chat
    if edited and _edit_too_old(message):
        logger.debug(
            "Ignoring an edit of an old message chat=%s message=%s",
            chat.id,
            message.message_id,
        )
        return
    text, entities = _text_and_entities(message)
    if not text:
        return
    urls = [
        url
        for url in providers.extract_urls(text, entities)
        if not providers.is_telegram_link(url)
    ]
    if not urls:
        return
    limit = _max_links_per_message()
    if len(urls) > limit:
        logger.info(
            "chat=%s message=%s has %d links; only the first %d are considered",
            chat.id,
            message.message_id,
            len(urls),
            limit,
        )
        urls = urls[:limit]

    checks = await _check_links(message, urls)
    description = providers.text_without_urls(text, entities) or None
    submitted: Counter[str] = Counter()  # submit status -> count
    refused: list[Rejection] = []
    for check in checks:
        if check.match is not None:
            submitted[await _submit(pool, message, check.match, description)] += 1
            continue
        reason = check.reason or Rejection.UNSUPPORTED
        logger.debug("Refused link (%s): %s", reason, telegram_api.redact(check.url))
        if reason is not Rejection.TELEGRAM:
            refused.append(reason)

    logger.info(
        "chat=%s message=%s%s: %d link(s), submitted %s, refused %d",
        chat.id,
        message.message_id,
        " (edited)" if edited else "",
        len(urls),
        dict(sorted(submitted.items())),
        len(refused),
    )
    if refused and not edited and should_reply_unsupported(chat.type):
        await _reply_refused(bot, message, refused, react=not submitted)


async def _check_links(message: Message, urls: Sequence[str]) -> list[LinkCheck]:
    """Check every link concurrently; a failing check refuses only its link."""
    results = await asyncio.gather(
        *(providers.check_link_async(url) for url in urls), return_exceptions=True
    )
    checks: list[LinkCheck] = []
    for url, result in zip(urls, results, strict=True):
        if not isinstance(result, BaseException):
            checks.append(result)
            continue
        if not isinstance(result, Exception):
            raise result  # CancelledError and friends
        logger.error(
            "Could not check a link of chat=%s message=%s: %s",
            message.chat.id,
            message.message_id,
            type(result).__name__,
            exc_info=result,
        )
        checks.append(LinkCheck(url=url, match=None, reason=Rejection.UNSUPPORTED))
    return checks


async def _submit(
    pool: SubmitPool,
    message: Message,
    match: ProviderMatch,
    description: str | None,
) -> str:
    """Hand one link to the worker; returns the submit status name."""
    chat = message.chat
    try:
        result = await pool.submit(
            chat.id,
            match.url,
            chat_type=chat.type,
            user_id=message.from_user.id if message.from_user else None,
            original_message_id=message.message_id,
            description=description,
            provider=match,
        )
    except Exception:
        logger.exception(
            "Could not submit a %s link of chat=%s message=%s",
            match.key,
            chat.id,
            message.message_id,
        )
        return "error"
    status = _status_name(result)
    logger.debug(
        "Submitted %s link of chat=%s message=%s -> %s",
        match.key,
        chat.id,
        message.message_id,
        status,
    )
    return status


async def _reply_refused(
    bot: Bot, message: Message, reasons: Sequence[Rejection], *, react: bool
) -> None:
    """One reply for the whole message (+ a reaction when nothing was accepted).

    The reaction is skipped when another link of the message was accepted:
    a bot has a single reaction per message and the worker uses it to show
    progress.
    """
    chat_id, message_id = message.chat.id, message.message_id
    sites = await _sites_text() if Rejection.UNSUPPORTED in reasons else ""
    result = await telegram_api.send_message(
        bot.token,
        chat_id,
        unsupported_reply_text(reasons, sites),
        reply_to_message_id=message_id,
    )
    if not result.get("ok"):
        logger.warning(
            "Could not answer refused links in chat=%s: %s",
            chat_id,
            telegram_api.redact(result.get("description"), bot.token),
        )
    if not react:
        return
    result = await telegram_api.set_message_reaction(
        bot.token, chat_id, message_id, REJECT_REACTION
    )
    if not result.get("ok"):
        logger.info(
            "Could not react to message %s in chat=%s: %s",
            message_id,
            chat_id,
            telegram_api.redact(result.get("description"), bot.token),
        )


async def _send_text(bot: Bot, chat_id: int, text: str) -> None:
    result = await telegram_api.send_message(bot.token, chat_id, text)
    if not result.get("ok"):
        logger.warning(
            "Could not send a message to chat=%s: %s",
            chat_id,
            telegram_api.redact(result.get("description"), bot.token),
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def on_message(message: Message, bot: Bot, pool: SubmitPool) -> None:
    await handle_links(message, bot, pool)


async def on_edited_message(message: Message, bot: Bot, pool: SubmitPool) -> None:
    await handle_links(message, bot, pool, edited=True)


async def on_start_or_help(message: Message, bot: Bot) -> None:
    logger.info("chat=%s: /start or /help", message.chat.id)
    await _send_text(bot, message.chat.id, help_text(await _sites_text()))


async def on_added_to_group(
    event: ChatMemberUpdated, bot: Bot, bot_info: dict[str, Any] | None = None
) -> None:
    """Greet a group once, when the bot becomes a member of it."""
    is_admin = event.new_chat_member.status in ("administrator", "creator")
    reads_all = bool((bot_info or {}).get("can_read_all_group_messages"))
    logger.info("Added to %s chat=%s", event.chat.type, event.chat.id)
    text = welcome_text(
        await _sites_text(), needs_admin_hint=not (is_admin or reads_all)
    )
    await _send_text(bot, event.chat.id, text)


async def on_error(event: ErrorEvent) -> bool:
    """Log handler failures (aiogram would otherwise re-raise them)."""
    logger.error(
        "Error while handling update %s: %s",
        event.update.update_id,
        type(event.exception).__name__,
        exc_info=event.exception,
    )
    return True


def create_router() -> Router:
    """A new router with every handler (a router belongs to one dispatcher)."""
    router = Router(name="social-media-reuploader")
    router.message.register(
        on_start_or_help, Command("start", "help"), F.chat.type == PRIVATE
    )
    router.message.register(on_message)
    router.edited_message.register(on_edited_message)
    router.channel_post.register(on_message)
    router.edited_channel_post.register(on_edited_message)
    router.my_chat_member.register(
        on_added_to_group,
        ChatMemberUpdatedFilter(JOIN_TRANSITION),
        F.chat.type.in_(GROUP_TYPES),
    )
    router.errors.register(on_error)
    return router
