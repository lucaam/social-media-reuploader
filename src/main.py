import logging
import time

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import __version__, config, http_client, telegram_api, telegram_client
from .link_utils import find_links
from .worker import WorkerPool

logger = logging.getLogger("telegram_downloader")
logging.basicConfig(level=logging.INFO)


async def handle_webhook(request: web.Request) -> web.Response:
    token = request.match_info.get("token")
    if token != config.BOT_TOKEN:
        return web.Response(status=403, text="forbidden")
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    message = body.get("message") or body.get("edited_message")
    if not message:
        return web.Response(text="no message")

    text = message.get("text") or message.get("caption") or ""
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    links = find_links(text)

    if links and chat_id:
        message_id = message.get("message_id")
        for url in links:
            try:
                request.app["worker"].enqueue(chat_id, url, original_message_id=message_id)
            except Exception:
                # enqueue failure: rely on worker to persist and notify (throttled)
                pass

    return web.Response(text="ok")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def metrics(request: web.Request) -> web.Response:
    data = generate_latest()
    return web.Response(body=data, headers={"Content-Type": CONTENT_TYPE_LATEST})


async def silly(request: web.Request) -> web.Response:
    """A tiny 'silly' feature endpoint for fun and quick smoke tests.

    Returns a short message including the package version.
    """
    try:
        return web.Response(text=f"Silly feature: 🦄 — version {__version__}")
    except Exception:
        return web.Response(text="Silly feature: 🦄")


async def _on_startup(app: web.Application):
    # Optionally register webhook with Telegram
    if config.MODE == "webhook" and config.WEBHOOK_URL and config.BOT_TOKEN:
        if "{token}" in config.WEBHOOK_URL:
            webhook = config.WEBHOOK_URL.replace("{token}", config.BOT_TOKEN)
        elif config.BOT_TOKEN in config.WEBHOOK_URL:
            webhook = config.WEBHOOK_URL
        else:
            webhook = config.WEBHOOK_URL.rstrip("/") + f"/webhook/{config.BOT_TOKEN}"
        logger.info("Setting webhook to %s", webhook)
        try:
            res = await telegram_api.set_webhook(config.BOT_TOKEN, webhook)
            logger.info("setWebhook result: %s", res)
        except Exception:
            logger.exception("Failed to set webhook")


async def _print_startup_banner(app: web.Application):
    try:
        logger.info(
            "Social Media Reuploader v%s starting — mode=%s host=%s port=%s workers=%s",
            __version__,
            config.MODE,
            config.HOST,
            config.PORT,
            config.WORKERS,
        )
    except Exception:
        logger.info("Social Media Reuploader starting")


async def _on_cleanup(app: web.Application):
    # ensure worker tasks are shutdown before closing HTTP/bot clients
    try:
        worker = app.get("worker")
        if worker:
            try:
                await worker.shutdown()
            except Exception:
                logger.debug("worker.shutdown() failed during cleanup")
    except Exception:
        logger.debug("Error while attempting worker shutdown")
    try:
        await http_client.close_session()
    except Exception:
        logger.debug("http_client.close_session failed during cleanup")
    try:
        await telegram_client.close_all_bots()
    except Exception:
        logger.debug("telegram_client.close_all_bots failed during cleanup")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook/{token}", handle_webhook)
    app.router.add_get("/health", health)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/silly", silly)
    app["worker"] = WorkerPool(config.BOT_TOKEN, workers=config.WORKERS)
    # print a short banner on startup and register webhook if needed
    app.on_startup.append(_print_startup_banner)
    app.on_startup.append(_on_startup)
    # ensure worker tasks/shutdown happens before closing shared resources
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host=config.HOST, port=config.PORT)
