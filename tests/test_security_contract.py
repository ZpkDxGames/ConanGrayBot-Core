import asyncio
import copy
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.config import DEFAULT_BOT_CONFIG, Settings, get_settings
from backend.firebase_client import MemoryStore
from backend.logging import redact, safe_payload
from backend.media_stream import (
    build_drive_stream_url,
    normalize_stream_filename,
    validate_drive_stream_signature,
)
from backend.migrations import RevisionConflict, migrate_config
from backend.models import BotConfig
from backend.security import actor_signature, rates, replays
from backend.state import TTLRegistry


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("CORE_SERVICE_TOKEN", "test-service-secret-" + "a" * 32)
    monkeypatch.setenv("MEDIA_STREAM_SIGNING_KEY", "test-media-secret-" + "b" * 32)
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("DISCORD_STAFF_ROLE_ID", "456")
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    replays.clear()
    rates.clear()
    yield get_settings()
    get_settings.cache_clear()


def signed(settings, method, path, body=b"", actor="789", **changes):
    stamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + settings.core_service_token,
        "x-conan-actor": actor,
        "x-conan-timestamp": stamp,
        "x-conan-nonce": nonce,
        "x-conan-signature": actor_signature(
            settings.core_service_token, method, path, actor, stamp, nonce, body
        ),
    }
    headers.update(changes)
    return headers


@pytest.fixture
def client(settings, monkeypatch):
    from backend.api import app
    from backend.management import runtime

    class Guild:
        async def fetch_member(self, actor):
            return SimpleNamespace(
                roles=[SimpleNamespace(id=456)] if actor == 789 else []
            )

    class Bot:
        def is_ready(self):
            return True

        def get_guild(self, guild):
            return Guild()

        async def apply_configured_presence(self, guild):
            pass

    monkeypatch.setattr(runtime, "settings", settings)
    monkeypatch.setattr(runtime, "store", MemoryStore())
    monkeypatch.setattr(runtime, "discord_bot", Bot())
    return TestClient(app)


def test_settings_dynamic_and_confidential(monkeypatch):
    monkeypatch.setenv("CORE_SERVICE_TOKEN", "sensitive-test-value")
    assert Settings().core_service_token == "sensitive-test-value"
    assert "sensitive-test-value" not in repr(Settings())


@pytest.mark.parametrize(
    "change",
    [
        {"MEDIA_STREAM_TTL_SECONDS": "1"},
        {"MEMORY_RETENTION_DAYS": "0"},
        {"ENVIRONMENT": "production"},
    ],
)
def test_settings_reject_invalid(monkeypatch, change):
    for name, value in change.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.parametrize(
    "key",
    [
        "ai",
        "media",
        "games",
        "presence",
        "commands",
        "appearance",
        "weather",
        "admin",
        "messageTemplates",
        "presentation",
    ],
)
def test_migrations_preserve_section(key):
    original = copy.deepcopy(DEFAULT_BOT_CONFIG)
    before = copy.deepcopy(original)
    result = migrate_config(original)
    assert result[key] == before[key]
    assert original == before
    assert migrate_config(result) == result


@pytest.mark.parametrize("version", [0, 5, True, "4"])
def test_reject_unknown_schema(version):
    with pytest.raises(ValueError):
        migrate_config({"schemaVersion": version})


@pytest.mark.parametrize(
    "field,value",
    [
        ("maxOutputTokens", -1),
        ("temperature", 3),
        ("providerOrder", ["groq", "groq"]),
        ("providerOrder", ["unknown"]),
        ("maxHistoryMessages", 1),
        ("memoryRetentionDays", 0),
        ("maxPromptCharacters", 5),
    ],
)
def test_config_boundaries(field, value):
    config = BotConfig().model_dump()
    config["ai"][field] = value
    with pytest.raises(ValidationError):
        BotConfig.model_validate(config)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        migrate_config({"unknown": "typo"})
    assert BotConfig().model_dump()["admin"]["roleId"] == ""


def test_cas_only_one_concurrent_writer():
    async def run():
        store = MemoryStore()
        config = await store.get_config("123")
        result = await asyncio.gather(
            store.set_config("123", config, 0),
            store.set_config("123", config, 0),
            return_exceptions=True,
        )
        assert sum(isinstance(x, RevisionConflict) for x in result) == 1
        assert (await store.get_config("123"))["revision"] == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        {"authorization": "Bearer wrong"},
        {"x-conan-actor": "bad"},
        {"x-conan-nonce": "bad"},
        {"x-conan-timestamp": "bad"},
        {"x-conan-timestamp": "0"},
        {"x-conan-signature": "wrong"},
    ],
)
def test_invalid_actor_proof(client, settings, change):
    path = "/api/v1/config/123"
    assert (
        client.get(path, headers=signed(settings, "GET", path, **change)).status_code
        == 401
    )


def test_replay_role_guild_and_body(client, settings):
    path = "/api/v1/config/123"
    headers = signed(settings, "GET", path)
    assert client.get(path, headers=headers).status_code == 200
    assert client.get(path, headers=headers).status_code == 401
    assert (
        client.get(path, headers=signed(settings, "GET", path, actor="999")).status_code
        == 403
    )
    path = "/api/v1/config/999"
    assert client.get(path, headers=signed(settings, "GET", path)).status_code == 403
    assert client.put(path, content=b"x" * 256001).status_code == 413


def test_update_conflict_and_errors(client, settings):
    import json

    path = "/api/v1/config/123"
    body = json.dumps({"config": BotConfig().model_dump(), "revision": 0}).encode()
    first = client.put(path, content=body, headers=signed(settings, "PUT", path, body))
    assert first.status_code == 200, first.text
    assert first.json()["config"]["revision"] == 1
    stale = client.put(path, content=body, headers=signed(settings, "PUT", path, body))
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    assert stale.json()["requestId"] and stale.headers["cache-control"] == "no-store"
    assert "expectedKeyId" not in stale.text
    invalid = b'{"config":{"ai":{"maxOutputTokens":-1}},"revision":1}'
    assert (
        client.put(
            path, content=invalid, headers=signed(settings, "PUT", path, invalid)
        ).status_code
        == 422
    )


def test_media_expiry_and_binding(settings):
    link = build_drive_stream_url("file123", "../image.png")
    q = parse_qs(urlparse(link).query)
    expiry = int(q["expires"][0])
    sig = q["sig"][0]
    assert validate_drive_stream_signature("file123", "image.png", sig, expiry)
    assert not validate_drive_stream_signature("other", "image.png", sig, expiry)
    assert not validate_drive_stream_signature("file123", "other.png", sig, expiry)
    assert not validate_drive_stream_signature(
        "file123", "image.png", sig, expiry, "attachment"
    )
    assert not validate_drive_stream_signature("file123", "image.png", sig, 0)
    assert not validate_drive_stream_signature(
        "file123", "image.png", sig, expiry + 3600
    )
    assert not validate_drive_stream_signature(
        "file123", "image.png", sig, expiry, "script"
    )
    assert normalize_stream_filename("..\\unsafe\n.png") == "unsafe.png"


def test_registry_capacity_expiry_and_busy(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr("backend.state.time.monotonic", lambda: clock[0])
    registry = TTLRegistry(1, 1)
    registry["a"] = "value"
    registry["b"] = "new"
    assert "a" not in registry
    clock[0] = 3
    assert len(registry) == 0

    async def run():
        lock = asyncio.Lock()
        await lock.acquire()
        registry["busy"] = lock
        clock[0] = 5
        assert len(registry) == 1
        with pytest.raises(RuntimeError):
            registry["c"] = "other"
        lock.release()
        assert len(registry) == 0

    asyncio.run(run())


def test_redaction(settings):
    assert settings.core_service_token not in redact(settings.core_service_token)
    assert safe_payload(
        {"token": "secret", "safe": settings.media_stream_signing_key}
    ) == {"safe": "[REDACTED]"}
    assert "abc" not in redact("https://test/?sig=abc&code=abc")
    assert (
        redact(
            "-----BEGIN " + "PRIVATE KEY-----\nsecret\n-----END " + "PRIVATE KEY-----"
        )
        == "[REDACTED]"
    )
