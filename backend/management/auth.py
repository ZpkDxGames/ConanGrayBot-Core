import discord
from fastapi import Depends, HTTPException

from ..security import require_service
from . import runtime


async def require_staff(actor: str = Depends(require_service)) -> str:
    bot = runtime.discord_bot
    if not bot or not bot.is_ready():
        raise HTTPException(503, "Authorization service unavailable")
    guild = bot.get_guild(int(runtime.settings.guild_id))
    if not guild:
        raise HTTPException(403, "Guild access denied")
    try:
        member = await guild.fetch_member(int(actor))
    except (discord.NotFound, discord.Forbidden):
        raise HTTPException(403, "Staff access denied") from None
    except discord.HTTPException:
        raise HTTPException(503, "Authorization service unavailable") from None
    if not runtime.settings.staff_role_id or not any(
        str(role.id) == runtime.settings.staff_role_id for role in member.roles
    ):
        raise HTTPException(403, "Staff access denied")
    return actor
