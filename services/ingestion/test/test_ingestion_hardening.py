"""Tests for the ingestion-core hardening pass.

Covers the audit findings remediated in this change:

- C1  — /upload `bot_tag` OData-injection: the route rejects an injection
        payload with 422 (route-side pattern), AND the sink helper in
        `custom_rag` OData-escapes the literal (defense in depth).
- H2  — /upload requires `X-Admin-Token` (the dependency that guards /admin/*).
- H3  — a partial upsert failure (some IndexingResults `succeeded=False`)
        surfaces as a degraded status with the failed keys, never "successful".
- M3  — stale-chunk enumeration paginates via `.by_page()` (no `top` cap).
- M7  — a concurrency cap on /upload returns 429 when the slot is taken.
- L-Ing1 — upsert-then-prune deletes only stale ids the fresh write did not
        recreate (set difference); an empty parse leaves prior chunks intact.
- L-Ing2 — folder mode rejects a symlinked file escaping the allowed root.

The route-level tests reuse the `sys.modules` `custom_rag` stub pattern from
`test_path_safety.py` so `app.py` imports without the heavy deps. The
`custom_rag` unit tests import the real module (its only heavy import that
matters here, fitz/langchain, is not exercised by the helpers under test).
"""

import asyncio
import os
import pathlib
import sys
import types
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

_INGESTION_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

# Search env so importing custom_rag / admin never trips a config check.
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://test.search.windows.net")
os.environ.setdefault("AZURE_SEARCH_KEY", "test-key")
os.environ.setdefault("INDEX_NAME", "test-index")

from path_safety import ALLOWED_UPLOAD_ROOT_ENV  # noqa: E402

_ADMIN_TOKEN = "test-admin-token"
_AUTH = {"X-Admin-Token": _ADMIN_TOKEN}


# ---------------------------------------------------------------------------
# Route-level harness (stubbed custom_rag, real app.py)
# ---------------------------------------------------------------------------
@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv("ADMIN_API_TOKEN", _ADMIN_TOKEN)
    # Default cap high; individual tests override before importing app.
    monkeypatch.setenv("INGESTION_MAX_CONCURRENT_UPLOADS", "4")

    stub = types.ModuleType("custom_rag")
    rag_mock = MagicMock()

    class _RagStub:
        def __new__(cls):
            return rag_mock

    stub.rag = _RagStub
    monkeypatch.setitem(sys.modules, "custom_rag", stub)
    monkeypatch.delitem(sys.modules, "app", raising=False)

    import app as app_module

    return TestClient(app_module.app, raise_server_exceptions=False), rag_mock, app_module


# ---------------------------------------------------------------------------
# C1 — OData injection rejected at the route (422)
# ---------------------------------------------------------------------------
class TestC1RouteInjectionRejected:
    def test_injection_payload_rejected_422(self, app_client):
        client, rag_mock, _ = app_client
        r = client.post(
            "/upload",
            params={"bot_tag": "x' or bot_tag ne 'zz", "filepath": "anything"},
            headers=_AUTH,
        )
        assert r.status_code == 422
        # The pipeline must never be reached for an invalid bot_tag.
        rag_mock.upload.assert_not_called()
        body = r.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("bad", ["a b", "a'b", "", "a" * 129, "тег"])
    def test_other_invalid_bot_tags_rejected(self, app_client, bad):
        client, rag_mock, _ = app_client
        r = client.post("/upload", params={"bot_tag": bad, "filepath": "x"}, headers=_AUTH)
        assert r.status_code == 422
        rag_mock.upload.assert_not_called()


# ---------------------------------------------------------------------------
# C1 — sink-side OData escape (defense in depth)
# ---------------------------------------------------------------------------
class TestC1SinkEscape:
    def test_document_tag_filter_escapes_single_quote(self):
        import custom_rag

        f = custom_rag._document_tag_filter("d1", "x' or bot_tag ne 'zz")
        # Every single quote in the literal is doubled; the injected `or` clause
        # is now inert text inside the bot_tag literal, not OData syntax.
        assert f == "document_id eq 'd1' and bot_tag eq 'x'' or bot_tag ne ''zz'"
        # No lone (unescaped) quote that could terminate the literal early.
        assert "''" in f

    def test_escape_odata_doubles_quotes(self):
        import custom_rag

        assert custom_rag._escape_odata("a'b'c") == "a''b''c"


# ---------------------------------------------------------------------------
# H2 — /upload requires the admin token
# ---------------------------------------------------------------------------
class TestH2UploadRequiresAdminToken:
    def test_missing_token_is_401(self, app_client):
        client, rag_mock, _ = app_client
        r = client.post("/upload", params={"bot_tag": "t1", "filepath": "x"})
        assert r.status_code == 401
        rag_mock.upload.assert_not_called()

    def test_wrong_token_is_401(self, app_client):
        client, rag_mock, _ = app_client
        r = client.post(
            "/upload",
            params={"bot_tag": "t1", "filepath": "x"},
            headers={"X-Admin-Token": "wrong"},
        )
        assert r.status_code == 401
        rag_mock.upload.assert_not_called()

    def test_valid_token_passes_auth(self, tmp_path, app_client):
        # With a valid token the request gets past auth; a non-dir filepath with
        # no file body yields a 400 (auth succeeded, business validation fired).
        client, rag_mock, _ = app_client
        r = client.post("/upload", params={"bot_tag": "t1", "filepath": "nope"}, headers=_AUTH)
        assert r.status_code == 400  # not 401


# ---------------------------------------------------------------------------
# H3 — the /upload route surfaces a degraded result (does not mask as success)
# ---------------------------------------------------------------------------
class TestH3RouteSurfacesDegraded:
    def test_single_file_degraded_returns_207(self, tmp_path, app_client):
        client, rag_mock, _ = app_client
        (tmp_path / "d.pdf").write_bytes(b"%PDF-1.4 x")

        async def _degraded(*args, **kwargs):
            return {"status": "degraded", "failed_chunks": 2, "failed_keys": ["c1", "c2"]}

        rag_mock.upload = _degraded
        files = {"file": ("d.pdf", b"%PDF-1.4 x", "application/pdf")}
        r = client.post("/upload", params={"bot_tag": "t1", "filepath": "d.pdf"}, headers=_AUTH, files=files)
        assert r.status_code == 207
        body = r.json()
        assert body["status"] == "partially indexed"
        assert body["detail"]["failed_chunks"] == 2

    def test_single_file_success_returns_200(self, tmp_path, app_client):
        client, rag_mock, _ = app_client
        (tmp_path / "d.pdf").write_bytes(b"%PDF-1.4 x")

        async def _ok(*args, **kwargs):
            return {"status": "successful", "failed_chunks": 0}

        rag_mock.upload = _ok
        files = {"file": ("d.pdf", b"%PDF-1.4 x", "application/pdf")}
        r = client.post("/upload", params={"bot_tag": "t1", "filepath": "d.pdf"}, headers=_AUTH, files=files)
        assert r.status_code == 200
        assert r.json()["status"] == "successfully indexed"

    def test_folder_mode_degraded_per_file(self, tmp_path, app_client):
        client, rag_mock, _ = app_client
        batch = tmp_path / "batch"
        batch.mkdir()
        (batch / "a.pdf").write_bytes(b"%PDF-1.4 ok")

        async def _degraded(*args, **kwargs):
            return {"status": "degraded", "failed_chunks": 1}

        rag_mock.upload = _degraded
        r = client.post("/upload", params={"bot_tag": "t1", "filepath": "batch"}, headers=_AUTH)
        assert r.status_code == 200
        assert r.json()[0]["status"] == "degraded"


# ---------------------------------------------------------------------------
# Multi-format — /upload accepts the new formats and rejects unknown types
# ---------------------------------------------------------------------------
class TestMultiFormatRoute:
    def test_unsupported_single_file_returns_415(self, tmp_path, app_client):
        client, rag_mock, _ = app_client
        # An unknown extension must be rejected at the route as a clean 4xx —
        # never a 500 — and the pipeline must never run.
        files = {"file": ("image.png", b"\x89PNG\r\n\x1a\n", "image/png")}
        r = client.post(
            "/upload",
            params={"bot_tag": "t1", "filepath": "image.png"},
            headers=_AUTH,
            files=files,
        )
        assert r.status_code == 415
        rag_mock.upload.assert_not_called()
        body = r.json()
        assert body["error"]["code"] == "INVALID_REQUEST"

    @pytest.mark.parametrize(
        "filename",
        ["notes.txt", "report.docx", "deck.pptx", "page.html", "legacy.htm", "guide.md"],
    )
    def test_supported_new_formats_reach_pipeline(self, tmp_path, app_client, filename):
        client, rag_mock, _ = app_client

        async def _ok(*args, **kwargs):
            return {"status": "successful", "failed_chunks": 0}

        rag_mock.upload = _ok
        files = {"file": (filename, b"some bytes", "application/octet-stream")}
        r = client.post(
            "/upload",
            params={"bot_tag": "t1", "filepath": filename},
            headers=_AUTH,
            files=files,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "successfully indexed"

    def test_folder_mode_skips_unsupported_types(self, tmp_path, app_client):
        client, rag_mock, _ = app_client
        batch = tmp_path / "batch"
        batch.mkdir()
        (batch / "a.txt").write_bytes(b"plain text content")
        (batch / "b.docx").write_bytes(b"PK\x03\x04 docx bytes")
        (batch / "skip.png").write_bytes(b"\x89PNG\r\n\x1a\n")  # unsupported → skipped

        calls = []

        async def _ok(file, *args, **kwargs):
            calls.append(file.filename)
            return {"status": "successful", "failed_chunks": 0}

        rag_mock.upload = _ok
        r = client.post("/upload", params={"bot_tag": "t1", "filepath": "batch"}, headers=_AUTH)
        assert r.status_code == 200
        # Only the two supported files were processed; the .png was skipped.
        assert sorted(calls) == ["a.txt", "b.docx"]


# ---------------------------------------------------------------------------
# M7 — concurrency cap returns 429
# ---------------------------------------------------------------------------
class TestM7ConcurrencyCap:
    def test_second_concurrent_upload_gets_429(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
        monkeypatch.setenv("ADMIN_API_TOKEN", _ADMIN_TOKEN)
        monkeypatch.setenv("INGESTION_MAX_CONCURRENT_UPLOADS", "1")

        # A single PDF in-root, single-file mode. The pipeline call blocks on an
        # event so the slot is held while a second request races in.
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 x")
        release = asyncio.Event()

        stub = types.ModuleType("custom_rag")
        rag_mock = MagicMock()

        async def _slow_upload(*args, **kwargs):
            await release.wait()
            return {"status": "successful"}

        rag_mock.upload = _slow_upload

        class _RagStub:
            def __new__(cls):
                return rag_mock

        stub.rag = _RagStub
        monkeypatch.setitem(sys.modules, "custom_rag", stub)
        monkeypatch.delitem(sys.modules, "app", raising=False)

        import app as app_module

        async def _scenario():
            transport = __import__("httpx").ASGITransport(app=app_module.app)
            async with __import__("httpx").AsyncClient(transport=transport, base_url="http://test") as ac:
                files = {"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")}
                params = {"bot_tag": "t1", "filepath": "a.pdf"}
                first = asyncio.create_task(ac.post("/upload", params=params, headers=_AUTH, files=files))
                # Give the first request time to acquire the only slot.
                await asyncio.sleep(0.05)
                second = await ac.post(
                    "/upload",
                    params=params,
                    headers=_AUTH,
                    files={"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")},
                )
                release.set()
                first_resp = await first
                return first_resp, second

        first_resp, second = asyncio.run(_scenario())
        assert second.status_code == 429
        assert second.headers.get("Retry-After") == "5"
        assert first_resp.status_code == 200


# ---------------------------------------------------------------------------
# L-Ing2 — folder mode rejects an escaping symlink
# ---------------------------------------------------------------------------
class TestLIng2SymlinkRejected:
    def test_symlinked_file_outside_root_is_not_ingested(self, tmp_path, app_client):
        client, rag_mock, _ = app_client

        # Secret file OUTSIDE the allowed root.
        outside = tmp_path.parent / "secret_outside.pdf"
        outside.write_bytes(b"%PDF-1.4 SECRET")

        # Folder INSIDE the root containing a symlink to the outside secret +
        # one legitimate in-root pdf.
        batch = tmp_path / "batch"
        batch.mkdir()
        (batch / "legit.pdf").write_bytes(b"%PDF-1.4 ok")
        (batch / "escape.pdf").symlink_to(outside)

        async def _ok_upload(file, *args, **kwargs):
            # Prove the symlink target bytes never reach the pipeline.
            data = await file.read()
            assert b"SECRET" not in data
            return {"status": "successful"}

        rag_mock.upload = _ok_upload

        r = client.post("/upload", params={"bot_tag": "t1", "filepath": "batch"}, headers=_AUTH)
        assert r.status_code == 200
        results = {entry["file"]: entry for entry in r.json()}
        assert results["legit.pdf"]["status"] == "success"
        assert results["escape.pdf"]["status"] == "error"
        # No secret bytes anywhere in the response.
        assert "SECRET" not in r.text


# ---------------------------------------------------------------------------
# H3 / M3 / L-Ing1 — upsert batching, result-checking, prune (real custom_rag)
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, key, succeeded):
        self.key = key
        self.succeeded = succeeded


class _FakePager:
    """Mimics SearchItemPaged: iterable AND exposes .by_page()."""

    def __init__(self, ids):
        self._ids = ids

    def by_page(self):
        # One page is fine for the test; the production code iterates all pages.
        yield [{"id": i} for i in self._ids]


class _RecordingClient:
    def __init__(self, *, prior_ids=None, fail_keys=None):
        self._prior_ids = prior_ids or []
        self._fail_keys = set(fail_keys or [])
        self.upserted_batches = []
        self.deleted = []

    def search(self, **kwargs):
        return _FakePager(self._prior_ids)

    def merge_or_upload_documents(self, documents):
        self.upserted_batches.append([d["id"] for d in documents])
        return [_Result(d["id"], d["id"] not in self._fail_keys) for d in documents]

    def delete_documents(self, documents):
        self.deleted.extend(d["id"] for d in documents)


class TestUpsertHelpers:
    def test_upsert_in_batches_collects_failed_keys(self):
        import custom_rag

        client = _RecordingClient(fail_keys={"c1", "c3"})
        docs = [{"id": f"c{i}"} for i in range(5)]
        failed = custom_rag.rag._upsert_in_batches(client, docs)
        assert set(failed) == {"c1", "c3"}

    def test_upsert_all_success_returns_empty(self):
        import custom_rag

        client = _RecordingClient()
        docs = [{"id": f"c{i}"} for i in range(3)]
        assert custom_rag.rag._upsert_in_batches(client, docs) == []

    def test_drain_chunk_ids_paginates(self):
        import custom_rag

        client = _RecordingClient(prior_ids=["a", "b", "c"])
        ids = custom_rag.rag._drain_chunk_ids(client, "filter")
        assert ids == ["a", "b", "c"]


class TestUploadResultSurfacing:
    """Drive the real upload() with everything but the search client mocked, to
    prove a partial failure surfaces (H3) and prune uses the set difference
    (L-Ing1)."""

    def _patched_rag(self, monkeypatch, client):
        import custom_rag

        instance = custom_rag.rag()
        monkeypatch.setattr(instance, "create_search_index", _async_ret(client))
        monkeypatch.setattr(instance, "get_embedding", _async_ret([0.1] * 3))
        monkeypatch.setattr(instance, "chunk_token", _async_ret(10))

        # fitz.open(...).page_count
        fake_doc = MagicMock()
        fake_doc.page_count = 1
        monkeypatch.setattr(custom_rag.fitz, "open", lambda **kw: fake_doc)

        # Document Intelligence loader → two-chunk markdown so we get >1 chunk.
        doc = MagicMock()
        doc.page_content = "# H1\nalpha\n## H2\nbeta\n"
        loader = MagicMock()
        loader.load.return_value = [doc]
        monkeypatch.setattr(custom_rag, "AzureAIDocumentIntelligenceLoader", lambda **kw: loader)
        return instance

    def test_partial_failure_returns_degraded(self, monkeypatch):
        # The upsert reports one chunk failed → status must be "degraded" with
        # the failed key, NOT "successful".
        client = _RecordingClient()

        # Force the first upserted chunk to fail regardless of its id.
        orig = client.merge_or_upload_documents

        def _fail_first(documents):
            results = orig(documents)
            if results:
                results[0].succeeded = False
            return results

        client.merge_or_upload_documents = _fail_first

        instance = self._patched_rag(monkeypatch, client)
        file = _BytesFile(b"%PDF-1.4 data", "doc.pdf")
        stats = asyncio.run(instance.upload(file, "t1", "layout", "doc.pdf"))
        assert isinstance(stats, dict)
        assert stats["status"] == "degraded"
        assert stats["failed_chunks"] == 1
        assert len(stats["failed_keys"]) == 1

    def test_success_prunes_only_stale_residue(self, monkeypatch):
        # The set-difference must spare an id the fresh write RE-CREATES and
        # delete only genuine residue. Compute the real deterministic id the
        # upload will write (uses the content hash) so prior_ids genuinely
        # overlaps a written id — otherwise the test can't catch a missing
        # `if cid not in new_ids` guard.
        import hashlib

        content = b"%PDF-1.4 data"
        doc_id = hashlib.sha256(content).hexdigest()[:16]
        recreated = f"t1_{doc_id}_layout_00000"  # the first chunk's id
        residue = f"t1_{doc_id}_read_99999"  # a prior read-mode chunk, not rewritten

        client = _RecordingClient(prior_ids=[recreated, residue])
        instance = self._patched_rag(monkeypatch, client)
        file = _BytesFile(content, "doc.pdf")
        stats = asyncio.run(instance.upload(file, "t1", "layout", "doc.pdf"))
        assert stats["status"] == "successful"

        written = set(client.upserted_batches[0])
        assert recreated in written, "precondition: the test id must actually be rewritten"
        # The re-created id must NOT be deleted (the set-difference protects it).
        assert recreated not in client.deleted
        # Nothing we wrote is ever deleted.
        assert not (written & set(client.deleted))
        # The genuine residue (different fr_mode, not rewritten) WAS pruned.
        assert residue in client.deleted

    def test_empty_parse_leaves_chunks_intact(self, monkeypatch):
        # An empty parse must not delete prior chunks (no zero-chunk window).
        import custom_rag

        client = _RecordingClient(prior_ids=["t1_x_read_00000"])
        instance = self._patched_rag(monkeypatch, client)

        # Override the loader to yield only whitespace → zero non-empty chunks.
        doc = MagicMock()
        doc.page_content = "   \n  "
        loader = MagicMock()
        loader.load.return_value = [doc]
        monkeypatch.setattr(custom_rag, "AzureAIDocumentIntelligenceLoader", lambda **kw: loader)

        file = _BytesFile(b"%PDF-1.4 data", "doc.pdf")
        result = asyncio.run(instance.upload(file, "t1", "layout", "doc.pdf"))
        assert result == "No documents to upload"
        assert client.deleted == []  # prior chunks untouched


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _async_ret(value):
    async def _f(*args, **kwargs):
        return value

    return _f


class _BytesFile:
    def __init__(self, content, filename):
        self._content = content
        self.filename = filename

    async def read(self):
        return self._content
