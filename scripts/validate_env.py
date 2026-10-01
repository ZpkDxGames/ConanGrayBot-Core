from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
values = dotenv_values(ROOT / ".env")
issues: list[str] = []
notes: list[str] = []


def value(name: str) -> str:
    return str(values.get(name) or "").strip()


def enabled(name: str, default: bool = False) -> bool:
    raw = value(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def decode_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return None


def load_credential(
    path_name: str, json_name: str
) -> tuple[dict[str, Any] | None, str]:
    inline = value(json_name)
    if inline:
        return decode_json(inline), f"{json_name}"
    raw_path = value(path_name)
    if not raw_path:
        return None, ""
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return None, str(path)
    try:
        return json.loads(path.read_text(encoding="utf-8")), str(path)
    except Exception:
        return None, str(path)


required = [
    "DISCORD_BOT_TOKEN",
    "DISCORD_APPLICATION_ID",
    "DISCORD_GUILD_ID",
    "DASHBOARD_SESSION_KEY",
    "FIREBASE_PROJECT_ID",
    "GEMINI_API_KEY",
    "OPENWEATHER_API_KEY",
]
missing = [name for name in required if not value(name)]
if missing:
    issues.append("Missing required values: " + ", ".join(missing))

firebase_payload, firebase_source = load_credential(
    "FIREBASE_SERVICE_ACCOUNT_PATH",
    "FIREBASE_SERVICE_ACCOUNT_JSON",
)
if not firebase_payload:
    issues.append(
        f"Firebase credential could not be loaded from {firebase_source or 'configured environment values'}."
    )
else:
    for field in ("project_id", "client_email", "private_key"):
        if not firebase_payload.get(field):
            issues.append(f"Firebase credential JSON is missing {field}.")
    firebase_project = value("FIREBASE_PROJECT_ID")
    credential_project = str(firebase_payload.get("project_id") or "")
    if (
        firebase_project
        and credential_project
        and firebase_project != credential_project
    ):
        issues.append(
            f"FIREBASE_PROJECT_ID ({firebase_project}) does not match Firebase credential project_id ({credential_project})."
        )

if value("GOOGLE_DRIVE_API_KEY"):
    issues.append(
        "Remove GOOGLE_DRIVE_API_KEY. Private Drive access must use service-account OAuth or Google-user OAuth."
    )

auth_mode = value("GOOGLE_DRIVE_AUTH_MODE") or "service_account"
if auth_mode not in {"service_account", "oauth_user"}:
    issues.append("GOOGLE_DRIVE_AUTH_MODE must be service_account or oauth_user.")

expected_project = value("GOOGLE_DRIVE_EXPECTED_PROJECT_ID")
active_project = ""

if auth_mode == "service_account":
    drive_payload, drive_source = load_credential(
        "GOOGLE_DRIVE_SERVICE_ACCOUNT_PATH",
        "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON",
    )
    if not drive_payload and enabled("GOOGLE_DRIVE_ALLOW_FIREBASE_FALLBACK"):
        drive_payload = firebase_payload
        drive_source = "Firebase credential fallback"
    if not drive_payload:
        issues.append(
            "Drive service-account credentials are missing. Configure GOOGLE_DRIVE_SERVICE_ACCOUNT_PATH/JSON, "
            "or deliberately enable GOOGLE_DRIVE_ALLOW_FIREBASE_FALLBACK."
        )
    else:
        for field in ("project_id", "client_email", "private_key"):
            if not drive_payload.get(field):
                issues.append(f"Drive service-account JSON is missing {field}.")
        active_project = str(drive_payload.get("project_id") or "")
        if expected_project and active_project and expected_project != active_project:
            issues.append(
                f"GOOGLE_DRIVE_EXPECTED_PROJECT_ID ({expected_project}) does not match active Drive "
                f"service-account project_id ({active_project})."
            )
        notes.append(
            f"Drive service-account mode uses project {active_project or 'unknown'} from {drive_source}."
        )
        if firebase_payload and drive_payload.get(
            "client_email"
        ) == firebase_payload.get("client_email"):
            notes.append(
                "Drive is explicitly using the Firebase service account. The Drive folder may belong to another "
                "Google account, but Drive API must still be enabled in this service account's Cloud project."
            )
else:
    oauth_client_id = value("GOOGLE_DRIVE_OAUTH_CLIENT_ID")
    oauth_client_secret = value("GOOGLE_DRIVE_OAUTH_CLIENT_SECRET")
    oauth_refresh_token = value("GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN")
    oauth_project = value("GOOGLE_DRIVE_OAUTH_PROJECT_ID")
    oauth_client_type = value("GOOGLE_DRIVE_OAUTH_CLIENT_TYPE").lower() or "desktop"
    oauth_redirect_uri = (
        value("GOOGLE_DRIVE_OAUTH_REDIRECT_URI") or "http://127.0.0.1:8765/"
    )
    if not oauth_client_id:
        issues.append("GOOGLE_DRIVE_OAUTH_CLIENT_ID is required for oauth_user mode.")
    elif not re.fullmatch(
        r"\d+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", oauth_client_id
    ):
        issues.append(
            "GOOGLE_DRIVE_OAUTH_CLIENT_ID does not look like a Google OAuth client ID."
        )
    if not oauth_client_secret:
        issues.append(
            "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET is required for oauth_user mode. Use the Google-generated secret, not a custom password."
        )
    elif len(oauth_client_secret) < 16:
        issues.append(
            "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET does not look like a Google-generated OAuth client secret."
        )
    if not oauth_refresh_token:
        issues.append(
            "GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN is required for oauth_user mode."
        )
    if not oauth_project:
        issues.append("GOOGLE_DRIVE_OAUTH_PROJECT_ID is required for oauth_user mode.")
    if oauth_client_type not in {"desktop", "web"}:
        issues.append("GOOGLE_DRIVE_OAUTH_CLIENT_TYPE must be desktop or web.")
    parsed_redirect = urlparse(oauth_redirect_uri)
    if (
        parsed_redirect.scheme != "http"
        or parsed_redirect.hostname not in {"127.0.0.1", "localhost"}
        or parsed_redirect.port is None
        or parsed_redirect.path not in {"", "/"}
        or parsed_redirect.params
        or parsed_redirect.query
        or parsed_redirect.fragment
    ):
        issues.append(
            "GOOGLE_DRIVE_OAUTH_REDIRECT_URI must be a fixed loopback URI such as http://127.0.0.1:8765/."
        )
    if oauth_client_type == "web":
        notes.append(
            f"Web OAuth client mode requires this exact Authorized redirect URI in Google Cloud: {oauth_redirect_uri}"
        )
    active_project = oauth_project
    if expected_project and active_project and expected_project != active_project:
        issues.append(
            f"GOOGLE_DRIVE_EXPECTED_PROJECT_ID ({expected_project}) does not match OAuth project ({active_project})."
        )
    notes.append(
        f"Drive Google-user OAuth mode uses project {active_project or 'unknown'} and the authorized human Drive account."
    )

client_id = value("GOOGLE_DRIVE_OAUTH_CLIENT_ID")
oauth_project = value("GOOGLE_DRIVE_OAUTH_PROJECT_ID")
if bool(client_id) != bool(oauth_project):
    issues.append(
        "Set both GOOGLE_DRIVE_OAUTH_CLIENT_ID and GOOGLE_DRIVE_OAUTH_PROJECT_ID, or leave both blank."
    )
if client_id and not re.fullmatch(
    r"\d+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id
):
    issues.append(
        "GOOGLE_DRIVE_OAUTH_CLIENT_ID does not look like a Google OAuth client ID."
    )
if auth_mode != "oauth_user" and client_id:
    notes.append(
        f"OAuth client metadata for project {oauth_project} is registered but inactive while auth mode is {auth_mode}."
    )


openweather_key = value("OPENWEATHER_API_KEY")
if openweather_key and len(openweather_key) < 20:
    issues.append(
        "OPENWEATHER_API_KEY does not look like a complete OpenWeather API key."
    )
if openweather_key:
    notes.append(
        "OpenWeather current conditions, geocoding, and 5-day forecast are configured."
    )

public_base_url = value("PUBLIC_BASE_URL") or "https://conanbot.discloud.app"
parsed_public_base = urlparse(public_base_url)
if parsed_public_base.scheme != "https" or not parsed_public_base.netloc:
    issues.append(
        "PUBLIC_BASE_URL must be the public HTTPS origin of the backend, such as https://conanbot.discloud.app."
    )
if not value("MEDIA_STREAM_SIGNING_KEY"):
    notes.append(
        "Large-media stream links use DASHBOARD_SESSION_KEY as their signing secret."
    )

if value("ENABLE_MESSAGE_CONTENT_INTENT").lower() not in {"1", "true", "yes", "on"}:
    issues.append("ENABLE_MESSAGE_CONTENT_INTENT must be true for reply-driven games.")

openrouter_model_values = [value("OPENROUTER_MODEL")]
openrouter_model_values.extend(
    item.strip() for item in value("OPENROUTER_MODELS").split(",") if item.strip()
)
blocked_openrouter_parts = {
    item.strip().lower()
    for item in (
        value("OPENROUTER_BLOCKED_MODEL_FRAGMENTS") or "qwen,nvidia,nemotron"
    ).split(",")
    if item.strip()
}
for model_id in openrouter_model_values:
    normalized_model = model_id.lower()
    if normalized_model in {"openrouter/free", "openrouter/auto"}:
        issues.append(
            f"OPENROUTER model {model_id} is a random router and cannot guarantee the Qwen/NVIDIA exclusion policy."
        )
    if any(part in normalized_model for part in blocked_openrouter_parts):
        issues.append(f"Blocked OpenRouter model configured: {model_id}.")
ignored_openrouter_providers = {
    item.strip().lower()
    for item in (value("OPENROUTER_IGNORED_PROVIDERS") or "nvidia").split(",")
    if item.strip()
}
if value("OPENROUTER_API_KEY") and "nvidia" not in ignored_openrouter_providers:
    issues.append("OPENROUTER_IGNORED_PROVIDERS must include nvidia.")

if issues:
    print("Environment check failed:")
    for issue in issues:
        print(f"- {issue}")
    raise SystemExit(1)

print(
    f"Environment check passed. Active Drive auth mode: {auth_mode}; active project: {active_project or 'unknown'}."
)
for note in notes:
    print(f"- {note}")
