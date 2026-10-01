from __future__ import annotations

from typing import Any

import discord

from ..firebase_client import FirestoreStore, MemoryStore
from ..weather import (
    WeatherError,
    extract_weather_location,
    is_weather_question,
    natural_weather_reply,
)


class WeatherEventsMixin:
    store: MemoryStore | FirestoreStore
    weather_client: Any

    @staticmethod
    def _weather_location_for_user(
        config: dict[str, Any],
        user_id: int | str,
        explicit_location: str = "",
    ) -> tuple[str, str]:
        weather_config = (
            (config.get("weather", {}) or {})
            if isinstance(config.get("weather"), dict)
            else {}
        )
        explicit = " ".join(str(explicit_location or "").split()).strip()
        if explicit:
            return explicit, "explicit"
        saved_locations = weather_config.get("userLocations")
        if isinstance(saved_locations, dict):
            saved = saved_locations.get(str(user_id))
            if isinstance(saved, dict):
                query = str(saved.get("query") or saved.get("label") or "").strip()
                if query:
                    return query, "saved"
            elif isinstance(saved, str) and saved.strip():
                return saved.strip(), "saved"
        default_location = str(weather_config.get("defaultLocation") or "").strip()
        if default_location:
            return default_location, "guild_default"
        return "", "missing"

    async def _remember_weather_location(
        self,
        guild_id: int | str,
        user_id: int | str,
        config: dict[str, Any],
        report: dict[str, Any],
    ) -> dict[str, Any]:
        weather_config = config.setdefault("weather", {})
        saved_locations = weather_config.setdefault("userLocations", {})
        if not isinstance(saved_locations, dict):
            saved_locations = {}
            weather_config["userLocations"] = saved_locations
        location = (
            (report.get("location") or {})
            if isinstance(report.get("location"), dict)
            else {}
        )
        latitude = float(location.get("latitude") or 0)
        longitude = float(location.get("longitude") or 0)
        saved_locations[str(user_id)] = {
            "query": f"{latitude:.6f},{longitude:.6f}",
            "label": str(location.get("label") or ""),
            "country": str(location.get("country") or ""),
        }
        return await self.store.set_config(str(guild_id), config)

    async def _handle_weather_chat(
        self,
        message: discord.Message,
        config: dict[str, Any],
        text: str,
    ) -> bool:
        if message.guild is None:
            return False
        weather_config = (
            (config.get("weather", {}) or {})
            if isinstance(config.get("weather"), dict)
            else {}
        )
        if not weather_config.get("enabled", True) or not weather_config.get(
            "aiDetectionEnabled", True
        ):
            return False
        if not is_weather_question(text):
            return False

        explicit_location = extract_weather_location(text)
        location_query, location_source = self._weather_location_for_user(
            config, message.author.id, explicit_location
        )
        if not location_query:
            await message.reply(
                "tell me a city/state/country first, or use `/weather location:your city remember:true` once so i know what ‘here’ means 💀",
                mention_author=False,
            )
            return True

        try:
            report = await self.weather_client.get_weather(
                location_query,
                units=str(weather_config.get("units") or "auto"),
                language=str(weather_config.get("language") or "en"),
                forecast_hours=int(weather_config.get("forecastHours") or 12),
            )
        except WeatherError as exc:
            await message.reply(
                f"weather betrayed me for a second. {str(exc).lower()}",
                mention_author=False,
            )
            await self.store.add_log(
                str(message.guild.id),
                "weather.failed",
                {
                    "channelId": str(message.channel.id),
                    "authorId": str(message.author.id),
                    "code": exc.code,
                },
            )
            return True

        reply = natural_weather_reply(
            report, details=bool(weather_config.get("showDetails", True))
        )
        reply = f"{reply}\n-# weather data © openweather"
        await message.reply(reply[:2000], mention_author=False)
        await self.store.add_log(
            str(message.guild.id),
            "weather.lookup",
            {
                "channelId": str(message.channel.id),
                "authorId": str(message.author.id),
                "location": str((report.get("location") or {}).get("label") or ""),
                "locationSource": location_source,
                "surface": "conversation",
            },
        )
        return True
