"""Tests for src/metrics.py (counters move together, /metrics rendering)."""

from prometheus_client import REGISTRY

from src import metrics

P = "telegram_downloader"


def value(name, **labels):
    return REGISTRY.get_sample_value(f"{P}_{name}", labels or None) or 0.0


def test_record_submit_counts_received_and_one_result():
    before = (
        value("links_received_total"),
        value("links_accepted_total", outcome="deferred"),
        value("links_refused_total", reason="pending_cap"),
    )
    metrics.record_submit("deferred")
    metrics.record_submit("pending_cap")
    assert value("links_received_total") == before[0] + 2
    assert value("links_accepted_total", outcome="deferred") == before[1] + 1
    assert value("links_refused_total", reason="pending_cap") == before[2] + 1


def test_record_job_done_counts_delivery():
    before = (
        value("processed_links_total", outcome="done"),
        value("downloads_ok_total", method="server_fetch"),
        value("files_sent_total", media_type="video"),
        value("bytes_sent_total"),
        value("processing_duration_seconds_count", outcome="done"),
    )
    metrics.record_job(
        "done", 1.5, method="server_fetch", sent={"video": 2}, bytes_sent=1000
    )
    assert value("processed_links_total", outcome="done") == before[0] + 1
    assert value("downloads_ok_total", method="server_fetch") == before[1] + 1
    assert value("files_sent_total", media_type="video") == before[2] + 2
    assert value("bytes_sent_total") == before[3] + 1000
    assert value("processing_duration_seconds_count", outcome="done") == before[4] + 1


def test_record_job_failures_by_phase_and_code():
    before = (
        value("downloads_failed_total", code="too_large"),
        value("files_too_large_total"),
        value("uploads_failed_total", code="send_failed"),
        value("downloads_failed_total", code="timeout"),
    )
    metrics.record_job("failed", 2.0, error_code="too_large", error_phase="download")
    metrics.record_job("failed", 2.0, error_code="send_failed", error_phase="send")
    metrics.record_job("retry", 2.0, error_code="timeout", error_phase="download")
    metrics.record_job("skipped", 0.1)
    assert value("downloads_failed_total", code="too_large") == before[0] + 1
    assert value("files_too_large_total") == before[1] + 1
    assert value("uploads_failed_total", code="send_failed") == before[2] + 1
    assert value("downloads_failed_total", code="timeout") == before[3] + 1


def test_record_job_without_a_phase_is_neither_a_download_nor_an_upload():
    before = (
        value("downloads_failed_total", code="invalid_request"),
        value("uploads_failed_total", code="invalid_request"),
        value("processed_links_total", outcome="failed"),
    )
    metrics.record_job("failed", 0.0, error_code="invalid_request")
    assert value("downloads_failed_total", code="invalid_request") == before[0]
    assert value("uploads_failed_total", code="invalid_request") == before[1]
    assert value("processed_links_total", outcome="failed") == before[2] + 1


def test_queue_depth_and_render():
    metrics.set_queue_depth(queued=4, deferred=2, running=1)
    assert value("queue_depth", state="queued") == 4
    assert value("queue_depth", state="deferred") == 2
    assert value("queue_depth", state="running") == 1
    body, content_type = metrics.render()
    assert content_type.startswith("text/plain")
    text = body.decode()
    assert f"{P}_links_received_total" in text
    assert f'{P}_links_refused_total{{reason="duplicate"}}' in text
    assert f"{P}_processing_duration_seconds_bucket" in text
