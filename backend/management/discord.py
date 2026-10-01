import logging
from typing import Any

import discord
from fastapi import APIRouter, Depends, HTTPException

from ..bot import command_catalog
from . import runtime
from .auth import require_staff

log = logging.getLogger("conan.management")
router = APIRouter()


@router.get("/api/v1/discord/{guild_id}/channels")
async def get_channels(
    guild_id: str, _: None = Depends(require_staff)
) -> dict[str, Any]:
    if not runtime.discord_bot or not runtime.discord_bot.is_ready():
        return {
            "guildId": guild_id,
            "channels": [],
            "categories": [],
            "botReady": False,
        }
    guild = runtime.discord_bot.get_guild(int(guild_id))
    source = "cache"
    if not guild:
        try:
            guild = await runtime.discord_bot.fetch_guild(int(guild_id))
            source = "rest"
        except discord.Forbidden:
            return {
                "guildId": guild_id,
                "channels": [],
                "categories": [],
                "botReady": True,
                "source": "forbidden",
                "error": "Missing access to this guild. Reinvite the bot with bot + applications.commands scopes.",
                "inviteUrl": runtime.invite_url(guild_id),
            }
        except discord.HTTPException as exc:
            return {
                "guildId": guild_id,
                "channels": [],
                "categories": [],
                "botReady": True,
                "source": "error",
                "error": str(exc),
            }
    try:
        raw_channels = list(getattr(guild, "channels", []) or [])
        if hasattr(guild, "fetch_channels"):
            raw_channels = await guild.fetch_channels()
            source = "rest"
    except discord.Forbidden:
        raw_channels = list(getattr(guild, "channels", []) or [])
        source = "cache_forbidden"
    except discord.HTTPException:
        raw_channels = list(getattr(guild, "channels", []) or [])
    categories = sorted(
        [
            {"id": str(channel.id), "name": channel.name}
            for channel in raw_channels
            if isinstance(channel, discord.CategoryChannel)
        ],
        key=lambda item: item["name"].lower(),
    )
    channels = sorted(
        [
            {
                "id": str(channel.id),
                "name": channel.name,
                "mention": getattr(channel, "mention", f"<#{channel.id}>"),
                "categoryId": str(channel.category_id)
                if getattr(channel, "category_id", None)
                else "",
                "type": str(channel.type),
            }
            for channel in raw_channels
            if isinstance(channel, discord.TextChannel)
        ],
        key=lambda item: item["name"].lower(),
    )
    if runtime.settings.allowed_category_id and (
        not any(
            (item["id"] == runtime.settings.allowed_category_id for item in categories)
        )
    ):
        categories.insert(
            0,
            {"id": runtime.settings.allowed_category_id, "name": "Configured category"},
        )
    if runtime.settings.ai_channel_id and (
        not any((item["id"] == runtime.settings.ai_channel_id for item in channels))
    ):
        channels.insert(
            0,
            {
                "id": runtime.settings.ai_channel_id,
                "name": "configured-ai-channel",
                "mention": f"<#{runtime.settings.ai_channel_id}>",
                "categoryId": "",
                "type": "text",
            },
        )
    return {
        "guildId": guild_id,
        "channels": channels,
        "categories": categories,
        "botReady": True,
        "source": source,
        "inviteUrl": runtime.invite_url(guild_id),
    }


@router.get("/api/v1/discord/{guild_id}/commands")
async def get_command_setup(
    guild_id: str, _: None = Depends(require_staff)
) -> dict[str, Any]:
    config = await runtime.store.get_config(guild_id)
    registered = set(
        runtime.discord_bot.registered_command_names if runtime.discord_bot else []
    )
    rows = []
    for item in command_catalog(config):
        row = dict(item)
        row["registered"] = item["key"] in registered
        rows.append(row)
    return {
        "guildId": guild_id,
        "commands": rows,
        "syncStatus": runtime.discord_bot.command_sync_status
        if runtime.discord_bot
        else "offline",
        "syncError": runtime.discord_bot.command_sync_error
        if runtime.discord_bot
        else None,
    }


@router.post("/api/v1/discord/{guild_id}/sync-commands")
async def sync_commands(
    guild_id: str, _: None = Depends(require_staff)
) -> dict[str, Any]:
    if not runtime.discord_bot:
        raise HTTPException(status_code=503, detail="Discord bot is not configured")
    try:
        registered = await runtime.discord_bot.rebuild_application_commands(
            guild_id, sync=True
        )
    except discord.Forbidden as exc:
        runtime.discord_bot.command_sync_status = "forbidden"
        runtime.discord_bot.command_sync_error = f"Forbidden: {exc}"
        await runtime.store.add_log(
            guild_id,
            "commands.sync_forbidden",
            {
                "error": runtime.discord_bot.command_sync_error,
                "inviteUrl": runtime.invite_url(guild_id),
            },
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Missing access. Reinvite the bot with bot + applications.commands scopes.",
                "error": runtime.discord_bot.command_sync_error,
                "inviteUrl": runtime.invite_url(guild_id),
            },
        )
    except Exception as exc:
        runtime.discord_bot.command_sync_status = "failed"
        runtime.discord_bot.command_sync_error = f"{type(exc).__name__}: {exc}"
        await runtime.store.add_log(
            guild_id,
            "commands.sync_failed",
            {"error": runtime.discord_bot.command_sync_error},
        )
        raise HTTPException(
            status_code=502, detail=runtime.discord_bot.command_sync_error
        )
    runtime.discord_bot.command_sync_status = "ok"
    runtime.discord_bot.command_sync_error = None
    await runtime.store.add_log(
        guild_id, "commands.synced", {"source": "dashboard", "registered": registered}
    )
    return {"ok": True, "registered": registered, "count": len(registered)}
