from fastapi import APIRouter, Depends

from ..models import ConfigEnvelope, ConfigUpdate
from . import runtime
from .auth import require_staff

router = APIRouter()


@router.get("/api/v1/config/{guild_id}", response_model=ConfigEnvelope)
async def get_config(guild_id: str, actor: str = Depends(require_staff)):
    return {"guildId": guild_id, "config": await runtime.store.get_config(guild_id)}


@router.put("/api/v1/config/{guild_id}", response_model=ConfigEnvelope)
async def put_config(
    guild_id: str, payload: ConfigUpdate, actor: str = Depends(require_staff)
):
    saved = await runtime.store.set_config(
        guild_id, payload.config.model_dump(), expected_revision=payload.revision
    )
    if runtime.discord_bot and runtime.discord_bot.is_ready():
        await runtime.discord_bot.apply_configured_presence(guild_id)
    return {"guildId": guild_id, "config": saved}
