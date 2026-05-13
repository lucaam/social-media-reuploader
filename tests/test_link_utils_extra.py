from src.link_utils import find_links, is_supported


def test_find_links_empty():
    assert find_links("") == []


def test_find_links_multiple_and_duplicates():
    text = (
        "First: https://youtu.be/abc123 and then https://www.tiktok.com/@u/video/1. "
        "Duplicate: https://youtu.be/abc123"
    )
    links = find_links(text)
    assert any("youtu.be/abc123" in link for link in links)
    assert any("tiktok.com" in link for link in links)
    # duplicates are allowed but at least two matches should be found
    assert len([link for link in links if "youtu.be/abc123" in link]) >= 2


def test_find_links_trailing_punctuation_and_parentheses():
    text = "See (https://youtu.be/xyz), and end. Also: https://www.instagram.com/p/ABC123." 
    links = find_links(text)
    # ensure the URL substring is present even if punctuation is attached
    assert any("youtu.be/xyz" in link for link in links)
    assert any("instagram.com/p/ABC123" in link for link in links)


def test_find_links_case_insensitive_scheme():
    text = "Check this: HTTPS://YOUTU.BE/UPPER"
    links = find_links(text)
    # match should be case-insensitive; normalize to lower for the assertion
    assert any("youtu" in link.lower() for link in links)


def test_is_supported_true_false():
    assert is_supported("https://youtu.be/abc") is True
    assert is_supported("https://example.com/foo") is False
