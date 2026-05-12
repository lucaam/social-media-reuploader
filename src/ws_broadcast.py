import asyncio
import logging
from typing import Set, Optional

logger = logging.getLogger(__name__)

# set by app startup
loop: Optional[asyncio.AbstractEventLoop] = None

_connections: Set[asyncio.Queue] = set()

async def register_queue(q: asyncio.Queue):
    _connections.add(q)

async def unregister_queue(q: asyncio.Queue):
    try:
        _connections.remove(q)
    except KeyError:
        pass

async def broadcast(message: dict):
    if not _connections:
        return
    payload = message
    dead = []
    for q in list(_connections):
        try:
            # put_nowait so a slow client doesn't block
            q.put_nowait(payload)
        except Exception:
            logger.exception("Failed to put message into client queue")
            dead.append(q)
    for q in dead:
        try:
            _connections.remove(q)
        except Exception:
            pass

# helper for sync callers
def publish_sync(message: dict):
    global loop
    if loop:
        try:
            asyncio.run_coroutine_threadsafe(broadcast(message), loop)
        except Exception:
            logger.exception("publish_sync failed")
 