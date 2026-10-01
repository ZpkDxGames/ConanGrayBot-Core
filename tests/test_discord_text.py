from types import SimpleNamespace

import pytest

from backend.discord_bot.common import (
    attachment_media_type,
    configured_guess_song_rounds,
    render_media_filename,
    split_discord_text,
)


@pytest.mark.parametrize(
    "filename,content_type,kind",
    [
        ("image.png", None, "image"),
        ("video.mp4", "video/mp4; charset=utf-8", "video"),
        ("file.bin", None, None),
    ],
)
def test_attachment_types(filename, content_type, kind):
    assert (
        attachment_media_type(
            SimpleNamespace(filename=filename, content_type=content_type)
        )[0]
        == kind
    )


@pytest.mark.parametrize(
    "template", ["{filename}", "{user}/{channel}/{stem}", "x" * 500, "\x00\\/ "]
)
def test_media_filename_is_bounded_basename_with_extension(template):
    attachment = SimpleNamespace(filename="../../photo.PNG")
    message = SimpleNamespace(
        id=123,
        author=SimpleNamespace(display_name="Fixture"),
        channel=SimpleNamespace(name="channel"),
        guild=SimpleNamespace(name="guild"),
    )
    result = render_media_filename(template, attachment, message)
    assert result.lower().endswith(".png") and len(result) <= 180
    assert "/" not in result and "\\" not in result and "\x00" not in result


@pytest.mark.parametrize("text", ["", "x" * 5000, ("word " * 1000), ("line\n" * 1000)])
def test_discord_chunks_preserve_nonwhitespace_content_and_limits(text):
    chunks = split_discord_text(text, 500)
    assert chunks and all(len(chunk) <= 500 for chunk in chunks)
    if text:
        assert "".join("".join(chunks).split()) == "".join(text.split())


def test_structured_and_legacy_song_rounds():
    rows = configured_guess_song_rounds(
        {
            "guessSongRounds": [
                {"answer": "Heather", "aliases": ["", "sweater"], "hint": "A sweater"},
                "Memories | past | A memory",
                "Winner | A win",
                "invalid",
            ]
        }
    )
    assert len(rows) == 3
    assert rows[0]["aliases"] == ["sweater"]
    assert rows[2]["answer"] == "Winner"
    assert configured_guess_song_rounds(
        {"guessSongRounds": [], "guessSongHints": ["A sweater"]}
    )[0]["answer"]


@pytest.mark.parametrize(
    "user,expected",
    [
        (
            SimpleNamespace(
                global_name="Global", name="Username", display_name="Nickname"
            ),
            "Global",
        ),
        (SimpleNamespace(name="Username", display_name="Nickname"), "Username"),
        (SimpleNamespace(display_name="Fixture"), "Fixture"),
        (SimpleNamespace(id=123), "Discord user 123"),
    ],
)
def test_profile_name_avoids_guild_nickname_when_account_name_available(user, expected):
    from backend.discord_bot.common import discord_profile_name

    assert discord_profile_name(user) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", False),
        ("hey conan!", True),
        ("conanology", False),
        ("hey Control Room", True),
        ("a quiet unrelated message", False),
    ],
)
def test_direct_calls_use_whole_configured_names(text, expected):
    from backend.discord_bot.common import message_calls_bot_by_name

    assert (
        message_calls_bot_by_name(
            text, {"talkinWakeWords": "Control Room, x, Control Room"}
        )
        is expected
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", False),
        ("Would that work", True),
        ("a plain statement", False),
        ("tell me?", True),
    ],
)
def test_unthreaded_question_detection(text, expected):
    from backend.discord_bot.common import message_looks_like_unthreaded_question

    assert message_looks_like_unthreaded_question(text) is expected


def test_bot_names_are_added_and_deduplicated():
    from backend.discord_bot.common import configured_talkin_wake_words

    names = configured_talkin_wake_words(
        {}, SimpleNamespace(display_name="Control", global_name="Conan", name="Control")
    )
    assert names.count("control") == 1 and "conan" in names


@pytest.mark.parametrize(
    "template,expected",
    [
        ("{user} in {channel}: {response}", "Fixture in channel: Reply"),
        ("{provider} {guild}", "provider guild\nReply"),
        ("", "Reply"),
    ],
)
def test_message_templates_preserve_reply_when_placeholder_missing(template, expected):
    from backend.discord_bot.common import apply_message_template

    message = SimpleNamespace(
        author=SimpleNamespace(name="Fixture"),
        channel=SimpleNamespace(name="channel"),
        guild=SimpleNamespace(name="guild"),
    )
    assert apply_message_template(template, "Reply", message, "provider") == expected


@pytest.mark.parametrize(
    "source,kind",
    [
        ("{random}", ""),
        ("{random:image}", "image"),
        ("{random:video}", "video"),
        ("{random:invalid}", None),
        ("https://fixture.invalid/image.png", None),
    ],
)
def test_random_media_tokens_only_accept_supported_kinds(source, kind):
    from backend.discord_bot.common import random_trigger_media_type

    assert random_trigger_media_type(source) == kind
