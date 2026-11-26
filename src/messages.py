"""Italian texts shown to Telegram users by the worker (the only place they live).

Code and logs are English; everything a user reads is built here, so wording,
plural forms and units can be reviewed (and tested) in one module. Functions
are pure: callers pass the numbers, this module only formats them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

MIB = 1024 * 1024
# Telegram's caption limit, in UTF-16 code units.
CAPTION_LIMIT = 1024

PROCESSING = "In lavorazione 👀"

# (singular, plural, seconds), largest first.
_WINDOW_UNITS = (
    ("giorno", "giorni", 86400),
    ("ora", "ore", 3600),
    ("minuto", "minuti", 60),
    ("secondo", "secondi", 1),
)


# ---------------------------------------------------------------------------
# Numbers, sizes and durations
# ---------------------------------------------------------------------------
def plural(count: int, singular: str, plural_form: str) -> str:
    """``"1 minuto"`` / ``"2 minuti"``."""
    return f"{count} {singular if count == 1 else plural_form}"


def size_mb(size: float | None) -> str:
    """``"80.1 MB"`` (MB = 1024 * 1024 bytes, like Telegram's 50 MB limit)."""
    if size is None:
        return "? MB"
    return f"{size / MIB:.1f} MB"


def _seconds(seconds: float) -> int:
    if not math.isfinite(seconds):
        return 1
    return max(1, round(seconds))


def duration(seconds: float) -> str:
    """A wait or a limit in natural Italian, rounded up to the shown unit.

    ``1 secondo``, ``45 secondi``, ``1 minuto``, ``2 minuti``, ``1 ora``,
    ``1 ora e 30 minuti``, ``3 giorni``, ``1 giorno e 5 ore``. Values below
    one second count as one second.
    """
    total = _seconds(seconds)
    if total < 60:
        return plural(total, "secondo", "secondi")
    if total < 3600:
        minutes = math.ceil(total / 60)
        if minutes < 60:
            return plural(minutes, "minuto", "minuti")
        return plural(1, "ora", "ore")
    if total < 86400:
        hours, rest = divmod(total, 3600)
        minutes = math.ceil(rest / 60)
        if minutes == 60:
            hours, minutes = hours + 1, 0
        if hours == 24:
            return plural(1, "giorno", "giorni")
        text = plural(hours, "ora", "ore")
        return f"{text} e {plural(minutes, 'minuto', 'minuti')}" if minutes else text
    days, rest = divmod(total, 86400)
    hours = math.ceil(rest / 3600)
    if hours == 24:
        days, hours = days + 1, 0
    text = plural(days, "giorno", "giorni")
    return f"{text} e {plural(hours, 'ora', 'ore')}" if hours else text


def period(seconds: float) -> str:
    """A rate-limit window after "ogni", exact (never rounded).

    ``minuto``, ``ora``, ``10 secondi``, ``90 secondi``, ``30 giorni``.
    """
    total = _seconds(seconds)
    for singular, plural_form, unit in _WINDOW_UNITS:
        if total % unit == 0:
            count = total // unit
            return singular if count == 1 else plural(count, singular, plural_form)
    return plural(total, "secondo", "secondi")  # unreachable: unit 1 divides all


# ---------------------------------------------------------------------------
# Notices sent when a link is refused or delayed (private chats only)
# ---------------------------------------------------------------------------
def rate_limited(delay_seconds: float, window: tuple[int, int] | None = None) -> str:
    """The chat sent too many links; ``window`` is the (count, seconds) limit hit."""
    wait = duration(delay_seconds)
    if window is None:
        return f"Hai inviato troppi link. Riprova tra {wait}."
    count, seconds = window
    return (
        f"Hai inviato troppi link: puoi inviarne al massimo {count} "
        f"ogni {period(seconds)}. Riprova tra {wait}."
    )


def pending_cap(active: int) -> str:
    """Too many links of the chat are still waiting or being processed."""
    return (
        f"Hai già {plural(active, 'link', 'link')} in coda: aspetta che li elabori "
        "prima di inviarne altri."
    )


def duplicate(*, in_progress: bool = False) -> str:
    """The same link was shared in this chat within the duplicate window."""
    if in_progress:
        return "Questo link è già in coda: lo invio appena è pronto."
    return "Questo link è già stato condiviso di recente."


def deferred(delay_seconds: float) -> str:
    """An accepted link that is processed later because of the rate limits."""
    return f"Link in coda: lo elaboro tra {duration(delay_seconds)}."


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------
def compression_note(original_size: int | None, final_size: int | None) -> str:
    """``"Video compresso: 80.1 MB → 48.3 MB"``."""
    return f"Video compresso: {size_mb(original_size)} → {size_mb(final_size)}"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _cut(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` UTF-16 units, with an ellipsis when cut."""
    if _utf16_len(text) <= limit:
        return text
    out: list[str] = []
    units = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit - 1:
            break
        out.append(char)
        units += size
    return "".join(out).rstrip() + "…"


def caption(description: str | None, note: str | None = None) -> str | None:
    """Caption of the posted media: the user's text, then the optional note.

    The description is shortened (never the note) so the whole caption fits
    Telegram's limit. ``None`` when there is nothing to show.
    """
    text = (description or "").strip()
    if not note:
        return _cut(text, CAPTION_LIMIT) if text else None
    if not text:
        return note
    room = CAPTION_LIMIT - _utf16_len(note) - 2
    if room <= 1:
        return _cut(note, CAPTION_LIMIT)
    return f"{_cut(text, room)}\n\n{note}"


# ---------------------------------------------------------------------------
# Failures (one text per downloader.DownloadError code)
# ---------------------------------------------------------------------------
_FAILURES: Mapping[str, str] = {
    "auth_required": (
        "Questo contenuto è privato o richiede l'accesso a un account: "
        "non posso scaricarlo."
    ),
    "unavailable": (
        "Questo contenuto non è disponibile: potrebbe essere stato rimosso "
        "o non esistere più."
    ),
    "geo_blocked": (
        "Questo contenuto non è disponibile nel paese da cui opera il bot."
    ),
    "live": (
        "Le dirette non sono supportate: riprova quando la diretta sarà terminata."
    ),
    "no_media": (
        "Non ho trovato un video da scaricare in questo link "
        "(ad esempio un post con sole immagini)."
    ),
    "unsupported": "Questo link non è supportato.",
    "timeout": "Il download ha impiegato troppo tempo. Riprova più tardi.",
    "conversion_failed": (
        "Non sono riuscito a convertire il video in un formato adatto a Telegram."
    ),
    "send_failed": "Non sono riuscito a inviare il video su Telegram. Riprova più tardi.",
    "failed": "Non sono riuscito a scaricare questo contenuto. Riprova più tardi.",
}
_SLIDESHOW = (
    "Questo post TikTok è una presentazione di foto: posso inviare solo video, "
    "non le immagini con la musica di sottofondo."
)


def too_long(max_duration_seconds: float | None) -> str:
    if not max_duration_seconds or max_duration_seconds <= 0:
        return "Il video è troppo lungo per essere scaricato."
    return (
        "Il video è troppo lungo: posso scaricare contenuti di al massimo "
        f"{duration(max_duration_seconds)}."
    )


def too_large(url: str | None) -> str:
    text = "Il video è troppo grande per Telegram, anche compresso."
    return f"{text} Puoi aprirlo qui: {url}" if url else text


def failure(
    code: str | None,
    *,
    url: str | None = None,
    max_duration_seconds: float | None = None,
    slideshow: bool = False,
) -> str:
    """The message explaining why a link could not be delivered.

    ``code`` is a ``downloader.DownloadError.code`` (or a worker code such as
    ``send_failed``); unknown codes get the generic ``failed`` text.
    """
    if code == "too_large":
        return too_large(url)
    if code == "too_long":
        return too_long(max_duration_seconds)
    if code == "no_media" and slideshow:
        return _SLIDESHOW
    return _FAILURES.get(code or "failed", _FAILURES["failed"])
