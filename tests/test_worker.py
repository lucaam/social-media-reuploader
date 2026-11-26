"""Tests for src/worker.py and src/pipeline.py.

Real SQLite database (one temp file per test); fakes replace yt-dlp
(``downloader.fetch_info``/``download``), ffmpeg (``media.prepare_for_telegram``/
``make_thumbnail``) and the Telegram Bot API (``telegram_api.*``). No network.
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

import src
from src import (
    config,
    db,
    downloader,
    media,
    messages,
    pipeline,
    providers,
    telegram_api,
    worker,
)
from src.downloader import DownloadedFile, MediaInfo, MediaItem
from src.providers import ProviderMatch
from src.worker import SubmitStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456:TEST-token-for-the-worker-tests-0000"
MIB = 1024 * 1024
URL = "https://www.instagram.com/reel/AAA/"
URL2 = "https://www.instagram.com/reel/BBB/"
URL3 = "https://www.instagram.com/reel/CCC/"
URL4 = "https://www.instagram.com/reel/DDD/"
DIRECT = "https://cdn.example.com/video.mp4"
UPLOADS = ("send_video", "send_audio", "send_document", "send_media_group")
DEFAULT = object()  # FakeTelegram.respond(): use the default answer


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeTelegram:
    """Records every Bot API call; answers ok unless told otherwise."""

    METHODS = (
        "send_message",
        "send_video",
        "send_audio",
        "send_document",
        "send_media_group",
        "set_message_reaction",
        "delete_message",
        "edit_message_text",
    )

    def __init__(self):
        self.calls = []  # (method, chat_id, args, kwargs)
        self.queued = defaultdict(list)
        self.reactions_ok = True
        self.upload_gate = None  # asyncio.Event: uploads wait for it
        self.upload_started = None  # asyncio.Event set when an upload starts
        self.message_ids = []  # ids of the messages "posted", in order
        self._next_id = 500

    def install(self, monkeypatch):
        for method in self.METHODS:
            monkeypatch.setattr(telegram_api, method, self._fake(method))

    def respond(self, method, *results):
        self.queued[method].extend(results)

    def _fake(self, method):
        async def call(token, chat_id, *args, **kwargs):
            assert token == TOKEN
            self.calls.append((method, chat_id, args, kwargs))
            if method in UPLOADS:
                if self.upload_started is not None:
                    self.upload_started.set()
                if self.upload_gate is not None:
                    await self.upload_gate.wait()
            if self.queued[method]:
                answer = self.queued[method].pop(0)
                if answer is not DEFAULT:
                    return answer
            return self._default(method, chat_id, args)

        return call

    def _message(self, chat_id):
        self._next_id += 1
        self.message_ids.append(self._next_id)
        return {"message_id": self._next_id, "chat": {"id": chat_id}}

    def _default(self, method, chat_id, args):
        if method == "set_message_reaction":
            if self.reactions_ok:
                return {"ok": True, "result": True}
            return {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: REACTION_INVALID",
            }
        if method in ("delete_message", "edit_message_text"):
            return {"ok": True, "result": True}
        if method == "send_media_group":
            return {"ok": True, "result": [self._message(chat_id) for _ in args[0]]}
        return {"ok": True, "result": self._message(chat_id)}

    def of(self, *methods):
        return [call for call in self.calls if call[0] in methods]

    def reactions(self):
        return [call[2][1] for call in self.of("set_message_reaction")]

    def texts(self):
        return [call[2][0] for call in self.of("send_message")]

    def uploads(self):
        return self.of(*UPLOADS)

    def sources(self, method="send_video"):
        return [call[2][0] for call in self.of(method)]


def write_file(path, size):
    with open(path, "wb") as fh:
        fh.truncate(size)  # sparse: instant, any size


def make_item(index=1, kind="video", **overrides):
    values = {
        "index": index,
        "id": f"id{index}",
        "title": f"title {index}",
        "kind": kind,
        "duration": 12.0,
        "width": 1080,
        "height": 1920,
        "filesize": 2 * MIB,
        "direct_url": None,
        "direct_ext": None,
        "http_headers": {},
        "thumbnail": None,
        "uploader": "someone",
        "webpage_url": None,
    }
    values.update(overrides)
    return MediaItem(**values)


class FakeSite:
    """downloader.fetch_info / downloader.download."""

    def __init__(self):
        self.items = [make_item(1)]
        self.extractor_key = "Instagram"
        self.fetch_errors = []
        self.download_errors = []
        self.sizes = {}  # item index -> downloaded size
        self.extensions = {}  # item index -> file extension
        self.fetches = []
        self.downloads = []
        self.gate = None  # asyncio.Event: downloads wait for it
        self.started = None  # asyncio.Event set when a download starts

    async def fetch_info(self, url, workdir, *, timeout=None):
        self.fetches.append((url, workdir))
        if self.fetch_errors:
            raise self.fetch_errors.pop(0)
        return MediaInfo(
            url=url,
            extractor_key=self.extractor_key,
            title="title",
            is_live=False,
            items=list(self.items),
            info_path=os.path.join(workdir, "info.json"),
            raw={},
        )

    async def download(self, info, workdir, *, items=None, timeout=None):
        self.downloads.append(workdir)
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.download_errors:
            raise self.download_errors.pop(0)
        files = []
        for item in info.items:
            ext = self.extensions.get(item.index, "mp4")
            path = os.path.join(workdir, f"item-{item.index:02d}.{ext}")
            size = self.sizes.get(item.index, 2 * MIB)
            await asyncio.to_thread(write_file, path, size)
            files.append(DownloadedFile(path, size, item))
        return files


class FakeMedia:
    """media.prepare_for_telegram / media.make_thumbnail."""

    def __init__(self):
        self.compress_to = None  # final size of a size-targeted re-encode
        self.compress_audio = False  # compress_to applies to audio files too
        self.errors = {}  # item index -> exception
        self.audio_only = False  # every file probes as audio-only
        self.thumbnails = True
        self.prepared = []

    async def prepare_for_telegram(
        self, path, workdir, *, max_bytes, probe_result=None
    ):
        self.prepared.append(path)
        index = int(os.path.basename(path)[5:7])
        if index in self.errors:
            raise self.errors[index]
        size = await asyncio.to_thread(os.path.getsize, path)
        has_video = not (self.audio_only or path.endswith((".m4a", ".mp3")))
        out, final, compressed = path, size, False
        if self.compress_to is not None and (has_video or self.compress_audio):
            out, final, compressed = f"{path[:-4]}.tg.mp4", self.compress_to, True
            await asyncio.to_thread(write_file, out, final)
        probe = media.ProbeResult(
            has_video=has_video,
            has_audio=True,
            duration=12.0,
            width=1080,
            height=1920,
            display_width=720,
            display_height=1280,
            vcodec="h264" if has_video else None,
            acodec="aac",
            container="mp4",
            size=final,
            rotation=0,
        )
        action = "compress" if compressed else "none"
        return media.PreparedMedia(out, probe, compressed, action, size)

    async def make_thumbnail(self, path, out_dir, *, duration=None):
        if not self.thumbnails:
            return None
        thumb = os.path.join(out_dir, "thumb.jpg")
        await asyncio.to_thread(write_file, thumb, 10)
        return thumb


SETTINGS = {
    "KEEP_DOWNLOADED_FILES": False,
    "WORKER_POLL_SECONDS": 0.05,
    "WORKER_HEARTBEAT_SECONDS": 0.05,
    "WORKER_SHUTDOWN_TIMEOUT": 5,
    "WORKER_MAX_ATTEMPTS": 3,
    "WORKER_REHYDRATE_ON_START": True,
    "WORKER_RETRY_BACKOFF_SECONDS": 30,
    "WORKER_GENERATE_THUMBNAIL": True,
    "TELEGRAM_SERVER_FETCH": True,
    "TELEGRAM_URL_FETCH_MAX_SIZE": 20 * MIB,
    "TELEGRAM_MAX_FILE_SIZE": 50 * MIB,
    "RATE_LIMITS": ((1, 10), (3, 60), (5, 3600), (20, 30 * 86400)),
    "RATE_LIMIT_MAX_DEFER_SECONDS": 120,
    "MAX_PENDING_PER_CHAT": 3,
    "DUPLICATE_WINDOW_SECONDS": 5400,
    "NOTIFY_RATE_THROTTLE_SECONDS": 60,
    "NOTIFY_DUPLICATE_THROTTLE_SECONDS": 600,
    "MAX_DURATION_SECONDS": 3600,
    "UPDATES_RETENTION_DAYS": 14,
    "REQUESTS_RETENTION_DAYS": 0,
    "TMP_RETENTION_HOURS": 24,
    "MODE": "polling",
    "WEBHOOK_URL": None,
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "requests.db"))
    db.init_db()
    work = tmp_path / "work"
    monkeypatch.setattr(config, "TMP_DIR", str(work), raising=False)
    for name, value in SETTINGS.items():
        monkeypatch.setattr(config, name, value, raising=False)
    tg = FakeTelegram()
    tg.install(monkeypatch)
    site = FakeSite()
    monkeypatch.setattr(downloader, "fetch_info", site.fetch_info)
    monkeypatch.setattr(downloader, "download", site.download)
    fake_media = FakeMedia()
    monkeypatch.setattr(media, "prepare_for_telegram", fake_media.prepare_for_telegram)
    monkeypatch.setattr(media, "make_thumbnail", fake_media.make_thumbnail)

    async def unknown_provider(url, **kwargs):
        return None

    monkeypatch.setattr(providers, "match_provider_async", unknown_provider)
    return SimpleNamespace(tg=tg, site=site, media=fake_media, work=work, tmp=tmp_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run(coro):
    return asyncio.run(coro)


def match(url=URL, key="Instagram"):
    return ProviderMatch(key=key, name=key, url=url, normalized_url=url)


def new_request(
    chat_id=42,
    url=URL,
    *,
    chat_type="private",
    message_id=7,
    description="guarda che bello",
    provider="Instagram",
    **fields,
):
    return db.create_request(
        chat_id,
        url,
        normalized_url=url,
        provider=provider,
        chat_type=chat_type,
        original_message_id=message_id,
        description=description,
        **fields,
    )


def force(request_id, **columns):
    """Put a row in an arbitrary state, bypassing the state machine."""
    sets = ", ".join(f"{name} = ?" for name in columns)
    with db.write() as conn:
        conn.execute(
            f"UPDATE requests SET {sets} WHERE id = ?", (*columns.values(), request_id)
        )


def claim(request_id):
    row = db.claim_request(request_id)
    assert row is not None, f"request {request_id} is not claimable"
    return row


async def process(row):
    return await pipeline.process(TOKEN, row, pipeline.JobState.from_row(row))


def process_new(**kwargs):
    request_id = new_request(**kwargs)
    result = run(process(claim(request_id)))
    return result, db.get_request(request_id)


def status(request_id):
    return db.get_request(request_id)["status"]


async def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def started_pool(workers=2):
    pool = worker.WorkerPool(TOKEN, workers=workers)
    await pool.start()
    return pool


def metric(name, **labels):
    sample = REGISTRY.get_sample_value(f"telegram_downloader_{name}", labels or None)
    return sample or 0.0


def workdirs(env):
    return sorted(path.name for path in env.work.iterdir()) if env.work.exists() else []


def break_db(monkeypatch, name, failures=None):
    """db.<name> raises 'database is locked' while state["on"] (or only for
    its first ``failures`` calls); it works normally afterwards."""
    real = getattr(db, name)
    state = {"on": True, "calls": 0}

    def fake(*args, **kwargs):
        state["calls"] += 1
        if state["on"] and (failures is None or state["calls"] <= failures):
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(db, name, fake)
    return state


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(pipeline, "OUTCOME_WRITE_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(worker, "SETTLE_RETRY_SECONDS", 0.05)


# ---------------------------------------------------------------------------
# Pipeline: delivery
# ---------------------------------------------------------------------------
def test_video_is_delivered_as_a_reply_with_caption_and_display_size(env):
    result, row = process_new()
    assert result.outcome == pipeline.Outcome.DONE
    assert result.method == "local"
    [(_, chat_id, args, kwargs)] = env.tg.of("send_video")
    assert chat_id == 42
    assert args[0].endswith("item-01.mp4")
    assert kwargs["caption"] == "guarda che bello"
    assert kwargs["reply_to_message_id"] == 7
    assert (kwargs["width"], kwargs["height"]) == (720, 1280)
    assert kwargs["thumbnail"].endswith("thumb.jpg")
    assert env.tg.reactions() == ["👀", "🏆"]
    assert env.tg.texts() == []
    assert row["status"] == "done"
    assert row["media_type"] == "video"
    assert row["sent_message_id"] == env.tg.message_ids[-1]
    assert (row["original_size"], row["final_size"]) == (2 * MIB, 2 * MIB)
    assert row["compressed"] == 0
    assert row["error_code"] is None
    assert workdirs(env) == []  # removed after the job


def test_compressed_video_caption_carries_the_note(env):
    env.site.sizes[1] = int(80.1 * MIB)
    env.media.compress_to = int(48.3 * MIB)
    _, row = process_new(description="ciao")
    caption = env.tg.of("send_video")[0][3]["caption"]
    assert caption == "ciao\n\nVideo compresso: 80,1 MB → 48,3 MB"
    assert env.tg.texts() == []  # no separate compression message
    assert row["compressed"] == 1
    assert row["original_size"] == int(80.1 * MIB)
    assert row["final_size"] == int(48.3 * MIB)


def test_compression_note_alone_when_there_is_no_description(env):
    env.media.compress_to = MIB
    process_new(description=None)
    assert env.tg.of("send_video")[0][3]["caption"].startswith("Video compresso: ")


def test_no_caption_without_description(env):
    process_new(description=None)
    assert env.tg.of("send_video")[0][3]["caption"] is None


def test_thumbnail_can_be_disabled(env, monkeypatch):
    monkeypatch.setattr(config, "WORKER_GENERATE_THUMBNAIL", False)
    process_new()
    assert env.tg.of("send_video")[0][3]["thumbnail"] is None


def test_audio_is_sent_with_send_audio(env):
    env.site.extractor_key = "Soundcloud"
    env.site.items = [make_item(1, "audio", title="Song", uploader="Band")]
    env.site.extensions[1] = "m4a"
    result, row = process_new(provider="Soundcloud", url="https://soundcloud.com/a/b")
    [(_, _, args, kwargs)] = env.tg.of("send_audio")
    assert args[0].endswith(".m4a")
    assert (kwargs["title"], kwargs["performer"]) == ("Song", "Band")
    assert kwargs["reply_to_message_id"] == 7
    assert env.tg.of("send_video") == []
    assert row["media_type"] == "audio"
    assert result.sent == {"audio": 1}


def test_several_items_are_sent_as_one_album(env):
    env.site.items = [make_item(i) for i in (1, 2, 3)]
    result, row = process_new(description="tre video")
    [(_, chat_id, args, kwargs)] = env.tg.of("send_media_group")
    assert chat_id == 42
    entries = args[0]
    assert [entry["type"] for entry in entries] == ["video"] * 3
    assert entries[0]["caption"] == "tre video"
    assert all("caption" not in entry for entry in entries[1:])
    assert kwargs["reply_to_message_id"] == 7
    assert env.tg.of("send_video") == []
    assert row["media_type"] == "album"
    assert row["sent_message_id"] == env.tg.message_ids[0]
    assert row["final_size"] == 3 * 2 * MIB
    assert result.sent == {"video": 3}


def test_albums_hold_ten_items_and_audio_is_not_mixed_with_videos(env):
    env.site.items = [make_item(i) for i in range(1, 12)] + [make_item(12, "audio")]
    env.site.extensions[12] = "m4a"
    result, row = process_new()
    groups = [[e["type"] for e in call[2][0]] for call in env.tg.of("send_media_group")]
    assert groups == [["video"] * 10, ["video"], ["audio"]]
    caption_holders = [
        e
        for call in env.tg.of("send_media_group")
        for e in call[2][0]
        if "caption" in e
    ]
    assert len(caption_holders) == 1
    assert result.sent == {"video": 11, "audio": 1}
    assert row["status"] == "done"


def test_album_part_failure_after_a_delivery_still_counts_as_done(env):
    env.site.items = [make_item(i) for i in range(1, 12)]
    env.tg.respond(
        "send_media_group",
        DEFAULT,
        {"ok": False, "error_code": 400, "description": "Bad Request: boom"},
    )
    result, row = process_new()
    assert len(env.tg.of("send_media_group")) == 2
    assert result.outcome == pipeline.Outcome.DONE
    assert result.sent == {"video": 10}
    assert row["status"] == "done"
    assert env.tg.reactions()[-1] == "🏆"


def test_an_item_that_fails_to_convert_is_skipped(env):
    env.site.items = [make_item(1), make_item(2)]
    env.media.errors[2] = downloader.ConversionFailed("ffmpeg exploded")
    _, row = process_new()
    assert len(env.tg.of("send_video")) == 1  # the remaining item, alone
    assert row["status"] == "done"


def test_tiktok_photo_slideshow_is_refused_as_no_media(env):
    env.site.extractor_key = "TikTok"
    env.site.items = [make_item(1, "audio")]
    _, row = process_new(provider="TikTok", url="https://www.tiktok.com/@a/photo/1")
    assert row["status"] == "failed"
    assert row["error_code"] == "no_media"
    assert env.site.downloads == []
    assert env.tg.uploads() == []
    assert env.tg.texts() == [messages.failure("no_media", slideshow=True)]
    assert env.tg.reactions() == ["👀", "🍌"]


def test_video_item_that_yields_only_audio_is_not_sent(env):
    env.media.audio_only = True
    _, row = process_new()
    assert row["error_code"] == "no_media"
    assert env.tg.uploads() == []


# ---------------------------------------------------------------------------
# Pipeline: Telegram server-side URL fetch
# ---------------------------------------------------------------------------
def test_server_side_fetch_delivers_without_downloading(env):
    env.site.items = [make_item(1, direct_url=DIRECT, filesize=5 * MIB)]
    result, row = process_new()
    [(_, _, args, kwargs)] = env.tg.uploads()
    assert args[0] == DIRECT
    assert kwargs["caption"] == "guarda che bello"
    assert kwargs["reply_to_message_id"] == 7
    assert env.site.downloads == []
    assert result.method == "server_fetch"
    assert row["status"] == "done"
    assert row["final_size"] == 5 * MIB
    assert row["media_type"] == "video"
    assert env.tg.reactions() == ["👀", "🏆"]


def test_server_side_fetch_refused_falls_back_to_a_local_upload(env):
    env.site.items = [make_item(1, direct_url=DIRECT, filesize=5 * MIB)]
    env.tg.respond(
        "send_video",
        {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: failed to get HTTP URL content",
        },
    )
    result, row = process_new()
    sources = env.tg.sources()
    assert sources[0] == DIRECT
    assert sources[1].endswith("item-01.mp4")
    assert len(env.site.downloads) == 1
    assert result.method == "local"
    assert row["status"] == "done"


def test_server_side_fetch_with_unknown_outcome_is_never_resent(env):
    env.site.items = [make_item(1, direct_url=DIRECT, filesize=5 * MIB)]
    env.tg.respond(
        "send_video", {"ok": False, "description": "network error: TimeoutError: "}
    )
    _, row = process_new()
    assert len(env.tg.uploads()) == 1
    assert env.site.downloads == []
    assert row["status"] == "failed"
    assert row["error_code"] == "send_failed"


def test_server_side_fetch_flood_wait_defers_the_request(env):
    env.site.items = [make_item(1, direct_url=DIRECT, filesize=5 * MIB)]
    env.tg.respond(
        "send_video",
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 600",
            "parameters": {"retry_after": 600},
        },
    )
    before = time.time()
    result, row = process_new()
    assert result.outcome == pipeline.Outcome.RETRY
    assert row["status"] == "queued"
    assert db.ts_from_iso(row["not_before"]) >= before + 599
    assert env.site.downloads == []
    assert env.tg.reactions() == ["👀"]  # no 🍌 for a retry
    assert env.tg.texts() == []


@pytest.mark.parametrize(
    "case", ["youtube", "too_big", "unknown_size", "two_items", "disabled"]
)
def test_server_side_fetch_is_skipped_when_it_cannot_work(env, monkeypatch, case):
    item = make_item(1, direct_url=DIRECT, filesize=5 * MIB)
    env.site.items = [item]
    provider = "Instagram"
    if case == "youtube":
        env.site.extractor_key = provider = "Youtube"
    elif case == "too_big":
        env.site.items = [make_item(1, direct_url=DIRECT, filesize=21 * MIB)]
    elif case == "unknown_size":
        env.site.items = [make_item(1, direct_url=DIRECT, filesize=None)]
    elif case == "two_items":
        env.site.items = [item, make_item(2, direct_url=DIRECT, filesize=MIB)]
    else:
        monkeypatch.setattr(config, "TELEGRAM_SERVER_FETCH", False)
    _, row = process_new(provider=provider)
    assert DIRECT not in env.tg.sources()
    assert len(env.site.downloads) == 1
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# Pipeline: errors and user feedback
# ---------------------------------------------------------------------------
ERRORS = [
    (downloader.AuthRequired, {}),
    (downloader.ContentUnavailable, {}),
    (downloader.GeoBlocked, {}),
    (downloader.LiveStream, {}),
    (downloader.TooLong, {}),
    (downloader.TooLarge, {}),
    (downloader.NoMedia, {}),
    (downloader.UnsupportedUrl, {}),
    (downloader.DownloadTimeout, {"retryable": False}),
    (downloader.ConversionFailed, {}),
    (downloader.DownloadError, {}),
]


@pytest.mark.parametrize(("error_class", "kwargs"), ERRORS, ids=lambda v: str(v))
@pytest.mark.parametrize("chat_type", ["private", "group", "supergroup", "channel"])
def test_failures_are_explained_per_code_and_chat_type(
    env, error_class, kwargs, chat_type
):
    error = error_class("technical detail", **kwargs)
    env.site.fetch_errors.append(error)
    chat_id = 42 if chat_type == "private" else -10042
    result, row = process_new(chat_id=chat_id, chat_type=chat_type)
    assert result.outcome == pipeline.Outcome.FAILED
    assert row["status"] == "failed"
    assert row["error_code"] == error.code
    assert row["error_message"] == "technical detail"
    assert env.tg.reactions() == ["👀", "🍌"]
    expected = messages.failure(
        error.code, url=URL, max_duration_seconds=3600, step="download"
    )
    if chat_type == "private" or error.code == "too_large":
        assert env.tg.texts() == [expected]
        assert env.tg.of("send_message")[0][3]["reply_to_message_id"] == 7
    else:
        assert env.tg.texts() == []
    assert env.tg.uploads() == []


def test_too_large_in_a_group_replies_with_the_original_link(env):
    env.site.download_errors.append(downloader.TooLarge("about 900 MiB"))
    _, row = process_new(chat_id=-100777, chat_type="supergroup")
    [text] = env.tg.texts()
    assert URL in text
    assert row["error_code"] == "too_large"


def test_too_long_mentions_the_limit(env, monkeypatch):
    monkeypatch.setattr(config, "MAX_DURATION_SECONDS", 1800)
    env.site.fetch_errors.append(downloader.TooLong("2h"))
    process_new()
    assert "30 minuti" in env.tg.texts()[0]


def test_retryable_error_is_deferred_with_backoff(env):
    env.site.fetch_errors.append(downloader.DownloadError("HTTP 503", retryable=True))
    before = time.time()
    result, row = process_new()
    assert result.outcome == pipeline.Outcome.RETRY
    assert result.error_code == "failed"
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert before + 29 <= db.ts_from_iso(row["not_before"]) <= time.time() + 31
    assert env.tg.reactions() == ["👀"]
    assert env.tg.texts() == []
    assert "deferred" in [e["event_type"] for e in db.get_request_events(row["id"])]


def test_retryable_error_fails_after_the_last_attempt(env):
    request_id = new_request()
    delays = []
    for attempt in (1, 2, 3):
        env.site.fetch_errors.append(downloader.DownloadTimeout("too slow"))
        row = claim(request_id)
        assert row["attempts"] == attempt
        before = time.time()
        result = run(process(row))
        if attempt < 3:
            assert result.outcome == pipeline.Outcome.RETRY
            not_before = db.get_request(request_id)["not_before"]
            delays.append(round(db.ts_from_iso(not_before) - before))
            force(request_id, not_before=db.iso_from_ts(time.time() - 1))
    assert delays == [30, 60]
    row = db.get_request(request_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "timeout"
    assert env.tg.texts() == [messages.failure("timeout")]
    assert env.tg.reactions()[-1] == "🍌"


CONVERSION_TIMEOUT = "compress (attempt 1): conversion time limit (900s) exhausted"


def test_conversion_timeout_of_a_job_converting_alone_is_not_retried(env):
    env.media.errors[1] = downloader.DownloadTimeout(CONVERSION_TIMEOUT)
    result, row = process_new()
    assert result.outcome == pipeline.Outcome.FAILED  # the same encode again: no
    assert result.error_code == "conversion_timeout"
    assert (row["status"], row["error_code"]) == ("failed", "conversion_timeout")
    assert row["not_before"] is None
    assert env.tg.texts() == [
        "La conversione del video per Telegram ha impiegato troppo tempo."
    ]
    assert len(env.site.downloads) == 1


def test_conversion_timeout_while_sharing_the_encoder_is_retried(env, monkeypatch):
    slow, timed_out = new_request(chat_id=42), new_request(chat_id=43, url=URL2)
    fake = env.media.prepare_for_telegram

    async def scenario():
        converting, release = asyncio.Event(), asyncio.Event()

        async def prepare(path, workdir, *, max_bytes, probe_result=None):
            if f"smr-{slow}-" in path:  # holds the encoder for a while
                converting.set()
                await release.wait()
                return await fake(path, workdir, max_bytes=max_bytes)
            await converting.wait()  # its time ran out waiting for a slot
            raise downloader.DownloadTimeout(CONVERSION_TIMEOUT)

        monkeypatch.setattr(media, "prepare_for_telegram", prepare)
        first = asyncio.create_task(process(claim(slow)))
        second = await process(claim(timed_out))
        release.set()
        return await first, second

    first, second = run(scenario())
    assert first.outcome == pipeline.Outcome.DONE
    assert second.outcome == pipeline.Outcome.RETRY
    assert second.error_code == "conversion_timeout"
    assert status(timed_out) == "queued"


def test_work_directory_failure_is_retried_later(env, monkeypatch):
    blocker = env.tmp / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setattr(config, "TMP_DIR", str(blocker / "sub"))
    result, row = process_new()
    assert result.outcome == pipeline.Outcome.RETRY
    assert row["status"] == "queued"
    assert env.site.fetches == []


def test_unexpected_exception_fails_the_request_cleanly(env):
    env.site.fetch_errors.append(RuntimeError("a bug"))
    result, row = process_new()
    assert result.outcome == pipeline.Outcome.FAILED
    assert row["status"] == "failed"
    assert row["error_code"] == "failed"
    assert "RuntimeError" in row["error_message"]
    assert env.tg.reactions() == ["👀", "🍌"]
    assert env.tg.texts() == [messages.failure("failed")]


def test_row_without_url_is_failed_as_invalid(env):
    request_id = new_request()
    row = dict(claim(request_id))
    row["url"] = None
    result = run(process(row))
    assert result.error_code == "invalid_request"
    assert result.error_phase is None  # neither a download nor an upload
    assert db.get_request(request_id)["status"] == "failed"
    assert env.tg.calls == []


def test_flood_wait_at_start_defers_before_downloading(env):
    flood = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 900",
        "parameters": {"retry_after": 900},
    }
    env.tg.respond("set_message_reaction", flood)
    env.tg.respond("send_message", flood)
    before = time.time()
    result, row = process_new()
    assert result.outcome == pipeline.Outcome.RETRY
    assert result.error_phase == pipeline.Phase.SEND
    assert row["status"] == "queued"
    assert db.ts_from_iso(row["not_before"]) >= before + 899
    assert env.site.fetches == []
    assert env.tg.uploads() == []


def test_upload_flood_wait_is_retried_in_place(env):
    env.tg.respond(
        "send_video",
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 0.05},
        },
    )
    _, row = process_new()
    assert len(env.tg.of("send_video")) == 2
    assert row["status"] == "done"


def test_long_upload_flood_wait_fails_without_resending(env):
    env.tg.respond(
        "send_video",
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 3600",
            "parameters": {"retry_after": 3600},
        },
    )
    _, row = process_new()
    assert len(env.tg.of("send_video")) == 1
    assert row["status"] == "failed"
    assert row["error_code"] == "send_failed"
    assert env.tg.texts() == [messages.failure("send_failed", kind="video")]


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": False, "error_code": 502, "description": "Bad Gateway"},
        {"ok": False, "description": "network error: TimeoutError: "},
    ],
)
def test_ambiguous_upload_failure_is_never_resent(env, answer):
    env.tg.respond("send_video", answer)
    _, row = process_new()
    assert len(env.tg.uploads()) == 1
    assert row["status"] == "failed"
    assert row["error_code"] == "send_failed"


def test_bad_request_on_send_video_falls_back_to_a_document(env):
    env.tg.respond(
        "send_video",
        {"ok": False, "error_code": 400, "description": "Bad Request: VIDEO_INVALID"},
    )
    result, row = process_new()
    [(_, _, args, kwargs)] = env.tg.of("send_document")
    assert args[0].endswith("item-01.mp4")
    assert kwargs["caption"] == "guarda che bello"
    assert row["status"] == "done"
    assert row["media_type"] == "document"
    assert result.sent == {"document": 1}


def test_forbidden_chat_gets_no_feedback(env):
    env.tg.respond(
        "send_video",
        {
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot was blocked by the user",
        },
    )
    _, row = process_new()
    assert row["error_code"] == "forbidden"
    assert env.tg.reactions() == ["👀"]
    assert env.tg.texts() == []


def test_telegram_413_is_reported_as_too_large(env):
    env.tg.respond(
        "send_video",
        {"ok": False, "error_code": 413, "description": "Request Entity Too Large"},
    )
    _, row = process_new()
    assert row["error_code"] == "too_large"
    assert URL in env.tg.texts()[0]


def test_telegram_413_counts_as_an_upload_failure(env):
    env.tg.respond(
        "send_video",
        {"ok": False, "error_code": 413, "description": "Request Entity Too Large"},
    )
    uploads = metric("uploads_failed_total", code="too_large")
    downloads = metric("downloads_failed_total", code="too_large")
    too_large = metric("files_too_large_total")

    async def scenario():
        pool = await started_pool()
        try:
            result = await pool.submit(42, URL, provider=match(), original_message_id=1)
            await wait_until(lambda: status(result.request_id) == "failed")
            await wait_until(lambda: pool.running == 0)
        finally:
            await pool.stop()

    run(scenario())
    assert metric("uploads_failed_total", code="too_large") == uploads + 1
    assert metric("downloads_failed_total", code="too_large") == downloads
    assert metric("files_too_large_total") == too_large + 1


@pytest.mark.parametrize(
    ("where", "text"),
    [
        ("download", "Il video è troppo grande per essere scaricato."),
        (
            "convert",
            (
                "Il video è troppo grande per Telegram e non riesco a comprimerlo "
                "abbastanza."
            ),
        ),
        ("send", "Il video è troppo grande per Telegram."),
    ],
)
def test_too_large_text_says_where_it_was_refused(env, where, text):
    if where == "download":
        env.site.download_errors.append(downloader.TooLarge("about 900 MiB"))
    elif where == "convert":
        env.media.errors[1] = downloader.TooLarge("could not compress below 50 MiB")
    else:
        env.tg.respond(
            "send_video",
            {"ok": False, "error_code": 413, "description": "Request Entity Too Large"},
        )
    _, row = process_new()
    assert row["error_code"] == "too_large"
    assert env.tg.texts() == [f"{text} Puoi aprirlo qui: {URL}"]


def test_audio_is_never_called_a_video(env):
    env.site.extractor_key = "Soundcloud"
    env.site.items = [make_item(1, "audio", title="Podcast", uploader="Radio")]
    env.site.extensions[1] = "m4a"
    env.site.sizes[1] = 90 * MIB
    env.media.compress_to = 40 * MIB
    env.media.compress_audio = True
    env.tg.respond(
        "send_audio",
        {"ok": False, "error_code": 400, "description": "Bad Request: AUDIO_INVALID"},
    )
    _, row = process_new(
        provider="Soundcloud", url="https://soundcloud.com/a/b", description=None
    )
    [(_, _, _, kwargs)] = env.tg.of("send_audio")
    assert kwargs["caption"] == "Audio compresso: 90,0 MB → 40,0 MB"
    assert row["error_code"] == "send_failed"
    assert env.tg.texts() == [
        "Non sono riuscito a inviare l'audio su Telegram. Riprova più tardi."
    ]


def test_reaction_fallback_status_message_is_deleted_on_success(env):
    env.tg.reactions_ok = False
    process_new()
    [(_, _, args, kwargs)] = env.tg.of("send_message")
    assert args[0] == messages.PROCESSING
    assert kwargs["reply_to_message_id"] == 7
    status_id = env.tg.message_ids[0]
    assert [call[2][0] for call in env.tg.of("delete_message")] == [status_id]


@pytest.mark.parametrize("chat", [(42, "private"), (-1001, "supergroup")])
def test_reaction_fallback_status_message_becomes_the_explanation(env, chat):
    env.tg.reactions_ok = False
    env.site.fetch_errors.append(downloader.ContentUnavailable("gone"))
    process_new(chat_id=chat[0], chat_type=chat[1])
    status_id = env.tg.message_ids[0]
    [(_, _, args, _)] = env.tg.of("edit_message_text")
    assert args == (status_id, messages.failure("unavailable"))
    assert env.tg.of("delete_message") == []  # kept: it carries the reason
    assert env.tg.texts() == [messages.PROCESSING]


def test_group_with_reactions_shows_only_the_banana(env):
    env.site.fetch_errors.append(downloader.ContentUnavailable("gone"))
    process_new(chat_id=-1001, chat_type="group")
    assert env.tg.reactions() == ["👀", "🍌"]
    assert env.tg.of("send_message", "edit_message_text") == []


REACTION_INVALID = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: REACTION_INVALID",
}


def test_group_that_refuses_the_banana_gets_the_text_and_loses_the_eyes(env):
    # The admins allow 👀 but not 🍌: no stale 👀, and the failure is explained.
    env.tg.respond("set_message_reaction", DEFAULT, REACTION_INVALID)
    env.site.fetch_errors.append(downloader.AuthRequired("private post"))
    _, row = process_new(chat_id=-1001, chat_type="supergroup")
    assert row["status"] == "failed"
    assert env.tg.reactions() == ["👀", "🍌", None]
    assert env.tg.texts() == [messages.failure("auth_required")]


def test_refused_trophy_removes_the_eyes(env):
    env.tg.respond("set_message_reaction", DEFAULT, REACTION_INVALID)
    _, row = process_new(chat_id=-1001, chat_type="supergroup")
    assert row["status"] == "done"
    assert env.tg.reactions() == ["👀", "🏆", None]
    assert env.tg.texts() == []


def test_row_taken_away_before_the_upload_is_not_sent(env):
    request_id = new_request()
    row = claim(request_id)

    async def scenario():
        env.site.gate = asyncio.Event()
        env.site.started = asyncio.Event()
        task = asyncio.create_task(process(row))
        await env.site.started.wait()
        force(request_id, status="cancelled")  # someone else settled the row
        env.site.gate.set()
        return await task

    result = run(scenario())
    assert result.outcome == pipeline.Outcome.SKIPPED
    assert env.tg.uploads() == []
    assert status(request_id) == "cancelled"
    assert env.tg.reactions() == ["👀", None]  # our 👀 is removed


def test_cancelled_job_goes_back_to_the_queue(env):
    request_id = new_request()
    row = claim(request_id)

    async def scenario():
        env.site.gate = asyncio.Event()
        env.site.started = asyncio.Event()
        task = asyncio.create_task(process(row))
        await env.site.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    row = db.get_request(request_id)
    assert row["status"] == "queued"
    assert row["not_before"] is not None
    assert env.tg.uploads() == []
    assert workdirs(env) == []


def test_cancelled_upload_is_never_resent(env):
    request_id = new_request()
    row = claim(request_id)

    async def scenario():
        env.tg.upload_gate = asyncio.Event()
        env.tg.upload_started = asyncio.Event()
        task = asyncio.create_task(process(row))
        await env.tg.upload_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    row = db.get_request(request_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "interrupted_while_sending"


def test_cancelled_server_side_fetch_is_never_resent(env):
    env.site.items = [make_item(1, direct_url=DIRECT, filesize=5 * MIB)]
    request_id = new_request()
    row = claim(request_id)

    async def scenario():
        env.tg.upload_gate = asyncio.Event()
        env.tg.upload_started = asyncio.Event()
        task = asyncio.create_task(process(row))
        await env.tg.upload_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert db.get_request(request_id)["error_code"] == "interrupted_while_sending"


def test_interrupted_upload_does_not_keep_the_eyes(env):
    request_id = new_request()
    row = claim(request_id)

    async def scenario():
        env.tg.upload_gate = asyncio.Event()
        env.tg.upload_started = asyncio.Event()
        task = asyncio.create_task(process(row))
        await env.tg.upload_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert status(request_id) == "failed"  # never retried: 👀 would be a lie
    assert env.tg.reactions() == ["👀", None]
    assert env.tg.texts() == []


def test_job_cancelled_after_a_delivery_is_done_with_the_trophy(env, monkeypatch):
    env.site.items = [make_item(i) for i in range(1, 12)]  # two album parts
    request_id = new_request()
    row = claim(request_id)
    first_part = telegram_api.send_media_group  # the fake

    async def scenario():
        second_part, gate = asyncio.Event(), asyncio.Event()

        async def send_media_group(token, chat_id, entries, **kwargs):
            if env.tg.of("send_media_group"):
                second_part.set()
                await gate.wait()
            return await first_part(token, chat_id, entries, **kwargs)

        monkeypatch.setattr(telegram_api, "send_media_group", send_media_group)
        task = asyncio.create_task(process(row))
        await second_part.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    row = db.get_request(request_id)
    assert (row["status"], row["media_type"]) == ("done", None)
    assert env.tg.reactions() == ["👀", "🏆"]


def cancel_during_mark_sending(env, monkeypatch, *, commit_first):
    """Cancel the job while mark_sending runs in its thread."""
    real = db.mark_sending
    inside = threading.Event()

    def slow(request_id):
        if commit_first:  # the claim lands, then e.g. closing the connection
            answer = real(request_id)
            inside.set()
            time.sleep(0.3)
            return answer
        inside.set()  # cancelled before the claim lands
        time.sleep(0.3)
        return real(request_id)

    monkeypatch.setattr(db, "mark_sending", slow)
    request_id = new_request()
    row = claim(request_id)

    async def scenario():
        task = asyncio.create_task(process(row))
        await asyncio.to_thread(inside.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)  # the claim's thread finishes

    run(scenario())
    return db.get_request(request_id)


def test_claim_landing_during_a_cancellation_does_not_strand_the_row(env, monkeypatch):
    row = cancel_during_mark_sending(env, monkeypatch, commit_first=True)
    # A 'sending' row cannot be requeued, but it must not stay 'sending' (a
    # pending slot, then "interrupted while uploading" at the next start).
    assert (row["status"], row["error_code"]) == ("failed", "interrupted")
    assert "before anything was uploaded" in row["error_message"]
    assert env.tg.uploads() == []
    assert env.tg.reactions() == ["👀", None]


def test_cancellation_before_the_claim_lands_requeues_the_row(env, monkeypatch):
    row = cancel_during_mark_sending(env, monkeypatch, commit_first=False)
    assert row["status"] == "queued"  # the late claim is refused
    assert env.tg.uploads() == []
    assert env.tg.reactions() == ["👀"]  # it will be processed again


def test_work_directory_is_removed_after_a_failure(env):
    env.site.download_errors.append(downloader.NoMedia("nothing"))
    process_new()
    assert len(env.site.downloads) == 1
    assert workdirs(env) == []


def test_keep_downloaded_files_keeps_the_work_directory(env, monkeypatch):
    monkeypatch.setattr(config, "KEEP_DOWNLOADED_FILES", True)
    _, row = process_new()
    [name] = workdirs(env)
    assert name.startswith(f"smr-{row['id']}-")
    assert (env.work / name / "item-01.mp4").exists()
    assert (env.work / name).stat().st_mode & 0o777 == 0o700


# ---------------------------------------------------------------------------
# Outcomes the database refuses: never a stranded row
# ---------------------------------------------------------------------------
def test_delivery_whose_mark_done_keeps_failing_is_settled_later(
    env, monkeypatch, fast_retries
):
    broken = break_db(monkeypatch, "mark_done")
    request_id = new_request()
    result = run(process(claim(request_id)))
    assert broken["calls"] == pipeline.OUTCOME_WRITE_ATTEMPTS
    assert result.outcome == pipeline.Outcome.DONE
    assert (result.sent, result.bytes_sent) == ({"video": 1}, 2 * MIB)  # metrics
    assert env.tg.reactions() == ["👀", "🏆"]
    assert status(request_id) == "sending"  # not recorded yet...
    settlement = result.settlement
    assert (settlement.request_id, settlement.outcome) == (request_id, "done")
    broken["on"] = False
    assert settlement.write() is True  # ...but the pool can record it later
    row = db.get_request(request_id)
    assert (row["status"], row["media_type"]) == ("done", "video")
    assert row["sent_message_id"] == env.tg.message_ids[-1]
    assert db.count_active_for_chat(42) == 0


def test_url_fetch_delivery_is_recorded_once_the_database_recovers(
    env, monkeypatch, fast_retries
):
    env.site.items = [make_item(1, direct_url=DIRECT, filesize=5 * MIB)]
    break_db(monkeypatch, "mark_done", failures=pipeline.OUTCOME_WRITE_ATTEMPTS + 2)
    sent = metric("files_sent_total", media_type="video")

    async def scenario():
        pool = await started_pool()
        try:
            result = await pool.submit(42, URL, provider=match(), original_message_id=1)
            await wait_until(lambda: status(result.request_id) == "done")
            await wait_until(lambda: not pool._unsettled)
            return result
        finally:
            await pool.stop()

    result = run(scenario())
    # Left 'processing', the next start would have requeued it: posted twice.
    assert len(env.tg.uploads()) == 1
    assert db.get_request(result.request_id)["final_size"] == 5 * MIB
    assert metric("files_sent_total", media_type="video") == sent + 1


def test_retry_whose_deferral_keeps_failing_is_settled_later(
    env, monkeypatch, fast_retries
):
    env.site.fetch_errors.append(downloader.DownloadError("HTTP 503", retryable=True))
    broken = break_db(monkeypatch, "defer_request")
    request_id = new_request()
    result = run(process(claim(request_id)))  # nothing escapes the job
    assert result.outcome == pipeline.Outcome.RETRY
    assert result.settlement.outcome == "deferred"
    assert status(request_id) == "processing"
    broken["on"] = False
    assert result.settlement.write() is True
    row = db.get_request(request_id)
    assert row["status"] == "queued"
    assert row["not_before"] is not None


def test_failure_whose_write_keeps_failing_is_reported_and_settled_later(
    env, monkeypatch, fast_retries
):
    env.site.fetch_errors.append(downloader.ContentUnavailable("gone"))
    broken = break_db(monkeypatch, "mark_failed")
    request_id = new_request()
    result = run(process(claim(request_id)))
    assert result.outcome == pipeline.Outcome.FAILED
    assert env.tg.reactions() == ["👀", "🍌"]  # the user gets the outcome now
    assert env.tg.texts() == [messages.failure("unavailable")]
    broken["on"] = False
    assert result.settlement.write() is True
    row = db.get_request(request_id)
    assert (row["status"], row["error_code"]) == ("failed", "unavailable")


def test_invalid_row_whose_failure_cannot_be_written_is_settled_later(
    env, monkeypatch, fast_retries
):
    request_id = new_request()
    row = dict(claim(request_id))
    row["url"] = None
    broken = break_db(monkeypatch, "mark_failed")
    result = run(process(row))
    assert result.error_code == "invalid_request"
    assert result.settlement.outcome == "failed"
    broken["on"] = False
    result.settlement.write()
    assert status(request_id) == "failed"


def test_pool_settles_a_refused_deferral_and_retries_the_link(
    env, monkeypatch, fast_retries
):
    monkeypatch.setattr(config, "WORKER_RETRY_BACKOFF_SECONDS", 0.05)
    env.site.fetch_errors.append(downloader.DownloadError("HTTP 503", retryable=True))
    break_db(monkeypatch, "defer_request", failures=pipeline.OUTCOME_WRITE_ATTEMPTS + 1)

    async def scenario():
        pool = await started_pool()
        try:
            result = await pool.submit(42, URL, provider=match(), original_message_id=1)
            await wait_until(lambda: status(result.request_id) == "done")
            return result
        finally:
            await pool.stop()

    result = run(scenario())
    assert db.get_request(result.request_id)["attempts"] == 2
    assert len(env.tg.uploads()) == 1
    assert db.count_active_for_chat(42) == 0  # no pending slot held


@pytest.mark.parametrize("claimed", [False, True], ids=["processing", "sending"])
def test_row_of_a_crashed_job_is_settled_like_a_restart_would(
    env, monkeypatch, fast_retries, claimed
):
    async def crash(token, row, state):
        if claimed:
            db.mark_sending(row["id"])
        raise RuntimeError("a bug")

    monkeypatch.setattr(pipeline, "process", crash)

    def settled(request_id):
        row = db.get_request(request_id)
        return row["status"] == "failed" or row["not_before"] is not None

    async def scenario():
        pool = await started_pool()
        try:
            result = await pool.submit(42, URL, provider=match(), original_message_id=1)
            await wait_until(lambda: settled(result.request_id))
            return result
        finally:
            await pool.stop()

    result = run(scenario())
    row = db.get_request(result.request_id)
    if claimed:  # the upload may have happened: never resent
        assert (row["status"], row["error_code"]) == (
            "failed",
            "interrupted_while_sending",
        )
    else:  # back in the queue, with the backoff of a retry
        assert (row["status"], row["attempts"]) == ("queued", 1)
        assert db.ts_from_iso(row["not_before"]) > time.time() + 20


def test_stop_makes_a_last_attempt_to_record_pending_outcomes(
    env, monkeypatch, fast_retries
):
    monkeypatch.setattr(worker, "SETTLE_RETRY_SECONDS", 60.0)  # only stop() retries
    broken = break_db(monkeypatch, "mark_done")

    async def scenario():
        pool = await started_pool()
        result = await pool.submit(42, URL, provider=match(), original_message_id=1)
        await wait_until(lambda: result.request_id in pool._unsettled)
        broken["on"] = False
        await pool.stop()
        return result

    result = run(scenario())
    assert status(result.request_id) == "done"


# ---------------------------------------------------------------------------
# Pool: submit
# ---------------------------------------------------------------------------
def test_submit_records_the_request_and_the_pool_delivers_it(env):
    async def scenario():
        pool = await started_pool()
        try:
            result = await pool.submit(
                42,
                URL,
                chat_type="private",
                user_id=9,
                original_message_id=7,
                description="  ciao  ",
                provider=match(),
            )
            await wait_until(lambda: status(result.request_id) == "done")
            return result
        finally:
            await pool.stop()

    result = run(scenario())
    assert result.status == SubmitStatus.QUEUED
    assert result.accepted
    row = db.get_request(result.request_id)
    assert (row["chat_type"], row["user_id"], row["provider"]) == (
        "private",
        9,
        "Instagram",
    )
    assert row["normalized_url"] == URL
    assert row["description"] == "ciao"
    assert row["original_message_id"] == 7
    assert len(env.tg.uploads()) == 1


def test_submit_looks_up_the_provider_when_not_given(env, monkeypatch):
    async def lookup(url, **kwargs):
        return match(url, key="Vimeo") if "vimeo" in url else None

    monkeypatch.setattr(providers, "match_provider_async", lookup)

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        found = await pool.submit(42, "https://vimeo.com/1", original_message_id=1)
        missing = await pool.submit(42, "https://example.com/x", original_message_id=2)
        await pool.stop()
        return found, missing

    found, missing = run(scenario())
    assert found.status == SubmitStatus.QUEUED
    assert db.get_request(found.request_id)["provider"] == "Vimeo"
    assert missing == worker.SubmitResult(SubmitStatus.UNSUPPORTED)
    assert db.count_requests() == 1


def test_same_message_and_link_is_ignored_but_an_added_link_is_not(env):
    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        kwargs = {"chat_type": "private", "original_message_id": 7}
        first = await pool.submit(42, URL, provider=match(), **kwargs)
        again = await pool.submit(42, URL, provider=match(), **kwargs)
        added = await pool.submit(42, URL2, provider=match(URL2), **kwargs)
        await pool.stop()
        return first, again, added

    first, again, added = run(scenario())
    assert again.status == SubmitStatus.IGNORED
    assert again.request_id == first.request_id
    assert added.accepted
    assert db.count_requests() == 2


def test_edits_of_old_messages_are_ignored(env):
    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        stale = await pool.submit(
            42, URL, provider=match(), edited_message_date=time.time() - 6000
        )
        fresh = await pool.submit(
            42, URL, provider=match(), edited_message_date=time.time() - 60
        )
        await pool.stop()
        return stale, fresh

    stale, fresh = run(scenario())
    assert stale == worker.SubmitResult(SubmitStatus.IGNORED)
    assert fresh.status == SubmitStatus.QUEUED


def test_two_links_in_one_message_are_both_processed(env):
    async def scenario():
        pool = await started_pool()
        try:
            kwargs = {"chat_type": "private", "original_message_id": 7}
            first = await pool.submit(42, URL, provider=match(), **kwargs)
            second = await pool.submit(42, URL2, provider=match(URL2), **kwargs)
            third = await pool.submit(42, URL3, provider=match(URL3), **kwargs)
            await wait_until(lambda: status(first.request_id) == "done")
            assert status(second.request_id) == "queued"
            # time passes: the deferred rows become due and polling finds them
            for result in (second, third):
                force(result.request_id, not_before=db.iso_from_ts(time.time() - 1))
            await wait_until(
                lambda: status(second.request_id) == status(third.request_id) == "done"
            )
            return first, second, third
        finally:
            await pool.stop()

    first, second, third = run(scenario())
    assert first.status == SubmitStatus.QUEUED
    assert second.status == SubmitStatus.DEFERRED
    assert 9 <= second.delay_seconds <= 10
    assert second.window == (1, 10)
    assert third.status == SubmitStatus.DEFERRED
    assert 19 <= third.delay_seconds <= 20
    assert len(env.tg.uploads()) == 3
    # one short notice for the message, not one per deferred link
    assert env.tg.texts() == [messages.deferred(second.delay_seconds)]
    assert env.tg.texts() == ["Link in coda: lo elaboro tra 10 secondi."]


def test_deferred_link_is_processed_at_not_before(env, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMITS", ((1, 1),))
    monkeypatch.setattr(config, "WORKER_POLL_SECONDS", 30.0)  # the timer must do it

    async def scenario():
        pool = await started_pool()
        try:
            first = await pool.submit(42, URL, provider=match(), original_message_id=1)
            second = await pool.submit(
                42, URL2, provider=match(URL2), original_message_id=2
            )
            await wait_until(lambda: status(second.request_id) == "done", timeout=4)
            return first, second
        finally:
            await pool.stop()

    first, second = run(scenario())
    assert second.status == SubmitStatus.DEFERRED
    row = db.get_request(second.request_id)
    assert db.ts_from_iso(row["processing_started_at"]) >= db.ts_from_iso(
        row["not_before"]
    )
    assert status(first.request_id) == "done"


def test_concurrent_submits_of_one_chat_are_serialized(env):
    urls = [URL, URL2, URL3, URL4]

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        results = await asyncio.gather(
            *(
                pool.submit(42, url, provider=match(url), original_message_id=i)
                for i, url in enumerate(urls)
            )
        )
        locks = dict(pool._chat_locks)
        await pool.stop()
        return results, locks

    results, locks = run(scenario())
    statuses = [result.status for result in results]
    assert statuses.count(SubmitStatus.QUEUED) == 1
    assert statuses.count(SubmitStatus.DEFERRED) == 2
    assert statuses.count(SubmitStatus.PENDING_CAP) == 1
    delays = sorted(round(r.delay_seconds) for r in results if r.status == "deferred")
    assert delays == [10, 20]
    assert locks == {}  # per-chat locks are dropped when idle


def test_duplicate_inside_the_window_replies_to_the_earlier_post(env):
    async def scenario():
        pool = await started_pool()
        try:
            first = await pool.submit(42, URL, provider=match(), original_message_id=7)
            await wait_until(lambda: status(first.request_id) == "done")
            again = await pool.submit(42, URL, provider=match(), original_message_id=8)
            await wait_until(lambda: len(env.tg.texts()) == 1)
            return first, again
        finally:
            await pool.stop()

    first, again = run(scenario())
    assert again.status == SubmitStatus.DUPLICATE
    row = db.get_request(again.request_id)
    assert (row["status"], row["error_code"]) == ("duplicate", "duplicate")
    [(_, _, args, kwargs)] = env.tg.of("send_message")
    assert args[0] == "Questo link è già stato condiviso di recente."
    assert (
        kwargs["reply_to_message_id"]
        == db.get_request(first.request_id)["sent_message_id"]
    )
    assert len(env.tg.uploads()) == 1  # not posted again


def test_duplicate_of_a_waiting_link_and_notice_throttling(env):
    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        first = await pool.submit(42, URL, provider=match(), original_message_id=7)
        second = await pool.submit(42, URL, provider=match(), original_message_id=8)
        third = await pool.submit(42, URL, provider=match(), original_message_id=9)
        await pool.stop()
        return first, second, third

    first, second, third = run(scenario())
    assert first.status == SubmitStatus.QUEUED
    assert second.status == third.status == SubmitStatus.DUPLICATE
    [(_, _, args, kwargs)] = env.tg.of("send_message")  # throttled: one notice
    assert args[0] == messages.duplicate(in_progress=True)
    assert kwargs["reply_to_message_id"] == 8


def test_duplicates_in_groups_are_silent(env):
    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        kwargs = {"chat_type": "supergroup", "provider": match()}
        await pool.submit(-100, URL, original_message_id=1, **kwargs)
        result = await pool.submit(-100, URL, original_message_id=2, **kwargs)
        await pool.stop()
        return result

    assert run(scenario()).status == SubmitStatus.DUPLICATE
    assert env.tg.calls == []


def test_same_link_outside_the_window_or_after_a_failure_is_accepted(env):
    old = new_request(message_id=1)
    force(
        old,
        status="done",
        created_at=db.iso_from_ts(time.time() - 7200),
        processing_started_at=db.iso_from_ts(time.time() - 7200),
    )
    failed = new_request(chat_id=43, message_id=1, status="failed", error_code="failed")

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        outside = await pool.submit(42, URL, provider=match(), original_message_id=2)
        retried = await pool.submit(43, URL, provider=match(), original_message_id=2)
        await pool.stop()
        return outside, retried

    outside, retried = run(scenario())
    assert outside.accepted
    assert retried.accepted  # a failed attempt never makes a duplicate
    assert failed != retried.request_id


def test_duplicate_window_zero_disables_the_check(env, monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 0)

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        await pool.submit(42, URL, provider=match(), original_message_id=1)
        result = await pool.submit(42, URL, provider=match(), original_message_id=2)
        await pool.stop()
        return result

    assert run(scenario()).accepted


def test_pending_cap_refuses_only_the_new_link(env, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMITS", ())
    monkeypatch.setattr(config, "MAX_PENDING_PER_CHAT", 2)

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        results = [
            await pool.submit(42, url, provider=match(url), original_message_id=i)
            for i, url in enumerate((URL, URL2, URL3), start=1)
        ]
        other_chat = await pool.submit(
            -5, URL3, provider=match(URL3), chat_type="group"
        )
        for result in results[:2]:  # the queue drains
            claim(result.request_id)
            db.mark_done(result.request_id)
        later = await pool.submit(42, URL4, provider=match(URL4), original_message_id=9)
        await pool.stop()
        return results, other_chat, later

    results, other_chat, later = run(scenario())
    assert [r.status for r in results] == ["queued", "queued", "pending_cap"]
    row = db.get_request(results[2].request_id)
    assert (row["status"], row["error_code"]) == ("rate_limited", "pending_cap")
    assert other_chat.status == SubmitStatus.QUEUED  # the chat block is per link
    assert later.status == SubmitStatus.QUEUED
    [(_, chat_id, args, kwargs)] = env.tg.of("send_message")
    assert chat_id == 42
    assert args[0] == messages.pending_cap(2)
    assert args[0].startswith("Hai già 2 link in coda")
    assert kwargs["reply_to_message_id"] == 3
    # the refused link consumed no quota
    assert len(db.accepted_times(42, db.iso_from_ts(time.time() - 60))) == 3


def test_pending_cap_notice_does_not_silence_a_later_rate_limit_notice(
    env, monkeypatch
):
    monkeypatch.setattr(config, "RATE_LIMITS", ((2, 3600),))
    monkeypatch.setattr(config, "MAX_PENDING_PER_CHAT", 2)
    url5 = "https://www.instagram.com/reel/EEE/"

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        statuses = []
        for i, url in enumerate((URL, URL2, URL3, URL4), start=1):
            result = await pool.submit(
                42, url, provider=match(url), original_message_id=i
            )
            statuses.append(result.status)
            await asyncio.sleep(0.05)  # let the notice go out
            if i == 2:
                ids = [row["id"] for row in db.list_requests(status="queued")]
        for request_id in ids:  # the queue drains
            claim(request_id)
            db.mark_done(request_id)
        later = await pool.submit(42, url5, provider=match(url5), original_message_id=5)
        await wait_until(lambda: len(env.tg.texts()) == 2)
        await pool.stop()
        return statuses, later

    statuses, later = run(scenario())
    assert statuses == ["queued", "queued", "pending_cap", "pending_cap"]
    assert later.status == SubmitStatus.RATE_LIMITED
    # One pending-cap notice (throttled), then the rate limit and its wait.
    assert env.tg.texts() == [
        messages.pending_cap(2),
        messages.rate_limited(later.delay_seconds, later.window),
    ]


def test_rate_limited_link_is_refused_with_blocked_until(env, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMITS", ((1, 3600),))

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        first = await pool.submit(42, URL, provider=match(), original_message_id=1)
        second = await pool.submit(
            42, URL2, provider=match(URL2), original_message_id=2
        )
        third = await pool.submit(42, URL3, provider=match(URL3), original_message_id=3)
        group = await pool.submit(
            -7, URL, provider=match(), chat_type="group", original_message_id=1
        )
        group2 = await pool.submit(
            -7, URL2, provider=match(URL2), chat_type="group", original_message_id=2
        )
        await pool.stop()
        return first, second, third, group, group2

    first, second, third, group, group2 = run(scenario())
    assert first.status == SubmitStatus.QUEUED
    assert second.status == third.status == SubmitStatus.RATE_LIMITED
    assert 3590 <= second.delay_seconds <= 3600
    assert second.window == (1, 3600)
    row = db.get_request(second.request_id)
    assert (row["status"], row["error_code"]) == ("rate_limited", "rate_limited")
    limit = db.get_chat_limit(42)
    assert abs(db.ts_from_iso(limit["blocked_until"]) - (time.time() + 3600)) < 30
    [(_, chat_id, args, kwargs)] = env.tg.of("send_message")  # throttled, private only
    assert chat_id == 42
    assert args[0] == (
        "Hai inviato troppi link: puoi inviarne al massimo 1 ogni ora. "
        "Riprova tra 1 ora."
    )
    assert kwargs["reply_to_message_id"] == 2
    assert group.status == SubmitStatus.QUEUED
    assert group2.status == SubmitStatus.RATE_LIMITED
    # refusals never consume quota
    assert len(db.accepted_times(42, db.iso_from_ts(time.time() - 3600))) == 1


def test_every_submit_is_counted_once(env):
    received = metric("links_received_total")
    accepted = sum(
        metric("links_accepted_total", outcome=o) for o in ("queued", "deferred")
    )
    refused = sum(
        metric("links_refused_total", reason=r) for r in ("duplicate", "unsupported")
    )

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        await pool.submit(42, URL, provider=match(), original_message_id=1)
        await pool.submit(42, URL2, provider=match(URL2), original_message_id=2)
        await pool.submit(42, URL, provider=match(), original_message_id=3)
        await pool.submit(42, "https://unsupported.example/x")
        await pool.stop()

    run(scenario())
    assert metric("links_received_total") == received + 4
    assert (
        sum(metric("links_accepted_total", outcome=o) for o in ("queued", "deferred"))
        == accepted + 2
    )
    assert (
        sum(
            metric("links_refused_total", reason=r)
            for r in ("duplicate", "unsupported")
        )
        == refused + 2
    )


# ---------------------------------------------------------------------------
# Pool: dispatching from the database
# ---------------------------------------------------------------------------
def test_rows_requeued_by_another_process_are_picked_up(env):
    request_id = new_request(status="failed", error_code="failed")
    script = f"from src import db; print(db.requeue_requests([{request_id}]))"

    async def scenario():
        pool = await started_pool()
        try:
            await asyncio.sleep(0.2)
            assert status(request_id) == "failed"
            done = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            assert done.stdout.strip() == f"[{request_id}]"
            await wait_until(lambda: status(request_id) == "done", timeout=10)
        finally:
            await pool.stop()

    run(scenario())
    assert len(env.tg.uploads()) == 1


def test_polling_never_dispatches_a_request_twice(env, monkeypatch):
    monkeypatch.setattr(config, "WORKER_POLL_SECONDS", 0.01)
    chats = (42, 43, 44)

    async def scenario():
        env.site.gate = asyncio.Event()
        pool = await started_pool(workers=2)
        try:
            ids = [
                (
                    await pool.submit(
                        chat, URL, provider=match(), original_message_id=1
                    )
                ).request_id
                for chat in chats
            ]
            await wait_until(lambda: len(env.site.downloads) == 2)
            await asyncio.sleep(0.3)  # ~30 polls while both slots are busy
            assert len(env.site.downloads) == 2
            assert status(ids[2]) == "queued"
            assert pool.running == 2
            env.site.gate.set()
            await wait_until(lambda: all(status(i) == "done" for i in ids))
            return ids
        finally:
            await pool.stop()

    run(scenario())
    assert sorted(call[1] for call in env.tg.uploads()) == list(chats)
    assert len(env.site.fetches) == 3


def test_cancelled_rows_are_never_processed(env):
    async def scenario():
        env.site.gate = asyncio.Event()
        pool = await started_pool(workers=1)
        try:
            first = await pool.submit(42, URL, provider=match(), original_message_id=1)
            second = await pool.submit(43, URL, provider=match(), original_message_id=1)
            await wait_until(lambda: len(env.site.downloads) == 1)
            cancelled = await asyncio.to_thread(db.cancel_queued, chat_id=43, actor="t")
            assert cancelled == [second.request_id]
            env.site.gate.set()
            await wait_until(lambda: status(first.request_id) == "done")
            await asyncio.sleep(0.2)
            return second
        finally:
            await pool.stop()

    second = run(scenario())
    assert status(second.request_id) == "cancelled"
    assert [call[1] for call in env.tg.uploads()] == [42]


def test_retry_is_dispatched_again_when_due(env, monkeypatch):
    monkeypatch.setattr(config, "WORKER_RETRY_BACKOFF_SECONDS", 0.2)
    env.site.fetch_errors.append(downloader.DownloadError("HTTP 503", retryable=True))

    async def scenario():
        pool = await started_pool()
        try:
            result = await pool.submit(42, URL, provider=match(), original_message_id=1)
            await wait_until(lambda: status(result.request_id) == "done")
            return result
        finally:
            await pool.stop()

    result = run(scenario())
    row = db.get_request(result.request_id)
    assert row["attempts"] == 2
    assert len(env.site.fetches) == 2
    assert env.tg.reactions() == ["👀", "👀", "🏆"]


# ---------------------------------------------------------------------------
# Pool: lifecycle, heartbeat, recovery, maintenance
# ---------------------------------------------------------------------------
def test_heartbeat_publishes_the_pool_status(env):
    async def scenario():
        env.site.gate = asyncio.Event()
        pool = await started_pool()
        try:
            result = await pool.submit(42, URL, provider=match(), original_message_id=1)
            await wait_until(lambda: len(env.site.downloads) == 1)
            await wait_until(
                lambda: (db.get_worker_status() or {}).get("running", []) != []
            )
            running_status = db.get_worker_status()
            env.site.gate.set()
            await wait_until(lambda: status(result.request_id) == "done")
            return result, running_status
        finally:
            await pool.stop()

    result, payload = run(scenario())
    assert payload["pid"] == os.getpid()
    assert payload["version"] == src.__version__
    assert payload["started_at"]
    assert payload["mode"] == "polling"
    assert payload["workers"] == 2
    assert payload["state"] == "running"
    [job] = payload["running"]
    assert job["request_id"] == result.request_id
    assert job["chat_id"] == 42
    assert job["url"] == URL
    assert job["provider"] == "Instagram"
    assert job["stage"] == "downloading"
    assert job["started_at"]
    assert payload["queued"] == 0
    assert payload["deferred"] == 0
    assert payload["rate_limits"] == [[1, 10], [3, 60], [5, 3600], [20, 2592000]]
    assert payload["max_pending_per_chat"] == 3
    assert payload["duplicate_window_seconds"] == 5400
    assert payload["age_seconds"] < 5
    final = db.get_worker_status()
    assert final["state"] == "stopped"
    assert final["running"] == []


def test_heartbeat_publishes_the_mode_the_bot_runs_in(env):
    # `python -m src.main` runs the webhook whatever MODE (here polling) says.
    assert config.MODE == "polling"

    async def scenario():
        pool = worker.WorkerPool(TOKEN, mode="webhook")
        await pool.start()
        try:
            await wait_until(lambda: db.get_worker_status() is not None)
            return db.get_worker_status()["mode"], pool
        finally:
            await pool.stop()

    published, pool = run(scenario())
    assert published == "webhook"
    pool.mode = None  # unknown: derived from the configuration
    assert pool.status_payload()["mode"] == "polling"


def test_start_recovers_interrupted_requests(env):
    interrupted = new_request(chat_id=42, message_id=1)
    claim(interrupted)  # 'processing' when the previous process died
    uploading = new_request(chat_id=43, message_id=1)
    claim(uploading)
    db.mark_sending(uploading)  # it may have been posted already
    exhausted = new_request(chat_id=44, message_id=1)
    claim(exhausted)
    force(exhausted, attempts=3)

    async def scenario():
        pool = await started_pool()
        try:
            await wait_until(lambda: status(interrupted) == "done")
        finally:
            await pool.stop()

    run(scenario())
    assert db.get_request(interrupted)["attempts"] == 2
    row = db.get_request(uploading)
    assert (row["status"], row["error_code"]) == ("failed", "interrupted_while_sending")
    row = db.get_request(exhausted)
    assert (row["status"], row["error_code"]) == ("failed", "interrupted")
    assert [call[1] for call in env.tg.uploads()] == [42]  # never resent


def test_start_removes_the_eyes_of_requests_the_recovery_failed(env):
    requeued = new_request(chat_id=42, message_id=1)
    claim(requeued)
    uploading = new_request(chat_id=43, message_id=2)
    claim(uploading)
    db.mark_sending(uploading)
    exhausted = new_request(chat_id=44, message_id=3)
    claim(exhausted)
    force(exhausted, attempts=3)

    def cleared():
        return sorted(
            (call[1], call[2][0])
            for call in env.tg.of("set_message_reaction")
            if call[2][1] is None
        )

    async def scenario():
        pool = await started_pool()
        try:
            await wait_until(lambda: status(requeued) == "done")
            await wait_until(lambda: len(cleared()) == 2)
        finally:
            await pool.stop()

    run(scenario())
    assert cleared() == [(43, 2), (44, 3)]  # the requeued one got 👀 then 🏆
    assert [call[1] for call in env.tg.uploads()] == [42]


def test_start_without_rehydrate_aborts_waiting_requests(env, monkeypatch):
    monkeypatch.setattr(config, "WORKER_REHYDRATE_ON_START", False)
    waiting = new_request()

    async def scenario():
        pool = await started_pool()
        await asyncio.sleep(0.2)
        await pool.stop()

    run(scenario())
    assert status(waiting) == "aborted"
    assert env.tg.calls == []


def test_stop_waits_for_running_jobs(env):
    async def scenario():
        env.site.gate = asyncio.Event()
        pool = await started_pool()
        result = await pool.submit(42, URL, provider=match(), original_message_id=1)
        await wait_until(lambda: len(env.site.downloads) == 1)
        asyncio.get_running_loop().call_later(0.2, env.site.gate.set)
        await pool.stop(timeout=5)
        return result

    result = run(scenario())
    assert status(result.request_id) == "done"
    assert len(env.tg.uploads()) == 1


def test_stop_cancels_long_jobs_and_the_next_start_resumes_them(env):
    async def first_run():
        env.site.gate = asyncio.Event()
        pool = await started_pool()
        result = await pool.submit(42, URL, provider=match(), original_message_id=1)
        await wait_until(lambda: len(env.site.downloads) == 1)
        started = time.monotonic()
        await pool.stop(timeout=0.2)
        elapsed = time.monotonic() - started
        tasks = set(pool._tasks)
        late = await pool.submit(42, URL2, provider=match(URL2), original_message_id=2)
        await pool.stop()  # idempotent
        with pytest.raises(RuntimeError):
            await pool.start()
        return result, elapsed, tasks, pool.running, late

    result, elapsed, tasks, running, late = run(first_run())
    assert elapsed < 3
    assert tasks == set()
    assert running == 0
    assert late == worker.SubmitResult(SubmitStatus.CLOSED)
    row = db.get_request(result.request_id)
    assert row["status"] == "queued"  # recoverable
    assert env.tg.uploads() == []
    assert workdirs(env) == []

    async def second_run():
        env.site.gate = None
        pool = await started_pool()
        try:
            await wait_until(lambda: status(result.request_id) == "done")
        finally:
            await pool.stop()

    run(second_run())
    assert db.get_request(result.request_id)["attempts"] == 2
    assert len(env.tg.uploads()) == 1


def test_row_claimed_while_stopping_is_given_back(env, monkeypatch):
    request_id = new_request()
    real_claim = db.claim_next_request
    entered, release = threading.Event(), threading.Event()

    def slow_claim(now_iso=None):
        entered.set()
        release.wait(5)
        return real_claim(now_iso)

    monkeypatch.setattr(db, "claim_next_request", slow_claim)

    async def scenario():
        pool = await started_pool()
        await asyncio.to_thread(entered.wait, 5)  # the claim is in flight
        stopping = asyncio.create_task(pool.stop(timeout=1))
        await asyncio.sleep(0.05)
        release.set()
        await stopping

    run(scenario())
    row = db.get_request(request_id)
    assert row["status"] == "queued"
    assert env.site.fetches == []
    assert env.tg.calls == []


def test_stop_during_start_starts_nothing(env, monkeypatch):
    real_recover = db.recover_interrupted
    entered, release = threading.Event(), threading.Event()

    def slow_recover(**kwargs):
        entered.set()
        release.wait(5)
        return real_recover(**kwargs)

    monkeypatch.setattr(db, "recover_interrupted", slow_recover)
    request_id = new_request()

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        starting = asyncio.create_task(pool.start())
        await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.create_task(pool.stop(timeout=1))
        await asyncio.sleep(0.05)
        release.set()
        await starting
        await stopping
        await asyncio.sleep(0.2)
        return pool

    pool = run(scenario())
    assert pool._tasks == set()
    assert status(request_id) == "queued"
    assert env.site.fetches == []


def test_stop_without_start_is_harmless(env):
    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        await pool.stop()
        await pool.shutdown()
        return pool

    pool = run(scenario())
    assert pool.closing
    assert db.get_worker_status() is None


def test_workers_default_comes_from_config(env, monkeypatch):
    monkeypatch.setattr(config, "WORKERS", 5)
    assert worker.WorkerPool(TOKEN).workers == 5
    assert worker.WorkerPool(TOKEN, workers=1).workers == 1


def test_sweep_removes_only_stale_pipeline_directories(tmp_path):
    old, fresh, kept = tmp_path / "smr-1-a", tmp_path / "smr-2-b", tmp_path / "smr-3-c"
    other = tmp_path / "unrelated"
    for path in (old, fresh, kept, other):
        path.mkdir()
        (path / "file").write_bytes(b"x")
    (tmp_path / "smr-file").write_bytes(b"x")
    past = time.time() - 7200
    for path in (old, kept, other):
        os.utime(path, (past, past))
    removed = worker.sweep_workdirs(str(tmp_path), keep={str(kept)}, older_than=3600)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists() and kept.exists() and other.exists()
    assert (tmp_path / "smr-file").exists()
    assert worker.sweep_workdirs(str(tmp_path / "missing"), keep=(), older_than=0) == 0


def test_start_creates_tmp_dir_and_removes_leftovers(env):
    async def scenario():
        pool = await started_pool()
        exists = env.work.is_dir()
        await pool.stop()
        return exists

    assert run(scenario())
    leftover = env.work / "smr-9-crashed"
    leftover.mkdir()
    (env.work / "unrelated").mkdir()

    async def again():
        pool = await started_pool()
        await pool.stop()

    run(again())
    assert workdirs(env) == ["unrelated"]


def test_start_keeps_recent_directories_with_keep_downloaded_files(env, monkeypatch):
    monkeypatch.setattr(config, "KEEP_DOWNLOADED_FILES", True)
    monkeypatch.setattr(config, "TMP_RETENTION_HOURS", 1)
    recent, old = env.work / "smr-1-recent", env.work / "smr-2-old"
    recent.mkdir(parents=True)
    old.mkdir()
    past = time.time() - 7200
    os.utime(old, (past, past))

    async def scenario():
        pool = await started_pool()
        await pool.stop()

    run(scenario())
    assert workdirs(env) == ["smr-1-recent"]


def test_maintenance_prunes_once_a_day(env, monkeypatch):
    calls = []

    def fake_prune(**kwargs):
        calls.append(kwargs)
        return {
            "updates": 0,
            "requests": 0,
            "request_events": 0,
            "processed_messages": 0,
        }

    monkeypatch.setattr(db, "prune", fake_prune)

    async def scenario():
        pool = worker.WorkerPool(TOKEN)
        first = await pool.run_maintenance()
        await pool.run_maintenance()
        await pool.run_maintenance(force_prune=True)
        return first

    first = run(scenario())
    assert calls == [{"updates_days": 14.0, "requests_days": 0.0}] * 2
    assert first["pruned"] == {
        "updates": 0,
        "requests": 0,
        "request_events": 0,
        "processed_messages": 0,
    }


def test_maintenance_task_runs_after_start(env, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "MAINTENANCE_FIRST_RUN_SECONDS", 0.01)
    monkeypatch.setattr(db, "prune", lambda **kwargs: calls.append(kwargs) or {})

    async def scenario():
        pool = await started_pool()
        try:
            await wait_until(lambda: calls)
        finally:
            await pool.stop()

    run(scenario())
    assert calls[0] == {"updates_days": 14.0, "requests_days": 0.0}


def test_job_metrics_follow_the_outcome(env):
    done = metric("processed_links_total", outcome="done")
    failed = metric("downloads_failed_total", code="unavailable")
    sent = metric("files_sent_total", media_type="video")
    env.site.fetch_errors.append(downloader.ContentUnavailable("gone"))

    async def scenario():
        pool = await started_pool()
        try:
            bad = await pool.submit(42, URL, provider=match(), original_message_id=1)
            good = await pool.submit(43, URL, provider=match(), original_message_id=1)
            await wait_until(
                lambda: status(bad.request_id) == "failed"
                and status(good.request_id) == "done"
            )
        finally:
            await pool.stop()

    run(scenario())
    assert metric("processed_links_total", outcome="done") == done + 1
    assert metric("downloads_failed_total", code="unavailable") == failed + 1
    assert metric("files_sent_total", media_type="video") == sent + 1
