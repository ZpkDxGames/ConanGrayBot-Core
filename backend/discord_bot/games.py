from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from ..game_limits import GameLease, game_slot
from ..presentation import (
    build_feedback_embed,
    interpret_action,
    send_interaction_feedback,
)
from .common import configured_guess_song_rounds, discord_profile_name, log
from .responses import (
    ensure_command_enabled,
    get_interaction_config,
    send_action_result,
)

if TYPE_CHECKING:
    from .client import ConanBot


def make_coinflip_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(name="coinflip", description="Flip a dramatic little coin.")
    async def coinflip(interaction: discord.Interaction) -> None:
        if not await ensure_command_enabled(interaction, "coinflip"):
            return
        config = await get_interaction_config(interaction)
        async with game_slot(interaction, config) as lease:
            if lease is None:
                return
            games = config.get("games", {})
            if not games.get("coinflipEnabled", True):
                await send_interaction_feedback(
                    interaction,
                    config,
                    title="Coinflip unavailable",
                    description="Coinflip is disabled from the dashboard.",
                    kind="warning",
                    ephemeral=True,
                )
                return
            heads = str(games.get("coinflipHeadsLabel") or "Heads")
            tails = str(games.get("coinflipTailsLabel") or "Tails")
            result = random.choice([heads, tails])
            message = str(
                games.get("coinflipMessage") or "The universe made a tiny decision."
            )
            await send_action_result(
                interaction,
                config,
                feature="coinflip",
                title="The coin has spoken",
                outcome=result,
                facts=f"The deterministic coin result is {result}. Dashboard message: {message}",
                fields=[
                    ("Result", f"**{result}**", True),
                    ("Official statement", message, False),
                ],
                kind="game",
            )

    return coinflip


def make_eightball_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(
        name="8ball", description="Ask the emotionally suspicious 8-ball."
    )
    @app_commands.describe(question="What do you want to ask?")
    async def eightball(interaction: discord.Interaction, question: str) -> None:
        if not await ensure_command_enabled(interaction, "eightball"):
            return
        config = await get_interaction_config(interaction)
        async with game_slot(interaction, config) as lease:
            if lease is None:
                return
            games = config.get("games", {})
            if not games.get("eightballEnabled", True):
                await send_interaction_feedback(
                    interaction,
                    config,
                    title="8-ball unavailable",
                    description="The 8-ball is disabled from the dashboard.",
                    kind="warning",
                    ephemeral=True,
                )
                return
            answers = games.get("eightballAnswers") or [
                "The vibes say yes.",
                "No, but dramatically.",
            ]
            answer = str(random.choice(answers))
            await send_action_result(
                interaction,
                config,
                feature="eightball",
                title="The emotionally suspicious 8-ball",
                outcome=answer,
                facts=f"Question: {question}. Selected answer: {answer}.",
                fields=[
                    ("You asked", question, False),
                    ("The answer", f"**{answer}**", False),
                ],
                kind="game",
            )

    return eightball


def make_rps_command(bot: ConanBot) -> app_commands.Command:
    choices = [
        app_commands.Choice(name="Rock", value="rock"),
        app_commands.Choice(name="Paper", value="paper"),
        app_commands.Choice(name="Scissors", value="scissors"),
    ]

    @app_commands.command(
        name="rps", description="Play rock paper scissors against the bot."
    )
    @app_commands.describe(choice="Your move")
    @app_commands.choices(choice=choices)
    async def rps(
        interaction: discord.Interaction, choice: app_commands.Choice[str]
    ) -> None:
        if not await ensure_command_enabled(interaction, "rps"):
            return
        config = await get_interaction_config(interaction)
        async with game_slot(interaction, config) as lease:
            if lease is None:
                return
            games = config.get("games", {})
            if not games.get("rpsEnabled", True):
                await send_interaction_feedback(
                    interaction,
                    config,
                    title="Game unavailable",
                    description="Rock Paper Scissors is disabled from the dashboard.",
                    kind="warning",
                    ephemeral=True,
                )
                return
            bot_choice = random.choice(["rock", "paper", "scissors"])
            beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
            if choice.value == bot_choice:
                outcome = "draw"
                result = games.get("rpsDrawMessage") or "Draw. We are equally dramatic."
            elif beats[choice.value] == bot_choice:
                outcome = "you win"
                result = (
                    games.get("rpsWinMessage")
                    or "You win. I will stare out a window about it."
                )
            else:
                outcome = "bot wins"
                result = games.get("rpsLoseMessage") or "I win. Very humble of me."
            await send_action_result(
                interaction,
                config,
                feature="rps",
                title="Rock, paper, emotional consequences",
                outcome=outcome,
                facts=f"User chose {choice.value}. Bot chose {bot_choice}. Outcome: {outcome}. Configured response: {result}",
                fields=[
                    ("Your move", choice.value.title(), True),
                    ("Bot move", bot_choice.title(), True),
                    ("Result", str(result), False),
                ],
                kind="game",
            )

    return rps


def make_guesssong_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(
        name="guesssong",
        description="Start a reply-driven AI-judged song guessing round.",
    )
    async def guesssong(interaction: discord.Interaction) -> None:
        if not await ensure_command_enabled(interaction, "guesssong"):
            return
        config = await get_interaction_config(interaction)
        async with game_slot(interaction, config) as lease:
            if lease is None:
                return
            games = config.get("games", {})
            if not games.get("guessSongEnabled", False):
                await send_interaction_feedback(
                    interaction,
                    config,
                    title="Game unavailable",
                    description="Guess the Song is disabled from the dashboard.",
                    kind="warning",
                    ephemeral=True,
                )
                return
            rounds = configured_guess_song_rounds(games)
            if not rounds:
                await send_interaction_feedback(
                    interaction,
                    config,
                    title="No mystery tracks configured",
                    description="Add at least one structured song round in Games → Guess the Song.",
                    kind="warning",
                    fields=[("Format", "`Answer | alias 1, alias 2 | Hint`", False)],
                    ephemeral=True,
                )
                return

            prompt = str(
                games.get("guessSongPrompt")
                or "Guess the Conan-coded song from this hint:"
            )
            round_data = random.choice(rounds)
            answer = str(round_data["answer"])
            aliases = [str(item) for item in round_data.get("aliases") or []]
            hint = str(round_data["hint"])
            max_attempts = max(1, min(int(games.get("guessSongMaxAttempts") or 5), 20))
            sent = await send_action_result(
                interaction,
                config,
                feature="guesssong",
                title="Mystery track",
                outcome="new clue",
                facts=f"Prompt: {prompt}. Selected hint: {hint}. The answer is locked and must not be revealed.",
                fields=[
                    ("Prompt", prompt, False),
                    ("Clue", hint, False),
                    (
                        "How to play",
                        "Reply directly to this message with the song title.",
                        False,
                    ),
                    ("Attempts", str(max_attempts), True),
                    ("Judge", "AI-assisted with deterministic fallback", True),
                ],
                kind="game",
            )

            bot_message_id = str(getattr(sent, "id", "") or "")
            if not bot_message_id:
                try:
                    original = await interaction.original_response()
                    bot_message_id = str(getattr(original, "id", "") or "")
                except Exception:
                    log.exception("Could not resolve /guesssong response message ID")
            if not bot_message_id:
                await bot.store.add_log(
                    str(interaction.guild_id or bot.settings.guild_id or "global"),
                    "game.guesssong_state_failed",
                    {
                        "reason": "missing_response_message_id",
                        "channelId": str(getattr(interaction, "channel_id", "") or ""),
                    },
                )
                return

            timeout_minutes = max(
                1, min(int(games.get("guessSongRoundTimeoutMinutes") or 10), 1440)
            )
            guild_id = str(interaction.guild_id or bot.settings.guild_id or "global")
            channel_id = str(
                getattr(interaction, "channel_id", "")
                or getattr(getattr(interaction, "channel", None), "id", "")
            )
            await bot.store.set_guessing_game(
                guild_id,
                channel_id,
                bot_message_id,
                {
                    "feature": "guesssong",
                    "answer": answer,
                    "aliases": aliases,
                    "hint": hint,
                    "starterId": str(interaction.user.id),
                    "attempts": 0,
                    "maxAttempts": max_attempts,
                    "expiresAt": (
                        datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)
                    ).isoformat(),
                },
            )
            lease.hold(timeout_minutes * 60, bot_message_id)
            await bot.store.add_log(
                guild_id,
                "game.guesssong_started",
                {
                    "channelId": channel_id,
                    "promptMessageId": bot_message_id,
                    "starterId": str(interaction.user.id),
                    "maxAttempts": max_attempts,
                    "timeoutMinutes": timeout_minutes,
                },
            )

    return guesssong


def make_wouldyourather_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(
        name="wouldyourather", description="Ask a dramatic would-you-rather question."
    )
    async def wouldyourather(interaction: discord.Interaction) -> None:
        if not await ensure_command_enabled(interaction, "wouldyourather"):
            return
        config = await get_interaction_config(interaction)
        async with game_slot(interaction, config) as lease:
            if lease is None:
                return
            games = config.get("games", {})
            if not games.get("wouldYouRatherEnabled", False):
                await send_interaction_feedback(
                    interaction,
                    config,
                    title="Game unavailable",
                    description="Would You Rather is disabled from the dashboard.",
                    kind="warning",
                    ephemeral=True,
                )
                return
            questions = games.get("wouldYouRatherQuestions") or [
                "Would you rather be dramatic forever or emotionally stable for one day?"
            ]
            question = str(random.choice(questions))
            await send_action_result(
                interaction,
                config,
                feature="wouldyourather",
                title="Choose your tiny crisis",
                outcome="question selected",
                facts=question,
                fields=[
                    ("Would you rather…", question, False),
                    (
                        "Rules",
                        "Pick one. Defend it like the bridge depends on it.",
                        False,
                    ),
                ],
                kind="game",
            )

    return wouldyourather


class TicTacToeView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        opponent_id: int | None = None,
        config: dict[str, Any] | None = None,
        lease: GameLease | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.lease = lease
        self.owner_id = owner_id
        self.opponent_id = opponent_id
        self.config = config or {}
        self.games_config = self.config.get("games", {})
        self.turn = "X"
        self.board = [""] * 9
        self.move_lock = asyncio.Lock()
        for index in range(9):
            self.add_item(TicTacToeButton(index))

    async def on_timeout(self) -> None:
        self.stop()
        self.disable_board()
        if self.lease is not None:
            self.lease.release()

    def current_player_id(self) -> int | None:
        return self.owner_id if self.turn == "X" else self.opponent_id

    def winner(self) -> str | None:
        wins = [
            (0, 1, 2),
            (3, 4, 5),
            (6, 7, 8),
            (0, 3, 6),
            (1, 4, 7),
            (2, 5, 8),
            (0, 4, 8),
            (2, 4, 6),
        ]
        for a, b, c in wins:
            if self.board[a] and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return "draw" if all(self.board) else None

    def board_text(self) -> str:
        cells = [mark or "·" for mark in self.board]
        return "\n".join("  ".join(cells[row : row + 3]) for row in (0, 3, 6))

    def disable_board(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

    async def make_bot_move_if_needed(self) -> None:
        if self.opponent_id is not None or self.turn != "O" or self.winner():
            return
        empty = [i for i, mark in enumerate(self.board) if not mark]
        if not empty:
            return
        choice = random.choice(empty)
        self.board[choice] = "O"
        button = self.children[choice]
        if isinstance(button, TicTacToeButton):
            button.label = "O"
            button.disabled = True
            button.style = discord.ButtonStyle.danger
        self.turn = "X"

    async def finish_or_update(self, interaction: discord.Interaction) -> None:
        winner = self.winner()
        if winner:
            self.stop()
            if self.lease is not None:
                self.lease.release()
            self.disable_board()
            if winner == "draw":
                outcome = "draw"
                result = str(
                    self.games_config.get("ticTacToeDrawMessage")
                    or "Tic-tac-toe ended in a draw. Very emotionally neutral."
                )
            else:
                outcome = f"{winner} wins"
                template = str(
                    self.games_config.get("ticTacToeWinMessage")
                    or "{winner} won. The drama has concluded."
                )
                result = template.replace("{winner}", winner)
            narration, source_note = await interpret_action(
                self.config,
                feature="tictactoe",
                outcome=outcome,
                facts=f"Final board: {self.board_text()}. Result: {result}",
                actor_name=discord_profile_name(interaction.user),
            )
            embed = build_feedback_embed(
                self.config,
                title="Tic-tac-toe finale",
                description=narration,
                kind="game",
                template_key="game_tictactoe",
                fields=[
                    ("Final board", f"```\n{self.board_text()}\n```", False),
                    ("Result", result, False),
                ],
                actor=interaction.user,
                source_note=source_note,
            )
            await interaction.edit_original_response(
                content=None, embed=embed, view=self
            )
            return

        embed = build_feedback_embed(
            self.config,
            title="Tic-tac-toe in progress",
            description="The board is still open. Choose carefully; every square is now somehow a personality test.",
            kind="game",
            template_key="game_tictactoe",
            fields=[
                ("Board", f"```\n{self.board_text()}\n```", False),
                ("Current turn", self.turn, True),
            ],
            actor=interaction.user,
        )
        await interaction.edit_original_response(content=None, embed=embed, view=self)


class TicTacToeButton(discord.ui.Button):
    def __init__(self, index: int) -> None:
        super().__init__(label="·", row=index // 3, style=discord.ButtonStyle.secondary)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TicTacToeView)
        async with view.move_lock:
            if view.is_finished():
                return
            if view.lease is not None and view.lease.expired():
                await view.on_timeout()
                await send_interaction_feedback(
                    interaction,
                    view.config,
                    title="Game expired",
                    description="Start a new game to play again.",
                    kind="warning",
                    ephemeral=True,
                )
                return
            await self._apply_move(interaction)

    async def _apply_move(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TicTacToeView)
        current = view.current_player_id()
        if current and interaction.user.id != current:
            await send_interaction_feedback(
                interaction,
                view.config,
                title="Not your turn",
                description="That square belongs to the other player's current emotional journey.",
                kind="warning",
                ephemeral=True,
            )
            return
        if view.board[self.index]:
            await send_interaction_feedback(
                interaction,
                view.config,
                title="Square unavailable",
                description="That square is already occupied and carrying enough narrative weight.",
                kind="warning",
                ephemeral=True,
            )
            return

        view.board[self.index] = view.turn
        self.label = view.turn
        self.disabled = True
        self.style = (
            discord.ButtonStyle.success
            if view.turn == "X"
            else discord.ButtonStyle.danger
        )
        view.turn = "O" if view.turn == "X" else "X"
        await interaction.response.defer()
        await view.make_bot_move_if_needed()
        await view.finish_or_update(interaction)


def make_tictactoe_command(bot: ConanBot) -> app_commands.Command:
    @app_commands.command(name="tictactoe", description="Start a tic-tac-toe game.")
    @app_commands.describe(
        opponent="Optional opponent. Leave empty to play against the bot."
    )
    async def tictactoe(
        interaction: discord.Interaction, opponent: discord.Member | None = None
    ) -> None:
        if not await ensure_command_enabled(interaction, "tictactoe"):
            return
        config = await get_interaction_config(interaction)
        async with game_slot(interaction, config) as lease:
            if lease is None:
                return
            games = config.get("games", {})
            if not games.get("ticTacToeEnabled", True):
                await send_interaction_feedback(
                    interaction,
                    config,
                    title="Game unavailable",
                    description="Tic-tac-toe is disabled from the dashboard.",
                    kind="warning",
                    ephemeral=True,
                )
                return
            if not games.get("ticTacToeAllowBotOpponent", True) and opponent is None:
                await send_interaction_feedback(
                    interaction,
                    config,
                    title="Opponent required",
                    description="The bot opponent is disabled. Choose another server member.",
                    kind="warning",
                    ephemeral=True,
                )
                return
            if opponent and opponent.bot:
                opponent = None
            view = TicTacToeView(
                interaction.user.id, opponent.id if opponent else None, config, lease
            )
            opponent_text = (
                opponent.mention
                if opponent
                else "Conan Gray Bot's extremely questionable strategy"
            )
            await send_action_result(
                interaction,
                config,
                feature="tictactoe",
                title="Tic-tac-toe opening scene",
                outcome="game started",
                facts=f"Player X is {interaction.user.mention}. Player O is {opponent_text}. X moves first.",
                fields=[
                    (
                        "Players",
                        f"{interaction.user.mention} **vs.** {opponent_text}",
                        False,
                    ),
                    ("Opening turn", "X", True),
                    ("Board", "```\n·  ·  ·\n·  ·  ·\n·  ·  ·\n```", False),
                ],
                kind="game",
                view=view,
            )
            lease.hold(300)

    return tictactoe
