import discord
from fastapi import Depends, HTTPException, Request

from ..http import pooled_session
from ..security import require_service
from . import runtime


async def require_staff(actor: str = Depends(require_service)) -> str:
    bot = runtime.discord_bot
    if not bot or not bot.is_ready():
        if not runtime.settings.discord_token or not runtime.settings.staff_role_id:
            raise HTTPException(503, "Authorization service unavailable")
        try:
            async with pooled_session() as session:
                async with session.get(
                    f"https://discord.com/api/v10/guilds/{runtime.settings.guild_id}/members/{actor}",
                    headers={"Authorization": "Bot " + runtime.settings.discord_token},
                ) as response:
                    if response.status in {403, 404}:
                        raise HTTPException(403, "Staff access denied")
                    if response.status != 200:
                        raise HTTPException(503, "Authorization service unavailable")
                    member_data = await response.json()
                    if runtime.settings.staff_role_id not in member_data.get(
                        "roles", []
                    ):
                        raise HTTPException(403, "Staff access denied")
                    return actor
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Authorization service unavailable") from None
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


async def require_mutation_audit(
    request: Request, actor: str = Depends(require_staff)
) -> None:
    if request.method not in {"POST", "PUT", "DELETE"}:
        return
    try:
        await runtime.store.add_log(
            runtime.settings.guild_id,
            "management.mutation_requested",
            {
                "actorId": actor,
                "method": request.method,
                "path": request.url.path,
                "requestId": request.state.request_id,
            },
        )
    except Exception:
        # No mutation is admitted when its authorization audit cannot be stored.
        raise HTTPException(503, "Audit service unavailable") from None
