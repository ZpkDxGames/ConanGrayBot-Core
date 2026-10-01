from __future__ import annotations

import asyncio
import copy
from contextlib import suppress
from typing import Any

import discord

from ..config import Settings
from ..firebase_client import FirestoreStore, MemoryStore
from .common import log


class PresenceMixin:
    change_presence: Any
    is_closed: Any
    presence_rotation_entry: Any
    presence_rotation_generation: Any
    presence_rotation_guild_id: Any
    presence_rotation_index: Any
    presence_rotation_task: Any
    settings: Settings
    store: MemoryStore | FirestoreStore

    @staticmethod
    def _presence_entries(presence: dict[str, Any]) -> list[dict[str, Any]]:
        raw_entries = (
            (presence.get("entries") or [])
            if isinstance(presence.get("entries"), list)
            else []
        )
        entries: list[dict[str, Any]] = []
        for raw in raw_entries[:20]:
            if not isinstance(raw, dict) or raw.get("enabled", True) is False:
                continue
            entries.append(
                {
                    "enabled": True,
                    "status": str(raw.get("status") or "online").lower(),
                    "activityType": str(raw.get("activityType") or "listening").lower(),
                    "activityText": str(raw.get("activityText") or "").strip()[:128],
                    "streamUrl": str(raw.get("streamUrl") or "").strip()[:500],
                }
            )
        if entries:
            return entries
        return [
            {
                "enabled": True,
                "status": str(presence.get("status") or "online").lower(),
                "activityType": str(
                    presence.get("activityType") or "listening"
                ).lower(),
                "activityText": str(
                    presence.get("activityText") or "dramatic bridge sections"
                ).strip()[:128],
                "streamUrl": str(presence.get("streamUrl") or "").strip()[:500],
            }
        ]

    @staticmethod
    def _presence_status(value: str) -> discord.Status:
        return {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
        }.get(str(value or "online").lower(), discord.Status.online)

    @staticmethod
    def _presence_activity(entry: dict[str, Any]) -> discord.BaseActivity | None:
        activity_text = str(entry.get("activityText") or "").strip()[:128]
        if not activity_text:
            return None
        activity_type_name = str(entry.get("activityType") or "listening").lower()
        if activity_type_name == "streaming":
            stream_url = str(entry.get("streamUrl") or "").strip()[:500]
            if stream_url:
                return discord.Streaming(name=activity_text, url=stream_url)
        activity_type = {
            "playing": discord.ActivityType.playing,
            "streaming": discord.ActivityType.streaming,
            "listening": discord.ActivityType.listening,
            "watching": discord.ActivityType.watching,
            "competing": discord.ActivityType.competing,
        }.get(activity_type_name, discord.ActivityType.listening)
        return discord.Activity(type=activity_type, name=activity_text)

    async def _apply_presence_entry(
        self, entry: dict[str, Any], *, index: int = 0
    ) -> None:
        await self.change_presence(
            status=self._presence_status(str(entry.get("status") or "online")),
            activity=self._presence_activity(entry),
        )
        self.presence_rotation_index = index
        self.presence_rotation_entry = copy.deepcopy(entry)

    async def _stop_presence_rotation(self) -> None:
        self.presence_rotation_generation += 1
        task = self.presence_rotation_task
        self.presence_rotation_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _presence_rotation_loop(
        self,
        guild_id: str,
        entries: list[dict[str, Any]],
        interval_seconds: int,
        generation: int,
    ) -> None:
        index = 0
        try:
            while (
                generation == self.presence_rotation_generation and not self.is_closed()
            ):
                await asyncio.sleep(interval_seconds)
                if generation != self.presence_rotation_generation or self.is_closed():
                    break
                index = (index + 1) % len(entries)
                try:
                    await self._apply_presence_entry(entries[index], index=index)
                    log.info(
                        "Presence rotated for guild %s: %s %s",
                        guild_id,
                        entries[index].get("activityType"),
                        entries[index].get("activityText"),
                    )
                except (discord.HTTPException, RuntimeError):
                    log.exception(
                        "Could not rotate Discord presence for guild %s", guild_id
                    )
        except asyncio.CancelledError:
            raise

    def presence_rotation_payload(self) -> dict[str, Any]:
        task = self.presence_rotation_task
        entry = self.presence_rotation_entry or {}
        return {
            "active": bool(task and not task.done()),
            "guildId": self.presence_rotation_guild_id,
            "currentIndex": self.presence_rotation_index,
            "current": copy.deepcopy(entry),
        }

    async def apply_configured_presence(
        self, guild_id: int | str | None = None
    ) -> None:
        target_guild = str(guild_id or self.settings.guild_id or "global")
        config = await self.store.get_config(target_guild)
        presence = (
            (config.get("presence", {}) or {})
            if isinstance(config.get("presence"), dict)
            else {}
        )
        entries = self._presence_entries(presence)
        try:
            interval_seconds = max(
                15, min(86400, int(presence.get("intervalSeconds") or 60))
            )
        except (TypeError, ValueError):
            interval_seconds = 60
        rotation_enabled = (
            bool(presence.get("rotationEnabled", False)) and len(entries) > 1
        )

        await self._stop_presence_rotation()
        self.presence_rotation_guild_id = target_guild
        self.presence_rotation_index = 0
        await self._apply_presence_entry(entries[0], index=0)

        if rotation_enabled:
            generation = self.presence_rotation_generation
            self.presence_rotation_task = asyncio.create_task(
                self._presence_rotation_loop(
                    target_guild, entries, interval_seconds, generation
                ),
                name=f"presence-rotation:{target_guild}",
            )
            log.info(
                "Presence rotation enabled for guild %s with %s entries every %ss",
                target_guild,
                len(entries),
                interval_seconds,
            )
        else:
            log.info("Applied static Discord presence for guild %s", target_guild)
