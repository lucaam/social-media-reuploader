"""Backwards-compatible facade over :mod:`src.providers`.

New code should use :func:`src.providers.extract_urls` and
:func:`src.providers.match_provider` directly.
"""

from __future__ import annotations

from .providers import extract_urls, match_provider

__all__ = ["find_links", "is_supported"]


def find_links(text: str | None) -> list[str]:
    """Links in ``text`` that the bot can download (text order, deduplicated).

    The URLs are returned cleaned (no trailing punctuation, ``https://`` added)
    but otherwise as written, so they can be compared with Telegram entities.
    """
    return [url for url in extract_urls(text) if match_provider(url) is not None]


def is_supported(url: str | None) -> bool:
    """True when a working yt-dlp extractor (not "generic") claims ``url``."""
    return bool(url) and match_provider(url) is not None
