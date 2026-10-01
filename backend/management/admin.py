import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from . import runtime
from .auth import require_staff

log = logging.getLogger("conan.management")
router = APIRouter()


@router.get("/api/v1/admin/{guild_id}/status")
async def admin_status(
    guild_id: str, _: None = Depends(require_staff)
) -> dict[str, Any]:
    config = await runtime.store.get_config(guild_id)
    stats = await runtime.store.session_stats(guild_id)
    status = "not_configured"
    if runtime.settings.discord_token:
        if runtime.discord_bot and runtime.discord_bot.is_ready():
            status = "online"
        elif runtime.discord_bot and runtime.discord_bot.is_closed():
            status = "closed"
        elif runtime.bot_task and (not runtime.bot_task.done()):
            status = "starting"
        else:
            status = "stopped"
    return {
        "guildId": guild_id,
        "botStatus": status,
        "aiEnabled": bool(config.get("ai", {}).get("enabled", True)),
        "adminRoleId": str(
            config.get("admin", {}).get("roleId") or runtime.settings.staff_role_id
        ),
        "memory": stats,
        "presenceRotation": runtime.discord_bot.presence_rotation_payload()
        if runtime.discord_bot
        else {"active": False, "currentIndex": 0, "current": {}},
    }


@router.post("/api/v1/admin/{guild_id}/bot/{action}")
async def bot_action(
    guild_id: str, action: str, _: None = Depends(require_staff)
) -> dict[str, Any]:
    try:
        if action == "start":
            return await runtime.start_discord_bot("dashboard", guild_id)
        if action == "restart":
            return await runtime.restart_discord_bot("dashboard", guild_id)
        if action == "shutdown":
            return await runtime.stop_discord_bot("dashboard", guild_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="Unknown bot action")


@router.post("/api/v1/admin/{guild_id}/memory/clear")
async def clear_memory(
    guild_id: str, request: Request, _: None = Depends(require_staff)
) -> dict[str, Any]:
    payload = await request.json()
    all_channels = bool(payload.get("allChannels"))
    channel_id = str(payload.get("channelId") or "").strip()
    if all_channels:
        cleared = await runtime.store.clear_guild_sessions(guild_id)
        await runtime.store.add_log(
            guild_id, "memory.cleared_all", {"channels": cleared, "source": "dashboard"}
        )
        return {"ok": True, "scope": "all", "clearedChannels": cleared}
    if not channel_id:
        config = await runtime.store.get_config(guild_id)
        channel_id = str(
            config.get("ai", {}).get("channelId")
            or runtime.settings.ai_channel_id
            or ""
        )
    if not channel_id:
        raise HTTPException(
            status_code=400, detail="Select a channel before clearing memory"
        )
    if runtime.discord_bot:
        await runtime.discord_bot.clear_ai_session(guild_id, channel_id)
    else:
        await runtime.store.clear_session(guild_id, channel_id)
    await runtime.store.add_log(
        guild_id, "memory.cleared", {"channelId": channel_id, "source": "dashboard"}
    )
    return {"ok": True, "scope": "channel", "channelId": channel_id}


@router.post("/api/v1/admin/{guild_id}/ai/{action}")
async def ai_action(
    guild_id: str, action: str, _: None = Depends(require_staff)
) -> dict[str, Any]:
    if action not in {"pause", "resume"}:
        raise HTTPException(status_code=404, detail="Unknown AI action")
    config = await runtime.store.get_config(guild_id)
    config.setdefault("ai", {})["enabled"] = action == "resume"
    saved = await runtime.store.set_config(guild_id, config)
    await runtime.store.add_log(
        guild_id,
        f"ai.{action}d" if action == "pause" else "ai.resumed",
        {"source": "dashboard"},
    )
    return {"ok": True, "aiEnabled": saved["ai"]["enabled"]}
