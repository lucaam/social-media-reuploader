"""Social Media Reuploader: the Telegram bot (src.bot) and the admin GUI (src.gui)."""

import os

#: Version of the running code (bot banner, /health, worker heartbeat, GUI).
#: No version is committed: release images get it from the release tag at
#: build time (Dockerfile ``ARG VERSION`` -> ``SMR_VERSION``); anything else,
#: e.g. a source checkout, is a development build.
__version__ = os.environ.get("SMR_VERSION", "").strip() or "0.0.0-dev"
