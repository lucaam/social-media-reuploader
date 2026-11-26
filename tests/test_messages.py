"""Tests for src/messages.py: Italian texts, plurals, durations, captions."""

import pytest

from src import messages


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "1 secondo"),
        (0.4, "1 secondo"),
        (1, "1 secondo"),
        (9.998, "10 secondi"),
        (59, "59 secondi"),
        (60, "1 minuto"),
        (61, "2 minuti"),
        (119, "2 minuti"),
        (120, "2 minuti"),
        (3599, "1 ora"),
        (3600, "1 ora"),
        (3601, "1 ora e 1 minuto"),
        (5400, "1 ora e 30 minuti"),
        (7200, "2 ore"),
        (86399, "1 giorno"),
        (86400, "1 giorno"),
        (90000, "1 giorno e 1 ora"),
        (3 * 86400, "3 giorni"),
        (30 * 86400, "30 giorni"),
        (float("inf"), "1 secondo"),
    ],
)
def test_duration_is_pluralised_italian(seconds, text):
    assert messages.duration(seconds) == text


def test_duration_never_says_minutoi():
    for seconds in range(1, 6000, 7):
        assert "minutoi" not in messages.duration(seconds)
        assert "orae" not in messages.duration(seconds)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (1, "secondo"),
        (10, "10 secondi"),
        (90, "90 secondi"),
        (60, "minuto"),
        (120, "2 minuti"),
        (5400, "90 minuti"),
        (3600, "ora"),
        (86400, "giorno"),
        (30 * 86400, "30 giorni"),
    ],
)
def test_period_is_exact(seconds, text):
    assert messages.period(seconds) == text


def test_plural_and_sizes():
    assert messages.plural(1, "link", "link") == "1 link"
    assert messages.plural(2, "minuto", "minuti") == "2 minuti"
    assert messages.size_mb(84 * 1024 * 1024) == "84.0 MB"
    assert messages.size_mb(None) == "? MB"


def test_rate_limited_mentions_limit_and_wait():
    text = messages.rate_limited(120, (3, 60))
    assert text == (
        "Hai inviato troppi link: puoi inviarne al massimo 3 ogni minuto. "
        "Riprova tra 2 minuti."
    )
    assert messages.rate_limited(3600, (5, 3600)).endswith("Riprova tra 1 ora.")
    assert "ogni 10 secondi" in messages.rate_limited(5, (1, 10))
    assert messages.rate_limited(3 * 86400, None) == (
        "Hai inviato troppi link. Riprova tra 3 giorni."
    )


def test_pending_cap_deferred_and_duplicate_notices():
    assert messages.pending_cap(3).startswith("Hai già 3 link in coda")
    assert messages.pending_cap(1).startswith("Hai già 1 link in coda")
    assert messages.deferred(10) == "Link in coda: lo elaboro tra 10 secondi."
    assert messages.deferred(90) == "Link in coda: lo elaboro tra 2 minuti."
    assert messages.duplicate() == "Questo link è già stato condiviso di recente."
    assert "già in coda" in messages.duplicate(in_progress=True)


def test_compression_note_and_caption():
    note = messages.compression_note(int(80.1 * 1024 * 1024), int(48.3 * 1024 * 1024))
    assert note == "Video compresso: 80.1 MB → 48.3 MB"
    assert messages.caption(None) is None
    assert messages.caption("   ") is None
    assert messages.caption(" guarda qui ") == "guarda qui"
    assert messages.caption(None, note) == note
    assert messages.caption("ciao", note) == f"ciao\n\n{note}"


def test_caption_keeps_the_note_when_the_description_is_long():
    note = messages.compression_note(100, 50)
    long_text = "😀" * 2000  # 4000 UTF-16 code units
    text = messages.caption(long_text, note)
    assert text.endswith(note)
    assert len(text.encode("utf-16-le")) // 2 <= messages.CAPTION_LIMIT
    plain = messages.caption("a" * 5000)
    assert len(plain) == messages.CAPTION_LIMIT
    assert plain.endswith("…")


CODES = [
    "auth_required",
    "unavailable",
    "geo_blocked",
    "live",
    "too_long",
    "too_large",
    "no_media",
    "unsupported",
    "timeout",
    "conversion_failed",
    "failed",
    "send_failed",
]


@pytest.mark.parametrize("code", CODES)
def test_every_error_code_has_its_own_italian_text(code):
    text = messages.failure(
        code, url="https://example.com/v/1", max_duration_seconds=3600
    )
    assert text
    if code != "failed":
        assert text != messages.failure("failed")


def test_failure_texts_carry_details():
    assert "https://x.com/i/status/1" in messages.failure(
        "too_large", url="https://x.com/i/status/1"
    )
    assert "1 ora" in messages.failure("too_long", max_duration_seconds=3600)
    assert "troppo lungo" in messages.failure("too_long", max_duration_seconds=0)
    assert "TikTok" in messages.failure("no_media", slideshow=True)
    assert "immagini" in messages.failure("no_media")
    assert messages.failure("unknown_code") == messages.failure("failed")
    assert messages.failure(None) == messages.failure("failed")
