import asyncio
import glob
import json
import logging
import os
import shutil
import sys
from asyncio.subprocess import PIPE
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


async def download(
    url: str,
    dest_dir: str,
    timeout: int = 300,
    max_bytes: Optional[int] = None,
    notify=None,
) -> (str, dict):
    """
    Download a media resource using yt-dlp to `dest_dir` and return the downloaded file path.
    If `max_bytes` is provided, pass it to yt-dlp as `--max-filesize` to abort early when possible.
    """
    os.makedirs(dest_dir, exist_ok=True)
    out_template = os.path.join(dest_dir, "%(id)s.%(ext)s")
    logger.debug("download(): dest_dir=%s out_template=%s", dest_dir, out_template)
    # track spawned subprocesses so we can terminate them on cancellation/exit
    procs: set = set()

    async def _cleanup_procs():
        # best-effort: terminate any remaining child processes to avoid subprocess
        # transports being garbage-collected after the event loop is closed
        for p in list(procs):
            try:
                if getattr(p, "returncode", None) is None:
                    try:
                        p.kill()
                    except Exception:
                        pass
                    try:
                        await p.communicate()
                    except Exception:
                        pass
            except Exception:
                pass

    async def _spawn(*cmd_args):
        p = await asyncio.create_subprocess_exec(*cmd_args, stdout=PIPE, stderr=PIPE)
        procs.add(p)
        return p

    async def _await_proc(p, to: int | None = None):
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout=to)
            return out, err
        except asyncio.TimeoutError:
            try:
                p.kill()
            except Exception:
                pass
            try:
                await p.communicate()
            except Exception:
                pass
            raise
        except asyncio.CancelledError:
            try:
                p.kill()
            except Exception:
                pass
            try:
                await p.communicate()
            except Exception:
                pass
            raise
        finally:
            try:
                procs.discard(p)
            except Exception:
                pass

    # prefer merging video+audio and produce mp4 when possible
    # resolve yt-dlp binary if available; fall back to running as a module
    yt_dlp_bin = shutil.which("yt-dlp")
    if yt_dlp_bin:
        base_cmd = [yt_dlp_bin]
    else:
        base_cmd = [sys.executable, "-m", "yt_dlp"]
    # Try to prefer H.264/AVC streams from the source before falling back to
    # generic bestvideo selection. This avoids downloading VP9/AV1 streams that
    # some Telegram clients cannot play in MP4 containers.
    format_candidates = [
        "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio/best",
        "bestvideo[vcodec^=avc1]+bestaudio/best",
        "bestvideo[ext=mp4]+bestaudio/best",
        "bestvideo+bestaudio/best",
    ]

    proc = None
    stdout = stderr = b""
    for fmt in format_candidates:
        cmd = base_cmd + ["--no-playlist", "-f", fmt, "-o", out_template]
        if max_bytes:
            cmd += ["--max-filesize", str(max_bytes)]
        cmd += [url]

        logger.debug("Trying yt-dlp format '%s': %s", fmt, " ".join(cmd))
        proc = await _spawn(*cmd)
        logger.debug(
            "spawned downloader proc=%s (fmt=%s)",
            getattr(proc, "returncode", None),
            fmt,
        )
        stdout, stderr = await _await_proc(proc, to=timeout)
        logger.debug("after yt-dlp attempt for fmt=%s", fmt)
        try:
            stdout_str = stdout.decode(errors="ignore") if stdout else ""
        except Exception:
            stdout_str = "<decoding error>"
        try:
            stderr_str = stderr.decode(errors="ignore") if stderr else ""
        except Exception:
            stderr_str = "<decoding error>"
        logger.debug("yt-dlp stdout (truncated): %s", stdout_str[:2000])
        logger.debug("yt-dlp stderr (truncated): %s", stderr_str[:2000])

        # If this attempt succeeded and produced a file, stop trying further formats
        if proc.returncode == 0:
            files = glob.glob(os.path.join(dest_dir, "*"))
            if files:
                latest = max(files, key=os.path.getmtime)
                logger.debug("picked latest file %s after fmt=%s", latest, fmt)
                break
            # no files produced despite exit code 0 — try next candidate

    if proc is None or (getattr(proc, "returncode", None) != 0):
        err = stderr.decode(errors="ignore") if stderr else ""
        logger.error("yt-dlp failed (all format attempts): %s", err)
        await _cleanup_procs()
        raise RuntimeError(f"yt-dlp failed: {err.strip()[:200]}")

    # if we fell through above but didn't set `latest`, pick newest file now
    if "latest" not in locals():
        files = glob.glob(os.path.join(dest_dir, "*"))
        if not files:
            await _cleanup_procs()
            raise RuntimeError("no file downloaded")
        latest = max(files, key=os.path.getmtime)
        logger.debug("picked latest file %s", latest)

    # pick the newest file in dest_dir (if not already set)
    # Note: stdout/stderr and returncode were already handled in the
    # format-candidates loop above; avoid duplicating that logic here.
    try:
        size = os.path.getsize(latest)
        orig_size = size
        logger.info("Downloaded file: %s (%d bytes)", latest, size)
    except Exception:
        orig_size = None
        logger.debug("Could not stat downloaded file: %s", latest)

    # inspect file/container/codec info when possible and prefer preserving original
    audio_exts = {"m4a", "mp3", "aac", "wav", "ogg", "opus"}
    ext = os.path.splitext(latest)[1].lower().lstrip(".")

    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    fmt = ""
    video_codec = None
    audio_codec = None
    has_video = False
    has_audio = False
    if ffprobe_bin:
        try:
            p = await _spawn(
                ffprobe_bin,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                latest,
            )
            outp, errp = await _await_proc(p)
            if outp:
                try:
                    info = json.loads(outp)
                    streams = info.get("streams", [])
                    fmt = info.get("format", {}).get("format_name", "") or ""
                    # capture some useful stream-level metadata for orientation-aware transcoding
                    video_width = None
                    video_height = None
                    video_bit_rate = None
                    video_profile = None
                    for s in streams:
                        if s.get("codec_type") == "video":
                            has_video = True
                            video_codec = s.get("codec_name")
                            video_profile = s.get("profile")
                            try:
                                video_width = int(s.get("width") or 0) or None
                            except Exception:
                                video_width = None
                            try:
                                video_height = int(s.get("height") or 0) or None
                            except Exception:
                                video_height = None
                            video_bit_rate = s.get("bit_rate") or None
                        elif s.get("codec_type") == "audio":
                            has_audio = True
                            audio_codec = s.get("codec_name")
                except Exception:
                    logger.debug("Could not parse ffprobe output")
        except Exception:
            logger.debug("ffprobe execution failed")
    # Log a concise ffprobe summary for debugging/diagnostics
    try:
        logger.info(
            "ffprobe: has_video=%s has_audio=%s video_codec=%s audio_codec=%s format=%s",
            has_video,
            has_audio,
            video_codec,
            audio_codec,
            fmt,
        )
    except Exception:
        pass

    logger.debug(
        "after ffprobe: has_video=%s has_audio=%s ext=%s fmt=%s",
        has_video,
        has_audio,
        ext,
        fmt,
    )
    logger.debug(
        "checking compatibility: ffmpeg_bin=%s yt_dlp_bin=%s", ffmpeg_bin, yt_dlp_bin
    )
    # DEBUG TRACING
    # import pdb; pdb.set_trace()

    # If we have a video+audio file but codecs are not widely compatible (e.g. vp9/opus),
    # transcode to h264/aac MP4 for better client compatibility. If codecs are compatible
    # but container is not MP4, remux to MP4 with faststart.
    if has_video and has_audio:
        preferred_video = ("h264", "mpeg4")
        preferred_audio = ("aac", "mp3", "mp4a")
        incompatible = False
        if (video_codec and video_codec not in preferred_video) or (
            audio_codec and audio_codec not in preferred_audio
        ):
            incompatible = True
        # Treat H.264 with non-baseline profile (e.g. Main/High) as incompatible so we can re-encode to baseline
        try:
            if (
                video_codec == "h264"
                and video_profile
                and "baseline" not in video_profile.lower()
            ):
                incompatible = True
        except Exception:
            pass
        if incompatible:
            # First, attempt a yt-dlp recode to mp4 (may use ffmpeg internally).
            # This is a lighter-weight fallback than a full manual ffmpeg transcode
            # and can succeed when a source can be re-encoded to H.264 by yt-dlp.
            if yt_dlp_bin:
                try:
                    logger.info(
                        "Attempting yt-dlp --recode-video mp4 fallback for %s", latest
                    )
                    recode_cmd = base_cmd + [
                        "--no-playlist",
                        "--recode-video",
                        "mp4",
                        "-o",
                        out_template,
                        url,
                    ]
                    if max_bytes:
                        recode_cmd = recode_cmd[:-1] + [
                            "--max-filesize",
                            str(max_bytes),
                            url,
                        ]
                    logger.debug("Running recode command: %s", " ".join(recode_cmd))
                    proc_recode = await _spawn(*recode_cmd)
                    try:
                        stdout_re, stderr_re = await _await_proc(
                            proc_recode, to=timeout
                        )
                    except asyncio.TimeoutError:
                        logger.warning("yt-dlp recode timed out")
                    else:
                        if proc_recode.returncode == 0:
                            files = glob.glob(os.path.join(dest_dir, "*"))
                            if files:
                                latest_candidate = max(files, key=os.path.getmtime)
                                if latest_candidate and latest_candidate != latest:
                                    latest = latest_candidate
                                    ext = (
                                        os.path.splitext(latest)[1].lower().lstrip(".")
                                    )
                                    # re-run ffprobe on the recoded file to refresh codec info
                                    if ffprobe_bin:
                                        try:
                                            p2 = await _spawn(
                                                ffprobe_bin,
                                                "-v",
                                                "error",
                                                "-print_format",
                                                "json",
                                                "-show_streams",
                                                "-show_format",
                                                latest,
                                            )
                                            out2, err2 = await _await_proc(p2)
                                            if out2:
                                                try:
                                                    info2 = json.loads(out2)
                                                    streams2 = info2.get("streams", [])
                                                    fmt = (
                                                        info2.get("format", {}).get(
                                                            "format_name", ""
                                                        )
                                                        or ""
                                                    )
                                                    # reset codec vars and capture profile/size info
                                                    video_codec = None
                                                    audio_codec = None
                                                    has_video = False
                                                    has_audio = False
                                                    video_width = None
                                                    video_height = None
                                                    video_bit_rate = None
                                                    video_profile = None
                                                    for s in streams2:
                                                        if (
                                                            s.get("codec_type")
                                                            == "video"
                                                        ):
                                                            has_video = True
                                                            video_codec = s.get(
                                                                "codec_name"
                                                            )
                                                            video_profile = s.get(
                                                                "profile"
                                                            )
                                                            try:
                                                                video_width = (
                                                                    int(
                                                                        s.get("width")
                                                                        or 0
                                                                    )
                                                                    or None
                                                                )
                                                            except Exception:
                                                                video_width = None
                                                            try:
                                                                video_height = (
                                                                    int(
                                                                        s.get("height")
                                                                        or 0
                                                                    )
                                                                    or None
                                                                )
                                                            except Exception:
                                                                video_height = None
                                                            video_bit_rate = (
                                                                s.get("bit_rate")
                                                                or None
                                                            )
                                                        elif (
                                                            s.get("codec_type")
                                                            == "audio"
                                                        ):
                                                            has_audio = True
                                                            audio_codec = s.get(
                                                                "codec_name"
                                                            )
                                                except Exception:
                                                    logger.debug(
                                                        "Could not parse ffprobe output for recoded file"
                                                    )
                                        except Exception:
                                            logger.debug(
                                                "ffprobe execution failed for recoded file"
                                            )
                                    # recompute compatibility
                                    if (
                                        video_codec and video_codec in preferred_video
                                    ) and (
                                        audio_codec and audio_codec in preferred_audio
                                    ):
                                        try:
                                            size = os.path.getsize(latest)
                                            logger.info(
                                                "yt-dlp recode produced compatible file: %s (%d bytes)",
                                                latest,
                                                size,
                                            )
                                        except Exception:
                                            logger.debug(
                                                "Could not stat recoded file: %s",
                                                latest,
                                            )
                                        incompatible = False
                except Exception:
                    logger.exception("yt-dlp recode failed")

            # If still incompatible, fall back to manual ffmpeg transcode (if available)
            try:
                base = os.path.splitext(os.path.basename(latest))[0]
                trans_path = os.path.join(dest_dir, f"{base}_transcoded.mp4")

                # choose orientation-aware scale/pad target
                try:
                    is_portrait = False
                    if video_width and video_height:
                        is_portrait = int(video_height) >= int(video_width)
                except Exception:
                    is_portrait = False

                if is_portrait:
                    target_w = 720
                    pad_w = 720
                    pad_h = 1280
                else:
                    target_w = 640
                    pad_w = 640
                    pad_h = 360

                # determine reasonable video bitrate (fall back to 1780k)
                try:
                    if video_bit_rate:
                        # ffprobe bit_rate is in bps, convert to kbps
                        kb = int(int(video_bit_rate) / 1000)
                        vb = f"{kb}k" if kb > 0 else "1780k"
                    else:
                        vb = "1780k"
                except Exception:
                    vb = "1780k"

                # Preserve original aspect ratio; avoid forcing fixed-pad frames
                scale_filter = f"scale=w={target_w}:h=-2:force_original_aspect_ratio=decrease,setsar=1"

                ffmpeg_cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-max_muxing_queue_size",
                    "9999",
                    "-i",
                    latest,
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-profile:v",
                    "baseline",
                    "-pix_fmt",
                    "yuv420p",
                    "-b:v",
                    vb,
                    "-maxrate",
                    vb,
                    "-bufsize",
                    str(int(int(vb.rstrip("k")) * 2000)),
                    "-vf",
                    scale_filter,
                    "-c:a",
                    "aac",
                    "-b:a",
                    "65k",
                    "-ac",
                    "2",
                    "-ar",
                    "44100",
                    "-movflags",
                    "+faststart",
                    trans_path,
                ]
                logger.info("Transcoding %s to mp4/h264 for compatibility", latest)
                proc4 = await _spawn(*ffmpeg_cmd)
                try:
                    out4, err4 = await _await_proc(proc4, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("ffmpeg transcode timed out")
                else:
                    if proc4.returncode == 0 and os.path.exists(trans_path):
                        latest = trans_path
                        ext = "mp4"
                        try:
                            size = os.path.getsize(latest)
                            logger.info("Transcoded file: %s (%d bytes)", latest, size)
                        except Exception:
                            logger.debug("Could not stat transcoded file: %s", latest)
                        # re-run ffprobe on the transcoded file to refresh codec/profile info
                        if ffprobe_bin:
                            try:
                                p3 = await _spawn(
                                    ffprobe_bin,
                                    "-v",
                                    "error",
                                    "-print_format",
                                    "json",
                                    "-show_streams",
                                    "-show_format",
                                    latest,
                                )
                                out3, err3 = await _await_proc(p3)
                                if out3:
                                    try:
                                        info3 = json.loads(out3)
                                        streams3 = info3.get("streams", [])
                                        fmt = (
                                            info3.get("format", {}).get(
                                                "format_name", ""
                                            )
                                            or ""
                                        )
                                        # reset codec vars and capture profile/size info
                                        video_codec = None
                                        audio_codec = None
                                        has_video = False
                                        has_audio = False
                                        video_width = None
                                        video_height = None
                                        video_bit_rate = None
                                        video_profile = None
                                        for s in streams3:
                                            if s.get("codec_type") == "video":
                                                has_video = True
                                                video_codec = s.get("codec_name")
                                                video_profile = s.get("profile")
                                                try:
                                                    video_width = (
                                                        int(s.get("width") or 0) or None
                                                    )
                                                except Exception:
                                                    video_width = None
                                                try:
                                                    video_height = (
                                                        int(s.get("height") or 0)
                                                        or None
                                                    )
                                                except Exception:
                                                    video_height = None
                                                video_bit_rate = (
                                                    s.get("bit_rate") or None
                                                )
                                            elif s.get("codec_type") == "audio":
                                                has_audio = True
                                                audio_codec = s.get("codec_name")
                                    except Exception:
                                        logger.debug(
                                            "Could not parse ffprobe output for transcoded file"
                                        )
                            except Exception:
                                logger.debug(
                                    "ffprobe execution failed for transcoded file"
                                )
            except Exception:
                logger.exception("Transcoding failed")
        else:
            # compatible codecs: remux to MP4 if not already MP4 to improve playback (faststart)
            if fmt and "mp4" not in fmt:
                try:
                    base = os.path.splitext(os.path.basename(latest))[0]
                    remux_path = os.path.join(dest_dir, f"{base}_remuxed.mp4")
                    ffmpeg_cmd = [
                        ffmpeg_bin,
                        "-y",
                        "-max_muxing_queue_size",
                        "9999",
                        "-i",
                        latest,
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        remux_path,
                    ]
                    logger.info("Remuxing %s to mp4 container", latest)
                    proc5 = await _spawn(*ffmpeg_cmd)
                    try:
                        out5, err5 = await _await_proc(proc5, to=timeout)
                    except asyncio.TimeoutError:
                        logger.warning("ffmpeg remux timed out")
                    else:
                        if proc5.returncode == 0 and os.path.exists(remux_path):
                            latest = remux_path
                            ext = "mp4"
                            try:
                                size = os.path.getsize(latest)
                                logger.info("Remuxed file: %s (%d bytes)", latest, size)
                            except Exception:
                                logger.debug("Could not stat remuxed file: %s", latest)
                except Exception:
                    logger.exception("Remux failed")

    # if yt-dlp returned an audio-only file (e.g. .m4a) try to package it into mp4 for Telegram
    if ext in audio_exts:
        logger.info(
            "Downloaded audio-only file (%s). Attempting to wrap into mp4 for Telegram compatibility.",
            ext,
        )
        # prefer ffmpeg wrapper (black video) if available
        if ffmpeg_bin:
            try:
                base = os.path.splitext(os.path.basename(latest))[0]
                mp4_path = os.path.join(dest_dir, f"{base}_wrapped.mp4")
                ffmpeg_cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-max_muxing_queue_size",
                    "9999",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=640x360:r=25",
                    "-i",
                    latest,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "faster",
                    "-crf",
                    "23",
                    "-maxrate",
                    "4.5M",
                    "-flags",
                    "+global_header",
                    "-pix_fmt",
                    "yuv420p",
                    "-profile:v",
                    "baseline",
                    "-c:a",
                    "aac",
                    "-ac",
                    "2",
                    "-b:a",
                    "128k",
                    "-vf",
                    "setsar=1",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    mp4_path,
                ]
                proc3 = await _spawn(*ffmpeg_cmd)
                try:
                    stdout3, stderr3 = await _await_proc(proc3, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("ffmpeg conversion timed out")
                else:
                    if proc3.returncode == 0 and os.path.exists(mp4_path):
                        latest = mp4_path
                        ext = "mp4"
                        try:
                            size = os.path.getsize(latest)
                            logger.info(
                                "After ffmpeg wrapper, file: %s (%d bytes)",
                                latest,
                                size,
                            )
                        except Exception:
                            logger.debug(
                                "Could not stat ffmpeg wrapper file: %s", latest
                            )
            except Exception:
                logger.exception("ffmpeg wrapper failed")
        else:
            # fallback: attempt a single recode with yt-dlp if available
            if yt_dlp_bin:
                logger.info("ffmpeg not available; attempting yt-dlp recode to mp4")
                recode_cmd = [
                    yt_dlp_bin,
                    "--no-playlist",
                    "--recode-video",
                    "mp4",
                    "-o",
                    out_template,
                    url,
                ]
                logger.debug("Running recode command: %s", " ".join(recode_cmd))
                proc2 = await _spawn(*recode_cmd)
                try:
                    stdout2, stderr2 = await _await_proc(proc2, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("Recode attempt timed out")
                else:
                    if proc2.returncode == 0:
                        files = glob.glob(os.path.join(dest_dir, "*"))
                        latest = max(files, key=os.path.getmtime)
                        ext = os.path.splitext(latest)[1].lower().lstrip(".")
                        try:
                            size = os.path.getsize(latest)
                            logger.info(
                                "After recode, file: %s (%d bytes)", latest, size
                            )
                        except Exception:
                            logger.debug("Could not stat recoded file: %s", latest)
    # NOTE: audio-only handling is done earlier (attempt wrapper/recode). Keep original audio
    # when possible; no additional blanket wrapper here to avoid unnecessary aspect-ratio issues.
    # final size check: if too large, attempt recompression or redownload (see above)
    size = os.path.getsize(latest)

    # If file is too large, attempt to recompress with ffmpeg to fit the Telegram limit.
    target = config.TELEGRAM_MAX_FILE_SIZE
    compressed_flag = False
    if target and size > target:
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            logger.info(
                "File %s (%d bytes) exceeds target %d bytes, attempting recompression",
                latest,
                size,
                target,
            )
            # notify compression start
            try:
                if notify:
                    await notify(
                        {
                            "type": "compress_start",
                            "original_size": size,
                            "target": target,
                        }
                    )
            except Exception:
                logger.debug("notify compress_start failed")

            async def _try_compress(
                input_path: str, dest_dir: str, target_bytes: int
            ) -> Optional[str]:
                base = os.path.splitext(os.path.basename(input_path))[0]
                out_path = os.path.join(dest_dir, f"{base}.recompressed.mp4")

                # try decreasing quality and resolution combinations
                scale_heights = [360, 240, 180]
                crf_values = [28, 30, 32, 34, 36]

                for h in scale_heights:
                    for crf in crf_values:
                        if os.path.exists(out_path):
                            try:
                                os.remove(out_path)
                            except Exception:
                                pass
                        cmd = [
                            ffmpeg_bin,
                            "-y",
                            "-max_muxing_queue_size",
                            "9999",
                            "-i",
                            input_path,
                            "-vf",
                            "scale=w=640:h=-2:force_original_aspect_ratio=decrease,setsar=1",
                            "-c:v",
                            "libx264",
                            "-preset",
                            "faster",
                            "-crf",
                            str(crf),
                            "-maxrate",
                            "4.5M",
                            "-flags",
                            "+global_header",
                            "-pix_fmt",
                            "yuv420p",
                            "-profile:v",
                            "baseline",
                            "-c:a",
                            "aac",
                            "-ac",
                            "2",
                            "-b:a",
                            "128k",
                            "-movflags",
                            "+faststart",
                            out_path,
                        ]
                        logger.debug("Running ffmpeg recompress: %s", " ".join(cmd))
                        try:
                            proc = await _spawn(*cmd)
                            try:
                                stdout, stderr = await _await_proc(proc, to=300)
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "ffmpeg recompress timed out for crf=%s h=%s",
                                    crf,
                                    h,
                                )
                                continue
                            try:
                                out_str = (
                                    stdout.decode(errors="ignore") if stdout else ""
                                )
                            except Exception:
                                out_str = "<decoding error>"
                            try:
                                err_str = (
                                    stderr.decode(errors="ignore") if stderr else ""
                                )
                            except Exception:
                                err_str = "<decoding error>"
                            logger.debug(
                                "ffmpeg recompress stdout (truncated): %s",
                                out_str[:2000],
                            )
                            logger.debug(
                                "ffmpeg recompress stderr (truncated): %s",
                                err_str[:2000],
                            )
                            if proc.returncode != 0:
                                logger.debug(
                                    "ffmpeg returned non-zero (%s). stderr=%s",
                                    proc.returncode,
                                    err_str,
                                )
                                continue
                            if os.path.exists(out_path):
                                new_size = os.path.getsize(out_path)
                                logger.info(
                                    "Recompressed file size=%d (target=%d) with crf=%s h=%s",
                                    new_size,
                                    target_bytes,
                                    crf,
                                    h,
                                )
                                if new_size <= target_bytes:
                                    return out_path
                                # otherwise keep trying
                        except Exception:
                            logger.exception("Exception while attempting recompress")
                            continue
                return None

            try:
                compressed = await _try_compress(
                    latest, os.path.dirname(latest), target
                )
                if compressed:
                    logger.info("Compression successful, using %s", compressed)
                    latest = compressed
                    size = os.path.getsize(latest)
                    compressed_flag = True
                    try:
                        if notify:
                            await notify({"type": "compress_done", "new_size": size})
                    except Exception:
                        logger.debug("notify compress_done failed")
                else:
                    logger.info(
                        "Compression attempts exhausted; file remains too large (%d bytes)",
                        size,
                    )
            except Exception:
                logger.exception("Compression step failed")
        else:
            logger.warning(
                "ffmpeg not available; attempting redownload with lower-quality formats"
            )
            # Try progressively lower-quality downloads using yt-dlp format selectors
            formats_to_try = [
                "bestvideo[height<=360]+bestaudio/best",
                "bestvideo[height<=240]+bestaudio/best",
                "bestvideo[height<=180]+bestaudio/best",
                "bestaudio/best",
            ]
            for fmt in formats_to_try:
                # clear existing files in dest_dir before attempting a fresh download
                for f in glob.glob(os.path.join(dest_dir, "*")):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
                rd_cmd = base_cmd + [
                    "--no-playlist",
                    "-f",
                    fmt,
                    "-o",
                    out_template,
                    url,
                ]
                logger.info("Attempting redownload with format %s", fmt)
                try:
                    if notify:
                        await notify({"type": "redownload_start", "format": fmt})
                except Exception:
                    logger.debug("notify redownload_start failed")
                logger.debug("Running redownload command: %s", " ".join(rd_cmd))
                proc_rd = await _spawn(*rd_cmd)
                try:
                    out_r, err_r = await _await_proc(proc_rd, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("Redownload with format %s timed out", fmt)
                    continue
                try:
                    out_r_str = out_r.decode(errors="ignore") if out_r else ""
                except Exception:
                    out_r_str = "<decoding error>"
                try:
                    err_r_str = err_r.decode(errors="ignore") if err_r else ""
                except Exception:
                    err_r_str = "<decoding error>"
                logger.debug("redownload stdout (truncated): %s", out_r_str[:2000])
                logger.debug("redownload stderr (truncated): %s", err_r_str[:2000])
                files2 = glob.glob(os.path.join(dest_dir, "*"))
                if not files2:
                    logger.info("No file produced for format %s", fmt)
                    continue
                latest = max(files2, key=os.path.getmtime)
                try:
                    size = os.path.getsize(latest)
                    logger.info(
                        "After redownload format=%s, file=%s size=%d", fmt, latest, size
                    )
                except Exception:
                    logger.debug("Could not stat redownloaded file: %s", latest)
                if size <= target:
                    logger.info("Redownload succeeded with format=%s", fmt)
                    compressed_flag = True
                    try:
                        if notify:
                            await notify({"type": "redownload_done", "new_size": size})
                    except Exception:
                        logger.debug("notify redownload_done failed")
                    break
            else:
                logger.info(
                    "All redownload attempts exhausted; file remains too large (%d bytes)",
                    size,
                )

        if config.TELEGRAM_MAX_FILE_SIZE and size > config.TELEGRAM_MAX_FILE_SIZE:
            await _cleanup_procs()
            raise RuntimeError(f"downloaded file too large ({size} bytes)")
    await _cleanup_procs()

    # return metadata for the downloaded file in all code paths
    meta = {
        "compressed": compressed_flag,
        "original_size": orig_size,
        "final_size": size,
        "has_video": has_video,
        "has_audio": has_audio,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "format": fmt,
        "video_width": locals().get("video_width"),
        "video_height": locals().get("video_height"),
        "video_profile": locals().get("video_profile"),
    }
    logger.debug("download(): returning latest=%s final_size=%s", latest, size)
    return latest, meta
