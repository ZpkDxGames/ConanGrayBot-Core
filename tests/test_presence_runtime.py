import asyncio
import copy
from unittest.mock import AsyncMock

import discord
import pytest

from backend.config import DEFAULT_BOT_CONFIG
from backend.discord_bot.client import ConanBot
from backend.discord_bot.presence import PresenceMixin
from backend.firebase_client import MemoryStore


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("playing", discord.ActivityType.playing),
        ("watching", discord.ActivityType.watching),
        ("competing", discord.ActivityType.competing),
        ("listening", discord.ActivityType.listening),
        ("unknown", discord.ActivityType.listening),
    ],
)
def test_presence_activity_types(kind, expected):
    activity = PresenceMixin._presence_activity(
        {"activityText": "Fixture", "activityType": kind}
    )
    assert activity.type == expected


def test_streaming_activity_and_empty_activity():
    streaming = PresenceMixin._presence_activity(
        {
            "activityText": "Fixture",
            "activityType": "streaming",
            "streamUrl": "https://twitch.tv/fixture",
        }
    )
    assert isinstance(streaming, discord.Streaming)
    assert PresenceMixin._presence_activity({"activityText": ""}) is None


@pytest.mark.parametrize(
    "raw,status",
    [
        ("online", discord.Status.online),
        ("idle", discord.Status.idle),
        ("dnd", discord.Status.dnd),
        ("invisible", discord.Status.invisible),
        ("unknown", discord.Status.online),
    ],
)
def test_presence_status(raw, status):
    assert PresenceMixin._presence_status(raw) == status


@pytest.mark.asyncio
async def test_presence_rotation_replaces_task_and_closes_cleanly(monkeypatch):
    store = MemoryStore()
    bot = ConanBot(store)
    monkeypatch.setattr(bot, "change_presence", AsyncMock())
    config = copy.deepcopy(DEFAULT_BOT_CONFIG)
    config["presence"]["rotationEnabled"] = True
    config["presence"]["entries"] = [
        {
            "enabled": True,
            "status": "online",
            "activityType": "listening",
            "activityText": "First",
            "streamUrl": "",
        },
        {
            "enabled": True,
            "status": "idle",
            "activityType": "watching",
            "activityText": "Second",
            "streamUrl": "",
        },
    ]
    await store.set_config("123", config)
    try:
        await bot.apply_configured_presence("123")
        first = bot.presence_rotation_task
        await asyncio.sleep(0)
        assert bot.presence_rotation_payload()["active"]
        await bot.apply_configured_presence("123")
        assert first.cancelled() and bot.presence_rotation_task is not first
        assert bot.presence_rotation_payload()["current"]["activityText"] == "First"
        await asyncio.sleep(0)
    finally:
        await bot.close()
    assert bot.presence_rotation_task is None


@pytest.mark.asyncio
async def test_rotation_applies_next_entry_and_stops_on_generation_change(monkeypatch):
    bot = PresenceMixin()
    bot.presence_rotation_generation = 7
    bot.is_closed = lambda: False
    applied = []

    async def apply(entry, *, index):
        applied.append((entry, index))
        bot.presence_rotation_generation = 8

    bot._apply_presence_entry = apply
    real_sleep = asyncio.sleep

    async def tick(interval):
        await real_sleep(0)

    monkeypatch.setattr("backend.discord_bot.presence.asyncio.sleep", tick)
    entries = [{"activityText": "First"}, {"activityText": "Second"}]
    await bot._presence_rotation_loop("123", entries, 15, 7)
    assert applied == [(entries[1], 1)]


def test_presence_filters_invalid_disabled_entries_and_bounds_text():
    entries = PresenceMixin._presence_entries(
        {"entries": [None, {"enabled": False}, {"activityText": "x" * 500}]}
    )
    assert len(entries) == 1 and len(entries[0]["activityText"]) == 128
    fallback = PresenceMixin._presence_entries(
        {"entries": [{"enabled": False}], "activityText": "Fallback"}
    )
    assert fallback[0]["activityText"] == "Fallback"
