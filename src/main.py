import asyncio
import logging
from aiohttp import web

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from .link_utils import find_links
from .worker import WorkerPool
from . import config, telegram_api

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
        message_id = message.get('message_id')
        for url in links:
            request.app["worker"].enqueue(chat_id, url, original_message_id=message_id)

    return web.Response(text="ok")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def metrics(request: web.Request) -> web.Response:
    data = generate_latest()
    return web.Response(body=data, headers={"Content-Type": CONTENT_TYPE_LATEST})


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


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post('/webhook/{token}', handle_webhook)
    app.router.add_get('/health', health)
    app.router.add_get('/metrics', metrics)
    app['worker'] = WorkerPool(config.BOT_TOKEN, workers=config.WORKERS)
    app.on_startup.append(_on_startup)
    return app


if __name__ == '__main__':
    app = create_app()
    web.run_app(app, host=config.HOST, port=config.PORT)
