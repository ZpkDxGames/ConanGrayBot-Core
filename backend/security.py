"""Server-only service authentication and body-bound actor signatures."""

import hashlib
import hmac
import re
import time

from fastapi import HTTPException, Request

from .config import get_settings
from .state import TTLRegistry

replays: TTLRegistry[bool] = TTLRegistry(10000, 120)
rates: TTLRegistry[list[float]] = TTLRegistry(5000, 60)


def actor_signature(
    secret: str, method: str, path: str, actor: str, stamp: str, nonce: str, body: bytes
) -> str:
    message = "\n".join(
        [
            "conan-actor-v1",
            method.upper(),
            path,
            actor,
            stamp,
            nonce,
            hashlib.sha256(body).hexdigest(),
        ]
    )
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


async def require_service(request: Request) -> str:
    settings = get_settings()
    secret = settings.core_service_token
    if not secret:
        raise HTTPException(503, "Service authentication unavailable")
    if not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + secret
    ):
        raise HTTPException(401, "Service authentication required")
    actor = request.headers.get("x-conan-actor", "")
    stamp = request.headers.get("x-conan-timestamp", "")
    nonce = request.headers.get("x-conan-nonce", "")
    signature = request.headers.get("x-conan-signature", "")
    try:
        fresh = abs(time.time() - int(stamp)) <= 60
    except ValueError:
        fresh = False
    path = request.url.path + ("?" + request.url.query if request.url.query else "")
    if not fresh or not actor.isdigit() or not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise HTTPException(401, "Invalid actor proof")
    expected = actor_signature(
        secret, request.method, path, actor, stamp, nonce, await request.body()
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid actor proof")
    if nonce in replays:
        raise HTTPException(401, "Actor proof replayed")
    replays[nonce] = True
    now = time.monotonic()
    recent = [v for v in rates.get(actor, []) if now - v < 60]
    if len(recent) >= 120:
        raise HTTPException(429, "Management request limit exceeded")
    rates[actor] = recent + [now]
    guild = request.path_params.get("guild_id")
    if guild and guild != settings.guild_id:
        raise HTTPException(403, "Guild access denied")
    request.state.actor_id = actor
    return actor
