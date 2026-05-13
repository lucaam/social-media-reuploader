from prometheus_client import Counter

processed_links_total = Counter(
    "telegram_downloader_processed_links_total", "Number of links processed"
)
downloads_failed_total = Counter(
    "telegram_downloader_downloads_failed_total", "Number of failed downloads"
)
files_sent_total = Counter(
    "telegram_downloader_files_sent_total", "Number of files successfully sent"
)
files_too_large_total = Counter(
    "telegram_downloader_files_too_large_total",
    "Number of files that exceeded Telegram size limit",
)
