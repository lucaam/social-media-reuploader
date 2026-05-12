FROM python:3.12-slim

ARG VCS_REF=""
ARG BUILD_DATE=""
ARG VERSION="0.1.0"

LABEL org.opencontainers.image.title="social-media-reuploader" \
	org.opencontainers.image.description="Telegram bot that downloads video content from YouTube, TikTok, Instagram and Facebook and posts it back to the chat" \
	org.opencontainers.image.url="https://github.com/lucaam/social-media-reuploader" \
	org.opencontainers.image.source="https://github.com/lucaam/social-media-reuploader" \
	org.opencontainers.image.licenses="MIT" \
	org.opencontainers.image.revision="$VCS_REF" \
	org.opencontainers.image.version="$VERSION" \
	org.opencontainers.image.created="$BUILD_DATE"

WORKDIR /app

# Install minimal runtime packages (ffmpeg) and Python requirements
COPY requirements.txt ./
RUN apt-get update \
	&& apt-get install -y --no-install-recommends ffmpeg ca-certificates \
	&& rm -rf /var/lib/apt/lists/* \
	&& python -m pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY README.md ./

# Create a non-root user and set ownership
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
	&& chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# Healthcheck using the application's /health endpoint
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys
try:
	r=urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)
	sys.exit(0 if getattr(r, 'status', 200)==200 else 1)
except Exception:
	sys.exit(1)"

CMD ["python", "-m", "src.bot"]
