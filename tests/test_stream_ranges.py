from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest

from backend.management import runtime
from backend.media_stream import build_drive_stream_url
from tests.test_security_contract import client as client
from tests.test_security_contract import settings as settings


@pytest.fixture
def stream_url(settings, monkeypatch):
    response = SimpleNamespace(
        headers={"content-length": "10", "content-range": "bytes 0-9/10"},
        status_code=206,
        iter_content=lambda **kwargs: iter([b"", b"0123456789"]),
        close=MagicMock(),
    )
    session = SimpleNamespace(close=MagicMock())
    archive = SimpleNamespace(
        configured=True,
        get_file_metadata=AsyncMock(
            return_value={"name": "picture.png", "mimeType": "image/png", "size": "10"}
        ),
        open_file_stream=AsyncMock(return_value=(session, response)),
    )
    monkeypatch.setattr(runtime, "drive_archive", archive)
    url = urlsplit(build_drive_stream_url("fixture-file", "picture.png"))
    return url.path + "?" + url.query, archive, session, response


@pytest.mark.parametrize(
    "value", ["bytes=9-1", "bytes=10-", "bytes=-0", "bytes=0-1,4-5", "invalid"]
)
def test_unsatisfiable_range_never_opens_upstream(client, stream_url, value):
    path, archive, _, _ = stream_url
    result = client.get(path, headers={"Range": value})
    assert result.status_code == 416
    archive.open_file_stream.assert_not_awaited()


def test_valid_range_clamps_end_and_closes_resources(client, stream_url):
    path, archive, session, response = stream_url
    result = client.get(path, headers={"Range": "bytes=0-999"})
    assert result.status_code == 206 and result.content == b"0123456789"
    archive.open_file_stream.assert_awaited_once_with(
        "fixture-file", range_header="bytes=0-9"
    )
    response.close.assert_called_once()
    session.close.assert_called_once()


def test_head_uses_only_safe_metadata(client, stream_url):
    path, archive, _, _ = stream_url
    result = client.head(path)
    assert (
        result.status_code == 200
        and result.headers["content-length"] == "10"
        and not result.content
    )
    archive.open_file_stream.assert_not_awaited()


def test_html_media_cannot_be_served(client, stream_url):
    path, archive, _, _ = stream_url
    archive.get_file_metadata.return_value = {
        "name": "page.html",
        "mimeType": "text/html",
        "size": "10",
    }
    assert client.get(path).status_code == 415
    archive.open_file_stream.assert_not_awaited()
