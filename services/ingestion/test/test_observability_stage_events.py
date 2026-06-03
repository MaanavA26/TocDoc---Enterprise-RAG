"""Stage-level observability event tests for the ingestion service (P1-1).

Drives `rag.upload(...)` end-to-end with every Azure / Document-Intelligence
call mocked, and asserts the stage events fire with the spec's field names:

- `ingestion_started`, `document_parsed`, `chunking_completed`,
  `embeddings_completed`, `index_upsert_completed`
- `ingestion_failed` on error, carrying the precise `stage` and a
  `safe_message` that contains NO raw exception text.
- The threaded `request_id` is reused across all stage events.
- Raw document content is never written into the structured events.

All tests are hermetic — no network, no real Azure SDK calls.
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

# Sentinel that must NEVER appear in any structured stage event.
_SECRET_CONTENT = "TOP-SECRET-DOCUMENT-BODY-DO-NOT-LOG"


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


class _FakeUploadFile:
    def __init__(self, content: bytes, filename: str = "doc.pdf"):
        self._content = content
        self.filename = filename

    async def read(self) -> bytes:
        return self._content


class _FakeSearchClient:
    def __init__(self):
        self.uploaded = None

    def search(self, **kwargs):
        return []  # no stale chunks

    def delete_documents(self, documents):
        return None

    def merge_or_upload_documents(self, documents):
        self.uploaded = documents
        return [MagicMock(succeeded=True) for _ in documents]


def _patched_rag(monkeypatch, *, fr_mode_content):
    """Build a rag() with Document-Intelligence + fitz + embeddings mocked."""
    instance = rag()
    fake_search = _FakeSearchClient()

    # create_search_index → our fake client
    monkeypatch.setattr(instance, "create_search_index", AsyncMock(return_value=fake_search))
    # embeddings + token counting → deterministic, no network
    monkeypatch.setattr(instance, "get_embedding", AsyncMock(return_value=[0.1] * 3))
    monkeypatch.setattr(instance, "chunk_token", AsyncMock(return_value=42))

    # fitz.open(...).page_count = 3
    fake_doc = MagicMock()
    fake_doc.page_count = 3
    monkeypatch.setattr(custom_rag.fitz, "open", MagicMock(return_value=fake_doc))

    # Document Intelligence loader → one doc whose content carries the secret.
    fake_loaded = MagicMock()
    fake_loaded.page_content = fr_mode_content
    fake_loader = MagicMock()
    fake_loader.load.return_value = [fake_loaded]
    monkeypatch.setattr(
        custom_rag,
        "AzureAIDocumentIntelligenceLoader",
        MagicMock(return_value=fake_loader),
    )
    return instance, fake_search


# ===========================================================================
# Happy-path stage events
# ===========================================================================
@pytest.mark.asyncio
async def test_read_mode_emits_all_stage_events(monkeypatch, caplog):
    content = f"# Heading\n\n{_SECRET_CONTENT} " * 50
    instance, _ = _patched_rag(monkeypatch, fr_mode_content=content)

    with caplog.at_level(logging.INFO):
        await instance.upload(
            _FakeUploadFile(b"%PDF-fake-bytes"),
            "tenant-x",
            "read",
            "/srv/files/doc.pdf",
            request_id="ing-req-1",
        )

    events = _events_by_name(caplog)
    for name in (
        "ingestion_started",
        "document_parsed",
        "chunking_completed",
        "embeddings_completed",
        "index_upsert_completed",
    ):
        assert name in events, f"missing event {name}"

    started = events["ingestion_started"]
    assert started["bot_tag"] == "tenant-x"
    assert started["fr_mode"] == "read"
    assert started["source_type"] == "upload"
    assert started["source_path"] == "/srv/files/doc.pdf"

    parsed = events["document_parsed"]
    assert parsed["parser"] == "azure_document_intelligence"
    assert parsed["page_count"] == 3
    assert "latency_ms" in parsed
    assert "content_length_chars" in parsed

    chunking = events["chunking_completed"]
    assert chunking["chunking_mode"] == "token"
    assert chunking["max_tokens"] == 500
    assert chunking["overlap_tokens"] == 50
    assert chunking["chunk_count"] >= 1

    embeddings = events["embeddings_completed"]
    assert embeddings["embedding_model"] == "text-embedding-3-small"
    assert embeddings["embedding_count"] >= 1

    upsert = events["index_upsert_completed"]
    assert upsert["bot_tag"] == "tenant-x"
    assert "document_id" in upsert
    assert "deleted_stale_chunks" in upsert
    assert upsert["chunk_count"] >= 1


@pytest.mark.asyncio
async def test_layout_mode_reports_markdown_chunking_mode(monkeypatch, caplog):
    content = f"# Title\n\n{_SECRET_CONTENT}\n\n## Section\n\nmore text"
    instance, _ = _patched_rag(monkeypatch, fr_mode_content=content)

    with caplog.at_level(logging.INFO):
        await instance.upload(
            _FakeUploadFile(b"%PDF-fake-bytes"),
            "tenant-y",
            "layout",
            "/srv/files/doc.pdf",
            request_id="ing-req-2",
        )

    events = _events_by_name(caplog)
    assert events["chunking_completed"]["chunking_mode"] == "markdown_header"


@pytest.mark.asyncio
async def test_threaded_request_id_reused_across_stage_events(monkeypatch, caplog):
    content = f"# H\n\n{_SECRET_CONTENT} " * 20
    instance, _ = _patched_rag(monkeypatch, fr_mode_content=content)

    with caplog.at_level(logging.INFO):
        await instance.upload(
            _FakeUploadFile(b"%PDF-fake-bytes"),
            "tenant-z",
            "read",
            "/srv/files/doc.pdf",
            request_id="corr-ing-777",
        )

    events = _events_by_name(caplog)
    for name in (
        "ingestion_started",
        "document_parsed",
        "chunking_completed",
        "embeddings_completed",
        "index_upsert_completed",
    ):
        assert events[name]["request_id"] == "corr-ing-777"


@pytest.mark.asyncio
async def test_stage_events_exclude_document_content(monkeypatch, caplog):
    content = f"# H\n\n{_SECRET_CONTENT} " * 30
    instance, _ = _patched_rag(monkeypatch, fr_mode_content=content)

    with caplog.at_level(logging.INFO):
        await instance.upload(
            _FakeUploadFile(b"%PDF-fake-bytes"),
            "tenant-x",
            "read",
            "/srv/files/doc.pdf",
            request_id="ing-secret",
        )

    events = _events_by_name(caplog)
    all_event_json = json.dumps(events)
    assert _SECRET_CONTENT not in all_event_json


# ===========================================================================
# Failure event
# ===========================================================================
@pytest.mark.asyncio
async def test_ingestion_failed_event_includes_stage_and_safe_message(monkeypatch, caplog):
    """A Document-Intelligence failure must emit ingestion_failed with the
    `document_intelligence` stage and a safe message free of exception text."""
    content = f"# H\n\n{_SECRET_CONTENT}"
    instance, _ = _patched_rag(monkeypatch, fr_mode_content=content)

    # Make the loader raise with a sensitive message.
    raising_loader = MagicMock()
    raising_loader.load.side_effect = RuntimeError(
        f"Doc Intelligence blew up while reading {_SECRET_CONTENT}"
    )
    monkeypatch.setattr(
        custom_rag,
        "AzureAIDocumentIntelligenceLoader",
        MagicMock(return_value=raising_loader),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            await instance.upload(
                _FakeUploadFile(b"%PDF-fake-bytes"),
                "tenant-x",
                "read",
                "/srv/files/doc.pdf",
                request_id="ing-fail-1",
            )

    events = _events_by_name(caplog)
    failed = events["ingestion_failed"]
    assert failed["stage"] == "document_intelligence"
    assert failed["bot_tag"] == "tenant-x"
    assert failed["error_class"] == "RuntimeError"
    assert "safe_message" in failed
    # The sensitive exception text must NOT leak into the structured event.
    assert _SECRET_CONTENT not in json.dumps(failed)
    assert failed["request_id"] == "ing-fail-1"


@pytest.mark.asyncio
async def test_ingestion_failed_validation_stage_when_read_fails(monkeypatch, caplog):
    """If reading the upload fails, the failure stage should be `file_read`."""
    instance = rag()
    monkeypatch.setattr(instance, "create_search_index", AsyncMock(return_value=_FakeSearchClient()))

    class _BadFile:
        filename = "bad.pdf"

        async def read(self):
            raise OSError("disk gone")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(OSError):
            await instance.upload(_BadFile(), "tenant-x", "read", "/srv/bad.pdf", request_id="ing-fail-2")

    events = _events_by_name(caplog)
    assert events["ingestion_failed"]["stage"] == "file_read"
