"""``python -m src``: run the Telegram bot (mode from ``config.MODE``)."""

from __future__ import annotations

from .bot import main

if __name__ == "__main__":
    raise SystemExit(main())
