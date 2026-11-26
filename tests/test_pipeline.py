"""Unit tests for the pure helpers of src/pipeline.py.

End-to-end processing (with fakes for yt-dlp, ffmpeg and Telegram) is in
tests/test_worker.py.
"""

import pytest

from src import config, downloader, media, pipeline
from src.downloader import DownloadedFile, MediaInfo, MediaItem


def make_item(index=1, kind="video", **overrides):
    values = {
        "index": index,
        "id": f"id{index}",
        "title": f"title {index}",
        "kind": kind,
        "duration": 12.0,
        "width": 720,
        "height": 1280,
        "filesize": 1000,
        "direct_url": None,
        "direct_ext": None,
        "http_headers": {},
        "thumbnail": None,
        "uploader": "someone",
        "webpage_url": None,
    }
    values.update(overrides)
    return MediaItem(**values)


def make_info(items, extractor_key="Instagram"):
    return MediaInfo(
        url="https://example.com/p/1",
        extractor_key=extractor_key,
        title="t",
        is_live=False,
        items=list(items),
        info_path="/nonexistent/info.json",
        raw={},
    )


def prepared_item(kind="video", index=1):
    probe = media.ProbeResult(
        has_video=kind == "video",
        has_audio=True,
        duration=5.0,
        width=640,
        height=360,
        display_width=640,
        display_height=360,
        vcodec="h264" if kind == "video" else None,
        acodec="aac",
        container="mp4",
        size=100,
        rotation=0,
    )
    item = make_item(index, kind)
    return pipeline.PreparedItem(
        DownloadedFile(f"/w/item-{index:02d}.mp4", 100, item),
        media.PreparedMedia(f"/w/item-{index:02d}.mp4", probe, False, "none", 100),
        kind,
    )


# ---------------------------------------------------------------------------
# send_failure: what may be retried, what must never be resent
# ---------------------------------------------------------------------------
def test_flood_wait_is_retryable_and_not_sent():
    failure = pipeline.send_failure(
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 120",
            "parameters": {"retry_after": 120},
        }
    )
    assert isinstance(failure, pipeline.SendFailed)
    assert failure.retryable and failure.not_sent
    assert failure.retry_after == 120.0
    assert failure.code == "send_failed"


def test_forbidden_and_too_large_and_bad_request():
    forbidden = pipeline.send_failure(
        {"ok": False, "error_code": 403, "description": "bot was blocked by the user"}
    )
    assert isinstance(forbidden, pipeline.SendForbidden)
    assert forbidden.code == "forbidden" and not forbidden.retryable
    too_large = pipeline.send_failure(
        {"ok": False, "error_code": 413, "description": "Request Entity Too Large"}
    )
    assert isinstance(too_large, downloader.TooLarge)
    bad = pipeline.send_failure(
        {"ok": False, "error_code": 400, "description": "Bad Request: wrong file"}
    )
    assert bad.not_sent and not bad.retryable and bad.error_code == 400


@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "error_code": 502, "description": "Bad Gateway"},
        {"ok": False, "description": "network error: TimeoutError: no details"},
        {"ok": False, "description": "network error: ServerDisconnectedError: x"},
    ],
)
def test_ambiguous_failures_are_never_retried(result):
    failure = pipeline.send_failure(result)
    assert not failure.not_sent
    assert not failure.retryable


def test_connection_failures_never_reached_telegram():
    failure = pipeline.send_failure(
        {"ok": False, "description": "network error: ClientConnectorError: refused"}
    )
    assert failure.not_sent and failure.retryable
    local = pipeline.send_failure(
        {"ok": False, "description": "file not found: item-01.mp4"}
    )
    assert local.not_sent and not local.retryable


def test_send_failure_redacts_the_token():
    failure = pipeline.send_failure(
        {
            "ok": False,
            "error_code": 400,
            "description": "bad https://api.telegram.org/bot123456:"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123/sendVideo",
        }
    )
    assert "ABCDEFGHIJ" not in failure.detail


# ---------------------------------------------------------------------------
# Other helpers
# ---------------------------------------------------------------------------
def test_retry_delay_backs_off_exponentially(monkeypatch):
    monkeypatch.setattr(config, "WORKER_RETRY_BACKOFF_SECONDS", 30, raising=False)
    assert pipeline.retry_delay(1) == 30
    assert pipeline.retry_delay(2) == 60
    assert pipeline.retry_delay(3) == 120
    assert pipeline.retry_delay(50) == pipeline.MAX_RETRY_BACKOFF_SECONDS
    assert pipeline.retry_delay(1, retry_after=300) == 300
    monkeypatch.setattr(config, "WORKER_RETRY_BACKOFF_SECONDS", "junk", raising=False)
    assert pipeline.retry_delay(1) == pipeline.DEFAULT_RETRY_BACKOFF_SECONDS


def test_album_groups_keep_order_kinds_and_size_limit():
    videos = [prepared_item("video", i) for i in range(1, 13)]
    groups = pipeline.album_groups(videos)
    assert [len(group) for group in groups] == [10, 2]
    mixed = [
        prepared_item("video", 1),
        prepared_item("video", 2),
        prepared_item("audio", 3),
        prepared_item("video", 4),
    ]
    kinds = [[item.kind for item in group] for group in pipeline.album_groups(mixed)]
    assert kinds == [["video", "video"], ["audio"], ["video"]]


def test_group_entries():
    video = prepared_item("video").group_entry("hello")
    assert video["type"] == "video"
    assert video["caption"] == "hello"
    assert (video["width"], video["height"]) == (640, 360)
    audio = prepared_item("audio").group_entry(None)
    assert audio["type"] == "audio"
    assert "caption" not in audio
    assert audio["title"] == "title 1" and audio["performer"] == "someone"


def test_tiktok_slideshow_detection():
    audio = [make_item(1, "audio")]
    assert pipeline.is_tiktok_slideshow(make_info(audio, "TikTok"))
    assert pipeline.is_tiktok_slideshow(make_info(audio, ""), provider="TikTok")
    assert not pipeline.is_tiktok_slideshow(make_info(audio, "Soundcloud"))
    assert not pipeline.is_tiktok_slideshow(make_info([make_item(1)], "TikTok"))


def test_request_from_row_and_chat_kind():
    row = {
        "id": 3,
        "chat_id": -100123,
        "url": "https://example.com/v",
        "chat_type": None,
        "original_message_id": 9,
        "description": "d",
        "provider": "Vimeo",
        "attempts": 2,
    }
    request = pipeline.Request.from_row(row)
    assert not request.is_private  # negative ids are groups/channels
    assert pipeline.Request.from_row({**row, "chat_id": 55}).is_private
    assert not pipeline.Request.from_row(
        {**row, "chat_id": 55, "chat_type": "group"}
    ).is_private
    assert request.attempts == 2
    with pytest.raises(ValueError):
        pipeline.Request.from_row({**row, "url": None})
    with pytest.raises(ValueError):
        pipeline.Request.from_row({**row, "chat_id": None})


def test_job_state_status():
    state = pipeline.JobState.from_row(
        {"id": 1, "chat_id": 2, "url": "u", "provider": "Youtube"}
    )
    status = state.as_status()
    assert status["request_id"] == 1
    assert status["stage"] == "starting"
    assert set(status) == {
        "request_id",
        "chat_id",
        "url",
        "provider",
        "stage",
        "started_at",
    }
