import asyncio
import time
import tempfile
import shutil
import os
import logging
import re

from . import downloader, telegram_api, link_utils, config, metrics, db

logger = logging.getLogger(__name__)


class WorkerPool:
    def __init__(self, token: str, workers: int = 2):
        self.token = token
        self._sem = asyncio.Semaphore(workers)

    

    def enqueue(self, chat_id: int, url: str, description: str = None, original_message_id: int = None, chat_type: str = 'private'):
        # compatibility wrapper if called with description
        try:
            db.init_db()
        except Exception:
            pass
        # Guard: never enqueue internal Telegram links (t.me / telegram.me) or unsupported sites
        try:
            if url and re.search(r'^https?://(?:t\.me|telegram\.me)/', url, re.IGNORECASE):
                logger.debug("Ignoring telegram internal link in enqueue: %s", url)
                return
        except Exception:
            pass
        try:
            if not link_utils.is_supported(url):
                logger.debug("enqueue ignored for unsupported url: %s", url)
                return
        except Exception:
            # if the support-check fails, proceed conservatively
            pass
        request_id = None
        try:
            request_id = db.add_request(chat_id, url, status='pending', description=description, original_message_id=original_message_id)
            logger.debug("Recorded request id=%s", request_id)
        except Exception:
            logger.exception("Failed to record request in DB")
        asyncio.create_task(self._process_link(chat_id, url, request_id, description, original_message_id, chat_type))

    async def _process_link(self, chat_id: int, url: str, request_id: int = None, description: str = None, original_message_id: int = None, chat_type: str = 'private'):
        async with self._sem:
            tmpdir = tempfile.mkdtemp(dir=config.TMP_DIR)
            metrics.processed_links_total.inc()
            try:
                logger.info("Processing %s in %s", url, tmpdir)
                def _mb(b):
                    try:
                        return f"{(float(b) / (1024*1024)):.1f} MB"
                    except Exception:
                        return 'unknown'

                reaction_added = False

                async def edit_status(text: str):
                    try:
                        if status_msg_id:
                            await telegram_api.edit_message_text(self.token, chat_id, status_msg_id, text)
                        else:
                            await telegram_api.send_message(self.token, chat_id, text, reply_to_message_id=original_message_id)
                    except Exception:
                        logger.debug('Could not edit/send status message')

                # notify callback updates the same status message and records event timings
                compress_start_ts = None
                redownload_start_ts = None
                original_size = None
                async def _notify(ev: dict):
                    nonlocal compress_start_ts, redownload_start_ts, original_size
                    try:
                        t = ev.get('type')
                        if t == 'compress_start':
                            compress_start_ts = time.time()
                            orig = ev.get('original_size')
                            original_size = orig
                            if orig and request_id:
                                try:
                                    db.set_request_original_size(request_id, int(orig))
                                except Exception:
                                    pass
                            try:
                                await edit_status(f"👀 compressing — orig: {_mb(ev.get('original_size'))}")
                            except Exception:
                                pass
                        elif t == 'compress_done':
                            ns = ev.get('new_size')
                            if compress_start_ts:
                                dur = time.time() - compress_start_ts
                                try:
                                    if request_id:
                                        db.add_request_event(request_id, 'compress', details=None, duration_seconds=dur)
                                except Exception:
                                    pass
                            try:
                                await edit_status(f"👀 compressed — new: {_mb(ns)}; uploading...")
                            except Exception:
                                pass
                        elif t == 'redownload_start':
                            redownload_start_ts = time.time()
                            try:
                                await edit_status(f"👀 redownloading lower-quality...")
                            except Exception:
                                pass
                        elif t == 'redownload_done':
                            ns = ev.get('new_size')
                            if redownload_start_ts:
                                dur = time.time() - redownload_start_ts
                                try:
                                    if request_id:
                                        db.add_request_event(request_id, 'redownload', details=None, duration_seconds=dur)
                                except Exception:
                                    pass
                            try:
                                await edit_status(f"👀 redownloaded — new: {_mb(ns)}; uploading...")
                            except Exception:
                                pass
                    except Exception:
                        logger.debug('notify failed')

                # attempt to claim the request for processing (download/compress) to avoid duplicate workers
                claimed_process = True
                if request_id is not None:
                    try:
                        claimed_process = db.claim_request_for_processing(request_id)
                    except Exception:
                        claimed_process = True
                if not claimed_process:
                    # another worker/process is handling this request
                    return

                # Try to add an eyes reaction to the original message; if that fails,
                # fall back to creating a short status message (so older bots still work).
                status_res = None
                status_msg_id = None
                if original_message_id:
                    try:
                        r = await telegram_api.set_message_reaction(self.token, chat_id, original_message_id, '👀')
                        # success if aiogram returned a truthy response or dict.ok
                        if isinstance(r, dict):
                            ok = r.get('ok')
                        else:
                            ok = bool(r)
                        if ok:
                            reaction_added = True
                    except Exception:
                        logger.debug('Could not add reaction; falling back to status message')
                        try:
                            status_res = await telegram_api.send_message(self.token, chat_id, "In lavorazione 👀", reply_to_message_id=original_message_id)
                            if isinstance(status_res, dict) and status_res.get('ok'):
                                status_msg_id = status_res.get('result', {}).get('message_id')
                        except Exception:
                            logger.debug('Could not create status message')
                else:
                    try:
                        status_res = await telegram_api.send_message(self.token, chat_id, "In lavorazione 👀", reply_to_message_id=original_message_id)
                        if isinstance(status_res, dict) and status_res.get('ok'):
                            status_msg_id = status_res.get('result', {}).get('message_id')
                    except Exception:
                        logger.debug('Could not create status message')

                file_path, meta = await downloader.download(url, tmpdir, max_bytes=config.TELEGRAM_MAX_FILE_SIZE, notify=_notify)
                size = os.path.getsize(file_path)
                compressed = meta.get('compressed') if isinstance(meta, dict) else False
                final_size = meta.get('final_size') if isinstance(meta, dict) else size
                async def _maybe_notify_compression():
                    try:
                        if compressed and chat_type and chat_type.lower() not in ('group', 'supergroup', 'channel'):
                            orig_mb = _mb(original_size) if original_size else 'unknown'
                            final_mb = _mb(final_size)
                            try:
                                await telegram_api.send_message(self.token, chat_id, f"Ho compresso il video: {orig_mb} -> {final_mb}", reply_to_message_id=original_message_id)
                            except Exception:
                                logger.debug('Could not send compression notice')
                    except Exception:
                        logger.debug('compression notify failed')
                # store final stats
                try:
                    if request_id:
                        db.mark_request_finished(request_id, final_size=final_size, compressed=compressed)
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
                        await edit_status("✅ already processed")
                    except Exception:
                        pass
                    return

                if size <= config.TELEGRAM_MAX_FILE_SIZE:
                    logger.info("Sending %s (size=%d)", file_path, size)
                    try:
                        # send final media as a reply to the original message
                        res = await telegram_api.send_media(self.token, chat_id, file_path, reply_to_message_id=original_message_id)
                        logger.debug("send_media response: %s", res)
                        ok = isinstance(res, dict) and res.get('ok')
                        if not ok:
                            logger.warning("send_media reported failure, attempting send_document fallback: %s", res)
                            try:
                                res2 = await telegram_api.send_document(self.token, chat_id, file_path, reply_to_message_id=original_message_id)
                                logger.debug("send_document response: %s", res2)
                                ok2 = isinstance(res2, dict) and res2.get('ok')
                                if not ok2:
                                    logger.warning("send_document also failed: %s", res2)
                                    try:
                                            await edit_status(f"👀 upload failed")
                                    except Exception:
                                        pass
                                    metrics.files_too_large_total.inc()
                                    try:
                                        if request_id:
                                            db.update_request_status(request_id, 'failed')
                                    except Exception:
                                        logger.debug('Could not update request status to failed')
                                else:
                                    metrics.files_sent_total.inc()
                                    try:
                                        if request_id:
                                            db.update_request_status(request_id, 'done')
                                    except Exception:
                                        logger.debug('Could not update request status to done')
                                    try:
                                        await _maybe_notify_compression()
                                    except Exception:
                                        pass
                                    # update status to indicate success
                                    try:
                                        await edit_status(f"✅ done — {_mb(final_size)}")
                                    except Exception:
                                        pass
                            except Exception:
                                logger.exception("send_document fallback failed")
                                try:
                                    await edit_status(f"👀 upload failed")
                                except Exception:
                                    pass
                                try:
                                    if request_id:
                                        db.update_request_status(request_id, 'failed')
                                except Exception:
                                    logger.debug('Could not update request status to failed')
                        else:
                            metrics.files_sent_total.inc()
                            try:
                                if request_id:
                                    db.update_request_status(request_id, 'done')
                            except Exception:
                                logger.debug('Could not update request status to done')
                            try:
                                await _maybe_notify_compression()
                            except Exception:
                                pass
                    except Exception:
                        logger.exception("Failed sending via send_media, falling back to send_document")
                        try:
                            res2 = await telegram_api.send_document(self.token, chat_id, file_path, reply_to_message_id=original_message_id)
                            logger.debug("send_document response: %s", res2)
                            ok2 = isinstance(res2, dict) and res2.get('ok')
                            if ok2:
                                metrics.files_sent_total.inc()
                                try:
                                    if request_id:
                                        db.update_request_status(request_id, 'done')
                                except Exception:
                                    logger.debug('Could not update request status to done')
                                try:
                                    await _maybe_notify_compression()
                                except Exception:
                                    pass
                            else:
                                logger.warning("send_document failed after exception: %s", res2)
                                try:
                                        await edit_status(f"👀 upload failed")
                                except Exception:
                                    pass
                                try:
                                    if request_id:
                                        db.update_request_status(request_id, 'failed')
                                except Exception:
                                    logger.debug('Could not update request status to failed')
                        except Exception:
                            logger.exception("send_document fallback also failed")
                            try:
                                    await edit_status(f"👀 upload failed")
                            except Exception:
                                pass
                            try:
                                if request_id:
                                    db.update_request_status(request_id, 'failed')
                            except Exception:
                                logger.debug('Could not update request status to failed')
                else:
                    logger.warning("File too large (%d bytes). Sending fallback link.", size)
                    metrics.files_too_large_total.inc()
                    try:
                            await edit_status(f"👀 file too large — {_mb(size)}")
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, 'failed')
                    except Exception:
                        logger.debug('Could not update request status to failed')
            except Exception as e:
                msg = str(e)
                logger.exception("Error processing link: %s", e)
                # record full error in request events for UI/diagnostics
                try:
                    if request_id:
                        db.add_request_event(request_id, 'error', details=msg)
                except Exception:
                    pass

                low = msg.lower()
                # authentication / instagram privacy related failures
                auth_indicators = ['you need to log in', 'no csrf token', "this content isn't available", 'not available to everyone', 'you need to log in to access', 'authentication', 'cookies']
                is_auth = any(sub in low for sub in auth_indicators)

                # If downloader indicated the file was too large, show concise MB message
                if 'downloaded file too large' in msg:
                    metrics.files_too_large_total.inc()
                    logger.warning("Downloaded file exceeds configured limit: %s", msg)
                    try:
                        size_part = msg.split('(')[-1].rstrip(')')
                    except Exception:
                        size_part = 'unknown'
                    try:
                        await edit_status(f"In lavorazione 👀 — file troppo grande: {size_part}")
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, 'failed')
                    except Exception:
                        logger.debug('Could not update request status to failed')
                elif is_auth:
                    # Auth / privacy: story/private content failures use reactions: 👀 while processing, 🍌 when failed.
                    # In private chats, also send an explanatory message.
                    metrics.downloads_failed_total.inc()
                    try:
                        hint = "Contenuto privato o richiede autenticazione: il contenuto non è accessibile."
                        if chat_type and chat_type.lower() in ('group', 'supergroup', 'channel'):
                            # group: replace eyes with banana reaction
                            if original_message_id:
                                try:
                                    if reaction_added:
                                        # remove eyes reaction (best-effort)
                                        try:
                                            await telegram_api.set_message_reaction(self.token, chat_id, original_message_id, '👀', remove=True)
                                        except Exception:
                                            pass
                                    # add banana reaction
                                    try:
                                        await telegram_api.set_message_reaction(self.token, chat_id, original_message_id, '🍌')
                                    except Exception:
                                        # fallback to sending a banana reply
                                        try:
                                            await telegram_api.send_message(self.token, chat_id, '🍌', reply_to_message_id=original_message_id)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            else:
                                try:
                                    await telegram_api.send_message(self.token, chat_id, '🍌', reply_to_message_id=original_message_id)
                                except Exception:
                                    pass
                            # Also reply in group with short explanatory hint (no implementation details)
                            try:
                                await telegram_api.send_message(self.token, chat_id, hint, reply_to_message_id=original_message_id)
                            except Exception:
                                pass
                        else:
                            # private chat: also include an explanatory message (separate)
                            if original_message_id:
                                try:
                                    if reaction_added:
                                        try:
                                            await telegram_api.set_message_reaction(self.token, chat_id, original_message_id, '👀', remove=True)
                                        except Exception:
                                            pass
                                    try:
                                        await telegram_api.set_message_reaction(self.token, chat_id, original_message_id, '🍌')
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                            try:
                                # send explanatory message as a separate reply
                                await telegram_api.send_message(self.token, chat_id, hint, reply_to_message_id=original_message_id)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, 'failed')
                    except Exception:
                        logger.debug('Could not update request status to failed')
                else:
                    # Generic error: suppress chat error messages per user preference.
                    metrics.downloads_failed_total.inc()
                    try:
                        # remove initial status message if created so no error is shown
                        if status_msg_id:
                            try:
                                await telegram_api.delete_message(self.token, chat_id, status_msg_id)
                            except Exception:
                                # best-effort: if delete fails, attempt to edit to a short neutral notice
                                try:
                                    await telegram_api.edit_message_text(self.token, chat_id, status_msg_id, "")
                                except Exception:
                                    pass
                        # if we added a reaction (👀) earlier, attempt to remove it
                        if reaction_added and original_message_id:
                            try:
                                await telegram_api.set_message_reaction(self.token, chat_id, original_message_id, '👀', remove=True)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        if request_id:
                            db.update_request_status(request_id, 'failed')
                    except Exception:
                        logger.debug('Could not update request status to failed')
            finally:
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
