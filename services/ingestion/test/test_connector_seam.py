"""PR-1 seam tests: source_type threading + delete_by_source_path.

Covers the keystone seam change in custom_rag.py:
- Existing callers (no source_type) still stamp source_type='upload' on every
  chunk AND in the ingestion_started event (back-compat).
- An explicit source_type threads through to every chunk dict and the event.
- delete_by_source_path removes prior chunks, is bot_tag-scoped, paginates
  beyond the 1000 per-page cap, escapes OData, and deletes in <=1000 batches.

Hermetic — no network, no real Azure SDK.
"""

import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com/")
os.environ.setdefault("AZURE_OPENAI_KEY", "test-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-02-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://test.search.windows.net")
os.environ.setdefault("AZURE_SEARCH_KEY", "test-key")
os.environ.setdefault("INDEX_NAME", "test-index")
os.environ.setdefault("DOC_INTELLIGENCE_ENDPOINT", "https://test.cognitiveservices.azure.com/")
os.environ.setdefault("DOC_INTELLIGENCE_KEY", "test-key")

import custom_rag  # noqa: E402
from custom_rag import rag  # noqa: E402


class _FakeUploadFile:
    def __init__(self, content: bytes, filename: str = "doc.pdf"):
        self._content = content
        self.filename = filename

    async def read(self) -> bytes:
        return self._content


class _FakeSearchClient:
    """Captures merge_or_upload + records search/delete calls."""

    def __init__(self):
        self.uploaded = None

    def search(self, **kwargs):
        return []  # no stale chunks for upload()'s document_id delete

    def delete_documents(self, documents):
        return None

    def merge_or_upload_documents(self, documents):
        self.uploaded = documents
        return [MagicMock(succeeded=True) for _ in documents]


def _patched_rag(monkeypatch, *, content):
    instance = rag()
    fake_search = _FakeSearchClient()
    monkeypatch.setattr(instance, "create_search_index", AsyncMock(return_value=fake_search))
    monkeypatch.setattr(instance, "get_embedding", AsyncMock(return_value=[0.1] * 3))
    monkeypatch.setattr(instance, "chunk_token", AsyncMock(return_value=42))

    fake_doc = MagicMock()
    fake_doc.page_count = 2
    monkeypatch.setattr(custom_rag.fitz, "open", MagicMock(return_value=fake_doc))

    fake_loaded = MagicMock()
    fake_loaded.page_content = content
    fake_loader = MagicMock()
    fake_loader.load.return_value = [fake_loaded]
    monkeypatch.setattr(
        custom_rag,
        "AzureAIDocumentIntelligenceLoader",
        MagicMock(return_value=fake_loader),
    )
    return instance, fake_search


def _events_by_name(caplog) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rec in caplog.records:
        msg = rec.getMessage()
        if not msg.startswith("{"):
            continue
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and "event" in payload:
            out[payload["event"]] = payload
    return out


# ---------------------------------------------------------------------------
# source_type threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_caller_default_source_type_is_upload(monkeypatch, caplog):
    """A caller that passes no source_type must still write 'upload' everywhere."""
    instance, fake_search = _patched_rag(monkeypatch, content="# H\n\nsome text " * 20)

    with caplog.at_level(logging.INFO):
        await instance.upload(
            _FakeUploadFile(b"%PDF-fake"), "tenant-x", "read", "/srv/doc.pdf", request_id="r1"
        )

    assert fake_search.uploaded, "expected chunks to be uploaded"
    for chunk in fake_search.uploaded:
        assert chunk["source_type"] == "upload"
    assert _events_by_name(caplog)["ingestion_started"]["source_type"] == "upload"


@pytest.mark.asyncio
async def test_explicit_source_type_threads_through(monkeypatch, caplog):
    """An explicit source_type must reach every chunk dict and the event."""
    instance, fake_search = _patched_rag(monkeypatch, content="# H\n\nsome text " * 20)

    with caplog.at_level(logging.INFO):
        await instance.upload(
            _FakeUploadFile(b"%PDF-fake"),
            "tenant-x",
            "read",
            "blob://c/doc.pdf",
            source_type="blob",
            request_id="r2",
        )

    assert fake_search.uploaded
    for chunk in fake_search.uploaded:
        assert chunk["source_type"] == "blob"
        assert chunk["source_path"] == "blob://c/doc.pdf"
        # bot_tag is the leading id segment — P0-4 isolation preserved.
        assert chunk["id"].startswith("tenant-x_")
        assert chunk["bot_tag"] == "tenant-x"
    assert _events_by_name(caplog)["ingestion_started"]["source_type"] == "blob"


# ---------------------------------------------------------------------------
# delete_by_source_path
# ---------------------------------------------------------------------------


class _PagedSearchClient:
    """Fake SearchClient whose search().by_page() yields fixed pages and records
    every deleted id and the filter it was queried with."""

    def __init__(self, pages):
        self._pages = pages
        self.last_filter = None
        self.deleted_batches = []

    def search(self, *, search_text, filter, select, **kwargs):
        self.last_filter = filter

        class _Result:
            def __init__(self, pages):
                self._pages = pages

            def by_page(self):
                return iter(self._pages)

        return _Result(self._pages)

    def delete_documents(self, documents):
        self.deleted_batches.append([d["id"] for d in documents])
        return [MagicMock(succeeded=True) for _ in documents]


@pytest.mark.asyncio
async def test_delete_by_source_path_removes_prior_chunks(monkeypatch):
    """Deletes exactly the matching chunk ids and returns the count."""
    instance = rag()
    client = _PagedSearchClient(
        pages=[[{"id": "tenant-x_aaa_read_00000"}, {"id": "tenant-x_aaa_read_00001"}]]
    )
    monkeypatch.setattr(instance, "create_search_index", AsyncMock(return_value=client))

    deleted = await instance.delete_by_source_path("blob://c/doc.pdf", "tenant-x")

    assert deleted == 2
    assert client.deleted_batches == [["tenant-x_aaa_read_00000", "tenant-x_aaa_read_00001"]]


@pytest.mark.asyncio
async def test_delete_by_source_path_is_bot_tag_scoped_and_escapes_odata(monkeypatch):
    """The filter constrains BOTH source_path and bot_tag, with OData escaping."""
    instance = rag()
    client = _PagedSearchClient(pages=[[]])
    monkeypatch.setattr(instance, "create_search_index", AsyncMock(return_value=client))

    await instance.delete_by_source_path("blob://c/o'brien.pdf", "ten'ant")

    assert "bot_tag eq 'ten''ant'" in client.last_filter
    assert "source_path eq 'blob://c/o''brien.pdf'" in client.last_filter


@pytest.mark.asyncio
async def test_delete_by_source_path_paginates_beyond_1000(monkeypatch):
    """Walks every page (>1000 ids) and deletes in batches of <=1000."""
    instance = rag()
    # 2300 ids spread across 3 pages → must produce 3 delete batches: 1000,1000,300.
    page1 = [{"id": f"t_{i:05d}"} for i in range(1000)]
    page2 = [{"id": f"t_{i:05d}"} for i in range(1000, 2000)]
    page3 = [{"id": f"t_{i:05d}"} for i in range(2000, 2300)]
    client = _PagedSearchClient(pages=[page1, page2, page3])
    monkeypatch.setattr(instance, "create_search_index", AsyncMock(return_value=client))

    deleted = await instance.delete_by_source_path("blob://c/big.pdf", "tenant-x")

    assert deleted == 2300
    assert [len(b) for b in client.deleted_batches] == [1000, 1000, 300]
    # No batch exceeds the Azure 1000-action cap.
    assert all(len(b) <= 1000 for b in client.deleted_batches)
