"""Edge cases of the legacy ``src.link_utils`` facade."""

import pytest

from src.link_utils import find_links, is_supported


def test_find_links_empty():
    assert find_links("") == []
    assert find_links(None) == []


def test_find_links_multiple_and_duplicates():
    text = (
        "First: https://youtu.be/abcdefghijk and then "
        "https://www.tiktok.com/@u/video/7234567890123456789. "
        "Duplicate: https://youtu.be/abcdefghijk, again "
        "https://www.youtube.com/watch?v=abcdefghijk&si=share"
    )
    # Text order, each link once (duplicates detected on the normalized URL).
    assert find_links(text) == [
        "https://youtu.be/abcdefghijk",
        "https://www.tiktok.com/@u/video/7234567890123456789",
    ]


def test_find_links_trailing_punctuation_and_parentheses():
    text = (
        "See (https://youtu.be/xyzxyzxyz12), and end. "
        "Also: https://www.instagram.com/p/ABC123."
    )
    assert find_links(text) == [
        "https://youtu.be/xyzxyzxyz12",
        "https://www.instagram.com/p/ABC123",
    ]


@pytest.mark.parametrize("suffix", [".", ",", ")", "!", "?", '"', "'", ";", ":"])
def test_find_links_strips_each_trailing_character(suffix):
    assert find_links(f"guarda https://youtu.be/dQw4w9WgXcQ{suffix}") == [
        "https://youtu.be/dQw4w9WgXcQ"
    ]


def test_find_links_case_insensitive_scheme_and_host():
    assert find_links("Check this: HTTPS://YOUTU.BE/UPPERCASEID") == [
        "https://youtu.be/UPPERCASEID"
    ]


def test_is_supported_true_false():
    assert is_supported("https://youtu.be/dQw4w9WgXcQ") is True
    assert is_supported("https://vimeo.com/123456789") is True
    assert is_supported("https://example.com/foo") is False
    # YouTube ids are 11 characters: "youtu.be/abc" is not a video link.
    assert is_supported("https://youtu.be/abc") is False
    assert is_supported("") is False
    assert is_supported(None) is False
