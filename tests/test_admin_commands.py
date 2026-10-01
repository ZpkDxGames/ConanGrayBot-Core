import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord import app_commands

from backend.config import DEFAULT_BOT_CONFIG
from backend.discord_bot import admin
from backend.firebase_client import MemoryStore
from backend.migrations import RevisionConflict


@pytest.fixture
def fixture(monkeypatch):
    config = copy.deepcopy(DEFAULT_BOT_CONFIG)
    bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id="123", staff_role_id="456"),
        store=MemoryStore(),
        clear_all_ai_sessions=AsyncMock(return_value=3),
        clear_ai_session=AsyncMock(),
        apply_configured_presence=AsyncMock(),
        request_control=AsyncMock(),
        latency=0.12,
        is_ready=lambda: True,
        _presence_entries=lambda presence: [
            {"status": "online", "activityType": "listening", "activityText": "Fixture"}
        ],
    )
    interaction = SimpleNamespace(
        guild_id=123, channel_id=456, user=SimpleNamespace(id=789)
    )
    monkeypatch.setattr(admin, "ensure_bot_admin", AsyncMock(return_value=config))
    monkeypatch.setattr(admin, "send_action_result", AsyncMock())
    monkeypatch.setattr(admin, "send_interaction_feedback", AsyncMock())
    return config, bot, interaction, admin.make_admin_group(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("clear-memory", (app_commands.Choice(name="all", value="all"),)),
        ("status", ()),
        ("pause-ai", ()),
        ("resume-ai", ()),
        ("apply-presence", ()),
        ("restart", ()),
        ("shutdown", ()),
    ],
)
async def test_admin_authorization_before_effects(fixture, name, args):
    _, bot, interaction, group = fixture
    admin.ensure_bot_admin.return_value = None
    await group.get_command(name).callback(interaction, *args)
    assert not await bot.store.list_logs("123")
    bot.clear_all_ai_sessions.assert_not_awaited()
    bot.clear_ai_session.assert_not_awaited()
    bot.apply_configured_presence.assert_not_awaited()
    admin.send_action_result.assert_not_awaited()
    admin.send_interaction_feedback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["channel", "all"])
async def test_memory_clear_scope_and_actor_audit(fixture, scope):
    _, bot, interaction, group = fixture
    await group.get_command("clear-memory").callback(
        interaction, app_commands.Choice(name=scope, value=scope)
    )
    if scope == "all":
        bot.clear_all_ai_sessions.assert_awaited_once_with("123")
    else:
        bot.clear_ai_session.assert_awaited_once_with("123", "456")
    log = (await bot.store.list_logs("123"))[0]
    assert log["payload"]["actorId"] == "789"
    assert admin.send_action_result.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("action,enabled", [("pause-ai", False), ("resume-ai", True)])
async def test_admin_ai_update_preserves_configuration_and_increments_revision(
    fixture, action, enabled
):
    config, bot, interaction, group = fixture
    saved = await bot.store.get_config("123")
    config.update(saved)
    await group.get_command(action).callback(interaction)
    result = await bot.store.get_config("123")
    assert result["ai"]["enabled"] is enabled
    assert result["revision"] == saved["revision"] + 1
    assert result["games"] == saved["games"]


@pytest.mark.asyncio
async def test_admin_stale_write_cannot_overwrite_dashboard_edit(fixture):
    config, bot, interaction, group = fixture
    config.update(await bot.store.get_config("123"))
    changed = copy.deepcopy(config)
    changed["presence"]["activityText"] = "Concurrent dashboard update"
    await bot.store.set_config("123", changed, expected_revision=config["revision"])
    with pytest.raises(RevisionConflict):
        await group.get_command("pause-ai").callback(interaction)
    result = await bot.store.get_config("123")
    assert result["presence"]["activityText"] == "Concurrent dashboard update"
    assert result["ai"]["enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["status", "apply-presence"])
async def test_admin_status_and_presence_present_deterministic_facts(fixture, name):
    _, bot, interaction, group = fixture
    await group.get_command(name).callback(interaction)
    assert admin.send_action_result.await_args.kwargs["facts"]
    if name == "apply-presence":
        bot.apply_configured_presence.assert_awaited_once_with("123")
