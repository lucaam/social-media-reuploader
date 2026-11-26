"""Link detection through the legacy ``src.link_utils`` facade."""

from src.link_utils import find_links


def test_find_youtube_short():
    text = "Check this out: https://youtu.be/dQw4w9WgXcQ"
    assert find_links(text) == ["https://youtu.be/dQw4w9WgXcQ"]


def test_find_youtube_mobile_music_live_and_shorts():
    text = (
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ "
        "https://music.youtube.com/watch?v=aaaaaaaaaaa "
        "https://www.youtube.com/live/bbbbbbbbbbb "
        "https://youtube.com/shorts/ccccccccccc?si=abc"
    )
    assert find_links(text) == [
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=aaaaaaaaaaa",
        "https://www.youtube.com/live/bbbbbbbbbbb",
        "https://youtube.com/shorts/ccccccccccc?si=abc",
    ]


def test_find_tiktok():
    text = "TikTok: https://www.tiktok.com/@user/video/7234567890123456789"
    assert find_links(text) == [
        "https://www.tiktok.com/@user/video/7234567890123456789"
    ]


def test_find_tiktok_share_links():
    text = "https://vm.tiktok.com/ZMabcdef/ e https://vt.tiktok.com/ZSabcdef/"
    assert find_links(text) == [
        "https://vm.tiktok.com/ZMabcdef/",
        "https://vt.tiktok.com/ZSabcdef/",
    ]


def test_find_instagram_reel_and_post():
    text = (
        "Instagram reel: https://www.instagram.com/reel/CR3l/ "
        "and a page https://instagram.com/p/ABC123"
    )
    assert find_links(text) == [
        "https://www.instagram.com/reel/CR3l/",
        "https://instagram.com/p/ABC123",
    ]


def test_instagram_profiles_are_not_media_links():
    assert find_links("profilo https://www.instagram.com/someuser/") == []


def test_find_facebook_watch():
    # fb.watch share links need a redirect lookup (providers.match_provider_async):
    # offline only the canonical watch URL is recognised.
    text = (
        "Check this video: https://fb.watch/abcd1234/ "
        "or https://www.facebook.com/watch/?v=12345"
    )
    assert find_links(text) == ["https://www.facebook.com/watch/?v=12345"]


def test_find_other_providers():
    text = (
        "https://x.com/someuser/status/1234567890123456789 "
        "https://www.reddit.com/r/videos/comments/1abcde/some_title/ "
        "https://vimeo.com/123456789 "
        "https://clips.twitch.tv/FunnyClipSlug-AbCdEf123 "
        "https://www.dailymotion.com/video/x8abcde"
    )
    assert find_links(text) == text.split()


def test_unsupported_and_telegram_links_are_ignored():
    text = "https://example.com/video https://t.me/somechannel/1 https://github.com/x/y"
    assert find_links(text) == []
