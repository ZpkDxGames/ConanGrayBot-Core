"""Exercise saved persona/providers without conversation-memory side effects."""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException

from ..ai_providers import AIProviderError, ask_ai
from . import runtime
from .auth import require_staff
from .models import SandboxRequest, SandboxResult

router = APIRouter()


@router.post("/api/v1/ai/{guild_id}/test-reply", response_model=SandboxResult)
async def test_reply(
    guild_id: str, payload: SandboxRequest, actor: str = Depends(require_staff)
):
    config = await runtime.store.get_config(guild_id)
    start = time.monotonic()
    try:
        answer, provider = await ask_ai(
            config,
            [],
            payload.prompt,
            "Isolated dashboard test. No conversation memory is read or written.",
        )
    except (AIProviderError, asyncio.TimeoutError):
        raise HTTPException(503, "AI providers unavailable") from None
    return {
        "answer": answer,
        "provider": provider,
        "latencyMs": max(0, int((time.monotonic() - start) * 1000)),
    }
