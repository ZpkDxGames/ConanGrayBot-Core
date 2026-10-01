from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..presentation import (
    send_interaction_feedback,
)
from .common import configured_admin_role_id, log
from .responses import ensure_bot_admin, send_action_result

if TYPE_CHECKING:
    from .client import ConanBot


async def _delayed_bot_control(
    bot: ConanBot, action: str, guild_id: str, actor_id: str
) -> None:
    await asyncio.sleep(0.75)
    try:
        await bot.request_control(action, guild_id, actor_id)
    except Exception:
        log.exception("Admin bot control action failed: %s", action)


def make_admin_group(bot: ConanBot) -> app_commands.Group:
    group = app_commands.Group(
        name="admin",
        description="Bot administration commands for the configured admin role.",
    )

    @group.command(
        name="clear-memory",
        description="Clear shared AI memory for this channel or the entire server.",
    )
    @app_commands.describe(
        scope="Clear only one channel or all remembered channels in this server.",
        channel="Optional channel to clear. Defaults to the current channel.",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Current/selected channel", value="channel"),
            app_commands.Choice(name="All channels", value="all"),
        ]
    )
    async def clear_memory(
        interaction: discord.Interaction,
        scope: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
    ) -> None:
        config = await ensure_bot_admin(interaction, bot)
        if not config:
            return
        guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
        if scope.value == "all":
            cleared_channels = await bot.clear_all_ai_sessions(guild_id)
            await bot.store.add_log(
                guild_id,
                "memory.cleared_all",
                {
                    "channels": cleared_channels,
                    "source": "slash",
                    "actorId": str(interaction.user.id),
                },
            )
            await send_action_result(
                interaction,
                config,
                feature="admin",
                title="Server memory reset",
                outcome="all branches cleared",
                facts=f"Cleared AI branch memory in {cleared_channels} channel(s).",
                fields=[
                    ("Scope", "Entire server", True),
                    ("Channels cleared", str(cleared_channels), True),
                ],
                kind="admin",
                ephemeral=True,
            )
            return
        channel_id = str((channel.id if channel else interaction.channel_id) or "")
        await bot.clear_ai_session(guild_id, channel_id)
        await bot.store.add_log(
            guild_id,
            "memory.cleared",
            {
                "channelId": channel_id,
                "source": "slash",
                "actorId": str(interaction.user.id),
            },
        )
        channel_label = channel.mention if channel else "this channel"
        await send_action_result(
            interaction,
            config,
            feature="admin",
            title="Channel memory reset",
            outcome="branch memory cleared",
            facts=f"Cleared every AI branch for {channel_label}.",
            fields=[("Scope", channel_label, True), ("Status", "Cleared", True)],
            kind="admin",
            ephemeral=True,
        )

    @group.command(name="status", description="Show bot, AI, and branch-memory status.")
    async def admin_status(interaction: discord.Interaction) -> None:
        config = await ensure_bot_admin(interaction, bot)
        if not config:
            return
        guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
        stats = await bot.store.session_stats(guild_id)
        ai_enabled = bool(config.get("ai", {}).get("enabled", True))
        role_id = configured_admin_role_id(config, bot.settings)
        latency = round(bot.latency * 1000) if bot.latency is not None else 0
        await send_action_result(
            interaction,
            config,
            feature="admin",
            title="Control-room status",
            outcome="status report",
            facts=f"Discord ready: {bot.is_ready()}; latency: {latency} ms; AI enabled: {ai_enabled}; memory channels: {stats['channels']}; stored messages: {stats['messages']}.",
            fields=[
                (
                    "Discord",
                    f"{'Online' if bot.is_ready() else 'Connecting/offline'} · `{latency} ms`",
                    True,
                ),
                ("AI", "Enabled" if ai_enabled else "Paused", True),
                (
                    "Branch memory",
                    f"{stats['channels']} channel(s) · {stats['messages']} message(s)",
                    False,
                ),
                ("Admin role", f"<@&{role_id}>", False),
            ],
            kind="admin",
            ephemeral=True,
        )

    @group.command(
        name="pause-ai",
        description="Pause AI replies without shutting down the Discord bot.",
    )
    async def pause_ai(interaction: discord.Interaction) -> None:
        config = await ensure_bot_admin(interaction, bot)
        if not config:
            return
        guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
        config.setdefault("ai", {})["enabled"] = False
        await bot.store.set_config(guild_id, config)
        await bot.store.add_log(
            guild_id,
            "ai.paused",
            {"source": "slash", "actorId": str(interaction.user.id)},
        )
        await send_action_result(
            interaction,
            config,
            feature="admin",
            title="AI replies paused",
            outcome="paused",
            facts="AI message replies are paused. Slash commands and media functions remain online.",
            fields=[("AI chat", "Paused", True), ("Other commands", "Online", True)],
            kind="admin",
            ephemeral=True,
        )

    @group.command(name="resume-ai", description="Resume AI replies.")
    async def resume_ai(interaction: discord.Interaction) -> None:
        config = await ensure_bot_admin(interaction, bot)
        if not config:
            return
        guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
        config.setdefault("ai", {})["enabled"] = True
        await bot.store.set_config(guild_id, config)
        await bot.store.add_log(
            guild_id,
            "ai.resumed",
            {"source": "slash", "actorId": str(interaction.user.id)},
        )
        await send_action_result(
            interaction,
            config,
            feature="admin",
            title="AI replies resumed",
            outcome="enabled",
            facts="AI message replies are enabled again.",
            fields=[("AI chat", "Enabled", True), ("Branch memory", "Preserved", True)],
            kind="admin",
            ephemeral=True,
        )

    @group.command(
        name="apply-presence",
        description="Apply the dashboard presence/status settings immediately.",
    )
    async def apply_presence(interaction: discord.Interaction) -> None:
        config = await ensure_bot_admin(interaction, bot)
        if not config:
            return
        guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
        await bot.apply_configured_presence(guild_id)
        await bot.store.add_log(
            guild_id,
            "presence.applied",
            {"source": "slash", "actorId": str(interaction.user.id)},
        )
        presence = config.get("presence", {})
        entries = bot._presence_entries(presence)
        rotation_enabled = (
            bool(presence.get("rotationEnabled", False)) and len(entries) > 1
        )
        interval = max(15, int(presence.get("intervalSeconds") or 60))
        first = entries[0]
        await send_action_result(
            interaction,
            config,
            feature="admin",
            title="Presence applied",
            outcome="rotation active" if rotation_enabled else "updated",
            facts=(
                f"{len(entries)} presence entries rotate every {interval} seconds."
                if rotation_enabled
                else f"Status {first.get('status')}; activity {first.get('activityType')} {first.get('activityText')}."
            ),
            fields=[
                ("Mode", "Rotating" if rotation_enabled else "Static", True),
                ("Entries", str(len(entries)), True),
                (
                    "Interval",
                    f"{interval}s" if rotation_enabled else "Not rotating",
                    True,
                ),
                (
                    "Current activity",
                    f"{first.get('activityType', 'listening')} {first.get('activityText', '')}".strip(),
                    False,
                ),
            ],
            kind="admin",
            ephemeral=True,
        )

    @group.command(
        name="restart",
        description="Restart the Discord bot connection. The dashboard stays online.",
    )
    async def restart(interaction: discord.Interaction) -> None:
        config = await ensure_bot_admin(interaction, bot)
        if not config:
            return
        guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
        message = (
            config.get("admin", {}).get("restartMessage")
            or "Restarting the Discord bot connection…"
        )
        await send_interaction_feedback(
            interaction,
            config,
            title="Restart queued",
            description=str(message),
            kind="admin",
            fields=[
                ("Dashboard/API", "Stays online", True),
                ("Discord connection", "Restarting", True),
            ],
            ephemeral=True,
        )
        asyncio.create_task(
            _delayed_bot_control(bot, "restart", guild_id, str(interaction.user.id))
        )

    @group.command(
        name="shutdown",
        description="Stop the Discord bot connection. Restart it from the dashboard.",
    )
    async def shutdown(interaction: discord.Interaction) -> None:
        config = await ensure_bot_admin(interaction, bot)
        if not config:
            return
        guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
        message = (
            config.get("admin", {}).get("shutdownMessage")
            or "Shutting down the Discord bot connection."
        )
        await send_interaction_feedback(
            interaction,
            config,
            title="Shutdown queued",
            description=str(message),
            kind="warning",
            fields=[
                ("Dashboard/API", "Stays online", True),
                ("Discord connection", "Stopping", True),
            ],
            ephemeral=True,
        )
        asyncio.create_task(
            _delayed_bot_control(bot, "shutdown", guild_id, str(interaction.user.id))
        )

    return group
