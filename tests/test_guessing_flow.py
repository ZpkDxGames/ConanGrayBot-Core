import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import DEFAULT_BOT_CONFIG
from backend.discord_bot import game_events
from backend.firebase_client import MemoryStore
from backend.game_limits import GameLimiter


@pytest.fixture
def fixture(monkeypatch):
    config = copy.deepcopy(DEFAULT_BOT_CONFIG)
    store = MemoryStore()
    bot = game_events.GameEventsMixin()
    bot.store = store
    bot.game_limiter = GameLimiter()
    bot.guessing_game_locks = {}
    bot._config_for = AsyncMock(return_value=config)
    message = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        channel=SimpleNamespace(id=456),
        author=SimpleNamespace(id=789),
        reference=SimpleNamespace(message_id=100),
        content="Heather",
    )
    monkeypatch.setattr(
        game_events,
        "judge_guess_reply",
        AsyncMock(return_value=(True, "deterministic")),
    )
    monkeypatch.setattr(
        game_events,
        "interpret_action",
        AsyncMock(return_value=("Round result", "fixture")),
    )
    monkeypatch.setattr(game_events, "send_message_feedback", AsyncMock())
    return config, store, bot, message


async def seed(store, bot, **changes):
    state = {
        "answer": "Heather",
        "aliases": [],
        "hint": "fixture",
        "starterId": "789",
        "attempts": 0,
        "maxAttempts": 3,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }
    state.update(changes)
    await store.set_guessing_game("123", "456", "100", state)
    lease = bot.game_limiter.acquire("123", "456", 1)
    assert lease is not None
    lease.hold(600, "100")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "correct,attempts,expected_title",
    [
        (True, 0, "You got the song"),
        (False, 0, "Not quite"),
        (False, 2, "The mystery track wins this round"),
    ],
)
async def test_guess_round_truth_attempts_and_lease_cleanup(
    fixture, correct, attempts, expected_title
):
    _, store, bot, message = fixture
    await seed(store, bot, attempts=attempts)
    game_events.judge_guess_reply.return_value = (correct, "deterministic")
    assert await bot._handle_guessing_game_reply(message)
    kwargs = game_events.send_message_feedback.await_args.kwargs
    assert kwargs["title"] == expected_title
    state = await store.get_guessing_game("123", "456", "100")
    ended = correct or attempts == 2
    assert bool(state) is not ended
    assert bool(bot.game_limiter.entries) is not ended
    if not ended:
        assert state["attempts"] == 1
        assert not any(
            "Heather" in value
            for name, value, _ in kwargs["fields"]
            if name != "Your guess"
        )


@pytest.mark.asyncio
async def test_simultaneous_correct_answers_consume_round_once(fixture):
    _, store, bot, message = fixture
    await seed(store, bot)

    async def judge(*args, **kwargs):
        await asyncio.sleep(0)
        return True, "deterministic"

    game_events.judge_guess_reply.side_effect = judge
    assert await asyncio.gather(
        bot._handle_guessing_game_reply(message),
        bot._handle_guessing_game_reply(message),
    ) == [True, True]
    game_events.judge_guess_reply.assert_awaited_once()
    assert not await store.get_guessing_game("123", "456", "100")


@pytest.mark.asyncio
@pytest.mark.parametrize("empty,foreign", [(True, False), (False, True)])
async def test_empty_and_foreign_guesses_do_not_spend_attempts(fixture, empty, foreign):
    config, store, bot, message = fixture
    await seed(store, bot)
    if empty:
        message.content = " "
    if foreign:
        config["games"]["guessSongAllowAnyone"] = False
        message.author.id = 999
    assert await bot._handle_guessing_game_reply(message)
    assert (await store.get_guessing_game("123", "456", "100"))["attempts"] == 0
    game_events.judge_guess_reply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dm", "no-reference", "unknown-round"])
async def test_unrelated_messages_continue_to_normal_routing(fixture, kind):
    _, _, bot, message = fixture
    if kind == "dm":
        message.guild = None
    elif kind == "no-reference":
        message.reference = None
    assert not await bot._handle_guessing_game_reply(message)
