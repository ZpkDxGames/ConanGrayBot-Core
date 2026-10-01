import asyncio
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.config import Settings
from backend.firebase_client import MemoryStore
from backend.management import runtime


class Bot:
    def __init__(self):
        self.closed = False
        self.close_calls = 0
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.user = None

    def is_closed(self):
        return self.closed

    async def start(self, token):
        self.started.set()
        await self.finished.wait()

    async def close(self):
        self.close_calls += 1
        self.closed = True
        self.finished.set()


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "settings",
        Settings(discord_token="fictional-token", discord_application_id="123"),
    )
    monkeypatch.setattr(runtime, "store", MemoryStore())
    monkeypatch.setattr(runtime, "discord_bot", None)
    monkeypatch.setattr(runtime, "bot_task", None)
    monkeypatch.setattr(runtime, "bot_lifecycle_lock", asyncio.Lock())
    created = []

    def factory():
        bot = Bot()
        created.append(bot)
        return bot

    monkeypatch.setattr(runtime, "_new_discord_bot", factory)
    return created


@pytest.mark.asyncio
async def test_concurrent_start_creates_one_gateway_and_stop_releases_it(fixture):
    first, second = await asyncio.gather(
        runtime.start_discord_bot(guild_id="123", actor_id="789"),
        runtime.start_discord_bot(),
    )
    assert first["status"] == second["status"] == "starting"
    assert len(fixture) == 1
    await fixture[0].started.wait()
    result = await runtime.stop_discord_bot(guild_id="123", actor_id="789")
    assert result["status"] == "closed"
    assert fixture[0].close_calls == 1 and runtime.bot_task is None
    assert (await runtime.store.list_logs("123"))[0]["payload"]["actorId"] == "789"
    await runtime.stop_discord_bot()
    assert fixture[0].close_calls == 1


@pytest.mark.asyncio
async def test_restart_closes_old_gateway_before_replacement(fixture):
    await runtime.start_discord_bot()
    await fixture[0].started.wait()
    result = await runtime.restart_discord_bot(guild_id="123", actor_id="789")
    assert result["action"] == "restart" and len(fixture) == 2
    assert fixture[0].closed and runtime.discord_bot is fixture[1]
    await fixture[1].started.wait()
    await runtime.stop_discord_bot()


@pytest.mark.asyncio
async def test_missing_token_cannot_create_gateway(fixture, monkeypatch):
    monkeypatch.setattr(runtime, "settings", Settings(discord_token=""))
    with pytest.raises(RuntimeError, match="not configured"):
        await runtime.start_discord_bot()
    assert not fixture and runtime.bot_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,target",
    [
        ("start", "start_discord_bot"),
        ("restart", "restart_discord_bot"),
        ("shutdown", "stop_discord_bot"),
    ],
)
async def test_control_dispatch_preserves_actor(monkeypatch, action, target):
    handler = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(runtime, target, handler)
    assert await runtime.handle_bot_control(action, "123", "789") == {"ok": True}
    handler.assert_awaited_once_with("discord", "123", "789")


@pytest.mark.asyncio
async def test_unknown_control_does_not_change_gateway(fixture):
    with pytest.raises(ValueError):
        await runtime.handle_bot_control("fictional-unknown", "123", "789")
    assert not fixture


def test_invite_is_scoped_to_application_and_guild(fixture):
    result = parse_qs(urlsplit(runtime.invite_url("456")).query)
    assert result["client_id"] == ["123"] and result["guild_id"] == ["456"]
    assert result["disable_guild_select"] == ["true"]
    assert "guild_id" not in parse_qs(urlsplit(runtime.invite_url()).query)
