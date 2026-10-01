from __future__ import annotations

import random
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .common import MOTIVATIONS, PUNS, SONG_RECS
from .responses import (
    ensure_bot_admin,
    ensure_command_enabled,
    get_interaction_config,
    send_action_result,
)

if TYPE_CHECKING:
    from .client import ConanBot


def make_ping_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(name="ping", description="Check if Conan Gray Bot is online.")
    async def ping(interaction: discord.Interaction) -> None:
        if not await ensure_command_enabled(interaction, "ping"):
            return
        config = await get_interaction_config(interaction)
        ms = round(bot.latency * 1000)
        quality = (
            "Excellent" if ms < 100 else "Good" if ms < 220 else "A little cinematic"
        )
        await send_action_result(
            interaction,
            config,
            feature="ping",
            title="Signal check",
            outcome="online",
            facts=f"The Discord connection is online with {ms} milliseconds of latency.",
            fields=[
                ("Connection", "Online", True),
                ("Latency", f"{ms} ms", True),
                ("Quality", quality, True),
            ],
            kind="success",
        )

    return ping


def make_help_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(name="help", description="Show Conan Gray Bot commands.")
    async def help_command(interaction: discord.Interaction) -> None:
        if not await ensure_command_enabled(interaction, "help"):
            return
        config = await get_interaction_config(interaction)
        await send_action_result(
            interaction,
            config,
            feature="help",
            title="Control-room directory",
            outcome="command list",
            facts="The bot offers AI branch conversations, utility commands, Drive-backed media pulls, music prompts, and six configurable games.",
            fields=[
                (
                    "Conversation",
                    "`@mention` starts a fresh branch. Reply to the newest bot message to continue it.",
                    False,
                ),
                (
                    "Quick commands",
                    "`/ping` · `/weather` · `/media` · `/pun` · `/motivation` · `/recommend` · `/lyrics`",
                    False,
                ),
                (
                    "Games",
                    "`/tictactoe` · `/coinflip` · `/8ball` · `/rps` · `/guesssong` · `/wouldyourather`",
                    False,
                ),
                (
                    "Admin",
                    "`/admin` and `/forget` require the configured admin role.",
                    False,
                ),
            ],
            kind="info",
        )

    return help_command


def make_pun_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(name="pun", description="Get a random pun.")
    async def pun(interaction: discord.Interaction) -> None:
        if not await ensure_command_enabled(interaction, "pun"):
            return
        config = await get_interaction_config(interaction)
        selected = random.choice(PUNS)
        await send_action_result(
            interaction,
            config,
            feature="pun",
            title="Pun department",
            outcome="delivered",
            facts=selected,
            fields=[
                ("The pun", selected, False),
                ("Damage level", "Emotionally unnecessary", True),
            ],
        )

    return pun


def make_motivation_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(
        name="motivation", description="Get a dramatic motivational quote."
    )
    async def motivation(interaction: discord.Interaction) -> None:
        if not await ensure_command_enabled(interaction, "motivation"):
            return
        config = await get_interaction_config(interaction)
        selected = random.choice(MOTIVATIONS)
        await send_action_result(
            interaction,
            config,
            feature="motivation",
            title="Tiny main-character reset",
            outcome="encouragement delivered",
            facts=selected,
            fields=[
                ("Keep this part", selected, False),
                ("Next move", "One manageable thing. Then another.", False),
            ],
            kind="success",
        )

    return motivation


def make_recommend_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(
        name="recommend", description="Get a Conan Gray song recommendation."
    )
    async def recommend(interaction: discord.Interaction) -> None:
        if not await ensure_command_enabled(interaction, "recommend"):
            return
        config = await get_interaction_config(interaction)
        song, vibe = random.choice(SONG_RECS)
        await send_action_result(
            interaction,
            config,
            feature="recommend",
            title="Tonight's song prescription",
            outcome=song,
            facts=f"Recommended song: {song}. Listening mood: {vibe}.",
            fields=[("Track", f"**{song}**", True), ("Best for", vibe, False)],
            kind="command",
        )

    return recommend


def make_lyrics_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(
        name="lyrics", description="Get a Conan song vibe card without full lyrics."
    )
    @app_commands.describe(song="Optional song name")
    async def lyrics(interaction: discord.Interaction, song: str | None = None) -> None:
        if not await ensure_command_enabled(interaction, "lyrics"):
            return
        config = await get_interaction_config(interaction)
        selected = song or random.choice([item[0] for item in SONG_RECS])
        await send_action_result(
            interaction,
            config,
            feature="lyrics",
            title=f"Song-vibe card: {selected}",
            outcome="vibe analysis",
            facts=f"Selected song: {selected}. Full copyrighted lyrics are not provided.",
            fields=[
                (
                    "Vibe",
                    "Emotionally cinematic, soft around the edges, and a little too relatable.",
                    False,
                ),
                (
                    "What I can do",
                    "Explain themes, discuss mood, or recommend something similar—without reproducing full lyrics.",
                    False,
                ),
            ],
            kind="command",
        )

    return lyrics


def make_forget_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(
        name="forget",
        description="Clear the shared AI memory for this channel. Bot admins only.",
    )
    async def forget(interaction: discord.Interaction) -> None:
        config = await ensure_bot_admin(interaction, bot, command_key="forget")
        if not config:
            return
        guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
        channel_id = str(interaction.channel_id)
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
        message = (
            config.get("admin", {}).get("memoryClearedMessage")
            or "Shared memory for this channel has been cleared."
        )
        await send_action_result(
            interaction,
            config,
            feature="admin",
            title="Branch memory cleared",
            outcome="channel memory cleared",
            facts=f"AI branch memory was cleared for channel ID {channel_id}.",
            fields=[
                ("Scope", "Current channel", True),
                ("Status", str(message), False),
            ],
            kind="admin",
            ephemeral=True,
        )

    return forget
