# syntax=docker/dockerfile:1
#
# One image, two processes:
#   bot (default CMD):  python -m src.bot          -> /health, /metrics on $PORT (8080)
#   admin GUI:          uvicorn src.gui:app --host 0.0.0.0 --port 8081
# Both share state only through the SQLite DB in /app/data (REQUESTS_DB).
#
# The release workflow builds it with --build-arg VERSION=X.Y.Z (the release
# tag) and adds the build-specific OCI labels (revision, created, version).
# The base image is pinned by digest: Renovate proposes its updates as
# fix(deps) pull requests, so a merged base image update ships as a patch
# release.
FROM python:3.14-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b

LABEL org.opencontainers.image.title="social-media-reuploader" \
      org.opencontainers.image.description="Telegram bot that re-posts the media behind social-network links (every site supported by yt-dlp: YouTube, TikTok, Instagram, Facebook, X, Reddit, Vimeo, Twitch and many more) back into the chat, with an admin GUI" \
      org.opencontainers.image.url="https://github.com/lucaam/social-media-reuploader" \
      org.opencontainers.image.source="https://github.com/lucaam/social-media-reuploader" \
      org.opencontainers.image.documentation="https://lucaam.github.io/social-media-reuploader/" \
      org.opencontainers.image.licenses="MIT"

# TMP_DIR holds the per-request work directories (downloads, ffmpeg outputs).
# The deno (YouTube challenge solver) and yt-dlp caches live under /tmp too,
# so the container also runs with a read-only root filesystem (the Helm chart
# mounts an emptyDir on /tmp).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Etc/UTC \
    TMP_DIR=/tmp/telegram_downloader \
    DENO_DIR=/tmp/deno \
    DENO_NO_UPDATE_CHECK=1 \
    XDG_CACHE_HOME=/tmp/.cache

WORKDIR /app

# ffmpeg/ffprobe: merging, probing and Telegram-compatible conversion.
# tini: PID 1 that forwards signals and reaps yt-dlp/ffmpeg/deno children.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp comes with its extras (see requirements.txt): curl_cffi
# (impersonation), yt-dlp-ejs and the deno wheel, whose binary is installed in
# /usr/local/bin next to python, where yt-dlp (run as `python -m yt_dlp`)
# looks for it first. The build fails if any runtime piece is missing.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && deno --version \
    && python -c "import curl_cffi, yt_dlp, yt_dlp_ejs; print('yt-dlp', yt_dlp.version.__version__)" \
    && ffmpeg -hide_banner -version > /dev/null \
    && ffprobe -hide_banner -version > /dev/null

# Unprivileged runtime user (uid/gid 1000, as in the Helm chart). The code
# stays owned by root, so the app cannot rewrite itself; only the data and
# work directories are writable.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data "$TMP_DIR" \
    && chown appuser:appuser /app/data "$TMP_DIR"

COPY src/ ./src/
RUN python -m compileall -q src

# The release version (the tag without "v"; src.__version__ reads
# SMR_VERSION). Declared after the last RUN so that a new version reuses the
# build cache of every layer above.
ARG VERSION=0.0.0-dev
ENV SMR_VERSION=$VERSION
LABEL org.opencontainers.image.version=$VERSION

USER 1000:1000

# 8080: bot health/metrics (and the webhook endpoint in webhook mode)
# 8081: admin GUI
EXPOSE 8080 8081

# Probes the bot on $PORT (default 8080). A GUI container must set PORT to its
# uvicorn port (docker-compose.yml overrides this check for the GUI).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('PORT') or '8080'; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "src.bot"]
