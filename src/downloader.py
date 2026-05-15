import asyncio
import glob
import json
import logging
import os
import shutil
import sys
from asyncio.subprocess import PIPE
from typing import Optional, Tuple

from . import config

logger = logging.getLogger(__name__)


async def download(
    url: str,
    dest_dir: str,
    timeout: int = 300,
    max_bytes: Optional[int] = None,
    notify=None,
    # yt-dlp impersonation / auth options (optional)
    ytdlp_user_agent: Optional[str] = None,
    ytdlp_cookies: Optional[str] = None,
    ytdlp_cookies_from_browser: Optional[str] = None,
    ytdlp_headers: Optional[dict] = None,
) -> Tuple[str, dict]:
    """
    Download a media resource using yt-dlp to `dest_dir` and return the downloaded file path
    and metadata. Preserves original codecs when possible; falls back to remuxing or
    transcoding (orientation-aware) when necessary.
    """
    os.makedirs(dest_dir, exist_ok=True)
    out_template = os.path.join(dest_dir, "%(id)s.%(ext)s")

    procs: set = set()

    async def _cleanup_procs():
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

    async def _await_proc(p, to: Optional[int] = None):
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

    # resolve yt-dlp binary
    yt_dlp_bin = shutil.which("yt-dlp") or None
    if yt_dlp_bin:
        base_cmd = [yt_dlp_bin]
    else:
        base_cmd = [sys.executable, "-m", "yt_dlp"]

    # prefer H.264 where possible to avoid AV1/VP9 in mp4
    proc = None
    stdout = stderr = b""
    # Minimal yt-dlp invocation: do not force format selection, let yt-dlp
    # pick the default/best format for the site. This avoids aggressive
    # attempts to prefer H.264 or otherwise modify which streams are chosen.
    cmd = base_cmd + ["--no-playlist", "-o", out_template]
    if max_bytes:
        cmd += ["--max-filesize", str(max_bytes)]
        ua = ytdlp_user_agent or getattr(config, "YTDLP_USER_AGENT", None)
        if ua:
            cmd += ["--user-agent", ua]
        cookies_path = ytdlp_cookies or getattr(config, "YTDLP_COOKIES", None)
        if cookies_path:
            cmd += ["--cookies", cookies_path]
        cookies_from_browser = ytdlp_cookies_from_browser or getattr(
            config, "YTDLP_COOKIES_FROM_BROWSER", None
        )
        if cookies_from_browser:
            cmd += ["--cookies-from-browser", cookies_from_browser]
        headers = ytdlp_headers or None
        if not headers and getattr(config, "YTDLP_HEADERS", None):
            try:
                headers = {}
                for part in getattr(config, "YTDLP_HEADERS").split("|"):
                    if ":" in part:
                        k, v = part.split(":", 1)
                        headers[k.strip()] = v.strip()
            except Exception:
                headers = {}
        if headers:
            for k, v in headers.items():
                cmd += ["--add-header", f"{k}: {v}"]
    cmd += [url]
    logger.debug("Running yt-dlp: %s", " ".join(cmd))
    proc = await _spawn(*cmd)
    stdout, stderr = await _await_proc(proc, to=timeout)
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

    if getattr(proc, "returncode", None) == 0:
        files = glob.glob(os.path.join(dest_dir, "*"))
        # prefer actual media files — exclude json and temporary parts that
        # may be present in the same directory (ffprobe JSON, partial files)
        media_exts = {
            ".mp4",
            ".m4a",
            ".webm",
            ".mkv",
            ".mp3",
            ".aac",
            ".ogg",
            ".opus",
            ".mov",
            ".flv",
            ".ts",
        }
        media_files = [f for f in files if os.path.splitext(f)[1].lower() in media_exts]
        candidates = (
            media_files
            if media_files
            else [
                f for f in files if not f.endswith(".json") and not f.endswith(".part")
            ]
        )
        if candidates:
            latest = max(candidates, key=os.path.getmtime)
            logger.debug(
                "picked latest file %s (from candidates: %s)", latest, candidates
            )

    if proc is None or (getattr(proc, "returncode", None) != 0):
        err = stderr.decode(errors="ignore") if stderr else ""
        await _cleanup_procs()
        raise RuntimeError(f"yt-dlp failed: {err.strip()[:200]}")

    if "latest" not in locals():
        files = glob.glob(os.path.join(dest_dir, "*"))
        if not files:
            await _cleanup_procs()
            raise RuntimeError("no file downloaded")
        media_exts = {
            ".mp4",
            ".m4a",
            ".webm",
            ".mkv",
            ".mp3",
            ".aac",
            ".ogg",
            ".opus",
            ".mov",
            ".flv",
            ".ts",
        }
        media_files = [f for f in files if os.path.splitext(f)[1].lower() in media_exts]
        candidates = (
            media_files
            if media_files
            else [
                f for f in files if not f.endswith(".json") and not f.endswith(".part")
            ]
        )
        latest = max(candidates, key=os.path.getmtime)

    try:
        size = os.path.getsize(latest)
        orig_size = size
        logger.info("Downloaded file: %s (%d bytes)", latest, size)
    except Exception:
        orig_size = None
        logger.debug("Could not stat downloaded file: %s", latest)
    # USER REQUEST: disable all post-download processing. Return the
    # original file as-is without running yt-dlp recode, remux, or ffmpeg
    # transcode. However, run a lightweight ffprobe (if available) to
    # capture metadata for diagnostics without modifying the video bytes.
    try:
        await _cleanup_procs()
    except Exception:
        pass
    ext = os.path.splitext(latest)[1].lower().lstrip(".")
    audio_exts = {"m4a", "mp3", "aac", "wav", "ogg", "opus"}
    has_video = False if ext in audio_exts else True
    has_audio = True if ext in audio_exts or has_video else False

    # default meta
    meta = {
        "compressed": False,
        "original_size": orig_size,
        "final_size": orig_size,
        "has_video": has_video,
        "has_audio": has_audio,
        "duration": None,
        "video_codec": None,
        "audio_codec": None,
        "format": ext,
        "video_width": None,
        "video_height": None,
        "video_profile": None,
        "video_rotation": None,
        "video_sample_aspect_ratio": None,
        "video_display_aspect_ratio": None,
        "ytdlp_only": True,
    }

    ffprobe_bin = shutil.which("ffprobe")
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
                    # write ffprobe JSON for the downloaded file for debugging
                    try:
                        base_name = os.path.splitext(os.path.basename(latest))[0]
                        probe_out = os.path.join(
                            dest_dir, f"{base_name}_orig.ffprobe.json"
                        )
                        with open(probe_out, "w") as _fh:
                            json.dump(info, _fh)
                        logger.info(
                            "Wrote ffprobe JSON for original file to %s", probe_out
                        )
                        # Also write a persistent copy into workspace `data/ffprobe/`
                        try:
                            persistent_dir = os.path.join(
                                os.getcwd(), "data", "ffprobe"
                            )
                            os.makedirs(persistent_dir, exist_ok=True)
                            persistent_path = os.path.join(
                                persistent_dir, f"{base_name}_orig.ffprobe.json"
                            )
                            with open(persistent_path, "w") as _pf:
                                json.dump(info, _pf)
                            logger.info(
                                "Wrote persistent ffprobe JSON to %s", persistent_path
                            )
                        except Exception:
                            logger.debug("Could not write persistent ffprobe JSON")
                    except Exception:
                        logger.debug("Could not write original ffprobe JSON")

                    # populate meta from ffprobe where possible
                    streams = info.get("streams", [])
                    for s in streams:
                        if s.get("codec_type") == "video":
                            meta["has_video"] = True
                            meta["video_codec"] = s.get("codec_name")
                            meta["video_profile"] = s.get("profile")
                            meta["video_sample_aspect_ratio"] = (
                                s.get("sample_aspect_ratio") or None
                            )
                            meta["video_display_aspect_ratio"] = (
                                s.get("display_aspect_ratio") or None
                            )
                            try:
                                meta["video_width"] = int(s.get("width") or 0) or None
                            except Exception:
                                meta["video_width"] = None
                            try:
                                meta["video_height"] = int(s.get("height") or 0) or None
                            except Exception:
                                meta["video_height"] = None
                            try:
                                tags = s.get("tags") or {}
                                if "rotate" in tags:
                                    meta["video_rotation"] = int(tags.get("rotate"))
                                else:
                                    sdl = s.get("side_data_list") or []
                                    for sd in sdl:
                                        if isinstance(sd, dict) and "rotation" in sd:
                                            meta["video_rotation"] = int(
                                                sd.get("rotation")
                                            )
                                            break
                            except Exception:
                                meta["video_rotation"] = None
                        elif s.get("codec_type") == "audio":
                            meta["has_audio"] = True
                            meta["audio_codec"] = s.get("codec_name")
                    # set format string if available
                    fmt_info = info.get("format", {}) or {}
                    ffmt = fmt_info.get("format_name") or ""
                    if ffmt:
                        meta["format"] = ffmt
                    # duration in seconds (float)
                    try:
                        fdur = fmt_info.get("duration")
                        if fdur is not None:
                            meta["duration"] = float(fdur)
                    except Exception:
                        pass
                except Exception:
                    logger.debug("Could not parse ffprobe output for downloaded file")
        except Exception:
            logger.debug("ffprobe execution failed for downloaded file")

    logger.debug("download(): returning latest=%s final_size=%s", latest, orig_size)
    return latest, meta

    audio_exts = {"m4a", "mp3", "aac", "wav", "ogg", "opus"}
    ext = os.path.splitext(latest)[1].lower().lstrip(".")

    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")

    fmt = ""
    video_codec = None
    audio_codec = None
    has_video = False
    has_audio = False
    video_width = None
    video_height = None
    video_bit_rate = None
    video_profile = None
    video_rotation = None
    video_sample_aspect_ratio = None
    video_display_aspect_ratio = None

    # probe via ffprobe if available
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
                    try:
                        fmt_tags = info.get("format", {}).get("tags") or {}
                        if "rotate" in fmt_tags:
                            video_rotation = int(fmt_tags.get("rotate"))
                    except Exception:
                        pass
                    for s in streams:
                        if s.get("codec_type") == "video":
                            has_video = True
                            video_codec = s.get("codec_name")
                            video_profile = s.get("profile")
                            video_sample_aspect_ratio = (
                                s.get("sample_aspect_ratio") or None
                            )
                            video_display_aspect_ratio = (
                                s.get("display_aspect_ratio") or None
                            )
                            try:
                                video_width = int(s.get("width") or 0) or None
                            except Exception:
                                video_width = None
                            try:
                                video_height = int(s.get("height") or 0) or None
                            except Exception:
                                video_height = None
                            video_bit_rate = s.get("bit_rate") or None
                            try:
                                tags = s.get("tags") or {}
                                if "rotate" in tags:
                                    video_rotation = int(tags.get("rotate"))
                                else:
                                    sdl = s.get("side_data_list") or []
                                    for sd in sdl:
                                        if isinstance(sd, dict) and "rotation" in sd:
                                            video_rotation = int(sd.get("rotation"))
                                            break
                            except Exception:
                                video_rotation = None
                        elif s.get("codec_type") == "audio":
                            has_audio = True
                            audio_codec = s.get("codec_name")
                            # Write ffprobe JSON for the downloaded file for debugging
                            try:
                                base_name = os.path.splitext(os.path.basename(latest))[
                                    0
                                ]
                                probe_out = os.path.join(
                                    dest_dir, f"{base_name}_orig.ffprobe.json"
                                )
                                with open(probe_out, "w") as _fh:
                                    json.dump(info, _fh)
                                logger.info(
                                    "Wrote ffprobe JSON for original file to %s",
                                    probe_out,
                                )
                            except Exception:
                                logger.debug("Could not write original ffprobe JSON")
                except Exception:
                    logger.debug("Could not parse ffprobe output")
        except Exception:
            logger.debug("ffprobe execution failed")

    logger.info(
        "ffprobe: has_video=%s has_audio=%s video_codec=%s audio_codec=%s format=%s",
        has_video,
        has_audio,
        video_codec,
        audio_codec,
        fmt,
    )

    # If configured to be "stupid", return the raw yt-dlp output immediately
    # (no recode/remux/transcode). We still return ffprobe-derived metadata so
    # the caller can decide how to upload the file.
    try:
        if getattr(config, "SIMPLE_YTDLP_ONLY", False):
            logger.info(
                "SIMPLE_YTDLP_ONLY enabled — returning yt-dlp output without transcode/remux"
            )
            await _cleanup_procs()
            meta = {
                "compressed": False,
                "original_size": orig_size,
                "final_size": orig_size,
                "has_video": has_video,
                "has_audio": has_audio,
                "video_codec": video_codec,
                "audio_codec": audio_codec,
                "format": fmt,
                "video_width": video_width,
                "video_height": video_height,
                "video_profile": video_profile,
                "video_rotation": video_rotation,
                "video_sample_aspect_ratio": video_sample_aspect_ratio,
                "video_display_aspect_ratio": video_display_aspect_ratio,
                "ytdlp_only": True,
            }
            return latest, meta
    except Exception:
        pass

    # compatibility checks
    # Preserve the original file whenever possible. Only consider the file
    # incompatible (and therefore a candidate for recode/remux/transcode)
    # if it violates Telegram constraints (size) or uses unsupported codecs.
    if has_video and has_audio:
        preferred_video = ("h264", "mpeg4")
        preferred_audio = ("aac", "mp3", "mp4a")

        # Size check against Telegram limits
        tele_max = getattr(config, "TELEGRAM_MAX_FILE_SIZE", None)
        try:
            size_ok = True
            if tele_max and orig_size is not None:
                size_ok = orig_size <= tele_max
        except Exception:
            size_ok = True

        # Codec support check (do not force baseline/profile changes)
        codec_ok = True
        if video_codec and video_codec not in preferred_video:
            codec_ok = False
        if audio_codec and audio_codec not in preferred_audio:
            codec_ok = False

        # If the file has rotation metadata, some Telegram clients ignore it.
        # Physically rotate the frames in this case to preserve the visual
        # orientation when delivering to Telegram.
        rotation_present = False
        try:
            rotation_present = bool(
                video_rotation is not None and int(video_rotation) != 0
            )
        except Exception:
            rotation_present = False

        sar_present = False
        try:
            if video_sample_aspect_ratio and str(video_sample_aspect_ratio) != "1:1":
                sar_present = True
            elif (
                video_display_aspect_ratio and str(video_display_aspect_ratio) != "1:1"
            ):
                sar_present = True
        except Exception:
            sar_present = False

        incompatible = (not (size_ok and codec_ok)) or rotation_present or sar_present
        # Do not force normalization based on host by default. Preserve the
        # original file unless it violates Telegram constraints (size/codecs)
        # or has rotation/SAR metadata that makes it incompatible.
        force_normalize = False
        logger.info(
            "compatibility: size_ok=%s codec_ok=%s incompatible=%s video_codec=%s audio_codec=%s size=%s tele_max=%s rotation=%s sar=%s dar=%s",
            size_ok,
            codec_ok,
            incompatible,
            video_codec,
            audio_codec,
            orig_size,
            tele_max,
            video_rotation,
            video_sample_aspect_ratio,
            video_display_aspect_ratio,
        )

        # Try yt-dlp recode as a lighter-weight fallback first (skip if we must force ffmpeg normalization)
        if incompatible and yt_dlp_bin and not force_normalize:
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
                    stdout_re, stderr_re = await _await_proc(proc_recode, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("yt-dlp recode timed out")
                else:
                    if getattr(proc_recode, "returncode", None) == 0:
                        files = glob.glob(os.path.join(dest_dir, "*"))
                        if files:
                            latest_candidate = max(files, key=os.path.getmtime)
                            latest = latest_candidate
                            ext = os.path.splitext(latest)[1].lower().lstrip(".")
                        # re-probe recoded file
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
                                        # Write ffprobe JSON for the recoded file
                                        try:
                                            base_name2 = os.path.splitext(
                                                os.path.basename(latest)
                                            )[0]
                                            probe_out2 = os.path.join(
                                                dest_dir,
                                                f"{base_name2}_recode.ffprobe.json",
                                            )
                                            with open(probe_out2, "w") as _fh2:
                                                json.dump(info2, _fh2)
                                            logger.info(
                                                "Wrote ffprobe JSON for recoded file to %s",
                                                probe_out2,
                                            )
                                        except Exception:
                                            logger.debug(
                                                "Could not write recoded ffprobe JSON"
                                            )
                                        fmt = (
                                            info2.get("format", {}).get(
                                                "format_name", ""
                                            )
                                            or ""
                                        )
                                        # reset codec variables
                                        video_codec = None
                                        audio_codec = None
                                        has_video = False
                                        has_audio = False
                                        video_width = None
                                        video_height = None
                                        video_bit_rate = None
                                        video_profile = None
                                        video_rotation = None
                                        video_sample_aspect_ratio = None
                                        video_display_aspect_ratio = None
                                        for s in streams2:
                                            if s.get("codec_type") == "video":
                                                has_video = True
                                                video_codec = s.get("codec_name")
                                                video_profile = s.get("profile")
                                                video_sample_aspect_ratio = (
                                                    s.get("sample_aspect_ratio") or None
                                                )
                                                video_display_aspect_ratio = (
                                                    s.get("display_aspect_ratio")
                                                    or None
                                                )
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
                                                try:
                                                    tags = s.get("tags") or {}
                                                    if "rotate" in tags:
                                                        video_rotation = int(
                                                            tags.get("rotate")
                                                        )
                                                    else:
                                                        sdl = (
                                                            s.get("side_data_list")
                                                            or []
                                                        )
                                                        for sd in sdl:
                                                            if (
                                                                isinstance(sd, dict)
                                                                and "rotation" in sd
                                                            ):
                                                                video_rotation = int(
                                                                    sd.get("rotation")
                                                                )
                                                                break
                                                except Exception:
                                                    video_rotation = None
                                            elif s.get("codec_type") == "audio":
                                                has_audio = True
                                                audio_codec = s.get("codec_name")
                                    except Exception:
                                        logger.debug(
                                            "Could not parse ffprobe output for recoded file"
                                        )
                            except Exception:
                                logger.debug(
                                    "ffprobe execution failed for recoded file"
                                )
                            # recompute compatibility
                        if (video_codec and video_codec in preferred_video) and (
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
                                logger.debug("Could not stat recoded file: %s", latest)
                            if force_normalize:
                                logger.info(
                                    "Ignoring yt-dlp recode result due to force_normalize; will perform manual ffmpeg transcode"
                                )
                            else:
                                incompatible = False
                            # If recode produced a compatible file we accept it.
                            # Do not force an additional manual transcode just because
                            # the file size did not change; the user prefers the
                            # original visual content when possible.
            except Exception:
                logger.exception("yt-dlp recode failed")

        # If still incompatible, attempt manual ffmpeg transcode (orientation-aware)
        if incompatible and ffmpeg_bin:
            try:
                base = os.path.splitext(os.path.basename(latest))[0]
                trans_path = os.path.join(dest_dir, f"{base}_transcoded.mp4")

                # consider rotation metadata when deciding portrait vs landscape
                try:
                    rotation = video_rotation
                    w = int(video_width) if video_width else None
                    h = int(video_height) if video_height else None
                except Exception:
                    rotation = None
                    w = video_width
                    h = video_height

                try:
                    if (
                        rotation in (90, -270, 270, -90)
                        and w is not None
                        and h is not None
                    ):
                        w, h = h, w
                    is_portrait = False
                    if w and h:
                        is_portrait = int(h) >= int(w)
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

                try:
                    if video_bit_rate:
                        kb = int(int(video_bit_rate) / 1000)
                        vb = f"{kb}k" if kb > 0 else "1780k"
                    else:
                        vb = "1780k"
                except Exception:
                    vb = "1780k"

                # Simple scaling approach: compute target dimensions in Python
                # and use ffmpeg scale to resize while preserving aspect ratio.
                # This avoids complex filter expressions and keeps the original
                # visual content unchanged except for resizing/codec normalization.
                try:
                    w = int(video_width) if video_width else None
                    h = int(video_height) if video_height else None
                except Exception:
                    w = video_width
                    h = video_height

                # swap if rotation indicates portrait/landscape swap
                if rotation in (90, -270, 270, -90) and w is not None and h is not None:
                    w, h = h, w

                # bounding box for Telegram-friendly size
                max_w = 720
                max_h = 1280

                if w and h:
                    scale_ratio = min(
                        1.0, float(max_w) / float(w), float(max_h) / float(h)
                    )
                    new_w = max(2, int((w * scale_ratio) // 2 * 2))
                    new_h = max(2, int((h * scale_ratio) // 2 * 2))
                    # if no scaling needed, we may still need to pad to target
                    if new_w == w and new_h == h:
                        if new_w == pad_w and new_h == pad_h:
                            vf = "setsar=1"
                        else:
                            pad_x = max(0, (pad_w - new_w) // 2)
                            pad_y = max(0, (pad_h - new_h) // 2)
                            vf = f"setsar=1,pad={pad_w}:{pad_h}:{pad_x}:{pad_y}:black"
                    else:
                        pad_x = max(0, (pad_w - new_w) // 2)
                        pad_y = max(0, (pad_h - new_h) // 2)
                        vf = f"scale={new_w}:{new_h},setsar=1,pad={pad_w}:{pad_h}:{pad_x}:{pad_y}:black"
                else:
                    # fallback: scale to fit height for portrait, width for landscape
                    if is_portrait:
                        vf = f"scale=-2:{max_h},setsar=1"
                    else:
                        vf = f"scale={max_w}:-2,setsar=1"
                scale_filter = vf

                # If rotation metadata exists, add a rotation filter so frames
                # are physically rotated (some Telegram clients ignore metadata).
                rotation_filter = None
                try:
                    r = int(rotation) if rotation is not None else 0
                except Exception:
                    r = 0
                if r in (90, -270):
                    rotation_filter = "transpose=1"
                elif r in (270, -90):
                    rotation_filter = "transpose=2"
                elif r in (180, -180):
                    rotation_filter = "transpose=1,transpose=1"

                if rotation_filter:
                    combined_vf = f"{rotation_filter},{scale_filter}"
                else:
                    combined_vf = scale_filter

                logger.info(
                    "Transcode params: src_w=%s src_h=%s video_rotation=%s is_portrait=%s vf=%s vb=%s",
                    video_width,
                    video_height,
                    rotation,
                    is_portrait,
                    combined_vf,
                    vb,
                )

                ffmpeg_cmd = [
                    ffmpeg_bin,
                    "-y",
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
                    str(max(4000, int(int(vb.rstrip("k")) * 2000))),
                    "-vf",
                    combined_vf,
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
                    "-metadata:s:v:0",
                    "rotate=0",
                    "-max_muxing_queue_size",
                    "9999",
                    trans_path,
                ]
                logger.info("Transcoding %s to mp4/h264 for compatibility", latest)
                proc4 = await _spawn(*ffmpeg_cmd)
                try:
                    out4, err4 = await _await_proc(proc4, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("ffmpeg transcode timed out")
                else:
                    if getattr(proc4, "returncode", None) == 0 and os.path.exists(
                        trans_path
                    ):
                        latest = trans_path
                        ext = "mp4"
                        try:
                            size = os.path.getsize(latest)
                            logger.info("Transcoded file: %s (%d bytes)", latest, size)
                        except Exception:
                            logger.debug("Could not stat transcoded file: %s", latest)
                        # re-probe transcoded file
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
                                        # Write ffprobe JSON for the transcoded file
                                        try:
                                            base_name3 = os.path.splitext(
                                                os.path.basename(latest)
                                            )[0]
                                            probe_out3 = os.path.join(
                                                dest_dir,
                                                f"{base_name3}_transcoded.ffprobe.json",
                                            )
                                            with open(probe_out3, "w") as _fh3:
                                                json.dump(info3, _fh3)
                                            logger.info(
                                                "Wrote ffprobe JSON for transcoded file to %s",
                                                probe_out3,
                                            )
                                        except Exception:
                                            logger.debug(
                                                "Could not write transcoded ffprobe JSON"
                                            )
                                        fmt = (
                                            info3.get("format", {}).get(
                                                "format_name", ""
                                            )
                                            or ""
                                        )
                                        # reset stream vars
                                        video_codec = None
                                        audio_codec = None
                                        has_video = False
                                        has_audio = False
                                        video_width = None
                                        video_height = None
                                        video_bit_rate = None
                                        video_profile = None
                                        video_rotation = None
                                        video_sample_aspect_ratio = None
                                        video_display_aspect_ratio = None
                                        for s in streams3:
                                            if s.get("codec_type") == "video":
                                                has_video = True
                                                video_codec = s.get("codec_name")
                                                video_profile = s.get("profile")
                                                video_sample_aspect_ratio = (
                                                    s.get("sample_aspect_ratio") or None
                                                )
                                                video_display_aspect_ratio = (
                                                    s.get("display_aspect_ratio")
                                                    or None
                                                )
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
                                                try:
                                                    tags = s.get("tags") or {}
                                                    if "rotate" in tags:
                                                        video_rotation = int(
                                                            tags.get("rotate")
                                                        )
                                                    else:
                                                        sdl = (
                                                            s.get("side_data_list")
                                                            or []
                                                        )
                                                        for sd in sdl:
                                                            if (
                                                                isinstance(sd, dict)
                                                                and "rotation" in sd
                                                            ):
                                                                video_rotation = int(
                                                                    sd.get("rotation")
                                                                )
                                                                break
                                                except Exception:
                                                    video_rotation = None
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
                    else:
                        try:
                            err_s = err4.decode(errors="ignore") if err4 else ""
                        except Exception:
                            err_s = "<decoding error>"
                        logger.error(
                            "ffmpeg transcode failed (code=%s). stderr: %s",
                            getattr(proc4, "returncode", None),
                            err_s[:4000],
                        )
            except Exception:
                logger.exception("Transcoding failed")

        # If codecs are compatible but container is not mp4, remux to mp4
        elif fmt and "mp4" not in fmt:
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
                    "-metadata:s:v:0",
                    "rotate=0",
                    "-max_muxing_queue_size",
                    "9999",
                    remux_path,
                ]
                logger.info("Remuxing %s to mp4 container", latest)
                proc5 = await _spawn(*ffmpeg_cmd)
                try:
                    out5, err5 = await _await_proc(proc5, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("ffmpeg remux timed out")
                else:
                    if getattr(proc5, "returncode", None) == 0 and os.path.exists(
                        remux_path
                    ):
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
                    "-max_muxing_queue_size",
                    "9999",
                    mp4_path,
                ]
                proc3 = await _spawn(*ffmpeg_cmd)
                try:
                    stdout3, stderr3 = await _await_proc(proc3, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("ffmpeg conversion timed out")
                else:
                    if getattr(proc3, "returncode", None) == 0 and os.path.exists(
                        mp4_path
                    ):
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
                    if getattr(proc2, "returncode", None) == 0:
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

    # final size check: if too large, attempt recompression
    size = os.path.getsize(latest)
    target = getattr(config, "TELEGRAM_MAX_FILE_SIZE", None)
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

            async def _try_compress(
                input_path: str, dest_dir: str, target_bytes: int
            ) -> Optional[str]:
                base = os.path.splitext(os.path.basename(input_path))[0]
                out_path = os.path.join(dest_dir, f"{base}.recompressed.mp4")
                scale_heights = [360, 240, 180]
                crf_values = [28, 30, 32, 34, 36]
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
                        "setsar=1",
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
                        "-max_muxing_queue_size",
                        "9999",
                        out_path,
                    ]
                    proc = await _spawn(*cmd)
                    try:
                        stdout, stderr = await _await_proc(proc, to=300)
                    except asyncio.TimeoutError:
                        logger.warning("ffmpeg recompress timed out for crf=%s", crf)
                        continue
                    if getattr(proc, "returncode", None) != 0:
                        try:
                            err_str = stderr.decode(errors="ignore") if stderr else ""
                        except Exception:
                            err_str = "<decoding error>"
                        logger.debug(
                            "ffmpeg returned non-zero (%s). stderr=%s",
                            getattr(proc, "returncode", None),
                            err_str,
                        )
                        continue
                    if os.path.exists(out_path):
                        new_size = os.path.getsize(out_path)
                        logger.info(
                            "Recompressed file size=%d (target=%d) with crf=%s (no-resize)",
                            new_size,
                            target_bytes,
                            crf,
                        )
                        if new_size <= target_bytes:
                            return out_path
                for h in scale_heights:
                    for crf in crf_values:
                        if os.path.exists(out_path):
                            try:
                                os.remove(out_path)
                            except Exception:
                                pass
                        vf = f"scale=-2:{h}:force_original_aspect_ratio=decrease,setsar=1"
                        cmd = [
                            ffmpeg_bin,
                            "-y",
                            "-i",
                            input_path,
                            "-vf",
                            vf,
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
                            "-max_muxing_queue_size",
                            "9999",
                            out_path,
                        ]
                        proc = await _spawn(*cmd)
                        try:
                            stdout, stderr = await _await_proc(proc, to=300)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "ffmpeg recompress timed out for crf=%s h=%s", crf, h
                            )
                            continue
                        if getattr(proc, "returncode", None) != 0:
                            try:
                                err_str = (
                                    stderr.decode(errors="ignore") if stderr else ""
                                )
                            except Exception:
                                err_str = "<decoding error>"
                            logger.debug(
                                "ffmpeg returned non-zero (%s). stderr=%s",
                                getattr(proc, "returncode", None),
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
            formats_to_try = [
                "bestvideo[height<=360]+bestaudio/best",
                "bestvideo[height<=240]+bestaudio/best",
                "bestvideo[height<=180]+bestaudio/best",
                "bestaudio/best",
            ]
            for fmt in formats_to_try:
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
                proc_rd = await _spawn(*rd_cmd)
                try:
                    out_r, err_r = await _await_proc(proc_rd, to=timeout)
                except asyncio.TimeoutError:
                    logger.warning("Redownload with format %s timed out", fmt)
                    continue
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

        if getattr(config, "TELEGRAM_MAX_FILE_SIZE", None) and size > getattr(
            config, "TELEGRAM_MAX_FILE_SIZE", 0
        ):
            await _cleanup_procs()
            raise RuntimeError(f"downloaded file too large ({size} bytes)")

    await _cleanup_procs()

    meta = {
        "compressed": compressed_flag,
        "original_size": orig_size,
        "final_size": size,
        "has_video": has_video,
        "has_audio": has_audio,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "format": fmt,
        "video_width": video_width,
        "video_height": video_height,
        "video_profile": video_profile,
        "video_rotation": video_rotation,
        "video_sample_aspect_ratio": video_sample_aspect_ratio,
        "video_display_aspect_ratio": video_display_aspect_ratio,
    }
    logger.debug("download(): returning latest=%s final_size=%s", latest, size)
    return latest, meta
