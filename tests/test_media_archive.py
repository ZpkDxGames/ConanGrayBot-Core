import asyncio
import copy
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import DEFAULT_BOT_CONFIG
from backend.discord_bot.media_events import MediaEventsMixin
from backend.firebase_client import MemoryStore
from backend.google_drive import GoogleDriveArchive
from backend.state import TTLRegistry


class Attachment:
    id = 1
    filename = "picture.png"
    content_type = "image/png"
    size = 10

    def __init__(self, actual=10):
        self.actual, self.paths = actual, []

    async def save(self, path, *, use_cached=False):
        assert use_cached
        self.paths.append(path)
        Path(path).write_bytes(b"x" * self.actual)


class ArchiveHarness(MediaEventsMixin):
    def __init__(self):
        self.store = MemoryStore()
        self.media_archive_locks = TTLRegistry(16, 60)
        self.config = copy.deepcopy(DEFAULT_BOT_CONFIG)
        self.config["media"].update(
            enabled=True,
            channelId="456",
            googleDriveFolderId="fixture-folder",
            maxFileSizeMb=1,
            notifyOnUpload=False,
            notifyOnFailure=False,
        )
        self.drive_archive = SimpleNamespace(
            configured=True,
            upload_file=AsyncMock(
                return_value={
                    "id": "drive-fixture",
                    "name": "picture.png",
                    "size": "10",
                }
            ),
            delete_file=AsyncMock(),
        )

    async def _config_for(self, guild):
        return self.config


def message(attachment):
    return SimpleNamespace(
        id=99,
        guild=SimpleNamespace(id=123, name="Fixture"),
        channel=SimpleNamespace(id=456, name="media"),
        author=SimpleNamespace(id=789, name="Fixture", display_name="Fixture"),
        attachments=[attachment],
        jump_url="https://discord.test/message",
    )


@pytest.mark.asyncio
async def test_duplicate_concurrent_delivery_archives_once_and_cleans_files():
    harness = ArchiveHarness()
    attachment = Attachment()
    event = message(attachment)
    await asyncio.gather(
        harness._handle_media_archive(event), harness._handle_media_archive(event)
    )
    harness.drive_archive.upload_file.assert_awaited_once()
    assert (await harness.store.get_media_record("123", "99-1"))[
        "driveFileId"
    ] == "drive-fixture"
    assert attachment.paths and all(
        not Path(path).exists() for path in attachment.paths
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actual,declared", [(10, 2 * 1024 * 1024), (2 * 1024 * 1024, 10)]
)
async def test_declared_and_actual_size_limits_prevent_upload(actual, declared):
    harness = ArchiveHarness()
    attachment = Attachment(actual)
    attachment.size = declared
    await harness._handle_media_archive(message(attachment))
    harness.drive_archive.upload_file.assert_not_awaited()
    assert all(not Path(path).exists() for path in attachment.paths)
    assert await harness.store.get_media_record("123", "99-1") is None


@pytest.mark.asyncio
async def test_record_failure_compensates_uploaded_drive_file(monkeypatch):
    harness = ArchiveHarness()
    attachment = Attachment()
    monkeypatch.setattr(
        harness.store,
        "add_media_record",
        AsyncMock(side_effect=RuntimeError("storage unavailable")),
    )
    await harness._handle_media_archive(message(attachment))
    harness.drive_archive.delete_file.assert_awaited_once_with("drive-fixture")
    assert all(not Path(path).exists() for path in attachment.paths)


@pytest.mark.asyncio
async def test_audit_failure_does_not_delete_persisted_archive(monkeypatch):
    harness = ArchiveHarness()
    attachment = Attachment()
    monkeypatch.setattr(
        harness.store,
        "add_log",
        AsyncMock(side_effect=RuntimeError("audit unavailable")),
    )
    with pytest.raises(RuntimeError):
        await harness._handle_media_archive(message(attachment))
    harness.drive_archive.delete_file.assert_not_awaited()
    assert await harness.store.get_media_record("123", "99-1")
    assert all(not Path(path).exists() for path in attachment.paths)


@pytest.mark.asyncio
async def test_shared_drive_sdk_transport_is_serialized():
    archive = GoogleDriveArchive()
    active = maximum = 0
    lock = threading.Lock()

    def work(value):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.005)
        with lock:
            active -= 1
        return value

    assert await asyncio.gather(
        *(archive._execute(work, i) for i in range(12))
    ) == list(range(12))
    assert maximum == 1
