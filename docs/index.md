# Social Media Reuploader

This project is a lightweight Telegram bot that detects links to media content (YouTube, TikTok, Instagram, Facebook), downloads them (using `yt-dlp`) and attempts to repost the media to the chat.

Key features:
- Automatic link detection from messages
- Asynchronous downloads with worker pool
- Size checks and fallback behavior for large files
- Prometheus metrics endpoint

See the Usage and Deploy pages for instructions to run locally, with Docker, or on Kubernetes via Helm.
