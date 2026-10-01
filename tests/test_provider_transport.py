from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend import ai_providers as ai
from backend.config import get_settings
from backend.http import close_sessions, pooled_session
from backend.management import auth, runtime
from backend.providers import ProviderManager


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    async def json(self, **kwargs):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    get = post


@pytest.fixture
def transport(monkeypatch):
    def use(body, status=200, module=ai):
        session = Session(Response(body, status))

        @asynccontextmanager
        async def pool():
            yield session

        monkeypatch.setattr(module, "pooled_session", pool)
        return session

    return use


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    monkeypatch.setenv("GROQ_API_KEY", "fixture-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)
    monkeypatch.setenv("OPENROUTER_DISCOVERY_ENABLED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(ai, "manager", ProviderManager())
    yield get_settings()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_http_pool_reuses_connections_and_closes():
    async with pooled_session() as first:
        async with pooled_session() as second:
            assert first is second and first.connector.limit == 32
    assert not first.closed
    await close_sessions()
    assert first.closed


def test_circuit_opens_cools_and_recovers(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("backend.providers.time.monotonic", lambda: clock[0])
    manager = ProviderManager()
    for _ in range(3):
        manager.failure("groq")
    assert not manager.available("groq")
    clock[0] += 31
    assert manager.available("groq")
    manager.success("groq", 130)
    assert manager.diagnostics()["groq"] == {
        "available": True,
        "failures": 0,
        "successes": 1,
        "lastLatencyMs": 1000,
    }


@pytest.mark.asyncio
async def test_gemini_payload_excludes_thought_output(settings, transport):
    session = transport(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "hidden", "thought": True},
                            {"text": "Visible response"},
                        ]
                    }
                }
            ]
        }
    )
    result = await ai._ask_gemini(
        [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "hello"},
        ],
        100,
        0.5,
    )
    assert result == "Visible response"
    payload = session.calls[0][1]["json"]
    assert payload["contents"][0]["role"] == "user"
    assert payload["systemInstruction"]["parts"][0]["text"] == "persona"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "groq", "openrouter"])
async def test_provider_errors_do_not_expose_echoed_prompt(
    settings, transport, provider
):
    transport({"error": "PRIVATE PROMPT fixture-key"}, 500)
    with pytest.raises(ai.AIProviderError) as error:
        await getattr(ai, "_ask_" + provider)(
            [{"role": "user", "content": "hello"}], 100, 0.5
        )
    assert "PRIVATE PROMPT" not in str(error.value) and "fixture-key" not in str(
        error.value
    )


@pytest.mark.asyncio
async def test_openrouter_discovery_requires_explicit_opt_in(settings, monkeypatch):
    discover = AsyncMock(return_value=["unapproved/model"])
    monkeypatch.setattr(ai, "_discover_openrouter_models", discover)
    assert await ai._openrouter_candidates() == [settings.openrouter_model]
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_models_are_task_local_and_prompt_history_bounded(settings, monkeypatch):
    snapshots = []

    async def capture(config, history, text, context):
        snapshots.append((ai.provider_settings().groq_model, history, text, context))
        return "reply", "groq"

    monkeypatch.setattr(ai, "_ask_ai", capture)
    old = ai.provider_settings().groq_model
    await ai.ask_ai(
        {"ai": {"models": {"groq": "saved-model"}, "maxPromptCharacters": 4000}},
        [{"role": "user", "content": "x" * 4000}] * 100,
        "x" * 5000,
        "context",
    )
    model, history, text, context = snapshots[0]
    assert (
        model == "saved-model" and history == [] and len(text) == 4000 and context == ""
    )
    assert ai.provider_settings().groq_model == old


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,roles,expected",
    [(200, ["456"], None), (200, [], 403), (404, [], 403), (429, [], 503)],
)
async def test_staff_authorization_without_gateway(
    transport, monkeypatch, status, roles, expected
):
    monkeypatch.setattr(runtime, "discord_bot", None)
    monkeypatch.setattr(
        runtime,
        "settings",
        SimpleNamespace(
            discord_token="fixture-bot", staff_role_id="456", guild_id="123"
        ),
    )
    session = transport({"roles": roles}, status, auth)
    if expected:
        with pytest.raises(HTTPException) as error:
            await auth.require_staff("789")
        assert error.value.status_code == expected
    else:
        assert await auth.require_staff("789") == "789"
    assert session.calls[0][0].endswith("/guilds/123/members/789")


def test_assembled_prompt_budget_includes_system_instructions():
    from backend.prompts import bounded_messages

    messages = (
        [{"role": "system", "content": "persona" * 10000}]
        + [{"role": "user", "content": "old" * 5000}]
        + [{"role": "user", "content": "latest message"}]
    )
    result = bounded_messages(messages, 4000)
    assert sum(len(row["content"]) for row in result) <= 4000
    assert result[-1]["content"] == "latest message"
    assert len(result) == 2
