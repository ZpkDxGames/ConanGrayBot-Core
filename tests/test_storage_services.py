import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from firebase_admin import firestore

from backend.firebase_client import FirestoreStore, MemoryStore
from backend.migrations import RevisionConflict
from backend.pagination import record_page
from tests.firestore_double import Client, transactional


@pytest.fixture(params=["memory", "firestore"])
def store(request, monkeypatch):
    if request.param == "memory":
        return MemoryStore()
    client = Client()
    monkeypatch.setattr(firestore, "transactional", transactional(client.lock))
    return FirestoreStore(client)


@pytest.mark.asyncio
async def test_atomic_config_revision_and_copy(store):
    config = await store.get_config("123")
    config["ai"]["personality"] = "Saved custom persona"
    saved = await store.set_config("123", config, 0)
    assert saved["revision"] == 1
    saved["ai"]["personality"] = "caller mutation"
    assert (await store.get_config("123"))["ai"][
        "personality"
    ] == "Saved custom persona"
    results = await asyncio.gather(
        store.set_config("123", config, 1),
        store.set_config("123", config, 1),
        return_exceptions=True,
    )
    assert sum(isinstance(r, RevisionConflict) for r in results) == 1


@pytest.mark.asyncio
async def test_session_indexes_prune_and_clear_consistently(store):
    assert await store.resolve_reply_branch("123", "456", "missing") is None
    await store.set_branch_session(
        "123",
        "456",
        "b",
        [{"role": "user", "content": "first"}],
        message_ids=["old"],
        latest_bot_message_id="bot",
        root_message_id="root",
    )
    assert await store.get_active_branch("123", "456") == "b"
    assert await store.resolve_reply_branch("123", "456", "old") == "b"
    await store.set_branch_session(
        "123",
        "456",
        "b",
        [{"role": "user", "content": "second"}],
        message_ids=["new"],
        latest_bot_message_id="bot2",
    )
    assert await store.resolve_reply_branch("123", "456", "old") is None
    assert await store.resolve_reply_branch("123", "999", "new") is None
    assert (await store.session_stats("123")) == {"channels": 1, "messages": 1}
    await store.clear_session("123", "456")
    assert await store.get_active_branch("123", "456") is None
    assert (await store.get_branch_session("123", "456", "b"))["messages"] == []
    await store.set_session("123", "456", [{"content": "one"}])
    await store.set_session("123", "999", [{"content": "two"}])
    assert await store.clear_guild_sessions("123") == 2
    assert (await store.session_stats("123")) == {"channels": 0, "messages": 0}


@pytest.mark.asyncio
async def test_log_sanitization_and_media_filtered_keyset_pages(store):
    await store.add_log(
        "123", "fixture", {"authorization": "private", "actorId": "789"}
    )
    assert (await store.list_logs("123"))[0]["payload"] == {"actorId": "789"}
    for i in range(7):
        await store.add_media_record(
            "123",
            {
                "recordId": str(i),
                "name": "Fixture",
                "mediaType": "image",
                "channelId": "456",
                "size": 10,
            },
        )
    first, cursor = await record_page(
        store, "123", "media", limit=3, media_type="image", channel_id="456"
    )
    second, next_cursor = await record_page(
        store,
        "123",
        "media",
        limit=3,
        cursor=cursor,
        media_type="image",
        channel_id="456",
    )
    assert len(first) == len(second) == 3 and not (
        {r["recordId"] for r in first} & {r["recordId"] for r in second}
    )
    last, done = await record_page(
        store,
        "123",
        "media",
        limit=3,
        cursor=next_cursor,
        media_type="image",
        channel_id="456",
    )
    assert len(last) == 1 and done is None
    assert (await store.media_stats("123"))["bytes"] == 70
    assert await store.get_media_record("123", "missing") is None
    assert await store.delete_media_record("123", first[0]["recordId"])
    assert await store.delete_media_record("123", "missing") is None


@pytest.mark.asyncio
async def test_game_expiry_and_delete(store):
    await store.set_guessing_game(
        "123",
        "456",
        "message",
        {
            "answer": "Heather",
            "expiresAt": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        },
    )
    assert (await store.get_guessing_game("123", "456", "message"))[
        "answer"
    ] == "Heather"
    assert await store.get_guessing_game("123", "999", "message") is None
    await store.delete_guessing_game("123", "456", "message")
    assert await store.get_guessing_game("123", "456", "message") is None
    await store.set_guessing_game(
        "123", "456", "message", {"expiresAt": "2000-01-01T00:00:00Z"}
    )
    assert await store.get_guessing_game("123", "456", "message") is None


@pytest.mark.asyncio
async def test_firestore_native_ttl_suppresses_reads(monkeypatch):
    client = Client()
    monkeypatch.setattr(firestore, "transactional", transactional(client.lock))
    store = FirestoreStore(client)
    config = await store.get_config("123")
    config["ai"]["memoryRetentionDays"] = 2
    await store.set_config("123", config, 0)
    await store.set_branch_session(
        "123", "456", "b", [{"content": "private"}], message_ids=["message"]
    )
    for path, row in client.docs.items():
        if "expiresAt" in row:
            assert isinstance(row["expiresAt"], datetime)
            row["expiresAt"] = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert not (await store.get_branch_session("123", "456", "b"))["messages"]
    assert await store.resolve_reply_branch("123", "456", "message") is None
    assert await store.get_active_branch("123", "456") is None


@pytest.mark.asyncio
async def test_session_statistics_suppress_expired_content(store):
    await store.set_branch_session(
        "123", "456", "branch", [{"role": "user", "content": "Expired fixture"}]
    )
    old = datetime.now(timezone.utc) - timedelta(days=2)
    if isinstance(store, MemoryStore):
        for row in store._sessions.values():
            row["expiresAt"] = old.isoformat()
    else:
        for path, row in store.client.docs.items():
            if "/ai_sessions/" in path:
                row["expiresAt"] = old
    assert await store.session_stats("123") == {"channels": 0, "messages": 0}


@pytest.mark.asyncio
async def test_native_media_statistics_use_aggregations_without_stream(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    store = FirestoreStore(object())
    query = MagicMock()
    query.count.return_value.get.return_value = [[SimpleNamespace(value=3)]]
    query.where.return_value.count.return_value.get.side_effect = [
        [[SimpleNamespace(value=2)]],
        [[SimpleNamespace(value=1)]],
    ]
    query.sum.return_value.get.return_value = [[SimpleNamespace(value=100)]]
    monkeypatch.setattr(store, "_media_collection", lambda guild: query)
    assert await store.media_stats("123") == {
        "files": 3,
        "images": 2,
        "videos": 1,
        "bytes": 100,
    }
    query.stream.assert_not_called()
    query.sum.assert_called_once_with("size", alias="bytes")
