"""Link extraction and provider detection.

The bot accepts every link that one of yt-dlp's *site specific* extractors
claims. yt-dlp's "generic" extractor (which would accept any web page) is never
used, so random links (news articles, shops, ...) are not downloaded.

Pipeline used by the bot::

    urls = extract_urls(message.text, message.entities)   # cleaned, deduped, ordered
    for url in urls:
        if is_telegram_link(url):
            continue
        check = await check_link_async(url)              # match + resolve share links
        if check.match is None:
            ...  # check.reason tells why (unsupported, collection, blocked, ...)

Design notes:

* The extractor list is built once per process (``gen_extractor_classes()``,
  lazy extractors), cached and protected by a lock. :func:`warm_up` also
  pre-compiles every extractor regex; call it from a worker thread at startup
  (``await asyncio.to_thread(providers.warm_up)``), it takes about one second.
* Matching mirrors yt-dlp's own selection (``--ies default,-generic``): the
  *first* enabled extractor whose ``suitable(url)`` is true wins. If that
  extractor is marked as broken (``working() is False``) the link is refused,
  because yt-dlp would use it anyway.
* Profile/channel/playlist/album pages ("collections") are refused unless
  ``config.ALLOW_COLLECTION_LINKS`` is true: posting a channel link must not
  make the bot re-upload the channel.
* A few share/short links that no extractor claims (``fb.watch``,
  ``facebook.com/share/...``, ``v.redd.it``, ``pin.it``, ``t.co``, ...) can be
  resolved with :func:`resolve_short_link`: only for a fixed allowlist of
  hosts of supported providers, HTTPS only, short timeout, at most 3
  redirects, never visiting hosts outside the provider's own domains. The
  final URL is checked like any other link.
* Open redirectors (``href.li``, ``consent.youtube.com``, Facebook's link
  warnings, ``t.co``) are never matched themselves: yt-dlp would hand their
  target to another extractor without the checks above. Their target is
  unwrapped offline (or resolved, for ``t.co``) and checked instead.
* Everything except the extractor lookup is pure string processing and safe to
  call from any thread or from the event loop.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, NamedTuple
from urllib.parse import quote, unquote_plus, urlencode, urljoin, urlsplit

import aiohttp

from . import config, http_client

logger = logging.getLogger(__name__)

__all__ = [
    "LinkCheck",
    "ProviderMatch",
    "Rejection",
    "check_link",
    "check_link_async",
    "clean_url",
    "config_problems",
    "extract_urls",
    "is_short_link",
    "is_telegram_link",
    "match_provider",
    "match_provider_async",
    "normalize_url",
    "provider_display_name",
    "resolve_short_link",
    "stats",
    "supported_sites_text",
    "text_without_urls",
    "warm_up",
]


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderMatch:
    """A link that the bot can hand to yt-dlp."""

    key: str  # yt-dlp extractor key, e.g. "Youtube", "Twitter", "Reddit", "TikTok"
    name: str  # human name, e.g. "YouTube", "X (Twitter)", "Reddit"
    url: str  # cleaned URL to give to yt-dlp
    normalized_url: str  # canonical dedupe key (see normalize_url)


class Rejection(StrEnum):
    """Why :func:`check_link` refused a link."""

    INVALID = "invalid"  # not a public http(s) URL (IP, localhost, port, userinfo...)
    TELEGRAM = "telegram"  # Telegram's own links (t.me, telegram.me, ...)
    UNSUPPORTED = "unsupported"  # no extractor (or a DRM/blocklisted site)
    BROKEN = "broken"  # yt-dlp marks the extractor for this URL as broken
    COLLECTION = "collection"  # profile / channel / playlist / album page
    BLOCKED = "blocked"  # excluded by ALLOWED_PROVIDERS / BLOCKED_PROVIDERS
    UNRESOLVED = "unresolved"  # a share link that could not be followed right now


@dataclass(frozen=True)
class LinkCheck:
    """Outcome of :func:`check_link` / :func:`check_link_async`."""

    url: str  # cleaned/canonical URL ("" when the input is not a URL)
    match: ProviderMatch | None
    reason: Rejection | None = None
    extractor_key: str | None = None  # extractor claiming the URL (also on refusal)
    resolvable: bool = False  # a known share link check_link_async() can resolve

    @property
    def ok(self) -> bool:
        return self.match is not None


# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _words(text: str) -> frozenset[str]:
    """``frozenset`` of the whitespace separated words of ``text``."""
    return frozenset(text.split())


class _Url(NamedTuple):
    scheme: str
    userinfo: str  # "" when absent
    host: str  # lowercase, no brackets, no trailing dot
    port: int | None  # None when absent or default
    path: str
    query: str
    fragment: str

    def netloc(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        netloc = f"{self.userinfo}@{host}" if self.userinfo else host
        if self.port is not None:
            netloc = f"{netloc}:{self.port}"
        return netloc

    def geturl(self) -> str:
        url = f"{self.scheme}://{self.netloc()}{self.path}"
        if self.query:
            url = f"{url}?{self.query}"
        if self.fragment:
            url = f"{url}#{self.fragment}"
        return url


def _parse(url: str) -> _Url | None:
    """Split an absolute URL; ``None`` when it cannot be parsed."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")
    if not scheme or not host:
        return None
    userinfo, sep, _hostport = parts.netloc.rpartition("@")
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    return _Url(
        scheme,
        userinfo if sep else "",
        host,
        port,
        parts.path,
        parts.query,
        parts.fragment,
    )


def _host_in(host: str, domains: Iterable[str]) -> bool:
    """True when ``host`` is one of ``domains`` or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domains)


def _host_of(url: str) -> str:
    parsed = _parse(url)
    return parsed.host if parsed else ""


_ASCII_LABEL_RE = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")


def _valid_host(host: str) -> bool:
    """Syntactic hostname check (IP literals are syntactically valid)."""
    if not host or len(host) > 253:
        return False
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return True
    for label in host.split("."):
        if not label or len(label) > 63:
            return False
        if label.isascii():
            if not _ASCII_LABEL_RE.fullmatch(label):
                return False
            continue
        try:
            label.encode("idna")
        except UnicodeError:
            return False
    return True


# Special-use / private names that never host public media.
_PRIVATE_SUFFIXES = _words(
    "localhost local localdomain internal intranet lan home corp home.arpa arpa"
    " invalid test example onion"
)


def _is_public_host(host: str) -> bool:
    if not _valid_host(host):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False  # IP literals are refused
    labels = host.split(".")
    if len(labels) < 2 or labels[-1].isdigit():
        return False  # single label ("intranet") or numeric TLD ("127.1")
    return not _host_in(host, _PRIVATE_SUFFIXES)


def _is_public_web_url(url: str) -> bool:
    """http(s), no userinfo, default port, public DNS name."""
    parsed = _parse(url)
    if parsed is None or parsed.scheme not in _DEFAULT_PORTS:
        return False
    if parsed.userinfo or parsed.port is not None:
        return False
    return _is_public_host(parsed.host)


def _query_items(query: str) -> list[tuple[str, str, str]]:
    """``[(key, value, raw_segment)]`` with decoded key/value."""
    items = []
    for segment in query.split("&"):
        if not segment:
            continue
        key, _, value = segment.partition("=")
        items.append((unquote_plus(key), unquote_plus(value), segment))
    return items


def _query_value(query: str, name: str) -> str | None:
    for key, value, _raw in _query_items(query):
        if key == name:
            return value
    return None


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------
# Zero-width space/joiners, word joiner, BOM and soft hyphen.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
_LEADING_JUNK = "([{<\"'`\u00ab\u2039\u201c\u2018\u201e\u201a\u300c\u300e"
_TRAILING_PUNCT = ".,;:!?\u2026\u3002\uff0c\u3001\uff01\uff1f\uff1a\uff1b"
_TRAILING_QUOTES = "\"'`\u00bb\u203a\u201d\u2019\u201c\u2018\u300d\u300f"
_CLOSING_BRACKETS = {")": "(", "]": "[", "}": "{", ">": "<"}
_SCHEME_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*):(?![0-9])")


def _strip_wrapping(text: str) -> str:
    """Remove wrapping quotes/brackets and trailing sentence punctuation."""
    text = text.lstrip(_LEADING_JUNK)
    while text:
        last = text[-1]
        if last in _TRAILING_PUNCT or last in _TRAILING_QUOTES:
            text = text[:-1]
            continue
        opener = _CLOSING_BRACKETS.get(last)
        if opener is not None and text.count(last) > text.count(opener):
            text = text[:-1]  # unbalanced closing bracket: "(see https://x/y)"
            continue
        break
    return text


def clean_url(url: str | None) -> str | None:
    """Return a syntactically clean absolute http(s) URL, or ``None``.

    Strips whitespace, zero-width characters, wrapping quotes/brackets and
    trailing punctuation; adds ``https://`` to scheme-less URLs
    (``youtu.be/x``); lowercases scheme and host; drops the default port.
    Path, query and fragment are kept as written (see :func:`match_provider`
    for the provider-specific rewrite handed to yt-dlp).
    """
    if not isinstance(url, str):
        return None
    text = _strip_wrapping(url.translate(_ZERO_WIDTH).strip())
    if not text or "\\" in text or any(ch.isspace() for ch in text):
        return None
    scheme_match = _SCHEME_RE.match(text)
    if text.startswith("//"):
        text = "https:" + text
    elif scheme_match:
        scheme = scheme_match.group(1).lower()
        rest = text[scheme_match.end() :]
        if scheme not in _DEFAULT_PORTS or not rest.startswith("//"):
            return None  # mailto:, tg://, javascript:, ftp://, "http:foo"
        text = f"{scheme}:{rest}"
    else:
        text = "https://" + text
    parsed = _parse(text)
    if parsed is None or not _valid_host(parsed.host):
        return None
    return parsed.geturl()


# ---------------------------------------------------------------------------
# Extraction from Telegram messages
# ---------------------------------------------------------------------------
# Fallback for URLs that are not covered by Telegram entities. Only explicit
# "http(s)://" or "www." URLs: Telegram itself linkifies scheme-less ones.
_TEXT_URL_RE = re.compile(
    r"(?<![\w@.])(?:https?://|www\.)"
    r"[^\s<>\"'`\u00ab\u00bb\u201c\u201d\u2018\u2019\u200b-\u200d\u2060\ufeff]+",
    re.IGNORECASE,
)
_URL_ENTITY_TYPES = ("url", "text_link")


class _Candidate(NamedTuple):
    start: int  # code point index in the text
    end: int
    raw: str
    from_text: bool  # the URL is written in the text (not a text_link target)


def _entity_field(entity: Any, name: str) -> Any:
    if isinstance(entity, dict):
        return entity.get(name)
    return getattr(entity, name, None)


class _Utf16Index:
    """Convert Telegram UTF-16 offsets into Python string indices."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._table: dict[int, int] | None = None
        if all(ord(ch) <= 0xFFFF for ch in text):
            return  # BMP only: offsets are code point indices
        table: dict[int, int] = {}
        units = 0
        for index, ch in enumerate(text):
            table[units] = index
            units += 2 if ord(ch) > 0xFFFF else 1
        table[units] = len(text)
        self._table = table

    def index(self, offset: int) -> int | None:
        if self._table is None:
            return offset if 0 <= offset <= len(self._text) else None
        return self._table.get(offset)


def _entity_candidates(text: str, entities: Iterable[Any]) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    utf16: _Utf16Index | None = None
    for entity in entities:
        etype = _entity_field(entity, "type")
        etype = str(getattr(etype, "value", etype) or "")
        if etype not in _URL_ENTITY_TYPES:
            continue
        try:
            offset = int(_entity_field(entity, "offset"))
            length = int(_entity_field(entity, "length"))
        except (TypeError, ValueError):
            continue
        if offset < 0 or length <= 0:
            continue
        if utf16 is None:
            utf16 = _Utf16Index(text)
        start, end = utf16.index(offset), utf16.index(offset + length)
        if start is None or end is None or start >= end:
            continue  # out of range or splitting a surrogate pair
        if etype == "url":
            candidates.append(_Candidate(start, end, text[start:end], True))
            continue
        target = _entity_field(entity, "url")
        if isinstance(target, str) and target:
            candidates.append(_Candidate(start, end, target, False))
    return candidates


def _candidates(text: str, entities: Iterable[Any] | None) -> list[_Candidate]:
    candidates = _entity_candidates(text, entities) if entities else []
    covered = [(c.start, c.end) for c in candidates]
    for found in _TEXT_URL_RE.finditer(text):
        start, end = found.span()
        if any(start < c_end and c_start < end for c_start, c_end in covered):
            continue
        candidates.append(_Candidate(start, end, found.group(0), True))
    candidates.sort(key=lambda c: (c.start, c.end))
    return candidates


def extract_urls(text: str | None, entities: Iterable[Any] | None = None) -> list[str]:
    """Return the URLs of a message: cleaned, in text order, deduplicated.

    ``entities`` are Telegram ``url``/``text_link`` entities (aiogram
    ``MessageEntity`` objects or dicts with ``type``/``offset``/``length``/
    ``url``); offsets are UTF-16 code units. URLs not covered by an entity are
    found with a regex. Duplicates are detected with :func:`normalize_url`
    (``youtu.be/ID`` and ``youtube.com/watch?v=ID&si=...`` are the same link).
    Telegram links and unsupported sites are returned too: callers decide.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for candidate in _candidates(text or "", entities):
        url = clean_url(candidate.raw)
        if url is None:
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def text_without_urls(text: str | None, entities: Iterable[Any] | None = None) -> str:
    """The message text with every written URL removed.

    The visible text of ``text_link`` entities is kept; spaces are collapsed
    and empty lines dropped, other line breaks are kept. Useful to build a
    description/caption from the rest of the message.
    """
    source = text or ""
    spans = [(c.start, c.end) for c in _candidates(source, entities) if c.from_text]
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        if start < cursor:
            continue
        pieces.append(source[cursor:start])
        cursor = end
    pieces.append(source[cursor:])
    lines = (" ".join(line.split()) for line in "".join(pieces).splitlines())
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Telegram links
# ---------------------------------------------------------------------------
_TELEGRAM_DOMAINS = ("t.me", "telegram.me", "telegram.dog")


def is_telegram_link(url: str | None) -> bool:
    """True for Telegram's own links (t.me, telegram.me, telegram.dog, tg://)."""
    if not isinstance(url, str):
        return False
    text = url.strip()
    if text.lower().startswith("tg:"):
        return True
    cleaned = clean_url(text)
    return cleaned is not None and _host_in(_host_of(cleaned), _TELEGRAM_DOMAINS)


# ---------------------------------------------------------------------------
# Offline provider rewrites (the URL handed to yt-dlp)
# ---------------------------------------------------------------------------
# Mirrors/embed fixers that keep the original path, and retired short domains.
_HOST_ALIASES = {
    "instagr.am": "www.instagram.com",
    "ddinstagram.com": "www.instagram.com",
    "fxtwitter.com": "x.com",
    "vxtwitter.com": "x.com",
    "fixupx.com": "x.com",
    "fixvx.com": "x.com",
    "twittpr.com": "x.com",
    "vxtiktok.com": "www.tiktok.com",
    "rxddit.com": "www.reddit.com",
    "vxreddit.com": "www.reddit.com",
    "fxbsky.app": "bsky.app",
    "m.bilibili.com": "www.bilibili.com",
}
# Outbound-link wrappers and open redirectors: host -> (path, query parameter
# holding the target; None when the whole query string is the target).
_REDIRECT_WRAPPERS: dict[str, tuple[str, str | None]] = {
    "l.facebook.com": ("/l.php", "u"),
    "lm.facebook.com": ("/l.php", "u"),
    "l.instagram.com": ("/", "u"),
    "consent.youtube.com": ("/m", "continue"),  # YouTube's cookie consent page
    "href.li": ("/", None),  # https://href.li/?<url>
}
# Facebook's external link warning (any facebook.com host): /flx/warn/?u=<url>.
_FACEBOOK_LINK_WARNING = "/flx/warn"


@dataclass(frozen=True)
class _SiteParams:
    """Query parameters of one site that never identify the media."""

    domains: tuple[str, ...]
    names: frozenset[str]
    prefixes: tuple[str, ...] = ()


_TRACKING_PREFIXES = ("utm_",)
_GLOBAL_TRACKING = _words(
    "fbclid gclid dclid gbraid wbraid msclkid mc_cid mc_eid igsh igshid yclid"
    " ttclid twclid srsltid _hsenc _hsmi mkt_tok _ga _gl"
)
_SITE_TRACKING = (
    _SiteParams(
        ("youtube.com", "youtu.be", "youtube-nocookie.com"),
        _words("si feature pp ab_channel source_ve_path"),
        ("embeds_",),
    ),
    _SiteParams(
        ("tiktok.com",),
        _words(
            "_r _t is_from_webapp sender_device sender_web_id web_id is_copy_url"
            " share_app_id share_item_id share_link_id social_sharing u_code"
            " timestamp tt_from checksum"
        ),
    ),
    _SiteParams(
        ("facebook.com", "fb.watch"),
        _words(
            "mibextid rdid share_url sfnsn extid ref refsrc fs _rdr paipv eav"
            " comment_tracking notif_id notif_t hc_ref fref"
        ),
        ("__cft__", "__tn__"),
    ),
    _SiteParams(("x.com", "twitter.com"), _words("s t ref_src ref_url")),
    _SiteParams(("reddit.com",), _words("share_id rdt ref ref_source")),
    _SiteParams(("soundcloud.com", "spotify.com"), _words("si ref")),
    _SiteParams(
        ("pinterest.com", "pin.it"), _words("invite_code sender sfo mweb_unauth_id")
    ),
    _SiteParams(
        ("bilibili.com", "b23.tv"),
        _words(
            "spm_id_from vd_source share_source share_medium share_plat"
            " share_session_id share_tag share_from bbid ts unique_k buvid"
            " is_story_h5 mid plat_id up_id from_spmid"
        ),
    ),
    _SiteParams(("vimeo.com",), _words("share")),
    _SiteParams(("twitch.tv",), _words("tt_content tt_medium")),
    _SiteParams(
        ("linkedin.com",), _words("trackingid lipi trk trkinfo originalsubdomain rcm")
    ),
)
# Playlist context of a YouTube *video* URL: yt-dlp would pick the playlist
# ("youtube:tab") extractor; the bot always wants the single video.
_YOUTUBE_PLAYLIST_CONTEXT = _words("list index start_radio playnext rv app")
_YOUTUBE_DOMAINS = ("youtube.com", "youtube-nocookie.com")
_YOUTUBE_ID_RE = re.compile(r"[0-9A-Za-z_-]{11}")
_YOUTUBE_PATH_ID_RE = re.compile(
    r"/(?:shorts|live|embed|v|e)/([0-9A-Za-z_-]{11})(?:[/?#]|$)"
)
_REDDIT_SHORT_RE = re.compile(r"/([a-z0-9]{4,12})/?$")
_REDDIT_GALLERY_RE = re.compile(r"/gallery/([a-z0-9]{4,12})/?$")


def _is_tracking_param(host: str, name: str) -> bool:
    key = name.lower()
    if key.startswith(_TRACKING_PREFIXES) or key in _GLOBAL_TRACKING:
        return True
    return any(
        _host_in(host, site.domains)
        and (key in site.names or key.startswith(site.prefixes))
        for site in _SITE_TRACKING
    )


def _youtube_video_id(parsed: _Url) -> str | None:
    if _host_in(parsed.host, ("youtu.be",)):
        first = parsed.path.lstrip("/").split("/", 1)[0]
        return first if _YOUTUBE_ID_RE.fullmatch(first) else None
    if not _host_in(parsed.host, _YOUTUBE_DOMAINS):
        return None
    if parsed.path == "/watch":
        video_id = _query_value(parsed.query, "v")
        return video_id if video_id and _YOUTUBE_ID_RE.fullmatch(video_id) else None
    found = _YOUTUBE_PATH_ID_RE.match(parsed.path)
    return found.group(1) if found else None


def _wrapped_target(parsed: _Url) -> str | None:
    """The URL behind an outbound-link wrapper or open redirector, if any."""
    wrapper = _REDIRECT_WRAPPERS.get(parsed.host)
    if wrapper is not None and parsed.path == wrapper[0]:
        name = wrapper[1]
        if name is not None:
            return _query_value(parsed.query, name)
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        return parsed.query + fragment if parsed.query else None
    if (
        _host_in(parsed.host, ("facebook.com",))
        and parsed.path.rstrip("/") == _FACEBOOK_LINK_WARNING
    ):
        return _query_value(parsed.query, "u")
    return None


def _unwrap(parsed: _Url) -> _Url:
    """Resolve outbound-link wrappers and retired short domains offline."""
    target = clean_url(_wrapped_target(parsed) or "")
    unwrapped = _parse(target) if target else None
    if unwrapped is not None:
        return unwrapped
    if _host_in(parsed.host, _YOUTUBE_DOMAINS) and parsed.path == "/attribution_link":
        inner = _query_value(parsed.query, "u") or ""
        if inner.startswith("/"):
            unwrapped = _parse("https://www.youtube.com" + inner)
            if unwrapped is not None:
                return unwrapped
    bare = parsed.host.removeprefix("www.")
    alias = _HOST_ALIASES.get(parsed.host) or _HOST_ALIASES.get(bare)
    if alias is not None:
        parsed = parsed._replace(host=alias)
    if parsed.host == "redd.it":
        found = _REDDIT_SHORT_RE.fullmatch(parsed.path)
        if found:
            return _parse(f"https://www.reddit.com/comments/{found.group(1)}") or parsed
    if _host_in(parsed.host, ("reddit.com",)):
        found = _REDDIT_GALLERY_RE.fullmatch(parsed.path)
        if found:
            return parsed._replace(path=f"/comments/{found.group(1)}", query="")
    return parsed


def _canonical_query(parsed: _Url) -> str:
    """Drop tracking (and YouTube playlist-context) parameters, keep the rest."""
    video = _youtube_video_id(parsed) is not None
    kept = [
        raw
        for key, _value, raw in _query_items(parsed.query)
        if not _is_tracking_param(parsed.host, key)
        and not (video and key.lower() in _YOUTUBE_PLAYLIST_CONTEXT)
    ]
    return "&".join(kept)


def _canonicalize(url: str) -> str:
    """Provider-specific offline rewrite of a cleaned URL (given to yt-dlp)."""
    parsed = _parse(url)
    if parsed is None:
        return url
    parsed = _unwrap(parsed)
    return parsed._replace(query=_canonical_query(parsed)).geturl()


# ---------------------------------------------------------------------------
# Normalization (dedupe key)
# ---------------------------------------------------------------------------
_GENERIC_HOST_PREFIXES = ("www.", "m.", "mobile.")
_SITE_HOST_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("facebook.com", ("web.", "mbasic.", "touch.")),
    ("youtube.com", ("music.",)),
    ("reddit.com", ("old.", "new.", "np.", "sh.")),
)
_CANONICAL_HOSTS = {"twitter.com": "x.com", "youtube-nocookie.com": "youtube.com"}
_NORMALIZE_DROP = frozenset({"si", "feature"})
_INSTAGRAM_ID_RE = re.compile(r"/(?:[^/]+/)?(?:p|reels?|tv)/([^/?#&]+)")
# /video/N and /photo/N select one media item of a post (yt-dlp downloads only
# that one with --no-playlist; both count the post's media list from 1).
_X_STATUS_RE = re.compile(
    r"/(?:(?:i/web|i|[^/]+)/status(?:es)?|statuses)/(\d+)(?:/(?:video|photo)/(\d+))?"
)
_REDDIT_POST_RE = re.compile(r"/(?:(?:r|u|user)/[^/]+/)?comments/([a-z0-9]+)")
_FACEBOOK_VIDEO_RE = re.compile(r"/(?:[^/]+/videos/(?:[^/]+/)?|watch/|reel/)(\d+)")
_VIMEO_ID_RE = re.compile(r"/(?:video/|channels/[^/]+/)?(\d+)(?:/([0-9a-f]{6,}))?/?$")
# Ids are case-sensitive: private/partner videos have mixed-case "k..." ids.
_DAILYMOTION_ID_RE = re.compile(r"/(?:embed/)?video/([0-9A-Za-z]+)")


def _normalize_host(host: str) -> str:
    changed = True
    while changed:
        changed = False
        for prefix in _GENERIC_HOST_PREFIXES:
            rest = host.removeprefix(prefix)
            if rest != host and "." in rest:
                host, changed = rest, True
        for domain, prefixes in _SITE_HOST_PREFIXES:
            for prefix in prefixes:
                if host == prefix + domain:
                    host, changed = domain, True
    return _CANONICAL_HOSTS.get(host, host)


def _identity_url(host: str, parsed: _Url) -> str | None:
    """Canonical form for providers whose URLs have many equivalent shapes."""
    path = parsed.path
    video_id = _youtube_video_id(parsed)
    if video_id is not None:
        return f"https://youtube.com/watch?v={video_id}"
    if host == "instagram.com" and (found := _INSTAGRAM_ID_RE.match(path)):
        return f"https://instagram.com/p/{found.group(1)}"
    if host == "x.com" and (found := _X_STATUS_RE.match(path)):
        media = f"/video/{found.group(2)}" if found.group(2) else ""
        return f"https://x.com/i/status/{found.group(1)}{media}"
    if host == "reddit.com" and (found := _REDDIT_POST_RE.match(path)):
        return f"https://reddit.com/comments/{found.group(1)}"
    if host == "facebook.com":
        found = _FACEBOOK_VIDEO_RE.match(path)
        if found and path.startswith("/reel/"):
            return f"https://facebook.com/reel/{found.group(1)}"
        video = found.group(1) if found else None
        if video is None and path in ("/watch", "/watch/", "/video.php"):
            video = _query_value(parsed.query, "v")
        if video and video.isdigit():
            return f"https://facebook.com/watch?v={video}"
    if host in ("vimeo.com", "player.vimeo.com") and (
        found := _VIMEO_ID_RE.match(path)
    ):
        suffix = f"/{found.group(2)}" if found.group(2) else ""
        return f"https://vimeo.com/{found.group(1)}{suffix}"
    if host == "dai.ly" and path.strip("/"):
        return f"https://dailymotion.com/video/{path.strip('/')}"
    if host == "dailymotion.com" and (found := _DAILYMOTION_ID_RE.match(path)):
        return f"https://dailymotion.com/video/{found.group(1)}"
    return None


def normalize_url(url: str) -> str:
    """Canonical key used to detect duplicate links.

    Lowercase host without ``www.``/``m.``/``mobile.`` (and a few
    provider-specific mirrors such as ``music.youtube.com``,
    ``old.reddit.com``, ``twitter.com`` -> ``x.com``), ``https`` scheme, no
    fragment, no tracking parameters (``utm_*``, ``si``, ``igsh``,
    ``fbclid``, ``feature``, ...), sorted query, no trailing slash. Known
    providers are reduced to their media id (``youtu.be/ID``,
    ``youtube.com/shorts/ID`` and ``watch?v=ID&list=...`` all become
    ``https://youtube.com/watch?v=ID``). The result is a key, not
    necessarily a fetchable URL.
    """
    cleaned = clean_url(url)
    if cleaned is None:
        return (url or "").strip() if isinstance(url, str) else ""
    parsed = _parse(_canonicalize(cleaned))
    if parsed is None:  # pragma: no cover - clean_url output always parses
        return cleaned
    host = _normalize_host(parsed.host)
    identity = _identity_url(host, parsed)
    if identity is not None:
        return identity
    pairs = sorted(
        (key, value)
        for key, value, _raw in _query_items(parsed.query)
        if not _is_tracking_param(host, key) and key.lower() not in _NORMALIZE_DROP
    )
    path = parsed.path.rstrip("/")
    query = urlencode(pairs, quote_via=quote)
    return _Url("https", "", host, parsed.port, path, query, "").geturl()


# ---------------------------------------------------------------------------
# yt-dlp extractor registry
# ---------------------------------------------------------------------------
# Never used for matching: Generic would accept any page, and the open
# redirectors hand any URL to another extractor, skipping every check of
# check_link() (block lists, collections, dedupe). Their targets are unwrapped
# offline (_unwrap) or resolved as share links (t.co) and checked instead.
_EXCLUDED_KEYS = frozenset(
    {
        "Generic",
        "FacebookRedirectURL",
        "HrefLiRedirect",
        "TwitterShortener",
        "YoutubeConsentRedirect",
    }
)
# Families of error-only extractors (KnownDRM, KnownPiracy, ...).
_UNSUPPORTED_FAMILIES = frozenset({"unsupported", "commonmistakes"})
# Collections whose _RETURN_TYPE is not "playlist" (feeds, channel tabs, ...).
_COLLECTION_KEYS = _words(
    "YoutubeTab YoutubeFavourites YoutubeHistory YoutubeNotifications"
    " YoutubeRecommended YoutubeShortsAudioPivot YoutubeSubscriptions"
    " YoutubeWatchLater YoutubeYtUser BilibiliPlaylist TwitterEvent"
)
# "playlist" extractors that are single posts with several media (like a
# carousel), not collections.
_MULTI_MEDIA_POST_KEYS = _words(
    "VKWallPost ImgurAlbum WykopPost TapTapMoment TapTapPostIntl"
)
# Extractors returning an error for truncated/incomplete links.
_INVALID_LINK_KEYS = frozenset({"YoutubeTruncatedID", "YoutubeTruncatedURL"})
_WARM_UP_URL = "https://warm-up.invalid/"


@dataclass(frozen=True)
class _Extractor:
    cls: Any  # yt-dlp (lazy) extractor class
    key: str
    ie_name: str
    family: str  # yt-dlp module, e.g. "youtube"; groups extractors of one site
    working: bool
    collection: bool
    unsupported: bool


@dataclass(frozen=True)
class _Registry:
    extractors: tuple[_Extractor, ...]
    by_key: dict[str, _Extractor]
    version: str
    import_seconds: float
    build_seconds: float


_registry_lock = threading.Lock()
_registry_value: _Registry | None = None
_compile_seconds: float | None = None


def _family_of(cls: Any) -> str:
    module = getattr(cls, "_module", None) or cls.__module__
    prefix = "yt_dlp.extractor."
    if module.startswith(prefix):
        family = module[len(prefix) :].split(".", 1)[0]
    else:  # plugins
        family = module.rsplit(".", 1)[-1]
    return family.lstrip("_").lower() or "unknown"


def _is_collection(cls: Any, key: str) -> bool:
    if key in _COLLECTION_KEYS:
        return True
    if key in _MULTI_MEDIA_POST_KEYS:
        return False
    return getattr(cls, "_RETURN_TYPE", None) == "playlist"


def _build_registry() -> _Registry:
    started = time.perf_counter()
    from yt_dlp.extractor import gen_extractor_classes
    from yt_dlp.version import __version__ as ytdlp_version

    imported = time.perf_counter()
    entries: list[_Extractor] = []
    for cls in gen_extractor_classes():
        key = cls.ie_key()
        if key in _EXCLUDED_KEYS or not cls._ENABLED:
            continue
        family = _family_of(cls)
        entries.append(
            _Extractor(
                cls=cls,
                key=key,
                ie_name=str(cls.IE_NAME),
                family=family,
                working=bool(cls.working()),
                collection=_is_collection(cls, key),
                unsupported=family in _UNSUPPORTED_FAMILIES,
            )
        )
    built = time.perf_counter()
    registry = _Registry(
        extractors=tuple(entries),
        by_key={entry.key: entry for entry in entries},
        version=ytdlp_version,
        import_seconds=imported - started,
        build_seconds=built - imported,
    )
    logger.debug(
        "yt-dlp %s: %d extractors loaded (import %.3fs, build %.3fs)",
        registry.version,
        len(entries),
        registry.import_seconds,
        registry.build_seconds,
    )
    return registry


def _registry() -> _Registry:
    """The cached extractor list (built on first use, thread-safe)."""
    global _registry_value
    registry = _registry_value
    if registry is not None:
        return registry
    with _registry_lock:
        if _registry_value is None:
            _registry_value = _build_registry()
        return _registry_value


@functools.lru_cache(maxsize=4096)
def _lookup(url: str) -> _Extractor | None:
    """First enabled extractor claiming ``url`` (yt-dlp's own order)."""
    for entry in _registry().extractors:
        try:
            if entry.cls.suitable(url):
                return entry
        except (re.error, TypeError, ValueError, AttributeError):
            logger.debug("suitable() failed for extractor %s", entry.key, exc_info=True)
    return None


def warm_up() -> None:
    """Build the extractor list and pre-compile every URL regex (~1 s).

    Blocking: call it from a thread (``await asyncio.to_thread(warm_up)``).
    Safe to call more than once and from several threads.
    """
    global _compile_seconds
    registry = _registry()
    with _registry_lock:
        if _compile_seconds is not None:
            return
        started = time.perf_counter()
        for entry in registry.extractors:
            try:
                entry.cls.suitable(_WARM_UP_URL)
            except (re.error, TypeError, ValueError, AttributeError):
                logger.warning(
                    "yt-dlp extractor %s has an invalid URL pattern", entry.key
                )
        _compile_seconds = time.perf_counter() - started
    logger.info(
        "yt-dlp %s ready: %d extractors, %d sites (import %.2fs, build %.2fs, "
        "regex compile %.2fs)",
        registry.version,
        len(registry.extractors),
        len(_site_families(registry)),
        registry.import_seconds,
        registry.build_seconds,
        _compile_seconds,
    )


def stats() -> dict[str, Any]:
    """Diagnostics about the extractor registry (builds it if needed)."""
    registry = _registry()
    cache = _lookup.cache_info()
    return {
        "yt_dlp_version": registry.version,
        "extractors": len(registry.extractors),
        "working_extractors": sum(1 for e in registry.extractors if e.working),
        "sites": len(_site_families(registry)),
        "import_seconds": round(registry.import_seconds, 4),
        "build_seconds": round(registry.build_seconds, 4),
        "compile_seconds": (
            round(_compile_seconds, 4) if _compile_seconds is not None else None
        ),
        "lookup_cache": {
            "hits": cache.hits,
            "misses": cache.misses,
            "size": cache.currsize,
        },
    }


# ---------------------------------------------------------------------------
# Allowed / blocked providers
# ---------------------------------------------------------------------------
_FAMILY_ALIASES: dict[str, tuple[str, ...]] = {
    "twitter": ("x",),
    "ninegag": ("9gag",),
    "lbry": ("odysee",),
    "archiveorg": ("archive.org", "internetarchive"),
    "afreecatv": ("soop",),
}
# Short and alternative domains of a site that are not subdomains of its main
# domain. Only domains serving that site's media: general-purpose shorteners
# (t.co, fb.me, lnkd.in) point anywhere and name no site.
_DOMAIN_FAMILIES: dict[str, str] = {
    "youtu.be": "youtube",
    "youtube-nocookie.com": "youtube",
    "dai.ly": "dailymotion",
    "redd.it": "reddit",
    "fb.watch": "facebook",
    "instagr.am": "instagram",
    "pin.it": "pinterest",
    "b23.tv": "bilibili",
}


def _config_list(name: str) -> tuple[str, ...]:
    value = getattr(config, name, ()) or ()
    if isinstance(value, str):
        value = value.split(",")
    return tuple(item.strip().lower() for item in value if str(item).strip())


def _entry_tokens(entry: _Extractor) -> set[str]:
    ie_name = entry.ie_name.lower()
    return {
        entry.key.lower(),
        ie_name,
        ie_name.split(":", 1)[0],
        entry.family,
        *_FAMILY_ALIASES.get(entry.family, ()),
    }


def _named_family(token: str, families: Iterable[str]) -> str | None:
    if token in families:
        return token
    for family, aliases in _FAMILY_ALIASES.items():
        if token in aliases:
            return family
    return None


def _family_of_rule(rule: str, families: Iterable[str]) -> str | None:
    """Site family that a rule names as a whole site, if any.

    A name ("youtube", "x", "9gag"), the site's own domain ("youtube.com",
    "twitter.com", also as "www.youtube.com" or "*.youtube.com") or one of
    its short domains ("youtu.be", "dai.ly"). A subdomain rule
    ("vm.tiktok.com", "music.youtube.com") names only the hosts under it:
    ``None``. Shared by the link checks and :func:`supported_sites_text`, so
    the sites listed to users are the ones the checks accept.
    """
    rule = rule.lstrip("*").lstrip(".")
    if "." not in rule:
        return _named_family(rule, families)
    domain = rule.removeprefix("www.")
    family = _DOMAIN_FAMILIES.get(domain) or _named_family(domain, families)
    if family is None and domain.count(".") == 1:  # "tiktok.com" -> "tiktok"
        family = _named_family(domain.split(".", 1)[0], families)
    return family


def _rule_matches(rule: str, tokens: set[str], host: str, family: str) -> bool:
    rule = rule.lstrip("*").lstrip(".")
    if not rule:
        return False
    if rule in tokens:
        return True
    if "." not in rule:
        return False
    # A domain covers the hosts under it and, when it names a whole site,
    # every link of that site: "youtube.com" also covers youtu.be,
    # youtube-nocookie.com and Invidious mirrors, "x.com" covers twitter.com.
    return _host_in(host, (rule,)) or _family_of_rule(rule, (family,)) == family


def _provider_allowed(entry: _Extractor, host: str) -> bool:
    tokens = _entry_tokens(entry)

    def matches(rule: str) -> bool:
        return _rule_matches(rule, tokens, host, entry.family)

    if any(matches(rule) for rule in _config_list("BLOCKED_PROVIDERS")):
        return False
    allowed = _config_list("ALLOWED_PROVIDERS")
    return not allowed or any(matches(rule) for rule in allowed)


def _collections_allowed() -> bool:
    return bool(getattr(config, "ALLOW_COLLECTION_LINKS", False))


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def check_link(url: str) -> LinkCheck:
    """Decide whether the bot can download ``url`` (no network access).

    Blocking for a few milliseconds (and about one second on the very first
    call if :func:`warm_up` was not called): from async code prefer
    :func:`check_link_async`.
    """
    cleaned = clean_url(url)
    if cleaned is None:
        return LinkCheck(url="", match=None, reason=Rejection.INVALID)
    if is_telegram_link(cleaned):
        return LinkCheck(url=cleaned, match=None, reason=Rejection.TELEGRAM)
    if not _is_public_web_url(cleaned):
        return LinkCheck(url=cleaned, match=None, reason=Rejection.INVALID)
    canonical = _canonicalize(cleaned)
    if is_telegram_link(canonical):
        return LinkCheck(url=canonical, match=None, reason=Rejection.TELEGRAM)
    if not _is_public_web_url(canonical):
        return LinkCheck(url=canonical, match=None, reason=Rejection.INVALID)
    entry = _lookup(canonical)
    key = entry.key if entry else None
    if entry is None or entry.unsupported:
        return LinkCheck(
            url=canonical,
            match=None,
            reason=Rejection.UNSUPPORTED,
            extractor_key=key,
            resolvable=is_short_link(canonical),
        )
    reason: Rejection | None = None
    host = _host_of(canonical)
    if entry.key in _INVALID_LINK_KEYS:
        reason = Rejection.INVALID
    elif entry.collection and not _collections_allowed():
        # Refused by policy whether or not the extractor works: "retry later"
        # would be wrong (many profile extractors, e.g. Instagram's, are broken).
        reason = Rejection.COLLECTION
    elif not entry.working:
        reason = Rejection.BROKEN
    elif not _provider_allowed(entry, host):
        reason = Rejection.BLOCKED
    if reason is not None:
        return LinkCheck(url=canonical, match=None, reason=reason, extractor_key=key)
    match = ProviderMatch(
        key=entry.key,
        name=_display_name(entry, host),
        url=canonical,
        normalized_url=normalize_url(canonical),
    )
    return LinkCheck(url=canonical, match=match, extractor_key=entry.key)


def match_provider(url: str) -> ProviderMatch | None:
    """The provider of ``url`` or ``None`` when the bot cannot download it.

    Only public http(s) URLs (no userinfo, default port, no IP literal,
    localhost or single-label host) claimed by a working, enabled yt-dlp
    extractor other than "generic", not a collection page, and allowed by
    ``config.ALLOWED_PROVIDERS`` / ``config.BLOCKED_PROVIDERS`` (extractor
    key, IE name, site family such as "youtube", or a domain such as
    "vimeo.com": a site's own domain covers the whole site, e.g.
    "youtube.com" also covers youtu.be links, a subdomain such as
    "vm.tiktok.com" only the hosts under it). Lookups are cached. No network
    access: share links such as ``fb.watch/...`` need
    :func:`match_provider_async`.
    """
    return check_link(url).match


async def check_link_async(url: str, *, resolve: bool = True) -> LinkCheck:
    """:func:`check_link` off the event loop, resolving known share links.

    A share link is answered with the check of its target (blocked,
    collection, unsupported site, ...); one that cannot be followed right now
    (network or HTTP error, login wall, too many redirects) with
    :attr:`Rejection.UNRESOLVED`.
    """
    result = await asyncio.to_thread(check_link, url)
    if result.match is not None or not resolve or not result.resolvable:
        return result
    target = await resolve_short_link(result.url)
    if target is None:
        return replace(result, reason=Rejection.UNRESOLVED)
    return await asyncio.to_thread(check_link, target)


async def match_provider_async(
    url: str, *, resolve: bool = True
) -> ProviderMatch | None:
    """Async :func:`match_provider` that also resolves known share links."""
    return (await check_link_async(url, resolve=resolve)).match


# ---------------------------------------------------------------------------
# Share / short links (network)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _ShortLinkRule:
    host: str
    domains: tuple[str, ...]  # hosts the resolver may visit for this rule
    path: re.Pattern[str] | None = None  # required path; None = any non-root path
    user_agent: str | None = None  # instead of the configured/browser one


# Share links of supported providers that no yt-dlp extractor claims
# (verified against yt-dlp 2026.11.23). t.co is X's shortener for any link:
# it redirects non-browser clients only (browsers get an HTML page), like
# yt-dlp's own TwitterShortener extractor, which is excluded above.
_SHORT_LINK_RULES = (
    _ShortLinkRule("t.co", ("t.co",), user_agent="curl"),
    _ShortLinkRule("fb.watch", ("fb.watch", "facebook.com")),
    _ShortLinkRule("fb.me", ("fb.me", "facebook.com")),
    _ShortLinkRule("facebook.com", ("facebook.com",), re.compile(r"/share/")),
    _ShortLinkRule("instagram.com", ("instagram.com",), re.compile(r"/share/")),
    _ShortLinkRule("v.redd.it", ("v.redd.it", "reddit.com")),
    _ShortLinkRule(
        "reddit.com",
        ("reddit.com",),
        re.compile(r"/(?:(?:r|u|user)/[^/]+/s/|video/)[^/]+"),
    ),
    _ShortLinkRule("pin.it", ("pin.it", "pinterest.com")),
    _ShortLinkRule("on.soundcloud.com", ("soundcloud.com",)),
    _ShortLinkRule("b23.tv", ("b23.tv", "bilibili.com")),
    _ShortLinkRule("t.snapchat.com", ("snapchat.com",)),
    _ShortLinkRule("lnkd.in", ("lnkd.in", "linkedin.com")),
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 3
_RESOLVE_TIMEOUT = 5.0
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _short_link_rule(url: str) -> _ShortLinkRule | None:
    parsed = _parse(url)
    if parsed is None or parsed.scheme not in _DEFAULT_PORTS:
        return None
    if not parsed.path.strip("/"):
        return None
    for rule in _SHORT_LINK_RULES:
        if not _host_in(parsed.host, (rule.host,)):
            continue
        if rule.path is None or rule.path.match(parsed.path):
            return rule
    return None


def is_short_link(url: str) -> bool:
    """True for share links that :func:`resolve_short_link` may follow."""
    cleaned = clean_url(url)
    return cleaned is not None and _short_link_rule(cleaned) is not None


class _ResolveCache:
    """Tiny TTL+LRU cache of resolved short links (event-loop only)."""

    def __init__(
        self, maxsize: int = 512, ttl: float = 3600.0, fail_ttl: float = 120.0
    ):
        self._items: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl
        self._fail_ttl = fail_ttl

    def get(self, key: str) -> tuple[bool, str | None]:
        item = self._items.get(key)
        if item is None:
            return False, None
        expires, value = item
        if expires < time.monotonic():
            del self._items[key]
            return False, None
        self._items.move_to_end(key)
        return True, value

    def put(self, key: str, value: str | None) -> None:
        ttl = self._ttl if value is not None else self._fail_ttl
        self._items[key] = (time.monotonic() + ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self._maxsize:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


_resolve_cache = _ResolveCache()


def _resolve_headers(rule: _ShortLinkRule) -> dict[str, str]:
    user_agent = (
        rule.user_agent or getattr(config, "YTDLP_USER_AGENT", None) or _BROWSER_UA
    )
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }


def _resolve_proxy() -> str | None:
    proxy = getattr(config, "YTDLP_PROXY", None)
    if isinstance(proxy, str) and proxy.lower().startswith(("http://", "https://")):
        return proxy
    return None


def config_problems() -> list[str]:
    """Settings the link checks cannot fully honour (logged at startup)."""
    problems: list[str] = []
    if getattr(config, "YTDLP_PROXY", None) and _resolve_proxy() is None:
        problems.append(
            "YTDLP_PROXY is not an http(s) proxy: share links (fb.watch, pin.it, "
            "t.co, ...) are resolved with direct connections, without the proxy"
        )
    return problems


def _with_https(url: str) -> str:
    parsed = _parse(url)
    if parsed is None or parsed.scheme == "https":
        return url
    return parsed._replace(scheme="https").geturl()


async def _fetch_location(
    session: aiohttp.ClientSession, url: str, timeout: float, rule: _ShortLinkRule
) -> tuple[int | None, str | None]:
    """``(status, Location)`` of ``url``; ``(None, None)`` on network errors.

    The body is never read; ``Location`` is ``None`` unless it redirects.
    """
    try:
        async with session.get(
            url,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers=_resolve_headers(rule),
            proxy=_resolve_proxy(),
        ) as resp:
            if resp.status in _REDIRECT_STATUSES:
                return resp.status, resp.headers.get("Location")
            return resp.status, None
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.info("Could not resolve share link on %s: %r", _host_of(url), exc)
        return None, None


async def _follow(
    url: str, rule: _ShortLinkRule, timeout: float, max_redirects: int
) -> str | None:
    session = await http_client.get_session()
    current = _with_https(url)
    claimed = False  # an extractor claims ``current`` (refusing it)
    for _hop in range(max_redirects):
        status, location = await _fetch_location(session, current, timeout, rule)
        if claimed and status is not None and 200 <= status < 300:
            return current  # no further redirect: the link leads to that page
        if not location:
            if status is not None:
                logger.info(
                    "Share link on %s answered HTTP %s without redirect",
                    _host_of(current),
                    status,
                )
            return None
        try:
            target = clean_url(urljoin(current, location))
        except ValueError:  # e.g. "https://host]/x": not a parsable URL
            target = None
        if target is None:
            logger.info("Share link on %s redirects to an invalid URL", rule.host)
            return None
        check = await asyncio.to_thread(check_link, target)
        if check.match is not None or check.reason is Rejection.BLOCKED:
            return target  # a blocked provider is not even visited
        if not _host_in(_host_of(target), rule.domains):
            # Never fetched: check_link_async() reports why it is refused.
            return target
        # Refused or unclaimed hops inside the provider are followed: they may
        # be intermediate pages that an extractor claims by mistake (pin.it's
        # api.pinterest.com/url_shortener/... looks like a Pinterest board).
        current, claimed = _with_https(target), check.extractor_key is not None
    logger.info(
        "Share link on %s needs more than %d redirects", rule.host, max_redirects
    )
    return None


async def resolve_short_link(
    url: str,
    *,
    timeout: float = _RESOLVE_TIMEOUT,
    max_redirects: int = _MAX_REDIRECTS,
) -> str | None:
    """Follow the redirects of a known share link (allowlisted hosts only).

    Returns the URL the link points to: the first redirect target that
    :func:`check_link` accepts or blocks, the first one outside the
    provider's domains (never fetched), or a page that an extractor claims
    and that answers without redirecting (e.g. a profile). ``None`` when the
    link cannot be followed (network or HTTP error, invalid redirect, a page
    no extractor claims such as a login wall, more than ``max_redirects``
    redirects). Requests go over HTTPS to the allowlisted host (and that
    provider's own domains), never read response bodies and time out after
    ``timeout`` seconds each. Results are cached for an hour (failures for
    two minutes).
    """
    cleaned = clean_url(url)
    if cleaned is None:
        return None
    rule = _short_link_rule(cleaned)
    if rule is None:
        return None
    hit, cached = _resolve_cache.get(cleaned)
    if hit:
        return cached
    result = await _follow(cleaned, rule, timeout, max_redirects)
    _resolve_cache.put(cleaned, result)
    return result


# ---------------------------------------------------------------------------
# Display names
# ---------------------------------------------------------------------------
# Keyed by yt-dlp module ("family"), which groups the extractors of one site.
_FAMILY_NAMES: dict[str, str] = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "twitter": "X (Twitter)",
    "reddit": "Reddit",
    "vimeo": "Vimeo",
    "twitch": "Twitch",
    "dailymotion": "Dailymotion",
    "threads": "Threads",
    "bluesky": "Bluesky",
    "pinterest": "Pinterest",
    "soundcloud": "SoundCloud",
    "bandcamp": "Bandcamp",
    "streamable": "Streamable",
    "imgur": "Imgur",
    "ninegag": "9GAG",
    "bilibili": "Bilibili",
    "kick": "Kick",
    "rumble": "Rumble",
    "vk": "VK",
    "tumblr": "Tumblr",
    "snapchat": "Snapchat",
    "linkedin": "LinkedIn",
    "loom": "Loom",
    "ted": "TED",
    "mixcloud": "Mixcloud",
    "lbry": "Odysee",
    "bitchute": "BitChute",
    "archiveorg": "Internet Archive",
    "dropbox": "Dropbox",
    "googledrive": "Google Drive",
    "patreon": "Patreon",
    "xiaohongshu": "Xiaohongshu (RedNote)",
    "likee": "Likee",
    "medaltv": "Medal",
    "niconico": "Niconico",
    "coub": "Coub",
    "weibo": "Weibo",
    "truth": "Truth Social",
    "gab": "Gab",
    "flickr": "Flickr",
    "rai": "RaiPlay",
    "mediaset": "Mediaset Infinity",
    "la7": "La7",
    "telegram": "Telegram",
    "zoom": "Zoom",
    "applepodcasts": "Apple Podcasts",
    "steam": "Steam",
    "nebula": "Nebula",
    "floatplane": "Floatplane",
    "peertube": "PeerTube",
    "odnoklassniki": "OK.ru",
    "rutube": "Rutube",
    "cda": "CDA",
    "arte": "Arte",
    "bbc": "BBC",
    "cnn": "CNN",
    "redgifs": "RedGifs",
    "youku": "Youku",
    "afreecatv": "SOOP (AfreecaTV)",
    "naver": "Naver",
    "weverse": "Weverse",
    "audiomack": "Audiomack",
    "zdf": "ZDF",
    "francetv": "France TV",
    "nbc": "NBC",
    "twitcasting": "TwitCasting",
    "plutotv": "Pluto TV",
    "tubitv": "Tubi",
    "imdb": "IMDb",
    "vevo": "Vevo",
    "wistia": "Wistia",
    "substack": "Substack",
    "kakao": "Kakao TV",
}
# Extractors living in another site's module.
_KEY_NAMES: dict[str, str] = {
    "Douyin": "Douyin",
    "VKPlay": "VK Play",
    "VKPlayLive": "VK Play",
}
# Shown first by supported_sites_text().
_POPULAR_FAMILIES = (
    "youtube",
    "tiktok",
    "instagram",
    "facebook",
    "twitter",
    "reddit",
    "vimeo",
    "twitch",
)


def _fallback_name(entry: _Extractor) -> str:
    base = entry.ie_name.split(":", 1)[0].strip() or entry.key
    if base.islower() and "." not in base:
        base = base[0].upper() + base[1:]
    return base


def _display_name(entry: _Extractor, host: str | None = None) -> str:
    if entry.key in _KEY_NAMES:
        return _KEY_NAMES[entry.key]
    if entry.family == "youtube" and host and _host_in(host, ("music.youtube.com",)):
        return "YouTube Music"
    return _FAMILY_NAMES.get(entry.family) or _fallback_name(entry)


def provider_display_name(key: str) -> str:
    """Human name for an extractor key ("Youtube" -> "YouTube", "Twitter" -> "X (Twitter)").

    Unknown keys fall back to the extractor's IE name, then to ``key`` itself.
    Loads yt-dlp's extractor list on first use (no regex compilation).
    """
    if not key:
        return ""
    if key in _KEY_NAMES:
        return _KEY_NAMES[key]
    registry = _registry()
    entry = registry.by_key.get(key)
    if entry is None:
        lowered = key.lower()
        entry = next((e for e in registry.extractors if e.key.lower() == lowered), None)
    if entry is None:
        return _FAMILY_NAMES.get(key.lower(), key)
    return _display_name(entry)


def _site_families(registry: _Registry) -> set[str]:
    collections = _collections_allowed()
    return {
        entry.family
        for entry in registry.extractors
        if entry.working
        and not entry.unsupported
        and entry.key not in _INVALID_LINK_KEYS
        and (collections or not entry.collection)
    }


def _family_name(family: str, registry: _Registry) -> str:
    name = _FAMILY_NAMES.get(family)
    if name:
        return name
    entry = next((e for e in registry.extractors if e.family == family), None)
    return _fallback_name(entry) if entry else family


def _rule_display_name(rule: str, registry: _Registry, families: set[str]) -> str:
    family = _family_of_rule(rule, families)
    if family is not None:
        return _family_name(family, registry)
    lowered = rule.lstrip("*").lstrip(".")
    for entry in registry.extractors:
        if lowered in (entry.key.lower(), entry.ie_name.lower()):
            return _display_name(entry)
    return lowered


def _join_italian(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " e " + names[-1]


def supported_sites_text() -> str:
    """Italian list of supported sites for user messages.

    e.g. "YouTube, TikTok, Instagram, Facebook, X (Twitter), Reddit, Vimeo,
    Twitch e oltre 800 altri siti". Honors ALLOWED/BLOCKED_PROVIDERS (only
    rules naming a whole site remove it from the list).
    """
    registry = _registry()
    families = _site_families(registry)
    blocked = {
        family
        for rule in _config_list("BLOCKED_PROVIDERS")
        if (family := _family_of_rule(rule, families))
    }
    allowed = _config_list("ALLOWED_PROVIDERS")
    if allowed:
        names: list[str] = []
        for rule in allowed:
            if _family_of_rule(rule, families) in blocked:
                continue
            name = _rule_display_name(rule, registry, families)
            if name not in names:
                names.append(name)
        return _join_italian(names) or "nessun sito"
    shown = [f for f in _POPULAR_FAMILIES if f in families and f not in blocked]
    others = len(families - set(shown) - blocked)
    names = [_FAMILY_NAMES[f] for f in shown]
    if others >= 100:
        names.append(f"oltre {others // 100 * 100} altri siti")
    elif others > 0:
        names.append(f"altri {others} siti")
    return _join_italian(names)


def _reset_for_tests() -> None:
    """Forget cached lookups and resolved links (the registry is kept)."""
    _lookup.cache_clear()
    _resolve_cache.clear()
