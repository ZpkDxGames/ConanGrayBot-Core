"""Single-process lifecycle owner. Deploy exactly one worker."""

import asyncio
import logging
from typing import Any
from urllib.parse import urlencode

from ..bot import ConanBot
from ..config import get_settings
from ..firebase_client import create_store
from ..google_drive import create_drive_archive

settings = get_settings()
store = create_store()
drive_archive = create_drive_archive()
discord_bot: ConanBot | None = None
bot_task: asyncio.Task[Any] | None = None
bot_lifecycle_lock = asyncio.Lock()
log = logging.getLogger("conan.runtime")


def invite_url(guild_id: str | None = None) -> str:
    query = {
        "client_id": settings.discord_application_id,
        "permissions": "379968",
        "scope": "bot applications.commands",
    }
    if guild_id:
        query["guild_id"] = guild_id
        query["disable_guild_select"] = "true"
    return "https://discord.com/oauth2/authorize?" + urlencode(query)


def _new_discord_bot() -> ConanBot:
    return ConanBot(
        store, control_callback=handle_bot_control, drive_archive=drive_archive
    )


async def _stop_discord_bot_locked() -> None:
    global bot_task
    current_bot = discord_bot
    current_task = bot_task
    if current_bot and not current_bot.is_closed():
        await current_bot.close()
    if current_task and not current_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(current_task), timeout=5)
        except asyncio.TimeoutError:
            current_task.cancel()
            try:
                await current_task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
    bot_task = None


async def _start_discord_bot_locked() -> ConanBot:
    global discord_bot, bot_task
    if not settings.discord_token:
        raise RuntimeError("Discord bot token is not configured")
    if discord_bot and bot_task and not bot_task.done() and not discord_bot.is_closed():
        return discord_bot
    if discord_bot and not discord_bot.is_closed():
        await discord_bot.close()
    discord_bot = _new_discord_bot()
    bot_task = asyncio.create_task(
        discord_bot.start(settings.discord_token), name="discord-bot"
    )
    bot_task.add_done_callback(_log_bot_task_result)
    return discord_bot


async def start_discord_bot(
    source: str = "dashboard", guild_id: str | None = None, actor_id: str | None = None
) -> dict[str, Any]:
    async with bot_lifecycle_lock:
        bot = await _start_discord_bot_locked()
    if guild_id:
        await store.add_log(
            guild_id, "bot.started", {"source": source, "actorId": actor_id or ""}
        )
    return {
        "ok": True,
        "action": "start",
        "status": "starting",
        "user": str(bot.user) if bot.user else None,
    }


async def stop_discord_bot(
    source: str = "dashboard", guild_id: str | None = None, actor_id: str | None = None
) -> dict[str, Any]:
    async with bot_lifecycle_lock:
        await _stop_discord_bot_locked()
    if guild_id:
        await store.add_log(
            guild_id, "bot.shutdown", {"source": source, "actorId": actor_id or ""}
        )
    return {"ok": True, "action": "shutdown", "status": "closed"}


async def restart_discord_bot(
    source: str = "dashboard", guild_id: str | None = None, actor_id: str | None = None
) -> dict[str, Any]:
    async with bot_lifecycle_lock:
        await _stop_discord_bot_locked()
        bot = await _start_discord_bot_locked()
    if guild_id:
        await store.add_log(
            guild_id, "bot.restarted", {"source": source, "actorId": actor_id or ""}
        )
    return {
        "ok": True,
        "action": "restart",
        "status": "starting",
        "user": str(bot.user) if bot.user else None,
    }


async def handle_bot_control(
    action: str, guild_id: str, actor_id: str
) -> dict[str, Any]:
    if action == "restart":
        return await restart_discord_bot("discord", guild_id, actor_id)
    if action == "shutdown":
        return await stop_discord_bot("discord", guild_id, actor_id)
    if action == "start":
        return await start_discord_bot("discord", guild_id, actor_id)
    raise ValueError(f"Unknown bot control action: {action}")


def _log_bot_task_result(task: asyncio.Task[Any]) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        log.info("Discord bot task cancelled.")
        return
    if exc:
        log.error(
            "Discord bot task crashed.", exc_info=(type(exc), exc, exc.__traceback__)
        )
    else:
        log.warning("Discord bot task stopped without an exception.")
