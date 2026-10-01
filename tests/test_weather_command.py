import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord import app_commands

from backend.config import DEFAULT_BOT_CONFIG
from backend.discord_bot import weather
from backend.discord_bot.client import ConanBot
from backend.firebase_client import MemoryStore
from backend.weather import OpenWeatherClient, WeatherError, WeatherLocation


@pytest.fixture
def fixture(monkeypatch):
    config = copy.deepcopy(DEFAULT_BOT_CONFIG)
    config["weather"]["defaultLocation"] = "City"
    report = OpenWeatherClient._normalize_report(
        WeatherLocation("City", "", "US", 40, -70),
        {"main": {"temp": 61}},
        {},
        units="imperial",
        forecast_hours=12,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id="123"),
        store=MemoryStore(),
        weather_client=SimpleNamespace(get_weather=AsyncMock(return_value=report)),
        _weather_location_for_user=ConanBot._weather_location_for_user,
        _remember_weather_location=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=789),
        response=SimpleNamespace(defer=AsyncMock()),
    )
    monkeypatch.setattr(weather, "ensure_command_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        weather, "get_interaction_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(weather, "send_interaction_feedback", AsyncMock())
    return config, bot, interaction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remember,allowed", [(False, True), (True, True), (True, False)]
)
async def test_weather_command_facts_units_and_saved_location_policy(
    fixture, remember, allowed
):
    config, bot, interaction = fixture
    config["weather"]["allowUserSavedLocations"] = allowed
    await weather.make_weather_command(bot).callback(
        interaction,
        units=app_commands.Choice(name="Fahrenheit", value="imperial"),
        remember=remember,
    )
    assert bot.weather_client.get_weather.await_args.kwargs["units"] == "imperial"
    assert bot._remember_weather_location.await_count == int(remember and allowed)
    assert "61°F" in weather.send_interaction_feedback.await_args.kwargs["description"]
    logs = await bot.store.list_logs("123")
    assert logs[0]["event"] == "weather.lookup"


@pytest.mark.asyncio
async def test_weather_provider_error_is_feedback_and_audited(fixture):
    _, bot, interaction = fixture
    bot.weather_client.get_weather.side_effect = WeatherError(
        "Weather unavailable", code="timeout"
    )
    await weather.make_weather_command(bot).callback(interaction)
    assert weather.send_interaction_feedback.await_args.kwargs["kind"] == "error"
    assert (await bot.store.list_logs("123"))[0]["payload"]["code"] == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [True, False])
async def test_disabled_or_missing_location_never_calls_provider(fixture, disabled):
    config, bot, interaction = fixture
    config["weather"].update(enabled=not disabled, defaultLocation="")
    await weather.make_weather_command(bot).callback(interaction)
    bot.weather_client.get_weather.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_saved_location_contract_preserves_concurrent_dashboard_edit():
    store = MemoryStore()
    bot = ConanBot(store)
    stale = await store.get_config("123")
    changed = copy.deepcopy(stale)
    changed["presence"]["activityText"] = "Dashboard edit"
    await store.set_config("123", changed, expected_revision=stale["revision"])
    try:
        result = await bot._remember_weather_location(
            "123",
            789,
            stale,
            {
                "location": {
                    "latitude": 40,
                    "longitude": -70,
                    "label": "City, US",
                    "country": "US",
                }
            },
        )
        assert result["presence"]["activityText"] == "Dashboard edit"
        assert (
            result["weather"]["userLocations"]["789"]["query"] == "40.000000,-70.000000"
        )
        assert bot._weather_location_for_user(result, 789) == (
            "40.000000,-70.000000",
            "saved",
        )
    finally:
        await bot.close()
