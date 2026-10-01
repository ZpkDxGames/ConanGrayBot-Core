import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import DEFAULT_BOT_CONFIG
from backend.discord_bot import media
from backend.discord_bot import media_delivery as delivery
from backend.google_drive import DriveConfigurationError


@pytest.fixture
def fixture(monkeypatch):
    config = copy.deepcopy(DEFAULT_BOT_CONFIG)
    config["media"].update(
        channelId="456",
        googleDriveFolderId="fixture-folder",
        randomCommandMaxFileSizeMb=1,
    )
    message = SimpleNamespace(
        guild=SimpleNamespace(id=123, name="Guild", filesize_limit=1024 * 1024),
        channel=SimpleNamespace(id=456, name="Channel", send=AsyncMock()),
        author=SimpleNamespace(id=789, display_name="Fixture"),
    )
    selected = {
        "id": "fixture-file",
        "name": "image.png",
        "size": "4",
        "mediaType": "image",
        "mimeType": "image/png",
    }
    paths = []

    async def download(file_id, path):
        paths.append(path)
        Path(path).write_bytes(b"test")

    drive = SimpleNamespace(
        configured=True,
        random_media_file=AsyncMock(return_value=selected),
        download_file=AsyncMock(side_effect=download),
    )
    interaction = SimpleNamespace(
        channel_id=456,
        guild_id=123,
        guild=message.guild,
        channel=message.channel,
        user=message.author,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    bot = SimpleNamespace(
        drive_archive=drive,
        store=SimpleNamespace(add_log=AsyncMock()),
        settings=SimpleNamespace(guild_id="123"),
    )
    for module in [media, delivery]:
        monkeypatch.setattr(
            module,
            "build_drive_stream_url",
            lambda *args: "https://fixture.invalid/private.mp4?sig=fictional",
        )
    monkeypatch.setattr(delivery, "send_message_feedback", AsyncMock())
    monkeypatch.setattr(delivery, "send_message_inline_media_card", AsyncMock())
    monkeypatch.setattr(media, "send_interaction_feedback", AsyncMock())
    monkeypatch.setattr(media, "send_interaction_inline_media_card", AsyncMock())
    monkeypatch.setattr(media, "ensure_command_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(media, "get_interaction_config", AsyncMock(return_value=config))
    monkeypatch.setattr(
        media, "interpret_action", AsyncMock(return_value=("Media selected", "fixture"))
    )
    return config, message, drive, selected, paths, interaction, bot


async def trigger(fixture):
    config, message, drive, *_ = fixture
    return await delivery.send_random_trigger_media(
        message,
        config,
        trigger_word="fixture",
        response_text="Selected media",
        requested_type="",
        drive_archive=drive,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["trigger", "command"])
@pytest.mark.parametrize(
    "kind,mode,fallback",
    [
        ("image", "embed_attachment", False),
        ("video", "embed_attachment", False),
        ("video", "inline_card", False),
        ("video", "inline_card", True),
    ],
)
async def test_media_delivery_files_cleanup_and_inline_fallback(
    fixture, source, kind, mode, fallback
):
    config, message, _, selected, paths, interaction, bot = fixture
    selected.update(
        name="../../clip.mp4" if kind == "video" else "../../image.png",
        mediaType=kind,
        mimeType="video/mp4" if kind == "video" else "image/png",
    )
    config["media"]["videoDisplayMode"] = mode
    inline = (
        delivery.send_message_inline_media_card
        if source == "trigger"
        else media.send_interaction_inline_media_card
    )
    if fallback:
        inline.side_effect = RuntimeError("fictional private detail")
    if source == "trigger":
        result = await trigger(fixture)
        assert result["mediaStatus"] == "sent"
        send = message.channel.send
    else:
        await media.make_media_command(bot).callback(interaction)
        send = interaction.edit_original_response
        assert bot.store.add_log.await_count == 1
    assert paths and all(not Path(path).exists() for path in paths)
    if kind == "video" and mode == "inline_card" and not fallback:
        inline.assert_awaited_once()
    else:
        send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["trigger", "command"])
@pytest.mark.parametrize("known_size", [True, False])
async def test_large_media_uses_private_stream(fixture, source, known_size):
    _, message, drive, selected, paths, interaction, bot = fixture
    selected.update(
        name="clip.mp4",
        mediaType="video",
        mimeType="video/mp4",
        size=str(2 * 1024 * 1024 if known_size else 0),
    )
    if not known_size:

        async def download(file_id, path):
            paths.append(path)
            with open(path, "wb") as stream:
                stream.truncate(2 * 1024 * 1024)

        drive.download_file.side_effect = download
    if source == "trigger":
        result = await trigger(fixture)
        assert result["mediaStatus"] == "streamed"
        args = message.channel.send.await_args.kwargs
    else:
        await media.make_media_command(bot).callback(interaction)
        args = interaction.edit_original_response.await_args.kwargs
        assert bot.store.add_log.await_args.args[1] == "media.random_streamed"
    assert args["content"].startswith("https://fixture.invalid/private.mp4?")
    assert drive.download_file.await_count == int(not known_size)
    assert all(not Path(path).exists() for path in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["trigger", "command"])
async def test_download_failure_is_private_and_cleans_temporary_file(fixture, source):
    _, _, drive, _, paths, interaction, bot = fixture

    async def fail(file_id, path):
        paths.append(path)
        raise ValueError("fictional confidential credential")

    drive.download_file.side_effect = fail
    if source == "trigger":
        result = await trigger(fixture)
        assert result["mediaStatus"] == "send_failed"
        feedback = delivery.send_message_feedback
    else:
        await media.make_media_command(bot).callback(interaction)
        feedback = media.send_interaction_feedback
    assert "confidential" not in feedback.await_args.kwargs["description"]
    assert all(not Path(path).exists() for path in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        RuntimeError("fictional"),
        DriveConfigurationError("Drive unavailable", code="drive_unavailable"),
    ],
)
async def test_trigger_empty_and_lookup_failures(fixture, failure):
    _, _, drive, *_ = fixture
    drive.random_media_file.return_value = None
    if failure:
        drive.random_media_file.side_effect = failure
    result = await trigger(fixture)
    assert result["mediaStatus"] in {"empty", "lookup_failed", "drive_unavailable"}
    drive.download_file.assert_not_awaited()
