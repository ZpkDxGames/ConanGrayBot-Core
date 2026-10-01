import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import DEFAULT_BOT_CONFIG
from backend.discord_bot import games, responses
from backend.discord_bot.client import ConanBot
from backend.firebase_client import MemoryStore
from backend.game_limits import GameLimiter, game_slot


@pytest.mark.parametrize(
    "feature",
    ["coinflip", "eightball", "rps", "guesssong", "wouldyourather", "tictactoe"],
)
@pytest.mark.parametrize(
    "category,parent_category,allowed",
    [
        (None, None, False),
        ("other", None, False),
        ("123", None, True),
        (None, "123", True),
    ],
)
@pytest.mark.asyncio
async def test_shared_category_policy(
    monkeypatch, feature, category, parent_category, allowed
):
    config = copy.deepcopy(DEFAULT_BOT_CONFIG)
    config["games"]["allowedCategoryId"] = "123"
    interaction = SimpleNamespace(
        client=SimpleNamespace(settings=SimpleNamespace(allowed_category_id="")),
        channel=SimpleNamespace(
            category_id=category, parent=SimpleNamespace(category_id=parent_category)
        ),
    )
    monkeypatch.setattr(
        responses, "get_interaction_config", AsyncMock(return_value=config)
    )
    feedback = AsyncMock()
    monkeypatch.setattr(responses, "send_interaction_feedback", feedback)
    assert await responses.ensure_command_enabled(interaction, feature) is allowed
    assert feedback.await_count == int(not allowed)


@pytest.mark.asyncio
async def test_runtime_configuration_lookup_has_runtime_import():
    store = MemoryStore()
    bot = ConanBot(store)
    try:
        result = await responses.get_interaction_config(
            SimpleNamespace(client=bot, guild_id=123)
        )
        assert result["schemaVersion"] == 4
    finally:
        await bot.close()


def test_limiter_expiry_capacity_and_scope():
    now = [0.0]
    limiter = GameLimiter(2, lambda: now[0])
    first = limiter.acquire("1", "2", 1)
    assert first is not None
    assert limiter.acquire("1", "2", 1) is None
    assert limiter.acquire("2", "2", 1) is not None
    assert limiter.acquire("3", "2", 1) is None
    now[0] = 121
    assert limiter.acquire("3", "2", 1) is not None
    assert len(limiter.entries) == 1


@pytest.mark.asyncio
async def test_slot_reservation_is_atomic_and_released_after_failure(monkeypatch):
    from backend import game_limits

    feedback = AsyncMock()
    monkeypatch.setattr(game_limits, "send_interaction_feedback", feedback)
    bot = SimpleNamespace(game_limiter=GameLimiter())
    interaction = SimpleNamespace(client=bot, guild_id=1, channel_id=2)
    config = {"games": {"maxActiveGamesPerChannel": 1}}
    entered = asyncio.Event()
    done = asyncio.Event()

    async def running():
        async with game_slot(interaction, config) as lease:
            assert lease is not None
            entered.set()
            await done.wait()
            raise RuntimeError("fictional failure")

    task = asyncio.create_task(running())
    await entered.wait()
    async with game_slot(interaction, config) as lease:
        assert lease is None
    done.set()
    with pytest.raises(RuntimeError):
        await task
    assert not bot.game_limiter.entries
    feedback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_slot_releases_reservation():
    limiter = GameLimiter()
    interaction = SimpleNamespace(
        client=SimpleNamespace(game_limiter=limiter), guild_id=1, channel_id=2
    )
    entered = asyncio.Event()

    async def running():
        async with game_slot(interaction, {}) as lease:
            assert lease is not None
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(running())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not limiter.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_tictactoe_releases_held_slot_on_finish_or_timeout(monkeypatch, timeout):
    limiter = GameLimiter()
    lease = limiter.acquire("1", "2", 1)
    assert lease is not None
    lease.hold(300)
    view = games.TicTacToeView(123, config={}, lease=lease)
    if timeout:
        await view.on_timeout()
    else:
        view.board[:3] = ["X", "X", "X"]
        monkeypatch.setattr(
            games, "interpret_action", AsyncMock(return_value=("Winner", "fixture"))
        )
        await view.finish_or_update(
            SimpleNamespace(user=None, edit_original_response=AsyncMock())
        )
    assert not limiter.entries
    assert view.is_finished()
    assert all(child.disabled for child in view.children)


def test_guess_completion_releases_only_matching_round():
    limiter = GameLimiter()
    first = limiter.acquire("1", "2", 2)
    second = limiter.acquire("1", "2", 2)
    assert first is not None and second is not None
    first.hold(600, "message-1")
    second.hold(600, "message-2")
    limiter.release_game("1", "2", "message-1")
    assert first.token not in limiter.entries
    assert second.token in limiter.entries
