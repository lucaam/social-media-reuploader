"""Tests for src.media (ffprobe parsing, Telegram compatibility, encode planning).

The pure parts run everywhere; the end-to-end conversions generate tiny media
with ffmpeg's lavfi sources at test time and are skipped without ffmpeg.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import pathlib
import shutil
import subprocess
import time

import pytest

from src import config, downloader, media
from src.downloader import ConversionFailed, DownloadTimeout, NoMedia, TooLarge
from src.media import (
    Action,
    ProbeResult,
    VideoPlan,
    fit_dimensions,
    is_telegram_compatible,
    parse_probe,
    payload_kbps,
    plan_audio,
    plan_audio_encode,
    plan_dimensions,
    plan_video_encode,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ffprobe"
MIB = 1024 * 1024
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    for name, value in {
        "TRANSCODE_MAX_SIDE": 1280,
        "TRANSCODE_PRESET": "veryfast",
        "TRANSCODE_TIMEOUT": 300,
        "TRANSCODE_CONCURRENCY": 1,
        "SIMPLE_YTDLP_ONLY": False,
    }.items():
        monkeypatch.setattr(config, name, value, raising=False)


def run(coro):
    return asyncio.run(coro)


def make_probe(**overrides) -> ProbeResult:
    values = {
        "has_video": True,
        "has_audio": True,
        "duration": 60.0,
        "width": 1920,
        "height": 1080,
        "display_width": 1920,
        "display_height": 1080,
        "vcodec": "h264",
        "acodec": "aac",
        "container": "mp4",
        "size": 10 * MIB,
        "rotation": 0,
        "pix_fmt": "yuv420p",
        "sample_aspect_ratio": 1.0,
        "fps": 30.0,
        "audio_bit_rate": 128_000,
        "audio_channels": 2,
        "video_index": 0,
        "audio_index": 1,
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
    }
    values.update(overrides)
    return ProbeResult(**values)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# ffprobe parsing
# ---------------------------------------------------------------------------


def test_parse_rotated_display_matrix():
    p = parse_probe(
        load_fixture("h264_rotated_display_matrix"), size=8379, path="a.mp4"
    )
    assert (p.has_video, p.has_audio) == (True, False)
    assert (p.width, p.height) == (320, 240)
    # display matrix rotation 90 is counter-clockwise: 270 degrees clockwise
    assert p.rotation == 270
    assert (p.display_width, p.display_height) == (240, 320)
    assert p.container == "mp4"
    assert p.vcodec == "h264" and p.pix_fmt == "yuv420p"
    assert p.fps == pytest.approx(29.97, abs=0.01)
    assert p.duration == pytest.approx(1.001)
    assert p.size == 8379
    assert p.video_index == 0 and p.audio_index is None


def test_parse_legacy_rotate_tag():
    p = parse_probe(load_fixture("h264_rotated_legacy_tag"), size=1, path="a.mp4")
    assert p.rotation == 90
    assert (p.display_width, p.display_height) == (240, 320)


def test_parse_anamorphic_sample_aspect_ratio():
    p = parse_probe(load_fixture("h264_aac_sar_4_3"), size=1, path="a.mp4")
    assert (p.width, p.height) == (480, 360)
    assert p.sample_aspect_ratio == pytest.approx(4 / 3)
    assert not p.square_pixels
    assert (p.display_width, p.display_height) == (640, 360)
    assert p.acodec == "aac" and p.audio_channels == 1


def test_parse_ignores_attached_cover_art():
    p = parse_probe(load_fixture("aac_with_cover_art"), size=1, path="song.m4a")
    assert p.has_video is False
    assert p.has_audio is True
    assert p.acodec == "aac"
    assert p.vcodec is None and p.display_width is None
    assert p.container == "mp4"


def test_parse_webm_and_matroska_container_names():
    data = load_fixture("vp9_opus_webm")
    assert parse_probe(data, size=1, path="x.webm").container == "webm"
    assert parse_probe(data, size=1, path="x.mkv").container == "matroska"
    p = parse_probe(data, size=1, path="x.webm")
    assert (p.vcodec, p.acodec) == ("vp9", "opus")
    assert p.duration is not None and p.duration > 0


def test_parse_empty_probe():
    p = parse_probe({}, size=0)
    assert (p.has_video, p.has_audio, p.duration, p.container) == (
        False,
        False,
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Telegram compatibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "path", "expected"),
    [
        ({}, "v.mp4", True),
        ({}, "v.m4v", True),
        ({}, "v.mkv", False),  # name/container mismatch: remux
        ({"container": "matroska"}, "v.mp4", False),
        ({"vcodec": "hevc"}, "v.mp4", False),
        ({"vcodec": "vp9"}, "v.mp4", False),
        ({"acodec": "opus"}, "v.mp4", False),
        ({"acodec": "mp3"}, "v.mp4", True),
        ({"has_audio": False, "acodec": None}, "v.mp4", True),
        ({"pix_fmt": "yuv420p10le"}, "v.mp4", False),
        ({"pix_fmt": "yuv444p"}, "v.mp4", False),
        ({"sample_aspect_ratio": 4 / 3}, "v.mp4", False),
        (
            {"rotation": 90, "display_width": 1080, "display_height": 1920},
            "v.mp4",
            True,
        ),
        ({"has_video": False, "vcodec": None}, "a.m4a", True),
        ({"has_video": False, "vcodec": None}, "a.mp4", False),
        (
            {"has_video": False, "vcodec": None, "acodec": "mp3", "container": "mp3"},
            "a.mp3",
            True,
        ),
        (
            {"has_video": False, "vcodec": None, "acodec": "opus", "container": "ogg"},
            "a.ogg",
            False,
        ),
        ({"has_video": False, "has_audio": False}, "x.mp4", False),
    ],
)
def test_is_telegram_compatible(overrides, path, expected):
    assert is_telegram_compatible(make_probe(**overrides), path) is expected


# ---------------------------------------------------------------------------
# Encode planning (pure)
# ---------------------------------------------------------------------------


def test_payload_kbps_accounts_for_overhead_and_duration():
    budget = 50 * MIB
    kbps_60 = payload_kbps(budget, 60)
    usable = budget * media.RATE_SAFETY - media.CONTAINER_OVERHEAD_BYTES - 600 * 60
    assert kbps_60 == pytest.approx(usable * 8 / 60 / 1000)
    assert payload_kbps(budget, 600) < kbps_60 / 9
    assert payload_kbps(budget, 0) == 0
    assert payload_kbps(10_000, 60) == 0  # overhead larger than the budget


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (5000, (128, 2)),
        (1500, (128, 2)),
        (1000, (96, 2)),
        (500, (64, 2)),
        (300, (48, 1)),
        (90, (32, 1)),
    ],
)
def test_audio_bitrate_scales_down_for_long_media(total, expected):
    assert plan_audio(total) == expected


def test_audio_plan_never_exceeds_the_source():
    assert plan_audio(5000, source_kbps=64, source_channels=2) == (64, 2)
    assert plan_audio(5000, source_kbps=20, source_channels=1) == (
        media.MIN_AUDIO_KBPS,
        1,
    )
    assert plan_audio(5000, source_channels=1) == (128, 1)


@pytest.mark.parametrize(
    ("size", "side", "expected"),
    [
        ((1920, 1080), 1280, (1280, 720)),
        ((1080, 1920), 1280, (720, 1280)),  # portrait keeps its orientation
        ((1080, 1080), 1280, (1080, 1080)),  # square stays square, no padding
        ((1080, 1080), 720, (720, 720)),
        ((853, 480), 2000, (852, 480)),  # even dimensions for x264
        ((320, 240), 1280, (320, 240)),  # never upscale
        ((1440, 1080), 1280, (1280, 960)),  # 4:3 keeps 4:3
    ],
)
def test_fit_dimensions(size, side, expected):
    assert fit_dimensions(*size, side) == expected


def test_plan_dimensions_downscales_only_when_needed():
    # plenty of bits: keep the source resolution (even above TRANSCODE_MAX_SIDE)
    assert plan_dimensions(1920, 1080, 8000, 30, downscale_side=1280) == (1920, 1080)
    # first step is TRANSCODE_MAX_SIDE
    assert plan_dimensions(1920, 1080, 2000, 30, downscale_side=1280) == (1280, 720)
    assert plan_dimensions(1920, 1080, 600, 30, downscale_side=1280) == (640, 360)
    assert plan_dimensions(1080, 1080, 1500, 30, downscale_side=1280) == (960, 960)
    assert plan_dimensions(720, 1280, 100, 30, downscale_side=1280) == (180, 320)
    assert plan_dimensions(720, 1280, 60, 30, downscale_side=1280) == (144, 256)
    # below the smallest step: the smallest size is used anyway
    assert plan_dimensions(720, 1280, 10, 30, downscale_side=1280) == (144, 256)
    # a custom TRANSCODE_MAX_SIDE is the first downscale step
    assert plan_dimensions(1920, 1080, 2500, 30, downscale_side=1600) == (1600, 900)
    # the hard cap applies even with infinite bitrate
    assert plan_dimensions(3840, 2160, float("inf"), 30, downscale_side=1280) == (
        1920,
        1080,
    )


def test_plan_compress_long_video():
    p = make_probe(duration=600.0)
    plan = plan_video_encode(p, 50 * MIB, size_targeted=True)
    total = payload_kbps(50 * MIB, 600)
    assert plan.crf is None
    assert plan.audio_kbps == 64 and plan.audio_channels == 2  # scaled down from 128k
    assert plan.video_kbps == int(total - 64)
    assert (plan.width, plan.height) == (640, 360)
    assert plan.fps is None  # 30 fps source
    assert not plan.copy_audio


def test_plan_compress_caps_frame_rate_and_copies_cheap_audio():
    p = make_probe(duration=120.0, fps=60.0, audio_bit_rate=64_000)
    plan = plan_video_encode(p, 50 * MIB, size_targeted=True)
    assert plan.fps == 30
    assert plan.copy_audio and plan.audio_kbps is None


def test_plan_compress_is_infeasible_for_impossible_budgets():
    with pytest.raises(TooLarge, match="cannot fit"):
        plan_video_encode(make_probe(duration=4 * 3600.0), 50 * MIB, size_targeted=True)
    with pytest.raises(TooLarge):
        plan_video_encode(make_probe(duration=10.0), 20_000, size_targeted=True)
    with pytest.raises(TooLarge, match="unknown duration"):
        plan_video_encode(make_probe(duration=None), 50 * MIB, size_targeted=True)


def test_plan_compress_retry_scale_lowers_the_bitrate():
    p = make_probe(duration=300.0)
    first = plan_video_encode(p, 50 * MIB, size_targeted=True)
    second = plan_video_encode(p, 50 * MIB, size_targeted=True, bitrate_scale=0.7)
    assert second.video_kbps < first.video_kbps * 0.75


def test_plan_compress_uses_display_size_for_rotated_and_anamorphic():
    rotated = make_probe(
        width=1920, height=1080, display_width=1080, display_height=1920, rotation=90
    )
    plan = plan_video_encode(rotated, 50 * MIB, size_targeted=True)
    assert plan.width < plan.height
    anamorphic = make_probe(
        width=480,
        height=360,
        display_width=640,
        display_height=360,
        sample_aspect_ratio=4 / 3,
        duration=10.0,
    )
    plan = plan_video_encode(anamorphic, 50 * MIB, size_targeted=True)
    assert (plan.width, plan.height) == (640, 360)


def test_plan_crf_transcode_caps_the_bitrate_by_budget():
    p = make_probe(vcodec="vp9", acodec="opus", duration=120.0, audio_bit_rate=96_000)
    plan = plan_video_encode(p, 50 * MIB, size_targeted=False)
    assert plan.crf == media.CRF
    assert not plan.copy_audio and plan.audio_kbps is not None
    assert 0 < plan.video_kbps < payload_kbps(50 * MIB, 120)
    assert (plan.width, plan.height) == (1920, 1080)
    plan_aac = plan_video_encode(
        make_probe(vcodec="vp9", duration=120.0), 50 * MIB, size_targeted=False
    )
    assert plan_aac.copy_audio
    no_duration = plan_video_encode(
        make_probe(vcodec="vp9", duration=None), 50 * MIB, size_targeted=False
    )
    assert no_duration.video_kbps == 0  # CRF without cap


def test_plan_audio_only():
    p = make_probe(
        has_video=False,
        vcodec=None,
        acodec="opus",
        duration=3600.0,
        audio_bit_rate=160_000,
    )
    plan = plan_audio_encode(p, 50 * MIB, size_targeted=True)
    assert plan.kbps == int(payload_kbps(50 * MIB, 3600))
    assert plan.channels == 2
    with pytest.raises(TooLarge):
        plan_audio_encode(p, 5 * MIB, size_targeted=True)
    small = make_probe(
        has_video=False,
        vcodec=None,
        acodec="opus",
        duration=60.0,
        audio_bit_rate=48_000,
    )
    assert plan_audio_encode(small, 50 * MIB, size_targeted=False).kbps == 64


def test_encode_argv_never_pads_and_uses_settings():
    p = make_probe(duration=600.0, video_index=0, audio_index=1)
    plan = plan_video_encode(p, 50 * MIB, size_targeted=True)
    argv = media.video_encode_argv(
        "ffmpeg", "/w/in.webm", "/w/out.mp4", p, plan, preset="veryfast"
    )
    joined = " ".join(argv)
    assert "pad" not in joined
    assert argv[argv.index("-vf") + 1] == "scale=640:360,setsar=1"
    assert argv[argv.index("-preset") + 1] == "veryfast"
    assert argv[argv.index("-profile:v") + 1] == "high"
    assert argv[argv.index("-b:v") + 1] == f"{plan.video_kbps}k"
    assert argv[argv.index("-c:a") + 1] == "aac"
    first_map = argv.index("-map")
    assert argv[first_map : first_map + 4] == ["-map", "0:0", "-map", "0:1"]
    assert argv[argv.index("-i") + 1] == "file:/w/in.webm"
    assert argv[-3:] == ["-f", "mp4", "file:/w/out.mp4"]
    assert "+faststart" in argv and "-nostdin" in argv
    crf_plan = VideoPlan(1280, 720, None, 2000, 23, None, None, copy_audio=True)
    crf = media.video_encode_argv(
        "ffmpeg", "/w/in", "/w/out", p, crf_plan, preset="medium"
    )
    assert crf[crf.index("-crf") + 1] == "23"
    assert crf[crf.index("-maxrate") + 1] == "2000k"
    assert crf[crf.index("-c:a") + 1] == "copy"
    silent = dataclasses.replace(p, has_audio=False, acodec=None, audio_index=None)
    silent_argv = media.video_encode_argv(
        "ffmpeg", "/w/in", "/w/out", silent, plan, preset="fast"
    )
    assert "-an" in silent_argv and "0:1" not in silent_argv


def test_remux_argv_for_audio_and_video():
    video = media.remux_argv("ffmpeg", "/w/in.mkv", "/w/out.mp4", make_probe())
    assert video[video.index("-c") + 1] == "copy" and video[-3:-1] == ["-f", "mp4"]
    mp3 = make_probe(
        has_video=False, vcodec=None, acodec="mp3", video_index=None, audio_index=0
    )
    argv = media.remux_argv("ffmpeg", "/w/in.mka", "/w/out.mp3", mp3)
    assert argv[-3:-1] == ["-f", "mp3"] and "0:0" in argv


# ---------------------------------------------------------------------------
# prepare_for_telegram without ffmpeg involvement
# ---------------------------------------------------------------------------


def test_no_streams_is_no_media(tmp_path):
    path = tmp_path / "x.mp4"
    path.write_bytes(b"0" * 10)
    probe = make_probe(
        has_video=False, has_audio=False, vcodec=None, acodec=None, size=10
    )
    with pytest.raises(NoMedia):
        run(
            media.prepare_for_telegram(
                str(path), str(tmp_path), max_bytes=MIB, probe_result=probe
            )
        )


def test_compatible_file_needs_no_ffmpeg(monkeypatch, tmp_path):
    path = tmp_path / "x.mp4"
    path.write_bytes(b"0" * 100)
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    prepared = run(
        media.prepare_for_telegram(
            str(path), str(tmp_path), max_bytes=MIB, probe_result=make_probe(size=100)
        )
    )
    assert prepared.action == Action.NONE == "none"
    assert prepared.path == str(path) and prepared.compressed is False
    assert prepared.original_size == 100


def test_conversion_without_ffmpeg_is_conversion_failed(monkeypatch, tmp_path):
    path = tmp_path / "x.webm"
    path.write_bytes(b"0" * 100)
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    with pytest.raises(ConversionFailed, match="ffmpeg is not installed"):
        run(
            media.prepare_for_telegram(
                str(path),
                str(tmp_path),
                max_bytes=MIB,
                probe_result=make_probe(vcodec="vp9", container="webm"),
            )
        )


def test_without_ffprobe_small_files_are_sent_as_is(monkeypatch, tmp_path):
    path = tmp_path / "x.mp4"
    path.write_bytes(b"0" * 100)
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    prepared = run(media.prepare_for_telegram(str(path), str(tmp_path), max_bytes=MIB))
    assert prepared.action == "none" and prepared.probe.has_video
    with pytest.raises(TooLarge):
        run(media.prepare_for_telegram(str(path), str(tmp_path), max_bytes=10))


def test_simple_ytdlp_only_never_converts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SIMPLE_YTDLP_ONLY", True)
    path = tmp_path / "x.webm"
    path.write_bytes(b"0" * 100)
    probe = make_probe(vcodec="vp9", container="webm", size=100)
    prepared = run(
        media.prepare_for_telegram(
            str(path), str(tmp_path), max_bytes=MIB, probe_result=probe
        )
    )
    assert prepared.action == "none" and prepared.path == str(path)
    with pytest.raises(TooLarge, match="SIMPLE_YTDLP_ONLY"):
        run(
            media.prepare_for_telegram(
                str(path), str(tmp_path), max_bytes=10, probe_result=probe
            )
        )


def test_transcode_semaphore_is_per_loop_and_follows_the_setting(monkeypatch):
    async def get():
        return media.transcode_semaphore(), media.transcode_semaphore()

    first, again = run(get())
    assert first is again
    other, _ = run(get())
    assert other is not first  # a new event loop gets a new semaphore
    monkeypatch.setattr(config, "TRANSCODE_CONCURRENCY", 3)

    async def value():
        return media.transcode_semaphore()._value

    assert run(value()) == 3


# ---------------------------------------------------------------------------
# End to end with ffmpeg (generated media)
# ---------------------------------------------------------------------------

LAVFI_VIDEO = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25"]
LAVFI_AUDIO = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
FAST_X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
FAST_VP9 = [
    "-c:v",
    "libvpx-vp9",
    "-deadline",
    "realtime",
    "-cpu-used",
    "8",
    "-b:v",
    "300k",
]


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True, timeout=120
    )


@pytest.fixture
def gen(tmp_path):
    """Build test media on demand: gen("name.ext", *ffmpeg_args) -> path."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    def build(name: str, *args: str) -> str:
        path = src_dir / name
        _ffmpeg(*args, str(path))
        return str(path)

    return build


def _workdir(tmp_path) -> str:
    path = tmp_path / "work"
    path.mkdir(exist_ok=True)
    return str(path)


@needs_ffmpeg
def test_probe_real_files(gen):
    webm = gen(
        "vp9.webm",
        *LAVFI_VIDEO,
        *LAVFI_AUDIO,
        "-t",
        "2",
        *FAST_VP9,
        "-c:a",
        "libopus",
        "-shortest",
    )
    p = run(media.probe(webm))
    assert (p.vcodec, p.acodec, p.container) == ("vp9", "opus", "webm")
    assert (p.display_width, p.display_height) == (320, 240)
    assert p.duration == pytest.approx(2.0, abs=0.2)
    assert p.size == os.path.getsize(webm)
    assert run(media.probe(webm + ".missing")) is None


@needs_ffmpeg
def test_probe_of_a_file_ffprobe_cannot_read_is_none(tmp_path, caplog):
    # an MP4 header without the moov atom, like a truncated download
    broken = tmp_path / "truncated.mp4"
    broken.write_bytes(b"\0\0\0\x18ftypmp42\0\0\0\0mp42isom" + b"\0" * 2000)
    with caplog.at_level(logging.WARNING, logger="src.media"):
        assert run(media.probe(str(broken))) is None
    assert any("ffprobe exited with" in r.getMessage() for r in caplog.records)


@needs_ffmpeg
def test_compatible_mp4_is_left_alone(gen, tmp_path):
    mp4 = gen(
        "ok.mp4",
        *LAVFI_VIDEO,
        *LAVFI_AUDIO,
        "-t",
        "2",
        *FAST_X264,
        "-c:a",
        "aac",
        "-shortest",
    )
    prepared = run(
        media.prepare_for_telegram(mp4, _workdir(tmp_path), max_bytes=50 * MIB)
    )
    assert prepared.action == "none"
    assert prepared.path == mp4
    assert prepared.probe.vcodec == "h264" and prepared.probe.acodec == "aac"


@needs_ffmpeg
def test_vp9_opus_webm_is_transcoded(gen, tmp_path):
    webm = gen(
        "vp9.webm",
        *LAVFI_VIDEO,
        *LAVFI_AUDIO,
        "-t",
        "2",
        *FAST_VP9,
        "-c:a",
        "libopus",
        "-shortest",
    )
    prepared = run(
        media.prepare_for_telegram(webm, _workdir(tmp_path), max_bytes=50 * MIB)
    )
    assert prepared.action == "transcode"
    assert prepared.compressed is False
    assert prepared.path.endswith("vp9.tg.mp4")
    q = prepared.probe
    assert (q.vcodec, q.acodec, q.container) == ("h264", "aac", "mp4")
    assert (q.display_width, q.display_height) == (
        320,
        240,
    )  # no 640x360 canvas, no bars
    assert q.duration == pytest.approx(2.0, abs=0.2)
    assert q.size == os.path.getsize(
        prepared.path
    )  # metadata refreshed from the output
    assert prepared.original_size == os.path.getsize(webm)
    assert is_telegram_compatible(q, prepared.path)


@needs_ffmpeg
def test_h264_aac_mkv_is_remuxed(gen, tmp_path):
    mkv = gen(
        "h264.mkv",
        *LAVFI_VIDEO,
        *LAVFI_AUDIO,
        "-t",
        "2",
        *FAST_X264,
        "-c:a",
        "aac",
        "-shortest",
    )
    prepared = run(
        media.prepare_for_telegram(mkv, _workdir(tmp_path), max_bytes=50 * MIB)
    )
    assert prepared.action == "remux"
    assert prepared.compressed is False
    assert prepared.path.endswith(".mp4")
    assert (prepared.probe.vcodec, prepared.probe.acodec, prepared.probe.container) == (
        "h264",
        "aac",
        "mp4",
    )


@needs_ffmpeg
def test_h264_with_opus_only_reencodes_the_audio(gen, tmp_path):
    mkv = gen(
        "h264opus.mkv",
        *LAVFI_VIDEO,
        *LAVFI_AUDIO,
        "-t",
        "2",
        *FAST_X264,
        "-c:a",
        "libopus",
        "-shortest",
    )
    source = run(media.probe(mkv))
    prepared = run(
        media.prepare_for_telegram(mkv, _workdir(tmp_path), max_bytes=50 * MIB)
    )
    assert prepared.action == "transcode"
    q = prepared.probe
    assert (q.vcodec, q.acodec) == ("h264", "aac")
    assert (q.width, q.height) == (source.width, source.height)
    assert q.pix_fmt == source.pix_fmt


@needs_ffmpeg
def test_square_video_stays_square(gen, tmp_path):
    webm = gen(
        "square.webm",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=240x240:rate=25",
        "-t",
        "2",
        *FAST_VP9,
    )
    prepared = run(
        media.prepare_for_telegram(webm, _workdir(tmp_path), max_bytes=50 * MIB)
    )
    assert prepared.action == "transcode"
    assert (prepared.probe.display_width, prepared.probe.display_height) == (240, 240)
    assert prepared.probe.has_audio is False


@needs_ffmpeg
def test_rotated_mp4(gen, tmp_path):
    plain = gen(
        "plain.mp4", *LAVFI_VIDEO, "-t", "3", *FAST_X264, "-qp", "4"
    )  # large on purpose
    rotated = str(tmp_path / "src" / "rotated.mp4")
    _ffmpeg("-display_rotation", "90", "-i", plain, "-c", "copy", rotated)
    p = run(media.probe(rotated))
    assert p.rotation in (90, 270)
    assert (p.display_width, p.display_height) == (240, 320)
    # compatible: sent as is, with the display size for sendVideo
    prepared = run(
        media.prepare_for_telegram(rotated, _workdir(tmp_path), max_bytes=50 * MIB)
    )
    assert prepared.action == "none"
    assert (prepared.probe.display_width, prepared.probe.display_height) == (240, 320)
    # re-encoded output is upright: rotation applied to the pixels
    budget = os.path.getsize(rotated) // 2
    squeezed = run(
        media.prepare_for_telegram(rotated, _workdir(tmp_path), max_bytes=budget)
    )
    assert squeezed.action == "compress" and squeezed.compressed
    assert squeezed.probe.rotation == 0
    assert (squeezed.probe.width, squeezed.probe.height) == (240, 320)
    assert squeezed.probe.size <= budget


@needs_ffmpeg
def test_anamorphic_video_gets_square_pixels(gen, tmp_path):
    mp4 = gen(
        "sar.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=480x360:rate=25",
        "-t",
        "2",
        "-vf",
        "setsar=4/3",
        *FAST_X264,
    )
    prepared = run(
        media.prepare_for_telegram(mp4, _workdir(tmp_path), max_bytes=50 * MIB)
    )
    assert prepared.action == "transcode"
    q = prepared.probe
    assert q.square_pixels
    assert (q.width, q.height) == (640, 360)


@needs_ffmpeg
def test_audio_only_inputs(gen, tmp_path):
    work = _workdir(tmp_path)
    opus = gen("a.webm", *LAVFI_AUDIO, "-t", "2", "-c:a", "libopus")
    prepared = run(media.prepare_for_telegram(opus, work, max_bytes=50 * MIB))
    assert prepared.action == "transcode"
    assert prepared.path.endswith(".m4a")
    assert (prepared.probe.has_video, prepared.probe.acodec) == (False, "aac")
    mp3 = gen("a.mp3", *LAVFI_AUDIO, "-t", "2", "-c:a", "libmp3lame")
    assert (
        run(media.prepare_for_telegram(mp3, work, max_bytes=50 * MIB)).action == "none"
    )
    aac_mp4 = gen("a.mp4", *LAVFI_AUDIO, "-t", "2", "-c:a", "aac")
    remuxed = run(media.prepare_for_telegram(aac_mp4, work, max_bytes=50 * MIB))
    assert remuxed.action == "remux" and remuxed.path.endswith(".m4a")
    long_opus = gen(
        "long.webm", *LAVFI_AUDIO, "-t", "10", "-c:a", "libopus", "-b:a", "128k"
    )
    budget = os.path.getsize(long_opus) // 2
    small = run(media.prepare_for_telegram(long_opus, work, max_bytes=budget))
    assert small.action == "compress" and small.compressed
    assert small.probe.size <= budget and small.probe.acodec == "aac"


@needs_ffmpeg
def test_oversized_video_is_compressed_to_the_budget(gen, tmp_path):
    big = gen(
        "big.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=25",
        *LAVFI_AUDIO,
        "-t",
        "6",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-qp",
        "8",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
    )
    size = os.path.getsize(big)
    budget = 350_000
    assert size > 3 * budget
    prepared = run(
        media.prepare_for_telegram(big, _workdir(tmp_path), max_bytes=budget)
    )
    assert prepared.action == "compress"
    assert prepared.compressed is True
    q = prepared.probe
    assert q.size <= budget
    assert (q.vcodec, q.acodec) == ("h264", "aac")
    assert q.display_width <= 640 and q.display_height <= 360
    assert q.display_width * 9 == q.display_height * 16  # aspect ratio kept
    assert prepared.original_size == size


@needs_ffmpeg
def test_infeasible_budget_raises_too_large(gen, tmp_path):
    mp4 = gen("ten.mp4", *LAVFI_VIDEO, "-t", "10", *FAST_X264)
    with pytest.raises(TooLarge):
        run(media.prepare_for_telegram(mp4, _workdir(tmp_path), max_bytes=20_000))


@needs_ffmpeg
def test_compress_retries_once_with_a_lower_bitrate(gen, monkeypatch, tmp_path):
    big = gen(
        "big.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=25",
        "-t",
        "6",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-qp",
        "8",
        "-pix_fmt",
        "yuv420p",
    )
    real_payload = media.payload_kbps
    monkeypatch.setattr(
        media,
        "payload_kbps",
        lambda max_bytes, duration: 3 * real_payload(max_bytes, duration),
    )
    labels = []
    real_run = media.run_command

    async def recording(argv, **kwargs):
        labels.append(kwargs.get("label"))
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(media, "run_command", recording)
    budget = 250_000
    prepared = run(
        media.prepare_for_telegram(big, _workdir(tmp_path), max_bytes=budget)
    )
    assert prepared.probe.size <= budget
    assert [label for label in labels if label.startswith("compress")] == [
        "compress (attempt 1)",
        "compress (attempt 2)",
    ]


@needs_ffmpeg
def test_thumbnail(gen, tmp_path):
    plain = gen("plain.mp4", *LAVFI_VIDEO, "-t", "2", *FAST_X264)
    rotated = str(tmp_path / "src" / "rotated.mp4")
    _ffmpeg("-display_rotation", "90", "-i", plain, "-c", "copy", rotated)
    work = _workdir(tmp_path)
    thumb = run(media.make_thumbnail(rotated, work, duration=2.0))
    assert thumb is not None and thumb.endswith(".jpg")
    assert os.path.getsize(thumb) <= media.THUMB_MAX_BYTES
    info = run(media.probe(thumb))
    assert info.vcodec == "mjpeg"
    assert max(info.width, info.height) <= 320
    assert info.width < info.height  # rotated video -> portrait thumbnail
    big = gen(
        "big.mp4",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=25",
        "-t",
        "1",
        *FAST_X264,
    )
    thumb_big = run(media.make_thumbnail(big, work))
    assert run(media.probe(thumb_big)).width == 320
    audio = gen("a.m4a", *LAVFI_AUDIO, "-t", "1", "-c:a", "aac")
    assert run(media.make_thumbnail(audio, work, duration=1.0)) is None


def _processes_mentioning(token: str) -> list[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read()
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                state = fh.read().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if token.encode() in cmdline and state not in ("Z", "X"):
            pids.append(int(entry))
    return pids


@needs_ffmpeg
@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_transcode_timeout_kills_ffmpeg(gen, monkeypatch, tmp_path):
    # 4:4:4 H.264 is not Telegram compatible: a real (slow) re-encode is needed
    src = gen(
        "slow.mkv",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=30",
        "-t",
        "20",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv444p",
    )
    monkeypatch.setattr(config, "TRANSCODE_PRESET", "veryslow")
    monkeypatch.setattr(config, "TRANSCODE_TIMEOUT", 1.0)
    work = _workdir(tmp_path)
    started = time.monotonic()
    with pytest.raises(DownloadTimeout, match="timed out"):
        run(media.prepare_for_telegram(src, work, max_bytes=50 * MIB))
    assert time.monotonic() - started < 10
    assert _processes_mentioning(work) == []
    assert not os.path.exists(
        os.path.join(work, "slow.tg.mp4")
    )  # partial output removed


@needs_ffmpeg
@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_cancellation_kills_ffmpeg(gen, monkeypatch, tmp_path):
    src = gen(
        "slow.mkv",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=30",
        "-t",
        "20",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv444p",
    )
    monkeypatch.setattr(config, "TRANSCODE_PRESET", "veryslow")
    work = _workdir(tmp_path)

    async def scenario():
        task = asyncio.create_task(
            media.prepare_for_telegram(src, work, max_bytes=50 * MIB)
        )
        for _ in range(200):
            if _processes_mentioning(work):
                break
            await asyncio.sleep(0.05)
        assert _processes_mentioning(work), "ffmpeg never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert _processes_mentioning(work) == []


@needs_ffmpeg
def test_concurrent_transcodes_are_limited(gen, monkeypatch, tmp_path):
    compatible = gen("ok.mp4", *LAVFI_VIDEO, "-t", "1", *FAST_X264)
    active = 0
    peak = 0

    async def fake_run(argv, **kwargs):
        nonlocal active, peak
        label = kwargs.get("label", "")
        if not label.startswith("transcode"):
            return await downloader.run_command(argv, **kwargs)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.3)
        shutil.copyfile(compatible, argv[-1].removeprefix("file:"))
        active -= 1
        return downloader.CommandResult(0, "", "", 0.3)

    monkeypatch.setattr(media, "run_command", fake_run)
    inputs = []
    for n in range(3):
        inputs.append(
            gen(
                f"in{n}.webm",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x120:rate=10",
                "-t",
                "1",
                *FAST_VP9,
            )
        )

    async def scenario(limit):
        monkeypatch.setattr(config, "TRANSCODE_CONCURRENCY", limit)
        work = _workdir(tmp_path)
        return await asyncio.gather(
            *(
                media.prepare_for_telegram(path, work, max_bytes=50 * MIB)
                for path in inputs
            )
        )

    results = run(scenario(1))
    assert [r.action for r in results] == ["transcode"] * 3
    assert peak == 1
    peak = 0
    run(scenario(3))
    assert peak == 3
