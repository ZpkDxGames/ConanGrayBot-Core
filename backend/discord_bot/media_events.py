from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import discord

from ..config import Settings
from ..firebase_client import FirestoreStore, MemoryStore
from ..presentation import (
    send_message_feedback,
)
from ..state import TTLRegistry
from .common import (
    attachment_media_type,
    discord_profile_name,
    log,
    render_media_filename,
)
from .responses import send_trigger


class MediaEventsMixin:
    _config_for: Any
    media_archive_locks: TTLRegistry[asyncio.Lock]
    drive_archive: Any
    settings: Settings
    store: MemoryStore | FirestoreStore

    async def _handle_media_archive(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        key = f"{message.guild.id}:{message.channel.id}"
        lock = self.media_archive_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._archive_media_message(message)

    async def _archive_media_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        attachments = list(getattr(message, "attachments", None) or [])
        if not attachments:
            return

        config = await self._config_for(message.guild.id)
        media_config = config.get("media", {})
        if not media_config.get("enabled", False):
            return

        configured_channel = str(media_config.get("channelId") or "").strip()
        if configured_channel and str(message.channel.id) != configured_channel:
            return

        folder_id = str(media_config.get("googleDriveFolderId") or "").strip()
        if not folder_id:
            await self.store.add_log(
                str(message.guild.id),
                "media.skipped",
                {
                    "reason": "missing_drive_folder",
                    "channelId": str(message.channel.id),
                },
            )
            return
        if self.drive_archive is None or not getattr(
            self.drive_archive, "configured", False
        ):
            await self.store.add_log(
                str(message.guild.id),
                "media.failed",
                {
                    "reason": "drive_not_configured",
                    "channelId": str(message.channel.id),
                },
            )
            if media_config.get("notifyOnFailure", True):
                await send_message_feedback(
                    message,
                    config,
                    title="Media archive unavailable",
                    description="Google Drive credentials are not configured for media archiving.",
                    kind="error",
                    fields=[
                        (
                            "Next step",
                            "Open Media → Google Drive in the dashboard and verify the service-account setup.",
                            False,
                        )
                    ],
                )
            return

        max_bytes = (
            max(1, min(int(media_config.get("maxFileSizeMb") or 100), 256))
            * 1024
            * 1024
        )
        uploaded = 0
        failed = 0
        guild_id = str(message.guild.id)
        profile_name = discord_profile_name(message.author)

        for attachment in attachments:
            record_id = f"{message.id}-{attachment.id}"
            if await self.store.get_media_record(guild_id, record_id):
                continue
            media_type, mime_type = attachment_media_type(attachment)
            if media_type == "image" and not media_config.get("uploadImages", True):
                continue
            if media_type == "video" and not media_config.get("uploadVideos", True):
                continue
            if media_type not in {"image", "video"}:
                continue
            size = int(getattr(attachment, "size", 0) or 0)
            if size > max_bytes:
                failed += 1
                await self.store.add_log(
                    guild_id,
                    "media.rejected",
                    {
                        "reason": "file_too_large",
                        "filename": str(getattr(attachment, "filename", "media")),
                        "size": size,
                        "maxBytes": max_bytes,
                        "messageId": str(message.id),
                    },
                )
                continue

            drive_file = None
            record_saved = False
            temp_path = ""
            try:
                suffix = Path(str(getattr(attachment, "filename", "") or "")).suffix
                with tempfile.NamedTemporaryFile(
                    prefix="conan-media-", suffix=suffix, delete=False
                ) as temporary:
                    temp_path = temporary.name
                await asyncio.wait_for(
                    attachment.save(temp_path, use_cached=True), timeout=30
                )
                if Path(temp_path).stat().st_size > max_bytes:
                    raise ValueError("Downloaded attachment exceeds configured limit")
                drive_name = render_media_filename(
                    str(
                        media_config.get("fileNameTemplate")
                        or "{date}_{messageId}_{filename}"
                    ),
                    attachment,
                    message,
                )
                description = (
                    f"Archived by Conan Gray Bot from Discord server {getattr(message.guild, 'name', message.guild.id)}, "
                    f"channel #{getattr(message.channel, 'name', message.channel.id)}. "
                    f"Uploaded by {profile_name} ({message.author.id}). Message: {getattr(message, 'jump_url', '')}"
                )
                drive_file = await self.drive_archive.upload_file(
                    temp_path,
                    folder_id_or_url=folder_id,
                    file_name=drive_name,
                    mime_type=mime_type,
                    description=description,
                    make_public=bool(media_config.get("makeFilesPublic", False)),
                )
                record = await self.store.add_media_record(
                    guild_id,
                    {
                        "recordId": record_id,
                        "driveFileId": str(drive_file.get("id") or ""),
                        "name": str(drive_file.get("name") or drive_name),
                        "originalName": str(
                            getattr(attachment, "filename", drive_name)
                        ),
                        "mimeType": str(drive_file.get("mimeType") or mime_type),
                        "mediaType": media_type,
                        "size": int(drive_file.get("size") or size),
                        "channelId": str(message.channel.id),
                        "channelName": str(getattr(message.channel, "name", "channel")),
                        "authorId": str(message.author.id),
                        "authorName": profile_name,
                        "messageId": str(message.id),
                        "messageUrl": str(getattr(message, "jump_url", "") or ""),
                        "webViewLink": str(drive_file.get("webViewLink") or ""),
                        "webContentLink": str(drive_file.get("webContentLink") or ""),
                        "thumbnailLink": str(drive_file.get("thumbnailLink") or ""),
                        "publicContentUrl": str(
                            drive_file.get("publicContentUrl") or ""
                        ),
                        "publicThumbnailUrl": str(
                            drive_file.get("publicThumbnailUrl") or ""
                        ),
                        "public": bool(drive_file.get("public", False)),
                    },
                )
                record_saved = True
                uploaded += 1
                await self.store.add_log(
                    guild_id,
                    "media.archived",
                    {
                        "recordId": record.get("recordId"),
                        "driveFileId": record.get("driveFileId"),
                        "filename": record.get("name"),
                        "channelId": str(message.channel.id),
                        "authorId": str(message.author.id),
                    },
                )
            except Exception as exc:
                if drive_file and drive_file.get("id") and not record_saved:
                    try:
                        await self.drive_archive.delete_file(str(drive_file["id"]))
                    except Exception:
                        log.error(
                            "Drive archive compensation failed; operator reconciliation required"
                        )
                failed += 1
                log.exception("Could not archive Discord attachment to Google Drive")
                await self.store.add_log(
                    guild_id,
                    "media.failed",
                    {
                        "filename": str(getattr(attachment, "filename", "media")),
                        "messageId": str(message.id),
                        "error": type(exc).__name__,
                    },
                )
            finally:
                if temp_path:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass

        if uploaded and media_config.get("notifyOnUpload", False):
            template = str(
                media_config.get("successMessageTemplate")
                or "Archived {count} media file(s) to Google Drive."
            )
            await send_message_feedback(
                message,
                config,
                title="Media archived",
                description=template.replace("{count}", str(uploaded)),
                kind="media",
                fields=[
                    ("Uploaded", str(uploaded), True),
                    ("Destination", "Google Drive", True),
                ],
            )
        if failed and media_config.get("notifyOnFailure", True):
            template = str(
                media_config.get("failureMessageTemplate")
                or "I could not archive {count} media file(s). Check the Media page and bot logs."
            )
            await send_message_feedback(
                message,
                config,
                title="Media archive incomplete",
                description=template.replace("{count}", str(failed)),
                kind="error",
                fields=[("Failed files", str(failed), True)],
            )

    async def _handle_media_triggers(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        config = await self._config_for(message.guild.id)
        allowed_category_id = str(
            config.get("games", {}).get("allowedCategoryId")
            or self.settings.allowed_category_id
        )
        if (
            allowed_category_id
            and str(getattr(message.channel, "category_id", "")) != allowed_category_id
        ):
            return

        content = message.content.lower()
        triggers = config.get("triggers") or []
        for trigger in triggers:
            if not trigger.get("enabled", True):
                continue
            word = (trigger.get("word") or "").strip().lower()
            if not word:
                continue
            channel_ids = [str(x) for x in trigger.get("channelIds") or []]
            if channel_ids and str(message.channel.id) not in channel_ids:
                continue
            if re.search(rf"\b{re.escape(word)}\b", content):
                result = await send_trigger(
                    message,
                    config,
                    trigger,
                    drive_archive=self.drive_archive,
                )
                await self.store.add_log(
                    str(message.guild.id),
                    "trigger.fired",
                    {
                        "word": word,
                        "channelId": str(message.channel.id),
                        **(result or {}),
                    },
                )
                break
