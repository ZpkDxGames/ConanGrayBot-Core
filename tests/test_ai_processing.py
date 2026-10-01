from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import ai_providers, presentation
from backend import bot as bot_module
from backend import config as config_module
from backend.config import get_settings
from backend.discord_bot import ai_chat as ai_chat_module
from backend.firebase_client import MemoryStore, merge_bot_config


class AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeUser:
    def __init__(self, user_id: int, name: str):
        self.id = user_id
        self.name = name
        self.global_name = name
        self.display_name = name
        self.bot = False
        self.mention = f"<@{user_id}>"
        self.roles = []


class FakeChannel:
    def __init__(self, channel_id: str, category_id: str, name: str = "talkin"):
        self.id = channel_id
        self.category_id = category_id
        self.name = name
        self.parent = None
        self.sent = []

    def typing(self):
        return AsyncContext()

    async def send(self, content=None, **kwargs):
        sent = SimpleNamespace(id=FakeMessage._next_id, content=content, kwargs=kwargs)
        FakeMessage._next_id += 1
        self.sent.append(sent)
        return sent


class FakeMessage:
    _next_id = 100

    def __init__(
        self, *, author, channel, guild, content, mentions=None, reference=None
    ):
        self.id = FakeMessage._next_id
        FakeMessage._next_id += 1
        self.author = author
        self.channel = channel
        self.guild = guild
        self.content = content
        self.clean_content = content
        self.mentions = list(mentions or [])
        self.reference = reference
        self.replies = []

    async def reply(self, content=None, **kwargs):
        sent = SimpleNamespace(id=FakeMessage._next_id, content=content, kwargs=kwargs)
        FakeMessage._next_id += 1
        self.replies.append(sent)
        return sent


class BotHarness:
    def __init__(self, config):
        self.config = config
        self.settings = SimpleNamespace(ai_channel_id="talk", allowed_category_id="cat")
        self.user = FakeUser(999, "Conan Gray")
        self.store = MemoryStore()
        self.ai_cooldowns = {}
        self.ai_session_locks = {}
        self.talkin_last_activity = {}
        self.talkin_last_spontaneous = {}
        self.talkin_last_spontaneous_check = {}

    async def _config_for(self, guild_id):
        return self.config

    def _ai_session_lock(self, guild_id, channel_id):
        key = f"{guild_id}:{channel_id}"
        self.ai_session_locks.setdefault(key, asyncio.Lock())
        return self.ai_session_locks[key]

    def _note_talkin_activity(self, guild_id, channel_id):
        bot_module.ConanBot._note_talkin_activity(self, guild_id, channel_id)

    async def _handle_weather_chat(self, message, config, text):
        return await bot_module.ConanBot._handle_weather_chat(
            self, message, config, text
        )

    def _weather_location_for_user(self, config, user_id, explicit_location=""):
        return bot_module.ConanBot._weather_location_for_user(
            config, user_id, explicit_location
        )

    async def _start_spontaneous_conversation(self, guild, channel, config):
        return await bot_module.ConanBot._start_spontaneous_conversation(
            self, guild, channel, config
        )


class AIOutputTests(unittest.TestCase):
    def test_internal_safety_metadata_is_removed(self):
        cleaned = ai_providers._clean_model_output(
            "User Safety: safe\nThat sounds like a regular coffee machine.",
            "test",
        )
        self.assertEqual(cleaned, "That sounds like a regular coffee machine.")

    def test_hidden_reasoning_is_removed(self):
        cleaned = ai_providers._clean_model_output(
            "<think>private chain</think>\nVisible reply",
            "test",
        )
        self.assertEqual(cleaned, "Visible reply")

    def test_metadata_only_response_is_rejected(self):
        with self.assertRaises(ai_providers.AIProviderError):
            ai_providers._clean_model_output("User Safety: safe", "test")

    def test_qwen_and_nvidia_are_blocked(self):
        self.assertTrue(
            ai_providers._is_blocked_openrouter_model("qwen/qwen3-coder:free")
        )
        self.assertTrue(
            ai_providers._is_blocked_openrouter_model("nvidia/llama-nemotron")
        )
        self.assertFalse(
            ai_providers._is_blocked_openrouter_model(
                "meta-llama/llama-3.3-70b-instruct:free"
            )
        )

    def test_workspace_openrouter_config_is_explicit_and_safe(self):
        settings = get_settings()
        models = [settings.openrouter_model, *(settings.openrouter_models or [])]
        self.assertNotIn(
            settings.openrouter_model, {"openrouter/free", "openrouter/auto"}
        )
        self.assertTrue(models)
        for model in models:
            self.assertFalse(ai_providers._is_blocked_openrouter_model(model))
        self.assertIn(
            "nvidia", [item.lower() for item in settings.openrouter_ignored_providers]
        )

    def test_existing_guild_configs_receive_talkin_defaults(self):
        merged = merge_bot_config({"ai": {"channelId": "123"}})
        self.assertTrue(merged["ai"]["talkinGroupMode"])
        self.assertTrue(merged["ai"]["talkinPlainReplies"])
        self.assertEqual(merged["ai"]["talkinActivationMode"], "direct_calls")
        self.assertTrue(merged["ai"]["talkinIgnoreRepliesToOthers"])
        self.assertTrue(merged["ai"]["spontaneousConversationEnabled"])

    def test_previous_default_persona_migrates_once(self):
        merged = merge_bot_config(
            {
                "ai": {
                    "personality": config_module.AI_PREVIOUS_DEFAULT_PERSONALITY,
                    "responseLength": "balanced",
                    "toneStyle": "adaptive",
                    "emojiStyle": "occasional",
                    "markdownStyle": "natural",
                    "temperature": 0.8,
                    "maxOutputTokens": 650,
                }
            }
        )
        self.assertEqual(
            merged["ai"]["personality"], config_module.AI_CONAN_BEST_FRIEND_PERSONALITY
        )
        self.assertEqual(merged["ai"]["responseLength"], "brief")
        self.assertEqual(merged["ai"]["toneStyle"], "natural")
        self.assertEqual(merged["ai"]["emojiStyle"], "rare")
        self.assertEqual(merged["ai"]["markdownStyle"], "none")
        self.assertTrue(merged["ai"]["forceLowercase"])
        self.assertEqual(merged["ai"]["personaPreset"], "public_conan")
        self.assertEqual(merged["ai"]["naturalnessLevel"], 92)
        self.assertEqual(merged["ai"]["questionFrequency"], 18)
        self.assertEqual(
            merged["ai"]["personaProfileVersion"],
            config_module.AI_PERSONA_PROFILE_VERSION,
        )

    def test_custom_personality_survives_persona_migration(self):
        merged = merge_bot_config(
            {
                "ai": {
                    "personality": "custom guild persona",
                    "responseLength": "detailed",
                }
            }
        )
        self.assertEqual(merged["ai"]["personality"], "custom guild persona")
        self.assertEqual(merged["ai"]["responseLength"], "detailed")
        self.assertEqual(merged["ai"]["personaPreset"], "custom")

    def test_v2_shipped_persona_migrates_to_natural_profile(self):
        merged = merge_bot_config(
            {
                "ai": {
                    "personaProfileVersion": 2,
                    "personality": config_module.AI_V2_BEST_FRIEND_PERSONALITY,
                    "structureInstructions": config_module.AI_V2_STRUCTURE_INSTRUCTIONS,
                    "toneStyle": "casual",
                    "catchphraseCooldownTurns": 8,
                }
            }
        )
        self.assertEqual(
            merged["ai"]["personality"], config_module.AI_CONAN_BEST_FRIEND_PERSONALITY
        )
        self.assertEqual(
            merged["ai"]["structureInstructions"],
            config_module.AI_CONAN_STRUCTURE_INSTRUCTIONS,
        )
        self.assertEqual(merged["ai"]["toneStyle"], "natural")
        self.assertEqual(merged["ai"]["catchphraseCooldownTurns"], 10)

    def test_naturalness_controls_are_injected_into_prompt(self):
        ai_config = dict(config_module.DEFAULT_BOT_CONFIG["ai"])
        ai_config.update(
            {
                "humorLevel": 81,
                "questionFrequency": 12,
                "avoidPhrases": ["customer support voice"],
                "recurringBits": ["tiny violin"],
                "styleExamples": {"casual": "well. there it is"},
            }
        )
        prompt = ai_providers._build_system_prompt(
            config_module.AI_CONAN_BEST_FRIEND_PERSONALITY,
            ai_config,
            history=[],
            user_text="Gina: movie night failed",
        )
        self.assertIn("Dry humor (81/100)", prompt)
        self.assertIn("Question tendency (12/100)", prompt)
        self.assertIn("customer support voice", prompt)
        self.assertIn("tiny violin", prompt)
        self.assertIn("well. there it is", prompt)

    def test_low_energy_turn_gets_strict_style_guard(self):
        prompt = ai_providers._build_system_prompt(
            config_module.AI_CONAN_BEST_FRIEND_PERSONALITY,
            config_module.DEFAULT_BOT_CONFIG["ai"],
            history=[
                {"role": "assistant", "content": "anything on your mind?"},
                {"role": "assistant", "content": "carrot cake could fix it?"},
            ],
            user_text="Gina: uhuhm...",
        )
        self.assertIn("low-energy", prompt)
        self.assertIn("do not ask a question", prompt.lower())
        self.assertIn("carrot cake", prompt.lower())

    def test_persona_output_is_lowercase_short_and_filters_forced_bits(self):
        config = dict(config_module.DEFAULT_BOT_CONFIG["ai"])
        history = [{"role": "assistant", "content": "i love gina!"}]
        shaped = ai_providers._shape_persona_output(
            "MAYBE CARROT CAKE WILL SPILL THE TEA? 😂 I LOVE GINA!",
            config,
            history,
            "Gina: uhuhm...",
        )
        self.assertEqual(shaped, shaped.lower())
        self.assertNotIn("?", shaped)
        self.assertNotIn("😂", shaped)
        self.assertNotIn("carrot cake", shaped)
        self.assertNotIn("i love gina", shaped)
        self.assertLessEqual(len(shaped), 120)


class GameMessageTemplateTests(unittest.TestCase):
    def test_each_game_routes_to_its_own_template_profile(self):
        expected = {
            "tictactoe": "game_tictactoe",
            "coinflip": "game_coinflip",
            "eightball": "game_eightball",
            "rps": "game_rps",
            "guesssong": "game_guesssong",
            "wouldyourather": "game_wouldyourather",
        }
        for feature, template_key in expected.items():
            with self.subTest(feature=feature):
                self.assertEqual(
                    bot_module.template_key_for_feature(feature, "game"), template_key
                )

    def test_game_profiles_inherit_shared_game_profile(self):
        config = merge_bot_config(
            {
                "messageTemplates": {
                    "game": {
                        "inheritGlobal": False,
                        "footerTemplate": "shared game footer",
                        "showSourceNote": False,
                    },
                    "game_coinflip": {"inheritGlobal": True},
                }
            }
        )
        profile = presentation.template_profile(config, "game_coinflip", "game")
        self.assertEqual(profile["footerTemplate"], "shared game footer")
        self.assertFalse(profile["showSourceNote"])

    def test_shipped_game_profile_migrates_to_clean_footer_defaults(self):
        config = merge_bot_config(
            {
                "messageTemplates": {
                    "game": {
                        "inheritGlobal": True,
                        "titleTemplate": "{title}",
                        "descriptionTemplate": "{description}",
                        "footerTemplate": "{footer}",
                    }
                }
            }
        )
        self.assertFalse(config["messageTemplates"]["game"]["inheritGlobal"])
        self.assertFalse(config["messageTemplates"]["game"]["showSourceNote"])
        self.assertEqual(config["presentation"]["messageTemplateProfileVersion"], 2)

    def test_custom_game_footer_survives_profile_migration(self):
        config = merge_bot_config(
            {
                "messageTemplates": {
                    "game": {
                        "inheritGlobal": False,
                        "footerTemplate": "my custom game footer",
                    }
                }
            }
        )
        self.assertEqual(
            config["messageTemplates"]["game"]["footerTemplate"],
            "my custom game footer",
        )
        self.assertFalse(config["messageTemplates"]["game"]["inheritGlobal"])

    def test_game_footer_hides_ai_narration_source_by_default(self):
        config = merge_bot_config({})
        rendered = presentation.render_feedback_template(
            config,
            title="The coin has spoken",
            description="heads",
            kind="game",
            template_key="game_coinflip",
            source_note="AI narration: gemini",
        )
        self.assertNotIn("gemini", rendered["footer"].lower())
        self.assertNotIn("ai narration", rendered["footer"].lower())

    def test_source_note_can_be_enabled_per_game(self):
        config = merge_bot_config(
            {
                "messageTemplates": {
                    "game_coinflip": {
                        "inheritGlobal": False,
                        "showSourceNote": True,
                    }
                }
            }
        )
        rendered = presentation.render_feedback_template(
            config,
            title="The coin has spoken",
            description="heads",
            kind="game",
            template_key="game_coinflip",
            source_note="AI narration: gemini",
        )
        self.assertIn("AI narration: gemini", rendered["footer"])

    def test_blank_footer_removes_footer_even_when_metadata_exists(self):
        config = merge_bot_config(
            {
                "messageTemplates": {
                    "game_guesssong": {
                        "inheritGlobal": False,
                        "footerTemplate": "",
                        "showProvider": False,
                        "showSourceNote": False,
                    }
                }
            }
        )
        rendered = presentation.render_feedback_template(
            config,
            title="Mystery track",
            description="guess it",
            kind="game",
            template_key="game_guesssong",
            provider="gemini",
            source_note="AI answer judge: gemini",
        )
        self.assertEqual(rendered["footer"], "")

    def test_clearing_global_embed_footer_is_respected(self):
        config = merge_bot_config(
            {
                "appearance": {"embedFooter": ""},
                "messageTemplates": {
                    "game": {
                        "inheritGlobal": False,
                        "footerTemplate": "{footer}",
                        "showProvider": False,
                        "showSourceNote": False,
                    }
                },
            }
        )
        rendered = presentation.render_feedback_template(
            config,
            title="Game",
            description="Result",
            kind="game",
            template_key="game_coinflip",
        )
        self.assertEqual(rendered["footer"], "")


class TalkinRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {
            "ai": {
                "enabled": True,
                "channelId": "talk",
                "talkinGroupMode": True,
                "talkinPlainReplies": True,
                "talkinActivationMode": "direct_calls",
                "talkinWakeWords": ["conan", "conan gray"],
                "talkinRespondToNameCalls": True,
                "talkinRespondToBotReplies": True,
                "talkinIgnoreRepliesToOthers": True,
                "sharedChannelMemory": True,
                "mentionStartsNewBranch": True,
                "replyContinuesBranch": True,
                "typingIndicator": False,
                "maxHistoryMessages": 36,
                "messageTemplate": "Conan said: {response}",
                "spontaneousConversationEnabled": True,
                "spontaneousPrompt": "Start a natural thought.",
            },
            "games": {"allowedCategoryId": "cat"},
            "presentation": {"embedEverywhere": True},
            "messageTemplates": {"ai": {"useEmbed": True}},
            "appearance": {},
            "admin": {},
        }
        self.harness = BotHarness(self.config)
        self.guild = SimpleNamespace(id=1, name="Guild")
        self.calls = []
        self.deliveries = []

        async def fake_ask(config, history, user_text, context):
            self.calls.append(
                {"history": list(history), "user_text": user_text, "context": context}
            )
            return "A natural reply", "gemini"

        async def fake_send(message, config, text, provider=None, **kwargs):
            self.deliveries.append(
                {"message": message, "text": text, "provider": provider, **kwargs}
            )
            return [SimpleNamespace(id=9000 + len(self.deliveries))]

        self.ask_patch = patch.object(ai_chat_module, "ask_ai", fake_ask)
        self.send_patch = patch.object(ai_chat_module, "send_styled_reply", fake_send)
        self.ask_patch.start()
        self.send_patch.start()

    def tearDown(self):
        self.ask_patch.stop()
        self.send_patch.stop()

    @staticmethod
    def reply_reference(author, content="previous message", message_id=700):
        resolved = SimpleNamespace(
            id=message_id,
            author=author,
            content=content,
            clean_content=content,
        )
        return SimpleNamespace(message_id=message_id, resolved=resolved)

    async def test_talkin_ignores_ordinary_unaddressed_message(self):
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="depends. are you using kcups?",
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.harness.talkin_last_activity)

    async def test_talkin_answers_when_name_is_called(self):
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="conan are you alive",
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.deliveries[0]["text"], "A natural reply")
        self.assertTrue(self.deliveries[0]["force_plain"])
        self.assertFalse(self.deliveries[0]["mention_author_override"])
        self.assertIn("calling your name", self.calls[0]["context"])
        self.assertEqual(self.calls[0]["user_text"], "Gina: are you alive")

    async def test_talkin_answers_direct_mention(self):
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="@Conan Gray hello",
            mentions=[self.harness.user],
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("direct @mention", self.calls[0]["context"])

    async def test_talkin_ignores_reply_to_another_user(self):
        toni = FakeUser(2, "Toni")
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="literally same",
            reference=self.reply_reference(toni, "that game is gone"),
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(self.calls, [])

    async def test_talkin_answers_reply_to_bot(self):
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="no that is not what i meant",
            reference=self.reply_reference(
                self.harness.user, "yeah, nerd badge looks good on you"
            ),
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("direct reply to one of your messages", self.calls[0]["context"])
        self.assertIn(
            "direct reply to one of your previous messages", self.calls[0]["context"]
        )

    async def test_explicit_name_call_can_join_reply_to_other_user_without_misattributing_target(
        self,
    ):
        toni = FakeUser(2, "Toni")
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="conan what do you think",
            reference=self.reply_reference(toni, "this badge is nerdy"),
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("another user, not you", self.calls[0]["context"])
        self.assertIn("Toni", self.calls[0]["context"])

    async def test_talkin_history_is_shared_between_called_users(self):
        channel = FakeChannel("talk", "cat")
        first = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=channel,
            guild=self.guild,
            content="conan coffee?",
        )
        second = FakeMessage(
            author=FakeUser(2, "Toni"),
            channel=channel,
            guild=self.guild,
            content="conan regular machine",
        )

        await bot_module.ConanBot._handle_ai_message(self.harness, first)
        await bot_module.ConanBot._handle_ai_message(self.harness, second)

        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.calls[0]["history"]), 0)
        self.assertEqual(len(self.calls[1]["history"]), 2)
        self.assertEqual(self.calls[1]["history"][0]["content"], "Gina: coffee?")

    async def test_legacy_every_message_mode_can_be_enabled(self):
        self.config["ai"]["talkinActivationMode"] = "all_messages"
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="just vibing",
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("legacy every-message mode", self.calls[0]["context"])

    async def test_optional_unthreaded_question_mode(self):
        self.config["ai"]["talkinActivationMode"] = "direct_calls_and_questions"
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="does anyone know where the charger is?",
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("unthreaded question", self.calls[0]["context"])

    async def test_spontaneous_start_is_standalone_and_persists_shared_history(self):
        channel = FakeChannel("talk", "cat")
        result = await bot_module.ConanBot._start_spontaneous_conversation(
            self.harness,
            self.guild,
            channel,
            self.config,
        )
        self.assertTrue(result)
        self.assertEqual(channel.sent[0].content, "A natural reply")
        self.assertIn("choosing to speak first", self.calls[0]["context"])
        session = await self.harness.store.get_branch_session(
            "1", "talk", "talkin-group"
        )
        self.assertTrue(session["messages"][-1]["spontaneous"])
        self.assertEqual(session["messages"][-1]["content"], "A natural reply")

    async def test_spontaneous_scheduler_waits_until_channel_is_idle(self):
        channel = FakeChannel("123", "cat")
        guild = SimpleNamespace(
            id=1,
            get_channel=lambda channel_id: (
                channel if channel_id == int(channel.id) else None
            ),
        )
        self.harness.guilds = [guild]
        self.config["ai"].update(
            {
                "channelId": "123",
                "spontaneousCheckMinutes": 1,
                "spontaneousIdleMinutes": 5,
                "spontaneousCooldownMinutes": 15,
                "spontaneousChancePercent": 100,
            }
        )
        now = asyncio.get_running_loop().time()
        key = "1:123"
        self.harness.talkin_last_activity[key] = now
        self.harness.talkin_last_spontaneous_check[key] = now - 61

        await bot_module.ConanBot._run_spontaneous_chat_checks(self.harness)
        self.assertEqual(channel.sent, [])

    async def test_spontaneous_scheduler_starts_after_idle_threshold(self):
        channel = FakeChannel("123", "cat")
        guild = SimpleNamespace(
            id=1,
            get_channel=lambda channel_id: (
                channel if channel_id == int(channel.id) else None
            ),
        )
        self.harness.guilds = [guild]
        self.config["ai"].update(
            {
                "channelId": "123",
                "spontaneousCheckMinutes": 1,
                "spontaneousIdleMinutes": 5,
                "spontaneousCooldownMinutes": 15,
                "spontaneousChancePercent": 100,
            }
        )
        now = asyncio.get_running_loop().time()
        key = "1:123"
        self.harness.talkin_last_activity[key] = now - 301
        self.harness.talkin_last_spontaneous_check[key] = now - 61

        await bot_module.ConanBot._run_spontaneous_chat_checks(self.harness)
        self.assertEqual(channel.sent[0].content, "A natural reply")
        self.assertIn(key, self.harness.talkin_last_spontaneous)

    async def test_other_channel_ignores_unmentioned_messages(self):
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("other", "cat", "general"),
            guild=self.guild,
            content="hello",
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(self.calls, [])

    async def test_other_channel_accepts_direct_mention_in_authorized_category(self):
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("other", "cat", "general"),
            guild=self.guild,
            content="@Conan Gray hello",
            mentions=[self.harness.user],
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.deliveries[0]["force_plain"])
        self.assertEqual(self.deliveries[0]["text"], "Conan said: A natural reply")
        self.assertIn("directly @mentioned", self.calls[0]["context"])

    async def test_weather_question_uses_deterministic_weather_even_when_ai_is_paused(
        self,
    ):
        self.config["ai"]["enabled"] = False
        self.config["weather"] = {
            "enabled": True,
            "aiDetectionEnabled": True,
            "units": "metric",
            "language": "en",
            "forecastHours": 12,
            "showDetails": True,
            "defaultLocation": "",
            "userLocations": {},
        }

        class FakeWeatherClient:
            async def get_weather(self, query, **kwargs):
                self.query = query
                return {
                    "location": {"label": "London, GB"},
                    "temperatureLabel": "°C",
                    "windLabel": "m/s",
                    "current": {
                        "temperature": 18,
                        "feelsLike": 18,
                        "description": "light rain",
                        "humidity": 70,
                        "windSpeed": 3,
                        "sunrise": "05:00",
                        "sunset": "21:00",
                    },
                    "forecast": {
                        "hours": 12,
                        "minimum": 16,
                        "maximum": 19,
                        "precipitationProbability": 0.8,
                        "description": "light rain",
                    },
                }

        self.harness.weather_client = FakeWeatherClient()
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("talk", "cat"),
            guild=self.guild,
            content="conan what's the weather in london?",
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(message.replies), 1)
        self.assertIn("18°C", message.replies[0].content)
        self.assertIn("weather data © openweather", message.replies[0].content)

    async def test_other_category_rejects_even_with_mention(self):
        message = FakeMessage(
            author=FakeUser(1, "Gina"),
            channel=FakeChannel("other", "wrong", "elsewhere"),
            guild=self.guild,
            content="@Conan Gray hello",
            mentions=[self.harness.user],
        )
        await bot_module.ConanBot._handle_ai_message(self.harness, message)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
