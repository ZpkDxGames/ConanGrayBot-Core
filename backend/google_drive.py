from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import random
from pathlib import Path
from typing import Any

from .config import get_settings

log = logging.getLogger("conan.drive")

_DRIVE_FOLDER_RE = re.compile(r"(?:folders/|[?&]id=)([A-Za-z0-9_-]{10,})")
_PROJECT_ID_RE = re.compile(r"project(?:=|\s+)([A-Za-z0-9_-]{6,})", re.IGNORECASE)

SUPPORTED_MEDIA_EXTENSIONS: dict[str, set[str]] = {
    "image": {".png", ".webp", ".jpg", ".jpeg"},
    "video": {".mp4"},
}
SUPPORTED_MEDIA_MIME_TYPES: dict[str, set[str]] = {
    "image": {"image/png", "image/webp", "image/jpeg"},
    "video": {"video/mp4"},
}


class DriveConfigurationError(RuntimeError):
    """A Drive error that can be shown safely and usefully in the dashboard."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "drive_error",
        reason: str = "",
        status: int = 502,
        project_id: str = "",
        action_url: str = "",
        raw_message: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status = status
        self.project_id = project_id
        self.action_url = action_url
        self.raw_message = raw_message

    def as_dict(
        self,
        *,
        service_account_email: str = "",
        principal_email: str = "",
        credential_project_id: str = "",
        auth_mode: str = "service_account",
    ) -> dict[str, Any]:
        effective_project = credential_project_id or self.project_id
        return {
            "message": str(self),
            "code": self.code,
            "reason": self.reason,
            "projectId": effective_project,
            "credentialProjectId": credential_project_id,
            "actionUrl": self.action_url,
            "authMode": auth_mode,
            "principalEmail": principal_email or service_account_email,
            "serviceAccountEmail": service_account_email,
        }


def is_supported_media_file(file: dict[str, Any], media_type: str = "") -> bool:
    """Return True only for the image/video formats supported by /media."""
    requested = media_type if media_type in SUPPORTED_MEDIA_EXTENSIONS else ""
    name = str(file.get("name") or "").lower()
    mime_type = str(file.get("mimeType") or "").lower().split(";", 1)[0]
    extension = Path(name).suffix
    types = [requested] if requested else ["image", "video"]
    return any(
        extension in SUPPORTED_MEDIA_EXTENSIONS[kind]
        or mime_type in SUPPORTED_MEDIA_MIME_TYPES[kind]
        for kind in types
    )


def drive_media_type(file: dict[str, Any]) -> str | None:
    for kind in ("image", "video"):
        if is_supported_media_file(file, kind):
            return kind
    return None


def _http_error_details(exc: Exception) -> tuple[int, str, str, str]:
    status = int(getattr(getattr(exc, "resp", None), "status", 502) or 502)
    raw = str(exc)
    reason = ""
    message = raw
    content = getattr(exc, "content", b"")
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        payload = json.loads(content) if content else {}
        error = payload.get("error") or {}
        message = str(error.get("message") or message)
        entries = error.get("errors") or []
        if entries:
            reason = str(entries[0].get("reason") or "")
        if not reason:
            details = error.get("details") or []
            for detail in details:
                reason = str(detail.get("reason") or detail.get("metadata", {}).get("reason") or "")
                if reason:
                    break
    except Exception:
        pass
    project_match = _PROJECT_ID_RE.search(f"{message} {raw}")
    project_id = project_match.group(1) if project_match else ""
    return status, reason, message, project_id


def normalize_drive_error(
    exc: Exception,
    *,
    credential_project_id: str = "",
    auth_mode: str = "service_account",
) -> DriveConfigurationError:
    """Translate Google HttpError responses into dashboard-friendly diagnostics."""
    status, reason, message, reported_project_id = _http_error_details(exc)
    normalized = reason.lower()
    lower_message = message.lower()
    effective_project = credential_project_id or reported_project_id
    project_url = (
        f"https://console.cloud.google.com/apis/library/drive.googleapis.com?project={effective_project}"
        if effective_project
        else "https://console.cloud.google.com/apis/library/drive.googleapis.com"
    )

    if "api key" in lower_message and "different project" in lower_message:
        return DriveConfigurationError(
            "The Drive API key and service-account credential belong to different Google Cloud projects. "
            "Use service-account OAuth by itself and remove GOOGLE_DRIVE_API_KEY from the deployment.",
            code="credential_project_mismatch",
            reason=reason or "credentialProjectMismatch",
            status=400,
            project_id=effective_project,
            action_url=project_url,
            raw_message=message,
        )
    if normalized == "accessnotconfigured" or "has not been used" in lower_message or "it is disabled" in lower_message:
        return DriveConfigurationError(
            "Google Drive API is disabled for the active credential project. Enable the Drive API in that "
            "project, wait a few minutes, then test again.",
            code="drive_api_disabled",
            reason=reason or "accessNotConfigured",
            status=424,
            project_id=effective_project,
            action_url=project_url,
            raw_message=message,
        )
    if normalized in {"notfound", "filenotfound"} or status == 404:
        return DriveConfigurationError(
            "The Drive folder was not found or is not shared with the configured service account.",
            code="folder_not_found",
            reason=reason or "notFound",
            status=404,
            project_id=effective_project,
            raw_message=message,
        )
    if normalized in {"insufficientpermissions", "forbidden"} or status == 403:
        return DriveConfigurationError(
            "The authenticated Drive identity cannot access this folder. Share the folder with that identity and try again.",
            code="folder_permission_denied",
            reason=reason or "forbidden",
            status=403,
            project_id=effective_project,
            raw_message=message,
        )
    if normalized == "storagequotaexceeded" or "storage quota" in lower_message:
        message_text = (
            "The Google user account has no available Drive storage."
            if auth_mode == "oauth_user"
            else "Service accounts cannot own files in My Drive. Use a Shared Drive or switch to Google-user OAuth."
        )
        return DriveConfigurationError(
            message_text,
            code="drive_storage_unavailable",
            reason=reason or "storageQuotaExceeded",
            status=403,
            project_id=effective_project,
            raw_message=message,
        )
    if normalized in {"ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded"} or status == 429:
        return DriveConfigurationError(
            "Google Drive temporarily rate-limited the service-account project. Wait briefly and retry.",
            code="drive_rate_limited",
            reason=reason or "rateLimitExceeded",
            status=429,
            project_id=effective_project,
            raw_message=message,
        )
    return DriveConfigurationError(
        f"Google Drive request failed: {message}",
        code="drive_request_failed",
        reason=reason,
        status=status if 400 <= status <= 599 else 502,
        project_id=effective_project,
        raw_message=message,
    )


def normalize_drive_folder_id(value: str | None) -> str:
    """Accept either a raw Drive folder ID or a standard folder URL."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = _DRIVE_FOLDER_RE.search(raw)
    return match.group(1) if match else raw


def _decode_service_account(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        log.exception("Could not parse Google Drive service account JSON")
        return None



def load_drive_service_account() -> dict[str, Any] | None:
    """Load a dedicated Drive service account.

    Firebase credentials are only considered when GOOGLE_DRIVE_ALLOW_FIREBASE_FALLBACK=true.
    This prevents a Drive folder owned by another account from silently using the Firebase
    project's service account.
    """
    settings = get_settings()
    raw_candidates = [settings.google_drive_service_account_json]
    if settings.google_drive_allow_firebase_fallback:
        raw_candidates.append(settings.firebase_service_account_json)
    for raw in raw_candidates:
        parsed = _decode_service_account(raw)
        if parsed:
            return parsed

    path_candidates = [settings.google_drive_service_account_path]
    if settings.google_drive_allow_firebase_fallback:
        path_candidates.extend([
            settings.firebase_service_account_path,
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
        ])
    for raw_path in path_candidates:
        path = Path(str(raw_path or ""))
        if not raw_path or not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Could not read Google Drive service account file: %s", path)
    return None


class GoogleDriveArchive:
    """Small async wrapper around Google Drive API v3 for Discord media files.

    Firebase and Drive authentication are deliberately independent. Drive supports either
    a dedicated service account or an OAuth refresh token belonging to the human Drive user.
    """

    SCOPES = ["https://www.googleapis.com/auth/drive"]
    AUTH_MODES = {"service_account", "oauth_user"}

    def __init__(self) -> None:
        self.settings = get_settings()
        requested_mode = self.settings.google_drive_auth_mode or "service_account"
        self.auth_mode = requested_mode if requested_mode in self.AUTH_MODES else "service_account"

        self.service_account_info = load_drive_service_account()
        self.service_account_email = str((self.service_account_info or {}).get("client_email") or "")
        self.service_account_project_id = str((self.service_account_info or {}).get("project_id") or "")

        self.oauth_client_id = self.settings.google_drive_oauth_client_id
        self.oauth_client_secret = self.settings.google_drive_oauth_client_secret
        self.oauth_refresh_token = self.settings.google_drive_oauth_refresh_token
        self.oauth_token_uri = self.settings.google_drive_oauth_token_uri or "https://oauth2.googleapis.com/token"
        self.oauth_project_id = self.settings.google_drive_oauth_project_id
        self.oauth_user_email = self.settings.google_drive_oauth_user_email

        self.credential_project_id = (
            self.oauth_project_id if self.auth_mode == "oauth_user" else self.service_account_project_id
        )
        self.expected_project_id = self.settings.google_drive_expected_project_id or self.credential_project_id
        self.project_aligned = bool(
            self.credential_project_id
            and (not self.expected_project_id or self.credential_project_id == self.expected_project_id)
        )
        self.principal_email = (
            self.oauth_user_email if self.auth_mode == "oauth_user" else self.service_account_email
        )
        self._credentials: Any | None = None
        self._credentials_lock = asyncio.Lock()
        self._service: Any | None = None
        self._service_lock = asyncio.Lock()

        if os.getenv("GOOGLE_DRIVE_API_KEY", "").strip():
            log.warning(
                "GOOGLE_DRIVE_API_KEY is ignored. Private Drive access uses OAuth credentials only."
            )
        if requested_mode not in self.AUTH_MODES:
            log.error("Unsupported GOOGLE_DRIVE_AUTH_MODE=%s", requested_mode)
        if self.auth_mode == "service_account" and not self.service_account_info:
            log.warning(
                "Dedicated Google Drive service-account credentials are missing. Firebase fallback is %s.",
                "enabled" if self.settings.google_drive_allow_firebase_fallback else "disabled",
            )
        if self.auth_mode == "oauth_user" and not self._oauth_complete:
            log.warning("Google Drive OAuth user credentials are incomplete.")
        if self.expected_project_id and self.credential_project_id and not self.project_aligned:
            log.error(
                "Drive credential project mismatch: expected %s but active credential project is %s.",
                self.expected_project_id,
                self.credential_project_id,
            )

    @property
    def _oauth_complete(self) -> bool:
        return bool(
            self.oauth_client_id
            and self.oauth_client_secret
            and self.oauth_refresh_token
            and self.oauth_project_id
        )

    @property
    def configured(self) -> bool:
        credential_ready = bool(self.service_account_info) if self.auth_mode == "service_account" else self._oauth_complete
        return bool(credential_ready and self.project_aligned)

    @property
    def identity_label(self) -> str:
        return "Google user OAuth" if self.auth_mode == "oauth_user" else "Service account"

    def status_payload(self, *, folder_id: str = "") -> dict[str, Any]:
        return {
            "configured": self.configured,
            "authMode": self.auth_mode,
            "identityLabel": self.identity_label,
            "principalEmail": self.principal_email,
            "serviceAccountEmail": self.service_account_email,
            "credentialProjectId": self.credential_project_id,
            "expectedProjectId": self.expected_project_id,
            "projectAligned": self.project_aligned,
            "firebaseFallbackEnabled": self.settings.google_drive_allow_firebase_fallback,
            "oauthClientConfigured": bool(self.oauth_client_id),
            "oauthCredentialsComplete": self._oauth_complete,
            "oauthClientProjectId": self.oauth_project_id,
            "oauthClientActive": self.auth_mode == "oauth_user",
            "folderId": normalize_drive_folder_id(folder_id),
        }

    def _configuration_error(self) -> DriveConfigurationError:
        action_project = self.expected_project_id or self.credential_project_id
        action_url = (
            f"https://console.cloud.google.com/apis/library/drive.googleapis.com?project={action_project}"
            if action_project
            else "https://console.cloud.google.com/apis/library/drive.googleapis.com"
        )
        if self.auth_mode == "oauth_user":
            missing: list[str] = []
            if not self.oauth_client_id:
                missing.append("GOOGLE_DRIVE_OAUTH_CLIENT_ID")
            if not self.oauth_client_secret:
                missing.append("GOOGLE_DRIVE_OAUTH_CLIENT_SECRET")
            if not self.oauth_refresh_token:
                missing.append("GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN")
            if not self.oauth_project_id:
                missing.append("GOOGLE_DRIVE_OAUTH_PROJECT_ID")
            if missing:
                guidance = (
                    " Run python scripts/google_drive_oauth_setup.py on a local computer while signed into "
                    "the Google account that owns the media folder."
                    if missing == ["GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN"]
                    else ""
                )
                return DriveConfigurationError(
                    "Google Drive user OAuth is incomplete. Add the missing private environment values: "
                    + ", ".join(missing)
                    + "."
                    + guidance,
                    code="drive_oauth_incomplete",
                    reason="oauthCredentialsIncomplete",
                    status=424,
                    project_id=action_project,
                    action_url=action_url,
                )
        elif not self.service_account_info:
            return DriveConfigurationError(
                "Dedicated Google Drive credentials are missing. Configure GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON or "
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_PATH. Firebase credentials are not reused unless "
                "GOOGLE_DRIVE_ALLOW_FIREBASE_FALLBACK=true.",
                code="drive_credentials_missing",
                reason="credentialsMissing",
                status=424,
                project_id=action_project,
                action_url=action_url,
            )
        if not self.project_aligned:
            return DriveConfigurationError(
                "The active Google Drive credential project does not match GOOGLE_DRIVE_EXPECTED_PROJECT_ID.",
                code="credential_project_mismatch",
                reason="credentialProjectMismatch",
                status=400,
                project_id=self.credential_project_id or action_project,
                action_url=action_url,
            )
        return DriveConfigurationError(
            "Google Drive credentials are not ready.",
            code="drive_credentials_missing",
            reason="credentialsMissing",
            status=424,
            project_id=action_project,
            action_url=action_url,
        )

    def _normalize_http_error(self, exc: Exception) -> DriveConfigurationError:
        return normalize_drive_error(
            exc,
            credential_project_id=self.credential_project_id,
            auth_mode=self.auth_mode,
        )

    def _build_credentials_sync(self) -> Any:
        if self.auth_mode == "oauth_user":
            from google.oauth2.credentials import Credentials

            return Credentials(
                token=None,
                refresh_token=self.oauth_refresh_token,
                token_uri=self.oauth_token_uri,
                client_id=self.oauth_client_id,
                client_secret=self.oauth_client_secret,
                scopes=self.SCOPES,
            )

        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            self.service_account_info,
            scopes=self.SCOPES,
        )
        if self.settings.google_drive_impersonate_user:
            credentials = credentials.with_subject(self.settings.google_drive_impersonate_user)
        return credentials

    async def _get_credentials(self) -> Any:
        if self._credentials is not None:
            return self._credentials
        if not self.configured:
            raise self._configuration_error()

        async with self._credentials_lock:
            if self._credentials is None:
                self._credentials = await asyncio.to_thread(self._build_credentials_sync)
            return self._credentials

    async def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        credentials = await self._get_credentials()

        async with self._service_lock:
            if self._service is not None:
                return self._service

            def build_service() -> Any:
                from googleapiclient.discovery import build

                return build("drive", "v3", credentials=credentials, cache_discovery=False)

            self._service = await asyncio.to_thread(build_service)
            return self._service

    async def test_folder(self, folder_id_or_url: str) -> dict[str, Any]:
        folder_id = normalize_drive_folder_id(folder_id_or_url)
        if not folder_id:
            raise ValueError("Google Drive folder ID is required")
        service = await self._get_service()

        def work() -> dict[str, Any]:
            data = (
                service.files()
                .get(
                    fileId=folder_id,
                    fields="id,name,mimeType,webViewLink,driveId,capabilities(canAddChildren)",
                    supportsAllDrives=True,
                )
                .execute()
            )
            if data.get("mimeType") != "application/vnd.google-apps.folder":
                raise ValueError("The configured Google Drive ID is not a folder")
            capabilities = data.get("capabilities") or {}
            about = service.about().get(fields="user(displayName,emailAddress),storageQuota(limit,usage)").execute()
            user = about.get("user") or {}
            storage = about.get("storageQuota") or {}
            return {
                "id": data.get("id"),
                "name": data.get("name"),
                "webViewLink": data.get("webViewLink"),
                "driveId": data.get("driveId"),
                "canAddChildren": capabilities.get("canAddChildren", True),
                "authenticatedUserEmail": user.get("emailAddress") or self.principal_email,
                "authenticatedUserName": user.get("displayName"),
                "storageLimit": storage.get("limit"),
                "storageUsage": storage.get("usage"),
            }

        try:
            result = await asyncio.to_thread(work)
            if result.get("authenticatedUserEmail"):
                self.principal_email = str(result["authenticatedUserEmail"])
            return result
        except DriveConfigurationError:
            raise
        except Exception as exc:
            try:
                from googleapiclient.errors import HttpError
            except Exception:  # pragma: no cover - dependency import guard
                HttpError = ()  # type: ignore[assignment]
            if HttpError and isinstance(exc, HttpError):
                raise self._normalize_http_error(exc) from exc
            raise

    async def list_media_files(
        self,
        folder_id_or_url: str,
        *,
        media_type: str = "",
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List supported images/videos directly from the configured Drive folder."""
        folder_id = normalize_drive_folder_id(folder_id_or_url)
        if not folder_id:
            raise ValueError("Google Drive folder ID is required")
        if media_type and media_type not in {"image", "video"}:
            raise ValueError("Media type must be image or video")
        service = await self._get_service()
        capped_limit = max(1, min(int(limit or 1000), 5000))

        def work() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            page_token: str | None = None
            while len(rows) < capped_limit:
                response = (
                    service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields=(
                            "nextPageToken,files(id,name,mimeType,size,createdTime,modifiedTime,"
                            "webViewLink,webContentLink,thumbnailLink,iconLink,parents)"
                        ),
                        pageSize=min(1000, capped_limit),
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                for item in response.get("files") or []:
                    if is_supported_media_file(item, media_type):
                        item = dict(item)
                        item["mediaType"] = drive_media_type(item)
                        rows.append(item)
                        if len(rows) >= capped_limit:
                            break
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return rows

        try:
            return await asyncio.to_thread(work)
        except DriveConfigurationError:
            raise
        except Exception as exc:
            try:
                from googleapiclient.errors import HttpError
            except Exception:  # pragma: no cover
                HttpError = ()  # type: ignore[assignment]
            if HttpError and isinstance(exc, HttpError):
                raise self._normalize_http_error(exc) from exc
            raise

    async def random_media_file(
        self,
        folder_id_or_url: str,
        *,
        media_type: str = "",
        limit: int = 1000,
    ) -> dict[str, Any] | None:
        files = await self.list_media_files(folder_id_or_url, media_type=media_type, limit=limit)
        return random.choice(files) if files else None

    async def download_file(self, file_id: str, destination: str | Path) -> Path:
        """Download a Drive file to a local path without making it public."""
        if not file_id:
            raise ValueError("Google Drive file ID is required")
        service = await self._get_service()
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)

        def work() -> Path:
            from googleapiclient.http import MediaIoBaseDownload

            request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            with path.open("wb") as handle:
                downloader = MediaIoBaseDownload(handle, request, chunksize=5 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return path

        try:
            return await asyncio.to_thread(work)
        except DriveConfigurationError:
            raise
        except Exception as exc:
            try:
                from googleapiclient.errors import HttpError
            except Exception:  # pragma: no cover
                HttpError = ()  # type: ignore[assignment]
            if HttpError and isinstance(exc, HttpError):
                raise self._normalize_http_error(exc) from exc
            raise

    async def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        """Return the fields needed to stream a private Drive file safely."""
        if not file_id:
            raise ValueError("Google Drive file ID is required")
        service = await self._get_service()

        def work() -> dict[str, Any]:
            return dict(
                service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,size,modifiedTime,webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )

        try:
            return await asyncio.to_thread(work)
        except DriveConfigurationError:
            raise
        except Exception as exc:
            try:
                from googleapiclient.errors import HttpError
            except Exception:  # pragma: no cover
                HttpError = ()  # type: ignore[assignment]
            if HttpError and isinstance(exc, HttpError):
                raise self._normalize_http_error(exc) from exc
            raise

    async def open_file_stream(self, file_id: str, *, range_header: str = "") -> tuple[Any, Any]:
        """Open an authenticated streaming response for a Drive file.

        The returned AuthorizedSession and Response must both be closed by the
        caller after iteration finishes.
        """
        if not file_id:
            raise ValueError("Google Drive file ID is required")
        credentials = await self._get_credentials()
        normalized_range = str(range_header or "").strip()
        if normalized_range and not re.fullmatch(r"bytes=\d*-\d*", normalized_range, re.IGNORECASE):
            normalized_range = ""

        def work() -> tuple[Any, Any]:
            from google.auth.transport.requests import AuthorizedSession

            session = AuthorizedSession(credentials)
            headers = {"Range": normalized_range} if normalized_range else {}
            response = session.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"alt": "media", "supportsAllDrives": "true"},
                headers=headers,
                stream=True,
                timeout=(15, 300),
            )
            if response.status_code >= 400:
                status = response.status_code
                detail = response.text[:2000]
                response.close()
                session.close()
                raise DriveConfigurationError(
                    f"Google Drive media stream failed with HTTP {status}: {detail}",
                    code="drive_stream_failed",
                    reason="streamRequestFailed",
                    status=status if 400 <= status <= 599 else 502,
                    project_id=self.credential_project_id,
                    raw_message=detail,
                )
            return session, response

        return await asyncio.to_thread(work)

    async def upload_file(
        self,
        file_path: str | Path,
        *,
        folder_id_or_url: str,
        file_name: str,
        mime_type: str,
        description: str = "",
        make_public: bool = False,
    ) -> dict[str, Any]:
        folder_id = normalize_drive_folder_id(folder_id_or_url)
        if not folder_id:
            raise ValueError("Google Drive folder ID is required")
        service = await self._get_service()
        path = Path(file_path)

        def work() -> dict[str, Any]:
            from googleapiclient.http import MediaFileUpload

            media = MediaFileUpload(
                str(path),
                mimetype=mime_type or "application/octet-stream",
                resumable=True,
                chunksize=5 * 1024 * 1024,
            )
            body = {
                "name": file_name,
                "parents": [folder_id],
                "description": description[:5000],
            }
            created = (
                service.files()
                .create(
                    body=body,
                    media_body=media,
                    fields=(
                        "id,name,mimeType,size,createdTime,modifiedTime,webViewLink,webContentLink,"
                        "thumbnailLink,iconLink,parents"
                    ),
                    supportsAllDrives=True,
                )
                .execute()
            )
            if make_public:
                service.permissions().create(
                    fileId=created["id"],
                    body={"type": "anyone", "role": "reader"},
                    supportsAllDrives=True,
                    fields="id",
                ).execute()
            created["public"] = bool(make_public)
            if make_public:
                created["publicContentUrl"] = f"https://drive.google.com/uc?id={created['id']}&export=download"
                created["publicThumbnailUrl"] = f"https://drive.google.com/thumbnail?id={created['id']}&sz=w1000"
            return created

        try:
            return await asyncio.to_thread(work)
        except DriveConfigurationError:
            raise
        except Exception as exc:
            try:
                from googleapiclient.errors import HttpError
            except Exception:  # pragma: no cover
                HttpError = ()  # type: ignore[assignment]
            if HttpError and isinstance(exc, HttpError):
                raise self._normalize_http_error(exc) from exc
            raise

    async def delete_file(self, file_id: str) -> None:
        if not file_id:
            return
        service = await self._get_service()

        def work() -> None:
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute()

        try:
            await asyncio.to_thread(work)
        except DriveConfigurationError:
            raise
        except Exception as exc:
            try:
                from googleapiclient.errors import HttpError
            except Exception:  # pragma: no cover
                HttpError = ()  # type: ignore[assignment]
            if HttpError and isinstance(exc, HttpError):
                raise self._normalize_http_error(exc) from exc
            raise


def create_drive_archive() -> GoogleDriveArchive:
    return GoogleDriveArchive()
