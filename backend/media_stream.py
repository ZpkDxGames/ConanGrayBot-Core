from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from urllib.parse import quote

from .config import get_settings


def normalize_stream_filename(value: str) -> str:
    """Return a stable, URL-safe basename for signed Drive stream links."""
    name = Path(str(value or "media")).name.strip() or "media"
    return name[:220]


def _signing_secret() -> str:
    settings = get_settings()
    return (
        str(settings.media_stream_signing_key or "").strip()
        or str(settings.dashboard_key or "").strip()
        or str(settings.discord_token or "").strip()
    )


def drive_stream_signature(file_id: str, filename: str) -> str:
    secret = _signing_secret()
    if not secret:
        return ""
    payload = f"{str(file_id or '').strip()}\n{normalize_stream_filename(filename)}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_drive_stream_url(file_id: str, filename: str) -> str:
    """Build a permanent signed backend URL that Discord can preview inline."""
    settings = get_settings()
    normalized_id = str(file_id or "").strip()
    normalized_name = normalize_stream_filename(filename)
    signature = drive_stream_signature(normalized_id, normalized_name)
    base_url = str(settings.public_base_url or "").strip().rstrip("/")
    if not normalized_id or not normalized_name or not signature or not base_url:
        return ""
    return (
        f"{base_url}/media/drive/{quote(normalized_id, safe='')}/"
        f"{quote(normalized_name, safe='')}?sig={signature}"
    )


def validate_drive_stream_signature(file_id: str, filename: str, signature: str) -> bool:
    expected = drive_stream_signature(file_id, filename)
    supplied = str(signature or "").strip().lower()
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))
