import re
from typing import List

PATTERNS = {
    "youtube": re.compile(
        r"(https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)[^\s]+)",
        re.IGNORECASE,
    ),
    "tiktok": re.compile(
        r"(https?://(?:(?:www\.)?tiktok\.com|vm\.tiktok\.com|m\.tiktok\.com|v\.tiktok\.com)/[^\s]+)",
        re.IGNORECASE,
    ),
    "instagram": re.compile(
        r"(https?://(?:www\.)?instagram\.com/[^\s]+)", re.IGNORECASE
    ),
    "facebook": re.compile(
        r"(https?://(?:www\.)?(?:facebook\.com|fb\.watch)/[^\s]+)", re.IGNORECASE
    ),
}


def find_links(text: str) -> List[str]:
    if not text:
        return []
    links = []
    for pattern in PATTERNS.values():
        for m in pattern.finditer(text):
            links.append(m.group(1))
    return links


def is_supported(url: str) -> bool:
    """Return True if the given url matches one of the supported site patterns."""
    if not url:
        return False
    for pattern in PATTERNS.values():
        if pattern.search(url):
            return True
    return False
