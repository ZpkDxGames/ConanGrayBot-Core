import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import presentation as p
from backend.config import DEFAULT_BOT_CONFIG


@pytest.fixture
def config():
    return copy.deepcopy(DEFAULT_BOT_CONFIG)


@pytest.fixture
def actor():
    return SimpleNamespace(
        id=789,
        display_name="Fixture Staff",
        name="Fixture",
        mention="<@789>",
        display_avatar=SimpleNamespace(url="https://fixture.test/avatar.png"),
    )


def test_embed_respects_aggregate_and_field_limits(config, actor):
    config["appearance"]["embedFooter"] = "x" * 3000
    embed = p.build_feedback_embed(
        config,
        title="t" * 400,
        description="d" * 5000,
        fields=[("name" * 100, "value" * 300, False)] * 50,
        actor=actor,
        image_url="https://fixture.test/image.png",
    )
    assert len(embed) <= 6000 and len(embed.fields) <= 25
    assert len(embed.title) <= 256 and len(embed.description or "") <= 4096
    assert all(
        len(field.name) <= 256 and len(field.value) <= 1024 for field in embed.fields
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["embed", "plain"])
@pytest.mark.parametrize("state", ["new", "followup", "edit"])
async def test_interaction_delivery_chooses_correct_lifecycle(
    config, actor, mode, state
):
    config["messageTemplates"]["global"]["useEmbed"] = mode == "embed"
    config["messageTemplates"]["command"]["inheritGlobal"] = True
    response = SimpleNamespace(
        is_done=lambda: state == "followup", send_message=AsyncMock(return_value="new")
    )
    interaction = SimpleNamespace(
        user=actor,
        guild=SimpleNamespace(name="Fixture", id=123),
        channel=SimpleNamespace(name="chat", id=456),
        response=response,
        followup=SimpleNamespace(send=AsyncMock(return_value="followup")),
        edit_original_response=AsyncMock(return_value="edit"),
    )
    assert (
        await p.send_interaction_feedback(
            interaction,
            config,
            title="Result",
            description="Exact result",
            kind="command",
            template_key="command",
            ephemeral=True,
            edit_original=state == "edit",
            view=object(),
        )
        == state
    )
    method = (
        interaction.edit_original_response
        if state == "edit"
        else interaction.followup.send
        if state == "followup"
        else response.send_message
    )
    assert (
        ("embed" in method.await_args.kwargs)
        if mode == "embed"
        else ("content" in method.await_args.kwargs)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("embed", [True, False])
async def test_message_delivery_preserves_mentions_and_presentation(
    config, actor, embed
):
    config["messageTemplates"]["global"]["useEmbed"] = embed
    config["messageTemplates"]["command"]["inheritGlobal"] = True
    message = SimpleNamespace(
        author=actor,
        guild=SimpleNamespace(name="Fixture", id=123),
        channel=SimpleNamespace(name="chat", id=456),
        reply=AsyncMock(return_value="sent"),
    )
    assert (
        await p.send_message_feedback(
            message,
            config,
            title="Result",
            description="Exact result",
            kind="command",
            template_key="command",
            mention_author=False,
        )
        == "sent"
    )
    assert message.reply.await_args.kwargs["mention_author"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["disabled", "failure", "empty", "success"])
async def test_narration_isolated_fallbacks_preserve_config(
    config, monkeypatch, behavior
):
    original = copy.deepcopy(config)
    ask = AsyncMock(
        return_value=("Narration" if behavior == "success" else "", "fixture")
    )
    if behavior == "failure":
        ask.side_effect = p.AIProviderError("unavailable")
    monkeypatch.setattr(p, "ask_ai", ask)
    if behavior == "disabled":
        config["presentation"]["aiActionInterpretation"] = False
    text, source = await p.interpret_action(
        config,
        feature="coinflip",
        outcome="heads",
        facts="Locked result: heads",
        actor_name="Fixture",
    )
    assert text
    if behavior == "success":
        assert source == "AI narration: fixture"
    else:
        assert source.startswith("Fallback pool:")
    if behavior == "disabled":
        ask.assert_not_awaited()
    else:
        assert ask.await_args.args[1] == []
        assert "Locked result: heads" in ask.await_args.args[2]
        assert config == original


def test_components_payload_disables_mentions_and_attaches_private_file(config, actor):
    payload = p.components_v2_media_payload(
        config,
        title="Media",
        description="Fixture",
        kind="media",
        template_key="media",
        fields=[("Source", "private", False)],
        actor=actor,
        provider=None,
        source_note=None,
        filename="picture.png",
        media_description="Preview",
    )
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["attachments"][0]["filename"] == "picture.png"
    gallery = next(
        item for item in payload["components"][0]["components"] if item["type"] == 12
    )
    assert gallery["items"][0]["media"]["url"] == "attachment://picture.png"
