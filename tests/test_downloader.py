"""Tests for src.downloader (yt-dlp invocation, errors, format selection).

No network: format selection runs yt-dlp's own selector in-process on
synthetic format lists; downloads run the real yt-dlp CLI against a loopback
HTTP server, from info JSON files (``--load-info-json``).
"""

from __future__ import annotations

import asyncio
import copy
import functools
import http.server
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from src import config, downloader
from src.downloader import (
    AuthRequired,
    ContentUnavailable,
    ConversionFailed,
    ConversionTimeout,
    DownloadError,
    DownloadTimeout,
    GeoBlocked,
    LiveStream,
    MediaInfo,
    NoMedia,
    TooLarge,
    TooLong,
    UnsupportedUrl,
    classify_ytdlp_error,
    run_command,
)

yt_dlp = pytest.importorskip("yt_dlp")

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
STDERR_FIXTURES = FIXTURES / "ytdlp_stderr"
MIB = 1024 * 1024
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """Deterministic settings, whatever the environment or config version."""
    values = {
        "TELEGRAM_MAX_FILE_SIZE": 50 * MIB,
        "MAX_DOWNLOAD_BYTES": 500 * MIB,
        "MAX_DURATION_SECONDS": 3600,
        "MAX_ITEMS_PER_LINK": 10,
        "EXTRACT_TIMEOUT": 60,
        "DOWNLOAD_TIMEOUT": 120,
        "TRANSCODE_MAX_SIDE": 1280,
        "YTDLP_FORMAT": None,
        "YTDLP_FORMAT_SORT": None,
        "YTDLP_USER_AGENT": None,
        "YTDLP_COOKIES_PATH": None,
        "YTDLP_COOKIES": None,
        "YTDLP_COOKIES_FROM_BROWSER": None,
        "YTDLP_HEADERS": None,
        "YTDLP_HEADERS_DICT": {},
        "YTDLP_IMPERSONATE": None,
        "YTDLP_PROXY": None,
    }
    for name, value in values.items():
        monkeypatch.setattr(config, name, value, raising=False)
    for var in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

EXPECTED_CLASSIFICATION = {
    "youtube_bot_check": ("auth_required", False),
    "youtube_private": ("auth_required", False),
    "youtube_age_gate": ("auth_required", False),
    "youtube_members_only": ("auth_required", False),
    "youtube_unavailable": ("unavailable", False),
    "youtube_terminated": ("unavailable", False),
    "youtube_geo": ("geo_blocked", False),
    "youtube_upcoming_live": ("live", False),
    "youtube_premiere": ("live", False),
    "youtube_rate_limited": ("failed", True),
    "instagram_login": ("auth_required", False),
    "instagram_followers_only": ("auth_required", False),
    "instagram_no_video": ("no_media", False),
    "facebook_no_formats": ("no_media", False),
    "twitter_no_video": ("no_media", False),
    "twitter_nsfw": ("auth_required", False),
    "reddit_no_media": ("no_media", False),
    "vimeo_geo": ("geo_blocked", False),
    "vimeo_password": ("auth_required", False),
    "vimeo_404": ("unavailable", False),
    "tiktok_login": ("auth_required", False),
    "tiktok_impersonation_missing": ("failed", False),
    "tiktok_read_timeout": ("timeout", True),
    "http_429": ("failed", True),
    "http_503": ("failed", True),
    "unsupported_url": ("unsupported", False),
    "no_suitable_extractor": ("unsupported", False),
    "drm": ("no_media", False),
    "generic_extractor_broken": ("failed", False),
}


def test_every_stderr_fixture_has_an_expectation():
    names = {path.stem for path in STDERR_FIXTURES.glob("*.txt")}
    assert names == set(EXPECTED_CLASSIFICATION)


@pytest.mark.parametrize(
    ("name", "code", "retryable"),
    sorted((name, *expected) for name, expected in EXPECTED_CLASSIFICATION.items()),
)
def test_classify_realistic_stderr(name, code, retryable):
    stderr = (STDERR_FIXTURES / f"{name}.txt").read_text(encoding="utf-8")
    err = classify_ytdlp_error(stderr, returncode=1)
    assert err.code == code
    assert err.retryable is retryable
    last_error = [line for line in stderr.splitlines() if line.startswith("ERROR:")][-1]
    # the detail is the LAST error line (minus prefix/boilerplate), never a warning
    assert err.detail[:30] in last_error
    assert "WARNING" not in err.detail
    assert "please report this issue" not in err.detail
    assert "ERROR:" in err.stderr_tail  # the (redacted) tail is kept for logs


def test_long_warning_prefix_does_not_hide_the_error():
    stderr = "WARNING: " + "x" * 5000 + "\n" + "WARNING: use --cookies\n"
    stderr += "ERROR: [youtube] abc: Video unavailable"
    err = classify_ytdlp_error(stderr)
    assert isinstance(err, ContentUnavailable)
    assert err.detail == "[youtube] abc: Video unavailable"


def test_last_specific_error_wins():
    stderr = (
        "ERROR: [Instagram] a: There is no video in this post\n"
        "ERROR: [Instagram] b: Requested content is not available, rate-limit reached or login required\n"
    )
    assert isinstance(classify_ytdlp_error(stderr), AuthRequired)
    stderr_generic_last = (
        "ERROR: [youtube] a: This live event will begin in 3 hours.\n"
        "ERROR: Something odd happened\n"
    )
    assert isinstance(classify_ytdlp_error(stderr_generic_last), LiveStream)


def test_continuation_lines_are_joined():
    stderr = "ERROR: [youtube] abc: The uploader has not made\n  this video available in your country\n"
    err = classify_ytdlp_error(stderr)
    assert isinstance(err, GeoBlocked)
    assert "available in your country" in err.detail


def test_max_filesize_notice_on_stdout_is_too_large():
    stdout = (
        "[info] abc: Downloading 1 format(s): 18\n"
        "\n[download] File is larger than max-filesize (31965 bytes > 1000 bytes). Aborting.\n"
    )
    err = classify_ytdlp_error("", stdout=stdout, returncode=0)
    assert isinstance(err, TooLarge)
    assert "31965 bytes > 1000 bytes" in err.detail


def test_classify_without_error_line():
    err = classify_ytdlp_error(
        "Traceback (most recent call last):\nKeyboardInterrupt\n", returncode=1
    )
    assert type(err) is DownloadError
    assert err.detail == "KeyboardInterrupt"
    err = classify_ytdlp_error("", returncode=2)
    assert err.detail == "yt-dlp exited with status 2"


def test_details_are_redacted():
    token = "123456789:" + "A" * 35
    stderr = (
        f"ERROR: [generic] Unable to download https://user:secret@proxy.example:8080/x "
        f"and https://cdn.example/v.mp4?sig=SECRET&ip=1.2.3.4 with bot{token}"
    )
    err = classify_ytdlp_error(stderr)
    for secret in ("secret@", "SECRET", "1.2.3.4", token):
        assert secret not in err.detail
        assert secret not in err.stderr_tail
    assert "https://***@proxy.example:8080/x" in err.detail


def test_error_classes_follow_the_contract():
    expected = {
        DownloadError: "failed",
        UnsupportedUrl: "unsupported",
        AuthRequired: "auth_required",
        ContentUnavailable: "unavailable",
        GeoBlocked: "geo_blocked",
        LiveStream: "live",
        TooLong: "too_long",
        TooLarge: "too_large",
        NoMedia: "no_media",
        DownloadTimeout: "timeout",
    }
    for cls, code in expected.items():
        assert cls.code == code
        assert issubclass(cls, DownloadError)
    err = NoMedia("x" * 5000)
    assert len(err.detail) <= downloader.DETAIL_MAX_CHARS
    assert str(err) == err.detail
    assert DownloadTimeout("t").retryable is True
    assert TooLarge("t").retryable is False
    # a too slow conversion is a (permanent) conversion failure, not a timeout
    # of the site: same code and user text as any failed conversion
    assert issubclass(ConversionTimeout, ConversionFailed)
    assert not issubclass(ConversionTimeout, DownloadTimeout)
    assert ConversionTimeout.code == ConversionFailed.code == "conversion_failed"
    assert ConversionTimeout("t").retryable is False


# ---------------------------------------------------------------------------
# yt-dlp command lines
# ---------------------------------------------------------------------------


def _parse_ytdlp_argv(argv):
    """Parse our argv with yt-dlp's own option parser (validates every flag)."""
    assert argv[:3] == [sys.executable, "-m", "yt_dlp"]
    return yt_dlp.parse_options(argv[3:])


def _configure_auth(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_USER_AGENT", "Mozilla/5.0 (Test)")
    monkeypatch.setattr(
        config,
        "YTDLP_HEADERS_DICT",
        {"Referer": "https://www.instagram.com/", "X-Test": "1"},
    )
    monkeypatch.setattr(config, "YTDLP_IMPERSONATE", "chrome")
    monkeypatch.setattr(config, "YTDLP_PROXY", "socks5://user:pw@127.0.0.1:1080")
    monkeypatch.setattr(config, "YTDLP_COOKIES_FROM_BROWSER", "firefox")


def test_fetch_argv_always_has_auth_options(monkeypatch):
    _configure_auth(monkeypatch)
    url = "https://www.instagram.com/reel/C3xYz12AbCd/"
    argv = downloader.build_fetch_argv(url, cookies_path="/work/.cookies-1.txt")
    assert argv[-2:] == ["--", url]
    for flag in (
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--no-update",
        "--dump-single-json",
    ):
        assert flag in argv
    assert argv[argv.index("--ies") + 1] == "default,-generic"
    assert argv[argv.index("--playlist-items") + 1] == "1:10"
    assert argv[argv.index("--cookies") + 1] == "/work/.cookies-1.txt"
    assert "--max-filesize" not in argv  # selection is size aware; cap only on download
    opts = _parse_ytdlp_argv(argv)
    ydl = opts.ydl_opts
    assert opts.urls == [url]
    assert ydl["cookiefile"] == "/work/.cookies-1.txt"
    assert ydl["cookiesfrombrowser"][0] == "firefox"
    headers = {k.lower(): v for k, v in ydl["http_headers"].items()}
    assert headers["user-agent"] == "Mozilla/5.0 (Test)"
    assert headers["referer"] == "https://www.instagram.com/"
    assert headers["x-test"] == "1"
    assert ydl["proxy"] == "socks5://user:pw@127.0.0.1:1080"
    assert ydl["impersonate"] is not None
    assert ydl["noplaylist"] is True
    assert ydl["merge_output_format"] == "mp4"
    assert ydl["format_sort"] == downloader.DEFAULT_FORMAT_SORT.split(",")
    assert "manifest-filesize-approx" in ydl["compat_opts"]
    assert ydl["allowed_extractors"] == ["default", "-generic"]


def test_download_argv_has_the_same_auth_options_and_safety_net(monkeypatch):
    _configure_auth(monkeypatch)
    argv = downloader.build_download_argv(
        "/work/info-01.json",
        output_template="/work/item-01.%(ext)s",
        paths_file="/work/paths-01.txt",
        format_id="137+140",
        cookies_path="/work/.cookies-2.txt",
    )
    ydl = _parse_ytdlp_argv(argv).ydl_opts
    assert ydl["cookiefile"] == "/work/.cookies-2.txt"
    assert {k.lower(): v for k, v in ydl["http_headers"].items()}[
        "user-agent"
    ] == "Mozilla/5.0 (Test)"
    assert ydl["proxy"] == "socks5://user:pw@127.0.0.1:1080"
    assert (
        ydl["max_filesize"] == 500 * MIB
    )  # MAX_DOWNLOAD_BYTES, not the Telegram limit
    assert ydl["format"].startswith("137+140/")
    assert argv[argv.index("--load-info-json") + 1] == "/work/info-01.json"
    assert argv[
        argv.index("--print-to-file") + 1 : argv.index("--print-to-file") + 3
    ] == [
        "after_move:filepath",
        "/work/paths-01.txt",
    ]
    assert "--" not in argv


def test_format_overrides_and_raw_headers(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_FORMAT", "bv*[height<=720]+ba/b")
    monkeypatch.setattr(config, "YTDLP_FORMAT_SORT", "res:720,codec:avc:m4a")
    monkeypatch.setattr(config, "YTDLP_HEADERS_DICT", None)
    monkeypatch.setattr(
        config, "YTDLP_HEADERS", "Referer: https://x.example/|Accept-Language: it"
    )
    argv = downloader.build_fetch_argv("https://x.example/v")
    ydl = _parse_ytdlp_argv(argv).ydl_opts
    assert ydl["format"] == "bv*[height<=720]+ba/b"
    assert ydl["format_sort"] == ["res:720", "codec:avc:m4a"]
    headers = {k.lower(): v for k, v in ydl["http_headers"].items()}
    assert headers["referer"] == "https://x.example/"
    assert headers["accept-language"] == "it"


def test_legacy_cookie_setting_name_is_honoured(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(config, "YTDLP_COOKIES", str(cookies))

    seen = {}

    def read_copy(path):
        seen["content"] = pathlib.Path(path).read_text()
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)

    async def scenario():
        async with downloader.private_cookies(str(tmp_path)) as copy_path:
            read_copy(copy_path)
            return copy_path

    copy_path = run(scenario())
    assert copy_path != str(cookies)
    assert seen == {"content": cookies.read_text(), "mode": 0o600}
    assert not os.path.exists(copy_path)


def test_argv_for_log_masks_secrets(monkeypatch):
    _configure_auth(monkeypatch)
    shown = downloader.argv_for_log(
        downloader.build_fetch_argv("https://e.example/v?token=abc")
    )
    assert "user:pw" not in shown
    assert "instagram.com" not in shown  # header value masked
    assert "Referer:***" in shown
    assert "token=abc" not in shown


def test_selection_budget_keeps_container_headroom():
    assert downloader.selection_budget(50 * MIB) == 50 * MIB - 256 * 1024
    assert downloader.selection_budget(1000 * MIB) == 1000 * MIB - int(
        1000 * MIB * 0.005
    )
    spec = downloader.format_spec()
    assert f"[filesize<?{int(downloader.selection_budget(50 * MIB) * 0.95)}]" in spec


def test_format_spec_is_valid_for_ytdlp():
    spec = downloader.build_format_spec(50 * MIB, 500 * MIB, 1280)
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        ydl.build_format_selector(spec)  # raises SyntaxError when invalid
    assert "[width<=?1280][height<=?1280]" in spec
    assert spec.endswith("bv*+ba/b")


# ---------------------------------------------------------------------------
# Format selection, verified with yt-dlp's own selector
# ---------------------------------------------------------------------------


def _fmt(
    fid,
    *,
    w=None,
    h=None,
    v="none",
    a="none",
    ext="mp4",
    tbr=None,
    dur=None,
    proto="https",
    sized=True,
    **extra,
):
    fmt = {
        "format_id": fid,
        "url": f"https://cdn.invalid/{fid}",
        "ext": ext,
        "vcodec": v,
        "acodec": a,
        "protocol": proto,
    }
    if w:
        fmt["width"], fmt["height"] = w, h
    if tbr:
        fmt["tbr"] = tbr
        if sized and dur:
            fmt["filesize"] = int(tbr * 1000 / 8 * dur)
    if proto.startswith("m3u8") or proto.endswith("dash"):
        fmt["manifest_url"] = "https://cdn.invalid/manifest"
    fmt.update(extra)
    return fmt


def _info(key, formats, dur):
    return {
        "id": f"{key.lower()}-id",
        "title": f"{key} title",
        "duration": dur,
        "extractor": key.lower(),
        "extractor_key": key,
        "webpage_url": f"https://{key.lower()}.invalid/watch",
        "formats": formats,
    }


def site_youtube(dur):
    def video(fid, codec, w, h, kbps, ext="mp4"):
        return _fmt(fid, w=w, h=h, v=codec, ext=ext, tbr=kbps, dur=dur, fps=30)

    def audio(fid, codec, kbps, ext):
        return _fmt(fid, a=codec, ext=ext, tbr=kbps, dur=dur, abr=kbps)

    formats = [
        audio("139", "mp4a.40.5", 48, "m4a"),
        audio("140", "mp4a.40.2", 129, "m4a"),
        audio("249", "opus", 50, "webm"),
        audio("251", "opus", 135, "webm"),
        video("160", "avc1.4d400c", 256, 144, 100),
        video("278", "vp9", 256, 144, 80, "webm"),
        video("133", "avc1.4d4015", 426, 240, 250),
        video("242", "vp9", 426, 240, 180, "webm"),
        video("134", "avc1.4d401e", 640, 360, 600),
        video("243", "vp9", 640, 360, 400, "webm"),
        video("135", "avc1.4d401f", 854, 480, 1100),
        video("244", "vp9", 854, 480, 750, "webm"),
        video("136", "avc1.4d401f", 1280, 720, 2200),
        video("247", "vp9", 1280, 720, 1500, "webm"),
        video("398", "av01.0.05M.08", 1280, 720, 1300),
        video("137", "avc1.640028", 1920, 1080, 4300),
        video("248", "vp9", 1920, 1080, 2700, "webm"),
        video("399", "av01.0.08M.08", 1920, 1080, 2500),
        video("313", "vp9", 3840, 2160, 17000, "webm"),
        _fmt(
            "18",
            w=640,
            h=360,
            v="avc1.42001E",
            a="mp4a.40.2",
            tbr=500,
            dur=dur,
            sized=False,
            filesize_approx=int(500 * 1000 / 8 * dur),
        ),
    ]
    return _info("Youtube", formats, dur)


def site_instagram(dur):
    formats = []
    for fid, w, h, tbr in (
        ("dash-1080v", 1080, 1920, 3500),
        ("dash-720v", 720, 1280, 1800),
        ("dash-480v", 480, 854, 900),
    ):
        formats.append(_fmt(fid, w=w, h=h, v="avc1.64001F", tbr=tbr, dur=dur))
        formats.append(
            _fmt(f"{fid}-vp9", w=w, h=h, v="vp09.00.40.08", tbr=tbr * 0.7, dur=dur)
        )
    formats.append(_fmt("dash-audio", a="mp4a.40.2", ext="m4a", tbr=128, dur=dur))
    # progressive version: codecs unknown (Instagram API), audio present
    formats.append(_fmt("1", w=720, h=1280, v=None, a=None, tbr=1900, dur=dur))
    return _info("Instagram", formats, dur)


def site_facebook(dur):
    formats = [
        _fmt("sd", w=640, h=360, v=None, a=None, tbr=700, dur=dur),
        _fmt("hd", w=1280, h=720, v=None, a=None, tbr=2000, dur=dur),
    ]
    for fid, w, h, tbr in (
        ("1080p", 1920, 1080, 4000),
        ("720p", 1280, 720, 2000),
        ("360p", 640, 360, 600),
    ):
        formats.append(_fmt(fid, w=w, h=h, v="avc1.640028", tbr=tbr, dur=dur))
        formats.append(
            _fmt(f"{fid}-vp9", w=w, h=h, v="vp09.00.41.08", tbr=tbr * 0.7, dur=dur)
        )
    formats.append(_fmt("audio", a="mp4a.40.5", ext="m4a", tbr=64, dur=dur))
    return _info("Facebook", formats, dur)


def site_tiktok(dur):
    formats = [
        _fmt(
            "download",
            w=576,
            h=1024,
            v="h264",
            a="aac",
            tbr=1200,
            dur=dur,
            preference=-2,
            format_note="watermarked",
        ),
        _fmt("h264_540p", w=576, h=1024, v="h264", a="aac", tbr=900, dur=dur),
        _fmt("h264_720p", w=720, h=1280, v="h264", a="aac", tbr=1600, dur=dur),
        _fmt("h264_1080p", w=1080, h=1920, v="h264", a="aac", tbr=3000, dur=dur),
        _fmt("bytevc1_1080p", w=1080, h=1920, v="bytevc1", a="aac", tbr=1800, dur=dur),
        _fmt("bytevc1_720p", w=720, h=1280, v="h265", a="aac", tbr=1000, dur=dur),
    ]
    return _info("TikTok", formats, dur)


def site_reddit(dur):
    mpd = "https://v.redd.it/abc/DASHPlaylist.mpd"
    formats = []
    for fid, w, h, tbr in (
        ("dash-1080", 1920, 1080, 4800),
        ("dash-720", 1280, 720, 2400),
        ("dash-480", 854, 480, 1200),
        ("dash-240", 426, 240, 300),
    ):
        formats.append(
            _fmt(
                fid,
                w=w,
                h=h,
                v="avc1.4d401f",
                tbr=tbr,
                dur=dur,
                sized=False,
                manifest_url=mpd,
            )
        )
    formats.append(
        _fmt(
            "dash-audio",
            a="mp4a.40.2",
            ext="m4a",
            tbr=128,
            dur=dur,
            sized=False,
            manifest_url=mpd,
        )
    )
    for fid, w, h, tbr in (
        ("hls-1080", 1920, 1080, 5000),
        ("hls-720", 1280, 720, 2600),
    ):
        formats.append(
            _fmt(
                fid,
                w=w,
                h=h,
                v="avc1.4d401f",
                a="mp4a.40.2",
                tbr=tbr,
                dur=dur,
                proto="m3u8_native",
                sized=False,
            )
        )
    return _info("Reddit", formats, dur)


def site_twitter(dur):
    formats = []
    for tbr, w, h in (
        (256, 480, 270),
        (832, 640, 360),
        (2176, 1280, 720),
        (10368, 1920, 1080),
    ):
        # progressive MP4 variants: no codec information
        formats.append(
            _fmt(f"http-{tbr}", w=w, h=h, v=None, a=None, tbr=tbr, dur=dur, sized=False)
        )
        formats.append(
            _fmt(
                f"hls-{tbr}",
                w=w,
                h=h,
                v="avc1.640020",
                tbr=tbr,
                dur=dur,
                proto="m3u8_native",
                sized=False,
            )
        )
    for abr in (32, 64, 128):
        formats.append(
            _fmt(
                f"hls-audio-{abr}000-Audio",
                a="mp4a.40.2",
                tbr=abr,
                dur=dur,
                proto="m3u8_native",
                sized=False,
            )
        )
    return _info("Twitter", formats, dur)


def site_vimeo(dur):
    formats = []
    for fid, w, h, tbr in (
        ("1080p", 1920, 1080, 4500),
        ("720p", 1280, 720, 2300),
        ("540p", 960, 540, 1400),
        ("360p", 640, 360, 700),
    ):
        formats.append(
            _fmt(
                f"hls-fastly_skyfire-{fid}",
                w=w,
                h=h,
                v="avc1.640028",
                tbr=tbr,
                dur=dur,
                proto="m3u8_native",
                sized=False,
            )
        )
        formats.append(
            _fmt(
                f"dash-fastly_skyfire-video-{fid}",
                w=w,
                h=h,
                v="avc1.640028",
                tbr=tbr,
                dur=dur,
                proto="http_dash_segments",
                sized=False,
            )
        )
    formats.append(
        _fmt(
            "hls-fastly_skyfire-audio-high-Original",
            a="mp4a.40.2",
            tbr=192,
            dur=dur,
            proto="m3u8_native",
            sized=False,
        )
    )
    formats.append(
        _fmt(
            "dash-fastly_skyfire-audio-high",
            a="mp4a.40.2",
            ext="m4a",
            tbr=192,
            dur=dur,
            proto="http_dash_segments",
            sized=False,
        )
    )
    return _info("Vimeo", formats, dur)


def site_soundcloud(dur):
    formats = [
        _fmt("http_mp3_1_0", a="mp3", ext="mp3", tbr=128, dur=dur),
        _fmt(
            "hls_aac_160k",
            a="aac",
            ext="m4a",
            tbr=160,
            dur=dur,
            proto="m3u8_native",
            sized=False,
        ),
        _fmt(
            "hls_opus_64k",
            a="opus",
            ext="opus",
            tbr=64,
            dur=dur,
            proto="m3u8_native",
            sized=False,
        ),
    ]
    return _info("Soundcloud", formats, dur)


def site_silent_gif(dur):
    return _info(
        "Twitter", [_fmt("http", w=480, h=480, v=None, a="none", tbr=500, dur=dur)], dur
    )


def site_vp9_only(dur):
    formats = [
        _fmt("303", w=1920, h=1080, v="vp9", ext="webm", tbr=3000, dur=dur),
        _fmt("247", w=1280, h=720, v="vp9", ext="webm", tbr=1500, dur=dur),
        _fmt("251", a="opus", ext="webm", tbr=130, dur=dur),
    ]
    return _info("Vp9Only", formats, dur)


SITES = {
    "youtube": site_youtube,
    "instagram": site_instagram,
    "facebook": site_facebook,
    "tiktok": site_tiktok,
    "reddit": site_reddit,
    "twitter": site_twitter,
    "vimeo": site_vimeo,
    "soundcloud": site_soundcloud,
    "silent": site_silent_gif,
    "vp9only": site_vp9_only,
}


def _ydl_opts(extra=()):
    argv = [
        "--ignore-config",
        "--quiet",
        "--no-warnings",
        "--simulate",
        *downloader.format_args(),
        *extra,
    ]
    return yt_dlp.parse_options(argv).ydl_opts


def select_with_ytdlp(info):
    """Run the real -f/-S/--compat-options through yt-dlp's selector."""
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        return ydl.process_ie_result(copy.deepcopy(info), download=False)


def _selected_size(result):
    parts = result.get("requested_formats") or [result]
    return sum(
        part.get("filesize") or part.get("filesize_approx") or 0 for part in parts
    )


@pytest.mark.parametrize(
    ("site", "duration", "expected"),
    [
        # Telegram-ready H.264/AAC that fits 50 MiB: best resolution <= 1080p
        ("youtube", 30, "137+140"),
        ("youtube", 180, "136+139"),  # 720p + low-rate AAC keeps the sum under budget
        ("youtube", 600, "18"),
        ("youtube", 1800, "160+139"),
        ("instagram", 30, "dash-1080v+dash-audio"),
        ("instagram", 180, "dash-720v+dash-audio"),
        ("facebook", 30, "1080p+audio"),
        ("facebook", 180, "720p+audio"),
        ("facebook", 600, "360p+audio"),
        ("tiktok", 30, "h264_1080p"),  # not the watermarked/HEVC variants
        ("tiktok", 180, "h264_720p"),
        ("reddit", 30, "dash-1080+dash-audio"),  # DASH (https) preferred over HLS
        ("reddit", 180, "dash-480+dash-audio"),  # sizes estimated from bitrate
        ("twitter", 30, "http-10368"),  # progressive MP4 over HLS
        ("twitter", 180, "http-2176"),
        (
            "vimeo",
            30,
            "hls-fastly_skyfire-1080p+hls-fastly_skyfire-audio-high-Original",
        ),
        (
            "vimeo",
            180,
            "hls-fastly_skyfire-540p+hls-fastly_skyfire-audio-high-Original",
        ),
        ("soundcloud", 180, "http_mp3_1_0"),
        ("silent", 30, "http"),
        # needs conversion: smallest sensible input for prepare_for_telegram
        ("youtube", 3600, "133+139"),
        ("instagram", 600, "dash-720v+dash-audio"),
        ("tiktok", 600, "h264_720p"),
        ("soundcloud", 3600, "hls_opus_64k"),
        ("vp9only", 30, "303+251"),
        ("vp9only", 180, "247+251"),  # fits the budget at 720p: one CRF re-encode
    ],
)
def test_default_selection_with_ytdlp_selector(site, duration, expected):
    result = select_with_ytdlp(SITES[site](duration))
    assert result["format_id"] == expected


READY = [
    (site, duration)
    for site in (
        "youtube",
        "instagram",
        "facebook",
        "tiktok",
        "reddit",
        "twitter",
        "vimeo",
    )
    for duration in (30, 60, 120, 180, 300)
] + [("youtube", 600), ("youtube", 1800), ("facebook", 600), ("twitter", 600)]


@pytest.mark.parametrize(("site", "duration"), READY)
def test_ready_selections_fit_the_budget_and_need_no_conversion(site, duration):
    result = select_with_ytdlp(SITES[site](duration))
    parts = result.get("requested_formats") or [result]
    assert 0 < _selected_size(result) <= 50 * MIB, result["format_id"]
    for part in parts:
        vcodec, acodec = part.get("vcodec"), part.get("acodec")
        assert vcodec in (None, "none") or vcodec.lower().startswith(("avc", "h264"))
        assert acodec in (None, "none") or acodec.lower().startswith(
            ("mp4a", "aac", "mp3")
        )
    assert result["ext"] == "mp4"
    assert any(part.get("vcodec") != "none" for part in parts)  # never audio only


def test_smaller_budget_picks_smaller_formats(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_MAX_FILE_SIZE", 20 * MIB)
    result = select_with_ytdlp(site_youtube(180))
    assert result["format_id"] == "18"  # 360p progressive instead of 720p
    assert _selected_size(result) <= 20 * MIB


def test_custom_format_is_used(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_FORMAT", "bv*[vcodec^=vp9]+ba[acodec=opus]")
    assert select_with_ytdlp(site_youtube(30))["format_id"] == "248+251"


# ---------------------------------------------------------------------------
# Subprocess handling
# ---------------------------------------------------------------------------

TREE_SCRIPT = textwrap.dedent("""
    import subprocess, sys, time
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1], "w") as fh:
        fh.write(str(grandchild.pid))
    sys.stderr.write("WARNING: working\\n")
    sys.stderr.flush()
    time.sleep(120)
    """)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state not in ("Z", "X")


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_for_file(path: pathlib.Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was not written")


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_timeout_kills_the_whole_process_group(tmp_path):
    script = tmp_path / "tree.py"
    script.write_text(TREE_SCRIPT)
    pidfile = tmp_path / "grandchild.pid"
    started = time.monotonic()
    with pytest.raises(DownloadTimeout) as excinfo:
        run(
            run_command(
                [sys.executable, str(script), str(pidfile)],
                time_limit=1.5,
                label="fake yt-dlp",
            )
        )
    elapsed = time.monotonic() - started
    # the grandchild keeps our pipes open: without the group kill this would hang 120 s
    assert elapsed < 6, elapsed
    assert "fake yt-dlp timed out after 1.5s" in str(excinfo.value)
    assert excinfo.value.code == "timeout"
    assert excinfo.value.retryable is True
    assert "WARNING: working" in excinfo.value.stderr_tail
    assert _wait_dead(int(pidfile.read_text()))


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_cancellation_kills_the_whole_process_group(tmp_path):
    script = tmp_path / "tree.py"
    script.write_text(TREE_SCRIPT)
    pidfile = tmp_path / "grandchild.pid"

    def grandchild_started():
        return pidfile.exists() and bool(pidfile.read_text().strip())

    async def scenario():
        task = asyncio.create_task(
            run_command(
                [sys.executable, str(script), str(pidfile)], time_limit=60, label="x"
            )
        )
        for _ in range(500):
            if grandchild_started():
                break
            await asyncio.sleep(0.02)
        assert grandchild_started()
        task.cancel()
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    assert run(scenario()) < 5
    assert _wait_dead(int(pidfile.read_text()))


def test_monitor_can_abort_the_process(tmp_path):
    calls = []

    def monitor():
        calls.append(1)
        if len(calls) >= 2:
            raise TooLarge("too much")

    started = time.monotonic()
    with pytest.raises(TooLarge):
        run(
            run_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                time_limit=30,
                label="x",
                monitor=monitor,
                monitor_interval=0.1,
            )
        )
    assert time.monotonic() - started < 5


def test_capture_is_bounded_and_keeps_the_tail():
    code = "import sys; sys.stderr.write('W' * 2_000_000 + '\\nERROR: [x] y: Video unavailable\\n')"
    result = run(run_command([sys.executable, "-c", code], time_limit=30, label="x"))
    assert result.returncode == 0
    assert len(result.stderr) < downloader.CAPTURE_LIMIT + 200
    assert "bytes omitted" in result.stderr
    assert isinstance(classify_ytdlp_error(result.stderr), ContentUnavailable)


def test_stdout_to_private_file(tmp_path):
    out = tmp_path / "info.json"
    result = run(
        run_command(
            [sys.executable, "-c", "print('{}')"],
            time_limit=30,
            label="x",
            stdout_path=str(out),
        )
    )
    assert result.returncode == 0
    assert out.read_text().strip() == "{}"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_missing_executable_is_a_download_error(tmp_path):
    with pytest.raises(DownloadError, match="cannot start"):
        run(run_command([str(tmp_path / "missing")], time_limit=5, label="x"))


def test_output_size_guard(tmp_path):
    (tmp_path / "item-01.f137.mp4.part").write_bytes(b"x" * 600)
    (tmp_path / "item-01.temp.mp4").write_bytes(b"x" * 10_000)  # merge temp: ignored
    (tmp_path / "item-02.mp4").write_bytes(b"x" * 10_000)  # another item
    guard = downloader._OutputSizeGuard(str(tmp_path), "item-01.", 1000)
    guard()
    (tmp_path / "item-01.f140.m4a").write_bytes(b"x" * 600)
    with pytest.raises(TooLarge):
        guard()


# ---------------------------------------------------------------------------
# fetch_info with a fake yt-dlp (records argv, replays output)
# ---------------------------------------------------------------------------

FAKE_YTDLP = textwrap.dedent("""
    import json, os, sys, time
    argv = sys.argv[1:]
    with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\\n")
    if "--cookies" in argv:
        # yt-dlp saves the cookie jar back to --cookies when it exits
        with open(argv[argv.index("--cookies") + 1], "a", encoding="utf-8") as fh:
            fh.write("# rewritten by yt-dlp\\n")
    if os.environ.get("FAKE_SLEEP"):
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    if os.environ.get("FAKE_STDOUT"):
        sys.stdout.write(open(os.environ["FAKE_STDOUT"], encoding="utf-8").read())
    if os.environ.get("FAKE_STDERR"):
        sys.stderr.write(open(os.environ["FAKE_STDERR"], encoding="utf-8").read())
    sys.exit(int(os.environ.get("FAKE_RC", "0")))
    """)


@pytest.fixture
def fake_ytdlp(monkeypatch, tmp_path):
    """Replace yt-dlp with a script replaying canned output."""
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(FAKE_YTDLP)
    argv_log = tmp_path / "argv.log"
    argv_log.touch()
    monkeypatch.setattr(
        downloader, "ytdlp_command", lambda: [sys.executable, str(script)]
    )
    monkeypatch.setenv("FAKE_ARGV_LOG", str(argv_log))

    class Fake:
        def respond(self, *, stdout=None, stderr=None, rc=0, sleep=None):
            if stdout is not None:
                path = tmp_path / "fake_stdout.txt"
                path.write_text(
                    stdout if isinstance(stdout, str) else json.dumps(stdout)
                )
                monkeypatch.setenv("FAKE_STDOUT", str(path))
            if stderr is not None:
                path = tmp_path / "fake_stderr.txt"
                path.write_text(stderr)
                monkeypatch.setenv("FAKE_STDERR", str(path))
            monkeypatch.setenv("FAKE_RC", str(rc))
            if sleep is not None:
                monkeypatch.setenv("FAKE_SLEEP", str(sleep))

        def calls(self):
            return [json.loads(line) for line in argv_log.read_text().splitlines()]

    return Fake()


def dump_single_json(raw, tmp_dir=None):
    """What `yt-dlp -J` prints for the extractor output ``raw``.

    Runs the real yt-dlp CLI offline with our format options; the raw info is
    loaded with ``--load-info-json --no-clean-info-json`` (playlists included).
    """
    tmp_dir = pathlib.Path(tmp_dir or os.environ.get("SMR_TEST_TMP", "/tmp"))
    raw_path = tmp_dir / f"raw-{os.getpid()}-{time.monotonic_ns()}.json"
    raw_path.write_text(json.dumps(raw))
    argv = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-clean-info-json",
        "--ies",
        "default,-generic",
        "--no-warnings",
        "--dump-single-json",
        *downloader.format_args(),
        "--playlist-items",
        f"1:{config.MAX_ITEMS_PER_LINK}",
        "--load-info-json",
        str(raw_path),
    ]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.stdout.strip(), proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(autouse=True)
def _scratch_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SMR_TEST_TMP", str(tmp_path))


def test_fetch_info_single_merged_video(fake_ytdlp, tmp_path):
    fake_ytdlp.respond(stdout=dump_single_json(site_youtube(180)))
    workdir = tmp_path / "work"
    url = "https://www.youtube.com/watch?v=abc"
    info = run(downloader.fetch_info(url, str(workdir)))
    assert isinstance(info, MediaInfo)
    assert info.url == url
    assert info.extractor_key == "Youtube"
    assert info.title == "Youtube title"
    assert info.is_live is False
    assert not info.is_playlist
    assert info.info_path == str(workdir / "info.json")
    assert (
        json.loads(pathlib.Path(info.info_path).read_text())["format_id"] == "136+139"
    )
    [item] = info.items
    assert item.index == 1
    assert item.kind == "video"
    assert item.format_id == "136+139"
    assert (item.width, item.height) == (1280, 720)
    assert item.duration == 180
    expected_size = int(2200 * 1000 / 8 * 180) + int(48 * 1000 / 8 * 180)
    assert item.filesize == expected_size
    assert item.direct_url is None  # merged formats cannot be fetched by Telegram
    assert item.ext == "mp4"
    assert "formats" not in info.raw and "requested_formats" not in info.raw
    [call] = fake_ytdlp.calls()
    assert call[-2:] == ["--", url]
    assert "--dump-single-json" in call


def test_fetch_info_progressive_item_exposes_direct_url(fake_ytdlp, tmp_path):
    raw = site_tiktok(30)
    for fmt in raw["formats"]:
        fmt["http_headers"] = {"Referer": "https://www.tiktok.com/"}
    fake_ytdlp.respond(stdout=dump_single_json(raw))
    info = run(
        downloader.fetch_info("https://www.tiktok.com/@u/video/1", str(tmp_path))
    )
    [item] = info.items
    assert item.format_id == "h264_1080p"
    assert item.direct_url == "https://cdn.invalid/h264_1080p"
    assert item.direct_ext == "mp4"
    assert item.filesize == int(3000 * 1000 / 8 * 30)
    assert item.http_headers.get("Referer") == "https://www.tiktok.com/"


def test_format_needing_cookies_has_no_direct_url(fake_ytdlp, tmp_path):
    data = dump_single_json(site_tiktok(30))
    data["cookies"] = "tt_chain_token=abc; Domain=.tiktok.com; Path=/"
    fake_ytdlp.respond(stdout=data)
    info = run(
        downloader.fetch_info("https://www.tiktok.com/@u/video/1", str(tmp_path))
    )
    assert info.items[0].direct_url is None


def test_item_mapping_details():
    entry = {
        "id": "x1",
        "title": "Clip",
        "duration": 12.5,
        "format_id": "hls-720+audio",
        "ext": "mp4",
        "vcodec": "avc1.64001f",
        "acodec": "mp4a.40.2",
        "requested_formats": [
            {
                "format_id": "hls-720",
                "vcodec": "avc1.64001f",
                "acodec": "none",
                "width": 720,
                "height": 1280,
                "filesize_approx": 1000,
                "url": "https://v/1",
                "protocol": "m3u8_native",
                "http_headers": {"Cookie": "a=b", "Referer": "https://site/"},
            },
            {
                "format_id": "audio",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": None,
                "url": "https://a/1",
                "protocol": "m3u8_native",
            },
        ],
    }
    item = downloader._make_item(2, entry)
    assert (item.index, item.id, item.kind, item.duration) == (2, "x1", "video", 12.5)
    assert (item.width, item.height) == (720, 1280)
    assert item.filesize is None  # one part has an unknown size
    assert item.direct_url is None
    assert item.http_headers == {"Referer": "https://site/"}
    single_unknown = {
        "id": "y",
        "format_id": "0",
        "url": "https://v/2",
        "ext": "mp3",
        "vcodec": None,
        "acodec": None,
        "protocol": "https",
    }
    assert downloader._make_item(1, single_unknown).kind == "audio"
    assert downloader._make_item(1, {**single_unknown, "ext": "bin"}).kind == "unknown"


def test_fetch_info_audio_only_item(fake_ytdlp, tmp_path):
    fake_ytdlp.respond(stdout=dump_single_json(site_soundcloud(180)))
    info = run(downloader.fetch_info("https://soundcloud.com/a/b", str(tmp_path)))
    [item] = info.items
    assert item.kind == "audio"
    assert item.direct_url is None


def _playlist(entries, **extra):
    data = {
        "_type": "playlist",
        "id": "post",
        "title": "Post by someone",
        "extractor": "instagram",
        "extractor_key": "Instagram",
        "webpage_url": "https://example.invalid/p/post",
        "entries": entries,
    }
    data.update(extra)
    return data


def _entry(n, dur=10):
    entry = site_tiktok(dur)
    entry.update(
        id=f"item{n}",
        title=f"Item {n}",
        extractor="instagram",
        extractor_key="Instagram",
    )
    return entry


def test_fetch_info_playlist_items_capped_and_saved(fake_ytdlp, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MAX_ITEMS_PER_LINK", 2)
    processed = dump_single_json(_playlist([_entry(n) for n in range(1, 5)]))
    fake_ytdlp.respond(stdout=processed)
    info = run(
        downloader.fetch_info("https://www.instagram.com/p/post/", str(tmp_path))
    )
    assert info.is_playlist
    assert [item.index for item in info.items] == [1, 2]
    assert [item.id for item in info.items] == ["item1", "item2"]
    assert info.entry_count == 4
    assert info.title == "Post by someone"
    for index in (1, 2):
        saved = json.loads((tmp_path / f"info-{index:02d}.json").read_text())
        assert saved["id"] == f"item{index}"
    [call] = fake_ytdlp.calls()
    assert call[call.index("--playlist-items") + 1] == "1:2"


def test_fetch_info_skips_failed_and_too_long_entries(
    fake_ytdlp, monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "MAX_DURATION_SECONDS", 60)
    processed = dump_single_json(
        _playlist([_entry(1), _entry(2), _entry(3, dur=600), _entry(4)])
    )
    processed["entries"][1] = None  # an image entry yt-dlp failed to process
    fake_ytdlp.respond(
        stdout=processed, stderr="ERROR: [Instagram] x: No video formats found!", rc=1
    )
    info = run(
        downloader.fetch_info("https://www.instagram.com/p/post/", str(tmp_path))
    )
    assert [item.index for item in info.items] == [1, 4]
    assert info.skipped == {2: "no_media", 3: "too_long"}


def test_fetch_info_live(fake_ytdlp, tmp_path):
    data = dump_single_json(site_youtube(0))
    data.update(is_live=True, live_status="is_live", duration=None)
    fake_ytdlp.respond(stdout=data)
    with pytest.raises(LiveStream, match="is_live"):
        run(
            downloader.fetch_info("https://www.youtube.com/watch?v=live", str(tmp_path))
        )


def test_fetch_info_too_long(fake_ytdlp, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MAX_DURATION_SECONDS", 900)
    fake_ytdlp.respond(stdout=dump_single_json(site_youtube(1800)))
    with pytest.raises(TooLong, match="1800s exceeds the 900s limit"):
        run(
            downloader.fetch_info("https://www.youtube.com/watch?v=long", str(tmp_path))
        )


def test_fetch_info_too_large_for_the_download_cap(fake_ytdlp, tmp_path):
    fake_ytdlp.respond(stdout=dump_single_json(site_vp9_only(3600)))
    with pytest.raises(TooLarge, match="limit 500 MiB"):
        run(downloader.fetch_info("https://vp9.invalid/v", str(tmp_path)))


def test_fetch_info_error_is_classified(fake_ytdlp, tmp_path):
    stderr = (STDERR_FIXTURES / "instagram_login.txt").read_text(encoding="utf-8")
    fake_ytdlp.respond(stdout="", stderr=stderr, rc=1)
    with pytest.raises(AuthRequired) as excinfo:
        run(
            downloader.fetch_info(
                "https://www.instagram.com/reel/C3xYz12AbCd/", str(tmp_path)
            )
        )
    assert "rate-limit reached or login required" in excinfo.value.detail
    assert "WARNING" not in excinfo.value.detail


def test_fetch_info_empty_output_is_no_media(fake_ytdlp, tmp_path):
    fake_ytdlp.respond(stdout="", rc=0)
    with pytest.raises(NoMedia):
        run(downloader.fetch_info("https://x.invalid/v", str(tmp_path)))


def test_fetch_info_timeout(fake_ytdlp, tmp_path):
    fake_ytdlp.respond(stdout="{}", sleep=30)
    with pytest.raises(DownloadTimeout, match="yt-dlp extraction timed out after 1s"):
        run(downloader.fetch_info("https://x.invalid/v", str(tmp_path), timeout=1))


def test_cookies_file_is_copied_and_never_rewritten(fake_ytdlp, monkeypatch, tmp_path):
    source = tmp_path / "secret" / "cookies.txt"
    source.parent.mkdir()
    source.write_text(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tabc\n"
    )
    source.chmod(0o444)  # like a Kubernetes Secret mount
    original = source.read_bytes()
    monkeypatch.setattr(config, "YTDLP_COOKIES_PATH", str(source))
    fake_ytdlp.respond(stdout=dump_single_json(site_tiktok(30)))
    workdir = tmp_path / "work"
    run(downloader.fetch_info("https://www.tiktok.com/@u/video/1", str(workdir)))
    [call] = fake_ytdlp.calls()
    used = call[call.index("--cookies") + 1]
    assert used != str(source)
    assert os.path.dirname(used) == str(workdir)
    assert not os.path.exists(used)  # the private copy is removed after the run
    assert source.read_bytes() == original
    assert stat.S_IMODE(source.stat().st_mode) == 0o444


def test_missing_cookies_file_is_not_fatal(fake_ytdlp, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "YTDLP_COOKIES_PATH", str(tmp_path / "missing.txt"))
    fake_ytdlp.respond(stdout=dump_single_json(site_tiktok(30)))
    run(downloader.fetch_info("https://www.tiktok.com/@u/video/1", str(tmp_path / "w")))
    [call] = fake_ytdlp.calls()
    assert "--cookies" not in call


# ---------------------------------------------------------------------------
# End to end: real yt-dlp, loopback HTTP server, --load-info-json
# ---------------------------------------------------------------------------

REAL_YTDLP_WRAPPER = textwrap.dedent("""
    import json, os, sys
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_ARGV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\\n")
    if "--" in argv:  # extraction: replace the URL with the raw extractor output
        argv = argv[: argv.index("--")] + [
            "--no-clean-info-json", "--load-info-json", os.environ["FAKE_RAW_INFO"]
        ]
    os.execv(sys.executable, [sys.executable, "-m", "yt_dlp", *argv])
    """)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return


class _StreamingHandler(_QuietHandler):
    """Streams /stream without Content-Length (like chunked CDN responses)."""

    def do_GET(self):
        if not self.path.startswith("/stream"):
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.end_headers()
        try:
            for _ in range(200):
                self.wfile.write(b"\0" * 256 * 1024)
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            return None
        return None


@pytest.fixture
def media_server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    handler = functools.partial(_StreamingHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def real_ytdlp(monkeypatch, tmp_path):
    script = tmp_path / "ytdlp_wrapper.py"
    script.write_text(REAL_YTDLP_WRAPPER)
    raw_path = tmp_path / "raw_info.json"
    argv_log = tmp_path / "argv.log"
    argv_log.touch()
    monkeypatch.setattr(
        downloader, "ytdlp_command", lambda: [sys.executable, str(script)]
    )
    monkeypatch.setenv("FAKE_RAW_INFO", str(raw_path))
    monkeypatch.setenv("FAKE_ARGV_LOG", str(argv_log))

    def set_raw(raw):
        raw_path.write_text(json.dumps(raw))

    set_raw.calls = lambda: [
        json.loads(line) for line in argv_log.read_text().splitlines()
    ]
    return set_raw


def _ffmpeg(*args):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True, timeout=60
    )


def _http_format(base, root, name, fid, **fields):
    fmt = {"format_id": fid, "url": f"{base}/{name}", "protocol": "http"}
    path = root / name
    if path.exists():
        fmt["filesize"] = path.stat().st_size
    fmt.update(fields)
    return fmt


def _raw_video(formats, *, vid="vid1", duration=2):
    return {
        "id": vid,
        "title": f"Title {vid}",
        "duration": duration,
        "extractor": "testsite",
        "extractor_key": "TestSite",
        "webpage_url": f"https://example.invalid/{vid}",
        "formats": formats,
    }


def test_download_progressive_end_to_end(real_ytdlp, media_server, tmp_path):
    root, base = media_server
    (root / "clip.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + os.urandom(20_000))
    raw = _raw_video(
        [
            _http_format(
                base,
                root,
                "clip.mp4",
                "http-720",
                ext="mp4",
                vcodec="h264",
                acodec="aac",
                width=1280,
                height=720,
            )
        ]
    )
    real_ytdlp(raw)
    workdir = tmp_path / "work"
    info = run(downloader.fetch_info("https://example.invalid/vid1", str(workdir)))
    assert info.items[0].format_id == "http-720"
    assert info.items[0].direct_url == f"{base}/clip.mp4"
    [downloaded] = run(downloader.download(info, str(workdir)))
    assert downloaded.path == str(workdir / "item-01.mp4")
    assert downloaded.size == (root / "clip.mp4").stat().st_size
    assert downloaded.item is info.items[0]
    calls = real_ytdlp.calls()
    assert len(calls) == 2  # one extraction, one download: no re-extraction
    assert "--load-info-json" in calls[1] and "--" not in calls[1]


@needs_ffmpeg
def test_download_merges_separate_video_and_audio(real_ytdlp, media_server, tmp_path):
    root, base = media_server
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=25",
        "-t",
        "2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(root / "v.mp4"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440",
        "-t",
        "2",
        "-c:a",
        "aac",
        str(root / "a.m4a"),
    )
    raw = _raw_video(
        [
            _http_format(
                base,
                root,
                "v.mp4",
                "137",
                ext="mp4",
                vcodec="avc1.4d401e",
                acodec="none",
                width=320,
                height=240,
            ),
            _http_format(
                base, root, "a.m4a", "140", ext="m4a", vcodec="none", acodec="mp4a.40.2"
            ),
        ]
    )
    real_ytdlp(raw)
    workdir = tmp_path / "work"
    info = run(downloader.fetch_info("https://example.invalid/vid1", str(workdir)))
    assert info.items[0].format_id == "137+140"
    [downloaded] = run(downloader.download(info, str(workdir)))
    assert downloaded.path == str(workdir / "item-01.mp4")
    probe = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                downloaded.path,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert sorted(s["codec_type"] for s in probe["streams"]) == ["audio", "video"]
    leftovers = [p.name for p in workdir.iterdir() if p.name.startswith("item-01.f")]
    assert leftovers == []


def test_download_skipped_by_max_filesize_is_too_large(
    real_ytdlp, media_server, monkeypatch, tmp_path
):
    root, base = media_server
    (root / "big.mp4").write_bytes(os.urandom(3 * MIB))
    # size unknown to the extractor: only the HTTP Content-Length reveals it
    raw = _raw_video(
        [
            {
                "format_id": "http",
                "url": f"{base}/big.mp4",
                "protocol": "http",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "aac",
            }
        ]
    )
    real_ytdlp(raw)
    monkeypatch.setattr(config, "MAX_DOWNLOAD_BYTES", MIB)
    workdir = tmp_path / "work"
    info = run(downloader.fetch_info("https://example.invalid/vid1", str(workdir)))
    with pytest.raises(TooLarge, match="max-filesize"):
        run(downloader.download(info, str(workdir)))


@needs_ffmpeg
def test_partial_merge_never_returns_the_audio_part(
    real_ytdlp, media_server, monkeypatch, tmp_path
):
    root, base = media_server
    (root / "v.mp4").write_bytes(os.urandom(3 * MIB))
    _ffmpeg("-f", "lavfi", "-i", "sine", "-t", "2", "-c:a", "aac", str(root / "a.m4a"))
    raw = _raw_video(
        [
            {
                "format_id": "137",
                "url": f"{base}/v.mp4",
                "protocol": "http",
                "ext": "mp4",
                "vcodec": "avc1.4d401e",
                "acodec": "none",
                "width": 1920,
                "height": 1080,
            },
            _http_format(
                base, root, "a.m4a", "140", ext="m4a", vcodec="none", acodec="mp4a.40.2"
            ),
        ]
    )
    real_ytdlp(raw)
    monkeypatch.setattr(config, "MAX_DOWNLOAD_BYTES", MIB)
    workdir = tmp_path / "work"
    info = run(downloader.fetch_info("https://example.invalid/vid1", str(workdir)))
    assert info.items[0].kind == "video"
    with pytest.raises(TooLarge):
        run(downloader.download(info, str(workdir)))


def test_streaming_without_content_length_is_capped(
    real_ytdlp, media_server, monkeypatch, tmp_path
):
    _, base = media_server
    raw = _raw_video(
        [
            {
                "format_id": "http",
                "url": f"{base}/stream.mp4",
                "protocol": "http",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "aac",
            }
        ]
    )
    real_ytdlp(raw)
    monkeypatch.setattr(config, "MAX_DOWNLOAD_BYTES", MIB)
    workdir = tmp_path / "work"
    info = run(downloader.fetch_info("https://example.invalid/vid1", str(workdir)))
    started = time.monotonic()
    with pytest.raises(TooLarge, match="download exceeded 1 MiB"):
        run(downloader.download(info, str(workdir)))
    assert time.monotonic() - started < 20


def test_download_multi_item_post_in_order(real_ytdlp, media_server, tmp_path):
    root, base = media_server
    entries = []
    for n in (1, 2, 3):
        (root / f"clip{n}.mp4").write_bytes(os.urandom(10_000 + n))
        entry = _raw_video(
            [
                _http_format(
                    base,
                    root,
                    f"clip{n}.mp4",
                    f"http-{n}",
                    ext="mp4",
                    vcodec="h264",
                    acodec="aac",
                    width=720,
                    height=1280,
                )
            ],
            vid=f"media{n}",
        )
        entries.append(entry)
    entries[1]["formats"] = []  # an image: yt-dlp reports an error for this entry only
    real_ytdlp(_playlist(entries))
    workdir = tmp_path / "work"
    info = run(downloader.fetch_info("https://example.invalid/p/post", str(workdir)))
    assert [item.index for item in info.items] == [1, 3]
    assert info.skipped == {2: "no_media"}
    files = run(downloader.download(info, str(workdir)))
    assert [os.path.basename(f.path) for f in files] == ["item-01.mp4", "item-03.mp4"]
    assert [f.size for f in files] == [10_001, 10_003]
    assert [f.item.id for f in files] == ["media1", "media3"]
    only_last = run(downloader.download(info, str(workdir), items=[3]))
    assert [f.item.index for f in only_last] == [3]
    with pytest.raises(ValueError, match="unknown item"):
        run(downloader.download(info, str(workdir), items=[2]))


def _fake_media_info(tmp_path, count):
    items = [
        downloader.MediaItem(
            index=n,
            id=f"id{n}",
            title=None,
            kind="video",
            duration=10.0,
            width=720,
            height=1280,
            filesize=1000,
            direct_url=None,
            direct_ext=None,
            http_headers={},
            thumbnail=None,
            uploader=None,
            webpage_url=None,
        )
        for n in range(1, count + 1)
    ]
    return MediaInfo(
        url="https://example.invalid/p",
        extractor_key="Test",
        title=None,
        is_live=False,
        items=items,
        info_path=str(tmp_path / "info.json"),
        raw={"_type": "playlist"},
        entry_count=count,
    )


def test_multi_item_failures_are_skipped_unless_all_fail(monkeypatch, tmp_path):
    info = _fake_media_info(tmp_path, 3)

    async def fake_item(info, item, workdir, *, time_limit):
        if item.index == 2:
            raise TooLarge("item 2 is huge")
        return downloader.DownloadedFile(f"/w/item-{item.index:02d}.mp4", 10, item)

    monkeypatch.setattr(downloader, "_download_item", fake_item)
    files = run(downloader.download(info, str(tmp_path)))
    assert [f.item.index for f in files] == [1, 3]

    async def always_fail(info, item, workdir, *, time_limit):
        raise AuthRequired(f"item {item.index}")

    monkeypatch.setattr(downloader, "_download_item", always_fail)
    with pytest.raises(AuthRequired, match="item 1"):
        run(downloader.download(info, str(tmp_path)))
    single = _fake_media_info(tmp_path, 1)
    with pytest.raises(AuthRequired):
        run(downloader.download(single, str(tmp_path)))


def test_download_timeout_bounds_the_whole_call(monkeypatch, tmp_path):
    info = _fake_media_info(tmp_path, 3)
    limits = []

    async def slow_item(info, item, workdir, *, time_limit):
        limits.append(time_limit)
        await asyncio.sleep(0.3)
        return downloader.DownloadedFile("/w/x.mp4", 1, item)

    monkeypatch.setattr(downloader, "_download_item", slow_item)
    files = run(downloader.download(info, str(tmp_path), timeout=0.5))
    assert [f.item.index for f in files] == [1, 2]  # no time left for item 3
    assert limits[0] == pytest.approx(0.5, abs=0.05)
    assert limits[1] < limits[0]
    limits.clear()
    run(downloader.download(info, str(tmp_path)))
    assert limits == [120.0, 120.0, 120.0]  # DOWNLOAD_TIMEOUT per item


def test_download_rejects_unknown_keywords(tmp_path):
    with pytest.raises(TypeError, match="max_bytes"):
        run(
            downloader.download(
                _fake_media_info(tmp_path, 1), str(tmp_path), max_bytes=1
            )
        )
