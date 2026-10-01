"""Atomic event-loop reservations for bounded concurrent Discord games."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .presentation import send_interaction_feedback


@dataclass
class GameLease:
    limiter: GameLimiter
    channel: str
    token: str
    held: bool = False

    def hold(self, seconds: float, message_id: str = "") -> None:
        self.held = True
        self.limiter.entries[self.token] = (
            self.channel,
            self.limiter.clock() + seconds,
            message_id,
        )

    def release(self) -> None:
        self.limiter.entries.pop(self.token, None)

    def expired(self) -> bool:
        entry = self.limiter.entries.get(self.token)
        return entry is None or entry[1] <= self.limiter.clock()


class GameLimiter:
    def __init__(
        self, capacity: int = 4096, clock: Callable[[], float] = time.monotonic
    ):
        self.capacity = capacity
        self.clock = clock
        self.entries: dict[str, tuple[str, float, str]] = {}

    def acquire(self, guild: str, channel: str, limit: int) -> GameLease | None:
        now = self.clock()
        for token, (_, expiry, _) in list(self.entries.items()):
            if expiry <= now:
                self.entries.pop(token, None)
        key = f"{guild}:{channel}"
        if (
            len(self.entries) >= self.capacity
            or sum(entry[0] == key for entry in self.entries.values()) >= limit
        ):
            return None
        token = uuid4().hex
        self.entries[token] = (key, now + 120, "")
        return GameLease(self, key, token)

    def release_game(self, guild: str, channel: str, message_id: str) -> None:
        key = f"{guild}:{channel}"
        for token, (owner, _, message) in list(self.entries.items()):
            if owner == key and message == message_id:
                self.entries.pop(token, None)


@asynccontextmanager
async def game_slot(interaction: Any, config: dict[str, Any]):
    lease = interaction.client.game_limiter.acquire(
        str(interaction.guild_id),
        str(interaction.channel_id),
        int(config.get("games", {}).get("maxActiveGamesPerChannel", 1)),
    )
    if lease is None:
        await send_interaction_feedback(
            interaction,
            config,
            title="Channel game limit reached",
            description="Finish the active game or wait for its timeout before starting another.",
            kind="warning",
            ephemeral=True,
        )
    try:
        yield lease
    finally:
        if lease is not None and not lease.held:
            lease.release()
