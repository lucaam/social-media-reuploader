import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
WORKERS = int(os.getenv("WORKERS", "2"))
TELEGRAM_MAX_FILE_SIZE = int(os.getenv("TELEGRAM_MAX_FILE_SIZE", str(50 * 1024 * 1024)))
MODE = os.getenv("MODE", "webhook")
TMP_DIR = os.getenv("TMP_DIR", "/tmp/telegram_downloader")

# Ensure temp dir exists
try:
    os.makedirs(TMP_DIR, exist_ok=True)
except Exception:
    pass

# Control whether the process creates a rotating file log handler.
# Default: do not write to a file (useful for containers where stdout is preferred).
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "false").lower() in ("1", "true", "yes")

# Whether the worker should generate and attach thumbnails using ffmpeg.
# Set to false to skip thumbnail generation and let Telegram handle thumbnails.
WORKER_GENERATE_THUMBNAIL = os.getenv("WORKER_GENERATE_THUMBNAIL", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Control whether health endpoints expose diagnostic details.
# Set to true only for debugging / operator troubleshooting; Kubernetes
# probes call /health frequently so keep disabled in production.
HEALTH_DEBUG = os.getenv("HEALTH_DEBUG", "false").lower() in ("1", "true", "yes")

# yt-dlp helper defaults: optional user-agent, cookies path or cookies-from-browser
# These can be set as environment variables in Helm values for bot workers.
YTDLP_USER_AGENT = os.getenv("YTDLP_USER_AGENT")
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES_PATH")
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER")
# Optional extra headers for yt-dlp (format: 'Header: Value|Other: Value')
YTDLP_HEADERS = os.getenv("YTDLP_HEADERS")

# When true, the downloader will only run a plain `yt-dlp` download and
# skip any remux/recode/transcode steps. Useful for debugging or when you
# want to upload the exact yt-dlp output verbatim.
SIMPLE_YTDLP_ONLY = os.getenv("SIMPLE_YTDLP_ONLY", "false").lower() in (
    "1",
    "true",
    "yes",
)

# When true and the downloader returned an untouched yt-dlp file (see
# SIMPLE_YTDLP_ONLY), the worker will upload the file as a document
# (sendDocument) instead of `sendVideo`. This preserves the original
# bytes and avoids any inline/telegram re-processing.
# NOTE: removed SEND_ORIGINAL_AS_DOCUMENT option — videos should not be sent as documents.

# When true, the worker will keep downloaded files and temporary directories
# instead of deleting them after processing. Useful for debugging or
# forensic inspection of downloaded media. Default: false.
KEEP_DOWNLOADED_FILES = os.getenv("KEEP_DOWNLOADED_FILES", "false").lower() in (
    "1",
    "true",
    "yes",
)
