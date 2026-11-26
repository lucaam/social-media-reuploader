"""Prometheus metrics of the bot process.

The worker records every link it receives (:func:`record_submit`) and every
processing attempt it finishes (:func:`record_job`) through the helpers below,
so each branch increments exactly one outcome. :func:`render` returns the
``/metrics`` response body and content type for the entrypoints.

Metric names keep the historical ``telegram_downloader_`` prefix; counters
that existed before (processed links, failed downloads, files sent, files
too large) keep their names and gained labels.
"""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

_PREFIX = "telegram_downloader"

# Submit results (src.worker.SubmitStatus values).
ACCEPTED_RESULTS = ("queued", "deferred")
REFUSED_RESULTS = (
    "duplicate",
    "rate_limited",
    "pending_cap",
    "unsupported",
    "ignored",
    "closed",
)
# Outcomes of one processing attempt (src.pipeline.Outcome values).
JOB_OUTCOMES = ("done", "failed", "retry", "skipped", "interrupted", "crashed")
DOWNLOAD_METHODS = ("local", "server_fetch")
MEDIA_TYPES = ("video", "audio", "document")
QUEUE_STATES = ("queued", "deferred", "running")
# Error phases: "download" (extract/download/convert) or "send" (Telegram).
SEND_PHASE = "send"

links_received_total = Counter(
    f"{_PREFIX}_links_received_total", "Links submitted to the worker"
)
links_accepted_total = Counter(
    f"{_PREFIX}_links_accepted_total",
    "Links accepted for processing (queued now or deferred)",
    ["outcome"],
)
links_refused_total = Counter(
    f"{_PREFIX}_links_refused_total",
    "Links refused or ignored at submit, by reason",
    ["reason"],
)
processed_links_total = Counter(
    f"{_PREFIX}_processed_links_total",
    "Processing attempts finished, by outcome",
    ["outcome"],
)
downloads_ok_total = Counter(
    f"{_PREFIX}_downloads_ok_total",
    "Links whose media was obtained and delivered, by method",
    ["method"],
)
downloads_failed_total = Counter(
    f"{_PREFIX}_downloads_failed_total",
    "Attempts that failed before delivery (extract/download/convert), by error code",
    ["code"],
)
uploads_failed_total = Counter(
    f"{_PREFIX}_uploads_failed_total",
    "Attempts whose Telegram upload failed, by error code",
    ["code"],
)
files_sent_total = Counter(
    f"{_PREFIX}_files_sent_total",
    "Files successfully sent to Telegram, by media type",
    ["media_type"],
)
bytes_sent_total = Counter(
    f"{_PREFIX}_bytes_sent_total",
    "Bytes of media delivered to Telegram (uploads and URL fetches)",
)
files_too_large_total = Counter(
    f"{_PREFIX}_files_too_large_total",
    "Links refused because the media cannot fit Telegram's size limit",
)
processing_duration_seconds = Histogram(
    f"{_PREFIX}_processing_duration_seconds",
    "Duration of one processing attempt, by outcome",
    ["outcome"],
    buckets=(1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600, 1200, 1800, 3600),
)
queue_depth = Gauge(
    f"{_PREFIX}_queue_depth",
    "Requests waiting (queued: due now, deferred: later) or running",
    ["state"],
)

# Create the known label values so they are exported (as 0) from the start.
for _value in ACCEPTED_RESULTS:
    links_accepted_total.labels(outcome=_value)
for _value in REFUSED_RESULTS:
    links_refused_total.labels(reason=_value)
for _value in JOB_OUTCOMES:
    processed_links_total.labels(outcome=_value)
    processing_duration_seconds.labels(outcome=_value)
for _value in DOWNLOAD_METHODS:
    downloads_ok_total.labels(method=_value)
for _value in MEDIA_TYPES:
    files_sent_total.labels(media_type=_value)
for _value in QUEUE_STATES:
    queue_depth.labels(state=_value)


def record_submit(result: str) -> None:
    """One link handed to ``WorkerPool.submit`` and its result."""
    links_received_total.inc()
    if result in ACCEPTED_RESULTS:
        links_accepted_total.labels(outcome=result).inc()
    else:
        links_refused_total.labels(reason=result).inc()


def record_job(
    outcome: str,
    duration_seconds: float,
    *,
    error_code: str | None = None,
    error_phase: str | None = None,
    method: str | None = None,
    sent: Mapping[str, int] | None = None,
    bytes_sent: int = 0,
) -> None:
    """One processing attempt finished.

    ``done`` counts the delivery (method, files by media type, bytes);
    ``failed``/``retry`` count the error code as a download or an upload
    failure (``error_phase == "send"``).
    """
    processed_links_total.labels(outcome=outcome).inc()
    processing_duration_seconds.labels(outcome=outcome).observe(
        max(0.0, duration_seconds)
    )
    if outcome == "done":
        downloads_ok_total.labels(method=method or "local").inc()
        for media_type, count in (sent or {}).items():
            if count > 0:
                files_sent_total.labels(media_type=media_type).inc(count)
        if bytes_sent > 0:
            bytes_sent_total.inc(bytes_sent)
        return
    if outcome not in ("failed", "retry") or not error_code:
        return
    if error_phase == SEND_PHASE:
        uploads_failed_total.labels(code=error_code).inc()
    else:
        downloads_failed_total.labels(code=error_code).inc()
    if error_code == "too_large" and outcome == "failed":
        files_too_large_total.inc()


def set_queue_depth(*, queued: int, deferred: int, running: int) -> None:
    queue_depth.labels(state="queued").set(queued)
    queue_depth.labels(state="deferred").set(deferred)
    queue_depth.labels(state="running").set(running)


def render(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    """``(body, content_type)`` of the Prometheus text exposition."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
