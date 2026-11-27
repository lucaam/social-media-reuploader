"""The pipeline with the real src.media (ffprobe/ffmpeg) on generated samples.

Only yt-dlp and Telegram are faked. Skipped when ffmpeg/ffprobe are missing.
"""

import asyncio
import os
import shutil
import subprocess

import pytest

from src import config, db, downloader, messages, pipeline, telegram_api
from src.downloader import DownloadedFile, MediaInfo, MediaItem

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")

TOKEN = "123456:TEST-token-for-the-media-tests-00000"
MIB = 1024 * 1024
URL = "https://vimeo.com/123456"


def generate(path, *args):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", *args, str(path)],
        check=True,
        timeout=180,
    )


LAVFI_VIDEO = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25"]
LAVFI_AUDIO = ["-f", "lavfi", "-i", "sine=frequency=440"]


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    files = {
        "h264": root / "h264.mp4",
        "vp9": root / "vp9.webm",
        "big": root / "big.mp4",
        "audio": root / "audio.m4a",
    }
    generate(
        files["h264"],
        *LAVFI_VIDEO,
        *LAVFI_AUDIO,
        *("-t", "2", "-c:v", "libx264", "-preset", "ultrafast"),
        *("-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"),
    )
    generate(
        files["vp9"],
        *LAVFI_VIDEO,
        *LAVFI_AUDIO,
        *("-t", "2", "-c:v", "libvpx-vp9", "-deadline", "realtime"),
        *("-cpu-used", "8", "-c:a", "libopus", "-shortest"),
    )
    generate(
        files["big"],
        *("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30"),
        *LAVFI_AUDIO,
        *("-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "8M"),
        *("-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"),
    )
    generate(files["audio"], *LAVFI_AUDIO, "-t", "2", "-c:a", "aac")
    return files


def _file_facts(path):
    if not path or not os.path.isfile(path):
        return None
    return {"size": os.path.getsize(path), "name": os.path.basename(path)}


class Telegram:
    def __init__(self):
        self.calls = []
        self._next_id = 100

    def install(self, monkeypatch):
        for name in (
            "send_message",
            "send_video",
            "send_audio",
            "send_document",
            "send_media_group",
            "set_message_reaction",
            "delete_message",
            "edit_message_text",
        ):
            monkeypatch.setattr(telegram_api, name, self._fake(name))

    def _fake(self, name):
        async def call(token, chat_id, *args, **kwargs):
            facts = {}
            if name in ("send_video", "send_audio", "send_document"):
                facts["file"] = await asyncio.to_thread(_file_facts, args[0])
                facts["thumbnail"] = await asyncio.to_thread(
                    _file_facts, kwargs.get("thumbnail")
                )
            self.calls.append((name, args, kwargs, facts))
            self._next_id += 1
            return {"ok": True, "result": {"message_id": self._next_id}}

        return call

    def of(self, name):
        return [call for call in self.calls if call[0] == name]


@pytest.fixture
def env(tmp_path, monkeypatch, samples):
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "requests.db"))
    db.init_db()
    for name, value in {
        "TMP_DIR": str(tmp_path / "work"),
        "KEEP_DOWNLOADED_FILES": False,
        "TELEGRAM_MAX_FILE_SIZE": 50 * MIB,
        "TELEGRAM_SERVER_FETCH": True,
        "WORKER_GENERATE_THUMBNAIL": True,
        "WORKER_MAX_ATTEMPTS": 3,
        "SIMPLE_YTDLP_ONLY": False,
        "TRANSCODE_PRESET": "veryfast",
        "TRANSCODE_TIMEOUT": 300,
        "TRANSCODE_MAX_SIDE": 1280,
        "TRANSCODE_CONCURRENCY": 1,
        "MAX_DURATION_SECONDS": 3600,
    }.items():
        monkeypatch.setattr(config, name, value, raising=False)
    telegram = Telegram()
    telegram.install(monkeypatch)
    source = {"path": samples["h264"], "kind": "video"}

    async def fetch_info(url, workdir, *, timeout=None):
        item = MediaItem(
            index=1,
            id="x",
            title="Sample",
            kind=source["kind"],
            duration=2.0,
            width=None,
            height=None,
            filesize=None,
            direct_url=None,
            direct_ext=None,
            http_headers={},
            thumbnail=None,
            uploader="Tester",
            webpage_url=None,
        )
        return MediaInfo(
            url, "Vimeo", "Sample", False, [item], os.path.join(workdir, "i.json"), {}
        )

    async def download(info, workdir, *, items=None, timeout=None):
        suffix = os.path.splitext(str(source["path"]))[1]
        target = os.path.join(workdir, f"item-01{suffix}")
        await asyncio.to_thread(shutil.copyfile, source["path"], target)
        size = await asyncio.to_thread(os.path.getsize, target)
        return [DownloadedFile(target, size, info.items[0])]

    monkeypatch.setattr(downloader, "fetch_info", fetch_info)
    monkeypatch.setattr(downloader, "download", download)
    return telegram, source, tmp_path


def deliver(description="ciao"):
    request_id = db.create_request(
        42,
        URL,
        normalized_url=URL,
        provider="Vimeo",
        chat_type="private",
        original_message_id=7,
        description=description,
    )
    row = db.claim_request(request_id)
    result = asyncio.run(pipeline.process(TOKEN, row, pipeline.JobState.from_row(row)))
    return result, db.get_request(request_id)


def test_compatible_mp4_is_sent_as_is_with_dimensions_and_thumbnail(env):
    telegram, _, tmp_path = env
    result, row = deliver()
    assert result.outcome == pipeline.Outcome.DONE
    [(_, _args, kwargs, facts)] = telegram.of("send_video")
    assert facts["file"]["name"] == "item-01.mp4"
    assert (kwargs["width"], kwargs["height"]) == (320, 240)
    assert kwargs["duration"] == pytest.approx(2.0, abs=0.2)
    assert kwargs["caption"] == "ciao"
    assert facts["thumbnail"] is not None
    assert facts["thumbnail"]["name"].endswith(".jpg")
    assert row["compressed"] == 0
    assert row["original_size"] == row["final_size"]
    assert os.listdir(tmp_path / "work") == []


def test_vp9_webm_is_converted_to_mp4(env, samples):
    telegram, source, _ = env
    source["path"] = samples["vp9"]
    _, row = deliver()
    [(_, _, kwargs, facts)] = telegram.of("send_video")
    assert facts["file"]["name"].endswith(".mp4")
    assert (kwargs["width"], kwargs["height"]) == (320, 240)
    assert row["status"] == "done"
    assert row["compressed"] == 0  # a codec conversion is not a compression


def test_oversized_video_is_compressed_and_the_caption_says_so(
    env, samples, monkeypatch
):
    telegram, source, _ = env
    source["path"] = samples["big"]
    original = source["path"].stat().st_size
    budget = MIB
    assert original > budget
    monkeypatch.setattr(config, "TELEGRAM_MAX_FILE_SIZE", budget)
    _, row = deliver(description="grande")
    [(_, _, kwargs, facts)] = telegram.of("send_video")
    assert facts["file"]["size"] <= budget
    note = messages.compression_note(original, facts["file"]["size"])
    assert kwargs["caption"] == f"grande\n\n{note}"
    assert row["compressed"] == 1
    assert row["original_size"] == original
    assert row["final_size"] == facts["file"]["size"]


def test_audio_is_sent_with_send_audio(env, samples):
    telegram, source, _ = env
    source["path"] = samples["audio"]
    source["kind"] = "audio"
    _, row = deliver()
    [(_, _, kwargs, facts)] = telegram.of("send_audio")
    assert facts["file"]["name"].endswith(".m4a")
    assert kwargs["title"] == "Sample"
    assert kwargs["performer"] == "Tester"
    assert row["media_type"] == "audio"


def test_impossible_budget_fails_as_too_large_with_the_link(env, samples, monkeypatch):
    telegram, source, _ = env
    source["path"] = samples["big"]
    monkeypatch.setattr(config, "TELEGRAM_MAX_FILE_SIZE", 20 * 1024)
    _, row = deliver()
    assert row["status"] == "failed"
    assert row["error_code"] == "too_large"
    [(_, args, _, _)] = telegram.of("send_message")
    assert URL in args[0]
    assert telegram.of("send_video") == []
