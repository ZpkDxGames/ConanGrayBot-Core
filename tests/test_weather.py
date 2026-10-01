from __future__ import annotations

import unittest

from backend import bot as bot_module
from backend.firebase_client import merge_bot_config
from backend.weather import (
    OpenWeatherClient,
    extract_weather_location,
    is_weather_question,
    natural_weather_reply,
    normalize_units,
    weather_fields,
)


class WeatherIntentTests(unittest.TestCase):
    def test_weather_intent_detection(self):
        self.assertTrue(is_weather_question("what's the weather today?"))
        self.assertTrue(is_weather_question("is it raining in london?"))
        self.assertTrue(is_weather_question("how cold is it"))
        self.assertFalse(is_weather_question("this game is kind of dramatic"))

    def test_explicit_location_extraction(self):
        self.assertEqual(
            extract_weather_location("what's the weather in Uberaba, MG, BR?"),
            "Uberaba, MG, BR",
        )
        self.assertEqual(
            extract_weather_location("is it raining in London today?"), "London"
        )
        self.assertEqual(
            extract_weather_location("forecast for Tokyo tomorrow"), "Tokyo"
        )
        self.assertEqual(extract_weather_location("what's the weather here?"), "")

    def test_unit_selection(self):
        self.assertEqual(normalize_units("auto", "BR"), "metric")
        self.assertEqual(normalize_units("auto", "US"), "imperial")
        self.assertEqual(normalize_units("celsius", "US"), "metric")
        self.assertEqual(normalize_units("fahrenheit", "BR"), "imperial")


class WeatherReportTests(unittest.TestCase):
    def setUp(self):
        self.location = type(
            "Location",
            (),
            {
                "as_dict": lambda self: {
                    "name": "Uberaba",
                    "state": "Minas Gerais",
                    "country": "BR",
                    "latitude": -19.75,
                    "longitude": -47.93,
                    "label": "Uberaba, Minas Gerais, BR",
                }
            },
        )()
        self.current = {
            "dt": 1_800_000_000,
            "timezone": -10800,
            "main": {
                "temp": 23.4,
                "feels_like": 24.2,
                "temp_min": 22.0,
                "temp_max": 25.0,
                "humidity": 61,
                "pressure": 1015,
            },
            "weather": [{"description": "broken clouds", "icon": "04d"}],
            "wind": {"speed": 3.5},
            "visibility": 10000,
            "clouds": {"all": 75},
            "sys": {"sunrise": 1_799_970_000, "sunset": 1_800_015_000},
        }
        self.forecast = {
            "city": {"timezone": -10800},
            "list": [
                {
                    "dt": 1_800_003_600,
                    "main": {"temp": 24.0},
                    "weather": [{"description": "broken clouds"}],
                    "pop": 0.15,
                },
                {
                    "dt": 1_800_014_400,
                    "main": {"temp": 20.0},
                    "weather": [{"description": "light rain"}],
                    "pop": 0.65,
                },
            ],
        }

    def test_report_preserves_weather_facts(self):
        report = OpenWeatherClient._normalize_report(
            self.location,
            self.current,
            self.forecast,
            units="metric",
            forecast_hours=12,
        )
        self.assertEqual(report["location"]["label"], "Uberaba, Minas Gerais, BR")
        self.assertEqual(round(report["current"]["temperature"]), 23)
        self.assertEqual(report["current"]["description"], "broken clouds")
        self.assertEqual(
            round(report["forecast"]["precipitationProbability"] * 100), 65
        )
        self.assertEqual(report["temperatureLabel"], "°C")

        reply = natural_weather_reply(report)
        self.assertIn("23°c", reply.lower())
        self.assertIn("uberaba", reply.lower())
        self.assertIn("65%", reply)

        fields = weather_fields(report)
        self.assertTrue(
            any(name == "Rain chance" and value == "65%" for name, value, _ in fields)
        )


class WeatherConfigurationTests(unittest.TestCase):
    def test_weather_defaults_are_migrated_into_existing_guilds(self):
        config = merge_bot_config({"ai": {"channelId": "123"}})
        self.assertTrue(config["weather"]["enabled"])
        self.assertTrue(config["weather"]["aiDetectionEnabled"])
        self.assertEqual(config["weather"]["units"], "auto")
        self.assertEqual(config["weather"]["forecastHours"], 12)
        self.assertTrue(config["commands"]["weather"])

    def test_location_priority_is_explicit_saved_then_guild_default(self):
        config = merge_bot_config(
            {
                "weather": {
                    "defaultLocation": "Paris, FR",
                    "userLocations": {
                        "42": {
                            "query": "-19.750000,-47.930000",
                            "label": "Uberaba, BR",
                        },
                    },
                }
            }
        )
        self.assertEqual(
            bot_module.ConanBot._weather_location_for_user(config, 42, "Tokyo, JP"),
            ("Tokyo, JP", "explicit"),
        )
        self.assertEqual(
            bot_module.ConanBot._weather_location_for_user(config, 42, ""),
            ("-19.750000,-47.930000", "saved"),
        )
        self.assertEqual(
            bot_module.ConanBot._weather_location_for_user(config, 99, ""),
            ("Paris, FR", "guild_default"),
        )

    def test_command_catalog_contains_weather(self):
        entry = next(
            row for row in bot_module.COMMAND_CATALOG if row["key"] == "weather"
        )
        self.assertEqual(entry["name"], "weather")
        self.assertEqual(entry["category"], "Utility")
        self.assertIn("forecast", entry["description"].lower())

    def test_workspace_has_openweather_key_setting(self):
        from unittest.mock import patch

        from backend.config import Settings

        with patch.dict(
            "os.environ", {"OPENWEATHER_API_KEY": "isolated-weather-fixture"}
        ):
            self.assertEqual(Settings().openweather_api_key, "isolated-weather-fixture")


if __name__ == "__main__":
    unittest.main()
