import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord import app_commands

from backend.config import DEFAULT_BOT_CONFIG
from backend.discord_bot import games, responses, utility


@pytest.fixture
def command_fixture(monkeypatch):
    config = copy.deepcopy(DEFAULT_BOT_CONFIG)
    config["games"]["allowedCategoryId"] = ""
    config["games"]["wouldYouRatherEnabled"] = True
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        channel=SimpleNamespace(id=456, category_id=None),
        user=SimpleNamespace(id=789, mention="<@789>", name="Fixture"),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(allowed_category_id=""), latency=0.15
    )
    for module in [games, utility]:
        monkeypatch.setattr(
            module, "ensure_command_enabled", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            module, "get_interaction_config", AsyncMock(return_value=config)
        )
        monkeypatch.setattr(module, "send_action_result", AsyncMock())
    monkeypatch.setattr(games, "send_interaction_feedback", AsyncMock())
    return config, interaction, bot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feature,args",
    [
        ("coinflip", ()),
        ("eightball", ("Will it work?",)),
        ("rps", (app_commands.Choice(name="Rock", value="rock"),)),
        ("wouldyourather", ()),
    ],
)
async def test_game_toggle_prevents_action(command_fixture, feature, args):
    config, interaction, bot = command_fixture
    key = "wouldYouRather" if feature == "wouldyourather" else feature
    config["games"][key + "Enabled"] = False
    await getattr(games, "make_" + feature + "_command")(bot).callback(
        interaction, *args
    )
    games.send_action_result.assert_not_awaited()
    games.send_interaction_feedback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot_move,outcome",
    [("rock", "draw"), ("paper", "bot wins"), ("scissors", "you win")],
)
async def test_rps_deterministic_truth_fields(
    command_fixture, monkeypatch, bot_move, outcome
):
    _, interaction, bot = command_fixture
    monkeypatch.setattr(games.random, "choice", lambda values: bot_move)
    await games.make_rps_command(bot).callback(
        interaction, app_commands.Choice(name="Rock", value="rock")
    )
    assert games.send_action_result.await_args.kwargs["outcome"] == outcome
    assert "rock" in games.send_action_result.await_args.kwargs["facts"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feature,args",
    [("coinflip", ()), ("eightball", ("Question?",)), ("wouldyourather", ())],
)
async def test_games_use_configured_pool_and_exact_result(
    command_fixture, monkeypatch, feature, args
):
    config, interaction, bot = command_fixture
    config["games"]["coinflipHeadsLabel"] = "Configured heads"
    config["games"]["eightballAnswers"] = ["Configured answer"]
    monkeypatch.setattr(games.random, "choice", lambda values: values[0])
    await getattr(games, "make_" + feature + "_command")(bot).callback(
        interaction, *args
    )
    games.send_action_result.assert_awaited_once()
    assert games.send_action_result.await_args.kwargs["feature"] == feature
    assert games.send_action_result.await_args.kwargs["facts"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feature,args",
    [
        ("ping", ()),
        ("help", ()),
        ("pun", ()),
        ("motivation", ()),
        ("recommend", ()),
        ("lyrics", (None,)),
    ],
)
async def test_utility_commands_present_result(command_fixture, feature, args):
    _, interaction, bot = command_fixture
    await getattr(utility, "make_" + feature + "_command")(bot).callback(
        interaction, *args
    )
    utility.send_action_result.assert_awaited_once()
    assert utility.send_action_result.await_args.kwargs["feature"] == feature


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "latency,quality",
    [(0.05, "Excellent"), (0.15, "Good"), (0.3, "A little cinematic")],
)
async def test_ping_quality_reports_measured_latency(command_fixture, latency, quality):
    _, interaction, bot = command_fixture
    bot.latency = latency
    await utility.make_ping_command(bot).callback(interaction)
    fields = utility.send_action_result.await_args.kwargs["fields"]
    assert ("Quality", quality, True) in fields


@pytest.mark.asyncio
async def test_disabled_command_guard_is_ephemeral(monkeypatch):
    monkeypatch.setattr(
        responses,
        "get_interaction_config",
        AsyncMock(return_value={"commands": {"ping": False}}),
    )
    notify = AsyncMock()
    monkeypatch.setattr(responses, "send_interaction_feedback", notify)
    assert not await responses.ensure_command_enabled(SimpleNamespace(), "ping")
    assert notify.await_args.kwargs["ephemeral"] is True
