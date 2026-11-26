"""Tests for src.providers.

Matching uses the *installed* yt-dlp (lazy extractors, no network). Only URLs
that yt-dlp 2026.11.23 really claims are asserted as supported. Share-link
resolution is tested with a fake HTTP session.
"""

import asyncio
import threading

import aiohttp
import pytest
from aiogram.enums import MessageEntityType
from aiogram.types import MessageEntity

from src import config, http_client, providers
from src.providers import LinkCheck, ProviderMatch, Rejection

YT_ID = "dQw4w9WgXcQ"
YT = f"https://youtu.be/{YT_ID}"
UTF16_EMOJI = "\U0001f3ac"  # clapper board: 2 UTF-16 code units
FIRE = "\U0001f525"


@pytest.fixture(scope="module", autouse=True)
def _warm_registry():
    providers.warm_up()


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", (), raising=False)
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", (), raising=False)
    monkeypatch.setattr(config, "ALLOW_COLLECTION_LINKS", False, raising=False)
    monkeypatch.setattr(config, "YTDLP_PROXY", None, raising=False)
    providers._reset_for_tests()
    yield
    providers._reset_for_tests()


def url_entity(text, url_text, **extra):
    """aiogram url entity for ``url_text`` inside ``text`` (UTF-16 offsets)."""
    start = text.index(url_text)
    offset = len(text[:start].encode("utf-16-le")) // 2
    length = len(url_text.encode("utf-16-le")) // 2
    return MessageEntity(type="url", offset=offset, length=length, **extra)


# ---------------------------------------------------------------------------
# clean_url
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (YT, YT),
        (f"  {YT}  ", YT),
        (f"{YT}.", YT),
        (f"{YT},", YT),
        (f"{YT}!?", YT),
        (f"{YT};:", YT),
        (f"{YT}\u2026", YT),
        (f"{YT}\u3002", YT),
        (f"({YT})", YT),
        (f"{YT})", YT),
        (f"[{YT}]", YT),
        (f'"{YT}"', YT),
        (f"'{YT}'", YT),
        (f"\u00ab{YT}\u00bb", YT),
        (f"\u201c{YT}\u201d", YT),
        ("<https://vimeo.com/123456789>", "https://vimeo.com/123456789"),
        (f"({YT}).", YT),
        (
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
        ),
        (
            "(https://en.wikipedia.org/wiki/Python_(programming_language))",
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
        ),
        (f"youtu.be/{YT_ID}", YT),
        (
            "www.instagram.com/reel/C9abcDEF123/",
            "https://www.instagram.com/reel/C9abcDEF123/",
        ),
        (f"//youtu.be/{YT_ID}", YT),
        (f"HTTPS://YOUTU.BE/{YT_ID}", YT),
        (
            f"https://www.YouTube.com:443/watch?v={YT_ID}",
            f"https://www.youtube.com/watch?v={YT_ID}",
        ),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        (f"https://you\u200btu.be/{YT_ID}", YT),
        (f"{YT}?si=abc#t=1", f"{YT}?si=abc#t=1"),
        ("https://example.com.", "https://example.com"),
        ("https://\u4f8b\u3048.jp/a", "https://\u4f8b\u3048.jp/a"),
    ],
)
def test_clean_url(raw, expected):
    assert providers.clean_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        42,
        "mailto:someone@example.com",
        "tg://resolve?domain=somebot",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "http:example.com",
        "https://",
        "https://exa mple.com/",
        "https://ex%61mple.com/",
        "https://-bad-.com/",
        "https:\\\\example.com\\a",
        "https://example.com:99999/",
        "https://exa_mple..com/",
        ".,;",
    ],
)
def test_clean_url_rejects_non_web_urls(raw):
    assert providers.clean_url(raw) is None


# ---------------------------------------------------------------------------
# extract_urls
# ---------------------------------------------------------------------------
def test_extract_urls_empty():
    assert providers.extract_urls(None) == []
    assert providers.extract_urls("") == []
    assert providers.extract_urls("nessun link qui") == []


def test_extract_urls_keeps_text_order():
    text = (
        f"prima https://vimeo.com/123456789 poi {YT} e infine https://x.com/u/status/1"
    )
    assert providers.extract_urls(text) == [
        "https://vimeo.com/123456789",
        YT,
        "https://x.com/u/status/1",
    ]


@pytest.mark.parametrize(
    "text",
    [
        f"guarda ({YT})",
        f"guarda {YT}.",
        f"guarda {YT}, bello",
        f"guarda {YT}!",
        f"guarda {YT}?",
        f'guarda "{YT}"',
        f"guarda \u00ab{YT}\u00bb",
        f"[video]({YT})",
        f"<{YT}>",
        f"guarda:{YT}",
    ],
)
def test_extract_urls_strips_wrapping_punctuation(text):
    assert providers.extract_urls(text) == [YT]


def test_extract_urls_keeps_balanced_parentheses():
    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert providers.extract_urls(f"vedi ({url}).") == [url]


def test_extract_urls_dedupes_by_normalized_url():
    text = (
        f"{YT} e https://www.youtube.com/watch?v={YT_ID}&si=abc "
        f"e http://youtu.be/{YT_ID}?feature=share e di nuovo {YT}"
    )
    assert providers.extract_urls(text) == [YT]


def test_extract_urls_keeps_different_links():
    text = f"{YT} https://youtu.be/AAAAAAAAAAA {YT}"
    assert providers.extract_urls(text) == [YT, "https://youtu.be/AAAAAAAAAAA"]


def test_extract_urls_utf16_offsets():
    """The example of the spec: an emoji before the link, entity at offset 3."""
    text = f"{UTF16_EMOJI} https://youtu.be/x"
    entity = MessageEntity(type="url", offset=3, length=18)
    assert entity.extract_from(text) == "https://youtu.be/x"
    assert providers.extract_urls(text, [entity]) == ["https://youtu.be/x"]


def test_extract_urls_utf16_offsets_several_emoji():
    second = "https://vimeo.com/123456789"
    text = f"{FIRE}{FIRE} {YT} {UTF16_EMOJI} ok {second}"
    entities = [url_entity(text, YT), url_entity(text, second)]
    # 2 emoji = 4 UTF-16 units (+1 space); the 2nd URL follows 28+1+2+4 more units.
    assert [e.offset for e in entities] == [5, 40]
    assert providers.extract_urls(text, entities) == [YT, second]


def test_extract_urls_accepts_dict_entities():
    text = f"{UTF16_EMOJI} {YT}"
    entities = [{"type": "url", "offset": 3, "length": len(YT)}]
    assert providers.extract_urls(text, entities) == [YT]


def test_extract_urls_accepts_enum_entity_types():
    text = f"vedi {YT}"
    entity = MessageEntity(type=MessageEntityType.URL, offset=5, length=len(YT))
    assert providers.extract_urls(text, [entity]) == [YT]


def test_extract_urls_scheme_less_entity():
    text = f"vedi youtu.be/{YT_ID} e www.instagram.com/reel/C9abcDEF123"
    entities = [
        url_entity(text, f"youtu.be/{YT_ID}"),
        url_entity(text, "www.instagram.com/reel/C9abcDEF123"),
    ]
    assert providers.extract_urls(text, entities) == [
        YT,
        "https://www.instagram.com/reel/C9abcDEF123",
    ]


def test_extract_urls_text_link_entities_in_text_order():
    text = "guarda qui e anche https://vimeo.com/123456789"
    entities = [
        url_entity(text, "https://vimeo.com/123456789"),
        MessageEntity(type="text_link", offset=7, length=3, url=YT),
    ]
    assert providers.extract_urls(text, entities) == [YT, "https://vimeo.com/123456789"]


def test_extract_urls_text_link_target_wins_over_visible_url():
    visible = "https://youtu.be/AAAAAAAAAAA"
    target = "https://youtu.be/BBBBBBBBBBB"
    entity = MessageEntity(type="text_link", offset=0, length=len(visible), url=target)
    assert providers.extract_urls(visible, [entity]) == [target]


def test_extract_urls_regex_fallback_for_uncovered_urls():
    second = "https://vimeo.com/123456789"
    text = f"a {YT} b {second}"
    assert providers.extract_urls(text, [url_entity(text, YT)]) == [YT, second]


def test_extract_urls_entity_and_regex_do_not_duplicate():
    text = f"guarda {YT}."
    assert providers.extract_urls(text, [url_entity(text, YT)]) == [YT]


def test_extract_urls_ignores_malformed_entities():
    text = f"vedi {YT}"
    entities = [
        {"type": "url"},
        {"type": "url", "offset": "x", "length": 3},
        {"type": "url", "offset": 500, "length": 10},
        {"type": "url", "offset": -1, "length": 10},
        {"type": "url", "offset": 0, "length": 0},
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "text_link", "offset": 0, "length": 4},
        {"type": "text_link", "offset": 0, "length": 4, "url": "tg://user?id=1"},
        object(),
    ]
    assert providers.extract_urls(text, entities) == [YT]


def test_extract_urls_entity_splitting_a_surrogate_pair_is_ignored():
    text = f"{UTF16_EMOJI}{YT}"
    entities = [{"type": "url", "offset": 1, "length": len(YT) + 1}]
    assert providers.extract_urls(text, entities) == [YT]


def test_extract_urls_returns_telegram_and_unsupported_links():
    text = "https://t.me/somechannel/1 https://example.com/page"
    assert providers.extract_urls(text) == [
        "https://t.me/somechannel/1",
        "https://example.com/page",
    ]


def test_extract_urls_www_without_scheme_and_emails():
    text = "sito www.example.com/path, mail foo@www.example.com"
    assert providers.extract_urls(text) == ["https://www.example.com/path"]


def test_extract_urls_case_insensitive_scheme():
    assert providers.extract_urls(f"HTTPS://YOUTU.BE/{YT_ID}") == [YT]


def test_text_without_urls():
    text = f"Guarda {YT} che bello!"
    assert providers.text_without_urls(text) == "Guarda che bello!"
    text = f"{UTF16_EMOJI} titolo youtu.be/{YT_ID} fine"
    entity = url_entity(text, f"youtu.be/{YT_ID}")
    assert providers.text_without_urls(text, [entity]) == f"{UTF16_EMOJI} titolo fine"
    linked = MessageEntity(type="text_link", offset=0, length=10, url=YT)
    assert providers.text_without_urls("guarda qui", [linked]) == "guarda qui"
    assert providers.text_without_urls(None) == ""
    multiline = f"Titolo\n\n  {YT}  \nseconda   riga {YT}?"
    assert providers.text_without_urls(multiline) == "Titolo\nseconda riga"


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("variants", "expected"),
    [
        (
            [
                YT,
                f"{YT}?si=xyz&t=10",
                f"https://www.youtube.com/watch?v={YT_ID}",
                f"https://m.youtube.com/watch?v={YT_ID}&feature=share",
                f"https://music.youtube.com/watch?v={YT_ID}",
                f"https://youtube.com/shorts/{YT_ID}?si=abc",
                f"https://www.youtube.com/live/{YT_ID}",
                f"https://www.youtube-nocookie.com/embed/{YT_ID}",
                f"https://www.youtube.com/watch?v={YT_ID}&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&index=2",
                f"http://youtube.com/watch?v={YT_ID}#t=5",
                f"youtu.be/{YT_ID}",
            ],
            f"https://youtube.com/watch?v={YT_ID}",
        ),
        (
            [
                "https://www.instagram.com/reel/C9abcDEF123/?igsh=MWQ1ZGUxMzBkMA==",
                "https://www.instagram.com/reels/C9abcDEF123/",
                "https://instagram.com/p/C9abcDEF123",
                "https://www.instagram.com/tv/C9abcDEF123/",
                "https://www.instagram.com/someuser/p/C9abcDEF123/",
                "https://instagr.am/p/C9abcDEF123/",
                "https://www.instagram.com/p/C9abcDEF123/?utm_source=ig_web_copy_link",
            ],
            "https://instagram.com/p/C9abcDEF123",
        ),
        (
            [
                "https://twitter.com/someuser/status/1234567890123456789?s=20&t=abc",
                "https://x.com/i/status/1234567890123456789",
                "https://mobile.twitter.com/someuser/status/1234567890123456789",
                "https://fxtwitter.com/someuser/status/1234567890123456789",
                "https://x.com/someuser/status/1234567890123456789/video/1",
                "https://twitter.com/i/web/status/1234567890123456789",
            ],
            "https://x.com/i/status/1234567890123456789",
        ),
        (
            [
                "https://www.reddit.com/r/videos/comments/1abcde/some_title/",
                "https://old.reddit.com/r/videos/comments/1abcde/some_title/",
                "https://new.reddit.com/r/videos/comments/1abcde/",
                "https://www.reddit.com/comments/1abcde",
                "https://redd.it/1abcde",
                "https://www.reddit.com/gallery/1abcde",
                "https://www.reddit.com/r/videos/comments/1abcde/t/?share_id=x&utm_name=y",
            ],
            "https://reddit.com/comments/1abcde",
        ),
        (
            [
                "https://www.facebook.com/watch?v=1234567890123456",
                "https://m.facebook.com/watch/?v=1234567890123456&mibextid=abc",
                "https://web.facebook.com/watch/?v=1234567890123456",
                "https://www.facebook.com/video.php?v=1234567890123456",
                "https://www.facebook.com/somepage/videos/1234567890123456/",
            ],
            "https://facebook.com/watch?v=1234567890123456",
        ),
        (
            [
                "https://www.facebook.com/reel/1234567890123456?mibextid=rS40",
                "https://web.facebook.com/reel/1234567890123456/",
            ],
            "https://facebook.com/reel/1234567890123456",
        ),
        (
            [
                "https://vimeo.com/123456789",
                "https://player.vimeo.com/video/123456789",
                "https://vimeo.com/123456789?share=copy",
            ],
            "https://vimeo.com/123456789",
        ),
        (
            [
                "https://dai.ly/x8abcde",
                "https://www.dailymotion.com/video/x8abcde",
            ],
            "https://dailymotion.com/video/x8abcde",
        ),
        (
            [
                "https://www.tiktok.com/@user/video/7234567890123456789?is_from_webapp=1&sender_device=pc",
                "https://m.tiktok.com/@user/video/7234567890123456789/",
                "https://www.tiktok.com/@user/video/7234567890123456789?_r=1&_t=8abc",
            ],
            "https://tiktok.com/@user/video/7234567890123456789",
        ),
        (
            [
                "https://www.example.com/a/b/?b=2&a=1&utm_source=x&fbclid=y#frag",
                "http://example.com/a/b?a=1&b=2",
                "HTTPS://WWW.EXAMPLE.COM/a/b?a=1&b=2",
            ],
            "https://example.com/a/b?a=1&b=2",
        ),
    ],
)
def test_normalize_url_equivalent_forms(variants, expected):
    for variant in variants:
        assert providers.normalize_url(variant) == expected, variant


def test_normalize_url_distinguishes_different_media():
    assert providers.normalize_url(YT) != providers.normalize_url(
        "https://youtu.be/AAAAAAAAAAA"
    )
    assert providers.normalize_url("https://example.com/a?id=1") != (
        providers.normalize_url("https://example.com/a?id=2")
    )
    # Path case identifies media (YouTube ids, Instagram shortcodes).
    assert (
        providers.normalize_url("https://example.com/Abc") == "https://example.com/Abc"
    )


def test_normalize_url_host_prefixes():
    assert providers.normalize_url("https://m.me/someone") == "https://m.me/someone"
    assert (
        providers.normalize_url("https://www.m.example.org/") == "https://example.org"
    )
    assert providers.normalize_url("https://vm.tiktok.com/ZMabc/") == (
        "https://vm.tiktok.com/ZMabc"
    )


def test_normalize_url_keeps_youtube_playlist_identity():
    url = (
        "https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&si=a"
    )
    assert providers.normalize_url(url) == (
        "https://youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"
    )


def test_normalize_url_invalid_input_is_returned_stripped():
    assert providers.normalize_url("  not a url ") == "not a url"
    assert providers.normalize_url("mailto:a@b.it") == "mailto:a@b.it"
    assert providers.normalize_url(None) == ""


# ---------------------------------------------------------------------------
# is_telegram_link
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/somechannel/123",
        "t.me/somechannel",
        "http://telegram.me/somechannel/1",
        "https://telegram.dog/somebot",
        "https://someone.t.me/",
        "  https://T.ME/somechannel ",
        "tg://resolve?domain=somebot",
        "https://t.me/joinchat/AbCdEf",
    ],
)
def test_is_telegram_link(url):
    assert providers.is_telegram_link(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://telegra.ph/Some-Article-01-01",
        "https://nott.me/x",
        "https://t.me.evil.com/x",
        YT,
        "",
        None,
    ],
)
def test_is_not_telegram_link(url):
    assert providers.is_telegram_link(url) is False


# ---------------------------------------------------------------------------
# match_provider with the installed yt-dlp
# ---------------------------------------------------------------------------
SUPPORTED = [
    # YouTube (desktop, mobile, music, live, shorts, share, embeds)
    (f"https://www.youtube.com/watch?v={YT_ID}", "Youtube", "YouTube"),
    (f"https://m.youtube.com/watch?v={YT_ID}", "Youtube", "YouTube"),
    (f"https://music.youtube.com/watch?v={YT_ID}", "Youtube", "YouTube Music"),
    (f"https://youtube.com/live/{YT_ID}", "Youtube", "YouTube"),
    (f"https://www.youtube.com/live/{YT_ID}?si=abc", "Youtube", "YouTube"),
    (f"https://youtube.com/shorts/{YT_ID}?si=abc", "Youtube", "YouTube"),
    (f"https://m.youtube.com/shorts/{YT_ID}", "Youtube", "YouTube"),
    (YT, "Youtube", "YouTube"),
    (f"{YT}?si=xyz&t=10", "Youtube", "YouTube"),
    (f"https://www.youtube.com/watch?feature=share&v={YT_ID}", "Youtube", "YouTube"),
    (f"https://www.youtube.com/watch?app=desktop&v={YT_ID}", "Youtube", "YouTube"),
    (f"https://www.youtube-nocookie.com/embed/{YT_ID}", "Youtube", "YouTube"),
    (f"https://www.youtube.com/embed/{YT_ID}", "Youtube", "YouTube"),
    (
        f"https://www.youtube.com/watch?v={YT_ID}&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
        "Youtube",
        "YouTube",
    ),
    (
        f"https://music.youtube.com/watch?v={YT_ID}&list=RDAMVM{YT_ID}",
        "Youtube",
        "YouTube Music",
    ),
    (
        "https://youtube.com/clip/UgkxU2HSeGL_NvmDJ-nQJrlLwllwMDBdGZFs",
        "YoutubeClip",
        "YouTube",
    ),
    (
        f"https://www.youtube.com/attribution_link?u=/watch%3Fv%3D{YT_ID}",
        "Youtube",
        "YouTube",
    ),
    # TikTok
    ("https://vm.tiktok.com/ZMabcdef/", "TikTokVM", "TikTok"),
    ("https://vt.tiktok.com/ZSabcdef/", "TikTokVM", "TikTok"),
    ("https://www.tiktok.com/t/ZTabcdef/", "TikTokVM", "TikTok"),
    ("https://www.tiktok.com/@user/video/7234567890123456789", "TikTok", "TikTok"),
    # Instagram
    ("https://www.instagram.com/reel/C9abcDEF123/", "Instagram", "Instagram"),
    ("https://www.instagram.com/reels/C9abcDEF123/", "Instagram", "Instagram"),
    ("https://www.instagram.com/p/C9abcDEF123/", "Instagram", "Instagram"),
    ("https://www.instagram.com/tv/C9abcDEF123/", "Instagram", "Instagram"),
    ("https://www.instagram.com/someuser/p/C9abcDEF123/", "Instagram", "Instagram"),
    ("https://instagr.am/p/C9abcDEF123/", "Instagram", "Instagram"),
    # Facebook
    ("https://www.facebook.com/reel/1234567890123456", "FacebookReel", "Facebook"),
    ("https://web.facebook.com/reel/1234567890123456", "FacebookReel", "Facebook"),
    ("https://www.facebook.com/watch?v=1234567890123456", "Facebook", "Facebook"),
    ("https://www.facebook.com/watch/?v=1234567890123456", "Facebook", "Facebook"),
    ("https://m.facebook.com/watch/?v=1234567890123456", "Facebook", "Facebook"),
    (
        "https://www.facebook.com/someuser/videos/1234567890123456/",
        "Facebook",
        "Facebook",
    ),
    # X / Twitter
    ("https://x.com/someuser/status/1234567890123456789", "Twitter", "X (Twitter)"),
    (
        "https://twitter.com/someuser/status/1234567890123456789",
        "Twitter",
        "X (Twitter)",
    ),
    (
        "https://mobile.twitter.com/someuser/status/1234567890123456789",
        "Twitter",
        "X (Twitter)",
    ),
    ("https://x.com/i/status/1234567890123456789", "Twitter", "X (Twitter)"),
    (
        "https://fxtwitter.com/someuser/status/1234567890123456789",
        "Twitter",
        "X (Twitter)",
    ),
    ("https://t.co/AbCdEf1234", "TwitterShortener", "X (Twitter)"),
    # Reddit
    (
        "https://www.reddit.com/r/videos/comments/1abcde/some_title/",
        "Reddit",
        "Reddit",
    ),
    ("https://old.reddit.com/r/videos/comments/1abcde/some_title/", "Reddit", "Reddit"),
    ("https://redd.it/1abcde", "Reddit", "Reddit"),
    ("https://www.reddit.com/gallery/1abcde", "Reddit", "Reddit"),
    # Twitch
    ("https://clips.twitch.tv/FunnyClipSlug-AbCdEf123", "TwitchClips", "Twitch"),
    (
        "https://www.twitch.tv/someuser/clip/FunnyClipSlug-AbCdEf123",
        "TwitchClips",
        "Twitch",
    ),
    ("https://www.twitch.tv/videos/1234567890", "TwitchVod", "Twitch"),
    ("https://m.twitch.tv/videos/1234567890", "TwitchVod", "Twitch"),
    # Others
    (
        "https://bsky.app/profile/someone.bsky.social/post/3kabc123def45",
        "Bluesky",
        "Bluesky",
    ),
    ("https://www.pinterest.com/pin/123456789012345678/", "Pinterest", "Pinterest"),
    ("https://it.pinterest.com/pin/123456789012345678/", "Pinterest", "Pinterest"),
    ("https://soundcloud.com/someartist/some-track", "Soundcloud", "SoundCloud"),
    ("https://m.soundcloud.com/someartist/some-track", "Soundcloud", "SoundCloud"),
    ("https://streamable.com/abc123", "Streamable", "Streamable"),
    ("https://www.dailymotion.com/video/x8abcde", "Dailymotion", "Dailymotion"),
    ("https://dai.ly/x8abcde", "Dailymotion", "Dailymotion"),
    ("https://vimeo.com/123456789", "Vimeo", "Vimeo"),
    ("https://vimeo.com/123456789/abcdef1234", "Vimeo", "Vimeo"),
    ("https://player.vimeo.com/video/123456789", "Vimeo", "Vimeo"),
    ("https://imgur.com/AbCdEf1", "Imgur", "Imgur"),
    ("https://imgur.com/a/AbCdEf1", "ImgurAlbum", "Imgur"),
    ("https://9gag.com/gag/aAbCdEf", "NineGag", "9GAG"),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "BiliBili", "Bilibili"),
    ("https://m.bilibili.com/video/BV1xx411c7mD", "BiliBili", "Bilibili"),
    (
        "https://kick.com/someuser/videos/0a1b2c3d-1234-5678-9abc-def012345678",
        "KickVOD",
        "Kick",
    ),
    (
        "https://kick.com/someuser/clips/clip_01ABCDEFGHJKMNPQRSTVWXYZ01",
        "KickClip",
        "Kick",
    ),
    ("https://rumble.com/v4abcde-some-title.html", "Rumble", "Rumble"),
    ("https://vk.com/video-12345678_456239017", "VK", "VK"),
    ("https://vkvideo.ru/video-12345678_456239017", "VK", "VK"),
    ("https://vk.com/wall-12345678_1234", "VKWallPost", "VK"),
    ("https://someblog.tumblr.com/post/123456789012/some-slug", "Tumblr", "Tumblr"),
    (
        "https://www.snapchat.com/spotlight/W7_EDlXWTBiXAEEniNoMPwAAYYWtidGhudGZpAX1TKn0JAX1TKnXJAAAAAA",
        "SnapchatSpotlight",
        "Snapchat",
    ),
    (
        "https://www.linkedin.com/posts/someone_some-title-activity-7123456789012345678-AbCd",
        "LinkedIn",
        "LinkedIn",
    ),
    ("https://www.loom.com/share/0123456789abcdef0123456789abcdef", "Loom", "Loom"),
    ("https://www.ted.com/talks/some_speaker_some_talk", "TedTalk", "TED"),
    ("https://www.mixcloud.com/someuser/some-mix/", "Mixcloud", "Mixcloud"),
    ("https://someartist.bandcamp.com/track/some-track", "Bandcamp", "Bandcamp"),
    (
        "https://www.raiplay.it/video/2024/01/some-title-0123abcd-4567-89ef-0123-456789abcdef.html",
        "RaiPlay",
        "RaiPlay",
    ),
    (
        "https://mediasetinfinity.mediaset.it/video/someshow/some-title_F311234567890102",
        "Mediaset",
        "Mediaset Infinity",
    ),
    ("https://odysee.com/@someone:a/some-video:b", "LBRY", "Odysee"),
    ("https://archive.org/details/some_item", "ArchiveOrg", "Internet Archive"),
    (
        "https://truthsocial.com/@someone/posts/112345678901234567",
        "Truth",
        "Truth Social",
    ),
]


@pytest.mark.parametrize(("url", "key", "name"), SUPPORTED)
def test_match_provider_supported(url, key, name):
    match = providers.match_provider(url)
    assert match is not None, url
    assert (match.key, match.name) == (key, name)
    assert match.normalized_url == providers.normalize_url(url)
    assert providers.match_provider(match.url) == match  # the canonical URL is stable


def test_match_provider_cleans_the_url_for_yt_dlp():
    match = providers.match_provider(
        f"https://www.youtube.com/watch?v={YT_ID}&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&index=2&si=x"
    )
    assert match == ProviderMatch(
        key="Youtube",
        name="YouTube",
        url=f"https://www.youtube.com/watch?v={YT_ID}",
        normalized_url=f"https://youtube.com/watch?v={YT_ID}",
    )
    match = providers.match_provider(f"{YT}?si=xyz&t=10")
    assert match.url == f"{YT}?t=10"
    match = providers.match_provider(
        "https://www.tiktok.com/@user/video/7234567890123456789?is_from_webapp=1&sender_device=pc"
    )
    assert match.url == "https://www.tiktok.com/@user/video/7234567890123456789"
    match = providers.match_provider(
        "(https://www.instagram.com/reel/C9abcDEF123/?igsh=MW)."
    )
    assert match.url == "https://www.instagram.com/reel/C9abcDEF123/"
    match = providers.match_provider(
        "https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D"
        f"{YT_ID}&h=AT0abc"
    )
    assert match.url == f"https://www.youtube.com/watch?v={YT_ID}"


REFUSED = [
    # No extractor in the installed yt-dlp (Threads has none; generic pages).
    ("https://www.threads.net/@someuser/post/C9abcDEF123", Rejection.UNSUPPORTED),
    ("https://www.threads.com/@someuser/post/C9abcDEF123", Rejection.UNSUPPORTED),
    ("https://example.com/video/12345", Rejection.UNSUPPORTED),
    ("https://github.com/yt-dlp/yt-dlp", Rejection.UNSUPPORTED),
    ("https://www.corriere.it/cronache/notizia-abc123.shtml", Rejection.UNSUPPORTED),
    ("https://www.facebook.com/groups/12345/", Rejection.UNSUPPORTED),
    ("https://x.com/someuser", Rejection.UNSUPPORTED),
    ("https://m.tiktok.com/v/7234567890123456789.html", Rejection.UNSUPPORTED),
    ("https://www.tiktok.com/@user/photo/7234567890123456789", Rejection.UNSUPPORTED),
    # Known DRM sites are answered by an error-only extractor.
    ("https://open.spotify.com/episode/0AbCdEfGhIjKlMnOpQrStU", Rejection.UNSUPPORTED),
    # Collections (profiles, channels, playlists, albums, feeds).
    ("https://www.youtube.com/@MrBeast", Rejection.COLLECTION),
    ("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", Rejection.COLLECTION),
    (
        "https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
        Rejection.COLLECTION,
    ),
    ("https://www.youtube.com/", Rejection.COLLECTION),
    ("https://www.tiktok.com/@user", Rejection.COLLECTION),
    ("https://soundcloud.com/someartist/sets/some-album", Rejection.COLLECTION),
    ("https://soundcloud.com/someartist", Rejection.COLLECTION),
    ("https://vimeo.com/someuser", Rejection.COLLECTION),
    ("https://www.dailymotion.com/someuser", Rejection.COLLECTION),
    (
        "https://www.instagram.com/stories/someuser/3412345678901234567/",
        Rejection.COLLECTION,
    ),
    ("https://www.pinterest.com/someuser/some-board/", Rejection.COLLECTION),
    ("https://someartist.bandcamp.com/album/some-album", Rejection.COLLECTION),
    ("https://www.twitch.tv/someuser/videos", Rejection.COLLECTION),
    # yt-dlp marks the extractor as broken.
    ("https://www.instagram.com/someuser/", Rejection.BROKEN),
    # Truncated id: yt-dlp would only print an error.
    ("https://www.youtube.com/watch?v=abc", Rejection.INVALID),
    # Telegram's own links.
    ("https://t.me/somechannel/123", Rejection.TELEGRAM),
    # Not public web URLs.
    (f"https://192.168.1.10/watch?v={YT_ID}", Rejection.INVALID),
    (f"https://[::1]/watch?v={YT_ID}", Rejection.INVALID),
    ("http://127.0.0.1/", Rejection.INVALID),
    ("https://127.1/x", Rejection.INVALID),
    (f"https://localhost/watch?v={YT_ID}", Rejection.INVALID),
    (f"https://foo.localhost/watch?v={YT_ID}", Rejection.INVALID),
    ("https://intranet/watch", Rejection.INVALID),
    ("https://nas.local/video.mp4", Rejection.INVALID),
    (f"https://user:pass@www.youtube.com/watch?v={YT_ID}", Rejection.INVALID),
    (f"https://www.youtube.com@evil.com/watch?v={YT_ID}", Rejection.INVALID),
    (f"https://www.youtube.com:8443/watch?v={YT_ID}", Rejection.INVALID),
    ("ftp://example.com/video.mp4", Rejection.INVALID),
    ("not a url", Rejection.INVALID),
]


@pytest.mark.parametrize(("url", "reason"), REFUSED)
def test_check_link_refusals(url, reason):
    result = providers.check_link(url)
    assert result.match is None
    assert result.reason is reason, result
    assert providers.match_provider(url) is None


def test_check_link_reports_the_claiming_extractor():
    assert providers.check_link("https://www.youtube.com/@MrBeast").extractor_key == (
        "YoutubeTab"
    )
    assert providers.check_link(
        "https://open.spotify.com/episode/0AbCdEfGhIjKlMnOpQrStU"
    ).extractor_key == ("KnownDRM")
    result = providers.check_link(YT)
    assert result.ok
    assert result.extractor_key == "Youtube"
    assert result.reason is None


SHORT_LINKS = [
    "https://fb.watch/abcDEF123/",
    "https://fb.me/abcdef",
    "https://www.facebook.com/share/r/1AbCdEfGh/",
    "https://www.facebook.com/share/v/1AbCdEfGh/",
    "https://m.facebook.com/share/p/1AbCdEfGh/",
    "https://www.instagram.com/share/reel/BAabcdef12/",
    "https://www.instagram.com/share/BAabcdef12",
    "https://v.redd.it/abc123xyz",
    "https://www.reddit.com/r/videos/s/AbCdEf1234",
    "https://www.reddit.com/video/abc123xyz",
    "https://pin.it/AbCdEf123",
    "https://on.soundcloud.com/AbCdEf123",
    "https://b23.tv/AbCdEf1",
    "https://t.snapchat.com/AbCdEf12",
    "https://lnkd.in/AbCdEf12",
]


@pytest.mark.parametrize("url", SHORT_LINKS)
def test_share_links_are_unsupported_offline_but_resolvable(url):
    assert providers.is_short_link(url)
    result = providers.check_link(url)
    assert result.reason is Rejection.UNSUPPORTED
    assert result.resolvable is True


@pytest.mark.parametrize(
    "url",
    [
        "https://fb.watch/",
        "https://www.facebook.com/watch?v=1234567890123456",
        "https://www.reddit.com/r/videos/",
        YT,
        "https://example.com/share/x",
        "not a url",
    ],
)
def test_not_short_links(url):
    assert providers.is_short_link(url) is False
    assert providers.check_link(url).resolvable is False


def test_link_check_dataclasses_are_frozen():
    match = providers.match_provider(YT)
    with pytest.raises(AttributeError):
        match.url = "x"
    check = LinkCheck(url="", match=None, reason=Rejection.INVALID)
    assert not check.ok
    assert str(Rejection.COLLECTION) == "collection"


# ---------------------------------------------------------------------------
# ALLOWED_PROVIDERS / BLOCKED_PROVIDERS / ALLOW_COLLECTION_LINKS
# ---------------------------------------------------------------------------
def test_allowed_providers_by_family(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ("youtube", "tiktok"))
    assert providers.match_provider(YT).key == "Youtube"
    assert providers.match_provider("https://vm.tiktok.com/ZMabcdef/").key == "TikTokVM"
    assert providers.check_link("https://vimeo.com/123456789").reason is (
        Rejection.BLOCKED
    )


def test_allowed_providers_by_domain_suffix(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ("vimeo.com",))
    assert providers.match_provider("https://player.vimeo.com/video/123456789")
    assert providers.match_provider(YT) is None


def test_allowed_providers_by_key_and_ie_name(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ("TwitchClips", "vm.tiktok"))
    assert providers.match_provider("https://clips.twitch.tv/FunnyClipSlug-AbCdEf123")
    assert providers.match_provider("https://vm.tiktok.com/ZMabcdef/")
    assert providers.match_provider("https://www.twitch.tv/videos/1234567890") is None


@pytest.mark.parametrize("rule", ["twitter", "x", "TWITTER", "x.com"])
def test_blocked_providers(monkeypatch, rule):
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", (rule,))
    result = providers.check_link("https://x.com/someuser/status/1234567890123456789")
    assert result.reason is Rejection.BLOCKED
    assert providers.match_provider(YT) is not None


def test_blocked_provider_key_only_blocks_that_extractor(monkeypatch):
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ("TikTokVM",))
    assert providers.match_provider("https://vm.tiktok.com/ZMabcdef/") is None
    assert providers.match_provider(
        "https://www.tiktok.com/@user/video/7234567890123456789"
    )


def test_blocked_wildcard_domain_and_precedence(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ("reddit",))
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ("*.reddit.com",))
    url = "https://www.reddit.com/r/videos/comments/1abcde/some_title/"
    assert providers.check_link(url).reason is Rejection.BLOCKED


def test_provider_lists_accept_comma_separated_strings(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", " youtube , vimeo ")
    assert providers.match_provider(YT)
    assert providers.match_provider("https://vimeo.com/123456789")
    assert (
        providers.match_provider("https://x.com/u/status/1234567890123456789") is None
    )


def test_allow_collection_links(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_COLLECTION_LINKS", True)
    match = providers.match_provider("https://www.youtube.com/@MrBeast")
    assert match is not None
    assert match.key == "YoutubeTab"
    # Broken extractors stay refused.
    assert providers.match_provider("https://www.instagram.com/someuser/") is None


# ---------------------------------------------------------------------------
# Display names and supported sites
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "name"),
    [
        ("Youtube", "YouTube"),
        ("YoutubeTab", "YouTube"),
        ("TikTokVM", "TikTok"),
        ("TikTok", "TikTok"),
        ("Douyin", "Douyin"),
        ("Instagram", "Instagram"),
        ("FacebookReel", "Facebook"),
        ("Twitter", "X (Twitter)"),
        ("Reddit", "Reddit"),
        ("Vimeo", "Vimeo"),
        ("TwitchClips", "Twitch"),
        ("Dailymotion", "Dailymotion"),
        ("Bluesky", "Bluesky"),
        ("Pinterest", "Pinterest"),
        ("Soundcloud", "SoundCloud"),
        ("Bandcamp", "Bandcamp"),
        ("Streamable", "Streamable"),
        ("Imgur", "Imgur"),
        ("NineGag", "9GAG"),
        ("BiliBili", "Bilibili"),
        ("KickVOD", "Kick"),
        ("Kicker", "Kicker"),
        ("Rumble", "Rumble"),
        ("VK", "VK"),
        ("Tumblr", "Tumblr"),
        ("SnapchatSpotlight", "Snapchat"),
        ("LinkedIn", "LinkedIn"),
        ("Loom", "Loom"),
        ("TedTalk", "TED"),
        ("Mixcloud", "Mixcloud"),
        ("LBRY", "Odysee"),
        ("YoutubeWebArchive", "Internet Archive"),
        ("ACast", "Acast"),
        ("youtube", "YouTube"),
        ("NotAnExtractor", "NotAnExtractor"),
        ("", ""),
    ],
)
def test_provider_display_name(key, name):
    assert providers.provider_display_name(key) == name


def test_supported_sites_text_default():
    text = providers.supported_sites_text()
    assert text.startswith(
        "YouTube, TikTok, Instagram, Facebook, X (Twitter), Reddit, Vimeo, Twitch e "
    )
    assert text.endswith(" altri siti")
    count = int(text.split("oltre ")[1].split()[0])
    assert count >= 500
    assert count % 100 == 0


def test_supported_sites_text_honours_blocked(monkeypatch):
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ("tiktok.com", "x"))
    text = providers.supported_sites_text()
    assert "TikTok" not in text
    assert "X (Twitter)" not in text
    assert text.startswith(
        "YouTube, Instagram, Facebook, Reddit, Vimeo, Twitch e oltre"
    )


def test_supported_sites_text_honours_allowed(monkeypatch):
    monkeypatch.setattr(
        config, "ALLOWED_PROVIDERS", ("youtube", "tiktok.com", "Reddit", "foo.example")
    )
    assert providers.supported_sites_text() == "YouTube, TikTok, Reddit e foo.example"
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ("vimeo",))
    assert providers.supported_sites_text() == "Vimeo"
    # Families without a curated name, single extractors, duplicates, aliases.
    monkeypatch.setattr(
        config, "ALLOWED_PROVIDERS", ("acast", "Douyin", "x", "twitter", "TwitchClips")
    )
    assert providers.supported_sites_text() == "Acast, Douyin, X (Twitter) e Twitch"


def test_supported_sites_text_ignores_extractor_level_blocks(monkeypatch):
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ("TikTokVM",))
    assert "TikTok" in providers.supported_sites_text()
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ("youtube", "reddit"))
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ("reddit.com",))
    assert providers.supported_sites_text() == "YouTube"
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ("youtube", "reddit"))
    assert providers.supported_sites_text() == "nessun sito"


# ---------------------------------------------------------------------------
# Registry, warm-up, thread safety
# ---------------------------------------------------------------------------
def test_registry_contents_and_stats():
    registry = providers._registry()
    assert "Generic" not in registry.by_key
    assert len(registry.extractors) > 1000
    assert registry.by_key["Youtube"].family == "youtube"
    assert registry.by_key["KnownDRM"].unsupported
    assert registry.by_key["YoutubeTab"].collection
    assert not registry.by_key["VKWallPost"].collection
    assert not registry.by_key["InstagramUser"].working
    info = providers.stats()
    assert info["extractors"] == len(registry.extractors)
    assert info["working_extractors"] < info["extractors"]
    assert info["sites"] > 500
    assert info["compile_seconds"] is not None
    assert {"hits", "misses", "size"} <= set(info["lookup_cache"])


def test_warm_up_is_idempotent():
    before = providers._registry()
    providers.warm_up()
    providers.warm_up()
    assert providers._registry() is before


def test_registry_is_built_once_under_concurrency(monkeypatch):
    calls = []
    real_build = providers._build_registry

    def counting_build():
        calls.append(threading.get_ident())
        return real_build()

    monkeypatch.setattr(providers, "_registry_value", None)
    monkeypatch.setattr(providers, "_build_registry", counting_build)
    barrier = threading.Barrier(8)
    results = []

    def worker():
        barrier.wait()
        results.append(providers._registry())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert len(results) == 8
    assert all(result is results[0] for result in results)


def test_concurrent_matching_is_consistent():
    urls = [url for url, _key, _name in SUPPORTED[:20]]
    expected = {url: providers.match_provider(url) for url in urls}
    providers._reset_for_tests()
    errors = []

    def worker():
        for url in urls:
            if providers.match_provider(url) != expected[url]:
                errors.append(url)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_lookups_are_cached():
    providers.match_provider("https://vimeo.com/123456789")
    before = providers._lookup.cache_info().hits
    providers.match_provider("https://vimeo.com/123456789")
    assert providers._lookup.cache_info().hits == before + 1


# ---------------------------------------------------------------------------
# Share-link resolution (fake HTTP session, no network)
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status, location=None):
        self.status = status
        self.headers = {"Location": location} if location else {}


class FakeRequest:
    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Answers GET requests from a {url: response-or-exception} map."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeRequest(self.routes.get(url, FakeResponse(404)))


@pytest.fixture
def fake_session(monkeypatch):
    holder = {}

    def install(routes):
        session = FakeSession(routes)
        holder["session"] = session

        async def get_session():
            return session

        monkeypatch.setattr(http_client, "get_session", get_session)
        return session

    return install


def test_resolve_fb_watch(fake_session):
    target = "https://www.facebook.com/watch/?v=1234567890123456&ref=sharing"
    session = fake_session({"https://fb.watch/abcDEF123/": FakeResponse(302, target)})
    match = asyncio.run(providers.match_provider_async("https://fb.watch/abcDEF123/"))
    assert match is not None
    assert match.key == "Facebook"
    assert match.url == "https://www.facebook.com/watch/?v=1234567890123456"
    assert match.normalized_url == "https://facebook.com/watch?v=1234567890123456"
    url, kwargs = session.calls[0]
    assert url == "https://fb.watch/abcDEF123/"
    assert kwargs["allow_redirects"] is False
    assert isinstance(kwargs["timeout"], aiohttp.ClientTimeout)
    assert kwargs["timeout"].total == 5.0
    assert "Mozilla" in kwargs["headers"]["User-Agent"]
    assert kwargs["proxy"] is None


def test_resolve_follows_provider_hops(fake_session):
    session = fake_session(
        {
            "https://pin.it/AbCdEf123": FakeResponse(
                301, "https://api.pinterest.com/url_shortener/AbCdEf123/redirect/"
            ),
            "https://api.pinterest.com/url_shortener/AbCdEf123/redirect/": FakeResponse(
                302,
                "https://www.pinterest.com/pin/123456789012345678/sent/?invite_code=x",
            ),
        }
    )
    check = asyncio.run(providers.check_link_async("pin.it/AbCdEf123"))
    assert check.match is not None
    assert check.match.key == "Pinterest"
    assert check.url == "https://www.pinterest.com/pin/123456789012345678/sent/"
    assert [url for url, _ in session.calls] == [
        "https://pin.it/AbCdEf123",
        "https://api.pinterest.com/url_shortener/AbCdEf123/redirect/",
    ]


def test_resolve_relative_location(fake_session):
    fake_session(
        {
            "https://www.reddit.com/r/videos/s/AbCdEf1234": FakeResponse(
                301, "/r/videos/comments/1abcde/some_title/?share_id=x"
            )
        }
    )
    match = asyncio.run(
        providers.match_provider_async("https://www.reddit.com/r/videos/s/AbCdEf1234")
    )
    assert match.key == "Reddit"
    assert match.normalized_url == "https://reddit.com/comments/1abcde"


def test_resolve_upgrades_to_https(fake_session):
    session = fake_session(
        {
            "https://v.redd.it/abc123xyz": FakeResponse(
                301, "https://www.reddit.com/r/videos/comments/1abcde/x/"
            )
        }
    )
    resolved = asyncio.run(providers.resolve_short_link("http://v.redd.it/abc123xyz"))
    assert resolved == "https://www.reddit.com/r/videos/comments/1abcde/x/"
    assert session.calls[0][0] == "https://v.redd.it/abc123xyz"


def test_resolve_stops_outside_the_provider_domains(fake_session):
    session = fake_session(
        {"https://lnkd.in/AbCdEf12": FakeResponse(302, "https://evil.example.com/x")}
    )
    assert asyncio.run(providers.resolve_short_link("https://lnkd.in/AbCdEf12")) is None
    assert len(session.calls) == 1


def test_resolve_accepts_supported_target_without_fetching_it(fake_session):
    session = fake_session({"https://lnkd.in/AbCdEf12": FakeResponse(302, YT)})
    assert asyncio.run(providers.resolve_short_link("https://lnkd.in/AbCdEf12")) == YT
    assert len(session.calls) == 1


def test_resolve_gives_up_after_three_redirects(fake_session):
    session = fake_session(
        {
            "https://v.redd.it/a": FakeResponse(301, "https://www.reddit.com/video/b"),
            "https://www.reddit.com/video/b": FakeResponse(
                301, "https://www.reddit.com/video/c"
            ),
            "https://www.reddit.com/video/c": FakeResponse(
                301, "https://www.reddit.com/video/d"
            ),
            "https://www.reddit.com/video/d": FakeResponse(
                301, "https://www.reddit.com/r/videos/comments/1abcde/"
            ),
        }
    )
    assert asyncio.run(providers.resolve_short_link("https://v.redd.it/a")) is None
    assert len(session.calls) == 3


@pytest.mark.parametrize(
    "outcome",
    [
        FakeResponse(200),
        FakeResponse(404),
        FakeResponse(302),  # redirect without Location
        FakeResponse(302, "javascript:alert(1)"),
        aiohttp.ClientConnectionError("boom"),
        TimeoutError(),
    ],
)
def test_resolve_failures_return_none(fake_session, outcome):
    fake_session({"https://fb.watch/abc/": outcome})
    assert asyncio.run(providers.resolve_short_link("https://fb.watch/abc/")) is None
    check = asyncio.run(providers.check_link_async("https://fb.watch/abc/"))
    assert check.reason is Rejection.UNSUPPORTED
    assert check.resolvable


def test_resolve_results_are_cached(fake_session):
    session = fake_session(
        {"https://fb.watch/abc/": FakeResponse(302, "https://www.facebook.com/reel/1")}
    )

    async def twice():
        first = await providers.resolve_short_link("https://fb.watch/abc/")
        second = await providers.resolve_short_link("https://fb.watch/abc/")
        return first, second

    assert asyncio.run(twice()) == (
        "https://www.facebook.com/reel/1",
        "https://www.facebook.com/reel/1",
    )
    assert len(session.calls) == 1


def test_resolve_ignores_non_short_links(fake_session):
    session = fake_session({})
    assert asyncio.run(providers.resolve_short_link("https://example.com/x")) is None
    assert asyncio.run(providers.resolve_short_link("not a url")) is None
    assert session.calls == []


def test_check_link_async_never_hits_the_network_for_direct_links(fake_session):
    session = fake_session({})
    check = asyncio.run(providers.check_link_async(YT))
    assert check.match.key == "Youtube"
    check = asyncio.run(providers.check_link_async("https://example.com/x"))
    assert check.reason is Rejection.UNSUPPORTED
    check = asyncio.run(
        providers.check_link_async("https://fb.watch/x/", resolve=False)
    )
    assert check.reason is Rejection.UNSUPPORTED
    assert session.calls == []


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [("http://proxy:3128", "http://proxy:3128"), ("socks5://proxy:1080", None)],
)
def test_resolve_uses_http_proxy_only(fake_session, monkeypatch, proxy, expected):
    monkeypatch.setattr(config, "YTDLP_PROXY", proxy, raising=False)
    session = fake_session({})
    asyncio.run(providers.resolve_short_link("https://fb.watch/abc/"))
    assert session.calls[0][1]["proxy"] == expected
