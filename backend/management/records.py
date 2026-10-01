import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..google_drive import DriveConfigurationError
from . import runtime
from .auth import require_staff

log = logging.getLogger("conan.management")
router = APIRouter()


@router.get("/api/v1/media/{guild_id}")
async def get_media(
    guild_id: str,
    limit: int = 100,
    media_type: str = "",
    channel_id: str = "",
    _: None = Depends(require_staff),
) -> dict[str, Any]:
    normalized_type = media_type if media_type in {"image", "video"} else ""
    records = await runtime.store.list_media_records(
        guild_id,
        limit=max(1, min(limit, 250)),
        media_type=normalized_type,
        channel_id=str(channel_id or ""),
    )
    config = await runtime.store.get_config(guild_id)
    media_config = config.get("media", {})
    return {
        "guildId": guild_id,
        "items": records,
        "stats": await runtime.store.media_stats(guild_id),
        "drive": runtime.drive_archive.status_payload(
            folder_id=str(media_config.get("googleDriveFolderId") or "")
        ),
    }


@router.post("/api/v1/media/{guild_id}/test-drive")
async def test_media_drive(
    guild_id: str, _: None = Depends(require_staff)
) -> dict[str, Any]:
    config = await runtime.store.get_config(guild_id)
    folder_id = str(config.get("media", {}).get("googleDriveFolderId") or "").strip()
    if not folder_id:
        raise HTTPException(
            status_code=400, detail="Set the Google Drive folder ID first"
        )
    try:
        folder = await runtime.drive_archive.test_folder(folder_id)
    except DriveConfigurationError as exc:
        log.warning("Google Drive folder test needs configuration: %s", exc)
        detail = exc.as_dict(
            service_account_email=runtime.drive_archive.service_account_email,
            principal_email=runtime.drive_archive.principal_email,
            credential_project_id=runtime.drive_archive.credential_project_id,
            auth_mode=runtime.drive_archive.auth_mode,
        )
        detail.update(runtime.drive_archive.status_payload(folder_id=folder_id))
        raise HTTPException(status_code=exc.status, detail=detail) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Google Drive folder test failed")
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Google Drive error: {type(exc).__name__}: {exc}",
                "code": "drive_request_failed",
                **runtime.drive_archive.status_payload(folder_id=folder_id),
            },
        ) from exc
    await runtime.store.add_log(
        guild_id,
        "media.drive_tested",
        {"folderId": folder.get("id"), "folderName": folder.get("name")},
    )
    return {
        "ok": True,
        "folder": folder,
        **runtime.drive_archive.status_payload(folder_id=folder_id),
    }


@router.delete("/api/v1/media/{guild_id}/{record_id}")
async def delete_media(
    guild_id: str,
    record_id: str,
    delete_drive_file: bool = True,
    _: None = Depends(require_staff),
) -> dict[str, Any]:
    record = await runtime.store.get_media_record(guild_id, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Media record not found")
    if delete_drive_file and record.get("driveFileId"):
        try:
            await runtime.drive_archive.delete_file(str(record["driveFileId"]))
        except Exception as exc:
            log.exception("Could not delete Google Drive media file")
            raise HTTPException(
                status_code=502,
                detail=f"Drive deletion failed: {type(exc).__name__}: {exc}",
            ) from exc
    removed = await runtime.store.delete_media_record(guild_id, record_id)
    await runtime.store.add_log(
        guild_id,
        "media.deleted",
        {
            "recordId": record_id,
            "driveFileId": record.get("driveFileId"),
            "driveFileDeleted": bool(delete_drive_file),
            "source": "dashboard",
        },
    )
    return {"ok": True, "record": removed, "driveFileDeleted": bool(delete_drive_file)}


@router.get("/api/v1/logs/{guild_id}")
async def get_logs(
    guild_id: str, limit: int = 80, _: None = Depends(require_staff)
) -> dict[str, Any]:
    return {
        "guildId": guild_id,
        "logs": await runtime.store.list_logs(guild_id, limit=limit),
    }
