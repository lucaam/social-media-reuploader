import asyncio
import logging
import os
import shutil
import tempfile
import time
from asyncio.subprocess import PIPE
from typing import Optional

from . import config, db, downloader, metrics, telegram_api

logger = logging.getLogger(__name__)


def _mb(n: int | None) -> str:
    try:
        if n is None:
            return "? MB"
        return f"{n / (1024 * 1024):.1f} MB"
    except Exception:
        return "? MB"


class WorkerPool:
    def __init__(self, token: str, workers: int = 2):
        self.token = token
        self._sem = asyncio.Semaphore(workers)
        self._tasks = set()
        self._closing = False

    def enqueue(
        self,
        chat_id,
        url,
        description: Optional[str] = None,
        original_message_id: Optional[int] = None,
        chat_type: Optional[str] = None,
    ) -> bool:
        """Schedule processing of a link. Returns True if scheduled."""
        if self._closing:
            return False
        try:

            async def _worker_wrapper():
                await self._sem.acquire()
                try:
                    await self._process(
                        chat_id, url, description, original_message_id, chat_type
                    )
                finally:
                    try:
                        self._sem.release()
                    except Exception:
                        pass

            t = asyncio.create_task(_worker_wrapper())
        except RuntimeError:
            # not in a running loop
            return False
        self._tasks.add(t)
        t.add_done_callback(lambda fut: self._tasks.discard(fut))
        return True

    async def shutdown(self, timeout: int | None = None):
        """Gracefully wait for currently running tasks to finish.

        New enqueues are rejected once shutdown starts. Waits up to `timeout`
        seconds (defaults to `config.WORKER_SHUTDOWN_TIMEOUT` or 30s) and then
        cancels remaining tasks.
        """
        self._closing = True
        if timeout is None:
            timeout = getattr(config, "WORKER_SHUTDOWN_TIMEOUT", 30)
        start = time.time()
        while self._tasks and (timeout is None or (time.time() - start) < timeout):
            await asyncio.sleep(0.1)
        # cancel remaining tasks
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True), timeout=5
            )
        except Exception:
            pass

    async def _generate_thumbnail(
        self, ffmpeg_bin: str, src_path: str, dst_dir: str
    ) -> Optional[str]:
        base = os.path.splitext(os.path.basename(src_path))[0]
        thumb = os.path.join(dst_dir, f"{base}_thumb.jpg")
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            src_path,
            "-ss",
            "00:00:01.000",
            "-vframes",
            "1",
            "-vf",
            "scale=w=320:h=-2:force_original_aspect_ratio=decrease,setsar=1",
            "-q:v",
            "2",
            thumb,
        ]
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
            try:
                await asyncio.wait_for(p.communicate(), timeout=15)
            except asyncio.TimeoutError:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    await p.communicate()
                except Exception:
                    pass
                return None
            if p.returncode == 0 and os.path.exists(thumb):
                return thumb
        except Exception:
            logger.debug("thumbnail generation failed for %s", src_path)
        return None

    async def _transcode_to_baseline(
        self, src_path: str, dst_path: str, tmpdir: str, meta: dict | None = None
    ) -> bool:
        """Ensure `dst_path` is an MP4 file compatible with Telegram.

        Strategy:
        - If possible, try a fast remux/copy into MP4 (`-c copy`).
        - If audio codec is incompatible (e.g. opus), copy video and transcode audio to AAC.
        - Otherwise fall back to full transcode to H.264 baseline profile.
        Logs ffmpeg stderr on failure to aid debugging.
        """
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            logger.debug("ffmpeg not available for transcode")
            return False

        # try to learn codecs from meta if available
        video_codec = None
        audio_codec = None
        try:
            if isinstance(meta, dict):
                video_codec = (meta.get("video_codec") or "").lower()
                audio_codec = (meta.get("audio_codec") or "").lower()
        except Exception:
            pass

        async def _run_cmd(cmd, timeout):
            logger.debug("running ffmpeg cmd: %s", " ".join(cmd))
            try:
                p = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
                try:
                    out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    try:
                        p.kill()
                    except Exception:
                        pass
                    try:
                        await p.communicate()
                    except Exception:
                        pass
                    logger.warning("ffmpeg timed out for %s", src_path)
                    return False, b"", b""
                return p.returncode == 0, out, err
            except Exception as e:
                logger.exception("ffmpeg invocation failed: %s", e)
                return False, b"", b""

        # 1) attempt fast remux (copy) when codecs look compatible
        if video_codec == "h264" and (audio_codec in (None, "aac", "mp3")):
            cmd_copy = [
                ffmpeg_bin,
                "-y",
                "-i",
                src_path,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                dst_path,
            ]
            ok, out, err = await _run_cmd(cmd_copy, timeout=60)
            if ok and os.path.exists(dst_path):
                return True
            logger.debug(
                "fast remux failed: %s",
                (err.decode(errors="ignore")[:1000] if err else ""),
            )

        # 2) if video is h264, try copying video and transcode audio to aac
        if video_codec == "h264":
            cmd_audio = [
                ffmpeg_bin,
                "-y",
                "-i",
                src_path,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                dst_path,
            ]
            ok, out, err = await _run_cmd(cmd_audio, timeout=120)
            if ok and os.path.exists(dst_path):
                return True
            logger.debug(
                "copy-video+transcode-audio failed: %s",
                (err.decode(errors="ignore")[:1000] if err else ""),
            )

        # 3) full transcode to baseline H.264
        # Respect config.AVOID_RESIZE: avoid changing resolution when requested.
        if getattr(config, "AVOID_RESIZE", False):
            vf_filter = "setsar=1"
        else:
            vf_filter = "scale=w=640:h=-2:force_original_aspect_ratio=decrease,setsar=1"

        cmd_full = [
            ffmpeg_bin,
            "-y",
            "-max_muxing_queue_size",
            "9999",
            "-i",
            src_path,
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
            "-vf",
            vf_filter,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            dst_path,
        ]
        ok, out, err = await _run_cmd(cmd_full, timeout=300)
        if ok and os.path.exists(dst_path):
            return True
        # log ffmpeg stderr for debugging
        try:
            stderr_txt = err.decode(errors="ignore") if err else ""
        except Exception:
            stderr_txt = "<decoding error>"
        logger.warning(
            "Worker ffmpeg transcode failed for %s: %s", src_path, stderr_txt[:2000]
        )
        return False

    async def _edit_status(
        self, token: str, chat_id: int, status_msg_id: int | None, text: str
    ):
        """Helper to edit an existing status message or send a new one.

        Returns the message_id of the status message (int) or None.
        """
        try:
            if status_msg_id:
                try:
                    res = await telegram_api.edit_message_text(
                        token, chat_id, status_msg_id, text
                    )
                    if isinstance(res, dict) and res.get("ok"):
                        return status_msg_id
                except Exception:
                    pass
            # send a new status message
            try:
                res = await telegram_api.send_message(token, chat_id, text)
                if isinstance(res, dict) and res.get("ok"):
                    return res.get("result", {}).get("message_id")
            except Exception:
                pass
        except Exception:
            pass
        return None

    async def _process(
        self,
        chat_id,
        url,
        description: Optional[str] = None,
        original_message_id: Optional[int] = None,
        chat_type: Optional[str] = None,
    ):
        request_id = None
        tmpdir = tempfile.mkdtemp()
        # track which reaction we added on the original message (None, 'eyes', 'banana')
        reaction_current: Optional[str] = None
        status_msg_id = None
        try:
            try:
                request_id = db.add_request(
                    chat_id,
                    url,
                    status="pending",
                    description=description,
                    original_message_id=original_message_id,
                )
            except Exception:
                request_id = None

            # Try to add an eyes reaction to the original message; fallback to status message
            if original_message_id:
                try:
                    r = await telegram_api.set_message_reaction(
                        self.token, chat_id, original_message_id, "👀"
                    )
                    if isinstance(r, dict):
                        ok = r.get("ok")
                    else:
                        ok = bool(r)
                    if ok:
                        reaction_current = "eyes"
                    else:
                        logger.debug(
                            "set_message_reaction returned non-ok; falling back to status message"
                        )
                        try:
                            status_res = await telegram_api.send_message(
                                self.token,
                                chat_id,
                                "In lavorazione 👀",
                                reply_to_message_id=original_message_id,
                            )
                            if isinstance(status_res, dict) and status_res.get("ok"):
                                status_msg_id = status_res.get("result", {}).get(
                                    "message_id"
                                )
                        except Exception:
                            logger.debug("Could not create status message")
                except Exception:
                    logger.debug(
                        "Could not add reaction; falling back to status message"
                    )
                    try:
                        status_res = await telegram_api.send_message(
                            self.token,
                            chat_id,
                            "In lavorazione 👀",
                            reply_to_message_id=original_message_id,
                        )
                        if isinstance(status_res, dict) and status_res.get("ok"):
                            status_msg_id = status_res.get("result", {}).get(
                                "message_id"
                            )
                    except Exception:
                        logger.debug("Could not create status message")

            # download
            try:
                file_path, meta = await downloader.download(
                    url,
                    tmpdir,
                    max_bytes=getattr(config, "TELEGRAM_MAX_FILE_SIZE", None),
                )
            except Exception as e:
                # Special-case auth/impersonation errors so we can notify the chat
                msg = str(e)
                low = msg.lower()
                auth_indicators = [
                    "you need to log in",
                    "you need to log in to access",
                    "login required",
                    "requiring login",
                    "requires login",
                    "no csrf token",
                    "this content isn't available",
                    "not available to everyone",
                    "authentication",
                    "cookies",
                    "impersonation",
                    "impersonate",
                ]
                is_auth = any(sub in low for sub in auth_indicators) or (
                    "login" in low and "tiktok" in (url or "").lower()
                )

                try:
                    if request_id:
                        db.add_request_event(request_id, "error", details=msg)
                except Exception:
                    pass

                try:
                    if is_auth and request_id:
                        try:
                            db.add_request_event(
                                request_id, "auth_required", details=msg
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

                if is_auth:
                    logger.warning(
                        "Auth/private content error processing %s: %s", url, msg
                    )
                    metrics.downloads_failed_total.inc()
                    try:
                        hint = "Contenuto privato o che richiede autenticazione: il bot al momento non riesce a scaricarlo."
                        if chat_type and chat_type.lower() in (
                            "group",
                            "supergroup",
                            "channel",
                        ):
                            if original_message_id:
                                try:
                                    if reaction_current == "eyes":
                                        try:
                                            rrem = (
                                                await telegram_api.set_message_reaction(
                                                    self.token,
                                                    chat_id,
                                                    original_message_id,
                                                    "👀",
                                                    remove=True,
                                                )
                                            )
                                            # if removal succeeded, clear state
                                            if isinstance(rrem, dict) and rrem.get(
                                                "ok"
                                            ):
                                                reaction_current = None
                                        except Exception:
                                            pass
                                    try:
                                        radd = await telegram_api.set_message_reaction(
                                            self.token,
                                            chat_id,
                                            original_message_id,
                                            "🍌",
                                        )
                                        ok_radd = (
                                            (isinstance(radd, dict) and radd.get("ok"))
                                            if isinstance(radd, dict)
                                            else bool(radd)
                                        )
                                        if ok_radd:
                                            reaction_current = "banana"
                                        else:
                                            radd = None
                                    except Exception:
                                        radd = None
                                    if not radd:
                                        try:
                                            await telegram_api.send_message(
                                                self.token,
                                                chat_id,
                                                "🍌",
                                                reply_to_message_id=original_message_id,
                                            )
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            else:
                                try:
                                    await telegram_api.send_message(
                                        self.token,
                                        chat_id,
                                        "🍌",
                                        reply_to_message_id=original_message_id,
                                    )
                                except Exception:
                                    pass
                            try:
                                await telegram_api.send_message(
                                    self.token,
                                    chat_id,
                                    hint,
                                    reply_to_message_id=original_message_id,
                                )
                            except Exception:
                                pass
                        else:
                            # In private chats try to remove the eyes reaction (if we added it)
                            # then attempt to add a banana reaction; if reactions are not
                            # supported fall back to sending a banana emoji message.
                            if original_message_id:
                                try:
                                    if reaction_current == "eyes":
                                        try:
                                            rrem = (
                                                await telegram_api.set_message_reaction(
                                                    self.token,
                                                    chat_id,
                                                    original_message_id,
                                                    "👀",
                                                    remove=True,
                                                )
                                            )
                                            if isinstance(rrem, dict) and not rrem.get(
                                                "ok"
                                            ):
                                                logger.debug(
                                                    "set_message_reaction(remove) returned: %s",
                                                    rrem,
                                                )
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                try:
                                    radd = await telegram_api.set_message_reaction(
                                        self.token, chat_id, original_message_id, "🍌"
                                    )
                                    ok_radd = (
                                        (isinstance(radd, dict) and radd.get("ok"))
                                        if isinstance(radd, dict)
                                        else bool(radd)
                                    )
                                    if ok_radd:
                                        reaction_current = "banana"
                                    else:
                                        try:
                                            await telegram_api.send_message(
                                                self.token,
                                                chat_id,
                                                "🍌",
                                                reply_to_message_id=original_message_id,
                                            )
                                        except Exception:
                                            pass
                                except Exception:
                                    try:
                                        await telegram_api.send_message(
                                            self.token,
                                            chat_id,
                                            "🍌",
                                            reply_to_message_id=original_message_id,
                                        )
                                    except Exception:
                                        pass
                            try:
                                await telegram_api.send_message(
                                    self.token,
                                    chat_id,
                                    hint,
                                    reply_to_message_id=original_message_id,
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
                else:
                    logger.exception("Error during download: %s", e)
                    metrics.downloads_failed_total.inc()
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass

                return

            try:
                size = os.path.getsize(file_path)
            except Exception:
                size = None
            final_size = (
                meta.get("final_size") if isinstance(meta, dict) else None
            ) or size

            # mark started
            try:
                if request_id:
                    db.mark_request_started(request_id)
            except Exception:
                pass

            # If the downloaded file lacks a video stream but has an mp4 extension,
            # attempt to add a minimal black video track so mobile clients can play it.
            try:
                has_video = meta.get("has_video") if isinstance(meta, dict) else True
            except Exception:
                has_video = True

            if not has_video:
                logger.info(
                    "Downloaded file appears to have no video stream; attempting wrapper for %s",
                    file_path,
                )
                ffmpeg_bin = shutil.which("ffmpeg")
                if ffmpeg_bin:
                    try:
                        base = os.path.splitext(os.path.basename(file_path))[0]
                        wrapper = os.path.join(
                            os.path.dirname(file_path), f"{base}_withvideo.mp4"
                        )
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
                            file_path,
                            "-map",
                            "0:v:0",
                            "-map",
                            "1:a:0?",
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
                            wrapper,
                        ]
                        logger.info("Running ffmpeg wrapper: %s", " ".join(ffmpeg_cmd))
                        pwrap = await asyncio.create_subprocess_exec(
                            *ffmpeg_cmd, stdout=PIPE, stderr=PIPE
                        )
                        try:
                            outw, errw = await asyncio.wait_for(
                                pwrap.communicate(), timeout=120
                            )
                        except asyncio.TimeoutError:
                            try:
                                pwrap.kill()
                            except Exception:
                                pass
                            try:
                                await pwrap.communicate()
                            except Exception:
                                pass
                            logger.warning("ffmpeg wrapper timed out for %s", file_path)
                        except asyncio.CancelledError:
                            try:
                                pwrap.kill()
                            except Exception:
                                pass
                            try:
                                await pwrap.communicate()
                            except Exception:
                                pass
                            raise
                        else:
                            if pwrap.returncode == 0 and os.path.exists(wrapper):
                                file_path = wrapper
                                size = os.path.getsize(file_path)
                                final_size = size
                                logger.info(
                                    "Created wrapper video %s (%d bytes)",
                                    file_path,
                                    size,
                                )
                            else:
                                try:
                                    errtxt = (
                                        errw.decode(errors="ignore") if errw else ""
                                    )
                                except Exception:
                                    errtxt = ""
                                logger.warning(
                                    "ffmpeg wrapper failed for %s: %s",
                                    file_path,
                                    errtxt[:1000],
                                )
                    except Exception:
                        logger.exception("Exception while attempting ffmpeg wrapper")
                else:
                    logger.info(
                        "ffmpeg not available; will send file as document (no video stream)"
                    )

            # helper: send compression notice in private chats
            async def _maybe_notify_compression():
                try:
                    compressed = (
                        meta.get("compressed") if isinstance(meta, dict) else False
                    )
                    if (
                        compressed
                        and chat_type
                        and chat_type.lower() not in ("group", "supergroup", "channel")
                    ):
                        orig_mb = _mb(
                            meta.get("original_size")
                            if isinstance(meta, dict)
                            else None
                        )
                        final_mb = _mb(final_size)
                        try:
                            await telegram_api.send_message(
                                self.token,
                                chat_id,
                                f"Ho compresso il video: {orig_mb} -> {final_mb}",
                                reply_to_message_id=original_message_id,
                            )
                        except Exception:
                            logger.debug("Could not send compression notice")
                except Exception:
                    logger.debug("compression notify failed")

            # store final stats
            try:
                if request_id:
                    db.mark_request_finished(
                        request_id,
                        final_size=final_size,
                        compressed=(
                            meta.get("compressed") if isinstance(meta, dict) else False
                        ),
                    )
            except Exception:
                pass

            # attempt to claim the request for sending to avoid duplicates
            claimed = True
            if request_id is not None:
                try:
                    claimed = db.claim_request_for_sending(request_id)
                except Exception:
                    claimed = True
            if not claimed:
                try:
                    status_msg_id = await self._edit_status(
                        self.token, chat_id, status_msg_id, "✅ already processed"
                    )
                except Exception:
                    pass
                return

            ext = os.path.splitext(file_path)[1].lower().lstrip(".")
            # generate thumbnail for mp4 (optional, controlled by config)
            thumb_path = None
            if (
                getattr(config, "WORKER_GENERATE_THUMBNAIL", True)
                and ext == "mp4"
                and shutil.which("ffmpeg")
            ):
                try:
                    thumb_path = await self._generate_thumbnail(
                        shutil.which("ffmpeg"), file_path, tmpdir
                    )
                except Exception:
                    logger.debug("thumbnail generation failed for %s", file_path)
            else:
                if not getattr(config, "WORKER_GENERATE_THUMBNAIL", True):
                    logger.debug(
                        "Skipping thumbnail generation (WORKER_GENERATE_THUMBNAIL=False)"
                    )

            # check codec/profile
            non_baseline = False
            try:
                if isinstance(meta, dict):
                    meta_profile = (meta.get("video_profile") or "").lower()
                    non_baseline = bool(meta_profile and "baseline" not in meta_profile)
            except Exception:
                meta_profile = ""
                non_baseline = False

            # track whether we performed a transcode here (not used elsewhere)
            # Ensure final file is an MP4 compatible with Telegram. If the file is not
            # MP4 or has a non-baseline profile, attempt a baseline transcode here.
            if ext != "mp4" or non_baseline:
                ffmpeg_bin = shutil.which("ffmpeg")
                if not ffmpeg_bin:
                    logger.warning(
                        "Cannot convert to MP4 baseline: ffmpeg not available for %s",
                        file_path,
                    )
                    try:
                        status_msg_id = await self._edit_status(
                            self.token,
                            chat_id,
                            status_msg_id,
                            "👀 conversion failed: ffmpeg missing",
                        )
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
                    return
                try:
                    base = os.path.splitext(os.path.basename(file_path))[0]
                    trans_path = os.path.join(tmpdir, f"{base}_tg_transcoded.mp4")
                    ok = await self._transcode_to_baseline(
                        file_path, trans_path, tmpdir, meta=meta
                    )
                    if ok and os.path.exists(trans_path):
                        try:
                            file_path = trans_path
                            ext = "mp4"
                            size = os.path.getsize(file_path)
                            final_size = size
                            # regenerate thumbnail if possible
                            if thumb_path and os.path.exists(thumb_path):
                                try:
                                    os.remove(thumb_path)
                                except Exception:
                                    pass
                            if getattr(
                                config, "WORKER_GENERATE_THUMBNAIL", True
                            ) and shutil.which("ffmpeg"):
                                try:
                                    thumb_path = await self._generate_thumbnail(
                                        shutil.which("ffmpeg"), file_path, tmpdir
                                    )
                                except Exception:
                                    logger.debug(
                                        "thumbnail regen failed after transcode"
                                    )
                            else:
                                if not getattr(
                                    config, "WORKER_GENERATE_THUMBNAIL", True
                                ):
                                    logger.debug(
                                        "Skipping thumbnail regen after transcode (WORKER_GENERATE_THUMBNAIL=False)"
                                    )
                            # transcode succeeded
                        except Exception:
                            pass
                    else:
                        logger.warning(
                            "Worker ffmpeg transcode failed for %s", file_path
                        )
                        try:
                            status_msg_id = await self._edit_status(
                                self.token,
                                chat_id,
                                status_msg_id,
                                "👀 conversion to mp4 failed",
                            )
                        except Exception:
                            pass
                        try:
                            if request_id:
                                db.update_request_status(request_id, "failed")
                        except Exception:
                            pass
                        return
                except Exception:
                    logger.exception("Worker transcode exception")

            # send media (only send as Telegram video; do not fall back to document)
            try:
                if size is not None and size <= getattr(
                    config, "TELEGRAM_MAX_FILE_SIZE", 50 * 1024 * 1024
                ):
                    if ext != "mp4":
                        logger.warning(
                            "Final file is not MP4; cannot send as video: %s", file_path
                        )
                        try:
                            status_msg_id = await self._edit_status(
                                self.token,
                                chat_id,
                                status_msg_id,
                                "👀 upload failed — incompatible format",
                            )
                        except Exception:
                            pass
                        try:
                            if request_id:
                                db.update_request_status(request_id, "failed")
                        except Exception:
                            pass
                    else:
                        try:
                            res = await telegram_api.send_video(
                                self.token,
                                chat_id,
                                file_path,
                                reply_to_message_id=original_message_id,
                                thumbnail_path=thumb_path,
                            )
                        except Exception as e:
                            logger.exception("send_video failed: %s", e)
                            try:
                                status_msg_id = await self._edit_status(
                                    self.token,
                                    chat_id,
                                    status_msg_id,
                                    "👀 upload failed",
                                )
                            except Exception:
                                pass
                            try:
                                if request_id:
                                    db.update_request_status(request_id, "failed")
                            except Exception:
                                pass
                            return

                        ok = isinstance(res, dict) and res.get("ok")
                        if not ok:
                            logger.warning("send_video returned non-ok: %s", res)
                            try:
                                status_msg_id = await self._edit_status(
                                    self.token,
                                    chat_id,
                                    status_msg_id,
                                    "👀 upload failed",
                                )
                            except Exception:
                                pass
                            try:
                                if request_id:
                                    db.update_request_status(request_id, "failed")
                            except Exception:
                                pass
                        else:
                            metrics.files_sent_total.inc()
                            try:
                                if request_id:
                                    db.update_request_status(request_id, "done")
                            except Exception:
                                pass
                            try:
                                await _maybe_notify_compression()
                            except Exception:
                                pass
                            try:
                                if reaction_current == "eyes" and original_message_id:
                                    try:
                                        await telegram_api.set_message_reaction(
                                            self.token,
                                            chat_id,
                                            original_message_id,
                                            "👀",
                                            remove=True,
                                        )
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            if status_msg_id:
                                try:
                                    await telegram_api.delete_message(
                                        self.token, chat_id, status_msg_id
                                    )
                                except Exception:
                                    pass
                else:
                    logger.warning("File too large (%s). Sending fallback link.", size)
                    metrics.files_too_large_total.inc()
                    try:
                        status_msg_id = await self._edit_status(
                            self.token,
                            chat_id,
                            status_msg_id,
                            f"👀 file troppo grande — {_mb(size)}",
                        )
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                auth_indicators = [
                    "you need to log in",
                    "you need to log in to access",
                    "login required",
                    "requiring login",
                    "requires login",
                    "no csrf token",
                    "this content isn't available",
                    "not available to everyone",
                    "authentication",
                    "cookies",
                    "impersonation",
                    "impersonate",
                ]
                is_auth = any(sub in low for sub in auth_indicators) or (
                    "login" in low and "tiktok" in (url or "").lower()
                )

                try:
                    if request_id:
                        db.add_request_event(request_id, "error", details=msg)
                except Exception:
                    pass

                try:
                    if is_auth and request_id:
                        try:
                            db.add_request_event(
                                request_id, "auth_required", details=msg
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

                if is_auth:
                    logger.warning(
                        "Auth/private content error processing %s: %s", url, msg
                    )
                    metrics.downloads_failed_total.inc()
                    try:
                        hint = "Contenuto privato o che richiede autenticazione: il bot al momento non riesce a scaricarlo."
                        if chat_type and chat_type.lower() in (
                            "group",
                            "supergroup",
                            "channel",
                        ):
                            if original_message_id:
                                try:
                                    if reaction_current == "eyes":
                                        try:
                                            await telegram_api.set_message_reaction(
                                                self.token,
                                                chat_id,
                                                original_message_id,
                                                "👀",
                                                remove=True,
                                            )
                                        except Exception:
                                            pass
                                    try:
                                        await telegram_api.set_message_reaction(
                                            self.token,
                                            chat_id,
                                            original_message_id,
                                            "🍌",
                                        )
                                    except Exception:
                                        try:
                                            await telegram_api.send_message(
                                                self.token,
                                                chat_id,
                                                "🍌",
                                                reply_to_message_id=original_message_id,
                                            )
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            else:
                                try:
                                    await telegram_api.send_message(
                                        self.token,
                                        chat_id,
                                        "🍌",
                                        reply_to_message_id=original_message_id,
                                    )
                                except Exception:
                                    pass
                            try:
                                await telegram_api.send_message(
                                    self.token,
                                    chat_id,
                                    hint,
                                    reply_to_message_id=original_message_id,
                                )
                            except Exception:
                                pass
                        else:
                            if reaction_current == "eyes" and original_message_id:
                                try:
                                    await telegram_api.set_message_reaction(
                                        self.token,
                                        chat_id,
                                        original_message_id,
                                        "👀",
                                        remove=True,
                                    )
                                except Exception:
                                    pass
                            try:
                                await telegram_api.send_message(
                                    self.token,
                                    chat_id,
                                    hint,
                                    reply_to_message_id=original_message_id,
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
                else:
                    logger.exception("Error processing link: %s", e)
                    metrics.downloads_failed_total.inc()
                    try:
                        if status_msg_id:
                            try:
                                await telegram_api.delete_message(
                                    self.token, chat_id, status_msg_id
                                )
                            except Exception:
                                try:
                                    await telegram_api.edit_message_text(
                                        self.token, chat_id, status_msg_id, ""
                                    )
                                except Exception:
                                    pass
                            if reaction_current == "eyes" and original_message_id:
                                try:
                                    await telegram_api.set_message_reaction(
                                        self.token,
                                        chat_id,
                                        original_message_id,
                                        "👀",
                                        remove=True,
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, "failed")
                    except Exception:
                        pass
            finally:
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass

        except Exception as e:
            logger.exception("Error processing link %s: %s", url, e)
            metrics.downloads_failed_total.inc()
            try:
                if request_id:
                    db.add_request_event(request_id, "error", details=str(e))
                    db.update_request_status(request_id, "failed")
            except Exception:
                pass
        finally:
            try:
                # Ensure we always try to clear the eyes reaction and delete the status
                # message regardless of how the processing ended.
                try:
                    if reaction_current == "eyes" and original_message_id:
                        try:
                            await telegram_api.set_message_reaction(
                                self.token,
                                chat_id,
                                original_message_id,
                                "👀",
                                remove=True,
                            )
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    if status_msg_id:
                        try:
                            await telegram_api.delete_message(
                                self.token, chat_id, status_msg_id
                            )
                        except Exception:
                            try:
                                await telegram_api.edit_message_text(
                                    self.token, chat_id, status_msg_id, ""
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
            finally:
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
