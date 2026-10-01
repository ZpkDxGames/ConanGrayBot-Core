"""Offline production configuration checks; never print configured values."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import Settings  # noqa: E402


def credential_valid(raw: str, path: str) -> bool:
    try:
        if raw:
            text = (
                raw
                if raw.lstrip().startswith("{")
                else base64.b64decode(raw, validate=True).decode()
            )
        else:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = ROOT / candidate
            text = candidate.read_text(encoding="utf-8")
        payload = json.loads(text)
        return isinstance(payload, dict) and all(
            isinstance(payload.get(key), str) and payload[key]
            for key in ("project_id", "client_email", "private_key")
        )
    except (OSError, ValueError, UnicodeError):
        return False


def validate(settings: Settings) -> list[str]:
    issues = []
    for name, configured in (
        ("DISCORD_BOT_TOKEN", settings.discord_token),
        ("DISCORD_APPLICATION_ID", settings.discord_application_id),
        ("DISCORD_GUILD_ID", settings.guild_id),
        ("DISCORD_STAFF_ROLE_ID", settings.staff_role_id),
        ("CORE_SERVICE_TOKEN", settings.core_service_token),
        ("MEDIA_STREAM_SIGNING_KEY", settings.media_stream_signing_key),
        ("FIREBASE_PROJECT_ID", settings.firebase_project_id),
    ):
        if not configured:
            issues.append(f"Missing required setting: {name}")
    if not credential_valid(
        settings.firebase_service_account_json, settings.firebase_service_account_path
    ):
        issues.append("Firebase credential JSON is missing or invalid.")
    if settings.google_drive_auth_mode == "service_account":
        if not credential_valid(
            settings.google_drive_service_account_json,
            settings.google_drive_service_account_path,
        ):
            issues.append(
                "Configure independent valid Drive service-account credentials."
            )
    elif settings.google_drive_auth_mode == "oauth_user":
        if not all(
            (
                settings.google_drive_oauth_client_id,
                settings.google_drive_oauth_client_secret,
                settings.google_drive_oauth_refresh_token,
                settings.google_drive_oauth_project_id,
            )
        ):
            issues.append("Complete the private Drive user OAuth configuration.")
    else:
        issues.append("GOOGLE_DRIVE_AUTH_MODE must be service_account or oauth_user.")
    origin = urlsplit(settings.public_base_url)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in {"", "/"}
    ):
        issues.append(
            "PUBLIC_BASE_URL must be an HTTPS origin without credentials or a path."
        )
    return issues


def main() -> int:
    try:
        issues = validate(Settings())
    except (ValueError, TypeError):
        # Settings errors can originate from arbitrary environment strings.
        issues = [
            "Configuration validation failed; check settings against .env.example."
        ]
    for issue in issues:
        print(issue)
    if not issues:
        print(
            "Offline configuration checks passed. Provider and deployment checks remain separate."
        )
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
