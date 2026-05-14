import logging
import os
from typing import Optional, Sequence

import aiohttp

from . import http_client, telegram_client

logger = logging.getLogger(__name__)


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
):
    # choose sendVideo for mp4 files to enable inline playback, otherwise sendDocument
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    if ext == "mp4":
        return await send_video(
            token,
            chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
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

        # prefer HTTP API when `remove` is requested (some aiogram versions don't accept remove)
        if remove:
            try:
                session = await http_client.get_session()
                url = f"https://api.telegram.org/bot{token}/setMessageReaction"
                # API expects an Array of ReactionType objects; always send a list
                payload = {
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "reaction": payload_reaction,
                    "remove": True,
                }
                logger.debug("set_message_reaction HTTP payload: %s", payload)
                async with session.post(url, json=payload) as resp:
                    try:
                        result = await resp.json()
                    except Exception:
                        result = {"ok": False, "status": resp.status}
                logger.debug(
                    "set_message_reaction HTTP response: %s for payload: %s",
                    result,
                    payload,
                )
                if not (isinstance(result, dict) and result.get("ok")):
                    logger.warning(
                        "set_message_reaction HTTP API returned non-ok: %s", result
                    )
                return result
            except Exception:
                # fallback to aiogram
                try:
                    bot = telegram_client.get_bot(token)
                    # aiogram may expect a single ReactionType for simple emoji.
                    reaction_for_aiogram = (
                        payload_reaction[0]
                        if len(payload_reaction) == 1
                        else payload_reaction
                    )
                    try:
                        # aiogram expects a list of ReactionType objects; always pass a list
                        reaction_for_aiogram = payload_reaction
                        res = await bot.set_message_reaction(
                            chat_id=chat_id,
                            message_id=int(message_id),
                            reaction=reaction_for_aiogram,
                            remove=True,
                        )
                    except TypeError as te:
                        # aiogram version does not support remove kwarg
                        logger.exception(
                            "set_message_reaction(aiogram) does not support remove kwarg: %s",
                            te,
                        )
                        return {"ok": False, "error": str(te)}
                    logger.debug(
                        "set_message_reaction(aiogram remove) returned: %s, payload: %s",
                        res,
                        reaction_for_aiogram,
                    )
                    if isinstance(res, dict) and not res.get("ok"):
                        logger.warning(
                            "set_message_reaction(aiogram) returned non-ok: %s", res
                        )
                    return res
                except Exception as e:
                    logger.exception("set_message_reaction fallback failed: %s", e)
                    return {"ok": False, "error": str(e)}

        # no remove: use aiogram Bot (cached)
        bot = telegram_client.get_bot(token)

        # aiogram expectations vary between versions. For a single emoji
        # some versions expect a plain emoji string, while others accept
        # structured ReactionType objects. Convert conservatively:
        def _to_aiogram_reaction(payload):
            if not payload:
                return payload
            if isinstance(payload, list) and len(payload) == 1:
                single = payload[0]
                if isinstance(single, dict):
                    if single.get("type") == "emoji" and isinstance(
                        single.get("emoji"), str
                    ):
                        return single["emoji"]
                    return single
                return single
            if isinstance(payload, list):
                # If all entries are simple emoji dicts, convert to list of emoji strings
                emoji_list = []
                for p in payload:
                    if (
                        isinstance(p, dict)
                        and p.get("type") == "emoji"
                        and isinstance(p.get("emoji"), str)
                    ):
                        emoji_list.append(p.get("emoji"))
                    else:
                        return payload
                return emoji_list
            return payload

        reaction_for_aiogram = _to_aiogram_reaction(payload_reaction)
        logger.debug("set_message_reaction payload (aiogram): %s", reaction_for_aiogram)
        try:
            res = await bot.set_message_reaction(
                chat_id=chat_id,
                message_id=int(message_id),
                reaction=reaction_for_aiogram,
            )
        except Exception as e:
            # If aiogram/Telegram rejects the supplied form, try the raw HTTP API
            logger.debug(
                "aiogram set_message_reaction failed (%s), trying HTTP fallback", e
            )
            session = await http_client.get_session()
            url = f"https://api.telegram.org/bot{token}/setMessageReaction"
            payload = {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "reaction": payload_reaction,
                "remove": False,
            }
            logger.debug("set_message_reaction HTTP payload (fallback): %s", payload)
            async with session.post(url, json=payload) as resp:
                try:
                    result = await resp.json()
                except Exception:
                    result = {"ok": False, "status": resp.status}
            logger.debug(
                "set_message_reaction HTTP fallback response: %s for payload: %s",
                result,
                payload,
            )
            if isinstance(result, dict) and not result.get("ok"):
                logger.warning(
                    "set_message_reaction HTTP fallback returned non-ok: %s", result
                )
            return result
        logger.debug("set_message_reaction(aiogram) response: %s", res)
        if isinstance(res, dict) and not res.get("ok"):
            logger.warning("set_message_reaction(aiogram) returned non-ok: %s", res)
        return res
    except Exception as e:
        logger.exception("set_message_reaction failed: %s", e)
        return {"ok": False, "error": str(e)}
