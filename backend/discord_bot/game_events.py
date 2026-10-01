from __future__ import annotations

import asyncio
from typing import Any

import discord

from ..firebase_client import FirestoreStore, MemoryStore
from ..game_limits import GameLimiter
from ..presentation import (
    interpret_action,
    send_message_feedback,
)
from .common import discord_profile_name, judge_guess_reply


class GameEventsMixin:
    game_limiter: GameLimiter
    _config_for: Any
    guessing_game_locks: Any
    store: MemoryStore | FirestoreStore

    def _guessing_game_lock(
        self, guild_id: int | str, channel_id: int | str, bot_message_id: int | str
    ) -> asyncio.Lock:
        key = f"{guild_id}:{channel_id}:{bot_message_id}"
        lock = self.guessing_game_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.guessing_game_locks[key] = lock
        return lock

    async def _handle_guessing_game_reply(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        reference = getattr(message, "reference", None)
        if reference is None:
            return False
        reply_message_id = str(
            getattr(reference, "message_id", "")
            or getattr(getattr(reference, "resolved", None), "id", "")
            or ""
        )
        if not reply_message_id:
            return False

        guild_id = str(message.guild.id)
        channel_id = str(message.channel.id)
        state = await self.store.get_guessing_game(
            guild_id, channel_id, reply_message_id
        )
        if not state:
            return False

        async with self._guessing_game_lock(guild_id, channel_id, reply_message_id):
            state = await self.store.get_guessing_game(
                guild_id, channel_id, reply_message_id
            )
            if not state:
                # Another simultaneous answer may have completed the round while this
                # message waited for the per-game lock. Consume it rather than routing
                # the stale guess into normal AI conversation handling.
                return True
            config = await self._config_for(guild_id)
            games = config.get("games", {})
            starter_id = str(state.get("starterId") or "")
            if (
                not games.get("guessSongAllowAnyone", True)
                and starter_id
                and str(message.author.id) != starter_id
            ):
                await send_message_feedback(
                    message,
                    config,
                    title="This clue belongs to someone else",
                    description="The current round only accepts guesses from the person who started it.",
                    kind="warning",
                    fields=[("Starter", f"<@{starter_id}>", True)],
                )
                return True

            user_guess = str(
                getattr(message, "clean_content", "") or getattr(message, "content", "")
            ).strip()
            if not user_guess:
                await send_message_feedback(
                    message,
                    config,
                    title="I need an actual guess",
                    description="Reply with the song title so the answer judge has something to work with.",
                    kind="warning",
                )
                return True

            answer = str(state.get("answer") or "")
            aliases = [str(item) for item in state.get("aliases") or []]
            hint = str(state.get("hint") or "")
            correct, judge_source = await judge_guess_reply(
                config,
                answer=answer,
                aliases=aliases,
                user_guess=user_guess,
                hint=hint,
            )
            attempts = int(state.get("attempts") or 0) + 1
            max_attempts = max(
                1,
                min(
                    int(
                        state.get("maxAttempts")
                        or games.get("guessSongMaxAttempts")
                        or 5
                    ),
                    20,
                ),
            )
            exhausted = not correct and attempts >= max_attempts
            remaining = max(0, max_attempts - attempts)

            if correct or exhausted:
                await self.store.delete_guessing_game(
                    guild_id, channel_id, reply_message_id
                )
                self.game_limiter.release_game(guild_id, channel_id, reply_message_id)
            else:
                state["attempts"] = attempts
                await self.store.set_guessing_game(
                    guild_id, channel_id, reply_message_id, state
                )

            if correct:
                outcome = "correct guess"
                facts = f"The user's guess '{user_guess}' matches the locked answer '{answer}'. Attempt {attempts} of {max_attempts}."
                title = "You got the song"
                kind = "success"
                result_fields = [
                    ("Your guess", user_guess, True),
                    ("Song", answer, True),
                    ("Attempts", f"{attempts}/{max_attempts}", True),
                ]
            elif exhausted:
                outcome = "attempts exhausted"
                facts = f"The user's guess '{user_guess}' is incorrect. The round ended after {attempts} attempts."
                title = "The mystery track wins this round"
                kind = "warning"
                result_fields = [
                    ("Last guess", user_guess, True),
                    ("Attempts", f"{attempts}/{max_attempts}", True),
                ]
                if games.get("guessSongRevealOnFailure", True):
                    result_fields.append(("Answer", answer, False))
            else:
                outcome = "incorrect guess"
                facts = f"The user's guess '{user_guess}' is incorrect. {remaining} attempts remain. Do not reveal the answer."
                title = "Not quite"
                kind = "game"
                result_fields = [
                    ("Your guess", user_guess, True),
                    ("Attempts left", str(remaining), True),
                    (
                        "Continue",
                        "Reply to the original clue with another title.",
                        False,
                    ),
                ]

            narration, narration_source = await interpret_action(
                config,
                feature=str(state.get("feature") or "guesssong"),
                outcome=outcome,
                facts=facts,
                actor_name=discord_profile_name(message.author),
            )
            await send_message_feedback(
                message,
                config,
                title=title,
                description=narration,
                kind=kind,
                template_key="game_guesssong",
                fields=result_fields,
                source_note=f"{judge_source} • {narration_source}",
            )
            await self.store.add_log(
                guild_id,
                "game.guess_answered",
                {
                    "feature": str(state.get("feature") or "guesssong"),
                    "channelId": channel_id,
                    "promptMessageId": reply_message_id,
                    "authorId": str(message.author.id),
                    "correct": correct,
                    "exhausted": exhausted,
                    "attempts": attempts,
                    "judge": judge_source,
                },
            )
            return True
