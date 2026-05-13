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
    # prefer merging video+audio and produce mp4 when possible
    # resolve yt-dlp binary if available; fall back to running as a module
    yt_dlp_bin = shutil.which("yt-dlp")
    if yt_dlp_bin:
        base_cmd = [yt_dlp_bin]
    else:
        base_cmd = [sys.executable, "-m", "yt_dlp"]
    cmd = base_cmd + [
        "--no-playlist",
        "-f",
        "bestvideo+bestaudio/best",
        "--merge-output-format",
        "mp4",
        "-o",
        out_template,
    ]
    if max_bytes:
        # yt-dlp accepts raw bytes or human readable (e.g. 50M); pass raw bytes
        cmd += ["--max-filesize", str(max_bytes)]
    cmd += [url]

    logger.debug("Running download command: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    # log subprocess output (truncated) for debugging
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

    if proc.returncode != 0:
        err = stderr_str
        logger.error("yt-dlp failed: %s", err)
        raise RuntimeError(f"yt-dlp failed: {err.strip()[:200]}")

    # pick the newest file in dest_dir
    files = glob.glob(os.path.join(dest_dir, "*"))
    if not files:
        raise RuntimeError("no file downloaded")
    latest = max(files, key=os.path.getmtime)
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
            p = await asyncio.create_subprocess_exec(
                ffprobe_bin,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                latest,
                stdout=PIPE,
                stderr=PIPE,
            )
            outp, errp = await p.communicate()
            if outp:
                try:
                    info = json.loads(outp)
                    streams = info.get("streams", [])
                    fmt = info.get("format", {}).get("format_name", "") or ""
                    for s in streams:
                        if s.get("codec_type") == "video":
                            has_video = True
                            video_codec = s.get("codec_name")
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

    # If we have a video+audio file but codecs are not widely compatible (e.g. vp9/opus),
    # transcode to h264/aac MP4 for better client compatibility. If codecs are compatible
    # but container is not MP4, remux to MP4 with faststart.
    if has_video and has_audio and ffmpeg_bin:
        preferred_video = ("h264", "mpeg4")
        preferred_audio = ("aac", "mp3", "mp4a")
        incompatible = False
        if (video_codec and video_codec not in preferred_video) or (
            audio_codec and audio_codec not in preferred_audio
        ):
            incompatible = True
        if incompatible:
            try:
                base = os.path.splitext(os.path.basename(latest))[0]
                trans_path = os.path.join(dest_dir, f"{base}_transcoded.mp4")
                ffmpeg_cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-i",
                    latest,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    trans_path,
                ]
                logger.info("Transcoding %s to mp4/h264 for compatibility", latest)
                proc4 = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd, stdout=PIPE, stderr=PIPE
                )
                try:
                    out4, err4 = await asyncio.wait_for(
                        proc4.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc4.kill()
                    await proc4.communicate()
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
                        "-i",
                        latest,
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        remux_path,
                    ]
                    logger.info("Remuxing %s to mp4 container", latest)
                    proc5 = await asyncio.create_subprocess_exec(
                        *ffmpeg_cmd, stdout=PIPE, stderr=PIPE
                    )
                    try:
                        out5, err5 = await asyncio.wait_for(
                            proc5.communicate(), timeout=timeout
                        )
                    except asyncio.TimeoutError:
                        proc5.kill()
                        await proc5.communicate()
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
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=640x360",
                    "-i",
                    latest,
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    mp4_path,
                ]
                proc3 = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd, stdout=PIPE, stderr=PIPE
                )
                try:
                    stdout3, stderr3 = await asyncio.wait_for(
                        proc3.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc3.kill()
                    await proc3.communicate()
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
                proc2 = await asyncio.create_subprocess_exec(
                    *recode_cmd, stdout=PIPE, stderr=PIPE
                )
                try:
                    stdout2, stderr2 = await asyncio.wait_for(
                        proc2.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc2.kill()
                    await proc2.communicate()
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
    # if still audio-only, try ffmpeg to create a minimal video wrapper (black video + audio)
    if ext in audio_exts:
        try:
            base = os.path.splitext(os.path.basename(latest))[0]
            mp4_path = os.path.join(dest_dir, f"{base}_wrapped.mp4")
            ffmpeg_cmd = [
                ffmpeg_bin or "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=640x360",
                "-i",
                latest,
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                "-movflags",
                "+faststart",
                mp4_path,
            ]
            proc3 = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd, stdout=PIPE, stderr=PIPE
            )
            try:
                stdout3, stderr3 = await asyncio.wait_for(
                    proc3.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc3.kill()
                await proc3.communicate()
                logger.warning("ffmpeg conversion timed out")
            else:
                try:
                    s3 = stdout3.decode(errors="ignore") if stdout3 else ""
                except Exception:
                    s3 = "<decoding error>"
                try:
                    e3 = stderr3.decode(errors="ignore") if stderr3 else ""
                except Exception:
                    e3 = "<decoding error>"
                logger.debug("ffmpeg container stdout (truncated): %s", s3[:2000])
                logger.debug("ffmpeg container stderr (truncated): %s", e3[:2000])
                if proc3.returncode == 0 and os.path.exists(mp4_path):
                    latest = mp4_path
                    ext = "mp4"
                    try:
                        size = os.path.getsize(latest)
                        logger.info(
                            "After ffmpeg container, file: %s (%d bytes)", latest, size
                        )
                    except Exception:
                        logger.debug("Could not stat ffmpeg container file: %s", latest)
                else:
                    logger.debug("ffmpeg failed: %s", e3)
        except Exception:
            logger.exception("ffmpeg conversion failed")
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
                            "-i",
                            input_path,
                            "-vf",
                            f"scale=-2:{h}",
                            "-c:v",
                            "libx264",
                            "-preset",
                            "fast",
                            "-crf",
                            str(crf),
                            "-c:a",
                            "aac",
                            "-b:a",
                            "128k",
                            "-movflags",
                            "+faststart",
                            out_path,
                        ]
                        logger.debug("Running ffmpeg recompress: %s", " ".join(cmd))
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                *cmd, stdout=PIPE, stderr=PIPE
                            )
                            try:
                                stdout, stderr = await asyncio.wait_for(
                                    proc.communicate(), timeout=300
                                )
                            except asyncio.TimeoutError:
                                proc.kill()
                                await proc.communicate()
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
                    "--merge-output-format",
                    "mp4",
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
                proc_rd = await asyncio.create_subprocess_exec(
                    *rd_cmd, stdout=PIPE, stderr=PIPE
                )
                try:
                    out_r, err_r = await asyncio.wait_for(
                        proc_rd.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc_rd.kill()
                    await proc_rd.communicate()
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
        raise RuntimeError(f"downloaded file too large ({size} bytes)")
    meta = {
        "compressed": compressed_flag,
        "original_size": orig_size,
        "final_size": size,
        "has_video": has_video,
        "has_audio": has_audio,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "format": fmt,
    }
    return latest, meta
