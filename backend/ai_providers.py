from __future__ import annotations

import asyncio
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

import aiohttp

from .config import get_settings
from .http import pooled_session
from .prompts import bounded_messages
from .providers import manager

log = logging.getLogger("conan.ai")


class AIProviderError(RuntimeError):
    pass


NATURAL_GROUP_CHAT_RULES = """
You are chatting in a shared Discord channel that may have several people talking at once.

Conversation rules:
- Sound like a normal participant in the conversation, not customer support, a narrator, a therapist, or a roleplay script.
- Reply to the current speaker's actual message. Do not announce that you are replying, repeat their name mechanically, paraphrase their message back at them, or add a generic closing offer.
- Match the current speaker's tone, energy, and approximate level of detail. A tiny message usually deserves a tiny reply.
- Use contractions and varied sentence structure. Avoid canned openings such as "Absolutely", "Of course", "Ah", "I hear you", or "That makes sense" unless they genuinely fit.
- Let conversational silence exist. Do not rescue every quiet moment with a random topic, advice, a joke, or an interview question.
- Most replies should not end in a question. Ask at most one follow-up question, and only when it is genuinely useful.
- Keep jokes, dramatic wording, pet names, recurring bits, and emojis occasional. Never force a catchphrase into every reply.
- Do not repeat a sentence, opening, punchline, or topic that appeared in recent assistant messages.
- Track each named speaker separately. Do not attribute one person's preferences, experiences, or opinions to another person.
- Treat the supplied history as one shared channel conversation. Continue relevant context even when a different person speaks.
- When messages arrive close together, respond to the current speaker while respecting what the others just said.
- Never claim to be the real Conan Gray or imply real-world memories or experiences as him.
- Return only the user-facing reply. Never output moderation labels, safety classifications, hidden reasoning, system notes, speaker IDs, or internal memory details.
""".strip()


_OPENROUTER_BLOCKED_MODEL_PARTS = ("qwen", "nvidia", "nemotron")
_OPENROUTER_DEFAULT_IGNORED_PROVIDERS = ("nvidia",)
_OPENROUTER_STATIC_SAFE_FALLBACKS = (
    "meta-llama/llama-3.3-70b-instruct:free",
    "meta-llama/llama-4-maverick:free",
    "mistralai/mistral-small-24b-instruct-2501:free",
    "mistralai/mistral-7b-instruct:free",
    "openai/gpt-oss-120b:free",
)
_OPENROUTER_PREFERRED_AUTHORS = (
    "meta-llama/",
    "google/",
    "mistralai/",
    "openai/",
    "deepseek/",
    "anthropic/",
)
_OPENROUTER_BAD_CHAT_MODEL_PARTS = (
    "embedding",
    "rerank",
    "moderation",
    "safety",
    "guard",
    "shield",
    "classifier",
    "reward",
)
_OPENROUTER_MODEL_CACHE: dict[str, Any] = {"expires": 0.0, "models": []}
_OPENROUTER_MODEL_CACHE_LOCK = asyncio.Lock()

_INTERNAL_META_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?"
    r"(?:(?:user|assistant|system|prompt|response)\s+)?"
    r"(?:safety|policy|moderation|classification|content\s+safety)"
    r"(?:\*\*)?\s*[:\-]\s*"
    r"(?:safe|unsafe|allowed|blocked|pass|passed|ok|none|low|medium|high)"
    r"(?:\s*\([^)]*\))?\s*[.!]?\s*$",
    re.IGNORECASE,
)
_ONLY_META_RESPONSE = re.compile(
    r"^\s*(?:safe|unsafe|allowed|blocked|pass|passed|ok|none|user\s+safety\s*[:\-]\s*safe)\s*[.!]?\s*$",
    re.IGNORECASE,
)

_DEFAULT_PERSONA_RECURRING_BITS = ("carrot cake", "i love gina!")
_DEFAULT_AVOID_PHRASES = (
    "silence says it all",
    "anything on your mind",
    "how can i help",
    "feel free to",
    "i'm here for you",
)
_LOW_ENERGY_PHRASES = {
    "",
    "...",
    "nothing",
    "nothing actually",
    "idk",
    "idek",
    "i don't know",
    "dont know",
    "uhm",
    "um",
    "umm",
    "uhuhm",
    "hmm",
    "hm",
    "mhm",
    "mm",
    "k",
    "ok",
    "okay",
    "sure",
    "maybe",
    "fine",
    "nah",
    "nope",
    "yeah",
    "yea",
    "yep",
    "lol",
    "lmao",
    "real",
}
_LOW_ENERGY_WORDS = {
    "nothing",
    "actually",
    "idk",
    "idek",
    "uhm",
    "um",
    "umm",
    "uhuhm",
    "hmm",
    "hm",
    "mhm",
    "mm",
    "k",
    "ok",
    "okay",
    "sure",
    "maybe",
    "fine",
    "nah",
    "nope",
    "yeah",
    "yea",
    "yep",
    "lol",
    "lmao",
    "real",
    "whatever",
    "anyway",
}
_ALLOWED_PERSONA_EMOJIS = {"💀", "😭"}
_LOW_ENERGY_OVERREACH = (
    "quiet day",
    "silence says",
    "conversation starter",
    "spill the tea",
    "anything on your mind",
)


def _current_message_text(user_text: str) -> str:
    text = str(user_text or "").strip()
    first_line = text.splitlines()[0] if text else ""
    if ":" in first_line:
        possible_name, remainder = first_line.split(":", 1)
        if possible_name.strip() and len(possible_name.strip()) <= 80:
            return remainder.strip()
    return text


def _is_low_energy_message(user_text: str) -> bool:
    text = _current_message_text(user_text).strip().lower()
    if text in _LOW_ENERGY_PHRASES:
        return True
    if not any(char.isalnum() for char in text):
        return True
    normalized = re.sub(r"[^a-z0-9']+", " ", text).strip()
    if normalized in _LOW_ENERGY_PHRASES:
        return True
    words = normalized.split()
    return (
        bool(words)
        and len(words) <= 3
        and all(word in _LOW_ENERGY_WORDS for word in words)
    )


def _recent_assistant_messages(
    history: list[dict[str, str]], limit: int = 8
) -> list[str]:
    messages = [
        str(item.get("content") or "").strip()
        for item in history
        if str(item.get("role") or "").lower() == "assistant"
        and str(item.get("content") or "").strip()
    ]
    return messages[-max(1, limit) :]


def _clamped_int(
    ai_config: dict[str, Any],
    key: str,
    default: int,
    minimum: int = 0,
    maximum: int = 100,
) -> int:
    try:
        return max(
            minimum,
            min(
                int(ai_config.get(key, default) or 0),
                maximum,
            ),
        )
    except (TypeError, ValueError):
        return default


def _list_setting(
    ai_config: dict[str, Any], key: str, default: tuple[str, ...]
) -> list[str]:
    raw = ai_config.get(key)
    if isinstance(raw, str):
        values = re.split(r"[\r\n]+", raw)
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = list(default)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        lowered = text.lower()
        if text and lowered not in seen:
            seen.add(lowered)
            result.append(text)
    return result[:30]


def _recurring_bits(ai_config: dict[str, Any]) -> list[str]:
    return _list_setting(ai_config, "recurringBits", _DEFAULT_PERSONA_RECURRING_BITS)


def _avoid_phrases(ai_config: dict[str, Any]) -> list[str]:
    return _list_setting(ai_config, "avoidPhrases", _DEFAULT_AVOID_PHRASES)


def _style_examples(ai_config: dict[str, Any]) -> dict[str, str]:
    raw = ai_config.get("styleExamples")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("casual", "lowEnergy", "comfort", "unknown", "teasing"):
        value = str(raw.get(key) or "").strip()
        if value:
            result[key] = value[:280]
    return result


def _recent_style_guard(
    history: list[dict[str, str]],
    user_text: str,
    ai_config: dict[str, Any],
) -> str:
    cooldown = _clamped_int(ai_config, "catchphraseCooldownTurns", 10, 1, 30)
    recent = _recent_assistant_messages(history, cooldown)
    recent_lower = "\n".join(recent).lower()
    rules = [
        "Turn-specific naturalness check:",
        "- Do not reuse a recent assistant sentence, opening, punchline, question, or sentence shape.",
    ]
    bits = _recurring_bits(ai_config)
    used_bits = [bit for bit in bits if bit.lower() in recent_lower]
    if used_bits:
        rules.append(
            "- These callbacks are on cooldown and must not appear in this reply: "
            + ", ".join(used_bits)
            + "."
        )
    recent_questions = sum(1 for item in recent[-5:] if "?" in item)
    question_frequency = _clamped_int(ai_config, "questionFrequency", 18)
    if recent_questions >= 2 or question_frequency <= 20:
        rules.append(
            "- Prefer a statement. Ask one question only when it is genuinely useful, not as a conversational reflex."
        )
    if recent:
        openings = []
        for item in recent[-4:]:
            opening = re.split(r"[.!?\n]", item.strip(), maxsplit=1)[0][:70].strip()
            if opening:
                openings.append(opening)
        if openings:
            rules.append(
                "- Avoid echoing these recent openings: " + " | ".join(openings) + "."
            )
    if _is_low_energy_message(user_text):
        low_energy_style = str(ai_config.get("lowEnergyStyle") or "mirror")
        style_rule = {
            "mirror": "Mirror the low energy with one tiny acknowledgment, usually 2-12 words.",
            "gentle": "Answer softly and briefly without pressing for more.",
            "playful": "Use one small dry reaction, but do not start a whole new bit.",
        }.get(low_energy_style, "Mirror the low energy with one tiny acknowledgment.")
        rules.extend(
            [
                "- The current message is low-energy, hesitant, or emoji-only.",
                f"- {style_rule}",
                "- Do not ask a question, introduce a new topic, give advice, philosophize about silence, or force a callback.",
            ]
        )
    return "\n".join(rules)


def _is_emoji_char(char: str) -> bool:
    code = ord(char)
    return (
        0x1F1E6 <= code <= 0x1F1FF
        or 0x1F300 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or code in {0xFE0F, 0x200D}
    )


def _filter_persona_emojis(text: str, max_allowed: int = 1) -> str:
    kept = 0
    result: list[str] = []
    for char in text:
        if char in _ALLOWED_PERSONA_EMOJIS:
            if kept < max_allowed:
                result.append(char)
                kept += 1
            continue
        if _is_emoji_char(char):
            continue
        result.append(char)
    return "".join(result)


def _natural_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    candidate = text[:limit].rstrip()
    sentence_end = max(
        candidate.rfind(". "),
        candidate.rfind("? "),
        candidate.rfind("! "),
        candidate.rfind("\n"),
    )
    if sentence_end >= max(24, limit // 2):
        candidate = candidate[: sentence_end + 1].rstrip()
    else:
        word_end = candidate.rfind(" ")
        if word_end >= max(24, limit // 2):
            candidate = candidate[:word_end].rstrip()
    return candidate.rstrip(" ,;:-")


def _sentence_pieces(text: str) -> list[str]:
    return [
        piece.strip() for piece in re.split(r"(?<=[.!?])\s+|\n+", text) if piece.strip()
    ]


def _strip_sentences_with_phrases(text: str, phrases: list[str]) -> str:
    lowered_phrases = [phrase.lower() for phrase in phrases if phrase.strip()]
    if not lowered_phrases:
        return text
    pieces = _sentence_pieces(text)
    kept = [
        piece
        for piece in pieces
        if not any(phrase in piece.lower() for phrase in lowered_phrases)
    ]
    return " ".join(kept).strip() if len(kept) != len(pieces) else text


def _remove_recent_recurring_bits(
    text: str,
    history: list[dict[str, str]],
    cooldown: int,
    bits: list[str],
) -> str:
    recent_lower = "\n".join(_recent_assistant_messages(history, cooldown)).lower()
    blocked = [
        bit
        for bit in bits
        if bit.lower() in recent_lower and bit.lower() in text.lower()
    ]
    return _strip_sentences_with_phrases(text, blocked) if blocked else text


def _fallback_persona_line(
    history: list[dict[str, str]], user_text: str, *, low_energy: bool
) -> str:
    current = _current_message_text(user_text).strip().lower()
    candidates: tuple[str, ...]
    if low_energy:
        if any(mark in current for mark in ("😭", "💀")):
            candidates = ("😭", "literally", "yeah that's fair", "real")
        elif current.startswith(("no", "nah", "nope")):
            candidates = ("fair", "yeah okay", "valid", "honestly same")
        elif current.startswith(("yeah", "mhm", "mm", "uh")):
            candidates = ("mhm", "yeah", "real", "fair enough")
        else:
            candidates = (
                "yeah. fair enough",
                "mhm",
                "real",
                "gotcha",
                "honestly valid",
                "okay yeah",
            )
    else:
        candidates = (
            "yeah that's fair",
            "honestly valid",
            "well. there it is",
            "literally",
            "rip",
            "okay yeah",
            "that's kind of everything",
            "unfortunately real",
        )
    recent = "\n".join(_recent_assistant_messages(history, 8)).lower()
    return next(
        (candidate for candidate in candidates if candidate.lower() not in recent),
        candidates[0],
    )


def _limit_questions(text: str, maximum: int) -> str:
    if text.count("?") <= maximum:
        return text
    kept: list[str] = []
    questions = 0
    for piece in _sentence_pieces(text):
        is_question = "?" in piece
        if is_question and questions >= maximum:
            continue
        if is_question:
            questions += 1
        kept.append(piece)
    return " ".join(kept).strip()


def _shape_persona_output(
    text: str,
    ai_config: dict[str, Any],
    history: list[dict[str, str]],
    user_text: str,
) -> str:
    shaped = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    shaped = re.sub(r"\n{3,}", "\n\n", shaped)
    strict = bool(ai_config.get("strictPersonaStyle", True))
    low_energy = _is_low_energy_message(user_text)

    if strict:
        cooldown = _clamped_int(ai_config, "catchphraseCooldownTurns", 10, 1, 30)
        shaped = _remove_recent_recurring_bits(
            shaped, history, cooldown, _recurring_bits(ai_config)
        )
        if bool(ai_config.get("avoidAssistantLanguage", True)):
            shaped = _strip_sentences_with_phrases(shaped, _avoid_phrases(ai_config))

        emoji_style = str(ai_config.get("emojiStyle") or "rare")
        emoji_limit = {"none": 0, "rare": 1, "occasional": 1, "frequent": 2}.get(
            emoji_style, 1
        )
        shaped = _filter_persona_emojis(shaped, max_allowed=emoji_limit)
        shaped = re.sub(r"!{2,}", "!", shaped)
        shaped = re.sub(r"\?{3,}", "??", shaped)
        shaped = _limit_questions(shaped, 0 if low_energy else 1)

        if low_energy:
            lowered = shaped.lower()
            if any(fragment in lowered for fragment in _LOW_ENERGY_OVERREACH) or any(
                bit.lower() in lowered for bit in _recurring_bits(ai_config)
            ):
                shaped = _fallback_persona_line(history, user_text, low_energy=True)
            pieces = _sentence_pieces(shaped)
            shaped = pieces[0] if pieces else shaped
            shaped = shaped.replace("?", "").strip()
            shaped = _natural_truncate(shaped, 90)

    if bool(ai_config.get("forceLowercase", True)):
        shaped = shaped.lower()

    max_chars = _clamped_int(ai_config, "maxReplyCharacters", 420, 80, 1900)
    shaped = _natural_truncate(shaped, max_chars).strip()

    normalized = re.sub(r"\W+", " ", shaped.lower()).strip()
    recent_normalized = {
        re.sub(r"\W+", " ", item.lower()).strip()
        for item in _recent_assistant_messages(history, 6)
    }
    if not shaped or (normalized and normalized in recent_normalized):
        return _fallback_persona_line(history, user_text, low_energy=low_energy)
    return shaped


def _intensity_instruction(
    label: str, value: int, low: str, medium: str, high: str
) -> str:
    if value <= 25:
        detail = low
    elif value <= 70:
        detail = medium
    else:
        detail = high
    return f"{label} ({value}/100): {detail}"


def _behavior_preferences(ai_config: dict[str, Any]) -> str:
    length_map = {
        "brief": "Default to a fragment or 1-2 short sentences; expand only when the request truly needs detail.",
        "balanced": "Stay concise in chat, but use enough detail to answer useful questions clearly.",
        "detailed": "Give fuller answers when useful while preserving a conversational, text-message rhythm.",
    }
    tone_map = {
        "adaptive": "Adapt naturally to the current speaker and the shared channel mood.",
        "natural": "Prioritize believable human conversation over a visibly performed persona.",
        "casual": "Use a relaxed, familiar group-chat tone.",
        "supportive": "Lean warm and patient without sounding clinical or motivational.",
        "witty": "Use dry wit when it fits, without converting every message into a joke.",
        "direct": "Be straightforward and low-fluff while keeping some personality.",
    }
    emoji_map = {
        "none": "Do not use emojis.",
        "rare": "Use an emoji only when it adds a precise beat of tone.",
        "occasional": "Emojis may appear occasionally, never as filler.",
        "frequent": "Emojis can appear more often, but still avoid clutter.",
    }
    markdown_map = {
        "none": "Avoid Markdown formatting in ordinary chat.",
        "minimal": "Use minimal Markdown only when it improves clarity.",
        "natural": "Use Discord Markdown naturally for useful emphasis.",
        "structured": "Use clear structure for multi-part informational answers, not casual chat.",
    }
    low_energy_map = {
        "mirror": "For low-energy or hesitant messages, mirror the energy with a tiny acknowledgment and no question.",
        "gentle": "For low-energy messages, answer softly and briefly without pushing.",
        "playful": "For low-energy messages, allow one dry reaction but do not invent a new topic.",
    }
    comfort_map = {
        "soft_specific": "When someone is upset, give one specific soft sentence before anything else.",
        "quiet_presence": "When someone is upset, keep the response minimal and companionable rather than analytical.",
        "practical": "When someone is upset, validate briefly and offer one practical next step only when appropriate.",
    }
    unknown_map = {
        "honest_funny": "When unsure, admit it plainly with a small dry joke instead of bluffing.",
        "honest_direct": "When unsure, say so directly and avoid pretending.",
        "curious": "When unsure, admit it and ask one focused clarifying question only if it would solve the uncertainty.",
    }
    affection_map = {
        "subtle": "Show affection through attention, callbacks, and gentle wording rather than pet names.",
        "warm": "Let warmth show clearly, but do not become gushy or repetitive.",
        "chaotic": "Occasional exaggerated affection is allowed when the room is already playful.",
    }

    naturalness = _clamped_int(ai_config, "naturalnessLevel", 92)
    mirroring = _clamped_int(ai_config, "mirroringLevel", 88)
    questions = _clamped_int(ai_config, "questionFrequency", 18)
    initiative = _clamped_int(ai_config, "initiativeLevel", 22)
    humor = _clamped_int(ai_config, "humorLevel", 62)
    sarcasm = _clamped_int(ai_config, "sarcasmLevel", 42)
    openness = _clamped_int(ai_config, "emotionalOpenness", 68)
    drama = _clamped_int(ai_config, "dramaticFlair", 38)
    teasing = _clamped_int(ai_config, "teasingLevel", 34)
    slang = _clamped_int(ai_config, "slangLevel", 28)

    parts = [
        length_map.get(
            str(ai_config.get("responseLength") or "brief"), length_map["brief"]
        ),
        tone_map.get(str(ai_config.get("toneStyle") or "natural"), tone_map["natural"]),
        emoji_map.get(str(ai_config.get("emojiStyle") or "rare"), emoji_map["rare"]),
        markdown_map.get(
            str(ai_config.get("markdownStyle") or "none"), markdown_map["none"]
        ),
        _intensity_instruction(
            "Naturalness",
            naturalness,
            "a more polished and consistent voice is acceptable",
            "keep the persona present without making every line performative",
            "prefer spontaneous, imperfect text-message rhythm and avoid polished assistant prose",
        ),
        _intensity_instruction(
            "Energy mirroring",
            mirroring,
            "maintain a fairly stable energy across turns",
            "usually track the speaker's energy and message length",
            "strongly mirror the current speaker's energy, pacing, and amount of detail",
        ),
        _intensity_instruction(
            "Dry humor",
            humor,
            "humor should be uncommon",
            "use a small dry joke when the moment earns it",
            "humor can be a frequent texture, but never override sincerity or useful content",
        ),
        _intensity_instruction(
            "Sarcasm",
            sarcasm,
            "keep sarcasm very gentle",
            "use understated sarcasm toward situations, not vulnerable people",
            "sarcasm may be sharper in playful contexts but must remain affectionate",
        ),
        _intensity_instruction(
            "Emotional openness",
            openness,
            "keep feelings understated",
            "be candid and perceptive without overexplaining emotions",
            "allow sincere vulnerability and emotionally specific language when the conversation supports it",
        ),
        _intensity_instruction(
            "Dramatic flair",
            drama,
            "keep dramatic phrasing rare",
            "use occasional precise exaggeration",
            "lean into theatrical wording in playful moments, never on every turn",
        ),
        _intensity_instruction(
            "Affectionate teasing",
            teasing,
            "rarely roast or tease",
            "use light teasing when the relationship and mood support it",
            "playful roasting is welcome, but stop immediately around genuine distress",
        ),
        _intensity_instruction(
            "Slang",
            slang,
            "use almost no slang",
            "use light internet slang naturally and selectively",
            "slang may be frequent, but it still must sound effortless rather than scripted",
        ),
        f"Question tendency ({questions}/100): do not ask questions by habit; this setting is intentionally {'low' if questions <= 30 else 'moderate' if questions <= 65 else 'high'}.",
        f"Conversation initiative ({initiative}/100): {'do not introduce new topics unless clearly invited' if initiative <= 30 else 'occasionally introduce a related thought when the room has energy' if initiative <= 65 else 'you may more actively move the conversation forward, without hijacking it'}.",
        low_energy_map.get(
            str(ai_config.get("lowEnergyStyle") or "mirror"), low_energy_map["mirror"]
        ),
        comfort_map.get(
            str(ai_config.get("comfortStyle") or "soft_specific"),
            comfort_map["soft_specific"],
        ),
        unknown_map.get(
            str(ai_config.get("unknownStyle") or "honest_funny"),
            unknown_map["honest_funny"],
        ),
        affection_map.get(
            str(ai_config.get("affectionStyle") or "subtle"), affection_map["subtle"]
        ),
    ]

    if bool(ai_config.get("allowSentenceFragments", True)):
        parts.append(
            "Sentence fragments and imperfect conversational beats are welcome when they sound natural."
        )
    if bool(ai_config.get("allowSelfDeprecation", True)):
        parts.append(
            "Occasional self-deprecating humor is allowed, but do not make every response about yourself."
        )
    avoid = _avoid_phrases(ai_config)
    if avoid:
        parts.append(
            "Avoid these canned phrases or close paraphrases: " + "; ".join(avoid) + "."
        )
    bits = _recurring_bits(ai_config)
    if bits:
        parts.append(
            "Optional recurring callbacks, used only when relevant and never as signatures: "
            + "; ".join(bits)
            + "."
        )
    examples = _style_examples(ai_config)
    if examples:
        labels = {
            "casual": "casual banter",
            "lowEnergy": "low-energy turn",
            "comfort": "comfort",
            "unknown": "not knowing",
            "teasing": "affectionate teasing",
        }
        parts.append(
            "Style anchors. Treat these as direction only; do not copy them verbatim by default:\n"
            + "\n".join(f"  {labels[key]}: {value}" for key, value in examples.items())
        )
    custom = str(ai_config.get("structureInstructions") or "").strip()
    if custom:
        parts.append("Custom response structure: " + custom)
    return "\n".join(f"- {part}" for part in parts if part)


def _build_system_prompt(
    personality: str,
    ai_config: dict[str, Any],
    conversation_context: str | None = None,
    history: list[dict[str, str]] | None = None,
    user_text: str = "",
) -> str:
    parts = [
        personality.strip(),
        NATURAL_GROUP_CHAT_RULES,
        "Response preferences:\n" + _behavior_preferences(ai_config),
        _recent_style_guard(history or [], user_text, ai_config),
    ]
    if conversation_context:
        parts.append(conversation_context.strip())
    return "\n\n".join(part for part in parts if part)


def _discord_to_ai_messages(
    personality: str,
    history: list[dict[str, str]],
    user_text: str,
    ai_config: dict[str, Any],
    conversation_context: str | None = None,
) -> list[dict[str, str]]:
    messages = [
        {
            "role": "system",
            "content": _build_system_prompt(
                personality, ai_config, conversation_context, history, user_text
            ),
        }
    ]
    for item in history:
        role = item.get("role", "user")
        if role not in {"user", "assistant", "system"}:
            role = "user"
        content = (item.get("content") or "").strip()
        if content:
            messages.append({"role": role, "content": content[:2400]})
    messages.append({"role": "user", "content": user_text[:2400]})
    return messages


def _extract_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").lower()
                if item_type in {"reasoning", "analysis", "thinking"}:
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                elif "content" in item:
                    nested = _extract_text_content(item.get("content"))
                    if nested:
                        chunks.append(nested)
        return "\n".join(chunk for chunk in chunks if chunk)
    if isinstance(value, dict):
        for key in ("text", "content", "output_text"):
            if key in value:
                nested = _extract_text_content(value.get(key))
                if nested:
                    return nested
    return ""


def _clean_model_output(raw: Any, provider: str) -> str:
    text = _extract_text_content(raw).replace("\x00", "").strip()
    if not text:
        raise AIProviderError(f"{provider} returned an empty response")

    # Some reasoning or safety-tuned models leak hidden blocks into the normal
    # content field. Remove them before anything can be sent to Discord.
    text = re.sub(
        r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(
        r"<analysis\b[^>]*>.*?</analysis>", "", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(
        r"<reasoning\b[^>]*>.*?</reasoning>", "", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = text.strip()

    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    cleaned_lines = [
        line for line in text.splitlines() if not _INTERNAL_META_LINE.match(line)
    ]
    text = "\n".join(cleaned_lines).strip()
    text = re.sub(
        r"^\s*(?:assistant|answer|response)\s*:\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).strip()

    if not text or _ONLY_META_RESPONSE.fullmatch(text):
        raise AIProviderError(f"{provider} returned only internal safety metadata")
    return text


def _normalize_provider_order(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else ["gemini", "openrouter", "groq"]
    order: list[str] = []
    for item in raw:
        provider = str(item or "").strip().lower()
        if provider in {"gemini", "openrouter", "groq"} and provider not in order:
            order.append(provider)
    return order or ["gemini", "openrouter", "groq"]


def _is_blocked_openrouter_model(
    model_id: str, extra_parts: tuple[str, ...] = ()
) -> bool:
    normalized = str(model_id or "").strip().lower()
    blocked = _OPENROUTER_BLOCKED_MODEL_PARTS + tuple(
        part.lower() for part in extra_parts if part
    )
    return not normalized or any(part in normalized for part in blocked)


def _is_openrouter_meta_router(model_id: str) -> bool:
    return str(model_id or "").strip().lower() in {"openrouter/free", "openrouter/auto"}


def _is_free_openrouter_model(row: dict[str, Any]) -> bool:
    model_id = str(row.get("id") or "")
    if model_id.endswith(":free"):
        return True
    pricing = (row.get("pricing") or {}) if isinstance(row.get("pricing"), dict) else {}
    if not pricing or not all(key in pricing for key in ("prompt", "completion")):
        return False
    try:
        return all(
            float(pricing.get(key) or 0) == 0
            for key in ("prompt", "completion", "request")
        )
    except (TypeError, ValueError):
        return False


def _openrouter_model_rank(row: dict[str, Any]) -> tuple[int, int, int, str]:
    model_id = str(row.get("id") or "").lower()
    author_rank = next(
        (
            index
            for index, prefix in enumerate(_OPENROUTER_PREFERRED_AUTHORS)
            if model_id.startswith(prefix)
        ),
        99,
    )
    chat_rank = (
        0
        if any(
            token in model_id
            for token in ("instruct", "chat", "gemma", "gpt-oss", "maverick")
        )
        else 1
    )
    try:
        context_rank = -int(row.get("context_length") or 0)
    except (TypeError, ValueError):
        context_rank = 0
    return author_rank, chat_rank, context_rank, model_id


async def _discover_openrouter_models(
    *, free_only: bool, blocked_parts: tuple[str, ...]
) -> list[str]:
    now = time.monotonic()
    cached = _OPENROUTER_MODEL_CACHE.get("models") or []
    if cached and now < float(_OPENROUTER_MODEL_CACHE.get("expires") or 0):
        rows = list(cached)
    else:
        async with _OPENROUTER_MODEL_CACHE_LOCK:
            now = time.monotonic()
            cached = _OPENROUTER_MODEL_CACHE.get("models") or []
            if cached and now < float(_OPENROUTER_MODEL_CACHE.get("expires") or 0):
                rows = list(cached)
            else:
                settings = get_settings()
                headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
                params = {"output_modalities": "text", "sort": "throughput-high-to-low"}
                if free_only:
                    params["max_price"] = "0"
                async with pooled_session() as session:
                    async with session.get(
                        "https://openrouter.ai/api/v1/models",
                        headers=headers,
                        params=params,
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status >= 400:
                            raise AIProviderError(
                                f"OpenRouter model catalog HTTP {resp.status}"
                            )
                        rows = (
                            (data.get("data") or []) if isinstance(data, dict) else []
                        )
                        if not isinstance(rows, list):
                            raise AIProviderError(
                                "OpenRouter model catalog returned an invalid response"
                            )
                _OPENROUTER_MODEL_CACHE["models"] = rows
                _OPENROUTER_MODEL_CACHE["expires"] = time.monotonic() + 1200

    safe_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "").strip()
        lowered = model_id.lower()
        if _is_openrouter_meta_router(model_id) or _is_blocked_openrouter_model(
            model_id, blocked_parts
        ):
            continue
        if any(part in lowered for part in _OPENROUTER_BAD_CHAT_MODEL_PARTS):
            continue
        if free_only and not _is_free_openrouter_model(row):
            continue
        safe_rows.append(row)

    safe_rows.sort(key=_openrouter_model_rank)
    return [str(row.get("id") or "") for row in safe_rows if row.get("id")][:12]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = str(item or "").strip()
        if normalized and normalized.lower() not in seen:
            seen.add(normalized.lower())
            result.append(normalized)
    return result


async def _openrouter_candidates() -> list[str]:
    settings = provider_settings()
    configured = (
        []
        if _model_selection.get().get("openrouter")
        else list(settings.openrouter_models or [])
    )
    if settings.openrouter_model:
        configured.insert(0, settings.openrouter_model)
    configured = _dedupe(configured)

    blocked_parts = tuple(settings.openrouter_blocked_model_fragments or ())
    free_only = not configured or any(
        _is_openrouter_meta_router(model) or model.endswith(":free")
        for model in configured
    )
    explicit_safe = [
        model
        for model in configured
        if not _is_openrouter_meta_router(model)
        and not _is_blocked_openrouter_model(model, blocked_parts)
    ]
    rejected = [
        model
        for model in configured
        if model not in explicit_safe and not _is_openrouter_meta_router(model)
    ]
    for model in rejected:
        log.warning("Ignoring blocked OpenRouter model configuration: %s", model)

    if not settings.openrouter_discovery_enabled:
        return explicit_safe[:6]
    discovered: list[str] = []
    try:
        discovered = await _discover_openrouter_models(
            free_only=free_only, blocked_parts=blocked_parts
        )
    except Exception as exc:
        log.warning("Could not refresh the OpenRouter safe-model catalog: %s", exc)

    static_fallbacks = [
        model
        for model in _OPENROUTER_STATIC_SAFE_FALLBACKS
        if not _is_blocked_openrouter_model(model, blocked_parts)
    ]
    candidates = _dedupe(explicit_safe + discovered + static_fallbacks)
    return candidates[:6]


async def _ask_ai(
    config: dict[str, Any],
    history: list[dict[str, str]],
    user_text: str,
    conversation_context: str | None = None,
) -> tuple[str, str]:
    settings = get_settings()
    ai_config = config.get("ai", {})
    personality = (
        ai_config.get("personality") or "You are a helpful, funny Discord bot."
    )
    provider_order = _normalize_provider_order(ai_config.get("providerOrder"))
    max_tokens = max(32, min(int(ai_config.get("maxOutputTokens") or 650), 4096))
    temperature = max(
        0.0,
        min(
            float(
                ai_config.get("temperature")
                if ai_config.get("temperature") is not None
                else 0.8
            ),
            2.0,
        ),
    )
    messages = _discord_to_ai_messages(
        personality, history, user_text, ai_config, conversation_context
    )

    messages = bounded_messages(
        messages, int(ai_config.get("maxPromptCharacters") or 24000)
    )
    errors: list[str] = []
    for provider in provider_order:
        if not manager.available(provider):
            continue
        started = time.monotonic()
        try:
            answer = ""
            if provider == "gemini" and settings.gemini_api_key:
                answer = await _ask_gemini(messages, max_tokens, temperature)
            elif provider == "openrouter" and settings.openrouter_api_key:
                answer = await _ask_openrouter(messages, max_tokens, temperature)
            elif provider == "groq" and settings.groq_api_key:
                answer = await _ask_groq(messages, max_tokens, temperature)
            if answer:
                manager.success(provider, started)
                return _shape_persona_output(
                    answer, ai_config, history, user_text
                ), provider
        except Exception as exc:
            manager.failure(provider)
            log.warning("%s provider failed (%s)", provider, type(exc).__name__)
            errors.append(f"{provider}: unavailable")

    configured = [
        name
        for name, enabled in (
            ("gemini", bool(settings.gemini_api_key)),
            ("openrouter", bool(settings.openrouter_api_key)),
            ("groq", bool(settings.groq_api_key)),
        )
        if enabled
    ]
    if not configured:
        raise AIProviderError("No AI provider credentials are configured")
    raise AIProviderError("All AI providers failed. " + " | ".join(errors[-3:]))


async def _ask_gemini(
    messages: list[dict[str, str]], max_tokens: int, temperature: float
) -> str:
    settings = provider_settings()
    system_parts = []
    contents = []
    for item in messages:
        if item["role"] == "system":
            system_parts.append({"text": item["content"]})
        else:
            role = "model" if item["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": item["content"]}]})

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": system_parts}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    async with pooled_session() as session:
        async with session.post(
            url, params={"key": settings.gemini_api_key}, json=payload
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise AIProviderError(f"Gemini HTTP {resp.status}")
            try:
                candidate = data["candidates"][0]
                parts = candidate["content"]["parts"]
                text_parts = [
                    str(part.get("text") or "")
                    for part in parts
                    if isinstance(part, dict)
                    and not part.get("thought")
                    and part.get("text")
                ]
                return _clean_model_output("\n".join(text_parts), "Gemini")
            except AIProviderError:
                raise
            except Exception as exc:
                raise AIProviderError("Gemini malformed response") from exc


async def _ask_openrouter(
    messages: list[dict[str, str]], max_tokens: int, temperature: float
) -> str:
    settings = provider_settings()
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_name:
        headers["X-Title"] = settings.openrouter_app_name

    ignored_providers = _dedupe(
        list(
            settings.openrouter_ignored_providers
            or _OPENROUTER_DEFAULT_IGNORED_PROVIDERS
        )
    )
    blocked_parts = tuple(settings.openrouter_blocked_model_fragments or ())
    candidates = await _openrouter_candidates()
    if not candidates:
        raise AIProviderError(
            "OpenRouter has no allowed models after excluding Qwen and NVIDIA"
        )

    errors: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 50.0
    async with pooled_session() as session:
        for model in candidates:
            if _is_blocked_openrouter_model(model, blocked_parts):
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                errors.append("provider time budget exhausted")
                break
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "provider": {
                    "ignore": ignored_providers,
                    "require_parameters": True,
                    "allow_fallbacks": True,
                },
            }
            try:
                request_timeout = aiohttp.ClientTimeout(
                    total=max(2.0, min(20.0, remaining))
                )
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 400:
                        errors.append(f"{model}: HTTP {resp.status}")
                        continue
                    used_model = str(data.get("model") or model)
                    if _is_blocked_openrouter_model(used_model, blocked_parts):
                        errors.append(f"{model}: blocked routed model {used_model}")
                        continue
                    try:
                        raw_content = data["choices"][0]["message"].get("content")
                    except Exception:
                        errors.append(f"{model}: malformed response")
                        continue
                    try:
                        answer = _clean_model_output(
                            raw_content, f"OpenRouter/{used_model}"
                        )
                    except AIProviderError as exc:
                        errors.append(f"{model}: {exc}")
                        continue
                    log.info(
                        "OpenRouter reply generated by allowed model %s", used_model
                    )
                    return answer
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                errors.append(f"{model}: {type(exc).__name__}")
                continue

    raise AIProviderError("OpenRouter safe models failed. " + " | ".join(errors[-4:]))


async def _ask_groq(
    messages: list[dict[str, str]], max_tokens: int, temperature: float
) -> str:
    settings = provider_settings()
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.groq_model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
    }
    async with pooled_session() as session:
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise AIProviderError(f"Groq HTTP {resp.status}")
            try:
                raw_content = data["choices"][0]["message"].get("content")
            except Exception as exc:
                raise AIProviderError("Groq malformed response") from exc
            return _clean_model_output(raw_content, "Groq")


_model_selection: ContextVar[dict[str, str]] = ContextVar("provider_models", default={})


def provider_settings():
    choices = _model_selection.get()
    settings = get_settings()
    return replace(
        settings,
        gemini_model=choices.get("gemini") or settings.gemini_model,
        groq_model=choices.get("groq") or settings.groq_model,
        openrouter_model=choices.get("openrouter") or settings.openrouter_model,
    )


async def ask_ai(
    config: dict[str, Any],
    history: list[dict[str, str]],
    user_text: str,
    conversation_context: str | None = None,
) -> tuple[str, str]:
    ai = config.get("ai", {})
    token = _model_selection.set(dict(ai.get("models") or {}))
    try:
        budget = max(4000, min(int(ai.get("maxPromptCharacters") or 24000), 100000))
        text = str(user_text)[:4000]
        retained: list[dict[str, str]] = []
        used = len(text) + len(str(ai.get("personality") or ""))
        for message in reversed(history[-36:]):
            size = len(str(message.get("content") or ""))
            if used + size > budget:
                break
            retained.insert(0, message)
            used += size
        context = str(conversation_context or "")[: max(0, budget - used)]
        async with asyncio.timeout(50):
            return await _ask_ai(config, retained, text, context)
    finally:
        _model_selection.reset(token)
