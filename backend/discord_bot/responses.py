from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from ..presentation import (
    build_feedback_embed,
    interpret_action,
    send_interaction_feedback,
    send_message_feedback,
)
from .common import (
    configured_admin_role_id,
    discord_profile_name,
    member_has_admin_role,
    random_trigger_media_type,
    split_discord_text,
    template_key_for_feature,
)
from .media_delivery import send_random_trigger_media

if TYPE_CHECKING:
    from .client import ConanBot


async def send_styled_reply(
    message: discord.Message,
    config: dict[str, Any],
    text: str,
    provider: str | None = None,
    *,
    force_plain: bool = False,
    mention_author_override: bool | None = None,
) -> list[Any]:
    appearance = config.get("appearance", {})
    ai_config = config.get("ai", {})
    presentation = config.get("presentation", {})
    ai_template_embed = bool(
        config.get("messageTemplates", {}).get("ai", {}).get("useEmbed", True)
    )
    reply_style = (
        "plain"
        if force_plain
        else (
            "embed"
            if presentation.get("embedEverywhere", True) and ai_template_embed
            else str(
                ai_config.get("replyStyle")
                or ("embed" if ai_config.get("embedReplies", True) else "plain")
            )
        )
    )
    max_length = int(ai_config.get("maxDiscordMessageLength") or 1900)
    split_long = ai_config.get("splitLongReplies", True)
    chunk_limit = min(max_length, 3900 if reply_style == "embed" else 2000)
    chunks = (
        split_discord_text(text, chunk_limit) if split_long else [text[:chunk_limit]]
    )
    mention_author = (
        bool(ai_config.get("mentionAuthor", False))
        if mention_author_override is None
        else bool(mention_author_override)
    )

    sent_messages: list[Any] = []
    for index, chunk in enumerate(chunks):
        if reply_style == "plain":
            sent = await message.reply(
                chunk, mention_author=mention_author if index == 0 else False
            )
        else:
            configured_title = str(appearance.get("embedTitle") or "").strip()
            title = configured_title or (
                "A note from the control room"
                if len(chunks) == 1
                else f"A note from the control room · {index + 1}/{len(chunks)}"
            )
            embed = build_feedback_embed(
                config,
                title=title,
                description=chunk,
                kind="ai",
                template_key="ai",
                actor=message.author,
                provider=provider,
                source_note="Branch-aware reply",
                context={
                    "channel": getattr(message.channel, "name", "channel"),
                    "guild": getattr(message.guild, "name", "server"),
                },
            )
            sent = await message.reply(
                embed=embed, mention_author=mention_author if index == 0 else False
            )
        if sent is not None:
            sent_messages.append(sent)
    return sent_messages


async def send_trigger(
    message: discord.Message,
    config: dict[str, Any],
    trigger: dict[str, Any],
    *,
    drive_archive: Any | None = None,
) -> dict[str, Any]:
    response_text = str(trigger.get("responseText") or "Conan-coded moment detected.")
    media_source = str(trigger.get("mediaUrl") or "").strip()
    trigger_word = str(trigger.get("word") or "trigger")
    requested_random_type = random_trigger_media_type(media_source)

    if requested_random_type is None and media_source.lower().startswith("{random"):
        await send_message_feedback(
            message,
            config,
            title="Invalid random-media token",
            description="Use `{random}`, `{random:image}`, or `{random:video}` in the trigger's Media source field.",
            kind="warning",
            template_key="trigger",
            fields=[
                ("Trigger", f"`{trigger_word}`", True),
                ("Received", media_source[:200], True),
            ],
        )
        return {"mediaSource": media_source, "mediaStatus": "invalid_token"}

    if requested_random_type is not None:
        return await send_random_trigger_media(
            message,
            config,
            trigger_word=trigger_word,
            response_text=response_text,
            requested_type=requested_random_type,
            drive_archive=drive_archive,
        )

    embed = build_feedback_embed(
        config,
        title="A media cue just fired",
        description=response_text,
        kind="trigger",
        template_key="trigger",
        fields=[
            ("Trigger", f"`{trigger_word}`", True),
            ("Channel", f"#{getattr(message.channel, 'name', 'channel')}", True),
        ],
        actor=message.author,
        source_note="Configured trigger",
        image_url=media_source or None,
        context={
            "channel": getattr(message.channel, "name", "channel"),
            "guild": getattr(message.guild, "name", "server"),
            "trigger": trigger_word,
        },
    )
    await message.channel.send(embed=embed)
    return {"mediaSource": "url" if media_source else "none"}


async def get_interaction_config(interaction: discord.Interaction) -> dict[str, Any]:
    from .client import ConanBot

    bot = interaction.client
    assert isinstance(bot, ConanBot)
    guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
    return await bot.store.get_config(guild_id)


async def send_action_result(
    interaction: discord.Interaction,
    config: dict[str, Any],
    *,
    feature: str,
    title: str,
    outcome: str,
    facts: str,
    fields: list[tuple[str, str, bool]] | None = None,
    kind: str = "command",
    ephemeral: bool = False,
    view: discord.ui.View | None = None,
) -> Any:
    # AI narration can take longer than Discord's initial interaction window.
    # Acknowledge first, then edit the original deferred response once narration
    # or its deterministic fallback is ready.
    deferred_here = False
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        deferred_here = True

    actor_name = discord_profile_name(getattr(interaction, "user", None))
    narration, source_note = await interpret_action(
        config,
        feature=feature,
        outcome=outcome,
        facts=facts,
        actor_name=actor_name,
    )
    template_key = template_key_for_feature(feature, kind)
    return await send_interaction_feedback(
        interaction,
        config,
        title=title,
        description=narration,
        kind=kind,
        template_key=template_key,
        fields=fields,
        ephemeral=ephemeral,
        view=view,
        source_note=source_note,
        edit_original=deferred_here,
        context={"feature": feature, "outcome": outcome, "facts": facts},
    )


async def ensure_command_enabled(interaction: discord.Interaction, key: str) -> bool:
    config = await get_interaction_config(interaction)
    if not config.get("commands", {}).get(key, True):
        await send_interaction_feedback(
            interaction,
            config,
            title="Command unavailable",
            description="That command is currently disabled from the dashboard.",
            kind="warning",
            fields=[("Command", f"/{key}", True), ("Status", "Disabled", True)],
            ephemeral=True,
        )
        return False
    if key in {
        "coinflip",
        "eightball",
        "rps",
        "guesssong",
        "wouldyourather",
        "tictactoe",
    }:
        bot: Any = interaction.client
        allowed = str(
            config.get("games", {}).get("allowedCategoryId")
            or bot.settings.allowed_category_id
            or ""
        )
        channel = interaction.channel
        category = getattr(channel, "category_id", None)
        if category is None:
            category = getattr(getattr(channel, "parent", None), "category_id", None)
        if allowed and str(category or "") != allowed:
            await send_interaction_feedback(
                interaction,
                config,
                title="Games unavailable here",
                description="Games are restricted to the configured Discord category.",
                kind="warning",
                ephemeral=True,
            )
            return False
    return True


async def ensure_bot_admin(
    interaction: discord.Interaction,
    bot: ConanBot,
    *,
    command_key: str = "admin",
) -> dict[str, Any] | None:
    config = await get_interaction_config(interaction)
    if not config.get("commands", {}).get(command_key, True):
        await send_interaction_feedback(
            interaction,
            config,
            title="Command unavailable",
            description="That command is currently disabled from the dashboard.",
            kind="warning",
            fields=[("Command", f"/{command_key}", True), ("Status", "Disabled", True)],
            ephemeral=True,
        )
        return None
    if member_has_admin_role(interaction.user, config, bot.settings):
        return config
    denied = (
        config.get("admin", {}).get("deniedMessage")
        or "You need the configured bot-admin role to use this command."
    )
    await send_interaction_feedback(
        interaction,
        config,
        title="Control room locked",
        description=str(denied),
        kind="error",
        fields=[
            (
                "Required role",
                f"<@&{configured_admin_role_id(config, bot.settings)}>",
                False,
            )
        ],
        ephemeral=True,
    )
    return None
