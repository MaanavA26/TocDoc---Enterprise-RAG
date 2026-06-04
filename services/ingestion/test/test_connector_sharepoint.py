"""PR-4 SharePoint connector tests (Microsoft Graph HTTP fully mocked).

Covers:
- enumerate() follows @odata.nextLink across >1 page (asserts ALL items).
- PDF allowlist (non-PDF and folder entries filtered out at enumerate).
- 100 MB skip (oversized items never yielded).
- source_path format: sharepoint://{site_id}/{drive_id}/{item_id} (opaque ids only).
- fetch() PDF magic-byte rejection of a non-PDF download (raises NotAPdfError).
- fetch() happy path returns a ConnectorFile with the downloaded bytes.
- 429 Retry-After is honored: backoff sleeps for the header value, then retries.
- credential presence validated at init (missing env → ConnectorError).

No live Azure/Graph — an httpx.Client backed by a MockTransport is injected, so
ClientSecretCredential is never constructed.
"""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from connectors import MAX_FILE_BYTES, ConnectorConfig, ConnectorError  # noqa: E402
from connectors.core import NotAPdfError  # noqa: E402
from connectors.sharepoint import SharePointConnector  # noqa: E402

SITE_ID = "site-1"
DRIVE_ID = "drive-1"


# ---------------------------------------------------------------------------
# Recording sleep — lets tests assert a backoff occurred and with what delay.
# ---------------------------------------------------------------------------


class _RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _file_entry(item_id, name, size, ctag="ctag-x"):
    return {"id": item_id, "name": name, "size": size, "file": {}, "cTag": ctag}


def _connector_with_handler(handler, sleep=None):
    """Build a SharePointConnector backed by a MockTransport handler."""
    sleep = sleep or _RecordingSleep()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = ConnectorConfig(bot_tag="tenant-x", fr_mode="read")
    conn = SharePointConnector(cfg, SITE_ID, DRIVE_ID, http_client=client, sleep=sleep)
    return conn, sleep


# ---------------------------------------------------------------------------
# enumerate — pagination
# ---------------------------------------------------------------------------


def test_enumerate_follows_nextlink_across_pages():
    """Items from EVERY page are visited (>1 page via @odata.nextLink)."""
    page2_url = "https://graph.microsoft.com/v1.0/drives/drive-1/root/children?$skiptoken=PAGE2"

    def handler(request: httpx.Request) -> httpx.Response:
        if "skiptoken=PAGE2" in str(request.url):
            return httpx.Response(
                200,
                json={"value": [_file_entry("3", "c.pdf", 300)]},
            )
        # First page carries the nextLink.
        return httpx.Response(
            200,
            json={
                "value": [
                    _file_entry("1", "a.pdf", 100),
                    _file_entry("2", "b.pdf", 200),
                ],
                "@odata.nextLink": page2_url,
            },
        )

    conn, _ = _connector_with_handler(handler)
    items = list(conn.enumerate())
    assert [i.filename for i in items] == ["a.pdf", "b.pdf", "c.pdf"]
    assert [i.identity for i in items] == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# enumerate — filtering
# ---------------------------------------------------------------------------


def test_enumerate_filters_non_pdf_and_folders():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    _file_entry("1", "doc.pdf", 100),
                    _file_entry("2", "notes.txt", 100),
                    # A folder entry: no `file` facet, has `folder` instead.
                    {"id": "3", "name": "subfolder", "folder": {"childCount": 2}},
                ]
            },
        )

    conn, _ = _connector_with_handler(handler)
    items = list(conn.enumerate())
    assert [i.filename for i in items] == ["doc.pdf"]


def test_enumerate_skips_oversized_item():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    _file_entry("1", "small.pdf", 100),
                    _file_entry("2", "huge.pdf", MAX_FILE_BYTES + 1),
                ]
            },
        )

    conn, _ = _connector_with_handler(handler)
    items = list(conn.enumerate())
    assert [i.filename for i in items] == ["small.pdf"]


def test_enumerate_source_path_format_uses_opaque_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [_file_entry("item-42", "report.pdf", 100)]})

    conn, _ = _connector_with_handler(handler)
    item = next(iter(conn.enumerate()))
    assert item.source_path == "sharepoint://site-1/drive-1/item-42"
    assert item.filename == "report.pdf"
    assert item.validator == "ctag-x"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_returns_connector_file_with_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/content"):
            return httpx.Response(200, content=b"%PDF-123")
        return httpx.Response(200, json={"value": [_file_entry("1", "a.pdf", 8)]})

    conn, _ = _connector_with_handler(handler)
    item = next(iter(conn.enumerate()))
    cfile = conn.fetch(item)
    assert cfile.filename == "a.pdf"
    assert await cfile.read() == b"%PDF-123"


def test_fetch_rejects_non_pdf_magic_bytes():
    """A download whose bytes are not a real PDF raises NotAPdfError."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/content"):
            return httpx.Response(200, content=b"GIF89a-not-a-pdf")
        return httpx.Response(200, json={"value": [_file_entry("1", "masquerade.pdf", 16)]})

    conn, _ = _connector_with_handler(handler)
    item = next(iter(conn.enumerate()))
    with pytest.raises(NotAPdfError):
        conn.fetch(item)


# ---------------------------------------------------------------------------
# 429 throttling — Retry-After honored
# ---------------------------------------------------------------------------


def test_enumerate_honors_429_retry_after():
    """A 429 with Retry-After: 7 sleeps 7s, then the retry succeeds."""
    state = {"hits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["hits"] += 1
        if state["hits"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={"value": [_file_entry("1", "a.pdf", 100)]})

    sleep = _RecordingSleep()
    conn, _ = _connector_with_handler(handler, sleep=sleep)
    items = list(conn.enumerate())
    assert [i.filename for i in items] == ["a.pdf"]
    # The 429 caused exactly one backoff, honoring the Retry-After header value.
    assert sleep.calls == [7.0]
    assert state["hits"] == 2


def test_fetch_honors_429_retry_after():
    """A 429 on the content download honors Retry-After before retrying."""
    state = {"content_hits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/content"):
            state["content_hits"] += 1
            if state["content_hits"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, json={})
            return httpx.Response(200, content=b"%PDF-ok")
        return httpx.Response(200, json={"value": [_file_entry("1", "a.pdf", 8)]})

    sleep = _RecordingSleep()
    conn, _ = _connector_with_handler(handler, sleep=sleep)
    item = next(iter(conn.enumerate()))
    cfile = conn.fetch(item)
    assert cfile.filename == "a.pdf"
    assert sleep.calls == [3.0]
    assert state["content_hits"] == 2


# ---------------------------------------------------------------------------
# auth / config
# ---------------------------------------------------------------------------


def test_build_client_requires_credentials(monkeypatch):
    """Missing Graph credentials → fail fast at init (no network call)."""
    monkeypatch.delenv("SHAREPOINT_TENANT_ID", raising=False)
    monkeypatch.delenv("SHAREPOINT_CLIENT_ID", raising=False)
    monkeypatch.delenv("SHAREPOINT_CLIENT_SECRET", raising=False)
    cfg = ConnectorConfig(bot_tag="tenant-x")
    with pytest.raises(ConnectorError):
        SharePointConnector(cfg, SITE_ID, DRIVE_ID)


def test_requires_site_and_drive():
    cfg = ConnectorConfig(bot_tag="tenant-x")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ConnectorError):
        SharePointConnector(cfg, "", DRIVE_ID, http_client=client)
    with pytest.raises(ConnectorError):
        SharePointConnector(cfg, SITE_ID, "", http_client=client)
