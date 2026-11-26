"""Deprecated entrypoint kept for ``python -m src.main``.

It starts the bot in webhook mode through the same code path as
``python -m src`` (see :mod:`src.bot`): same handlers, same worker pool, same
/health, /ready and /metrics endpoints. Without ``WEBHOOK_URL`` the bot logs a
warning and falls back to polling.

Prefer ``MODE=webhook python -m src``; this module will be removed in a future
release.
"""

from __future__ import annotations

from . import bot


def main() -> int:
    """Run the bot in webhook mode; returns the process exit code."""
    return bot.main(mode="webhook")


if __name__ == "__main__":
    raise SystemExit(main())
