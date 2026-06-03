"""PR-3 Blob connector tests (azure-storage-blob client fully mocked).

Covers:
- enumerate() pagination via continuation tokens (>1 page).
- PDF allowlist (non-PDF blobs filtered out at enumerate).
- 100 MB skip (oversized blobs never yielded).
- source_path format: blob://{container}/{blob_name}.
- fetch() PDF magic-byte rejection of a non-PDF download (raises NotAPdfError).
- fetch() happy path returns a ConnectorFile with the downloaded bytes.

No live Azure — a fake ContainerClient is injected.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from connectors import MAX_FILE_BYTES, ConnectorConfig  # noqa: E402
from connectors.blob import BlobConnector  # noqa: E402
from connectors.core import NotAPdfError  # noqa: E402

# ---------------------------------------------------------------------------
# Fake azure-storage-blob ContainerClient
# ---------------------------------------------------------------------------


class _FakeBlobItem:
    def __init__(self, name, size, etag="etag-x"):
        self.name = name
        self.size = size
        self.etag = etag


class _FakeBlobList:
    """Mimics ItemPaged: `.by_page()` yields pages (lists) of blob items."""

    def __init__(self, pages):
        self._pages = pages

    def by_page(self):
        return iter(self._pages)


class _FakeDownloader:
    def __init__(self, content, fail_times=0):
        self._content = content
        self._fail_times = fail_times

    def readall(self):
        return self._content


class _FakeBlobClient:
    def __init__(self, content, fail_times=0):
        self._content = content
        self._fail_times = fail_times
        self._attempts = 0

    def download_blob(self, timeout=None):
        self._attempts += 1
        if self._attempts <= self._fail_times:
            raise TimeoutError("transient transport error")
        return _FakeDownloader(self._content)


class _FakeContainerClient:
    def __init__(self, pages, blob_contents=None):
        self._pages = pages
        self._blob_contents = blob_contents or {}
        self._fail_map = {}

    def list_blobs(self):
        return _FakeBlobList(self._pages)

    def set_fail_times(self, name, n):
        self._fail_map[name] = n

    def get_blob_client(self, name):
        return _FakeBlobClient(self._blob_contents.get(name, b"%PDF-default"), self._fail_map.get(name, 0))


def _connector(pages, blob_contents=None):
    cfg = ConnectorConfig(bot_tag="tenant-x", fr_mode="read")
    client = _FakeContainerClient(pages, blob_contents)
    conn = BlobConnector(cfg, "mycontainer", container_client=client, sleep=lambda _s: None)
    return conn, client


# ---------------------------------------------------------------------------
# enumerate
# ---------------------------------------------------------------------------


def test_enumerate_paginates_across_continuation_tokens():
    """Items from every page are visited (>1 page)."""
    pages = [
        [_FakeBlobItem("a.pdf", 100), _FakeBlobItem("b.pdf", 200)],
        [_FakeBlobItem("c.pdf", 300)],
    ]
    conn, _ = _connector(pages)
    items = list(conn.enumerate())
    names = [i.filename for i in items]
    assert names == ["a.pdf", "b.pdf", "c.pdf"]


def test_enumerate_filters_non_pdf():
    pages = [[_FakeBlobItem("doc.pdf", 100), _FakeBlobItem("notes.txt", 100), _FakeBlobItem("img.PNG", 100)]]
    conn, _ = _connector(pages)
    items = list(conn.enumerate())
    assert [i.filename for i in items] == ["doc.pdf"]


def test_enumerate_skips_oversized_blob():
    pages = [[_FakeBlobItem("small.pdf", 100), _FakeBlobItem("huge.pdf", MAX_FILE_BYTES + 1)]]
    conn, _ = _connector(pages)
    items = list(conn.enumerate())
    assert [i.filename for i in items] == ["small.pdf"]


def test_enumerate_source_path_format():
    pages = [[_FakeBlobItem("folder/sub/report.pdf", 100)]]
    conn, _ = _connector(pages)
    item = next(iter(conn.enumerate()))
    assert item.source_path == "blob://mycontainer/folder/sub/report.pdf"
    # filename is the basename; source_path keeps the full blob name.
    assert item.filename == "report.pdf"
    assert item.validator == "etag-x"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_returns_connector_file_with_bytes():
    pages = [[_FakeBlobItem("a.pdf", 8)]]
    conn, _ = _connector(pages, blob_contents={"a.pdf": b"%PDF-123"})
    item = next(iter(conn.enumerate()))
    cfile = conn.fetch(item)
    assert cfile.filename == "a.pdf"
    assert await cfile.read() == b"%PDF-123"


def test_fetch_rejects_non_pdf_magic_bytes():
    """A download whose bytes are not a real PDF raises NotAPdfError."""
    pages = [[_FakeBlobItem("masquerade.pdf", 10)]]
    conn, _ = _connector(pages, blob_contents={"masquerade.pdf": b"GIF89a-not-a-pdf"})
    item = next(iter(conn.enumerate()))
    with pytest.raises(NotAPdfError):
        conn.fetch(item)


def test_fetch_retries_transient_then_succeeds():
    pages = [[_FakeBlobItem("a.pdf", 8)]]
    conn, client = _connector(pages, blob_contents={"a.pdf": b"%PDF-ok"})
    client.set_fail_times("a.pdf", 2)  # first two attempts raise, third succeeds
    item = next(iter(conn.enumerate()))
    cfile = conn.fetch(item)
    assert cfile.filename == "a.pdf"


# ---------------------------------------------------------------------------
# auth config
# ---------------------------------------------------------------------------


def test_build_container_client_requires_credentials(monkeypatch):
    """With no BLOB_ACCOUNT_URL and no connection string, init fails fast."""
    from connectors.core import ConnectorError

    monkeypatch.delenv("BLOB_ACCOUNT_URL", raising=False)
    monkeypatch.delenv("BLOB_STORAGE_CONNECTION_STRING", raising=False)
    cfg = ConnectorConfig(bot_tag="tenant-x")
    with pytest.raises(ConnectorError):
        BlobConnector(cfg, "mycontainer")
