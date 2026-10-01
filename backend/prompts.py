"""Final provider payload budget, including system instructions and current message."""

from typing import Any


def bounded_messages(
    messages: list[dict[str, Any]], limit: int
) -> list[dict[str, str]]:
    limit = max(4000, min(limit, 100000))
    system = [row for row in messages if row.get("role") == "system"]
    conversation = [row for row in messages if row.get("role") != "system"]
    system_text = "\n\n".join(str(row.get("content") or "") for row in system)[
        : limit // 2
    ]
    remaining = limit - len(system_text)
    retained: list[dict[str, str]] = []
    for row in reversed(conversation):
        content = str(row.get("content") or "")
        if not retained:
            content = content[: min(4000, remaining)]
        if len(content) > remaining:
            break
        retained.insert(0, {"role": str(row.get("role") or "user"), "content": content})
        remaining -= len(content)
    return (
        [{"role": "system", "content": system_text}] if system_text else []
    ) + retained
