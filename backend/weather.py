from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp


class WeatherError(RuntimeError):
    def __init__(
        self, message: str, *, code: str = "weather_error", status: int = 502
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class WeatherLocation:
    name: str
    state: str
    country: str
    latitude: float
    longitude: float

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.state and self.state.casefold() != self.name.casefold():
            parts.append(self.state)
        if self.country:
            parts.append(self.country)
        return ", ".join(part for part in parts if part)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "country": self.country,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "label": self.label,
        }


_WEATHER_TERMS_RE = re.compile(
    r"\b(?:weather|forecast|temperature|temp|rain|raining|rainy|snow|snowing|sunny|cloudy|wind|windy|humidity|"
    r"storm|stormy|hot|cold|heat|umbrella)\b",
    re.IGNORECASE,
)
_LOCATION_PATTERNS = (
    re.compile(
        r"\b(?:weather|forecast|temperature|temp)\s+(?:in|for|at|near)\s+(.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:raining|rainy|snowing|sunny|cloudy|windy|hot|cold)\s+(?:in|at|near)\s+(.+)$",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:in|for|at|near)\s+([^?]+?)(?:\?|$)", re.IGNORECASE),
)
_TRAILING_TIME_RE = re.compile(
    r"(?:\s+(?:right now|now|today|tonight|tomorrow|this morning|this afternoon|this evening|please))+[?.!\s]*$",
    re.IGNORECASE,
)


def is_weather_question(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(_WEATHER_TERMS_RE.search(value))


def extract_weather_location(text: str) -> str:
    value = " ".join(str(text or "").replace("\n", " ").split()).strip()
    if not value:
        return ""
    for pattern in _LOCATION_PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        candidate = _TRAILING_TIME_RE.sub("", match.group(1)).strip(" ,.!?;:\t")
        candidate = re.sub(
            r"^(?:the\s+)?(?:city|state|country)\s+of\s+", "", candidate, flags=re.I
        )
        if 1 < len(candidate) <= 120 and not _WEATHER_TERMS_RE.fullmatch(candidate):
            return candidate
    return ""


def normalize_units(value: str, country_code: str = "") -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized in {"metric", "c", "celsius"}:
        return "metric"
    if normalized in {"imperial", "f", "fahrenheit"}:
        return "imperial"
    # These countries still commonly use Fahrenheit for public weather reports.
    return "imperial" if country_code.upper() in {"US", "LR", "MM"} else "metric"


def unit_labels(units: str) -> tuple[str, str]:
    return ("°F", "mph") if units == "imperial" else ("°C", "m/s")


def _round_temperature(value: Any) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _format_clock(timestamp: Any, offset_seconds: int) -> str:
    try:
        utc_value = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "—"
    local_value = datetime.fromtimestamp(
        utc_value.timestamp() + int(offset_seconds or 0), tz=timezone.utc
    )
    return local_value.strftime("%H:%M")


def _condition_phrase(description: str) -> str:
    value = str(description or "weather").strip().lower()
    return value or "weather"


class OpenWeatherClient:
    def __init__(self, api_key: str, *, timeout_seconds: float = 12.0) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = max(3.0, min(float(timeout_seconds), 30.0))
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._geocode_cache: dict[str, tuple[float, WeatherLocation]] = {}
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(
                timeout=timeout, raise_for_status=False
            )
        return self._session

    async def _request_json(self, url: str, params: dict[str, Any]) -> Any:
        if not self.configured:
            raise WeatherError(
                "The OpenWeather API key is not configured on the backend.",
                code="not_configured",
                status=503,
            )
        session = await self._get_session()
        query = {**params, "appid": self.api_key}
        try:
            async with session.get(url, params=query) as response:
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise WeatherError(
                "The weather service took too long to answer.",
                code="timeout",
                status=504,
            ) from exc
        except (aiohttp.ClientError, ValueError) as exc:
            raise WeatherError(
                "The weather service could not be reached.",
                code="network_error",
                status=502,
            ) from exc

        if response.status >= 400:
            if response.status == 401:
                raise WeatherError(
                    "The OpenWeather API key was rejected.",
                    code="invalid_key",
                    status=503,
                )
            if response.status == 429:
                raise WeatherError(
                    "The weather request limit was reached. Try again shortly.",
                    code="rate_limited",
                    status=429,
                )
            raise WeatherError(
                "The weather service returned an error.",
                status=response.status,
            )
        return payload

    def _remember_location(self, key: str, location: WeatherLocation) -> None:
        self._geocode_cache[key] = (time.monotonic(), location)
        if len(self._geocode_cache) > 256:
            oldest = sorted(
                self._geocode_cache, key=lambda item: self._geocode_cache[item][0]
            )[:64]
            for item in oldest:
                self._geocode_cache.pop(item, None)

    async def geocode(self, query: str) -> WeatherLocation:
        cleaned = " ".join(str(query or "").split()).strip()
        if not cleaned:
            raise WeatherError(
                "A city, state, or country is needed first.",
                code="location_required",
                status=400,
            )

        cache_key = cleaned.casefold()
        cached = self._geocode_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 86400:
            return cached[1]

        coordinate_match = re.fullmatch(
            r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", cleaned
        )
        if coordinate_match:
            lat = float(coordinate_match.group(1))
            lon = float(coordinate_match.group(2))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise WeatherError(
                    "Those coordinates are outside the valid range.",
                    code="invalid_coordinates",
                    status=400,
                )
            reverse = await self._request_json(
                "https://api.openweathermap.org/geo/1.0/reverse",
                {"lat": lat, "lon": lon, "limit": 1},
            )
            row = reverse[0] if isinstance(reverse, list) and reverse else {}
            location = WeatherLocation(
                name=str(row.get("name") or f"{lat:.3f}, {lon:.3f}"),
                state=str(row.get("state") or ""),
                country=str(row.get("country") or ""),
                latitude=lat,
                longitude=lon,
            )
            self._remember_location(cache_key, location)
            return location

        zip_match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9 -]{1,11})(?:\s*,\s*([A-Za-z]{2}))?", cleaned
        )
        if zip_match and any(character.isdigit() for character in zip_match.group(1)):
            zip_value = zip_match.group(1).replace(" ", "")
            country = str(zip_match.group(2) or "").upper()
            zip_query = f"{zip_value},{country}" if country else zip_value
            payload = await self._request_json(
                "https://api.openweathermap.org/geo/1.0/zip",
                {"zip": zip_query},
            )
            if (
                isinstance(payload, dict)
                and payload.get("lat") is not None
                and payload.get("lon") is not None
            ):
                location = WeatherLocation(
                    name=str(payload.get("name") or cleaned),
                    state="",
                    country=str(payload.get("country") or country),
                    latitude=float(payload.get("lat") or 0),
                    longitude=float(payload.get("lon") or 0),
                )
                self._remember_location(cache_key, location)
                return location

        payload = await self._request_json(
            "https://api.openweathermap.org/geo/1.0/direct",
            {"q": cleaned, "limit": 5},
        )
        if not isinstance(payload, list) or not payload:
            raise WeatherError(
                f"I couldn't match a place called {cleaned!r}.",
                code="location_not_found",
                status=404,
            )
        row = payload[0]
        location = WeatherLocation(
            name=str(row.get("name") or cleaned),
            state=str(row.get("state") or ""),
            country=str(row.get("country") or ""),
            latitude=float(row.get("lat")),
            longitude=float(row.get("lon")),
        )
        self._remember_location(cache_key, location)
        return location

    async def get_weather(
        self,
        location_query: str,
        *,
        units: str = "auto",
        language: str = "en",
        forecast_hours: int = 12,
    ) -> dict[str, Any]:
        location = await self.geocode(location_query)
        resolved_units = normalize_units(units, location.country)
        language = re.sub(r"[^a-z-]", "", str(language or "en").lower())[:8] or "en"
        forecast_hours = max(3, min(int(forecast_hours or 12), 48))
        cache_key = f"{location.latitude:.4f}:{location.longitude:.4f}:{resolved_units}:{language}:{forecast_hours}"
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] < 600:
            return cached[1]

        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 600:
                return cached[1]
            common = {
                "lat": location.latitude,
                "lon": location.longitude,
                "units": resolved_units,
                "lang": language,
            }
            current_payload, forecast_payload = await asyncio.gather(
                self._request_json(
                    "https://api.openweathermap.org/data/2.5/weather", common
                ),
                self._request_json(
                    "https://api.openweathermap.org/data/2.5/forecast", common
                ),
            )
            report = self._normalize_report(
                location,
                current_payload if isinstance(current_payload, dict) else {},
                forecast_payload if isinstance(forecast_payload, dict) else {},
                units=resolved_units,
                forecast_hours=forecast_hours,
            )
            self._cache[cache_key] = (time.monotonic(), report)
            # Keep the tiny in-process cache bounded.
            if len(self._cache) > 128:
                oldest = sorted(self._cache.items(), key=lambda item: item[1][0])[:32]
                for key, _ in oldest:
                    self._cache.pop(key, None)
            return report

    @staticmethod
    def _normalize_report(
        location: WeatherLocation,
        current: dict[str, Any],
        forecast: dict[str, Any],
        *,
        units: str,
        forecast_hours: int,
    ) -> dict[str, Any]:
        main = (
            (current.get("main") or {}) if isinstance(current.get("main"), dict) else {}
        )
        wind = (
            (current.get("wind") or {}) if isinstance(current.get("wind"), dict) else {}
        )
        weather_rows = (
            (current.get("weather") or [])
            if isinstance(current.get("weather"), list)
            else []
        )
        weather_now = (
            weather_rows[0]
            if weather_rows and isinstance(weather_rows[0], dict)
            else {}
        )
        timezone_offset = int(
            current.get("timezone") or forecast.get("city", {}).get("timezone") or 0
        )
        temperature_label, wind_label = unit_labels(units)
        current_dt = int(current.get("dt") or datetime.now(timezone.utc).timestamp())
        cutoff = current_dt + forecast_hours * 3600

        forecast_rows: list[dict[str, Any]] = []
        for row in forecast.get("list") or []:
            if not isinstance(row, dict):
                continue
            timestamp = int(row.get("dt") or 0)
            if timestamp <= 0 or timestamp > cutoff:
                continue
            row_main = (
                (row.get("main") or {}) if isinstance(row.get("main"), dict) else {}
            )
            row_weather = (
                (row.get("weather") or [])
                if isinstance(row.get("weather"), list)
                else []
            )
            row_condition = (
                row_weather[0]
                if row_weather and isinstance(row_weather[0], dict)
                else {}
            )
            forecast_rows.append(
                {
                    "timestamp": timestamp,
                    "time": _format_clock(timestamp, timezone_offset),
                    "temperature": float(row_main.get("temp") or 0),
                    "description": _condition_phrase(
                        str(row_condition.get("description") or "")
                    ),
                    "precipitationProbability": max(
                        0.0, min(float(row.get("pop") or 0), 1.0)
                    ),
                }
            )

        temperatures = [float(row["temperature"]) for row in forecast_rows]
        precipitation = [
            float(row["precipitationProbability"]) for row in forecast_rows
        ]
        forecast_descriptions = [str(row["description"]) for row in forecast_rows]
        dominant_description = (
            max(set(forecast_descriptions), key=forecast_descriptions.count)
            if forecast_descriptions
            else ""
        )

        report = {
            "location": location.as_dict(),
            "units": units,
            "temperatureLabel": temperature_label,
            "windLabel": wind_label,
            "current": {
                "temperature": float(main.get("temp") or 0),
                "feelsLike": float(main.get("feels_like") or main.get("temp") or 0),
                "minimum": float(main.get("temp_min") or main.get("temp") or 0),
                "maximum": float(main.get("temp_max") or main.get("temp") or 0),
                "humidity": int(main.get("humidity") or 0),
                "pressure": int(main.get("pressure") or 0),
                "visibilityKm": round(float(current.get("visibility") or 0) / 1000, 1),
                "windSpeed": float(wind.get("speed") or 0),
                "windGust": float(wind.get("gust") or 0),
                "description": _condition_phrase(
                    str(weather_now.get("description") or weather_now.get("main") or "")
                ),
                "icon": str(weather_now.get("icon") or ""),
                "clouds": int((current.get("clouds") or {}).get("all") or 0),
                "sunrise": _format_clock(
                    (current.get("sys") or {}).get("sunrise"), timezone_offset
                ),
                "sunset": _format_clock(
                    (current.get("sys") or {}).get("sunset"), timezone_offset
                ),
                "observedAt": _format_clock(current_dt, timezone_offset),
            },
            "forecast": {
                "hours": forecast_hours,
                "rows": forecast_rows,
                "minimum": min(temperatures)
                if temperatures
                else float(main.get("temp") or 0),
                "maximum": max(temperatures)
                if temperatures
                else float(main.get("temp") or 0),
                "precipitationProbability": max(precipitation)
                if precipitation
                else 0.0,
                "description": dominant_description,
            },
            "timezoneOffsetSeconds": timezone_offset,
        }
        return report


def weather_fields(
    report: dict[str, Any], *, details: bool = True
) -> list[tuple[str, str, bool]]:
    current = report.get("current") or {}
    forecast = report.get("forecast") or {}
    temp_unit = str(report.get("temperatureLabel") or "°C")
    wind_unit = str(report.get("windLabel") or "m/s")
    fields: list[tuple[str, str, bool]] = [
        (
            "Now",
            f"{_round_temperature(current.get('temperature'))}{temp_unit} · {current.get('description') or 'weather'}",
            True,
        ),
        (
            "Feels like",
            f"{_round_temperature(current.get('feelsLike'))}{temp_unit}",
            True,
        ),
        (
            "Rain chance",
            f"{round(float(forecast.get('precipitationProbability') or 0) * 100)}%",
            True,
        ),
    ]
    if details:
        fields.extend(
            [
                ("Humidity", f"{int(current.get('humidity') or 0)}%", True),
                ("Wind", f"{float(current.get('windSpeed') or 0):g} {wind_unit}", True),
                (
                    "Sun",
                    f"{current.get('sunrise') or '—'} → {current.get('sunset') or '—'}",
                    True,
                ),
                (
                    f"Next {int(forecast.get('hours') or 12)} hours",
                    f"{_round_temperature(forecast.get('minimum'))}{temp_unit} to {_round_temperature(forecast.get('maximum'))}{temp_unit} · "
                    f"{forecast.get('description') or current.get('description') or 'mixed conditions'}",
                    False,
                ),
            ]
        )
    return fields


def natural_weather_reply(report: dict[str, Any], *, details: bool = True) -> str:
    location = str((report.get("location") or {}).get("label") or "there")
    current = report.get("current") or {}
    forecast = report.get("forecast") or {}
    temp_unit = str(report.get("temperatureLabel") or "°C")
    temperature = _round_temperature(current.get("temperature"))
    feels_like = _round_temperature(current.get("feelsLike"))
    description = str(current.get("description") or "weather")
    rain_chance = round(float(forecast.get("precipitationProbability") or 0) * 100)

    first = f"it's {temperature}{temp_unit} and {description} in {location} rn"
    if feels_like != temperature:
        first += f". feels like {feels_like}{temp_unit}"
    if not details:
        return first + "."

    if rain_chance >= 70:
        second = f"rain is very much plotting something at {rain_chance}% over the next {int(forecast.get('hours') or 12)} hours"
    elif rain_chance >= 35:
        second = f"there's a {rain_chance}% rain chance over the next {int(forecast.get('hours') or 12)} hours, so the sky is being indecisive"
    else:
        second = f"rain is only around {rain_chance}% over the next {int(forecast.get('hours') or 12)} hours"
    return f"{first}. {second}."
