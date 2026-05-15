import logging
import os
from typing import Optional, Sequence

import aiohttp

from . import config, http_client, telegram_client

logger = logging.getLogger(__name__)

# per-chat reaction suppression when API reports reactions are invalid/unavailable
_reaction_disabled_until: dict = {}
_reaction_disable_seconds = getattr(config, "REACTION_DISABLE_SECONDS", 300)


async def send_message(
    token: str, chat_id: int, text: str, reply_to_message_id: int = None
):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = int(reply_to_message_id)
    session = await http_client.get_session()
    async with session.post(url, json=payload) as resp:
        data = await resp.json()
        return data


async def edit_message_text(token: str, chat_id: int, message_id: int, text: str):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    session = await http_client.get_session()
    async with session.post(url, json=payload) as resp:
        try:
            return await resp.json()
        except Exception:
            return {"ok": False, "status": resp.status}


async def send_document(
    token: str,
    chat_id: int,
    file_path: str,
    caption: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    if caption:
        data.add_field("caption", caption)
    if reply_to_message_id is not None:
        data.add_field("reply_to_message_id", str(int(reply_to_message_id)))
    try:
        f = open(file_path, "rb")
    except Exception as e:
        logger.exception("open file failed: %s", e)
        raise
    data.add_field(
        "document",
        f,
        filename=os.path.basename(file_path),
        content_type="application/octet-stream",
    )
    # attach a thumbnail if provided (Telegram API accepts 'thumbnail' for documents)
    tf = None
    if thumbnail_path:
        try:
            tf = open(thumbnail_path, "rb")
            data.add_field(
                "thumbnail",
                tf,
                filename=os.path.basename(thumbnail_path),
                content_type="image/jpeg",
            )
        except Exception:
            logger.debug("Could not attach thumbnail %s", thumbnail_path)
    session = await http_client.get_session()
    async with session.post(url, data=data) as resp:
        try:
            result = await resp.json()
        except Exception:
            result = {"ok": False, "status": resp.status}
        finally:
            f.close()
            try:
                if tf:
                    tf.close()
            except Exception:
                pass
        return result


async def send_video(
    token: str,
    chat_id: int,
    file_path: str,
    caption: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
    meta: Optional[dict] = None,
):
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    if caption:
        data.add_field("caption", caption)
    if reply_to_message_id is not None:
        data.add_field("reply_to_message_id", str(int(reply_to_message_id)))
    try:
        f = open(file_path, "rb")
    except Exception as e:
        logger.exception("open file failed: %s", e)
        raise
    data.add_field(
        "video", f, filename=os.path.basename(file_path), content_type="video/mp4"
    )
    # attach a pre-generated thumbnail if available (Telegram accepts JPEG/PNG)
    if thumbnail_path:
        try:
            tf = open(thumbnail_path, "rb")
            data.add_field(
                "thumbnail",
                tf,
                filename=os.path.basename(thumbnail_path),
                content_type="image/jpeg",
            )
        except Exception:
            logger.debug("Could not attach thumbnail %s", thumbnail_path)
    # If ffprobe/meta information is available, include explicit fields
    # so Telegram can correctly interpret orientation and sizing.
    try:
        if isinstance(meta, dict):
            # duration in seconds (integer) — handle 0 and string values explicitly
            dur = None
            try:
                if "duration" in meta and meta.get("duration") is not None:
                    dur = float(meta.get("duration"))
                else:
                    fmt = (
                        meta.get("format")
                        if isinstance(meta.get("format"), dict)
                        else None
                    )
                    if fmt and fmt.get("duration") is not None:
                        dur = float(fmt.get("duration"))
            except Exception:
                dur = None
            if dur is not None:
                try:
                    data.add_field("duration", str(int(float(dur))))
                except Exception:
                    pass

            # width/height from ffprobe — preserve zeros if explicit
            w = None
            h = None
            try:
                if "video_width" in meta and meta.get("video_width") is not None:
                    w = meta.get("video_width")
                elif "width" in meta and meta.get("width") is not None:
                    w = meta.get("width")
                if "video_height" in meta and meta.get("video_height") is not None:
                    h = meta.get("video_height")
                elif "height" in meta and meta.get("height") is not None:
                    h = meta.get("height")
            except Exception:
                w = None
                h = None
            try:
                if w is not None:
                    data.add_field("width", str(int(w)))
            except Exception:
                pass
            try:
                if h is not None:
                    data.add_field("height", str(int(h)))
            except Exception:
                pass

            # hint to Telegram that this supports streaming
            try:
                data.add_field("supports_streaming", "true")
            except Exception:
                pass
    except Exception:
        logger.debug("Could not attach ffprobe meta fields to sendVideo multipart")
    session = await http_client.get_session()
    async with session.post(url, data=data) as resp:
        try:
            result = await resp.json()
        except Exception:
            result = {"ok": False, "status": resp.status}
        finally:
            f.close()
            try:
                if thumbnail_path:
                    tf.close()
            except Exception:
                pass
        return result


async def send_media(
    token: str,
    chat_id: int,
    file_path: str,
    caption: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    meta: Optional[dict] = None,
):
    # If meta from ffprobe is provided, prefer it to decide inline-video vs document.
    try:
        if isinstance(meta, dict):
            fmt = (meta.get("format") or "").lower()
            has_video = meta.get("has_video")
            if has_video and "mp4" in fmt:
                return await send_video(
                    token,
                    chat_id,
                    file_path,
                    caption=caption,
                    reply_to_message_id=reply_to_message_id,
                    meta=meta,
                )
    except Exception:
        pass

    # fallback: choose sendVideo for mp4 files to enable inline playback, otherwise sendDocument
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    if ext == "mp4":
        return await send_video(
            token,
            chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            meta=meta,
        )
    logger.info(
        "send_media: file %s is not mp4 (.%s); sending as document. Consider converting to mp4 for inline playback",
        file_path,
        ext,
    )
    return await send_document(
        token,
        chat_id,
        file_path,
        caption=caption,
        reply_to_message_id=reply_to_message_id,
    )


async def set_webhook(
    token: str, webhook_url: str, allowed_updates: Optional[Sequence[str]] = None
):
    url = f"https://api.telegram.org/bot{token}/setWebhook"
    payload = {"url": webhook_url}
    if allowed_updates is not None:
        payload["allowed_updates"] = list(allowed_updates)
    session = await http_client.get_session()
    async with session.post(url, json=payload) as resp:
        try:
            return await resp.json()
        except Exception:
            return {"ok": False, "status": resp.status}


async def delete_message(token: str, chat_id: int, message_id: int):
    url = f"https://api.telegram.org/bot{token}/deleteMessage"
    payload = {"chat_id": chat_id, "message_id": int(message_id)}
    session = await http_client.get_session()
    async with session.post(url, json=payload) as resp:
        try:
            return await resp.json()
        except Exception:
            return {"ok": False, "status": resp.status}


async def set_message_reaction(
    token: str, chat_id: int, message_id: int, reaction: str, remove: bool = False
):
    """Set or remove a reaction on a message using aiogram's Bot API wrapper.

    This uses aiogram.Bot.set_message_reaction under the hood so the library
    constructs the correct payload for the Telegram Bot API.
    """
    try:
        import time

        # per-chat suppression: if we recently detected reactions are invalid
        try:
            now = time.time()
            disabled_until = _reaction_disabled_until.get(chat_id)
            if disabled_until and now < disabled_until:
                logger.debug(
                    "set_message_reaction suppressed for chat %s until %s",
                    chat_id,
                    disabled_until,
                )
                return {"ok": False, "error": "reactions_suppressed"}
        except Exception:
            pass
        # Normalize reaction payload into Telegram's ReactionType objects.
        # ReactionType for emoji should be: {"type": "emoji", "emoji": "🍌"}
        payload_reaction = []

        def _ensure_type(obj: dict) -> dict:
            if "type" not in obj:
                if "emoji" in obj:
                    obj["type"] = "emoji"
                else:
                    obj["type"] = "emoji"
            return obj

        if isinstance(reaction, str):
            payload_reaction = [{"type": "emoji", "emoji": reaction}]
        elif isinstance(reaction, dict):
            payload_reaction = [_ensure_type(reaction)]
        elif isinstance(reaction, (list, tuple)):
            for r in reaction:
                if isinstance(r, str):
                    payload_reaction.append({"type": "emoji", "emoji": r})
                elif isinstance(r, dict):
                    payload_reaction.append(_ensure_type(r))
                else:
                    payload_reaction.append({"type": "emoji", "emoji": str(r)})
        else:
            payload_reaction = [{"type": "emoji", "emoji": str(reaction)}]

        # Use aiogram only; do not perform raw HTTP fallbacks. If aiogram raises
        # or returns an error, record and suppress further attempts for this chat.
        bot = telegram_client.get_bot(token)

        # aiogram expectations vary; convert payload conservatively for simple emoji
        def _to_aiogram_reaction(payload):
            """Normalize reaction payload to a list acceptable by aiogram.

            Always return a list. Elements can be emoji strings or ReactionType
            dicts (with a 'type' field). This avoids passing a bare string which
            some aiogram versions reject with a pydantic validation error.
            """
            out = []
            if payload is None:
                return out
            # If single string, wrap into list
            if isinstance(payload, str):
                return [payload]
            # If a dict, ensure it has a type and wrap
            if isinstance(payload, dict):
                if "type" not in payload and "emoji" in payload:
                    payload = {"type": "emoji", "emoji": payload.get("emoji")}
                return [payload]
            # If iterable/list-like, normalize each element
            if isinstance(payload, (list, tuple)):
                for p in payload:
                    if isinstance(p, str):
                        out.append(p)
                    elif isinstance(p, dict):
                        if "type" not in p and "emoji" in p:
                            out.append({"type": "emoji", "emoji": p.get("emoji")})
                        else:
                            out.append(p)
                    else:
                        out.append(str(p))
                return out
            # Fallback: stringify and wrap
            return [str(payload)]

        reaction_for_aiogram = _to_aiogram_reaction(payload_reaction)
        logger.debug("set_message_reaction payload (aiogram): %s", reaction_for_aiogram)
        try:
            # aiogram 3.28.2 does not support a `remove` kwarg on set_message_reaction.
            # To remove reactions, pass an empty list. To set reactions, pass the list.
            method = getattr(bot, "set_message_reaction")

            if remove:
                # Remove reactions by setting an empty list
                try:
                    res = await method(
                        chat_id=chat_id,
                        message_id=int(message_id),
                        reaction=[],
                    )
                except Exception as e:
                    # If setting empty list fails, try using DeleteMessageReaction method directly
                    try:
                        from aiogram.methods.delete_message_reaction import DeleteMessageReaction
                        delete_method = DeleteMessageReaction(
                            chat_id=chat_id,
                            message_id=int(message_id),
                        )
                        res = await bot(delete_method)
                    except Exception:
                        # Fallback: re-raise original exception
                        raise e
            else:
                # Set reactions normally
                res = await method(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    reaction=reaction_for_aiogram,
                )
        except Exception as e:
            # Log and disable reactions for this chat for a while to avoid spam
            logger.exception("aiogram set_message_reaction failed: %s", e)
            try:
                errtxt = str(e)
                if errtxt:
                    if (
                        "REACTION_INVALID" in errtxt.upper()
                        or "invalid reaction" in errtxt.lower()
                        or "bad request" in errtxt.lower()
                    ):
                        import time

                        _reaction_disabled_until[chat_id] = time.time() + int(
                            _reaction_disable_seconds or 300
                        )
                        logger.info(
                            "Disabling reactions for chat %s for %s seconds due to aiogram error: %s",
                            chat_id,
                            _reaction_disable_seconds,
                            errtxt,
                        )
            except Exception:
                pass
            return {"ok": False, "error": str(e)}

        # If aiogram returned a dict-like error payload, inspect for invalid reaction
        try:
            if isinstance(res, dict) and not res.get("ok"):
                try:
                    desc = res.get("description") or ""
                    if isinstance(desc, str) and "REACTION_INVALID" in desc.upper():
                        import time

                        _reaction_disabled_until[chat_id] = time.time() + int(
                            _reaction_disable_seconds or 300
                        )
                        logger.info(
                            "Disabling reactions for chat %s for %s seconds due to REACTION_INVALID (aiogram response)",
                            chat_id,
                            _reaction_disable_seconds,
                        )
                except Exception:
                    pass
                logger.warning("set_message_reaction(aiogram) returned non-ok: %s", res)
        except Exception:
            pass
        return res
    except Exception as e:
        logger.exception("set_message_reaction failed: %s", e)
        return {"ok": False, "error": str(e)}
