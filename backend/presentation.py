from __future__ import annotations

import asyncio
import copy
import random
from collections import ChainMap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import discord

from .ai_providers import AIProviderError, ask_ai

KIND_TITLES = {
    "ai": "A note from the control room",
    "command": "Command result",
    "game": "Game room update",
    "success": "Everything landed",
    "warning": "Small plot complication",
    "error": "Something went off-script",
    "admin": "Admin control room",
    "media": "Media archive update",
    "trigger": "A media cue just fired",
    "info": "Conan Gray Bot",
}

KIND_COLORS = {
    "success": 0x4ADE80,
    "warning": 0xFBBF24,
    "error": 0xFB7185,
    "admin": 0xA78BFA,
    "media": 0x38BDF8,
    "trigger": 0x38BDF8,
    "game": 0xF472B6,
}

FEATURE_SUBJECTS = {
    "ping": "the signal check",
    "help": "the command map",
    "pun": "that pun",
    "motivation": "that tiny motivational monologue",
    "recommend": "that song recommendation",
    "lyrics": "that song-vibe card",
    "coinflip": "the coin's extremely serious decision",
    "eightball": "the emotionally suspicious 8-ball",
    "rps": "the rock-paper-scissors showdown",
    "guesssong": "the mystery-song clue",
    "wouldyourather": "that impossible little choice",
    "tictactoe": "the tic-tac-toe situation",
    "trigger": "the media trigger",
    "media": "the archive operation",
    "admin": "the control-room action",
    "command": "the command result",
}

GAME_FEATURES = {
    "coinflip",
    "eightball",
    "rps",
    "guesssong",
    "wouldyourather",
    "tictactoe",
}

TEMPLATE_PARENT_KEYS = {
    "game_tictactoe": "game",
    "game_coinflip": "game",
    "game_eightball": "game",
    "game_rps": "game",
    "game_guesssong": "game",
    "game_wouldyourather": "game",
}

FALLBACK_OPENINGS = [
    "Okay, {subject} has officially entered the chat.",
    "There it is: {subject}, delivered with suspicious confidence.",
    "The control room has reviewed {subject} and chosen drama.",
    "I would call {subject} subtle, but that would be dishonest.",
    "Against all odds, {subject} has a complete emotional arc.",
    "A tiny spotlight just found {subject} and refused to leave.",
    "The universe handed us {subject}; apparently we live here now.",
    "I ran the numbers and {subject} is somehow personal.",
    "This is the exact amount of chaos {subject} deserved.",
    "For the record, {subject} happened with excellent timing.",
]

FALLBACK_CLOSERS = [
    "Clean result, dramatic aftertaste.",
    "No notes—except several emotional ones.",
    "We can all pretend that was completely normal.",
    "The facts are stable; the vibes are doing cartwheels.",
    "I am being very calm about this, visibly.",
]


class _SafeTemplateValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def parse_color(value: str | None, fallback: int = 0x67E8F9) -> int:
    if not value:
        return fallback
    try:
        return int(str(value).strip().replace("#", ""), 16)
    except (TypeError, ValueError):
        return fallback


def _avatar_url(user: Any | None) -> str:
    if user is None:
        return ""
    avatar = getattr(user, "display_avatar", None) or getattr(user, "avatar", None)
    return str(getattr(avatar, "url", "") or "")


def _actor_name(user: Any | None) -> str:
    if user is None:
        return "Discord user"
    return str(
        getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or "Discord user"
    )


def _format_template(
    template: Any, values: Mapping[str, Any], fallback: str = ""
) -> str:
    raw = str(template if template is not None else fallback)
    normalized = _SafeTemplateValues(
        {key: str(value or "") for key, value in values.items()}
    )
    try:
        return raw.format_map(normalized).strip()
    except (ValueError, KeyError):
        return fallback.strip()


def template_profile(
    config: dict[str, Any], template_key: str | None, kind: str
) -> dict[str, Any]:
    templates = (
        (config.get("messageTemplates") or {})
        if isinstance(config.get("messageTemplates"), dict)
        else {}
    )
    global_profile = (
        (templates.get("global") or {})
        if isinstance(templates.get("global"), dict)
        else {}
    )
    requested_key = str(template_key or kind or "info")

    def resolve(profile_key: str, visited: set[str] | None = None) -> dict[str, Any]:
        visited = set(visited or ())
        if profile_key in visited:
            return dict(global_profile)
        visited.add(profile_key)
        if profile_key == "global":
            return dict(global_profile)

        selected = (
            (templates.get(profile_key) or {})
            if isinstance(templates.get(profile_key), dict)
            else {}
        )
        parent_key = TEMPLATE_PARENT_KEYS.get(profile_key, "global")
        parent = resolve(parent_key, visited)
        if bool(selected.get("inheritGlobal", False)):
            return parent
        return dict(ChainMap(selected, parent))

    return resolve(requested_key)


def render_feedback_template(
    config: dict[str, Any],
    *,
    title: str | None,
    description: str,
    kind: str,
    template_key: str | None = None,
    actor: Any | None = None,
    provider: str | None = None,
    source_note: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    appearance = config.get("appearance", {})
    presentation = config.get("presentation", {})
    profile = template_profile(config, template_key, kind)
    base_title = str(title or KIND_TITLES.get(kind, KIND_TITLES["info"]))
    actor_name = _actor_name(actor)

    raw_footer = appearance.get("embedFooter")
    footer = str(
        "Conan Gray Bot • soft-pop control room" if raw_footer is None else raw_footer
    ).strip()
    notes: list[str] = []
    if (
        provider
        and config.get("ai", {}).get("includeProviderFooter", True)
        and bool(profile.get("showProvider", True))
    ):
        notes.append(f"AI via {provider}")
    if source_note and bool(profile.get("showSourceNote", True)):
        notes.append(source_note)
    if notes:
        footer = f"{footer} • {' • '.join(notes)}" if footer else " • ".join(notes)

    values: dict[str, Any] = {
        "title": base_title,
        "description": str(description or "").strip(),
        "response": str(description or "").strip(),
        "footer": footer,
        "actor": actor_name,
        "user": actor_name,
        "provider": provider or "",
        "source": source_note or "",
        "kind": kind,
        "system": template_key or kind,
    }
    if context:
        values.update(context)

    accent = parse_color(appearance.get("accentColor"))
    semantic_color = (
        KIND_COLORS.get(kind, accent)
        if presentation.get("semanticColors", True)
        else accent
    )
    profile_color = str(profile.get("color") or "").strip()
    color = (
        parse_color(profile_color, semantic_color) if profile_color else semantic_color
    )

    timestamp_default = bool(
        presentation.get("showTimestamp", appearance.get("embedShowTimestamp", True))
    )
    requester_default = bool(presentation.get("showRequester", True))
    fields_default = bool(presentation.get("richDetailFields", True))

    return {
        "key": str(template_key or kind or "info"),
        "profile": profile,
        "useEmbed": bool(profile.get("useEmbed", True))
        and bool(presentation.get("embedEverywhere", True)),
        "title": _format_template(
            profile.get("titleTemplate", "{title}"), values, base_title
        )[:256],
        "description": _format_template(
            profile.get("descriptionTemplate", "{description}"),
            values,
            str(description or ""),
        )[:4096],
        "footer": _format_template(
            profile.get("footerTemplate", "{footer}"), values, footer
        )[:2048],
        "author": _format_template(
            profile.get("authorTemplate", "Requested by {actor}"),
            values,
            f"Requested by {actor_name}",
        )[:256],
        "thumbnail": _format_template(
            profile.get("thumbnailUrl") or appearance.get("embedThumbnailUrl") or "",
            values,
            "",
        ),
        "color": color,
        "showTimestamp": timestamp_default and bool(profile.get("showTimestamp", True)),
        "showRequester": requester_default and bool(profile.get("showRequester", True)),
        "showFields": fields_default and bool(profile.get("showFields", True)),
        "values": values,
    }


def build_feedback_embed(
    config: dict[str, Any],
    *,
    title: str | None = None,
    description: str = "",
    kind: str = "info",
    template_key: str | None = None,
    fields: Iterable[tuple[str, str, bool]] | None = None,
    actor: Any | None = None,
    provider: str | None = None,
    source_note: str | None = None,
    image_url: str | None = None,
    thumbnail_url: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> discord.Embed:
    rendered = render_feedback_template(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        actor=actor,
        provider=provider,
        source_note=source_note,
        context=context,
    )

    field_rows = [
        (str(name), str(value), bool(inline))
        for name, value, inline in (fields or [])
        if value is not None and str(value).strip()
    ]
    final_description = rendered["description"]
    if field_rows and not rendered["showFields"]:
        flat_fields = "\n".join(f"**{name}:** {value}" for name, value, _ in field_rows)
        final_description = "\n\n".join(
            part for part in [final_description, flat_fields] if part
        )
        field_rows = []

    embed = discord.Embed(
        title=rendered["title"][:256] or None,
        description=final_description[:4096] or None,
        color=rendered["color"],
        timestamp=datetime.now(timezone.utc) if rendered["showTimestamp"] else None,
    )
    for name, value, inline in field_rows:
        embed.add_field(name=name[:256], value=value[:1024], inline=inline)

    if rendered["showRequester"] and actor is not None:
        avatar_url = _avatar_url(actor)
        if avatar_url:
            embed.set_author(name=rendered["author"], icon_url=avatar_url)
        else:
            embed.set_author(name=rendered["author"])

    thumbnail = str(thumbnail_url or rendered["thumbnail"] or "").strip()
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image_url:
        embed.set_image(url=str(image_url))
    if rendered["footer"]:
        embed.set_footer(text=rendered["footer"][:2048])
    if embed.author.name:
        embed.set_author(
            name=embed.author.name[:256], icon_url=embed.author.icon_url or None
        )
    # Discord limits all embed text together to 6000 characters and 25 fields.
    budget = max(
        0,
        6000
        - len(embed.title or "")
        - len(embed.footer.text or "")
        - len(embed.author.name or ""),
    )
    embed.description = (embed.description or "")[:budget] or None
    budget -= len(embed.description or "")
    original_fields = list(embed.fields)
    embed.clear_fields()
    for field in original_fields[:25]:
        name = (field.name or "")[: min(256, max(0, budget - 1))]
        budget -= len(name)
        value = (field.value or "")[: min(1024, budget)]
        if not name or not value:
            break
        embed.add_field(name=name, value=value, inline=field.inline)
        budget -= len(value)
    return embed


def feedback_plain_text(
    config: dict[str, Any],
    *,
    title: str,
    description: str,
    kind: str,
    template_key: str | None,
    fields: Iterable[tuple[str, str, bool]] | None,
    actor: Any | None,
    provider: str | None,
    source_note: str | None,
    context: Mapping[str, Any] | None = None,
) -> str:
    rendered = render_feedback_template(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        actor=actor,
        provider=provider,
        source_note=source_note,
        context=context,
    )
    field_text = "\n".join(f"**{name}:** {value}" for name, value, _ in fields or [])
    return "\n\n".join(
        part
        for part in [
            f"**{rendered['title']}**" if rendered["title"] else "",
            rendered["description"],
            field_text,
            f"-# {rendered['footer']}" if rendered["footer"] else "",
        ]
        if part
    )[:2000]


def fallback_pool(feature: str, outcome: str = "", pool_size: int = 50) -> list[str]:
    size = max(1, min(int(pool_size or 50), 50))
    subject = FEATURE_SUBJECTS.get(feature, FEATURE_SUBJECTS["command"])
    if outcome:
        subject = f"{subject} ({outcome})"
    pool = [
        f"{opening.format(subject=subject)} {closer}"
        for opening in FALLBACK_OPENINGS
        for closer in FALLBACK_CLOSERS
    ]
    return pool[:size]


async def interpret_action(
    config: dict[str, Any],
    *,
    feature: str,
    outcome: str,
    facts: str,
    actor_name: str = "",
) -> tuple[str, str]:
    presentation = config.get("presentation", {})
    enabled = bool(presentation.get("aiActionInterpretation", True))
    scope_key = "interpretGames" if feature in GAME_FEATURES else "interpretFunctions"
    enabled = enabled and bool(presentation.get(scope_key, True))
    pool_size = max(1, min(int(presentation.get("fallbackPoolSize") or 50), 50))

    if enabled:
        narrator_config = copy.deepcopy(config)
        narrator_ai = narrator_config.setdefault("ai", {})
        narrator_ai["personality"] = str(
            presentation.get("actionNarrationPrompt")
            or (
                "You are the presentation voice of a Conan Gray-inspired Discord bot. "
                "Use dry, self-aware wit, tender observation, slightly awkward charm, and soft-pop drama. "
                "Never claim to be Conan Gray, never invent private facts, and never contradict the deterministic result."
            )
        )
        narrator_ai["maxOutputTokens"] = max(
            40, min(int(presentation.get("narrationMaxTokens") or 140), 300)
        )
        narrator_ai["temperature"] = max(
            0.0, min(float(presentation.get("narrationTemperature") or 0.9), 1.5)
        )
        prompt = (
            f"Feature: {feature}\nOutcome: {outcome}\nExact facts: {facts}\n"
            f"Requester: {actor_name or 'unknown'}\n\n"
            "Write 1-3 concise sentences of reaction only. Preserve every exact fact. "
            "Do not add headings, lists, quotes, fake memories, or a sign-off."
        )
        try:
            timeout_seconds = max(
                2.0, min(float(presentation.get("narrationTimeoutSeconds") or 8), 20.0)
            )
            text, provider = await asyncio.wait_for(
                ask_ai(
                    narrator_config,
                    [],
                    prompt,
                    "This is isolated function/game narration. It must not read or modify conversation memory.",
                ),
                timeout=timeout_seconds,
            )
            text = " ".join(str(text).split()).strip()
            if text:
                return text[:700], f"AI narration: {provider}"
        except (AIProviderError, Exception):
            pass

    return random.choice(
        fallback_pool(feature, outcome, pool_size)
    ), f"Fallback pool: {pool_size}"


def _interaction_context(
    interaction: discord.Interaction, context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    merged = {
        "channel": getattr(getattr(interaction, "channel", None), "name", "channel"),
        "guild": getattr(getattr(interaction, "guild", None), "name", "server"),
    }
    if context:
        merged.update(context)
    return merged


def _message_context(
    message: discord.Message, context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    merged = {
        "channel": getattr(getattr(message, "channel", None), "name", "channel"),
        "guild": getattr(getattr(message, "guild", None), "name", "server"),
    }
    if context:
        merged.update(context)
    return merged


async def send_interaction_feedback(
    interaction: discord.Interaction,
    config: dict[str, Any],
    *,
    title: str,
    description: str = "",
    kind: str = "info",
    template_key: str | None = None,
    fields: Iterable[tuple[str, str, bool]] | None = None,
    ephemeral: bool = False,
    view: discord.ui.View | None = None,
    provider: str | None = None,
    source_note: str | None = None,
    edit_original: bool = False,
    context: Mapping[str, Any] | None = None,
) -> Any:
    context = _interaction_context(interaction, context)
    rendered = render_feedback_template(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        actor=getattr(interaction, "user", None),
        provider=provider,
        source_note=source_note,
        context=context,
    )
    if rendered["useEmbed"]:
        embed = build_feedback_embed(
            config,
            title=title,
            description=description,
            kind=kind,
            template_key=template_key,
            fields=fields,
            actor=getattr(interaction, "user", None),
            provider=provider,
            source_note=source_note,
            context=context,
        )
        if edit_original:
            kwargs: dict[str, Any] = {"content": None, "embed": embed}
            if view is not None:
                kwargs["view"] = view
            return await interaction.edit_original_response(**kwargs)
        if interaction.response.is_done():
            kwargs = {"embed": embed, "ephemeral": ephemeral}
            if view is not None:
                kwargs["view"] = view
            return await interaction.followup.send(**kwargs)
        kwargs = {"embed": embed, "ephemeral": ephemeral}
        if view is not None:
            kwargs["view"] = view
        return await interaction.response.send_message(**kwargs)

    content = feedback_plain_text(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        fields=fields,
        actor=getattr(interaction, "user", None),
        provider=provider,
        source_note=source_note,
        context=context,
    )
    if edit_original:
        kwargs = {"content": content, "embed": None}
        if view is not None:
            kwargs["view"] = view
        return await interaction.edit_original_response(**kwargs)
    if interaction.response.is_done():
        kwargs = {"content": content, "ephemeral": ephemeral}
        if view is not None:
            kwargs["view"] = view
        return await interaction.followup.send(**kwargs)
    kwargs = {"content": content, "ephemeral": ephemeral}
    if view is not None:
        kwargs["view"] = view
    return await interaction.response.send_message(**kwargs)


async def send_message_feedback(
    message: discord.Message,
    config: dict[str, Any],
    *,
    title: str,
    description: str = "",
    kind: str = "info",
    template_key: str | None = None,
    fields: Iterable[tuple[str, str, bool]] | None = None,
    provider: str | None = None,
    source_note: str | None = None,
    mention_author: bool = False,
    context: Mapping[str, Any] | None = None,
) -> Any:
    context = _message_context(message, context)
    rendered = render_feedback_template(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        actor=getattr(message, "author", None),
        provider=provider,
        source_note=source_note,
        context=context,
    )
    if rendered["useEmbed"]:
        embed = build_feedback_embed(
            config,
            title=title,
            description=description,
            kind=kind,
            template_key=template_key,
            fields=fields,
            actor=getattr(message, "author", None),
            provider=provider,
            source_note=source_note,
            context=context,
        )
        return await message.reply(embed=embed, mention_author=mention_author)
    content = feedback_plain_text(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        fields=fields,
        actor=getattr(message, "author", None),
        provider=provider,
        source_note=source_note,
        context=context,
    )
    return await message.reply(content, mention_author=mention_author)


def components_v2_media_payload(
    config: dict[str, Any],
    *,
    title: str,
    description: str,
    kind: str,
    template_key: str,
    fields: Iterable[tuple[str, str, bool]] | None,
    actor: Any | None,
    provider: str | None,
    source_note: str | None,
    filename: str,
    media_description: str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rendered = render_feedback_template(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        actor=actor,
        provider=provider,
        source_note=source_note,
        context=context,
    )
    children: list[dict[str, Any]] = []
    heading = f"## {rendered['title']}" if rendered["title"] else ""
    requester = (
        f"-# {rendered['author']}"
        if rendered["showRequester"] and actor is not None
        else ""
    )
    intro = "\n".join(
        part for part in [requester, heading, rendered["description"]] if part
    )
    if intro:
        children.append({"type": 10, "content": intro[:4000]})
    children.append(
        {
            "type": 12,
            "items": [
                {
                    "media": {"url": f"attachment://{filename}"},
                    "description": media_description[:1024],
                    "spoiler": False,
                }
            ],
        }
    )
    field_lines = [
        f"**{name}:** {value}"
        for name, value, _ in (fields or [])
        if str(value).strip()
    ]
    if field_lines or rendered["footer"]:
        children.append({"type": 14, "divider": True, "spacing": 1})
        details = "\n".join(field_lines)
        footer = f"-# {rendered['footer']}" if rendered["footer"] else ""
        children.append(
            {
                "type": 10,
                "content": "\n".join(part for part in [details, footer] if part)[:4000],
            }
        )
    return {
        "flags": 1 << 15,
        "components": [
            {"type": 17, "accent_color": rendered["color"], "components": children}
        ],
        "attachments": [
            {"id": 0, "filename": filename, "description": media_description[:1024]}
        ],
        "allowed_mentions": {"parse": []},
    }


async def send_interaction_inline_media_card(
    interaction: discord.Interaction,
    config: dict[str, Any],
    *,
    file_path: str | Path,
    filename: str,
    title: str,
    description: str,
    kind: str = "media",
    template_key: str = "media",
    fields: Iterable[tuple[str, str, bool]] | None = None,
    provider: str | None = None,
    source_note: str | None = None,
    media_description: str = "Video attachment",
    edit_original: bool = True,
    context: Mapping[str, Any] | None = None,
) -> Any:
    """Send an uploaded video inside a styled Discord Components V2 media card.

    Discord does not let applications set an embed's ``video`` field. A Media
    Gallery inside a Container is the supported inline-video equivalent.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(path)

    rendered = render_feedback_template(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        actor=getattr(interaction, "user", None),
        provider=provider,
        source_note=source_note,
        context=context,
    )
    requester = f"-# {rendered['author']}" if rendered["showRequester"] else ""
    heading = f"## {rendered['title']}" if rendered["title"] else ""
    intro = "\n".join(
        part for part in [requester, heading, rendered["description"]] if part
    )

    attachment = discord.File(
        path, filename=filename, description=media_description[:1024]
    )
    gallery_item = discord.components.MediaGalleryItem(
        attachment,
        description=media_description[:1024],
        spoiler=False,
    )
    gallery: discord.ui.MediaGallery[Any] = discord.ui.MediaGallery(gallery_item)
    children: list[Any] = []
    if intro:
        children.append(discord.ui.TextDisplay(intro[:4000]))
    children.append(gallery)

    field_lines = [
        f"**{name}:** {value}"
        for name, value, _ in (fields or [])
        if str(value).strip()
    ]
    if field_lines or rendered["footer"]:
        children.append(discord.ui.Separator())
        details = "\n".join(field_lines)
        footer = f"-# {rendered['footer']}" if rendered["footer"] else ""
        children.append(
            discord.ui.TextDisplay(
                "\n".join(part for part in [details, footer] if part)[:4000]
            )
        )

    container = discord.ui.Container(*children, accent_color=rendered["color"])
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)

    try:
        if edit_original:
            return await interaction.edit_original_response(
                content=None,
                embed=None,
                attachments=[attachment],
                view=view,
            )
        if interaction.response.is_done():
            return await interaction.followup.send(
                file=attachment, view=view, wait=True
            )
        return await interaction.response.send_message(file=attachment, view=view)
    finally:
        attachment.close()


async def send_message_inline_media_card(
    message: discord.Message,
    config: dict[str, Any],
    *,
    file_path: str | Path,
    filename: str,
    title: str,
    description: str,
    kind: str = "media",
    template_key: str = "media",
    fields: Iterable[tuple[str, str, bool]] | None = None,
    provider: str | None = None,
    source_note: str | None = None,
    media_description: str = "Video attachment",
    context: Mapping[str, Any] | None = None,
) -> Any:
    """Send an uploaded video in a Components V2 card from a normal message flow."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(path)

    rendered = render_feedback_template(
        config,
        title=title,
        description=description,
        kind=kind,
        template_key=template_key,
        actor=getattr(message, "author", None),
        provider=provider,
        source_note=source_note,
        context=_message_context(message, context),
    )
    requester = f"-# {rendered['author']}" if rendered["showRequester"] else ""
    heading = f"## {rendered['title']}" if rendered["title"] else ""
    intro = "\n".join(
        part for part in [requester, heading, rendered["description"]] if part
    )

    attachment = discord.File(
        path, filename=filename, description=media_description[:1024]
    )
    gallery_item = discord.components.MediaGalleryItem(
        attachment,
        description=media_description[:1024],
        spoiler=False,
    )
    gallery: discord.ui.MediaGallery[Any] = discord.ui.MediaGallery(gallery_item)
    children: list[Any] = []
    if intro:
        children.append(discord.ui.TextDisplay(intro[:4000]))
    children.append(gallery)

    field_lines = [
        f"**{name}:** {value}"
        for name, value, _ in (fields or [])
        if str(value).strip()
    ]
    if field_lines or rendered["footer"]:
        children.append(discord.ui.Separator())
        details = "\n".join(field_lines)
        footer = f"-# {rendered['footer']}" if rendered["footer"] else ""
        children.append(
            discord.ui.TextDisplay(
                "\n".join(part for part in [details, footer] if part)[:4000]
            )
        )

    container = discord.ui.Container(*children, accent_color=rendered["color"])
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)

    try:
        return await message.channel.send(
            file=attachment,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    finally:
        attachment.close()
