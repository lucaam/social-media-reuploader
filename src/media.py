"""ffprobe/ffmpeg helpers: probe media, make thumbnails, make files Telegram-ready.

:func:`prepare_for_telegram` decides ONE strategy up front from the probe:

* compatible (MP4, H.264 + AAC/MP3 or silent) and within the budget: nothing;
* compatible codecs in another container: lossless remux (``-c copy``);
* H.264 with incompatible audio: copy the video, re-encode the audio;
* other codecs that fit: CRF re-encode with a VBV cap derived from the budget;
* too large: size-targeted re-encode (bitrate from the budget, audio bitrate
  scaled down for long media, aspect ratio kept, never padded, downscaled
  only when the bitrate requires it); the output size is verified and the
  encode retried once with a lower bitrate; impossible budgets raise
  :class:`src.downloader.TooLarge`.

Every output is re-probed, so the returned metadata (dimensions, codecs,
size) describes the file that will actually be sent. CPU-heavy encodes are
limited by a module-wide semaphore (``TRANSCODE_CONCURRENCY``) and every
ffmpeg run is killed with its process group on timeout or cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
import weakref
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .downloader import (
    ConversionFailed,
    DownloadError,
    DownloadTimeout,
    NoMedia,
    TooLarge,
    run_command,
    setting,
)

logger = logging.getLogger(__name__)

KIB = 1024
MIB = 1024 * 1024

PROBE_TIMEOUT = 30.0
THUMBNAIL_TIMEOUT = 30.0
PROBE_CAPTURE_LIMIT = 1024 * KIB
THUMB_MAX_SIDE = 320
THUMB_MAX_BYTES = 200 * KIB

# --- encode planning --------------------------------------------------------
# Share of the byte budget given to the encoder (rate-control error margin).
RATE_SAFETY = 0.94
# MP4 container overhead (moov sample tables grow with duration).
CONTAINER_OVERHEAD_BYTES = 4 * KIB
CONTAINER_OVERHEAD_BYTES_PER_SECOND = 600
MIN_VIDEO_KBPS = 64
MIN_AUDIO_KBPS = 32
MAX_AUDIO_ONLY_KBPS = 192
DEFAULT_AUDIO_KBPS = 128
# Minimum bits per pixel per frame before the planner lowers the resolution.
MIN_BITS_PER_PIXEL = 0.05
# Hard cap for any encode (CPU), and the resolution ladder used when the
# bitrate requires downscaling (after TRANSCODE_MAX_SIDE).
ENCODE_MAX_SIDE = 1920
SIDE_LADDER = (1280, 960, 854, 640, 480, 426, 320, 256)
COMPRESS_FPS_CAP = 30.0
DEFAULT_FPS = 30.0
CRF = 23
RETRY_SAFETY = 0.92
# (minimum total kbps, audio kbps, channels) for videos, best first.
AUDIO_LADDER: tuple[tuple[float, int, int], ...] = (
    (1500.0, 128, 2),
    (800.0, 96, 2),
    (400.0, 64, 2),
    (200.0, 48, 1),
    (0.0, 32, 1),
)

_MP4_FAMILY = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
_VIDEO_EXTS = {".mp4", ".m4v"}
_COMPATIBLE_PIX_FMTS = {"yuv420p", "yuvj420p"}
_VIDEO_AUDIO_CODECS = {"aac", "mp3"}
_GUESS_VIDEO_EXTS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mkv",
    ".flv",
    ".ts",
    ".avi",
    ".3gp",
}


class Action(StrEnum):
    NONE = "none"
    REMUX = "remux"
    TRANSCODE = "transcode"
    COMPRESS = "compress"


@dataclass
class ProbeResult:
    has_video: bool
    has_audio: bool
    duration: float | None
    width: int | None  # coded size
    height: int | None
    display_width: int | None  # rotation and sample aspect ratio applied
    display_height: int | None
    vcodec: str | None
    acodec: str | None
    container: str | None  # "mp4" (whole ISO-BMFF family), "webm", "matroska", ...
    size: int
    rotation: int | None  # clockwise degrees needed for display: 0/90/180/270
    # additive fields
    pix_fmt: str | None = None
    sample_aspect_ratio: float | None = None
    fps: float | None = None
    bit_rate: int | None = None
    video_bit_rate: int | None = None
    audio_bit_rate: int | None = None
    audio_channels: int | None = None
    video_index: int | None = None
    audio_index: int | None = None
    format_name: str | None = None

    @property
    def square_pixels(self) -> bool:
        sar = self.sample_aspect_ratio
        return sar is None or abs(sar - 1.0) < 0.01


@dataclass
class PreparedMedia:
    path: str
    probe: ProbeResult
    compressed: bool  # True only for a size-targeted re-encode
    action: str  # Action value
    original_size: int | None = None


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def _ratio(value: Any) -> float | None:
    """``"4:3"``/``"30000/1001"`` -> float; ``"0:1"``/``"N/A"`` -> None."""
    if not isinstance(value, str):
        return None
    for sep in (":", "/"):
        if sep in value:
            num_s, _, den_s = value.partition(sep)
            num, den = _to_float(num_s), _to_float(den_s)
            if num and den:
                return num / den
            return None
    return _to_float(value) or None


def _is_attached_pic(stream: dict[str, Any]) -> bool:
    disposition = stream.get("disposition") or {}
    return _to_int(disposition.get("attached_pic")) == 1


def _rotation(stream: dict[str, Any]) -> int:
    """Clockwise display rotation, normalized to 0/90/180/270."""
    degrees: float | None = None
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and side.get("rotation") is not None:
            value = _to_float(side.get("rotation"))
            if value is not None:
                degrees = -value  # display matrix rotation is counter-clockwise
            break
    if degrees is None:
        degrees = _to_float((stream.get("tags") or {}).get("rotate"))
    if degrees is None:
        return 0
    return (round(degrees / 90.0) * 90) % 360


def _container(format_name: str | None, path: str | None) -> str | None:
    if not format_name:
        return None
    names = {name.strip() for name in format_name.split(",")}
    if names & _MP4_FAMILY:
        return "mp4"
    if "matroska" in names or "webm" in names:
        ext = os.path.splitext(path or "")[1].lower()
        return "webm" if ext == ".webm" else "matroska"
    return format_name.split(",")[0].strip()


def parse_probe(
    data: dict[str, Any], *, size: int, path: str | None = None
) -> ProbeResult:
    """Build a :class:`ProbeResult` from ``ffprobe -show_streams -show_format`` JSON."""
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not _is_attached_pic(s)
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    duration = next(
        (
            d
            for d in (
                _to_float(fmt.get("duration")),
                _to_float((video or {}).get("duration")),
                _to_float((audio or {}).get("duration")),
            )
            if d is not None and d > 0
        ),
        None,
    )
    width = height = display_width = display_height = None
    rotation: int | None = None
    sar: float | None = None
    fps: float | None = None
    if video is not None:
        width = _to_int(video.get("width")) or None
        height = _to_int(video.get("height")) or None
        rotation = _rotation(video)
        sar = _ratio(video.get("sample_aspect_ratio"))
        fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
        if width and height:
            display_width = max(1, round(width * sar)) if sar else width
            display_height = height
            if rotation in (90, 270):
                display_width, display_height = display_height, display_width
    format_name = fmt.get("format_name") or None
    return ProbeResult(
        has_video=video is not None,
        has_audio=audio is not None,
        duration=duration,
        width=width,
        height=height,
        display_width=display_width,
        display_height=display_height,
        vcodec=(video or {}).get("codec_name") or None,
        acodec=(audio or {}).get("codec_name") or None,
        container=_container(format_name, path),
        size=size,
        rotation=rotation,
        pix_fmt=(video or {}).get("pix_fmt") or None,
        sample_aspect_ratio=sar,
        fps=fps,
        bit_rate=_to_int(fmt.get("bit_rate")),
        video_bit_rate=_to_int((video or {}).get("bit_rate")),
        audio_bit_rate=_to_int((audio or {}).get("bit_rate")),
        audio_channels=_to_int((audio or {}).get("channels")),
        video_index=_to_int((video or {}).get("index")),
        audio_index=_to_int((audio or {}).get("index")),
        format_name=format_name,
    )


def _ff_path(path: str) -> str:
    """Absolute ``file:`` URL so a path is never parsed as an option/protocol."""
    return "file:" + os.path.abspath(path)


async def probe(path: str) -> ProbeResult | None:
    """ffprobe ``path``; ``None`` when ffprobe is missing or cannot read it."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        logger.warning(
            "ffprobe is not installed: cannot inspect %s", os.path.basename(path)
        )
        return None
    argv = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        _ff_path(path),
    ]
    try:
        size = await asyncio.to_thread(os.path.getsize, path)
        result = await run_command(
            argv,
            time_limit=PROBE_TIMEOUT,
            label="ffprobe",
            capture_limit=PROBE_CAPTURE_LIMIT,
        )
    except (OSError, DownloadError) as exc:
        logger.warning("ffprobe failed for %s: %s", os.path.basename(path), exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "ffprobe exited with %s for %s: %s",
            result.returncode,
            os.path.basename(path),
            _last_lines(result.stderr),
        )
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("ffprobe printed invalid JSON for %s", os.path.basename(path))
        return None
    if not isinstance(data, dict):
        return None
    return parse_probe(data, size=size, path=path)


def _video_stream_ok(p: ProbeResult) -> bool:
    return (
        p.vcodec == "h264"
        and (p.pix_fmt is None or p.pix_fmt in _COMPATIBLE_PIX_FMTS)
        and p.square_pixels
    )


def _audio_ok_with_video(p: ProbeResult) -> bool:
    return not p.has_audio or p.acodec in _VIDEO_AUDIO_CODECS


def is_telegram_compatible(p: ProbeResult, path: str) -> bool:
    """True when Telegram clients play ``path`` inline as it is.

    Video: MP4 container with ``.mp4``/``.m4v`` name, H.264 (8-bit 4:2:0,
    square pixels; rotation metadata is fine) and AAC/MP3 audio or no audio.
    Audio only (``sendAudio``): MP3 in ``.mp3`` or AAC in ``.m4a``.
    """
    ext = os.path.splitext(path)[1].lower()
    if p.has_video:
        return (
            p.container == "mp4"
            and ext in _VIDEO_EXTS
            and _video_stream_ok(p)
            and _audio_ok_with_video(p)
        )
    if p.has_audio:
        if p.acodec == "mp3":
            return p.container == "mp3" and ext == ".mp3"
        if p.acodec == "aac":
            return p.container == "mp4" and ext == ".m4a"
    return False


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


def _thumbnail_seek(duration: float | None) -> float:
    if duration is None:
        return 1.0
    if duration < 1.0:
        return 0.0
    return min(duration * 0.1, 3.0)


async def _render_thumbnail(
    ffmpeg: str, src: str, dst: str, seek: float, quality: int
) -> bool:
    argv = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{seek:.3f}",
        "-i",
        _ff_path(src),
        "-an",
        "-sn",
        "-frames:v",
        "1",
        "-vf",
        (
            "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
            f"scale={THUMB_MAX_SIDE}:{THUMB_MAX_SIDE}:force_original_aspect_ratio=decrease"
        ),
        "-q:v",
        str(quality),
        "-f",
        "image2",
        "-update",
        "1",
        _ff_path(dst),
    ]
    try:
        result = await run_command(
            argv, time_limit=THUMBNAIL_TIMEOUT, label="thumbnail"
        )
    except DownloadError as exc:
        logger.warning(
            "thumbnail extraction failed for %s: %s", os.path.basename(src), exc
        )
        return False
    if result.returncode != 0:
        logger.debug("thumbnail ffmpeg failed: %s", _last_lines(result.stderr))
        return False
    return _file_size(dst) > 0


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


async def make_thumbnail(
    path: str, out_dir: str, *, duration: float | None = None
) -> str | None:
    """JPEG thumbnail (longest side <= 320 px, <= 200 KB) or ``None``.

    The frame is taken ~10% into the video (at most 3 s, first frame for very
    short clips or when seeking fails); rotation and non-square pixels are
    applied so the thumbnail matches what players show.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    dst = os.path.join(out_dir, f"{stem}.thumb.jpg")
    seek = _thumbnail_seek(duration)
    seeks = [seek, 0.0] if seek > 0 else [0.0]
    for position in seeks:
        for quality in (3, 8, 15):
            if not await _render_thumbnail(ffmpeg, path, dst, position, quality):
                break
            if _file_size(dst) <= THUMB_MAX_BYTES:
                return dst
    _unlink(dst)
    return None


# ---------------------------------------------------------------------------
# Encode planning (pure functions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoPlan:
    width: int
    height: int
    fps: float | None  # output frame-rate cap (None: keep the source rate)
    video_kbps: int  # ABR target, or the VBV cap in CRF mode (0: no cap)
    crf: int | None  # None: size-targeted ABR
    audio_kbps: int | None  # None: no audio, or copied (see copy_audio)
    audio_channels: int | None
    copy_audio: bool = False


@dataclass(frozen=True)
class AudioPlan:
    kbps: int
    channels: int


def payload_kbps(max_bytes: int, duration: float) -> float:
    """Total audio+video kbps that fits ``max_bytes`` for ``duration`` seconds."""
    if duration <= 0:
        return 0.0
    overhead = CONTAINER_OVERHEAD_BYTES + CONTAINER_OVERHEAD_BYTES_PER_SECOND * duration
    usable = max_bytes * RATE_SAFETY - overhead
    return max(0.0, usable * 8 / duration / 1000)


def plan_audio(
    total_kbps: float,
    *,
    source_kbps: float | None = None,
    source_channels: int | None = None,
) -> tuple[int, int]:
    """Audio bitrate/channels for a video encode of ``total_kbps``.

    Long media (low total bitrate) gets less audio: 128k stereo down to 32k
    mono. Never above the source bitrate or channel count.
    """
    kbps, channels = AUDIO_LADDER[-1][1], AUDIO_LADDER[-1][2]
    for threshold, ladder_kbps, ladder_channels in AUDIO_LADDER:
        if total_kbps >= threshold:
            kbps, channels = ladder_kbps, ladder_channels
            break
    if source_kbps:
        kbps = max(MIN_AUDIO_KBPS, min(kbps, int(source_kbps)))
    if source_channels:
        channels = max(1, min(channels, source_channels))
    return kbps, channels


def _even_floor(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def fit_dimensions(width: int, height: int, max_side: int) -> tuple[int, int]:
    """Scale ``width x height`` so the longest side is <= ``max_side``.

    Keeps the aspect ratio, never upscales, returns even dimensions (x264).
    """
    longest = max(width, height)
    scale = min(1.0, max_side / longest) if longest else 1.0
    return _even_floor(width * scale), _even_floor(height * scale)


def plan_dimensions(
    width: int,
    height: int,
    video_kbps: float,
    fps: float,
    *,
    downscale_side: int,
    max_side: int = ENCODE_MAX_SIDE,
) -> tuple[int, int]:
    """Largest output size whose bits/pixel/frame stay >= ``MIN_BITS_PER_PIXEL``.

    The source size is kept when the bitrate allows it; the first downscale
    step is ``downscale_side`` (``TRANSCODE_MAX_SIDE``), then ``SIDE_LADDER``.
    """
    start = min(max(width, height), max_side)
    sides = sorted(
        {start, *(side for side in (downscale_side, *SIDE_LADDER) if side < start)},
        reverse=True,
    )
    rate = max(fps, 1.0)
    for side in sides:
        out_w, out_h = fit_dimensions(width, height, side)
        if video_kbps * 1000 / (out_w * out_h * rate) >= MIN_BITS_PER_PIXEL:
            return out_w, out_h
    return fit_dimensions(width, height, sides[-1])


def _source_fps(p: ProbeResult) -> float:
    return p.fps if p.fps and 1.0 <= p.fps <= 240.0 else DEFAULT_FPS


def _transcode_max_side() -> int:
    try:
        return max(240, int(setting("TRANSCODE_MAX_SIDE", 1280)))
    except (TypeError, ValueError):
        return 1280


def _display_size(p: ProbeResult) -> tuple[int, int]:
    width = p.display_width or p.width
    height = p.display_height or p.height
    if not width or not height:
        raise ConversionFailed("cannot convert: unknown video dimensions")
    return width, height


def _audio_copyable(p: ProbeResult, kbps: int | None) -> bool:
    if not p.has_audio or p.acodec not in _VIDEO_AUDIO_CODECS:
        return False
    if kbps is None:
        return True
    return p.audio_bit_rate is not None and p.audio_bit_rate <= kbps * 1000 * 1.1


def plan_video_encode(
    p: ProbeResult,
    max_bytes: int,
    *,
    size_targeted: bool,
    bitrate_scale: float = 1.0,
) -> VideoPlan:
    """Plan an H.264 encode of ``p``.

    ``size_targeted``: ABR whose bitrate comes from ``max_bytes`` (raises
    :class:`TooLarge` when the video bitrate would drop below
    ``MIN_VIDEO_KBPS``). Otherwise CRF with a VBV cap derived from the budget.
    """
    width, height = _display_size(p)
    src_fps = _source_fps(p)
    source_audio_kbps = p.audio_bit_rate / 1000 if p.audio_bit_rate else None
    duration = p.duration
    if size_targeted:
        if not duration:
            raise TooLarge("cannot compress a file of unknown duration")
        total = payload_kbps(max_bytes, duration) * bitrate_scale
        audio_kbps = channels = None
        if p.has_audio:
            audio_kbps, channels = plan_audio(
                total, source_kbps=source_audio_kbps, source_channels=p.audio_channels
            )
        video_kbps = total - (audio_kbps or 0)
        if video_kbps < MIN_VIDEO_KBPS:
            raise TooLarge(
                f"{duration:.0f}s of video cannot fit {max_bytes / MIB:.1f} MiB "
                f"(video bitrate would be {max(video_kbps, 0):.0f} kbps)"
            )
        fps_cap = COMPRESS_FPS_CAP if src_fps > COMPRESS_FPS_CAP + 0.5 else None
        out_w, out_h = plan_dimensions(
            width,
            height,
            video_kbps,
            min(src_fps, COMPRESS_FPS_CAP),
            downscale_side=_transcode_max_side(),
        )
        copy_audio = _audio_copyable(p, audio_kbps)
        return VideoPlan(
            width=out_w,
            height=out_h,
            fps=fps_cap,
            video_kbps=int(video_kbps),
            crf=None,
            audio_kbps=None if copy_audio else audio_kbps,
            audio_channels=None if copy_audio else channels,
            copy_audio=copy_audio,
        )

    # CRF re-encode for compatibility: quality driven, but capped (VBV) so
    # that the result still fits the budget; the resolution is lowered only
    # when that cap is too low for the source size.
    copy_audio = _audio_copyable(p, None)
    total = payload_kbps(max_bytes, duration) if duration else None
    audio_kbps = channels = None
    if p.has_audio and not copy_audio:
        if total is None:
            audio_kbps, channels = DEFAULT_AUDIO_KBPS, min(p.audio_channels or 2, 2)
        else:
            audio_kbps, channels = plan_audio(
                total, source_kbps=source_audio_kbps, source_channels=p.audio_channels
            )
    cap = 0.0
    if total is not None:
        if audio_kbps is not None:
            used_audio = float(audio_kbps)
        elif copy_audio:
            used_audio = source_audio_kbps or float(DEFAULT_AUDIO_KBPS)
        else:
            used_audio = 0.0
        cap = max(total - used_audio, float(MIN_VIDEO_KBPS))
    out_w, out_h = plan_dimensions(
        width,
        height,
        cap if cap else float("inf"),
        src_fps,
        downscale_side=_transcode_max_side(),
    )
    return VideoPlan(
        width=out_w,
        height=out_h,
        fps=None,
        video_kbps=int(cap),
        crf=CRF,
        audio_kbps=audio_kbps,
        audio_channels=channels,
        copy_audio=copy_audio,
    )


def plan_audio_encode(
    p: ProbeResult,
    max_bytes: int,
    *,
    size_targeted: bool,
    bitrate_scale: float = 1.0,
) -> AudioPlan:
    """Plan an AAC encode of an audio-only file."""
    source_kbps = (p.audio_bit_rate or p.bit_rate or 0) / 1000 or None
    if size_targeted:
        if not p.duration:
            raise TooLarge("cannot compress a file of unknown duration")
        budget = payload_kbps(max_bytes, p.duration) * bitrate_scale
        kbps = min(budget, MAX_AUDIO_ONLY_KBPS, source_kbps or MAX_AUDIO_ONLY_KBPS)
        if kbps < MIN_AUDIO_KBPS:
            raise TooLarge(
                f"{p.duration:.0f}s of audio cannot fit {max_bytes / MIB:.1f} MiB "
                f"(bitrate would be {max(kbps, 0):.0f} kbps)"
            )
    else:
        kbps = DEFAULT_AUDIO_KBPS if source_kbps is None else source_kbps * 1.25
        kbps = min(max(kbps, 64.0), float(MAX_AUDIO_ONLY_KBPS))
        if p.duration:
            kbps = min(
                kbps, max(payload_kbps(max_bytes, p.duration), float(MIN_AUDIO_KBPS))
            )
    channels = min(p.audio_channels or 2, 2) if kbps >= 64 else 1
    return AudioPlan(kbps=int(kbps), channels=channels)


# ---------------------------------------------------------------------------
# ffmpeg command lines (pure)
# ---------------------------------------------------------------------------


def _ffmpeg_head(ffmpeg: str, src: str) -> list[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        "-y",
        "-i",
        _ff_path(src),
    ]


def _ffmpeg_tail(dst: str, muxer: str) -> list[str]:
    tail = ["-max_muxing_queue_size", "1024"]
    if muxer == "mp4":
        tail += ["-movflags", "+faststart"]
    return [*tail, "-f", muxer, _ff_path(dst)]


def _map(index: int | None, fallback: str) -> list[str]:
    return ["-map", f"0:{index}" if index is not None else fallback]


def remux_argv(ffmpeg: str, src: str, dst: str, p: ProbeResult) -> list[str]:
    """Lossless copy of the main streams into MP4 (video) or M4A/MP3 (audio)."""
    argv = _ffmpeg_head(ffmpeg, src)
    if p.has_video:
        argv += _map(p.video_index, "0:v:0")
    if p.has_audio:
        argv += _map(p.audio_index, "0:a:0?")
    argv += ["-c", "copy"]
    muxer = "mp3" if (not p.has_video and p.acodec == "mp3") else "mp4"
    return argv + _ffmpeg_tail(dst, muxer)


def audio_fix_argv(
    ffmpeg: str, src: str, dst: str, p: ProbeResult, kbps: int
) -> list[str]:
    """Copy the (compatible) video stream, re-encode the audio to AAC."""
    argv = _ffmpeg_head(ffmpeg, src)
    argv += _map(p.video_index, "0:v:0") + _map(p.audio_index, "0:a:0?")
    argv += [
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        f"{kbps}k",
        "-ac",
        str(min(p.audio_channels or 2, 2)),
    ]
    return argv + _ffmpeg_tail(dst, "mp4")


def video_encode_argv(
    ffmpeg: str, src: str, dst: str, p: ProbeResult, plan: VideoPlan, *, preset: str
) -> list[str]:
    """H.264 (high profile, yuv420p) + AAC MP4 following ``plan``.

    ffmpeg auto-rotates while decoding, so ``plan`` dimensions are display
    dimensions; ``setsar=1`` makes pixels square (no padding, ever).
    """
    argv = _ffmpeg_head(ffmpeg, src)
    argv += _map(p.video_index, "0:v:0")
    has_audio = p.has_audio and (plan.copy_audio or plan.audio_kbps is not None)
    if has_audio:
        argv += _map(p.audio_index, "0:a:0?")
    filters = [f"scale={plan.width}:{plan.height}", "setsar=1"]
    if plan.fps:
        filters.append(f"fps={plan.fps:g}")
    argv += [
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
    ]
    kbps = plan.video_kbps
    if plan.crf is not None:
        argv += ["-crf", str(plan.crf)]
        if kbps > 0:
            argv += ["-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k"]
    else:
        argv += [
            "-b:v",
            f"{kbps}k",
            "-maxrate",
            f"{int(kbps * 1.5)}k",
            "-bufsize",
            f"{2 * kbps}k",
        ]
    if not has_audio:
        argv += ["-an"]
    elif plan.copy_audio:
        argv += ["-c:a", "copy"]
    else:
        argv += [
            "-c:a",
            "aac",
            "-b:a",
            f"{plan.audio_kbps}k",
            "-ac",
            str(plan.audio_channels or 2),
        ]
    return argv + _ffmpeg_tail(dst, "mp4")


def audio_encode_argv(
    ffmpeg: str, src: str, dst: str, p: ProbeResult, plan: AudioPlan
) -> list[str]:
    """Audio-only AAC (M4A) encode following ``plan`` (cover art dropped)."""
    argv = _ffmpeg_head(ffmpeg, src)
    argv += _map(p.audio_index, "0:a:0")
    argv += ["-vn", "-c:a", "aac", "-b:a", f"{plan.kbps}k", "-ac", str(plan.channels)]
    return argv + _ffmpeg_tail(dst, "mp4")


# ---------------------------------------------------------------------------
# Running conversions
# ---------------------------------------------------------------------------

_SLOTS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def transcode_semaphore() -> asyncio.Semaphore:
    """Module-wide limit of concurrent encodes (``TRANSCODE_CONCURRENCY``)."""
    loop = asyncio.get_running_loop()
    try:
        limit = max(1, int(setting("TRANSCODE_CONCURRENCY", 1)))
    except (TypeError, ValueError):
        limit = 1
    current = _SLOTS.get(loop)
    if current is None or current[0] != limit:
        current = (limit, asyncio.Semaphore(limit))
        _SLOTS[loop] = current
    return current[1]


def _last_lines(text: str, count: int = 3) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " | ".join(lines[-count:]) or "no error output"


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self._loop = asyncio.get_running_loop()
        self._end = self._loop.time() + seconds
        self.seconds = seconds

    def remaining(self, label: str) -> float:
        left = self._end - self._loop.time()
        if left <= 0:
            raise DownloadTimeout(
                f"{label}: conversion time limit ({self.seconds:g}s) exhausted"
            )
        return left


@dataclass
class _Job:
    ffmpeg: str
    src: str
    workdir: str
    probe: ProbeResult
    max_bytes: int
    original_size: int
    deadline: _Deadline

    def output(self, ext: str) -> str:
        stem = os.path.splitext(os.path.basename(self.src))[0]
        return os.path.join(self.workdir, f"{stem}.tg{ext}")

    def fits(self, prepared: PreparedMedia) -> bool:
        return prepared.probe.size <= self.max_bytes and is_telegram_compatible(
            prepared.probe, prepared.path
        )

    async def run(
        self, argv: list[str], dst: str, action: Action, label: str
    ) -> PreparedMedia:
        """Run one ffmpeg command and probe its output (ConversionFailed on error)."""
        logger.info(
            "%s: %s -> %s", label, os.path.basename(self.src), os.path.basename(dst)
        )
        try:
            result = await run_command(
                argv, time_limit=self.deadline.remaining(label), label=label
            )
        except BaseException:
            _unlink(dst)
            raise
        if result.returncode != 0:
            _unlink(dst)
            raise ConversionFailed(
                f"{label} failed: {_last_lines(result.stderr)}",
                stderr_tail=result.stderr,
            )
        out = await probe(dst)
        if out is None:
            raise ConversionFailed(f"{label}: the output cannot be read back")
        logger.info(
            "%s done: %.1f MiB -> %.1f MiB (%sx%s %s/%s)",
            label,
            self.original_size / MIB,
            out.size / MIB,
            out.display_width,
            out.display_height,
            out.vcodec,
            out.acodec,
        )
        return PreparedMedia(
            dst, out, action == Action.COMPRESS, action, self.original_size
        )

    async def try_run(
        self, argv: list[str], dst: str, action: Action, label: str
    ) -> PreparedMedia | None:
        """Like :meth:`run`, but a failure returns ``None`` so that the caller
        falls back to a heavier strategy (timeouts/cancellation still raise)."""
        try:
            return await self.run(argv, dst, action, label)
        except ConversionFailed as exc:
            logger.warning("%s; falling back to a full conversion", exc.detail)
            return None

    def preset(self) -> str:
        return str(setting("TRANSCODE_PRESET", "veryfast") or "veryfast")

    async def video(self) -> PreparedMedia:
        p = self.probe
        dst = self.output(".mp4")
        if self.original_size > self.max_bytes:
            # Too large: only a size-targeted encode helps (never a remux).
            return await self.compress_video(dst)
        cheap: PreparedMedia | None = None
        if _video_stream_ok(p) and _audio_ok_with_video(p):
            argv = remux_argv(self.ffmpeg, self.src, dst, p)
            cheap = await self.try_run(argv, dst, Action.REMUX, "remux")
        elif _video_stream_ok(p):
            kbps = plan_audio_encode(p, self.max_bytes, size_targeted=False).kbps
            argv = audio_fix_argv(self.ffmpeg, self.src, dst, p, kbps)
            cheap = await self.try_run(argv, dst, Action.TRANSCODE, "audio re-encode")
        if cheap is not None:
            return cheap if self.fits(cheap) else await self.compress_video(dst)
        plan = plan_video_encode(p, self.max_bytes, size_targeted=False)
        argv = video_encode_argv(
            self.ffmpeg, self.src, dst, p, plan, preset=self.preset()
        )
        async with transcode_semaphore():
            prepared = await self.run(argv, dst, Action.TRANSCODE, "transcode")
        if self.fits(prepared):
            return prepared
        return await self.compress_video(dst)

    async def compress_video(self, dst: str) -> PreparedMedia:
        async with transcode_semaphore():
            return await self._compress(dst, video=True)

    async def compress_audio(self, dst: str) -> PreparedMedia:
        return await self._compress(dst, video=False)

    async def _compress(self, dst: str, *, video: bool) -> PreparedMedia:
        """Size-targeted encode; verified, retried once with a lower bitrate."""
        scale = 1.0
        try:
            for attempt in (1, 2):
                if video:
                    plan = plan_video_encode(
                        self.probe,
                        self.max_bytes,
                        size_targeted=True,
                        bitrate_scale=scale,
                    )
                    argv = video_encode_argv(
                        self.ffmpeg,
                        self.src,
                        dst,
                        self.probe,
                        plan,
                        preset=self.preset(),
                    )
                    label = f"compress (attempt {attempt})"
                else:
                    audio_plan = plan_audio_encode(
                        self.probe,
                        self.max_bytes,
                        size_targeted=True,
                        bitrate_scale=scale,
                    )
                    argv = audio_encode_argv(
                        self.ffmpeg, self.src, dst, self.probe, audio_plan
                    )
                    label = f"audio compress (attempt {attempt})"
                prepared = await self.run(argv, dst, Action.COMPRESS, label)
                if prepared.probe.size <= self.max_bytes:
                    return prepared
                scale *= self.max_bytes / prepared.probe.size * RETRY_SAFETY
                logger.info(
                    "compressed output is %d bytes > %d: retrying with %.0f%% of the bitrate",
                    prepared.probe.size,
                    self.max_bytes,
                    scale * 100,
                )
        except TooLarge:
            _unlink(dst)
            raise
        _unlink(dst)
        raise TooLarge(f"could not compress below {self.max_bytes / MIB:.1f} MiB")

    async def audio(self) -> PreparedMedia:
        p = self.probe
        m4a = self.output(".m4a")
        if self.original_size > self.max_bytes:
            return await self.compress_audio(m4a)
        if p.acodec in _VIDEO_AUDIO_CODECS:
            dst = self.output(".mp3") if p.acodec == "mp3" else m4a
            argv = remux_argv(self.ffmpeg, self.src, dst, p)
            cheap = await self.try_run(argv, dst, Action.REMUX, "audio remux")
            if cheap is not None:
                return cheap if self.fits(cheap) else await self.compress_audio(m4a)
        plan = plan_audio_encode(p, self.max_bytes, size_targeted=False)
        argv = audio_encode_argv(self.ffmpeg, self.src, m4a, p, plan)
        prepared = await self.run(argv, m4a, Action.TRANSCODE, "audio transcode")
        if self.fits(prepared):
            return prepared
        return await self.compress_audio(m4a)


def _unlink(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def _unprobed(path: str, size: int, max_bytes: int) -> PreparedMedia:
    """ffprobe is missing: send the file as it is when it fits (best effort)."""
    if size > max_bytes:
        raise TooLarge(
            f"file is {size / MIB:.1f} MiB and ffprobe is not available to compress it"
        )
    ext = os.path.splitext(path)[1].lower()
    guessed = ProbeResult(
        has_video=ext in _GUESS_VIDEO_EXTS,
        has_audio=True,
        duration=None,
        width=None,
        height=None,
        display_width=None,
        display_height=None,
        vcodec=None,
        acodec=None,
        container="mp4" if ext in (".mp4", ".m4v", ".m4a", ".mov") else None,
        size=size,
        rotation=None,
    )
    return PreparedMedia(path, guessed, False, Action.NONE, size)


async def prepare_for_telegram(
    path: str,
    workdir: str,
    *,
    max_bytes: int,
    probe_result: ProbeResult | None = None,
) -> PreparedMedia:
    """Return a file Telegram can play inline and that fits ``max_bytes``.

    See the module docstring for the strategy. ``PreparedMedia.probe``
    always describes ``PreparedMedia.path`` (re-probed after conversion), so
    use its ``display_width``/``display_height``/``duration`` for sendVideo.
    Raises :class:`TooLarge` (cannot fit), :class:`NoMedia` (no streams),
    :class:`ConversionFailed` (ffmpeg error/missing) or
    :class:`DownloadTimeout` (``TRANSCODE_TIMEOUT`` exceeded; ffmpeg killed).
    With ``SIMPLE_YTDLP_ONLY`` the file is never converted.
    """
    original_size = await asyncio.to_thread(os.path.getsize, path)
    p = probe_result if probe_result is not None else await probe(path)
    if p is None:
        if shutil.which("ffprobe") is None:
            return _unprobed(path, original_size, max_bytes)
        raise ConversionFailed(f"ffprobe cannot read {os.path.basename(path)}")
    if p.size != original_size:
        p = replace(p, size=original_size)
    if not p.has_video and not p.has_audio:
        raise NoMedia("the downloaded file has no audio or video stream")
    fits = original_size <= max_bytes
    if fits and is_telegram_compatible(p, path):
        return PreparedMedia(path, p, False, Action.NONE, original_size)
    if setting("SIMPLE_YTDLP_ONLY", False):
        if fits:
            return PreparedMedia(path, p, False, Action.NONE, original_size)
        raise TooLarge(
            f"file is {original_size / MIB:.1f} MiB and SIMPLE_YTDLP_ONLY forbids compression"
        )
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ConversionFailed(
            "ffmpeg is not installed: cannot convert the file for Telegram"
        )
    try:
        seconds = float(setting("TRANSCODE_TIMEOUT", 900))
    except (TypeError, ValueError):
        seconds = 900.0
    if seconds <= 0:
        seconds = 900.0
    job = _Job(ffmpeg, path, workdir, p, max_bytes, original_size, _Deadline(seconds))
    if p.has_video:
        return await job.video()
    return await job.audio()
