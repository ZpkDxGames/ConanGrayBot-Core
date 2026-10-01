from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from typing import Any

import discord
from discord.ext import commands

from ..config import get_settings
from ..firebase_client import FirestoreStore, MemoryStore
from ..state import TTLRegistry
from ..weather import (
    OpenWeatherClient,
)
from .ai_chat import AiChatMixin
from .common import BotControlCallback, log
from .game_events import GameEventsMixin
from .media_events import MediaEventsMixin
from .presence import PresenceMixin
from .registry import application_command_factories
from .weather_events import WeatherEventsMixin


class ConanBot(
    PresenceMixin,
    AiChatMixin,
    WeatherEventsMixin,
    GameEventsMixin,
    MediaEventsMixin,
    commands.Bot,
):
    def __init__(
        self,
        store: MemoryStore | FirestoreStore,
        control_callback: BotControlCallback | None = None,
        drive_archive: Any | None = None,
    ) -> None:
        self.settings = get_settings()
        intents = discord.Intents.default()
        intents.message_content = self.settings.enable_message_content_intent
        intents.guilds = True
        intents.members = self.settings.enable_members_intent

        super().__init__(command_prefix="c!", intents=intents)
        self.store = store
        self.control_callback = control_callback
        self.drive_archive = drive_archive
        self.weather_client = OpenWeatherClient(self.settings.openweather_api_key)
        self.command_sync_status = "pending"
        self.command_sync_error: str | None = None
        self.registered_command_names: list[str] = []
        self.media_archive_locks: TTLRegistry[asyncio.Lock] = TTLRegistry(1024, 3600)
        self.ai_cooldowns: TTLRegistry[float] = TTLRegistry(1024, 86400)
        self.ai_session_locks: TTLRegistry[asyncio.Lock] = TTLRegistry(1024, 3600)
        self.guessing_game_locks: TTLRegistry[asyncio.Lock] = TTLRegistry(1024, 3600)
        self.presence_rotation_task: asyncio.Task[None] | None = None
        self.presence_rotation_generation = 0
        self.presence_rotation_index = 0
        self.presence_rotation_guild_id = ""
        self.presence_rotation_entry: dict[str, Any] | None = None
        self.spontaneous_chat_task: asyncio.Task[None] | None = None
        self.talkin_last_activity: TTLRegistry[float] = TTLRegistry(1024, 86400)
        self.talkin_last_spontaneous: TTLRegistry[float] = TTLRegistry(1024, 86400)
        self.talkin_last_spontaneous_check: TTLRegistry[float] = TTLRegistry(
            1024, 86400
        )
        log.info(
            "Discord intents configured: message_content=%s, members=%s",
            intents.message_content,
            intents.members,
        )

    async def setup_hook(self) -> None:
        try:
            await self.rebuild_application_commands(
                self.settings.guild_id or None, sync=True, force=False
            )
        except discord.Forbidden as exc:
            self.command_sync_status = "forbidden"
            self.command_sync_error = f"Forbidden: {exc}"
            log.warning(
                "Slash command sync failed: missing access. Bot will continue online."
            )
        except discord.HTTPException as exc:
            self.command_sync_status = "failed"
            self.command_sync_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "Slash command sync failed, but bot will continue online: %s", exc
            )

    async def rebuild_application_commands(
        self,
        guild_id: int | str | None = None,
        *,
        sync: bool = True,
        force: bool = True,
    ) -> list[str]:
        """Rebuild the Discord slash-command tree from dashboard enable states."""
        target_guild_id = str(guild_id or self.settings.guild_id or "global")
        config = await self.store.get_config(target_guild_id)
        enabled_map = config.get("commands", {})

        self.tree.clear_commands(guild=None)
        registered: list[str] = []
        for key, factory in application_command_factories():
            if not bool(enabled_map.get(key, True)):
                continue
            command = factory(self)
            self.tree.add_command(command)
            registered.append(key)

        if not sync:
            return registered

        manifest = json.dumps(
            [command.to_dict(self.tree) for command in self.tree.get_commands()],
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(manifest.encode()).hexdigest()
        if (
            not force
            and await self.store.get_command_manifest(target_guild_id) == digest
        ):
            self.command_sync_status = "unchanged"
            self.command_sync_error = None
            self.registered_command_names = registered
            return registered
        if guild_id or self.settings.guild_id:
            guild_object = discord.Object(id=int(guild_id or self.settings.guild_id))
            self.tree.clear_commands(guild=guild_object)
            self.tree.copy_global_to(guild=guild_object)
            await self.tree.sync(guild=guild_object)
            log.info(
                "Slash commands synced to guild %s: %s",
                guild_object.id,
                ", ".join(registered),
            )
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally: %s", ", ".join(registered))

        await self.store.set_command_manifest(target_guild_id, digest)
        self.command_sync_status = "ok"
        self.command_sync_error = None
        self.registered_command_names = registered
        return registered

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown"
        )
        await self.apply_configured_presence()
        if self.spontaneous_chat_task is None or self.spontaneous_chat_task.done():
            self.spontaneous_chat_task = asyncio.create_task(
                self._spontaneous_chat_loop(),
                name="talkin-spontaneous-conversation",
            )

    async def close(self) -> None:
        task = self.spontaneous_chat_task
        self.spontaneous_chat_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._stop_presence_rotation()
        await self.weather_client.close()
        await super().close()

    async def request_control(
        self, action: str, guild_id: int | str, actor_id: int | str
    ) -> dict[str, Any]:
        if not self.control_callback:
            raise RuntimeError(
                "Bot lifecycle controls are unavailable in this deployment."
            )
        return await self.control_callback(action, str(guild_id), str(actor_id))

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if message.author.bot or not message.guild:
            return

        handled_guess = await self._handle_guessing_game_reply(message)
        if not handled_guess:
            await self._handle_ai_message(message)
        await self._handle_media_archive(message)
        await self._handle_media_triggers(message)
        await self.process_commands(message)

    async def _config_for(self, guild_id: int | str) -> dict[str, Any]:
        return await self.store.get_config(str(guild_id))

    async def clear_ai_session(
        self, guild_id: int | str, channel_id: int | str
    ) -> None:
        async with self._ai_session_lock(guild_id, channel_id):
            await self.store.clear_session(str(guild_id), str(channel_id))

    async def clear_all_ai_sessions(self, guild_id: int | str) -> int:
        return await self.store.clear_guild_sessions(str(guild_id))


async def run_bot(store: Any) -> None:
    settings = get_settings()
    if not settings.discord_token:
        log.warning("DISCORD_BOT_TOKEN is missing; bot will not start.")
        return
    bot = ConanBot(store)
    await bot.start(settings.discord_token)
