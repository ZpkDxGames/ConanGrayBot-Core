from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import logging
import math
import mimetypes
from urllib.parse import quote, urlencode
from typing import Any

import discord
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .bot import ConanBot, command_catalog
from .config import get_settings
from .firebase_client import create_store
from .google_drive import DriveConfigurationError, create_drive_archive, normalize_drive_folder_id
from .media_stream import normalize_stream_filename, validate_drive_stream_signature

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("conan.api")

settings = get_settings()
store = create_store()
drive_archive = create_drive_archive()
discord_bot: ConanBot | None = None
bot_task: asyncio.Task[Any] | None = None
bot_lifecycle_lock = asyncio.Lock()


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

@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.discord_token:
        log.warning("Discord token missing; API will run without bot websocket.")
    else:
        await start_discord_bot(source="startup")
        log.info("Discord bot startup task created.")
    try:
        yield
    finally:
        async with bot_lifecycle_lock:
            await _stop_discord_bot_locked()


app = FastAPI(title="Conan Gray Bot API", version="1.0.0", lifespan=lifespan)
_cors_origins = settings.cors_origins
_allow_all_origins = "*" in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all_origins else _cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Dashboard-Key"],
    max_age=86400,
)

@app.middleware("http")
async def protect_api_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _normalize_dashboard_key(value: str | None) -> str:
    return (value or "").strip()


def _dashboard_key_id(value: str | None) -> str | None:
    normalized = _normalize_dashboard_key(value)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _dashboard_auth_info(received_key: str | None) -> dict[str, Any]:
    expected = _normalize_dashboard_key(settings.dashboard_key)
    received = _normalize_dashboard_key(received_key)
    configured = bool(expected)
    valid = configured and bool(received) and hmac.compare_digest(received, expected)
    return {
        "configured": configured,
        "headerReceived": bool(received),
        "valid": valid,
        "expectedKeyId": _dashboard_key_id(expected),
        "receivedKeyId": _dashboard_key_id(received),
    }


async def require_dashboard_key(x_dashboard_key: str | None = Header(default=None)) -> None:
    auth = _dashboard_auth_info(x_dashboard_key)
    if not auth["configured"]:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "dashboard_key_not_configured",
                "message": "The backend dashboard key is not configured.",
                **auth,
            },
        )
    if not auth["valid"]:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_dashboard_key",
                "message": "The dashboard key does not match this Discloud deployment.",
                **auth,
            },
        )


def _new_discord_bot() -> ConanBot:
    return ConanBot(store, control_callback=handle_bot_control, drive_archive=drive_archive)


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
    bot_task = asyncio.create_task(discord_bot.start(settings.discord_token), name="discord-bot")
    bot_task.add_done_callback(_log_bot_task_result)
    return discord_bot


async def start_discord_bot(source: str = "dashboard", guild_id: str | None = None, actor_id: str | None = None) -> dict[str, Any]:
    async with bot_lifecycle_lock:
        bot = await _start_discord_bot_locked()
    if guild_id:
        await store.add_log(guild_id, "bot.started", {"source": source, "actorId": actor_id or ""})
    return {"ok": True, "action": "start", "status": "starting", "user": str(bot.user) if bot.user else None}


async def stop_discord_bot(source: str = "dashboard", guild_id: str | None = None, actor_id: str | None = None) -> dict[str, Any]:
    async with bot_lifecycle_lock:
        await _stop_discord_bot_locked()
    if guild_id:
        await store.add_log(guild_id, "bot.shutdown", {"source": source, "actorId": actor_id or ""})
    return {"ok": True, "action": "shutdown", "status": "closed"}


async def restart_discord_bot(source: str = "dashboard", guild_id: str | None = None, actor_id: str | None = None) -> dict[str, Any]:
    async with bot_lifecycle_lock:
        await _stop_discord_bot_locked()
        bot = await _start_discord_bot_locked()
    if guild_id:
        await store.add_log(guild_id, "bot.restarted", {"source": source, "actorId": actor_id or ""})
    return {"ok": True, "action": "restart", "status": "starting", "user": str(bot.user) if bot.user else None}


async def handle_bot_control(action: str, guild_id: str, actor_id: str) -> dict[str, Any]:
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
        log.error("Discord bot task crashed.", exc_info=(type(exc), exc, exc.__traceback__))
    else:
        log.warning("Discord bot task stopped without an exception.")


@app.get("/", response_model=None)
async def api_index():
    return JSONResponse(
        {
            "ok": True,
            "name": settings.app_name,
            "service": "api",
            "dashboardHosting": "external",
            "dashboardProvider": "Vercel",
            "apiPrefix": "/api",
            "message": "API is online. The dashboard is deployed separately and connects through its Vercel /api proxy.",
        }
    )


@app.get("/favicon.ico", response_model=None)
async def favicon():
    # A 204 response must not contain a JSON body. Returning JSONResponse here
    # causes Uvicorn to reject the response because Content-Length is zero.
    return Response(status_code=204)


@app.api_route("/media/drive/{file_id}/{filename}", methods=["GET", "HEAD"], response_model=None)
async def stream_drive_media(file_id: str, filename: str, request: Request, sig: str = ""):
    """Proxy a private Drive file through a signed URL with byte-range support.

    Discord can render this endpoint as a native image/video preview even when
    the source file is larger than the guild's attachment limit.
    """
    normalized_name = normalize_stream_filename(filename)
    if not validate_drive_stream_signature(file_id, normalized_name, sig):
        raise HTTPException(status_code=403, detail="Invalid media stream signature")
    if not drive_archive.configured:
        raise HTTPException(status_code=503, detail="Google Drive is not configured")

    try:
        metadata = await drive_archive.get_file_metadata(file_id)
    except DriveConfigurationError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    actual_name = normalize_stream_filename(str(metadata.get("name") or normalized_name))
    mime_type = str(metadata.get("mimeType") or "").split(";", 1)[0].strip().lower()
    if not mime_type or mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(actual_name)[0] or "application/octet-stream"
    try:
        total_size = max(0, int(metadata.get("size") or 0))
    except (TypeError, ValueError):
        total_size = 0

    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(actual_name, safe='')}",
        "X-Content-Type-Options": "nosniff",
    }
    if request.method == "HEAD":
        if total_size:
            base_headers["Content-Length"] = str(total_size)
        return Response(status_code=200, media_type=mime_type, headers=base_headers)

    range_header = str(request.headers.get("range") or "").strip()
    try:
        session, drive_response = await drive_archive.open_file_stream(file_id, range_header=range_header)
    except DriveConfigurationError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    for source_name, target_name in (
        ("content-length", "Content-Length"),
        ("content-range", "Content-Range"),
        ("accept-ranges", "Accept-Ranges"),
        ("etag", "ETag"),
        ("last-modified", "Last-Modified"),
    ):
        value = drive_response.headers.get(source_name)
        if value:
            base_headers[target_name] = value

    def body_iterator():
        try:
            for chunk in drive_response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            drive_response.close()
            session.close()

    return StreamingResponse(
        body_iterator(),
        status_code=drive_response.status_code,
        media_type=mime_type,
        headers=base_headers,
    )


@app.get("/api/auth/check")
async def check_dashboard_auth(x_dashboard_key: str | None = Header(default=None)) -> dict[str, Any]:
    auth = _dashboard_auth_info(x_dashboard_key)
    return {
        "ok": True,
        **auth,
        "message": (
            "Dashboard key accepted."
            if auth["valid"]
            else "Dashboard key is missing or belongs to a different backend deployment."
        ),
    }


@app.get("/api/health")
async def health(x_dashboard_key: str | None = Header(default=None)) -> dict[str, Any]:
    privileged = bool(_dashboard_auth_info(x_dashboard_key)["valid"])
    bot_status = "not_configured"
    bot_error = None
    task_done = False

    if discord_bot:
        if bot_task is None:
            bot_status = "closed" if discord_bot.is_closed() else "stopped"
        elif bot_task.done():
            task_done = True
            try:
                exc = bot_task.exception()
            except asyncio.CancelledError:
                exc = None
                bot_status = "stopped"
            if exc:
                bot_status = "crashed"
                bot_error = f"{type(exc).__name__}: {exc}"
            elif discord_bot.is_ready():
                bot_status = "online"
            elif discord_bot.is_closed():
                bot_status = "closed"
            else:
                bot_status = "stopped"
        elif discord_bot.is_ready():
            bot_status = "online"
        elif discord_bot.user:
            bot_status = "connecting"
        else:
            bot_status = "starting"

    latency_ms = None
    if discord_bot and discord_bot.latency is not None:
        try:
            if math.isfinite(discord_bot.latency) and discord_bot.latency >= 0:
                latency_ms = round(discord_bot.latency * 1000)
        except (TypeError, OverflowError):
            latency_ms = None

    return {
        "ok": True,
        "name": settings.app_name,
        "environment": settings.environment,
        "dashboard": {
            "hosting": "external",
            "provider": "Vercel",
            "connectionMode": "same_origin_proxy",
            "apiPrefix": "/api",
        },
        "dashboardAuth": {
            "configured": bool(_normalize_dashboard_key(settings.dashboard_key)),
            "keyId": _dashboard_key_id(settings.dashboard_key),
        },
        "bot": {
            "configured": bool(settings.discord_token),
            "ready": bool(discord_bot and discord_bot.is_ready()),
            "status": bot_status,
            "taskDone": task_done,
            "closed": bool(discord_bot and discord_bot.is_closed()),
            "latencyMs": latency_ms,
            "user": str(discord_bot.user) if discord_bot and discord_bot.user else None,
            "error": bot_error,
            "commandSync": {
                "status": getattr(discord_bot, "command_sync_status", "not_started") if discord_bot else "not_configured",
                "error": getattr(discord_bot, "command_sync_error", None) if discord_bot else None,
                "inviteUrl": invite_url(settings.guild_id) if settings.discord_application_id else "",
            },
            "presenceRotation": discord_bot.presence_rotation_payload() if discord_bot else {"active": False, "currentIndex": 0, "current": {}},
        },
        "defaults": {
            "guildId": settings.guild_id,
            "aiChannelId": settings.ai_channel_id,
            "allowedCategoryId": settings.allowed_category_id,
            "staffRoleId": settings.staff_role_id,
        },
        "providers": {
            "gemini": bool(settings.gemini_api_key),
            "openrouter": bool(settings.openrouter_api_key),
            "groq": bool(settings.groq_api_key),
        },
        "weather": {
            "configured": bool(settings.openweather_api_key),
            "provider": "OpenWeather",
        },
        "googleDrive": {
            **{
                key: value
                for key, value in drive_archive.status_payload().items()
                if privileged or key not in {"principalEmail", "serviceAccountEmail"}
            },
            "impersonationEnabled": bool(settings.google_drive_impersonate_user),
        },
    }


@app.get("/api/diagnostics")
async def diagnostics(_: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    payload = await health(settings.dashboard_key)
    payload["diagnostics"] = {
        "backendOrigin": settings.public_base_url.rstrip("/"),
        "dashboardHosting": "external",
        "dashboardProvider": "Vercel",
        "dashboardProxyPath": "/api",
        "corsOrigins": settings.cors_origins,
    }
    return payload


@app.get("/api/config/{guild_id}")
async def get_config(guild_id: str, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    config = await store.get_config(guild_id)
    return {"guildId": guild_id, "config": config}


@app.put("/api/config/{guild_id}")
async def put_config(
    guild_id: str,
    request: Request,
    _: None = Depends(require_dashboard_key),
) -> dict[str, Any]:
    payload = await request.json()
    config = payload.get("config", payload)
    saved = await store.set_config(guild_id, config)
    if discord_bot and discord_bot.is_ready():
        try:
            await discord_bot.apply_configured_presence(guild_id)
        except Exception:
            log.exception("Could not apply presence after dashboard config update")
    return {"guildId": guild_id, "config": saved}


@app.get("/api/admin/{guild_id}/status")
async def admin_status(guild_id: str, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    config = await store.get_config(guild_id)
    stats = await store.session_stats(guild_id)
    status = "not_configured"
    if settings.discord_token:
        if discord_bot and discord_bot.is_ready():
            status = "online"
        elif discord_bot and discord_bot.is_closed():
            status = "closed"
        elif bot_task and not bot_task.done():
            status = "starting"
        else:
            status = "stopped"
    return {
        "guildId": guild_id,
        "botStatus": status,
        "aiEnabled": bool(config.get("ai", {}).get("enabled", True)),
        "adminRoleId": str(config.get("admin", {}).get("roleId") or settings.staff_role_id),
        "memory": stats,
        "presenceRotation": discord_bot.presence_rotation_payload() if discord_bot else {"active": False, "currentIndex": 0, "current": {}},
    }


@app.post("/api/admin/{guild_id}/bot/{action}")
async def bot_action(guild_id: str, action: str, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    try:
        if action == "start":
            return await start_discord_bot("dashboard", guild_id)
        if action == "restart":
            return await restart_discord_bot("dashboard", guild_id)
        if action == "shutdown":
            return await stop_discord_bot("dashboard", guild_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="Unknown bot action")


@app.post("/api/admin/{guild_id}/memory/clear")
async def clear_memory(
    guild_id: str,
    request: Request,
    _: None = Depends(require_dashboard_key),
) -> dict[str, Any]:
    payload = await request.json()
    all_channels = bool(payload.get("allChannels"))
    channel_id = str(payload.get("channelId") or "").strip()
    if all_channels:
        cleared = await store.clear_guild_sessions(guild_id)
        await store.add_log(guild_id, "memory.cleared_all", {"channels": cleared, "source": "dashboard"})
        return {"ok": True, "scope": "all", "clearedChannels": cleared}
    if not channel_id:
        config = await store.get_config(guild_id)
        channel_id = str(config.get("ai", {}).get("channelId") or settings.ai_channel_id or "")
    if not channel_id:
        raise HTTPException(status_code=400, detail="Select a channel before clearing memory")
    if discord_bot:
        await discord_bot.clear_ai_session(guild_id, channel_id)
    else:
        await store.clear_session(guild_id, channel_id)
    await store.add_log(guild_id, "memory.cleared", {"channelId": channel_id, "source": "dashboard"})
    return {"ok": True, "scope": "channel", "channelId": channel_id}


@app.post("/api/admin/{guild_id}/ai/{action}")
async def ai_action(guild_id: str, action: str, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    if action not in {"pause", "resume"}:
        raise HTTPException(status_code=404, detail="Unknown AI action")
    config = await store.get_config(guild_id)
    config.setdefault("ai", {})["enabled"] = action == "resume"
    saved = await store.set_config(guild_id, config)
    await store.add_log(guild_id, f"ai.{action}d" if action == "pause" else "ai.resumed", {"source": "dashboard"})
    return {"ok": True, "aiEnabled": saved["ai"]["enabled"]}




@app.get("/api/media/{guild_id}")
async def get_media(
    guild_id: str,
    limit: int = 100,
    media_type: str = "",
    channel_id: str = "",
    _: None = Depends(require_dashboard_key),
) -> dict[str, Any]:
    normalized_type = media_type if media_type in {"image", "video"} else ""
    records = await store.list_media_records(
        guild_id,
        limit=max(1, min(limit, 250)),
        media_type=normalized_type,
        channel_id=str(channel_id or ""),
    )
    config = await store.get_config(guild_id)
    media_config = config.get("media", {})
    return {
        "guildId": guild_id,
        "items": records,
        "stats": await store.media_stats(guild_id),
        "drive": drive_archive.status_payload(
            folder_id=str(media_config.get("googleDriveFolderId") or "")
        ),
    }


@app.post("/api/media/{guild_id}/test-drive")
async def test_media_drive(guild_id: str, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    config = await store.get_config(guild_id)
    folder_id = str(config.get("media", {}).get("googleDriveFolderId") or "").strip()
    if not folder_id:
        raise HTTPException(status_code=400, detail="Set the Google Drive folder ID first")
    try:
        folder = await drive_archive.test_folder(folder_id)
    except DriveConfigurationError as exc:
        log.warning("Google Drive folder test needs configuration: %s", exc)
        detail = exc.as_dict(
            service_account_email=drive_archive.service_account_email,
            principal_email=drive_archive.principal_email,
            credential_project_id=drive_archive.credential_project_id,
            auth_mode=drive_archive.auth_mode,
        )
        detail.update(drive_archive.status_payload(folder_id=folder_id))
        raise HTTPException(status_code=exc.status, detail=detail) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Google Drive folder test failed")
        raise HTTPException(status_code=502, detail={
            "message": f"Google Drive error: {type(exc).__name__}: {exc}",
            "code": "drive_request_failed",
            **drive_archive.status_payload(folder_id=folder_id),
        }) from exc
    await store.add_log(guild_id, "media.drive_tested", {"folderId": folder.get("id"), "folderName": folder.get("name")})
    return {
        "ok": True,
        "folder": folder,
        **drive_archive.status_payload(folder_id=folder_id),
    }


@app.delete("/api/media/{guild_id}/{record_id}")
async def delete_media(
    guild_id: str,
    record_id: str,
    delete_drive_file: bool = True,
    _: None = Depends(require_dashboard_key),
) -> dict[str, Any]:
    record = await store.get_media_record(guild_id, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Media record not found")
    if delete_drive_file and record.get("driveFileId"):
        try:
            await drive_archive.delete_file(str(record["driveFileId"]))
        except Exception as exc:
            log.exception("Could not delete Google Drive media file")
            raise HTTPException(status_code=502, detail=f"Drive deletion failed: {type(exc).__name__}: {exc}") from exc
    removed = await store.delete_media_record(guild_id, record_id)
    await store.add_log(
        guild_id,
        "media.deleted",
        {
            "recordId": record_id,
            "driveFileId": record.get("driveFileId"),
            "driveFileDeleted": bool(delete_drive_file),
            "source": "dashboard",
        },
    )
    return {"ok": True, "record": removed, "driveFileDeleted": bool(delete_drive_file)}


@app.get("/api/logs/{guild_id}")
async def get_logs(guild_id: str, limit: int = 80, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    return {"guildId": guild_id, "logs": await store.list_logs(guild_id, limit=limit)}


@app.get("/api/discord/{guild_id}/channels")
async def get_channels(guild_id: str, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    if not discord_bot or not discord_bot.is_ready():
        return {"guildId": guild_id, "channels": [], "categories": [], "botReady": False}

    guild = discord_bot.get_guild(int(guild_id))
    source = "cache"
    if not guild:
        try:
            guild = await discord_bot.fetch_guild(int(guild_id))
            source = "rest"
        except discord.Forbidden:
            return {
                "guildId": guild_id,
                "channels": [],
                "categories": [],
                "botReady": True,
                "source": "forbidden",
                "error": "Missing access to this guild. Reinvite the bot with bot + applications.commands scopes.",
                "inviteUrl": invite_url(guild_id),
            }
        except discord.HTTPException as exc:
            return {"guildId": guild_id, "channels": [], "categories": [], "botReady": True, "source": "error", "error": str(exc)}

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
                "categoryId": str(channel.category_id) if getattr(channel, "category_id", None) else "",
                "type": str(channel.type),
            }
            for channel in raw_channels
            if isinstance(channel, discord.TextChannel)
        ],
        key=lambda item: item["name"].lower(),
    )

    if settings.allowed_category_id and not any(item["id"] == settings.allowed_category_id for item in categories):
        categories.insert(0, {"id": settings.allowed_category_id, "name": "Configured category"})
    if settings.ai_channel_id and not any(item["id"] == settings.ai_channel_id for item in channels):
        channels.insert(
            0,
            {
                "id": settings.ai_channel_id,
                "name": "configured-ai-channel",
                "mention": f"<#{settings.ai_channel_id}>",
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
        "inviteUrl": invite_url(guild_id),
    }


@app.get("/api/discord/{guild_id}/commands")
async def get_command_setup(guild_id: str, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    config = await store.get_config(guild_id)
    registered = set(discord_bot.registered_command_names if discord_bot else [])
    rows = []
    for item in command_catalog(config):
        row = dict(item)
        row["registered"] = item["key"] in registered
        rows.append(row)
    return {
        "guildId": guild_id,
        "commands": rows,
        "syncStatus": discord_bot.command_sync_status if discord_bot else "offline",
        "syncError": discord_bot.command_sync_error if discord_bot else None,
    }


@app.post("/api/discord/{guild_id}/sync-commands")
async def sync_commands(guild_id: str, _: None = Depends(require_dashboard_key)) -> dict[str, Any]:
    if not discord_bot:
        raise HTTPException(status_code=503, detail="Discord bot is not configured")
    try:
        registered = await discord_bot.rebuild_application_commands(guild_id, sync=True)
    except discord.Forbidden as exc:
        discord_bot.command_sync_status = "forbidden"
        discord_bot.command_sync_error = f"Forbidden: {exc}"
        await store.add_log(
            guild_id,
            "commands.sync_forbidden",
            {"error": discord_bot.command_sync_error, "inviteUrl": invite_url(guild_id)},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Missing access. Reinvite the bot with bot + applications.commands scopes.",
                "error": discord_bot.command_sync_error,
                "inviteUrl": invite_url(guild_id),
            },
        )
    except Exception as exc:
        discord_bot.command_sync_status = "failed"
        discord_bot.command_sync_error = f"{type(exc).__name__}: {exc}"
        await store.add_log(guild_id, "commands.sync_failed", {"error": discord_bot.command_sync_error})
        raise HTTPException(status_code=502, detail=discord_bot.command_sync_error)

    discord_bot.command_sync_status = "ok"
    discord_bot.command_sync_error = None
    await store.add_log(guild_id, "commands.synced", {"source": "dashboard", "registered": registered})
    return {"ok": True, "registered": registered, "count": len(registered)}
