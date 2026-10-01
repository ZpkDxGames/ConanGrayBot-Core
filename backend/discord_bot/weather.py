from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..presentation import (
    send_interaction_feedback,
)
from ..weather import (
    WeatherError,
    natural_weather_reply,
    weather_fields,
)
from .responses import ensure_command_enabled, get_interaction_config

if TYPE_CHECKING:
    from .client import ConanBot


def make_weather_command(bot: ConanBot) -> app_commands.Command:
    unit_choices = [
        app_commands.Choice(name="Automatic", value="auto"),
        app_commands.Choice(name="Celsius", value="metric"),
        app_commands.Choice(name="Fahrenheit", value="imperial"),
    ]

    @app_commands.command(
        name="weather", description="Show current weather and a short forecast."
    )
    @app_commands.describe(
        location="City, state, country, ZIP code, or coordinates. Leave blank for your saved/default place.",
        units="Use automatic local units, Celsius, or Fahrenheit.",
        remember="Save this location as your personal default for future weather questions.",
    )
    @app_commands.choices(units=unit_choices)
    async def weather_command(
        interaction: discord.Interaction,
        location: str | None = None,
        units: app_commands.Choice[str] | None = None,
        remember: bool = False,
    ) -> None:
        if not await ensure_command_enabled(interaction, "weather"):
            return
        config = await get_interaction_config(interaction)
        weather_config = (
            (config.get("weather", {}) or {})
            if isinstance(config.get("weather"), dict)
            else {}
        )
        if not weather_config.get("enabled", True):
            await send_interaction_feedback(
                interaction,
                config,
                title="Weather is disabled",
                description="The weather feature is turned off from the dashboard.",
                kind="warning",
                ephemeral=True,
            )
            return

        location_query, location_source = bot._weather_location_for_user(
            config, interaction.user.id, location or ""
        )
        if not location_query:
            await send_interaction_feedback(
                interaction,
                config,
                title="I need a location first",
                description="Use `/weather location:city, state, country remember:true` once, or set a server default on the dashboard.",
                kind="warning",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        selected_units = (
            units.value if units else str(weather_config.get("units") or "auto")
        )
        try:
            report = await bot.weather_client.get_weather(
                location_query,
                units=selected_units,
                language=str(weather_config.get("language") or "en"),
                forecast_hours=int(weather_config.get("forecastHours") or 12),
            )
        except WeatherError as exc:
            await send_interaction_feedback(
                interaction,
                config,
                title="The forecast disappeared",
                description=str(exc),
                kind="error",
                ephemeral=True,
                edit_original=True,
            )
            await bot.store.add_log(
                str(interaction.guild_id or bot.settings.guild_id or "global"),
                "weather.failed",
                {
                    "authorId": str(interaction.user.id),
                    "code": exc.code,
                    "surface": "command",
                },
            )
            return

        remembered = False
        if remember and weather_config.get("allowUserSavedLocations", True):
            await bot._remember_weather_location(
                interaction.guild_id or bot.settings.guild_id or "global",
                interaction.user.id,
                config,
                report,
            )
            remembered = True

        place = str((report.get("location") or {}).get("label") or "Weather")
        fields = weather_fields(
            report, details=bool(weather_config.get("showDetails", True))
        )
        if remembered:
            fields.append(
                (
                    "Saved location",
                    "This is now your default for conversational weather questions.",
                    False,
                )
            )
        fields.append(("Data", "Weather data © OpenWeather", False))
        await send_interaction_feedback(
            interaction,
            config,
            title=f"Weather in {place}",
            description=natural_weather_reply(report, details=False),
            kind="info",
            template_key="command",
            fields=fields,
            edit_original=True,
            context={"feature": "weather", "location": place},
        )
        await bot.store.add_log(
            str(interaction.guild_id or bot.settings.guild_id or "global"),
            "weather.lookup",
            {
                "authorId": str(interaction.user.id),
                "location": place,
                "locationSource": location_source,
                "remembered": remembered,
                "surface": "command",
            },
        )

    return weather_command
