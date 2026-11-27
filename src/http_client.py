"""Shared aiohttp client session.

One session per event loop (a session cannot be used from another loop, e.g.
after ``asyncio.run()`` is called again in tests). The session has a default
timeout, keeps no cookies and caches DNS answers. Requests that need another
total timeout pass :func:`request_timeout` (a bare
``ClientTimeout(total=...)`` replaces the session default entirely and drops
its connect timeout).
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


def request_timeout(total: float) -> aiohttp.ClientTimeout:
    """Per-request timeout of ``total`` seconds that keeps the connect timeout.

    Without ``sock_connect`` a hung TCP connect only ends when ``total``
    expires, as a plain ``TimeoutError`` (the request may have been sent)
    instead of ``aiohttp.ConnectionTimeoutError`` (it was not).
    """
    connect = DEFAULT_TIMEOUT.sock_connect
    return aiohttp.ClientTimeout(
        total=total, sock_connect=total if connect is None else min(total, connect)
    )


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
