"""One bounded HTTP pool per event loop, explicitly closed at runtime shutdown."""

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

import aiohttp

_sessions: WeakKeyDictionary[asyncio.AbstractEventLoop, aiohttp.ClientSession] = (
    WeakKeyDictionary()
)


@asynccontextmanager
async def pooled_session():
    loop = asyncio.get_running_loop()
    session = _sessions.get(loop)
    if session is None or session.closed:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=42, connect=5, sock_read=30),
            connector=aiohttp.TCPConnector(limit=32, limit_per_host=12),
        )
        _sessions[loop] = session
    yield session


async def close_sessions() -> None:
    session = _sessions.pop(asyncio.get_running_loop(), None)
    if session and not session.closed:
        await session.close()
