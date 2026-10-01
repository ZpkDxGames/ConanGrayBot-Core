"""Native TTL deadlines; expired or malformed records never expose retained content."""

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import get_settings


def expiry(days: int | None = None) -> datetime:
    return datetime.now(timezone.utc) + timedelta(
        days=days if days is not None else get_settings().memory_retention_days
    )


def expired(row: dict[str, Any]) -> bool:
    value = row.get("expiresAt")
    if value is None:
        return False
    try:
        stamp = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if not isinstance(stamp, datetime):
            return True
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True
