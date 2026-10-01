from __future__ import annotations

import asyncio
import random
import re
from typing import Any

import discord

from ..ai_providers import AIProviderError, ask_ai
from ..config import Settings
from ..firebase_client import FirestoreStore, MemoryStore
from ..presentation import (
    send_message_feedback,
)
from .common import (
    _NoopAsyncContext,
    apply_message_template,
    configured_admin_role_id,
    configured_talkin_wake_words,
    discord_profile_name,
    log,
    member_has_admin_role,
    message_calls_bot_by_name,
    message_looks_like_unthreaded_question,
    trim_conversation_history,
)
from .responses import send_styled_reply


class AiChatMixin:
    _config_for: Any
    _handle_weather_chat: Any
    ai_cooldowns: Any
    ai_session_locks: Any
    clear_ai_session: Any
    get_channel: Any
    guilds: Any
    is_closed: Any
    settings: Settings
    store: MemoryStore | FirestoreStore
    talkin_last_activity: Any
    talkin_last_spontaneous: Any
    talkin_last_spontaneous_check: Any
    user: Any

    def _note_talkin_activity(self, guild_id: int | str, channel_id: int | str) -> None:
        key = f"{guild_id}:{channel_id}"
        self.talkin_last_activity[key] = asyncio.get_running_loop().time()

    async def _start_spontaneous_conversation(
        self,
        guild: Any,
        channel: Any,
        config: dict[str, Any],
    ) -> bool:
        ai_config = config.get("ai", {})
        guild_id = str(guild.id)
        channel_id = str(channel.id)
        branch_id = "talkin-group"
        lock = self._ai_session_lock(guild_id, channel_id)
        async with lock:
            session = await self.store.get_branch_session(
                guild_id, channel_id, branch_id
            )
            history = list(session.get("messages") or [])
            max_history = max(
                4, min(int(ai_config.get("maxHistoryMessages") or 36), 80)
            )
            history = trim_conversation_history(history, max_history)
            prompt = str(ai_config.get("spontaneousPrompt") or "").strip() or (
                "Start one short, natural group-chat thought. Do not mention automation or inactivity."
            )
            context = (
                f"Current Discord channel: #{getattr(channel, 'name', 'talkin')}. "
                "This is the dedicated Talkin' group chat. You are choosing to speak first after a quiet stretch, "
                "like a normal participant who had a thought and decided to send it. Write one standalone message, "
                "not a reply. It can be an observation, callback, tiny anecdote, or casual question. "
                "Use recent shared history only when it is genuinely relevant. Do not mention timers, automation, "
                "the bot, the channel being quiet, or that you were instructed to start a conversation."
            )
            typing_context = (
                channel.typing()
                if ai_config.get("typingIndicator", True) and hasattr(channel, "typing")
                else _NoopAsyncContext()
            )
            async with typing_context:
                try:
                    answer, provider = await ask_ai(config, history, prompt, context)
                except AIProviderError:
                    await self.store.add_log(
                        guild_id,
                        "ai.spontaneous_failed",
                        {"channelId": channel_id, "branchId": branch_id},
                    )
                    return False
            try:
                sent = await channel.send(answer)
            except (discord.HTTPException, discord.Forbidden, AttributeError):
                log.exception(
                    "Could not send a spontaneous Talkin' message in guild %s", guild_id
                )
                return False

            sent_id = str(getattr(sent, "id", "") or "")
            history.append(
                {
                    "role": "system",
                    "content": "Conan naturally started a new group-chat beat without being prompted by a user.",
                    "spontaneous": True,
                }
            )
            history.append(
                {
                    "role": "assistant",
                    "content": answer,
                    "messageId": sent_id,
                    "branchId": branch_id,
                    "spontaneous": True,
                }
            )
            stored_history = trim_conversation_history(history, max_history)
            retained_message_ids = [
                str(item.get("messageId") or "")
                for item in stored_history
                if str(item.get("messageId") or "")
            ]
            await self.store.set_branch_session(
                guild_id,
                channel_id,
                branch_id,
                stored_history,
                latest_bot_message_id=sent_id,
                root_message_id=str(session.get("rootMessageId") or sent_id),
                message_ids=retained_message_ids,
                make_active=True,
            )
            await self.store.add_log(
                guild_id,
                "ai.spontaneous_started",
                {
                    "channelId": channel_id,
                    "branchId": branch_id,
                    "provider": provider,
                    "messageId": sent_id,
                },
            )
            return True

    async def _run_spontaneous_chat_checks(self) -> None:
        now = asyncio.get_running_loop().time()
        for guild in list(self.guilds):
            try:
                config = await self._config_for(guild.id)
                ai_config = config.get("ai", {})
                if not ai_config.get("enabled", True) or not ai_config.get(
                    "spontaneousConversationEnabled", True
                ):
                    continue
                channel_id = str(
                    ai_config.get("channelId") or self.settings.ai_channel_id or ""
                )
                if not channel_id:
                    continue
                channel: Any = (
                    guild.get_channel(int(channel_id))
                    if hasattr(guild, "get_channel")
                    else None
                )
                if channel is None:
                    channel = self.get_channel(int(channel_id))
                if channel is None or not hasattr(channel, "send"):
                    continue

                key = f"{guild.id}:{channel_id}"
                self.talkin_last_activity.setdefault(key, now)
                self.talkin_last_spontaneous_check.setdefault(key, now)
                check_minutes = max(
                    1, min(int(ai_config.get("spontaneousCheckMinutes") or 10), 1440)
                )
                if now - self.talkin_last_spontaneous_check[key] < check_minutes * 60:
                    continue
                self.talkin_last_spontaneous_check[key] = now

                idle_minutes = max(
                    5, min(int(ai_config.get("spontaneousIdleMinutes") or 90), 10080)
                )
                cooldown_minutes = max(
                    15,
                    min(int(ai_config.get("spontaneousCooldownMinutes") or 240), 43200),
                )
                chance_percent = max(
                    0.0,
                    min(float(ai_config.get("spontaneousChancePercent") or 0), 100.0),
                )
                if now - self.talkin_last_activity[key] < idle_minutes * 60:
                    continue
                last_spontaneous = self.talkin_last_spontaneous.get(key)
                if (
                    last_spontaneous is not None
                    and now - last_spontaneous < cooldown_minutes * 60
                ):
                    continue
                if random.random() * 100 >= chance_percent:
                    continue
                if await self._start_spontaneous_conversation(guild, channel, config):
                    self.talkin_last_spontaneous[key] = now
                    self.talkin_last_activity[key] = now
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Spontaneous Talkin' check failed for guild %s",
                    getattr(guild, "id", "unknown"),
                )

    async def _spontaneous_chat_loop(self) -> None:
        try:
            while not self.is_closed():
                await asyncio.sleep(60)
                await self._run_spontaneous_chat_checks()
        except asyncio.CancelledError:
            raise

    def _ai_session_lock(
        self, guild_id: int | str, channel_id: int | str
    ) -> asyncio.Lock:
        key = f"{guild_id}:{channel_id}"
        lock = self.ai_session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.ai_session_locks[key] = lock
        return lock

    async def _handle_ai_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        config = await self._config_for(message.guild.id)
        ai_config = config.get("ai", {})
        ai_enabled = bool(ai_config.get("enabled", True))

        guild_id = str(message.guild.id)
        channel_key = str(message.channel.id)
        talkin_channel_id = str(
            ai_config.get("channelId") or self.settings.ai_channel_id or ""
        )
        is_talkin_channel = bool(talkin_channel_id and channel_key == talkin_channel_id)

        raw_text = str(
            getattr(message, "clean_content", "")
            or getattr(message, "content", "")
            or ""
        )
        is_mentioned = self.user is not None and self.user in message.mentions
        name_call = bool(
            is_talkin_channel
            and ai_config.get("talkinRespondToNameCalls", True)
            and message_calls_bot_by_name(raw_text, ai_config, self.user)
        )

        reply_message_id = ""
        resolved_reply = None
        if message.reference:
            resolved_reply = getattr(message.reference, "resolved", None)
            reply_message_id = str(
                getattr(message.reference, "message_id", "")
                or getattr(resolved_reply, "id", "")
                or ""
            )
            if (
                resolved_reply is None
                and reply_message_id
                and hasattr(message.channel, "fetch_message")
            ):
                try:
                    resolved_reply = await message.channel.fetch_message(
                        int(reply_message_id)
                    )
                except (
                    discord.HTTPException,
                    discord.NotFound,
                    discord.Forbidden,
                    TypeError,
                    ValueError,
                ):
                    resolved_reply = None

        resolved_author = (
            getattr(resolved_reply, "author", None)
            if resolved_reply is not None
            else None
        )
        bot_user_id = str(getattr(self.user, "id", "") or "")
        resolved_author_id = str(getattr(resolved_author, "id", "") or "")
        reply_to_bot = bool(bot_user_id and resolved_author_id == bot_user_id)
        reply_to_other_user = bool(resolved_author_id and not reply_to_bot)

        category_id = str(getattr(message.channel, "category_id", "") or "")
        if not category_id:
            parent = getattr(message.channel, "parent", None)
            category_id = str(getattr(parent, "category_id", "") or "")
        authorized_category_id = str(
            config.get("games", {}).get("allowedCategoryId")
            or self.settings.allowed_category_id
            or ""
        )

        reset_keyword = str(ai_config.get("resetKeyword") or "").strip().lower()
        is_reset_request = bool(
            reset_keyword and raw_text.strip().lower() == reset_keyword
        )
        activation_reason = ""

        if is_talkin_channel:
            self._note_talkin_activity(guild_id, channel_key)
            if raw_text.strip().lower().startswith("c!"):
                return

            activation_mode = (
                str(ai_config.get("talkinActivationMode") or "direct_calls")
                .strip()
                .lower()
            )
            bot_reply_call = bool(
                ai_config.get("talkinRespondToBotReplies", True) and reply_to_bot
            )
            implicit_question = bool(
                activation_mode == "direct_calls_and_questions"
                and not message.reference
                and message_looks_like_unthreaded_question(raw_text)
            )
            legacy_all_messages = activation_mode == "all_messages"
            explicit_call = is_mentioned or name_call or bot_reply_call

            if (
                reply_to_other_user
                and ai_config.get("talkinIgnoreRepliesToOthers", True)
                and not (is_mentioned or name_call)
                and not is_reset_request
            ):
                return
            if not (
                explicit_call
                or implicit_question
                or legacy_all_messages
                or is_reset_request
            ):
                return

            if is_mentioned:
                activation_reason = "a direct @mention"
            elif bot_reply_call:
                activation_reason = "a direct reply to one of your messages"
            elif name_call:
                activation_reason = "the speaker calling your name"
            elif implicit_question:
                activation_reason = (
                    "an unthreaded question allowed by the channel settings"
                )
            elif legacy_all_messages:
                activation_reason = "legacy every-message mode"
            else:
                activation_reason = "the configured reset keyword"
        else:
            if (
                not authorized_category_id
                or category_id != authorized_category_id
                or not is_mentioned
            ):
                return
            activation_reason = "a direct @mention in an authorized channel"

        mapped_reply_branch = None
        if reply_message_id and ai_config.get("replyContinuesBranch", True):
            mapped_reply_branch = await self.store.resolve_reply_branch(
                guild_id, channel_key, reply_message_id
            )

        text = raw_text
        if self.user:
            user_id = str(getattr(self.user, "id", "") or "")
            if user_id:
                text = re.sub(rf"<@!?{re.escape(user_id)}>", "", text)
            for bot_name in {
                getattr(self.user, "display_name", ""),
                getattr(self.user, "name", ""),
            }:
                if bot_name:
                    text = re.sub(rf"@?{re.escape(bot_name)}", "", text, flags=re.I)
            for wake_word in configured_talkin_wake_words(ai_config, self.user):
                pattern = re.escape(wake_word).replace(r"\ ", r"\s+")
                text = re.sub(
                    rf"^\s*(?:(?:hey|hi|yo|okay|ok)\s+)?{pattern}\s*[,.:!?—-]*\s*",
                    "",
                    text,
                    count=1,
                    flags=re.I,
                )
            text = text.strip()
        is_reset_request = bool(reset_keyword and text.strip().lower() == reset_keyword)
        if not text:
            text = str(
                ai_config.get("emptyMessagePrompt")
                or "The user only called your name. Reply with a tiny natural acknowledgement."
            )

        async def send_runtime_notice(
            content: str, *, kind: str = "warning", title: str = "Conan paused"
        ) -> None:
            if is_talkin_channel and ai_config.get("talkinPlainReplies", True):
                await message.reply(content, mention_author=False)
                return
            await send_message_feedback(
                message,
                config,
                title=title,
                description=content,
                kind=kind,
            )

        if is_reset_request:
            if ai_config.get(
                "resetKeywordAdminOnly", True
            ) and not member_has_admin_role(message.author, config, self.settings):
                denied = (
                    config.get("admin", {}).get("deniedMessage")
                    or "You need the configured bot-admin role to do that."
                )
                await send_message_feedback(
                    message,
                    config,
                    title="Memory reset denied",
                    description=str(denied),
                    kind="error",
                    fields=[
                        (
                            "Required role",
                            f"<@&{configured_admin_role_id(config, self.settings)}>",
                            False,
                        )
                    ],
                )
                return
            await self.clear_ai_session(message.guild.id, message.channel.id)
            cleared = (
                config.get("admin", {}).get("memoryClearedMessage")
                or "Shared memory for this channel has been cleared."
            )
            await send_message_feedback(
                message,
                config,
                title="Group memory cleared"
                if is_talkin_channel
                else "Branch memory cleared",
                description=str(cleared),
                kind="admin",
                fields=[
                    ("Scope", "Current channel", True),
                    ("Status", "Cleared", True),
                ],
            )
            await self.store.add_log(
                guild_id,
                "memory.cleared",
                {
                    "channelId": channel_key,
                    "source": "keyword",
                    "actorId": str(message.author.id),
                },
            )
            return

        if await self._handle_weather_chat(message, config, text):
            return
        if not ai_enabled:
            return

        cooldown = int(ai_config.get("channelCooldownSeconds") or 0)
        cooldown_key = f"{guild_id}:{channel_key}"
        if cooldown > 0:
            now = asyncio.get_running_loop().time()
            until = self.ai_cooldowns.get(cooldown_key, 0)
            if now < until:
                remaining = round(until - now)
                await send_runtime_notice(
                    f"Give me about {remaining} more seconds, then send that again."
                )
                return
            self.ai_cooldowns[cooldown_key] = now + cooldown

        lock = self._ai_session_lock(message.guild.id, message.channel.id)
        async with lock:
            talkin_group_mode = is_talkin_channel and bool(
                ai_config.get("talkinGroupMode", True)
            )
            shared_channel_memory = (
                True
                if talkin_group_mode
                else bool(ai_config.get("sharedChannelMemory", True))
            )
            mention_starts_branch = bool(ai_config.get("mentionStartsNewBranch", True))
            reply_continues_branch = bool(ai_config.get("replyContinuesBranch", True))
            branch_reason = "shared_channel"
            root_message_id = str(message.id)

            branch_id: str | None
            if talkin_group_mode:
                branch_id = "talkin-group"
                session = await self.store.get_branch_session(
                    guild_id, channel_key, branch_id
                )
                root_message_id = str(session.get("rootMessageId") or message.id)
                branch_reason = f"talkin_{activation_reason.replace(' ', '_')}"
            elif is_mentioned and mention_starts_branch:
                branch_id = f"mention-{message.id}"
                session = {"messages": [], "rootMessageId": root_message_id}
                branch_reason = "mention_started_shared"
            elif reply_message_id and reply_continues_branch:
                branch_id = mapped_reply_branch or ""
                if not branch_id and shared_channel_memory:
                    branch_id = await self.store.get_active_branch(
                        guild_id, channel_key
                    )
                    if branch_id:
                        branch_reason = "active_shared_branch_fallback"
                if branch_id:
                    session = await self.store.get_branch_session(
                        guild_id, channel_key, branch_id
                    )
                    root_message_id = str(session.get("rootMessageId") or message.id)
                    if branch_reason == "shared_channel":
                        branch_reason = "shared_reply_continued"
                else:
                    branch_id = f"reply-{message.id}"
                    session = {"messages": [], "rootMessageId": root_message_id}
                    branch_reason = "untracked_reply_started"
            else:
                branch_id = (
                    await self.store.get_active_branch(guild_id, channel_key)
                    if shared_channel_memory
                    else ""
                )
                if not branch_id:
                    branch_id = "shared"
                session = await self.store.get_branch_session(
                    guild_id, channel_key, branch_id
                )
                root_message_id = str(session.get("rootMessageId") or message.id)

            if shared_channel_memory:
                await self.store.set_active_branch(guild_id, channel_key, branch_id)

            history = list(session.get("messages") or [])
            max_history = max(
                4, min(int(ai_config.get("maxHistoryMessages") or 36), 80)
            )
            history = trim_conversation_history(history, max_history)
            profile_name = discord_profile_name(message.author)
            user_line = f"{profile_name}: {text}"
            reply_note = ""
            if resolved_reply is not None and resolved_author is not None:
                target_name = discord_profile_name(resolved_author)
                target_text = str(
                    getattr(resolved_reply, "clean_content", "")
                    or getattr(resolved_reply, "content", "")
                    or ""
                ).strip()
                if reply_to_bot:
                    reply_note = " The current message is a direct reply to one of your previous messages."
                elif target_text:
                    reply_note = (
                        f" The current message is threaded as a reply to {target_name}'s message: {target_text[:500]!r}. "
                        "That reply target is another user, not you. Only join because the current speaker explicitly called you; "
                        "do not pretend their reply was addressed to you or answer on the other user's behalf."
                    )
                else:
                    reply_note = (
                        f" The current message is threaded as a reply to {target_name}, another user. "
                        "Only join because the current speaker explicitly called you."
                    )

            if talkin_group_mode:
                context = (
                    f"Current Discord channel: #{getattr(message.channel, 'name', 'unknown')}. "
                    f"Current speaker: {profile_name} (Discord user ID {message.author.id}). "
                    "This is the dedicated Talkin' channel, an iMessage-style group chat. "
                    f"You are replying because of {activation_reason}. "
                    "Respond naturally to the current speaker and keep track of exactly who is speaking and who their Discord reply targets. "
                    "Do not jump into unrelated user-to-user replies. Do not announce names, routing rules, or that you were activated. "
                    "The bot will attach your answer as a direct Discord reply without pinging the author. "
                    "Use one shared channel history across everyone while keeping each person's identity, preferences, and claims separate. "
                    "Speaker labels at the start of stored user messages only identify who said each line; never repeat those labels in the answer."
                    + reply_note
                )
            else:
                context = (
                    f"Current Discord channel: #{getattr(message.channel, 'name', 'unknown')}. "
                    f"Current speaker: {profile_name} (Discord user ID {message.author.id}). "
                    f"Conversation branch: {branch_id}. "
                    "You were directly @mentioned inside an authorized Conan channel. Answer the current speaker naturally. "
                    "Do not mention the routing rules, the category, or the fact that an @mention was required. "
                    "Carry relevant branch context across speakers while keeping each person's identity, preferences, and claims separate."
                    + reply_note
                )

            typing_context = (
                message.channel.typing()
                if ai_config.get("typingIndicator", True)
                else _NoopAsyncContext()
            )
            async with typing_context:
                try:
                    answer, provider = await ask_ai(config, history, user_line, context)
                except AIProviderError:
                    await send_runtime_notice(
                        "I lost the thread for a second. Send that once more and I'll pick it back up.",
                        kind="error",
                        title="The AI went off-script",
                    )
                    await self.store.add_log(
                        guild_id,
                        "ai.failed",
                        {"channelId": channel_key, "branchId": branch_id},
                    )
                    return

            talkin_plain = talkin_group_mode and bool(
                ai_config.get("talkinPlainReplies", True)
            )
            rendered_answer = (
                answer
                if talkin_plain
                else apply_message_template(
                    str(ai_config.get("messageTemplate") or "{response}"),
                    answer,
                    message,
                    provider,
                )
            )
            sent_messages = await send_styled_reply(
                message,
                config,
                rendered_answer,
                provider=provider,
                force_plain=talkin_plain,
                mention_author_override=False if talkin_group_mode else None,
            )
            latest_bot_message_id = ""
            for sent in reversed(sent_messages):
                sent_id = getattr(sent, "id", None)
                if sent_id is not None:
                    latest_bot_message_id = str(sent_id)
                    break

            history.append(
                {
                    "role": "user",
                    "content": user_line,
                    "authorId": str(message.author.id),
                    "authorName": profile_name,
                    "messageId": str(message.id),
                    "replyTargetAuthorId": resolved_author_id,
                    "replyTargetAuthorName": discord_profile_name(resolved_author)
                    if resolved_author is not None
                    else "",
                    "activationReason": activation_reason,
                    "branchId": branch_id,
                }
            )
            history.append(
                {
                    "role": "assistant",
                    "content": answer,
                    "replyToAuthorId": str(message.author.id),
                    "messageId": latest_bot_message_id,
                    "branchId": branch_id,
                }
            )
            stored_history = trim_conversation_history(history, max_history)
            retained_message_ids = [
                str(item.get("messageId") or "")
                for item in stored_history
                if str(item.get("messageId") or "")
            ]
            if reply_message_id:
                retained_message_ids.append(reply_message_id)
            await self.store.set_branch_session(
                guild_id,
                channel_key,
                branch_id,
                stored_history,
                latest_bot_message_id=latest_bot_message_id,
                root_message_id=root_message_id,
                message_ids=retained_message_ids,
                make_active=shared_channel_memory,
            )
            await self.store.add_log(
                guild_id,
                "ai.reply",
                {
                    "channelId": channel_key,
                    "branchId": branch_id,
                    "branchReason": branch_reason,
                    "activationReason": activation_reason,
                    "provider": provider,
                    "authorId": str(message.author.id),
                    "replyTargetAuthorId": resolved_author_id,
                    "latestBotMessageId": latest_bot_message_id,
                    "delivery": "talkin_plain_reply"
                    if talkin_plain
                    else "styled_reply",
                },
            )
