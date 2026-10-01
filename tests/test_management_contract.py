from unittest.mock import AsyncMock

import pytest
from test_security_contract import client as client
from test_security_contract import settings as settings
from test_security_contract import signed

from backend.firebase_client import MemoryStore
from backend.management import runtime, sandbox
from backend.pagination import record_page


def test_every_management_operation_has_response_schema(client):
    schema = client.get("/openapi.json").json()
    for path, operations in schema["paths"].items():
        if path.startswith("/api/v1/"):
            for operation in operations.values():
                assert (
                    "$ref"
                    in operation["responses"]["200"]["content"]["application/json"][
                        "schema"
                    ]
                )
                assert "400" in operation["responses"]


def test_memory_clear_rejects_string_boolean(client, settings):
    path = "/api/v1/admin/123/memory/clear"
    body = b'{"allChannels":"false"}'
    response = client.post(
        path, content=body, headers=signed(settings, "POST", path, body)
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_sandbox_uses_saved_config_without_memory_write(client, settings, monkeypatch):
    provider = AsyncMock(return_value=("Fixture reply", "groq"))
    monkeypatch.setattr(sandbox, "ask_ai", provider)
    path = "/api/v1/ai/123/test-reply"
    body = b'{"prompt":"hello"}'
    response = client.post(
        path, content=body, headers=signed(settings, "POST", path, body)
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "Fixture reply"
    assert provider.await_args.args[1] == []
    assert len(runtime.store._sessions) == 0


def test_pagination_limits_are_strict(client, settings):
    for query in ["limit=0", "limit=101", "media_type=html", "channel_id=bad"]:
        path = "/api/v1/media/123?" + query
        response = client.get(path, headers=signed(settings, "GET", path))
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_filtered_pagination_no_duplicate_rows():
    store = MemoryStore()
    for i in range(8):
        await store.add_media_record(
            "123",
            {
                "recordId": str(i),
                "name": "kept" if i % 2 else "other",
                "mediaType": "image",
                "channelId": "456",
            },
        )
    first, cursor = await record_page(
        store, "123", "media", limit=2, search="kept", channel_id="456"
    )
    second, final = await record_page(
        store, "123", "media", limit=2, cursor=cursor, search="kept", channel_id="456"
    )
    assert [row["recordId"] for row in first + second] == ["7", "5", "3", "1"]
    assert final is None
    with pytest.raises(ValueError):
        await record_page(store, "123", "media", cursor="invalid")
