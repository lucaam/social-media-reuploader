"""Deprecated compatibility wrapper around :mod:`src.downloader`.

The worker no longer calls this module: one ``downloader.fetch_info()`` gives
both the metadata and :attr:`~src.downloader.MediaItem.direct_url` (the
progressive MP4 Telegram can fetch server-side), and ``downloader.download()``
reuses the same extraction. The yt-dlp command-line helpers are re-exported
here for code that imported them from this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import warnings
from typing import Any

from . import config
from .downloader import (
    DownloadError,
    build_download_argv,
    build_fetch_argv,
    common_args,
    fetch_info,
    format_args,
    redact,
    ytdlp_command,
)

__all__ = [
    "build_download_argv",
    "build_fetch_argv",
    "common_args",
    "extract_direct_url_and_meta",
    "format_args",
    "ytdlp_command",
]

logger = logging.getLogger(__name__)


def _make_tmpdir() -> str:
    base = getattr(config, "TMP_DIR", None) or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="ytdlp-", dir=base)


async def extract_direct_url_and_meta(
    url: str,
    timeout: int = 15,  # noqa: ASYNC109, RUF100
    ytdlp_user_agent: str | None = None,
    ytdlp_cookies: str | None = None,
    ytdlp_headers: dict[str, str] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Deprecated: ``(direct_url, meta)`` of the first item, or ``(None, None)``.

    Performs one extraction (previously two: ``-g`` then ``-j``). The
    ``ytdlp_*`` overrides are ignored: yt-dlp options come from ``src.config``.
    """
    warnings.warn(
        "ytdlp.extract_direct_url_and_meta() is deprecated; use downloader.fetch_info()",
        DeprecationWarning,
        stacklevel=2,
    )
    del ytdlp_user_agent, ytdlp_cookies, ytdlp_headers
    workdir = await asyncio.to_thread(_make_tmpdir)
    try:
        info = await fetch_info(url, workdir, timeout=float(timeout))
    except DownloadError as exc:
        logger.info(
            "direct URL lookup failed for %s: [%s] %s",
            redact(url),
            exc.code,
            exc.detail,
        )
        return None, None
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)
    item = info.items[0]
    if not item.direct_url:
        return None, None
    meta: dict[str, Any] = {
        "duration": item.duration,
        "format": item.direct_ext,
        "video_width": item.width,
        "video_height": item.height,
        "video_codec": item.vcodec,
        "audio_codec": item.acodec,
        "final_size": item.filesize,
    }
    return item.direct_url, {k: v for k, v in meta.items() if v is not None}
