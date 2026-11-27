"""Per-token cache of aiogram ``Bot`` instances.

The raw Bot API helpers in :mod:`src.telegram_api` do not need aiogram; this
module only serves the aiogram Dispatcher of the bot entrypoint. Bots created
here use the same API server as :mod:`src.telegram_api` (local Bot API server
when ``config.TELEGRAM_API_BASE`` is set).
"""

from __future__ import annotations

import logging

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from . import telegram_api

logger = logging.getLogger(__name__)

_bots: dict[str, Bot] = {}


def _session() -> AiohttpSession | None:
    base = telegram_api.api_base()
    if base == telegram_api.DEFAULT_API_BASE:
        return None  # aiogram default
    return AiohttpSession(api=TelegramAPIServer.from_base(base))


def set_bot(bot: Bot) -> None:
    """Register an existing Bot so :func:`get_bot` returns it for its token."""
    _bots[bot.token] = bot


def get_bot(token: str) -> Bot:
    """The cached Bot for ``token`` (created on first use)."""
    bot = _bots.get(token)
    if bot is None:
        bot = Bot(token=token, session=_session())
        _bots[token] = bot
    return bot


async def close_all_bots() -> None:
    """Close the HTTP sessions of every cached Bot."""
    bots = list(_bots.values())
    _bots.clear()
    for bot in bots:
        try:
            await bot.session.close()
        except (aiohttp.ClientError, OSError, RuntimeError):
            logger.warning("Error while closing a Bot session", exc_info=True)
