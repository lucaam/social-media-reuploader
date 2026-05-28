import asyncio
import json
import shutil
import sys
from asyncio.subprocess import PIPE
from typing import Optional, Tuple

from . import config


async def extract_direct_url_and_meta(
    url: str,
    timeout: int = 15,
    ytdlp_user_agent: Optional[str] = None,
    ytdlp_cookies: Optional[str] = None,
    ytdlp_headers: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[dict]]:
    """
    Minimal helper: use `yt-dlp -g` to get a direct URL and `-j` to fetch
    lightweight metadata. Returns (direct_url, meta) or (None, None).
    """
    yt_dlp_bin = shutil.which("yt-dlp") or None
    if yt_dlp_bin:
        base_cmd = [yt_dlp_bin]
    else:
        base_cmd = [sys.executable, "-m", "yt_dlp"]

    cmd_common = list(base_cmd) + ["--no-playlist"]
    ua = ytdlp_user_agent or getattr(config, "YTDLP_USER_AGENT", None)
    if ua:
        cmd_common += ["--user-agent", ua]
    cookies_path = ytdlp_cookies or getattr(config, "YTDLP_COOKIES", None)
    if cookies_path:
        cmd_common += ["--cookies", cookies_path]
    headers = ytdlp_headers or None
    if not headers and getattr(config, "YTDLP_HEADERS", None):
        try:
            headers = {}
            for part in getattr(config, "YTDLP_HEADERS").split("|"):
                if ":" in part:
                    k, v = part.split(":", 1)
                    headers[k.strip()] = v.strip()
        except Exception:
            headers = None
    if headers:
        for k, v in headers.items():
            cmd_common += ["--add-header", f"{k}: {v}"]

    # 1) get direct URL
    cmd_g = list(cmd_common) + ["-g", "-f", "best", url]
    try:
        p = await asyncio.create_subprocess_exec(*cmd_g, stdout=PIPE, stderr=PIPE)
        try:
            outb, errb = await asyncio.wait_for(p.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                p.kill()
            except Exception:
                pass
            try:
                await p.communicate()
            except Exception:
                pass
            return None, None
        if p.returncode != 0:
            return None, None
        out = outb.decode(errors="ignore").strip() if outb else ""
        if not out:
            return None, None
        direct = None
        for line in out.splitlines():
            s = line.strip()
            if s and (s.startswith("http://") or s.startswith("https://")):
                direct = s
                break
        if not direct:
            return None, None
    except Exception:
        return None, None

    # 2) optional metadata via -j
    meta = None
    try:
        cmd_j = list(cmd_common) + [
            "-j",
            "--no-warnings",
            "--skip-download",
            "-f",
            "best",
            url,
        ]
        p2 = await asyncio.create_subprocess_exec(*cmd_j, stdout=PIPE, stderr=PIPE)
        try:
            outb2, errb2 = await asyncio.wait_for(p2.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                p2.kill()
            except Exception:
                pass
            try:
                await p2.communicate()
            except Exception:
                pass
            return direct, None
        if p2.returncode == 0 and outb2:
            try:
                info = json.loads(outb2.decode(errors="ignore"))
                meta = {}
                if info.get("duration") is not None:
                    try:
                        meta["duration"] = float(info.get("duration"))
                    except Exception:
                        pass
                if info.get("ext"):
                    meta["format"] = info.get("ext")
                if info.get("width"):
                    try:
                        meta["video_width"] = int(info.get("width"))
                    except Exception:
                        pass
                if info.get("height"):
                    try:
                        meta["video_height"] = int(info.get("height"))
                    except Exception:
                        pass
                fmts = info.get("formats") or []
                chosen = None
                for f in reversed(fmts):
                    try:
                        # Compare exact URLs to avoid false substring matches
                        if f.get("url") and f.get("url") == direct:
                            chosen = f
                            break
                    except Exception:
                        pass
                if chosen is None and fmts:
                    chosen = fmts[-1]
                if chosen:
                    if chosen.get("vcodec"):
                        meta["video_codec"] = chosen.get("vcodec")
                    if chosen.get("acodec"):
                        meta["audio_codec"] = chosen.get("acodec")
                    if chosen.get("filesize"):
                        try:
                            meta["final_size"] = int(chosen.get("filesize"))
                        except Exception:
                            pass
            except Exception:
                meta = None
    except Exception:
        meta = None

    return direct, meta
