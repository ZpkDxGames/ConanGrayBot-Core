import hashlib
import hmac
import re
import time
from urllib.parse import quote, urlencode

from .config import get_settings


def normalize_stream_filename(value: str) -> str:
    return (
        re.sub(
            r"[\x00-\x1f\x7f]", "", str(value).replace("\\", "/").split("/")[-1]
        ).strip()
        or "media"
    )[:220]


def drive_stream_signature(
    file_id: str, filename: str, expires: int, disposition: str = "inline"
) -> str:
    secret = get_settings().media_stream_signing_key
    if not secret or disposition not in {"inline", "attachment"}:
        return ""
    message = f"conan-media-v1\n{file_id}\n{normalize_stream_filename(filename)}\n{expires}\n{disposition}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def build_drive_stream_url(
    file_id: str, filename: str, disposition: str = "inline"
) -> str:
    settings = get_settings()
    expires = int(time.time()) + settings.media_stream_ttl_seconds
    signature = drive_stream_signature(file_id, filename, expires, disposition)
    if not file_id or not settings.public_base_url or not signature:
        return ""
    return (
        f"{settings.public_base_url.rstrip('/')}/media/drive/{quote(file_id, safe='')}/{quote(normalize_stream_filename(filename), safe='')}?"
        + urlencode({"expires": expires, "disposition": disposition, "sig": signature})
    )


def validate_drive_stream_signature(
    file_id: str,
    filename: str,
    signature: str,
    expires: int = 0,
    disposition: str = "inline",
) -> bool:
    now = int(time.time())
    if (
        not now <= expires <= now + get_settings().media_stream_ttl_seconds
        or disposition not in {"inline", "attachment"}
    ):
        return False
    expected = drive_stream_signature(file_id, filename, expires, disposition)
    return bool(expected and hmac.compare_digest(expected, signature))
