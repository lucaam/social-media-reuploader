from typing import Optional

from aiogram import Bot

_bot: Optional[Bot] = None


def set_bot(bot: Bot) -> None:
    """Register a Bot instance to be reused by telegram API helper functions."""
    global _bot
    _bot = bot


def get_bot(token: str) -> Bot:
    """Return a cached Bot instance for the given token, creating if needed."""
    global _bot
    if _bot is None:
        _bot = Bot(token=token)
    return _bot


async def close_all_bots() -> None:
    """Close the cached Bot session(s) if present."""
    global _bot
    if _bot is not None:
        try:
            await _bot.session.close()
        except Exception:
            pass
        _bot = None
