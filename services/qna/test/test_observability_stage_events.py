"""Stage-level observability event tests for the QnA service (P1-1).

Covers the pipeline / auth / error stage events added on top of the PR-8
request-ID middleware + `log_event` helper:

- `query_rephrased`, `retrieval_completed`, `answer_generated` fire from the
  pipeline with the spec's field names.
- `retrieval_completed` carries source IDs/paths but NEVER chunk text.
- `answer_generated` carries metadata only (no answer body by default).
- The pipeline reuses a threaded `request_id` (and falls back to a local one
  when not passed — backward compatibility).
- `auth_success` / `auth_failure` fire from auth.py and NEVER log the token.
- `request_failed` fires from the P0-6 error handlers with the safe category /
  http_status / safe_message and no raw exception text.

All tests are hermetic — Azure clients are mocked; no network calls.
"""

import json
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Required env vars before importing app/config/pipeline modules.
# ---------------------------------------------------------------------------
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake-openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-06-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake-search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience-id")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _events_by_name(caplog) -> dict[str, dict]:
    """Parse JSON `log_event` lines from caplog into {event_name: payload}.

    Non-JSON log lines (the plain `logger.info(...)` ones) are skipped.
    """
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


class _FakeSearchClient:
    def search(self, **kwargs):
        # Two chunks from the SAME document — exercises de-duplication of
        # document IDs / source paths in the retrieval event.
        yield {
            "id": "tenant-a_doc111_fr_read_00000",
            "content": "SECRET CHUNK TEXT ONE should never be logged",
            "section_header": "sec",
            "filename": "doc.md",
            "filepath": "/docs/doc.md",
            "document_id": "doc111",
            "source_path": "/sources/doc.pdf",
        }
        yield {
            "id": "tenant-a_doc111_fr_read_00001",
            "content": "SECRET CHUNK TEXT TWO should never be logged",
            "section_header": "sec",
            "filename": "doc.md",
            "filepath": "/docs/doc.md",
            "document_id": "doc111",
            "source_path": "/sources/doc.pdf",
        }


class _FakeAzure:
    def __init__(self):
        self.search_client = _FakeSearchClient()
        self.embedding_client = MagicMock()
        self.openai_client = MagicMock()


async def _run_pipeline(caplog, *, request_id=None):
    """Run generate_answer end-to-end with all external calls mocked."""
    from src.pipeline import qna_pipeline

    azure = _FakeAzure()
    history = [{"user_query": "what is procurement?", "bot_response": None}]

    with (
        patch.object(
            qna_pipeline,
            "rephrase_queries",
            new=AsyncMock(
                return_value={
                    "rephrased_query": "what is procurement policy?",
                    "is_greeting": False,
                    "is_followup": False,
                    "extracted_snippet": "",
                    "original_response": "",
                    "was_rephrased": True,
                }
            ),
        ),
        patch.object(qna_pipeline, "get_embedding", new=AsyncMock(return_value=[0.1] * 3)),
        patch.object(
            qna_pipeline,
            "generate_openai_response",
            new=AsyncMock(return_value="The answer. SECRET ANSWER BODY [doc.md]"),
        ),
        patch.object(
            qna_pipeline,
            "extract_answer_and_filenames_from_text",
            new=AsyncMock(return_value=("The answer body here.", ["doc.md"])),
        ),
    ):
        with caplog.at_level(logging.INFO):
            return await qna_pipeline.generate_answer(
                query="what is procurement?",
                fr_mode="read",
                bot_tag="tenant-a",
                history=history,
                azure=azure,
                request_id=request_id,
            )


# ===========================================================================
# Pipeline stage events
# ===========================================================================
@pytest.mark.asyncio
async def test_pipeline_emits_stage_events_with_field_names(caplog):
    await _run_pipeline(caplog, request_id="req-123")
    events = _events_by_name(caplog)

    assert "query_rephrased" in events
    assert "retrieval_completed" in events
    assert "answer_generated" in events

    rephrased = events["query_rephrased"]
    assert "history_turns_used" in rephrased
    assert "latency_ms" in rephrased

    retrieval = events["retrieval_completed"]
    for field in (
        "bot_tag",
        "fr_tag",
        "retrieved_chunk_count",
        "top_k",
        "latency_ms",
        "source_document_ids",
        "source_paths",
    ):
        assert field in retrieval, f"missing {field} in retrieval_completed"
    assert retrieval["bot_tag"] == "tenant-a"
    assert retrieval["fr_tag"] == "fr_read"
    assert retrieval["retrieved_chunk_count"] == 2
    # De-duplicated: both chunks share one document / one source path.
    assert retrieval["source_document_ids"] == ["doc111"]
    assert retrieval["source_paths"] == ["/sources/doc.pdf"]

    answer = events["answer_generated"]
    for field in ("model", "latency_ms", "citation_count", "answer_length_chars"):
        assert field in answer, f"missing {field} in answer_generated"
    assert answer["citation_count"] == 1


@pytest.mark.asyncio
async def test_retrieval_event_excludes_chunk_text(caplog):
    await _run_pipeline(caplog, request_id="req-xyz")
    events = _events_by_name(caplog)
    retrieval_line = json.dumps(events["retrieval_completed"])
    assert "SECRET CHUNK TEXT" not in retrieval_line


@pytest.mark.asyncio
async def test_answer_event_excludes_answer_body_by_default(caplog):
    await _run_pipeline(caplog, request_id="req-ans")
    events = _events_by_name(caplog)
    answer_line = json.dumps(events["answer_generated"])
    # The body returned to the client is "The answer body here." — it must not
    # appear in the structured event when the debug preview flag is off.
    assert "answer_preview" not in events["answer_generated"]
    assert "The answer body here." not in answer_line


@pytest.mark.asyncio
async def test_threaded_request_id_is_used_in_stage_events(caplog):
    await _run_pipeline(caplog, request_id="corr-id-999")
    events = _events_by_name(caplog)
    for name in ("query_rephrased", "retrieval_completed", "answer_generated"):
        assert events[name]["request_id"] == "corr-id-999"


@pytest.mark.asyncio
async def test_pipeline_falls_back_to_local_request_id(caplog):
    """Backward compatibility: no request_id passed → a local gen_<ts> id."""
    await _run_pipeline(caplog, request_id=None)
    events = _events_by_name(caplog)
    rid = events["retrieval_completed"]["request_id"]
    assert rid.startswith("gen_")


# ===========================================================================
# Auth events
# ===========================================================================
@pytest.mark.asyncio
async def test_auth_failure_missing_token_event(caplog):
    from src.core import auth as auth_mod

    request = MagicMock()
    request.url.path = "/qna"
    request.method = "POST"
    request.headers = {}
    request.state.request_id = "auth-req-1"

    call_next = AsyncMock()

    with caplog.at_level(logging.WARNING):
        await auth_mod.AuthUtils.auth_middleware(request, call_next)

    events = _events_by_name(caplog)
    assert events["auth_failure"]["failure_type"] == "missing_token"
    assert events["auth_failure"]["request_id"] == "auth-req-1"
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_failure_event_excludes_token(caplog):
    from src.core import auth as auth_mod
    from src.core.token_validator import TokenValidationError

    secret_token = "SUPERSECRETJWTTOKENVALUE"
    request = MagicMock()
    request.url.path = "/qna"
    request.method = "POST"
    request.headers = {"Authorization": f"Bearer {secret_token}"}
    request.state.request_id = "auth-req-2"

    call_next = AsyncMock()

    with patch.object(
        auth_mod,
        "validate_token",
        new=AsyncMock(side_effect=TokenValidationError("Token has expired", status_code=401)),
    ):
        with caplog.at_level(logging.WARNING):
            await auth_mod.AuthUtils.auth_middleware(request, call_next)

    events = _events_by_name(caplog)
    assert events["auth_failure"]["failure_type"] == "expired_token"
    # The token value must never appear in ANY emitted log record.
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_token not in full_log


@pytest.mark.asyncio
async def test_auth_failure_jwks_unavailable(caplog):
    from src.core import auth as auth_mod
    from src.core.token_validator import TokenValidationError

    request = MagicMock()
    request.url.path = "/qna"
    request.method = "POST"
    request.headers = {"Authorization": "Bearer sometoken"}
    request.state.request_id = "auth-req-3"

    with patch.object(
        auth_mod,
        "validate_token",
        new=AsyncMock(side_effect=TokenValidationError("Unable to retrieve keys", status_code=503)),
    ):
        with caplog.at_level(logging.WARNING):
            await auth_mod.AuthUtils.auth_middleware(request, AsyncMock())

    events = _events_by_name(caplog)
    assert events["auth_failure"]["failure_type"] == "jwks_unavailable"


@pytest.mark.asyncio
async def test_auth_success_event(caplog):
    from src.core import auth as auth_mod

    request = MagicMock()
    request.url.path = "/qna"
    request.method = "POST"
    request.headers = {"Authorization": "Bearer goodtoken"}
    request.state.request_id = "auth-req-ok"

    call_next = AsyncMock(return_value="downstream-response")

    with patch.object(
        auth_mod,
        "validate_token",
        new=AsyncMock(return_value={"upn": "user@example.com"}),
    ):
        with caplog.at_level(logging.INFO):
            result = await auth_mod.AuthUtils.auth_middleware(request, call_next)

    assert result == "downstream-response"
    events = _events_by_name(caplog)
    assert "auth_success" in events
    assert events["auth_success"]["request_id"] == "auth-req-ok"


# ===========================================================================
# request_failed (P0-6 error handlers)
# ===========================================================================
@pytest.mark.asyncio
async def test_request_failed_event_from_http_handler(caplog):
    from fastapi import HTTPException
    from src.core import errors

    request = MagicMock()
    request.state.request_id = "fail-req-1"

    exc = HTTPException(status_code=400, detail={"code": "INVALID_REQUEST", "message": "Bad input"})

    with caplog.at_level(logging.WARNING):
        await errors.http_exception_handler(request, exc)

    events = _events_by_name(caplog)
    rf = events["request_failed"]
    assert rf["http_status"] == 400
    assert rf["error_category"] == "INVALID_REQUEST"
    assert rf["safe_message"] == "Bad input"
    assert rf["error_class"] == "HTTPException"


@pytest.mark.asyncio
async def test_request_failed_event_from_unhandled_handler_excludes_exc_text(caplog):
    from src.core import errors

    request = MagicMock()
    request.state.request_id = "fail-req-2"

    exc = RuntimeError("SECRET internal failure with /private/path")

    with caplog.at_level(logging.ERROR):
        await errors.unhandled_exception_handler(request, exc)

    events = _events_by_name(caplog)
    rf = events["request_failed"]
    assert rf["http_status"] == 500
    assert rf["error_category"] == "INTERNAL_ERROR"
    assert rf["error_class"] == "RuntimeError"
    # Raw exception text must NOT be in the structured request_failed event.
    assert "SECRET internal failure" not in json.dumps(rf)


# ===========================================================================
# Full-stack integration: request_id correlation + the known double-emit on
# the unhandled-500 path (RequestIDMiddleware + P0-6 catch-all both fire).
# ===========================================================================
def _all_events(caplog) -> list[dict]:
    """Return EVERY parsed structured event (no name de-dup), in order."""
    out: list[dict] = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if not msg.startswith("{"):
            continue
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and "event" in payload:
            out.append(payload)
    return out


def test_unhandled_500_double_emits_request_failed_with_shared_request_id(caplog):
    """End-to-end: a non-HTTPException through the full middleware stack.

    Documents (and pins) the known behavior that an unhandled 500 produces
    TWO `request_failed` records sharing one request_id:
      1. RequestIDMiddleware (PR-8) — transport-level, generic safe_message.
      2. The P0-6 catch-all handler — contract-level, carries `http_status`
         and `error_category` (the field dashboards should key on to dedupe).
    HTTPException paths do NOT double-emit (verified separately).
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.core.errors import register_exception_handlers
    from src.core.observability import RequestIDMiddleware

    a = FastAPI()
    register_exception_handlers(a)

    @a.get("/boom")
    def boom():
        raise RuntimeError("boom internal detail")

    a.add_middleware(RequestIDMiddleware)
    client = TestClient(a, raise_server_exceptions=False)

    with caplog.at_level(logging.INFO):
        r = client.get("/boom", headers={"X-Request-ID": "corr-e2e-1"})

    assert r.status_code == 500
    assert r.headers.get("X-Request-ID") == "corr-e2e-1" or r.headers.get("X-Request-ID")

    events = _all_events(caplog)
    failed = [e for e in events if e["event"] == "request_failed"]
    # Known double-emit on the unhandled-500 path (see docstring).
    assert len(failed) == 2
    # Exactly one carries the contract-level fields (http_status/error_category).
    contract = [e for e in failed if "http_status" in e]
    assert len(contract) == 1
    assert contract[0]["error_category"] == "INTERNAL_ERROR"
    # Both share the client-supplied correlation ID.
    started = [e for e in events if e["event"] == "request_started"]
    assert started and started[0]["request_id"] == "corr-e2e-1"
    for e in failed:
        assert e["request_id"] == "corr-e2e-1"
    # The raw exception text never appears in any structured event.
    assert "boom internal detail" not in json.dumps(events)


def test_http_exception_500_emits_single_request_failed(caplog):
    """raise_api_error(...500) goes through ExceptionMiddleware → single event."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.core.errors import ApiErrorCode, raise_api_error, register_exception_handlers
    from src.core.observability import RequestIDMiddleware

    a = FastAPI()
    register_exception_handlers(a)

    @a.get("/upstream")
    def upstream():
        raise_api_error(ApiErrorCode.UPSTREAM_UNAVAILABLE, "Search index down", 503)

    a.add_middleware(RequestIDMiddleware)
    client = TestClient(a, raise_server_exceptions=False)

    with caplog.at_level(logging.INFO):
        r = client.get("/upstream", headers={"X-Request-ID": "corr-e2e-2"})

    assert r.status_code == 503
    failed = [e for e in _all_events(caplog) if e["event"] == "request_failed"]
    # HTTPException is converted to a response by ExceptionMiddleware before
    # RequestIDMiddleware's except can see it — so exactly one event fires.
    assert len(failed) == 1
    assert failed[0]["http_status"] == 503
    assert failed[0]["error_category"] == "UPSTREAM_UNAVAILABLE"
