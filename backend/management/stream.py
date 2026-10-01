import logging
import mimetypes
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..google_drive import DriveConfigurationError
from ..media_stream import normalize_stream_filename, validate_drive_stream_signature
from . import runtime

log = logging.getLogger("conan.management")
router = APIRouter()


@router.api_route(
    "/media/drive/{file_id}/{filename}", methods=["GET", "HEAD"], response_model=None
)
async def stream_drive_media(
    file_id: str,
    filename: str,
    request: Request,
    sig: str = "",
    expires: int = 0,
    disposition: str = "inline",
):
    """Proxy a private Drive file through a signed URL with byte-range support.

    Discord can render this endpoint as a native image/video preview even when
    the source file is larger than the guild's attachment limit.
    """
    normalized_name = normalize_stream_filename(filename)
    if not validate_drive_stream_signature(
        file_id, normalized_name, sig, expires, disposition
    ):
        raise HTTPException(status_code=403, detail="Invalid media stream signature")
    if not runtime.drive_archive.configured:
        raise HTTPException(status_code=503, detail="Google Drive is not configured")
    try:
        metadata = await runtime.drive_archive.get_file_metadata(file_id)
    except DriveConfigurationError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    actual_name = normalize_stream_filename(
        str(metadata.get("name") or normalized_name)
    )
    mime_type = str(metadata.get("mimeType") or "").split(";", 1)[0].strip().lower()
    if not mime_type or mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(actual_name)[0] or "application/octet-stream"
    if mime_type not in {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "video/mp4",
        "video/webm",
    }:
        raise HTTPException(415, "Unsupported media type")
    try:
        total_size = max(0, int(metadata.get("size") or 0))
    except (TypeError, ValueError):
        total_size = 0
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(actual_name, safe='')}",
        "X-Content-Type-Options": "nosniff",
    }
    if request.method == "HEAD":
        if total_size:
            base_headers["Content-Length"] = str(total_size)
        return Response(status_code=200, media_type=mime_type, headers=base_headers)
    range_header = str(request.headers.get("range") or "").strip()
    if range_header and not re.fullmatch(
        r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", range_header
    ):
        raise HTTPException(416, "Invalid byte range")
    try:
        session, drive_response = await runtime.drive_archive.open_file_stream(
            file_id, range_header=range_header
        )
    except DriveConfigurationError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    for source_name, target_name in (
        ("content-length", "Content-Length"),
        ("content-range", "Content-Range"),
        ("accept-ranges", "Accept-Ranges"),
        ("etag", "ETag"),
        ("last-modified", "Last-Modified"),
    ):
        value = drive_response.headers.get(source_name)
        if value:
            base_headers[target_name] = value

    def body_iterator():
        try:
            for chunk in drive_response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            drive_response.close()
            session.close()

    return StreamingResponse(
        body_iterator(),
        status_code=drive_response.status_code,
        media_type=mime_type,
        headers=base_headers,
    )
