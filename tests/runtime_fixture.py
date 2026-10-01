"""Isolated integration runtime: real API/auth/storage, fictional Discord/provider ports."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock


class Guild:
    channels = []

    async def fetch_member(self, actor):
        return SimpleNamespace(roles=[SimpleNamespace(id=456)] if actor == 789 else [])

    async def fetch_channels(self):
        return []


class Bot:
    command_sync_status = "ok"
    command_sync_error = None
    registered_command_names = ["ping", "weather"]

    def is_ready(self):
        return True

    def is_closed(self):
        return False

    def get_guild(self, guild):
        return Guild() if guild == 123 else None

    async def apply_configured_presence(self, guild):
        pass

    async def rebuild_application_commands(self, *args, **kwargs):
        return self.registered_command_names

    def presence_rotation_payload(self):
        return {"active": False, "currentIndex": 0, "current": {}}

    async def close(self):
        pass


def application():
    os.environ.update(
        ENVIRONMENT="development",
        DISCORD_BOT_TOKEN="",
        CORE_SERVICE_TOKEN="integration-service-" + "a" * 40,
        MEDIA_STREAM_SIGNING_KEY="integration-media-" + "b" * 40,
        DISCORD_GUILD_ID="123",
        DISCORD_STAFF_ROLE_ID="456",
        AI_CHANNEL_ID="456",
    )
    from backend.config import get_settings

    get_settings.cache_clear()
    from backend.api import app
    from backend.firebase_client import MemoryStore
    from backend.management import runtime, sandbox

    runtime.settings = get_settings()
    runtime.store = MemoryStore()
    runtime.discord_bot = Bot()
    sandbox.ask_ai = AsyncMock(return_value=("Sandbox fixture reply", "groq"))

    async def seed():
        for i in range(60):
            await runtime.store.add_log(
                "123", "fixture.event", {"actorId": "789", "requestId": str(i)}
            )

    asyncio.run(seed())
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(application(), host="127.0.0.1", port=8001, access_log=False)
