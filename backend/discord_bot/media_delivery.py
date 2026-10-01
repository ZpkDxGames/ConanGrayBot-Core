from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import discord

from ..google_drive import DriveConfigurationError, drive_media_type
from ..media_stream import build_drive_stream_url
from ..presentation import (
    build_feedback_embed,
    send_message_feedback,
    send_message_inline_media_card,
)
from .common import discord_profile_name, log


async def send_random_trigger_media(
    message: discord.Message,
    config: dict[str, Any],
    *,
    trigger_word: str,
    response_text: str,
    requested_type: str,
    drive_archive: Any | None,
) -> dict[str, Any]:
    """Resolve a {random} trigger token against the configured Drive folder."""
    media_config = config.get("media", {})
    folder_id = str(media_config.get("googleDriveFolderId") or "").strip()
    source_token = "{random}" if not requested_type else f"{{random:{requested_type}}}"

    if not folder_id:
        await send_message_feedback(
            message,
            config,
            title="Random media is not configured",
            description="Set and test the Google Drive folder on the dashboard before using a random trigger.",
            kind="warning",
            template_key="trigger",
            fields=[
                ("Trigger", f"`{trigger_word}`", True),
                ("Source", source_token, True),
            ],
        )
        return {"mediaSource": source_token, "mediaStatus": "folder_missing"}

    if drive_archive is None or not getattr(drive_archive, "configured", False):
        await send_message_feedback(
            message,
            config,
            title="Random media is unavailable",
            description="The backend does not have a complete Google Drive authentication configuration.",
            kind="error",
            template_key="trigger",
            fields=[
                ("Trigger", f"`{trigger_word}`", True),
                ("Source", source_token, True),
            ],
        )
        return {"mediaSource": source_token, "mediaStatus": "drive_unavailable"}

    try:
        selected = await drive_archive.random_media_file(
            folder_id,
            media_type=requested_type,
            limit=int(media_config.get("randomCommandListLimit") or 1000),
        )
    except DriveConfigurationError as exc:
        await send_message_feedback(
            message,
            config,
            title="Google Drive needs attention",
            description=str(exc),
            kind="error",
            template_key="trigger",
            fields=[
                ("Trigger", f"`{trigger_word}`", True),
                ("Reason", exc.reason or exc.code, True),
                ("Project", exc.project_id or "Unknown", True),
            ],
        )
        return {"mediaSource": source_token, "mediaStatus": exc.code}
    except Exception as exc:
        log.exception("Could not select random trigger media from Google Drive")
        await send_message_feedback(
            message,
            config,
            title="Random media lookup failed",
            description=f"Google Drive returned {type(exc).__name__}. Check the Drive connection and bot logs.",
            kind="error",
            template_key="trigger",
            fields=[
                ("Trigger", f"`{trigger_word}`", True),
                ("Source", source_token, True),
            ],
        )
        return {"mediaSource": source_token, "mediaStatus": "lookup_failed"}

    if not selected:
        label = (
            "MP4 videos"
            if requested_type == "video"
            else "supported images"
            if requested_type == "image"
            else "supported media"
        )
        await send_message_feedback(
            message,
            config,
            title="The archive is empty",
            description=f"The configured Drive folder has no {label} available for this trigger.",
            kind="warning",
            template_key="trigger",
            fields=[
                ("Trigger", f"`{trigger_word}`", True),
                ("Images", ".png · .webp · .jpg · .jpeg", False),
                ("Videos", ".mp4", False),
            ],
        )
        return {"mediaSource": source_token, "mediaStatus": "empty"}

    file_name = Path(str(selected.get("name") or "media")).name
    file_id = str(selected.get("id") or "")
    selected_type = str(
        selected.get("mediaType")
        or drive_media_type(selected)
        or requested_type
        or "media"
    )
    file_size = int(selected.get("size") or 0)
    configured_limit = (
        max(1, int(media_config.get("randomCommandMaxFileSizeMb") or 25)) * 1024 * 1024
    )
    guild_limit = int(
        getattr(message.guild, "filesize_limit", configured_limit) or configured_limit
    )
    upload_limit = min(configured_limit, guild_limit)
    fields = [
        ("Trigger", f"`{trigger_word}`", True),
        ("Type", selected_type.title(), True),
        ("File", file_name, False),
        ("Size", format_file_size(file_size), True),
        ("Source", "Random Google Drive pull", True),
    ]
    template_context = {
        "filename": file_name,
        "mediaType": selected_type,
        "size": format_file_size(file_size),
        "channel": getattr(message.channel, "name", "channel"),
        "guild": getattr(message.guild, "name", "server"),
        "trigger": trigger_word,
    }

    async def send_as_secure_stream(size_value: int) -> dict[str, Any]:
        stream_url = build_drive_stream_url(file_id, file_name)
        if not stream_url:
            raise ValueError(
                "Large-media streaming is not configured. Set PUBLIC_BASE_URL and keep a dashboard, "
                "Discord, or MEDIA_STREAM_SIGNING_KEY secret configured."
            )
        size_text = format_file_size(size_value)
        template_context["size"] = size_text
        stream_fields = list(fields)
        stream_fields[3] = ("Size", size_text, True)
        stream_fields.append(("Delivery", "Secure direct stream", True))
        link = str(selected.get("webViewLink") or "").strip()
        view = discord.ui.View()
        if link:
            view.add_item(discord.ui.Button(label="Open in Google Drive", url=link))
        embed = build_feedback_embed(
            config,
            title="A media cue just fired",
            description=response_text,
            kind="trigger",
            template_key="trigger",
            fields=stream_fields,
            actor=message.author,
            source_note="Random Drive trigger • secure stream",
            image_url=stream_url if selected_type == "image" else None,
            thumbnail_url=str(selected.get("thumbnailLink") or "")
            if selected_type == "video"
            else None,
            context=template_context,
        )
        send_kwargs: dict[str, Any] = {"embed": embed}
        if selected_type == "video":
            # A raw, signed .mp4 URL lets Discord render its native player above the embed.
            send_kwargs["content"] = stream_url
        if link:
            send_kwargs["view"] = view
        await message.channel.send(**send_kwargs)
        return {
            "mediaSource": source_token,
            "mediaStatus": "streamed",
            "mediaType": selected_type,
            "fileId": file_id,
            "fileName": file_name,
        }

    if file_size and file_size > upload_limit:
        return await send_as_secure_stream(file_size)

    suffix = Path(file_name).suffix or (".mp4" if selected_type == "video" else ".jpg")
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="conan-trigger-media-", suffix=suffix, delete=False
        ) as handle:
            temp_path = handle.name
        await drive_archive.download_file(file_id, temp_path)
        actual_size = Path(temp_path).stat().st_size
        if actual_size > upload_limit:
            return await send_as_secure_stream(actual_size)

        safe_attachment_name = (
            re.sub(r"[^A-Za-z0-9._ -]+", "_", file_name).strip() or f"media{suffix}"
        )
        actual_size_text = format_file_size(actual_size)
        template_context["size"] = actual_size_text
        fields[3] = ("Size", actual_size_text, True)
        actor_name = discord_profile_name(message.author)
        alt_template = str(
            media_config.get("videoAltTextTemplate")
            or "{filename} · requested by {actor}"
        )
        media_alt = (
            alt_template.replace("{filename}", file_name)
            .replace("{actor}", actor_name)
            .replace("{mediaType}", selected_type)
            .replace("{size}", actual_size_text)
        )[:1024]

        if (
            selected_type == "video"
            and str(media_config.get("videoDisplayMode") or "embed_attachment")
            == "inline_card"
        ):
            try:
                await send_message_inline_media_card(
                    message,
                    config,
                    file_path=temp_path,
                    filename=safe_attachment_name,
                    title="A media cue just fired",
                    description=response_text,
                    kind="trigger",
                    template_key="trigger",
                    fields=fields,
                    source_note="Random Drive trigger",
                    media_description=media_alt,
                    context=template_context,
                )
            except Exception:
                log.exception(
                    "Inline random-trigger video failed; falling back to embed plus native attachment"
                )
                attachment = discord.File(
                    temp_path, filename=safe_attachment_name, description=media_alt
                )
                try:
                    embed = build_feedback_embed(
                        config,
                        title="A media cue just fired",
                        description=response_text,
                        kind="trigger",
                        template_key="trigger",
                        fields=fields,
                        actor=message.author,
                        source_note="Random Drive trigger • inline player fallback",
                        thumbnail_url=str(selected.get("thumbnailLink") or "") or None,
                        context=template_context,
                    )
                    await message.channel.send(file=attachment, embed=embed)
                finally:
                    attachment.close()
        else:
            attachment = discord.File(
                temp_path, filename=safe_attachment_name, description=media_alt
            )
            try:
                image_url = (
                    f"attachment://{safe_attachment_name}"
                    if selected_type == "image"
                    else None
                )
                embed = build_feedback_embed(
                    config,
                    title="A media cue just fired",
                    description=response_text,
                    kind="trigger",
                    template_key="trigger",
                    fields=fields,
                    actor=message.author,
                    source_note="Random Drive trigger",
                    image_url=image_url,
                    thumbnail_url=str(selected.get("thumbnailLink") or "")
                    if selected_type == "video"
                    else None,
                    context=template_context,
                )
                await message.channel.send(file=attachment, embed=embed)
            finally:
                attachment.close()

        return {
            "mediaSource": source_token,
            "mediaStatus": "sent",
            "mediaType": selected_type,
            "fileId": file_id,
            "fileName": file_name,
        }
    except Exception as exc:
        log.exception("Could not download/send random trigger media")
        await send_message_feedback(
            message,
            config,
            title="Could not send the random media",
            description=str(exc)
            if isinstance(exc, ValueError)
            else "The selected file could not be downloaded or attached to Discord.",
            kind="error",
            template_key="trigger",
            fields=fields,
        )
        return {
            "mediaSource": source_token,
            "mediaStatus": "send_failed",
            "mediaType": selected_type,
            "fileId": file_id,
            "fileName": file_name,
        }
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def format_file_size(value: int | float) -> str:
    size = float(value or 0)
    units = ["B", "KB", "MB", "GB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return (
        f"{size:.0f} {units[index]}"
        if index == 0 or size >= 10
        else f"{size:.1f} {units[index]}"
    )
