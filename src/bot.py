import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher
from aiogram.types import ChatMemberUpdated, Message, Update
from aiohttp import web

from . import __version__, config, db, http_client, telegram_api, telegram_client
from .link_utils import find_links, is_supported
from .worker import WorkerPool

TELEGRAM_URL_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/", re.IGNORECASE)

logger = logging.getLogger(__name__)


async def handle_message(message: Message):
    try:
        # Log a detailed dump for debugging (entities, caption, media)
        try:
            dump = (
                message.model_dump()
                if hasattr(message, "model_dump")
                else repr(message)
            )
            logger.debug("Message dump: %s", dump)
        except Exception:
            logger.debug("Could not dump message object")

        # Extract text from common fields
        text = ""
        if getattr(message, "text", None):
            text = message.text or ""
        elif getattr(message, "caption", None):
            text = message.caption or ""

        chat = message.chat
        chat_id = chat.id if chat else None
        message_id = getattr(message, "message_id", None)
        # deduplicate: skip processing if we've already handled this message
        try:
            if chat_id and message_id and db.is_message_processed(chat_id, message_id):
                logger.info(
                    "Skipping already-processed message chat=%s id=%s",
                    chat_id,
                    message_id,
                )
                return
            if chat_id and message_id:
                db.mark_message_processed(chat_id, message_id)
        except Exception:
            # don't fail if DB dedup check fails
            pass

        logger.info(
            "Incoming message: chat=%s type=%s from=%s text=%s",
            getattr(chat, "id", None),
            getattr(chat, "type", None),
            getattr(getattr(message, "from_user", None), "id", None),
            (text or "")[:200],
        )

        # extract links from text and from entities (text_link, url)
        links = []
        if text:
            links.extend(find_links(text))
        try:
            entities = getattr(message, "entities", None) or getattr(
                message, "caption_entities", None
            )
            if entities:
                for ent in entities:
                    t = getattr(ent, "type", None)
                    if t == "text_link":
                        url = getattr(ent, "url", None)
                        if url:
                            links.append(url)
                    elif t == "url":
                        # extract substring from message text
                        try:
                            off = int(getattr(ent, "offset", 0))
                            ln = int(getattr(ent, "length", 0))
                            src = text or ""
                            url = src[off : off + ln]
                            if url:
                                links.append(url)
                        except Exception:
                            pass
        except Exception:
            logger.debug("Could not parse message entities")

        # deduplicate links preserving order
        if links:
            seen = set()
            deduped = []
            for link in links:
                if link not in seen:
                    seen.add(link)
                    deduped.append(link)
            links = deduped

        # also detect any other http(s) URLs so we can handle unsupported sites
        url_pattern = re.compile(r"https?://[^\s)\]\>]+", re.IGNORECASE)
        all_urls = []
        if text:
            for m in url_pattern.finditer(text):
                all_urls.append(m.group(0))
        if all_urls:
            seen2 = set()
            deduped2 = []
            for u in all_urls:
                if u not in seen2:
                    seen2.add(u)
                    deduped2.append(u)
            all_urls = deduped2

        if (links or all_urls) and chat_id:
            # persist the update as well (defensive)
            try:
                db.add_update(str({"chat_id": chat_id, "links": links or all_urls}))
            except Exception:
                logger.debug("Could not persist link update")

            # build a short description by removing urls from message text
            description = ""
            try:
                description = text or ""
                for link in links or all_urls:
                    description = description.replace(link, "")
                description = description.strip()
            except Exception:
                description = ""

            # pass chat type so worker can decide whether to reply on errors
            ctype = getattr(chat, "type", "private")

            # handle supported links (enqueue)
            for url in links:
                try:
                    # ignore internal Telegram links (t.me) for enqueue (not processed)
                    if TELEGRAM_URL_RE.search(url):
                        logger.info("Ignoring telegram internal link: %s", url)
                        continue
                    app_worker.enqueue(
                        chat_id,
                        url,
                        description=description,
                        original_message_id=message_id,
                        chat_type=ctype,
                    )
                except Exception:
                    logger.exception(
                        "Failed to enqueue link %s for chat=%s", url, chat_id
                    )

            # handle unsupported urls: ignore in groups, notify in private
            supported_sites = "YouTube, TikTok, Instagram, Facebook"
            for url in all_urls:
                try:
                    # skip urls that are supported/handled already
                    if url in links:
                        continue
                    if is_supported(url):
                        continue
                    # internal telegram links: ignore in groups, notify in private
                    if TELEGRAM_URL_RE.search(url):
                        if ctype and ctype.lower() in (
                            "group",
                            "supergroup",
                            "channel",
                        ):
                            logger.info(
                                "Ignoring telegram internal URL (no action): %s", url
                            )
                            continue
                        try:
                            if (
                                app_worker
                                and getattr(app_worker, "token", None)
                                and message_id
                            ):
                                await telegram_api.set_message_reaction(
                                    app_worker.token, chat_id, message_id, "👎"
                                )
                        except Exception:
                            logger.debug(
                                "Could not add 👎 reaction for telegram internal link"
                            )
                        try:
                            await telegram_api.send_message(
                                app_worker.token,
                                chat_id,
                                f"Link non valido. Supporto solo i seguenti siti: {supported_sites}",
                                reply_to_message_id=message_id,
                            )
                        except Exception:
                            logger.debug("Could not send unsupported link notice")
                        continue

                    # general unsupported: ignore in groups, notify in private
                    if ctype and ctype.lower() in ("group", "supergroup", "channel"):
                        continue
                    try:
                        if (
                            app_worker
                            and getattr(app_worker, "token", None)
                            and message_id
                        ):
                            await telegram_api.set_message_reaction(
                                app_worker.token, chat_id, message_id, "👎"
                            )
                    except Exception:
                        logger.debug("Could not add 👎 reaction for unsupported link")
                    try:
                        await telegram_api.send_message(
                            app_worker.token,
                            chat_id,
                            f"Link non valido. Supporto solo i seguenti siti: {supported_sites}",
                            reply_to_message_id=message_id,
                        )
                    except Exception:
                        logger.debug("Could not send unsupported link notice")
                except Exception:
                    logger.exception("Error handling unsupported url %s", url)
    except Exception:
        logger.exception("Error handling incoming message")


async def log_update(update: Update):
    try:
        # pydantic model -> json
        j = update.json()
    except Exception:
        j = repr(update)
    logger.info("Raw update received: %s", j)
    try:
        # ensure DB exists and persist update for remote debugging
        db.init_db()
        db.add_update(j)
    except Exception:
        logger.debug("Could not persist update to DB")


async def main():
    # allow overriding log level with env LOG_LEVEL for diagnostics
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level_val = getattr(logging, log_level, logging.INFO)

    # configure logging with rotating file handler for persistent debug logs
    log_dir = os.path.join(os.getcwd(), "data")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        pass
    log_file = os.environ.get(
        "LOG_FILE", os.path.join(log_dir, "telegram_downloader.log")
    )

    # Configure root logger so all module loggers propagate here
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level_val)
    # console handler
    ch = logging.StreamHandler()
    ch.setLevel(log_level_val)
    ch.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger.addHandler(ch)

    # rotating file handler (only if enabled via LOG_TO_FILE)
    try:
        log_to_file = getattr(config, "LOG_TO_FILE", False)
        if log_to_file:
            from logging.handlers import RotatingFileHandler

            fh = RotatingFileHandler(
                log_file,
                maxBytes=int(os.environ.get("LOG_MAX_BYTES", 5 * 1024 * 1024)),
                backupCount=int(os.environ.get("LOG_BACKUP_COUNT", 5)),
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            root_logger.addHandler(fh)
        else:
            root_logger.debug(
                "File logging disabled by LOG_TO_FILE (logging to stdout only)"
            )
    except Exception:
        root_logger.debug("Could not create RotatingFileHandler")
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in config")

    # Print a concise startup banner and a short runtime configuration
    try:
        root_logger.info("Social Media Reuploader v%s starting", __version__)
        root_logger.info(
            "Mode=%s host=%s port=%s workers=%s tmp_dir=%s thumbnail=%s max_file_size=%s",
            getattr(config, "MODE", "polling"),
            getattr(config, "HOST", "0.0.0.0"),
            getattr(config, "PORT", "8080"),
            getattr(config, "WORKERS", 2),
            getattr(config, "TMP_DIR", "/tmp/telegram_downloader"),
            getattr(config, "WORKER_GENERATE_THUMBNAIL", True),
            getattr(config, "TELEGRAM_MAX_FILE_SIZE", 50 * 1024 * 1024),
        )
        # Helpful operator hint when running the image in Kubernetes: the
        # container's default command runs the polling bot (python -m src.bot).
        # To use webhook mode you must either run `python -m src.main` in the
        # container or set a command override in your Deployment/Helm values.
        if getattr(config, "MODE", "polling") == "webhook":
            if not getattr(config, "WEBHOOK_URL", None):
                root_logger.warning(
                    "MODE=webhook but WEBHOOK_URL is not set; container image default runs polling unless you override the command/entrypoint"
                )
    except Exception:
        root_logger.info("Social Media Reuploader starting")

    bot = Bot(token=config.BOT_TOKEN)
    # register bot instance for reuse by helper APIs
    telegram_client.set_bot(bot)
    dp = Dispatcher()

    # create a WorkerPool instance that handlers will use
    global app_worker
    app_worker = WorkerPool(config.BOT_TOKEN, workers=config.WORKERS)

    # Provide a lightweight HTTP health endpoint so Kubernetes
    # liveness/readiness probes don't kill the process while
    # long-running downloads/transcodes are in progress.
    # Start the health endpoint unless we're running in webhook
    # mode with a configured `WEBHOOK_URL` (in that case another
    # HTTP server will be serving webhooks/health).
    health_runner = None
    mode = getattr(config, "MODE", "polling")
    webhook_url = getattr(config, "WEBHOOK_URL", None)
    if not (mode == "webhook" and webhook_url):
        try:

            async def _health(request: web.Request):
                # Minimal response for probes to keep Kubernetes happy.
                # Only expose diagnostic details when HEALTH_DEBUG is enabled.
                try:
                    if not getattr(config, "HEALTH_DEBUG", False):
                        return web.Response(text="ok")
                except Exception:
                    return web.Response(text="ok")

                # Diagnostic payload (debug mode)
                try:
                    worker_tasks = 0
                    worker_running = 0
                    worker_slots_free = None
                    if globals().get("app_worker"):
                        try:
                            tasks = getattr(app_worker, "_tasks", set()) or set()
                            worker_tasks = len(tasks)
                            worker_running = sum(
                                1
                                for t in tasks
                                if not getattr(t, "done", lambda: True)()
                            )
                        except Exception:
                            worker_tasks = len(getattr(app_worker, "_tasks", set()))
                        try:
                            worker_slots_free = getattr(
                                getattr(app_worker, "_sem", None), "_value", None
                            )
                        except Exception:
                            worker_slots_free = None
                    db_ok = False
                    try:
                        db.init_db()
                        db_ok = True
                    except Exception:
                        db_ok = False
                    payload = {
                        "ok": True,
                        "mode": getattr(config, "MODE", "polling"),
                        "worker_tasks": worker_tasks,
                        "worker_running": worker_running,
                        "worker_slots_free": worker_slots_free,
                        "db_ok": db_ok,
                    }
                    return web.json_response(payload)
                except Exception:
                    return web.Response(text="ok")

            health_app = web.Application()
            health_app.router.add_get("/health", _health)
            health_runner = web.AppRunner(health_app)
            await health_runner.setup()
            site = web.TCPSite(health_runner, host=config.HOST, port=config.PORT)
            await site.start()
            logger.info("Health endpoint listening on %s:%s", config.HOST, config.PORT)
        except Exception:
            logger.exception("Failed to start health endpoint")

    # ensure DB exists for update storage
    try:
        db.init_db()
    except Exception:
        logger.debug("db.init_db() failed in bot startup")

    # register diagnostic logger for all updates
    dp.update.register(log_update)

    # register handlers for different update types
    dp.message.register(handle_message)
    try:
        dp.channel_post.register(handle_message)
    except Exception:
        # some aiogram builds may not have channel_post convenience; ignore
        pass
    try:
        dp.edited_message.register(handle_message)
    except Exception:
        pass

    # send a short welcome message when the bot is added to a group
    async def _handle_my_chat_member(update: ChatMemberUpdated):
        try:
            old = getattr(update, "old_chat_member", None)
            new = getattr(update, "new_chat_member", None)
            old_status = getattr(old, "status", None)
            new_status = getattr(new, "status", None)
            # if the bot transitioned from left/kicked -> member/administrator, greet
            if new_status in ("member", "administrator") and old_status in (
                "left",
                "kicked",
                "restricted",
                None,
            ):
                chat = getattr(update, "chat", None)
                chat_id = getattr(chat, "id", None)
                if chat_id:
                    text = (
                        "Ciao! 👋 Sono il bot che prova a scaricare brevi video da YouTube, TikTok, Instagram e Facebook e a condividerli qui. "
                        "In questo gruppo rispondo solo quando qualcuno incolla un link. "
                        "Nota: non posso scaricare contenuti che richiedono autenticazione."
                    )
                    try:
                        await telegram_api.send_message(config.BOT_TOKEN, chat_id, text)
                    except Exception:
                        logger.debug("Could not send group welcome message")
        except Exception:
            logger.exception("Error in my_chat_member handler")

    try:
        dp.my_chat_member.register(_handle_my_chat_member)
    except Exception:
        # some aiogram versions may not expose my_chat_member convenience
        pass

    # Start long-polling and ensure graceful shutdown on cancellation
    try:
        await dp.start_polling(bot)
    finally:
        try:
            await dp.stop_polling()
        except Exception:
            pass
        # ensure workers finish before closing bots and http sessions
        try:
            if app_worker:
                await app_worker.shutdown()
        except Exception:
            logger.debug("app_worker.shutdown() failed during bot shutdown")
        try:
            # close cached bot(s) and shared http session
            await telegram_client.close_all_bots()
        except Exception:
            pass
        try:
            await http_client.close_session()
        except Exception:
            pass
        try:
            if health_runner:
                await health_runner.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
