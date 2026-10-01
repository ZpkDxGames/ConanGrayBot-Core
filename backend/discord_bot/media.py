from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..google_drive import DriveConfigurationError, drive_media_type
from ..media_stream import build_drive_stream_url
from ..presentation import (
    build_feedback_embed,
    interpret_action,
    send_interaction_feedback,
    send_interaction_inline_media_card,
)
from .common import discord_profile_name, log
from .media_delivery import format_file_size
from .responses import ensure_command_enabled, get_interaction_config

if TYPE_CHECKING:
    from .client import ConanBot


def make_media_command(bot: ConanBot) -> app_commands.Command:
    choices = [
        app_commands.Choice(name="Image", value="image"),
        app_commands.Choice(name="Video", value="video"),
    ]

    @app_commands.command(
        name="media",
        description="Send a random image or video from the configured Google Drive folder.",
    )
    @app_commands.rename(media_type="type")
    @app_commands.describe(
        media_type="Optional media type. Leave empty for either image or video."
    )
    @app_commands.choices(media_type=choices)
    async def media(
        interaction: discord.Interaction,
        media_type: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await ensure_command_enabled(interaction, "media"):
            return
        config = await get_interaction_config(interaction)
        media_config = config.get("media", {})
        configured_channel_id = str(media_config.get("channelId") or "").strip()
        current_channel_id = str(interaction.channel_id or "")

        if not configured_channel_id:
            await send_interaction_feedback(
                interaction,
                config,
                title="Media channel not configured",
                description="Set the Media channel on the dashboard before using this command.",
                kind="warning",
                ephemeral=True,
            )
            return
        if current_channel_id != configured_channel_id:
            await send_interaction_feedback(
                interaction,
                config,
                title="Wrong channel",
                description="The random-media command only works inside the configured media channel.",
                kind="warning",
                fields=[("Media channel", f"<#{configured_channel_id}>", False)],
                ephemeral=True,
            )
            return
        folder_id = str(media_config.get("googleDriveFolderId") or "").strip()
        if not folder_id:
            await send_interaction_feedback(
                interaction,
                config,
                title="Drive folder not configured",
                description="Set and test the Google Drive folder on the dashboard first.",
                kind="warning",
                ephemeral=True,
            )
            return
        if not bot.drive_archive or not getattr(bot.drive_archive, "configured", False):
            await send_interaction_feedback(
                interaction,
                config,
                title="Google Drive credentials missing",
                description="The backend does not have Google Drive service-account credentials.",
                kind="error",
                ephemeral=True,
            )
            return

        requested_type = media_type.value if media_type else ""
        await interaction.response.defer(thinking=True)
        try:
            selected = await bot.drive_archive.random_media_file(
                folder_id,
                media_type=requested_type,
                limit=int(media_config.get("randomCommandListLimit") or 1000),
            )
        except DriveConfigurationError as exc:
            await send_interaction_feedback(
                interaction,
                config,
                title="Google Drive needs attention",
                description=str(exc),
                kind="error",
                fields=[
                    ("Reason", exc.reason or exc.code, True),
                    ("Project", exc.project_id or "Unknown", True),
                ],
                ephemeral=True,
            )
            return
        except Exception as exc:
            log.exception("Could not list random media from Google Drive")
            await send_interaction_feedback(
                interaction,
                config,
                title="Media lookup failed",
                description=f"Google Drive returned {type(exc).__name__}. Check the dashboard connection test and logs.",
                kind="error",
                ephemeral=True,
            )
            return

        if not selected:
            label = (
                "MP4 videos"
                if requested_type == "video"
                else "supported images"
                if requested_type == "image"
                else "supported media"
            )
            await send_interaction_feedback(
                interaction,
                config,
                title="Nothing to pull from the archive",
                description=f"The configured Drive folder has no {label} yet.",
                kind="warning",
                fields=[
                    ("Images", ".png · .webp · .jpg · .jpeg", False),
                    ("Videos", ".mp4", False),
                ],
                ephemeral=True,
            )
            return

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
            max(1, int(media_config.get("randomCommandMaxFileSizeMb") or 25))
            * 1024
            * 1024
        )
        guild_limit = int(
            getattr(interaction.guild, "filesize_limit", configured_limit)
            or configured_limit
        )
        upload_limit = min(configured_limit, guild_limit)
        fields = [
            ("Type", selected_type.title(), True),
            ("File", file_name, False),
            ("Size", format_file_size(file_size), True),
            ("Source", "Google Drive", True),
        ]

        async def send_as_secure_stream(size_value: int) -> None:
            stream_url = build_drive_stream_url(file_id, file_name)
            if not stream_url:
                raise ValueError(
                    "Large-media streaming is not configured. Set PUBLIC_BASE_URL and keep a dashboard, "
                    "Discord, or MEDIA_STREAM_SIGNING_KEY secret configured."
                )
            size_text = format_file_size(size_value)
            stream_fields = list(fields)
            stream_fields[2] = ("Size", size_text, True)
            stream_fields.append(("Delivery", "Secure direct stream", True))
            narration, source_note = await interpret_action(
                config,
                feature="media",
                outcome=f"random {selected_type} selected",
                facts=f"Selected file: {file_name}. Type: {selected_type}. Size: {size_text}.",
                actor_name=discord_profile_name(interaction.user),
            )
            template_context = {
                "filename": file_name,
                "mediaType": selected_type,
                "size": size_text,
                "channel": getattr(interaction.channel, "name", "channel"),
                "guild": getattr(interaction.guild, "name", "server"),
            }
            link = str(selected.get("webViewLink") or "").strip()
            view = discord.ui.View()
            if link:
                view.add_item(discord.ui.Button(label="Open in Google Drive", url=link))
            embed = build_feedback_embed(
                config,
                title="Random archive pull",
                description=narration,
                kind="media",
                template_key="media",
                fields=stream_fields,
                actor=interaction.user,
                source_note=f"{source_note} • secure Drive stream",
                image_url=stream_url if selected_type == "image" else None,
                thumbnail_url=str(selected.get("thumbnailLink") or "")
                if selected_type == "video"
                else None,
                context=template_context,
            )
            await interaction.edit_original_response(
                content=stream_url if selected_type == "video" else None,
                embed=embed,
                view=view if link else None,
                attachments=[],
            )
            await bot.store.add_log(
                str(interaction.guild_id or bot.settings.guild_id or "global"),
                "media.random_streamed",
                {
                    "fileId": file_id,
                    "fileName": file_name,
                    "mediaType": selected_type,
                    "size": size_value,
                    "channelId": current_channel_id,
                    "requesterId": str(interaction.user.id),
                },
            )

        if file_size and file_size > upload_limit:
            await send_as_secure_stream(file_size)
            return

        suffix = Path(file_name).suffix or (
            ".mp4" if selected_type == "video" else ".jpg"
        )
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                prefix="conan-random-media-", suffix=suffix, delete=False
            ) as handle:
                temp_path = handle.name
            await bot.drive_archive.download_file(file_id, temp_path)
            actual_size = Path(temp_path).stat().st_size
            if actual_size > upload_limit:
                await send_as_secure_stream(actual_size)
                return

            narration, source_note = await interpret_action(
                config,
                feature="media",
                outcome=f"random {selected_type} selected",
                facts=f"Selected file: {file_name}. Type: {selected_type}. Size: {format_file_size(actual_size)}.",
                actor_name=discord_profile_name(interaction.user),
            )
            safe_attachment_name = (
                re.sub(r"[^A-Za-z0-9._ -]+", "_", file_name).strip() or f"media{suffix}"
            )
            template_context = {
                "filename": file_name,
                "mediaType": selected_type,
                "size": format_file_size(actual_size),
                "channel": getattr(interaction.channel, "name", "channel"),
                "guild": getattr(interaction.guild, "name", "server"),
            }
            actor_name = discord_profile_name(interaction.user)
            alt_template = str(
                media_config.get("videoAltTextTemplate")
                or "{filename} · requested by {actor}"
            )
            media_alt = (
                alt_template.replace("{filename}", file_name)
                .replace("{actor}", actor_name)
                .replace("{mediaType}", selected_type)
                .replace("{size}", format_file_size(actual_size))
            )[:1024]

            if (
                selected_type == "video"
                and str(media_config.get("videoDisplayMode") or "embed_attachment")
                == "inline_card"
            ):
                try:
                    await send_interaction_inline_media_card(
                        interaction,
                        config,
                        file_path=temp_path,
                        filename=safe_attachment_name,
                        title="Random archive pull",
                        description=narration,
                        kind="media",
                        template_key="media",
                        fields=fields,
                        source_note=source_note,
                        media_description=media_alt,
                        edit_original=True,
                        context=template_context,
                    )
                except Exception:
                    log.exception(
                        "Discord inline video card failed; falling back to an embed plus native attachment"
                    )
                    attachment = discord.File(
                        temp_path, filename=safe_attachment_name, description=media_alt
                    )
                    try:
                        embed = build_feedback_embed(
                            config,
                            title="Random archive pull",
                            description=narration,
                            kind="media",
                            template_key="media",
                            fields=fields,
                            actor=interaction.user,
                            source_note=f"{source_note} • inline player fallback",
                            thumbnail_url=str(selected.get("thumbnailLink") or "")
                            or None,
                            context=template_context,
                        )
                        await interaction.edit_original_response(
                            content=None, embed=embed, attachments=[attachment]
                        )
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
                        title="Random archive pull",
                        description=narration,
                        kind="media",
                        template_key="media",
                        fields=fields,
                        actor=interaction.user,
                        source_note=source_note,
                        image_url=image_url,
                        thumbnail_url=str(selected.get("thumbnailLink") or "")
                        if selected_type == "video"
                        else None,
                        context=template_context,
                    )
                    await interaction.edit_original_response(
                        content=None, embed=embed, attachments=[attachment]
                    )
                finally:
                    attachment.close()
            await bot.store.add_log(
                str(interaction.guild_id or bot.settings.guild_id or "global"),
                "media.random_sent",
                {
                    "fileId": file_id,
                    "fileName": file_name,
                    "mediaType": selected_type,
                    "channelId": current_channel_id,
                    "requesterId": str(interaction.user.id),
                },
            )
        except Exception as exc:
            log.exception("Could not download/send random Google Drive media")
            await send_interaction_feedback(
                interaction,
                config,
                title="Could not send that media file",
                description=str(exc)
                if isinstance(exc, ValueError)
                else "The file could not be downloaded or attached to Discord.",
                kind="error",
                fields=fields,
                ephemeral=True,
            )
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    return media
