"""Versioned API boundary: body limits, redacted errors and mutation audit."""

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .logging import configure_logging
from .management import admin, configuration, discord, records, runtime, stream
from .management.auth import require_staff
from .migrations import RevisionConflict
from .version import VERSION

configure_logging()


@asynccontextmanager
async def lifespan(app):
    if runtime.settings.discord_token:
        await runtime.start_discord_bot(source="startup")
    try:
        yield
    finally:
        async with runtime.bot_lifecycle_lock:
            await runtime._stop_discord_bot_locked()


app = FastAPI(title="ConanGrayBot Core", version=VERSION, lifespan=lifespan)
for router in (
    configuration.router,
    admin.router,
    discord.router,
    records.router,
    stream.router,
):
    app.include_router(router)


def error(request, status, code, message, fields=None):
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "requestId": getattr(request.state, "request_id", ""),
            "fields": fields or [],
        },
    )


@app.middleware("http")
async def boundary(request: Request, call_next):
    request.state.request_id = uuid4().hex
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 256000:
            return error(request, 413, "body_too_large", "Request exceeds 256 KB")
    setattr(request, "_body", bytes(body))
    try:
        response = await call_next(request)
    except Exception:
        response = error(request, 500, "internal_error", "Request failed")
    if (
        request.method in {"POST", "PUT", "DELETE"}
        and response.status_code < 400
        and hasattr(request.state, "actor_id")
    ):
        await runtime.store.add_log(
            runtime.settings.guild_id,
            "management.mutation",
            {
                "actorId": request.state.actor_id,
                "method": request.method,
                "path": request.url.path,
                "requestId": request.state.request_id,
            },
        )
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    message = (
        str(exc.detail)
        if isinstance(exc.detail, str) and exc.status_code < 500
        else "Request unavailable"
    )
    return error(request, exc.status_code, f"http_{exc.status_code}", message)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return error(
        request,
        422,
        "validation_error",
        "Correct the highlighted fields",
        [
            {"path": ".".join(str(p) for p in row["loc"]), "message": row["msg"]}
            for row in exc.errors()
        ],
    )


@app.exception_handler(RevisionConflict)
async def conflict(request, exc):
    return error(
        request, 409, "revision_conflict", "Configuration changed; reload before saving"
    )


@app.get("/health/live")
async def live():
    return {"ok": True, "version": VERSION}


@app.get("/health/ready")
async def ready():
    online = bool(runtime.discord_bot and runtime.discord_bot.is_ready())
    return JSONResponse({"ok": online}, status_code=200 if online else 503)


@app.get("/api/v1/auth/check")
async def auth_check(actor: str = Depends(require_staff)):
    return {"allowed": True, "actorId": actor, "guildId": runtime.settings.guild_id}


@app.get("/api/v1/compatibility")
async def compatibility(actor: str = Depends(require_staff)):
    return {"coreVersion": VERSION, "apiVersion": "v1", "schemaVersion": 4}


@app.get("/api/v1/diagnostics")
async def diagnostics(actor: str = Depends(require_staff)):
    bot = runtime.discord_bot
    return {
        "version": VERSION,
        "botReady": bool(bot and bot.is_ready()),
        "commandSync": bot.command_sync_status if bot else "offline",
        "store": type(runtime.store).__name__,
        "providers": {
            "gemini": bool(runtime.settings.gemini_api_key),
            "openrouter": bool(runtime.settings.openrouter_api_key),
            "groq": bool(runtime.settings.groq_api_key),
        },
        "weatherConfigured": bool(runtime.settings.openweather_api_key),
        "driveConfigured": runtime.drive_archive.configured,
    }
