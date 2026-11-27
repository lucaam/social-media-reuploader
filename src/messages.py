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
    """``"80,1 MB"``: Italian decimal comma; MB = 1024 * 1024 bytes, like
    Telegram's 50 MB limit."""
    if size is None:
        return "? MB"
    return f"{size / MIB:.1f} MB".replace(".", ",")


def _seconds(seconds: float) -> int:
    """Whole seconds, rounded up: a wait shown too short makes users retry early."""
    if not math.isfinite(seconds):
        return 1
    return max(1, math.ceil(seconds))


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
    if active == 1:
        return (
            "Hai già 1 link in coda: aspetta che lo elabori prima di inviarne "
            "un altro."
        )
    return (
        f"Hai già {active} link in coda: aspetta che li elabori "
        "prima di inviarne altri."
    )


def duplicate(*, in_progress: bool = False) -> str:
    """The same link was shared in this chat within the duplicate window."""
    if in_progress:
        return "Questo link è già in coda: lo invio appena è pronto."
    return "Questo link è già stato condiviso di recente."


INTERRUPTED_WHILE_SENDING = (
    "Il bot si è riavviato mentre inviava questo contenuto: se non lo vedi qui, "
    "invia di nuovo il link."
)


def deferred(delay_seconds: float) -> str:
    """An accepted link that is processed later because of the rate limits."""
    return f"Link in coda: lo elaboro tra {duration(delay_seconds)}."


# ---------------------------------------------------------------------------
# The media a text talks about: ``kind`` is "video", "audio", or None when it
# is unknown (e.g. the link could not even be read) or mixed.
# ---------------------------------------------------------------------------
# (subject, object, "of ...") forms. All masculine singular, so the adjectives
# and pronouns of the texts ("troppo lungo", "aprirlo") agree with each.
_NOUNS: Mapping[str, tuple[str, str, str]] = {
    "video": ("Il video", "il video", "del video"),
    "audio": ("L'audio", "l'audio", "dell'audio"),
}
_UNKNOWN_NOUNS = ("Questo contenuto", "il contenuto", "del contenuto")
_NOTE_LABELS: Mapping[str, str] = {"video": "Video", "audio": "Audio"}


def _nouns(kind: str | None) -> tuple[str, str, str]:
    return _NOUNS.get(kind or "", _UNKNOWN_NOUNS)


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------
def compression_note(
    original_size: int | None,
    final_size: int | None,
    *,
    kind: str | None = "video",
    count: int = 1,
) -> str:
    """``"Video compresso: 80,1 MB → 48,3 MB"``.

    ``kind`` is the kind of the ``count`` compressed files ("Audio
    compresso", "File compressi" when they are mixed).
    """
    label = _NOTE_LABELS.get(kind or "", "File")
    label += " compresso" if count == 1 else " compressi"
    return f"{label}: {size_mb(original_size)} → {size_mb(final_size)}"


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
        "Non ho trovato video o audio da scaricare in questo link "
        "(ad esempio un post con sole immagini)."
    ),
    "unsupported": "Questo link non è supportato.",
    "blocked": "Questo sito è stato disattivato dall'amministratore del bot.",
    "timeout": "Il download ha impiegato troppo tempo. Riprova più tardi.",
    "failed": "Non sono riuscito a scaricare questo contenuto. Riprova più tardi.",
}
# Texts naming the media: {noun} and {of_noun} come from _nouns(kind).
_MEDIA_FAILURES: Mapping[str, str] = {
    "conversion_failed": (
        "Non sono riuscito a convertire {noun} in un formato adatto a Telegram."
    ),
    "conversion_timeout": (
        "La conversione {of_noun} per Telegram ha impiegato troppo tempo."
    ),
    "send_failed": "Non sono riuscito a inviare {noun} su Telegram. Riprova più tardi.",
}
# too_large by ``step``: refused before downloading (the download size limit),
# while converting (it cannot be compressed enough) or by Telegram.
_TOO_LARGE: Mapping[str, str] = {
    "download": "{subject} è troppo grande per essere scaricato.",
    "convert": (
        "{subject} è troppo grande per Telegram e non riesco a comprimerlo "
        "abbastanza."
    ),
    "send": "{subject} è troppo grande per Telegram.",
}
_SLIDESHOW = (
    "Questo post TikTok è una presentazione di foto: posso inviare solo video, "
    "non le immagini con la musica di sottofondo."
)


def too_long(max_duration_seconds: float | None, *, kind: str | None = None) -> str:
    subject = _nouns(kind)[0]
    if not max_duration_seconds or max_duration_seconds <= 0:
        return f"{subject} è troppo lungo per essere scaricato."
    return (
        f"{subject} è troppo lungo: posso scaricare contenuti di al massimo "
        f"{duration(max_duration_seconds)}."
    )


def too_large(
    url: str | None, *, kind: str | None = None, step: str | None = None
) -> str:
    template = _TOO_LARGE.get(step or "", _TOO_LARGE["send"])
    text = template.format(subject=_nouns(kind)[0])
    return f"{text} Puoi aprirlo qui: {url}" if url else text


def failure(
    code: str | None,
    *,
    url: str | None = None,
    max_duration_seconds: float | None = None,
    slideshow: bool = False,
    kind: str | None = None,
    step: str | None = None,
) -> str:
    """The message explaining why a link could not be delivered.

    ``code`` is a ``downloader.DownloadError.code`` (or a worker code such as
    ``send_failed``); unknown codes get the generic ``failed`` text. ``kind``
    names the media (see :func:`_nouns`); ``step`` says where a ``too_large``
    refusal happened: "download", "convert" or "send" (Telegram refused it).
    """
    if code == "too_large":
        return too_large(url, kind=kind, step=step)
    if code == "too_long":
        return too_long(max_duration_seconds, kind=kind)
    if code == "no_media" and slideshow:
        return _SLIDESHOW
    template = _MEDIA_FAILURES.get(code or "")
    if template is not None:
        _subject, noun, of_noun = _nouns(kind)
        return template.format(noun=noun, of_noun=of_noun)
    return _FAILURES.get(code or "failed", _FAILURES["failed"])
