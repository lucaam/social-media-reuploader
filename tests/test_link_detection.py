from src.link_utils import find_links


def test_find_youtube_short():
    text = "Check this out: https://youtu.be/dQw4w9WgXcQ"
    links = find_links(text)
    assert any("youtu" in link for link in links)


def test_find_tiktok():
    text = "TikTok: https://www.tiktok.com/@user/video/12345"
    links = find_links(text)
    assert any("tiktok.com" in link for link in links)


def test_find_instagram_reel():
    text = "Instagram reel: https://www.instagram.com/reel/CR3l/ and a page https://instagram.com/p/ABC123"
    links = find_links(text)
    assert any("instagram.com" in link for link in links)


def test_find_facebook_watch():
    text = "Check this video: https://fb.watch/abcd1234/ or https://www.facebook.com/watch/?v=12345"
    links = find_links(text)
    assert any("facebook" in link or "fb.watch" in link for link in links)
