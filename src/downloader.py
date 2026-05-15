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


def _select_latest_media_file(dest_dir: str):
    files = glob.glob(os.path.join(dest_dir, "*"))
    if not files:
        return None
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
        else [f for f in files if not f.endswith(".json") and not f.endswith(".part")]
    )
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


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
    and metadata. Preserves original codecs when possible; runs a lightweight ffprobe
    to collect metadata for diagnostics without modifying the video bytes.
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

    if getattr(proc, "returncode", None) != 0:
        err = stderr.decode(errors="ignore") if stderr else ""
        await _cleanup_procs()
        raise RuntimeError(f"yt-dlp failed: {err.strip()[:200]}")

    latest = _select_latest_media_file(dest_dir)
    if not latest:
        await _cleanup_procs()
        raise RuntimeError("no file downloaded")

    try:
        size = os.path.getsize(latest)
        orig_size = size
        logger.info("Downloaded file: %s (%d bytes)", latest, size)
    except Exception:
        orig_size = None
        logger.debug("Could not stat downloaded file: %s", latest)

    # default meta
    meta = {
        "compressed": False,
        "original_size": orig_size,
        "final_size": orig_size,
        "has_video": None,
        "has_audio": None,
        "duration": None,
        "video_codec": None,
        "audio_codec": None,
        "format": None,
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

    try:
        await _cleanup_procs()
    except Exception:
        pass

    logger.debug("download(): returning latest=%s final_size=%s", latest, orig_size)
    return latest, meta
