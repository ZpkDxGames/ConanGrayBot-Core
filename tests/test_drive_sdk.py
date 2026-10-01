import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from googleapiclient.errors import HttpError

from backend import google_drive as drive
from backend.config import get_settings


@pytest.fixture(autouse=True)
def fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "name,mime,kind",
    [
        ("picture.PNG", "image/png", "image"),
        ("clip.mp4", "video/mp4", "video"),
        ("document.txt", "text/plain", None),
        ("image", "image/webp", "image"),
    ],
)
def test_media_format_allowlist(name, mime, kind):
    assert drive.drive_media_type({"name": name, "mimeType": mime}) == kind


@pytest.mark.parametrize(
    "url",
    [
        "fixture-folder-123",
        "https://drive.google.com/drive/folders/fixture-folder-123",
        "https://drive.google.com/open?id=fixture-folder-123",
    ],
)
def test_normalized_folder_reference(url):
    assert drive.normalize_drive_folder_id(url) == "fixture-folder-123"


@pytest.mark.parametrize(
    "status,reason,code",
    [
        (403, "accessNotConfigured", "drive_api_disabled"),
        (404, "notFound", "folder_not_found"),
        (403, "forbidden", "folder_permission_denied"),
        (403, "storageQuotaExceeded", "drive_storage_unavailable"),
        (429, "rateLimitExceeded", "drive_rate_limited"),
        (500, "backendError", "drive_request_failed"),
    ],
)
def test_error_translation_does_not_return_raw_response(status, reason, code):
    error = HttpError(
        SimpleNamespace(status=status, reason="fixture"),
        json.dumps(
            {"error": {"message": "private response", "errors": [{"reason": reason}]}}
        ).encode(),
    )
    result = drive.normalize_drive_error(error, credential_project_id="fixture-project")
    assert result.code == code
    assert "private response" not in json.dumps(result.as_dict())


@pytest.mark.parametrize("encoded", [False, True])
def test_separate_credentials_can_be_supplied_privately(monkeypatch, encoded):
    value = json.dumps(
        {"client_email": "fixture@example.test", "project_id": "fixture-project"}
    )
    if encoded:
        value = base64.b64encode(value.encode()).decode()
    monkeypatch.setenv("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", value)
    assert drive.load_drive_service_account()["project_id"] == "fixture-project"


@pytest.fixture
def archive(monkeypatch):
    monkeypatch.setenv("GOOGLE_DRIVE_AUTH_MODE", "oauth_user")
    monkeypatch.setenv("GOOGLE_DRIVE_OAUTH_PROJECT_ID", "fixture-project")
    monkeypatch.setenv("GOOGLE_DRIVE_OAUTH_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("GOOGLE_DRIVE_OAUTH_CLIENT_SECRET", "fixture-secret")
    monkeypatch.setenv("GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN", "fixture-refresh")
    get_settings.cache_clear()
    archive = drive.GoogleDriveArchive()
    yield archive
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_missing_auth_fails_before_cloud_requests(monkeypatch):
    monkeypatch.setattr(drive, "load_drive_service_account", lambda: None)
    archive = drive.GoogleDriveArchive()
    archive.auth_mode = "service_account"
    assert not archive.configured
    with pytest.raises(drive.DriveConfigurationError):
        await archive._get_credentials()


def test_oauth_credentials_and_safe_status(archive):
    assert archive.configured
    credentials = archive._build_credentials_sync()
    assert credentials.refresh_token == "fixture-refresh"
    assert "fixture-secret" not in json.dumps(archive.status_payload())
    assert "fixture-refresh" not in json.dumps(archive.status_payload())


@pytest.mark.asyncio
async def test_list_filters_pages_and_respects_limit(archive, monkeypatch):
    service = MagicMock()
    service.files.return_value.list.return_value.execute.side_effect = [
        {
            "files": [{"id": "a", "name": "a.png"}, {"id": "bad", "name": "a.txt"}],
            "nextPageToken": "next",
        },
        {"files": [{"id": "b", "name": "b.png"}]},
    ]
    monkeypatch.setattr(archive, "_get_service", AsyncMock(return_value=service))
    rows = await archive.list_media_files(
        "fixture-folder-123", media_type="image", limit=2
    )
    assert [row["id"] for row in rows] == ["a", "b"]
    assert service.files.return_value.list.call_count == 2
    with pytest.raises(ValueError):
        await archive.list_media_files("", media_type="image")
    with pytest.raises(ValueError):
        await archive.list_media_files("fixture-folder-123", media_type="html")


@pytest.mark.asyncio
async def test_metadata_delete_and_validation(archive, monkeypatch):
    service = MagicMock()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "a",
        "name": "picture.png",
        "size": "10",
    }
    monkeypatch.setattr(archive, "_get_service", AsyncMock(return_value=service))
    assert (await archive.get_file_metadata("a"))["name"] == "picture.png"
    await archive.delete_file("a")
    service.files.return_value.delete.assert_called_once_with(
        fileId="a", supportsAllDrives=True
    )
    with pytest.raises(ValueError):
        await archive.get_file_metadata("")


@pytest.mark.asyncio
async def test_permission_failure_compensates_created_file(
    archive, monkeypatch, tmp_path
):
    path = tmp_path / "media.png"
    path.write_bytes(b"fixture")
    service = MagicMock()
    service.files.return_value.create.return_value.execute.return_value = {
        "id": "new-file"
    }
    service.permissions.return_value.create.return_value.execute.side_effect = (
        RuntimeError("permission unavailable")
    )
    monkeypatch.setattr(archive, "_get_service", AsyncMock(return_value=service))
    with pytest.raises(RuntimeError):
        await archive.upload_file(
            path,
            folder_id_or_url="fixture-folder-123",
            file_name="media.png",
            mime_type="image/png",
            make_public=True,
        )
    service.files.return_value.delete.assert_called_once_with(
        fileId="new-file", supportsAllDrives=True
    )


@pytest.mark.asyncio
async def test_stream_transport_failure_closes_authorized_session(archive, monkeypatch):
    monkeypatch.setattr(archive, "_get_credentials", AsyncMock(return_value=object()))
    session = MagicMock()
    session.get.side_effect = RuntimeError("transport")
    monkeypatch.setattr(
        "google.auth.transport.requests.AuthorizedSession", lambda credentials: session
    )
    with pytest.raises(RuntimeError):
        await archive.open_file_stream("file")
    session.close.assert_called_once()


@pytest.mark.asyncio
async def test_stream_http_failure_closes_response_and_session(archive, monkeypatch):
    monkeypatch.setattr(archive, "_get_credentials", AsyncMock(return_value=object()))
    response = MagicMock(status_code=403, text="private response")
    session = MagicMock()
    session.get.return_value = response
    monkeypatch.setattr(
        "google.auth.transport.requests.AuthorizedSession", lambda credentials: session
    )
    with pytest.raises(drive.DriveConfigurationError) as error:
        await archive.open_file_stream("file", range_header="bytes=0-9")
    assert "private response" not in str(error.value)
    session.close.assert_called_once()
    response.close.assert_called_once()


@pytest.mark.asyncio
async def test_close_releases_cached_sdk_transport(archive):
    service = MagicMock()
    archive._service = service
    await archive.close()
    service._http.close.assert_called_once()
    assert archive._service is None
    await archive.close()
    service._http.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_upload_waits_for_worker_and_compensates(
    archive, monkeypatch, tmp_path
):
    import asyncio
    import threading

    started, finish = threading.Event(), threading.Event()
    service = MagicMock()

    def create():
        started.set()
        assert finish.wait(3)
        return {"id": "cancelled-file"}

    service.files.return_value.create.return_value.execute.side_effect = create
    monkeypatch.setattr(archive, "_get_service", AsyncMock(return_value=service))
    cleanup = AsyncMock()
    monkeypatch.setattr(archive, "delete_file", cleanup)
    path = tmp_path / "media.png"
    path.write_bytes(b"fixture")
    task = asyncio.create_task(
        archive.upload_file(
            path,
            folder_id_or_url="fixture-folder-123",
            file_name="media.png",
            mime_type="image/png",
        )
    )
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    cleanup.assert_awaited_once_with("cancelled-file")
