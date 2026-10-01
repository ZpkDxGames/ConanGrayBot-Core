from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def set_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0]
            if key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def validate_google_client_secret(secret: str) -> None:
    # Current Google-generated client secrets are usually much longer than a
    # human-chosen password. Keep the check intentionally conservative so old
    # valid secrets are still accepted.
    if len(secret.strip()) < 16:
        raise SystemExit(
            "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET does not look like a Google-generated OAuth client secret. "
            "Open Google Cloud Console > APIs & Services > Credentials, open the OAuth client, and use the "
            "client secret shown there or download the client JSON. Do not invent a password."
        )


def parse_loopback_redirect(uri: str) -> tuple[str, int, bool]:
    parsed = urlparse(uri)
    if parsed.scheme != "http":
        raise SystemExit(
            "OAuth redirect URI must use http for the local loopback helper."
        )
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("OAuth redirect URI must use 127.0.0.1 or localhost.")
    if parsed.port is None:
        raise SystemExit(
            "OAuth redirect URI must include a fixed port, for example http://127.0.0.1:8765/."
        )
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise SystemExit(
            "OAuth redirect URI must point to the loopback root path, for example http://127.0.0.1:8765/."
        )
    return parsed.hostname, parsed.port, uri.endswith("/")


def load_downloaded_client(path: Path) -> tuple[str, dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read OAuth client JSON: {exc}") from exc

    if isinstance(payload.get("installed"), dict):
        return "desktop", payload["installed"]
    if isinstance(payload.get("web"), dict):
        return "web", payload["web"]
    raise SystemExit(
        "OAuth client JSON must contain either an 'installed' or 'web' section."
    )


def build_client_config(
    values: dict[str, object],
    *,
    client_secrets_path: Path | None,
    redirect_override: str | None,
) -> tuple[dict[str, object], str, str, str, str, str]:
    if client_secrets_path:
        client_type, section = load_downloaded_client(client_secrets_path)
        client_id = str(section.get("client_id") or "").strip()
        client_secret = str(section.get("client_secret") or "").strip()
        project_id = str(section.get("project_id") or "").strip()
        registered_redirects = [
            str(item) for item in section.get("redirect_uris", []) if item
        ]
    else:
        raw_type = (
            str(values.get("GOOGLE_DRIVE_OAUTH_CLIENT_TYPE") or "desktop")
            .strip()
            .lower()
        )
        aliases = {
            "installed": "desktop",
            "desktop_app": "desktop",
            "web_application": "web",
        }
        client_type = aliases.get(raw_type, raw_type)
        if client_type not in {"desktop", "web"}:
            raise SystemExit(
                "GOOGLE_DRIVE_OAUTH_CLIENT_TYPE must be 'desktop' or 'web'."
            )
        client_id = str(values.get("GOOGLE_DRIVE_OAUTH_CLIENT_ID") or "").strip()
        client_secret = str(
            values.get("GOOGLE_DRIVE_OAUTH_CLIENT_SECRET") or ""
        ).strip()
        project_id = str(values.get("GOOGLE_DRIVE_OAUTH_PROJECT_ID") or "").strip()
        registered_redirects = []

    missing = [
        name
        for name, value in (
            ("GOOGLE_DRIVE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_DRIVE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_DRIVE_OAUTH_PROJECT_ID", project_id),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing OAuth values: " + ", ".join(missing))

    validate_google_client_secret(client_secret)

    redirect_uri = (
        redirect_override
        or str(values.get("GOOGLE_DRIVE_OAUTH_REDIRECT_URI") or "").strip()
        or DEFAULT_REDIRECT_URI
    )
    parse_loopback_redirect(redirect_uri)

    if (
        client_type == "web"
        and registered_redirects
        and redirect_uri not in registered_redirects
    ):
        raise SystemExit(
            "The downloaded Web application OAuth client does not authorize the helper redirect URI. "
            f"Add this exact URI under Authorized redirect URIs, download the JSON again, and retry: {redirect_uri}"
        )

    section_name = "installed" if client_type == "desktop" else "web"
    client_config: dict[str, object] = {
        section_name: {
            "client_id": client_id,
            "client_secret": client_secret,
            "project_id": project_id,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": registered_redirects or [redirect_uri],
        }
    }
    return (
        client_config,
        client_type,
        client_id,
        client_secret,
        project_id,
        redirect_uri,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authorize the Google account that owns the Conan media folder and save its refresh token."
    )
    parser.add_argument(
        "--client-secrets",
        type=Path,
        help="Path to the OAuth client JSON downloaded from Google Cloud. Recommended.",
    )
    parser.add_argument(
        "--redirect-uri",
        help=f"Fixed local redirect URI. Default: {DEFAULT_REDIRECT_URI}",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening it automatically.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Do not update .env; only confirm that authorization completed.",
    )
    args = parser.parse_args()

    values = dict(dotenv_values(ENV_PATH))
    client_config, client_type, client_id, client_secret, project_id, redirect_uri = (
        build_client_config(
            values,
            client_secrets_path=args.client_secrets,
            redirect_override=args.redirect_uri,
        )
    )
    host, port, trailing_slash = parse_loopback_redirect(redirect_uri)

    if client_type == "web":
        print("This OAuth client is a Web application.")
        print("Google Cloud must list this exact Authorized redirect URI:")
        print(redirect_uri)
    else:
        print(
            "This OAuth client is a Desktop app. Google accepts its local loopback redirect automatically."
        )

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    try:
        credentials = flow.run_local_server(
            host=host,
            port=port,
            redirect_uri_trailing_slash=trailing_slash,
            authorization_prompt_message=(
                "Open this URL in a browser and authorize the Google account that owns the media Drive folder:\n{url}"
            ),
            success_message="Google Drive authorization completed. You can close this tab.",
            open_browser=not args.no_browser,
            access_type="offline",
            prompt="consent",
        )
    except OSError as exc:
        raise SystemExit(
            f"Could not start the local OAuth callback on {redirect_uri}. Close any app using port {port}, "
            "or choose another fixed loopback port with --redirect-uri and register that exact URI in Google Cloud."
        ) from exc

    if not credentials.refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Revoke this app's access in your Google Account, "
            "then run the helper again with consent enabled."
        )

    if args.no_write:
        print(
            "Authorization succeeded. Re-run without --no-write to save the refresh token into .env."
        )
        return

    backup = ROOT / ".env.before-drive-oauth"
    if ENV_PATH.exists() and not backup.exists():
        shutil.copy2(ENV_PATH, backup)

    set_env_values(
        ENV_PATH,
        {
            "GOOGLE_DRIVE_AUTH_MODE": "oauth_user",
            "GOOGLE_DRIVE_EXPECTED_PROJECT_ID": project_id,
            "GOOGLE_DRIVE_OAUTH_PROJECT_ID": project_id,
            "GOOGLE_DRIVE_OAUTH_CLIENT_TYPE": client_type,
            "GOOGLE_DRIVE_OAUTH_CLIENT_ID": client_id,
            "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET": client_secret,
            "GOOGLE_DRIVE_OAUTH_REDIRECT_URI": redirect_uri,
            "GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN": credentials.refresh_token,
        },
    )
    print("Google Drive OAuth is now active in the private .env.")
    print("Run: python scripts/validate_env.py")
    print(
        "Then copy the updated private .env values to Discloud and restart the application."
    )
    print("The refresh token was not printed to the terminal.")


if __name__ == "__main__":
    main()
