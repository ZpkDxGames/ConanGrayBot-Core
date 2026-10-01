import asyncio
from unittest.mock import AsyncMock

import aiohttp
import pytest

from backend.weather import OpenWeatherClient, WeatherError, WeatherLocation
from tests.test_provider_transport import Response, Session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, "invalid_key", 503),
        (429, "rate_limited", 429),
        (503, "weather_error", 503),
    ],
)
async def test_weather_errors_never_echo_provider_body(status, code, expected):
    client = OpenWeatherClient("fictional-weather-key")
    client._session = Session(
        Response({"message": "fictional-confidential-provider-body"}, status)
    )
    client._session.closed = False
    with pytest.raises(WeatherError) as caught:
        await client._request_json("https://api.openweathermap.org/test", {})
    assert caught.value.code == code and caught.value.status == expected
    assert "confidential" not in str(caught.value)
    assert client._session.calls[0][1]["params"]["appid"] == "fictional-weather-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code",
    [
        (asyncio.TimeoutError(), "timeout"),
        (aiohttp.ClientConnectionError("fictional"), "network_error"),
        (ValueError("fictional"), "network_error"),
    ],
)
async def test_weather_transport_failures_are_typed(error, code):
    class Failed:
        def get(self, *args, **kwargs):
            raise error

    client = OpenWeatherClient("fictional-key")
    client._session = Failed()
    client._session.closed = False
    with pytest.raises(WeatherError) as caught:
        await client._request_json("https://api.openweathermap.org/test", {})
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_unconfigured_weather_never_opens_session():
    client = OpenWeatherClient("")
    with pytest.raises(WeatherError, match="not configured"):
        await client._request_json("https://api.openweathermap.org/test", {})
    assert client._session is None


@pytest.mark.asyncio
async def test_session_reuse_and_shutdown():
    client = OpenWeatherClient("fictional")
    session = await client._get_session()
    assert await client._get_session() is session
    await client.close()
    assert session.closed and client._session is None
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,payload,label",
    [
        ("40,-70", [], "40.000, -70.000"),
        (
            "90210,US",
            {"lat": 40, "lon": -70, "name": "City", "country": "US"},
            "City, US",
        ),
        (
            "Paris",
            [
                {
                    "lat": 40,
                    "lon": -70,
                    "name": "Paris",
                    "state": "Paris",
                    "country": "FR",
                }
            ],
            "Paris, FR",
        ),
    ],
)
async def test_geocoding_modes_cache_normalized_locations(
    monkeypatch, query, payload, label
):
    client = OpenWeatherClient("fictional")
    request = AsyncMock(return_value=payload)
    monkeypatch.setattr(client, "_request_json", request)
    result = await client.geocode(query)
    assert result.label == label
    assert await client.geocode(query) is result
    request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,code",
    [
        ("", "location_required"),
        ("91,0", "invalid_coordinates"),
        ("Missing", "location_not_found"),
    ],
)
async def test_geocoding_input_errors(monkeypatch, query, code):
    client = OpenWeatherClient("fictional")
    monkeypatch.setattr(client, "_request_json", AsyncMock(return_value=[]))
    with pytest.raises(WeatherError) as caught:
        await client.geocode(query)
    assert caught.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["40,-70", "90210,US", "Paris"])
async def test_geocoding_cache_bounded_in_every_mode(monkeypatch, query):
    client = OpenWeatherClient("fictional")
    location = WeatherLocation("City", "", "US", 40, -70)
    for number in range(256):
        client._geocode_cache[str(number)] = (0.0, location)

    async def request(url, params):
        row = {"lat": 40, "lon": -70, "name": "City", "country": "US"}
        return row if url.endswith("/zip") else [row]

    monkeypatch.setattr(client, "_request_json", request)
    await client.geocode(query)
    assert len(client._geocode_cache) <= 256


@pytest.mark.asyncio
async def test_weather_concurrency_coalesces_requests_and_preserves_facts(monkeypatch):
    client = OpenWeatherClient("fictional")
    monkeypatch.setattr(
        client,
        "geocode",
        AsyncMock(return_value=WeatherLocation("City", "", "US", 40, -70)),
    )
    calls = []

    async def request(url, params):
        calls.append((url, params))
        await asyncio.sleep(0)
        return (
            {"dt": 1000, "main": {"temp": 61}}
            if url.endswith("/weather")
            else {"list": []}
        )

    monkeypatch.setattr(client, "_request_json", request)
    first, second = await asyncio.gather(
        client.get_weather("City", language="EN!"),
        client.get_weather("City", language="EN!"),
    )
    assert first is second
    assert first["current"]["temperature"] == 61 and first["units"] == "imperial"
    assert len(calls) == 2
    assert calls[0][1]["lang"] == "en"
