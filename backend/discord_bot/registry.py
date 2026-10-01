from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from .admin import make_admin_group
from .games import (
    make_coinflip_command,
    make_eightball_command,
    make_guesssong_command,
    make_rps_command,
    make_tictactoe_command,
    make_wouldyourather_command,
)
from .media import make_media_command
from .utility import (
    make_forget_command,
    make_help_command,
    make_lyrics_command,
    make_motivation_command,
    make_ping_command,
    make_pun_command,
    make_recommend_command,
)
from .weather import make_weather_command

if TYPE_CHECKING:
    from .client import ConanBot


def application_command_factories() -> tuple[
    tuple[str, Callable[[ConanBot], Any]], ...
]:
    return (
        ("ping", make_ping_command),
        ("help", make_help_command),
        ("weather", make_weather_command),
        ("media", make_media_command),
        ("pun", make_pun_command),
        ("motivation", make_motivation_command),
        ("recommend", make_recommend_command),
        ("lyrics", make_lyrics_command),
        ("tictactoe", make_tictactoe_command),
        ("coinflip", make_coinflip_command),
        ("eightball", make_eightball_command),
        ("rps", make_rps_command),
        ("guesssong", make_guesssong_command),
        ("wouldyourather", make_wouldyourather_command),
        ("forget", make_forget_command),
        ("admin", make_admin_group),
    )
