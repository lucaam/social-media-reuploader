"""Shared aiohttp client session.

One session per event loop (a session cannot be used from another loop, e.g.
after ``asyncio.run()`` is called again in tests). The session has a default
timeout (callers pass stricter per-request timeouts), keeps no cookies and
caches DNS answers.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

# Applies when a request does not pass its own timeout.
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60, sock_connect=15)
_CONNECTION_LIMIT = 50

_session: aiohttp.ClientSession | None = None
_session_loop: asyncio.AbstractEventLoop | None = None


def _new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=DEFAULT_TIMEOUT,
        cookie_jar=aiohttp.DummyCookieJar(),
        connector=aiohttp.TCPConnector(limit=_CONNECTION_LIMIT, ttl_dns_cache=300),
    )


async def get_session() -> aiohttp.ClientSession:
    """The shared session of the running event loop (created on first use)."""
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    session = _session
    if session is not None and not session.closed and _session_loop is loop:
        return session
    if session is not None and not session.closed:
        logger.debug("Replacing an HTTP session created by another event loop")
    _session = _new_session()
    _session_loop = loop
    return _session


async def close_session() -> None:
    """Close the shared session (safe to call more than once)."""
    global _session, _session_loop
    session, loop = _session, _session_loop
    _session, _session_loop = None, None
    if session is None or session.closed:
        return
    if loop is not asyncio.get_running_loop():
        logger.debug("Not closing an HTTP session owned by another event loop")
        return
    try:
        await session.close()
    except (aiohttp.ClientError, OSError, RuntimeError):
        logger.warning("Error while closing the HTTP session", exc_info=True)
