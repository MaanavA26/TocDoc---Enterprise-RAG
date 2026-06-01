"""Tests for RequestIDMiddleware and log_event (Phase 2 Workstream B PR-1).

Tests run against a minimal FastAPI app that mounts only the request-ID
middleware — this avoids the QnA app's startup complexity (Azure clients,
Key Vault loading, etc.) and lets the tests run without any Azure
connectivity.
"""

import json
import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.core.observability import (
    RequestIDMiddleware,
    get_current_request_id,
    log_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    """Minimal FastAPI app with only the request-ID middleware mounted."""
    a = FastAPI()
    a.add_middleware(RequestIDMiddleware)

    @a.get("/ping")
    def ping():
        # Echo the request_id resolved via ContextVar so tests can verify
        # the middleware made it available to the handler.
        return {"request_id": get_current_request_id()}

    @a.get("/boom")
    def boom():
        # Plain Exception (not HTTPException) so it escapes the FastAPI
        # exception handlers and reaches our middleware.
        raise RuntimeError("simulated handler failure with sensitive details xyz123")

    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False so we can assert on the 500 response
    # body when /boom raises, instead of the test runner re-raising.
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# X-Request-ID header behavior
# ---------------------------------------------------------------------------


class TestRequestIdHeader:
    def test_provided_id_is_reused(self, client: TestClient):
        incoming = "client-supplied-id-12345"
        r = client.get("/ping", headers={"X-Request-ID": incoming})
        assert r.status_code == 200
        assert r.headers["X-Request-ID"] == incoming
        assert r.json()["request_id"] == incoming

    def test_missing_id_generates_uuid4(self, client: TestClient):
        r = client.get("/ping")
        assert r.status_code == 200
        rid = r.headers["X-Request-ID"]
        # Must parse as a v4 UUID
        parsed = uuid.UUID(rid)
        assert parsed.version == 4
        assert r.json()["request_id"] == rid

    def test_malformed_id_is_ignored(self, client: TestClient, caplog):
        bad = "; DROP TABLE--"
        caplog.set_level(logging.WARNING)
        r = client.get("/ping", headers={"X-Request-ID": bad})
        assert r.status_code == 200
        rid = r.headers["X-Request-ID"]
        # Must NOT be the malformed value (defense against log injection)
        assert rid != bad
        # Must be a valid v4 UUID generated as a fallback
        parsed = uuid.UUID(rid)
        assert parsed.version == 4
        # A structured event MUST be emitted so this is greppable, but the
        # bad value MUST NOT be in the log (otherwise log injection wins).
        rejected = [r for r in caplog.records if "invalid_request_id_rejected" in r.message]
        assert len(rejected) >= 1, "expected an invalid_request_id_rejected event"
        for record in rejected:
            assert bad not in record.message, "bad value leaked into log"

    def test_oversized_id_is_ignored(self, client: TestClient):
        r = client.get("/ping", headers={"X-Request-ID": "a" * 129})
        assert r.status_code == 200
        parsed = uuid.UUID(r.headers["X-Request-ID"])
        assert parsed.version == 4

    def test_id_with_newline_is_ignored(self, client: TestClient):
        # Log-injection canary: newlines must be rejected.
        r = client.get("/ping", headers={"X-Request-ID": "abc\ndef"})
        assert r.status_code == 200
        rid = r.headers["X-Request-ID"]
        assert "\n" not in rid
        parsed = uuid.UUID(rid)
        assert parsed.version == 4


# ---------------------------------------------------------------------------
# log_event behavior
# ---------------------------------------------------------------------------


class TestLogEvent:
    def test_includes_event_and_request_id(self, caplog):
        caplog.set_level(logging.INFO)
        logger = logging.getLogger("test_log_event")
        log_event(logger, "test_event", request_id="my-id")
        # Find the record we emitted
        record = next(r for r in caplog.records if "test_event" in r.message)
        payload = json.loads(record.message)
        assert payload["event"] == "test_event"
        assert payload["request_id"] == "my-id"

    def test_truncates_long_string_values(self, caplog):
        caplog.set_level(logging.INFO)
        logger = logging.getLogger("test_log_event")
        long_value = "x" * 500
        log_event(logger, "trunc_event", payload=long_value, request_id="rid")
        record = next(r for r in caplog.records if "trunc_event" in r.message)
        payload = json.loads(record.message)
        # 200 chars + "..." suffix
        assert payload["payload"] == "x" * 200 + "..."
        # The full 500-char string must NOT appear anywhere
        assert "x" * 500 not in record.message

    def test_drops_none_values(self, caplog):
        caplog.set_level(logging.INFO)
        logger = logging.getLogger("test_log_event")
        log_event(
            logger,
            "drop_event",
            maybe_none=None,
            present="here",
            request_id="rid",
        )
        record = next(r for r in caplog.records if "drop_event" in r.message)
        payload = json.loads(record.message)
        assert "maybe_none" not in payload
        assert payload["present"] == "here"

    def test_max_field_len_zero_disables_truncation(self, caplog):
        caplog.set_level(logging.INFO)
        logger = logging.getLogger("test_log_event")
        long_value = "x" * 500
        log_event(
            logger,
            "no_trunc_event",
            payload=long_value,
            request_id="rid",
            max_field_len=0,
        )
        record = next(r for r in caplog.records if "no_trunc_event" in r.message)
        payload = json.loads(record.message)
        assert payload["payload"] == long_value

    def test_does_not_raise_on_unserializable_field(self, caplog):
        caplog.set_level(logging.INFO)
        logger = logging.getLogger("test_log_event")

        class WeirdNotSerializable:
            # json.dumps(default=str) calls str() on unknown types. Make BOTH
            # __str__ and __repr__ raise so the JSON serialization fallback in
            # log_event is genuinely exercised.
            def __str__(self):
                raise RuntimeError("str() failed")

            def __repr__(self):
                raise RuntimeError("repr() failed")

        # Must not raise; produces a fallback line instead.
        log_event(logger, "weird_event", thing=WeirdNotSerializable(), request_id="rid")
        # The fallback line is plain key=value, not JSON.
        fallback = next(
            r
            for r in caplog.records
            if "weird_event" in r.message and "json_serialization_failed" in r.message
        )
        assert "rid" in fallback.message

    def test_resolves_request_id_from_context_var_when_not_passed(self, app, client, caplog):
        """When called inside a request, log_event picks up the ContextVar."""
        caplog.set_level(logging.INFO)

        @app.get("/log-without-rid")
        def log_without_rid():
            log_event(logging.getLogger("handler"), "handler_event", info="value")
            return {"ok": True}

        r = client.get("/log-without-rid", headers={"X-Request-ID": "from-context"})
        assert r.status_code == 200
        record = next(r for r in caplog.records if "handler_event" in r.message)
        payload = json.loads(record.message)
        assert payload["request_id"] == "from-context"


# ---------------------------------------------------------------------------
# Lifecycle events emitted by the middleware
# ---------------------------------------------------------------------------


class TestLifecycleEvents:
    def test_request_started_and_completed_emitted_on_success(
        self,
        client: TestClient,
        caplog,
    ):
        caplog.set_level(logging.INFO)
        r = client.get("/ping")
        assert r.status_code == 200
        events_seen = set()
        for record in caplog.records:
            if "request_started" in record.message:
                events_seen.add("started")
            if "request_completed" in record.message:
                events_seen.add("completed")
            if "request_failed" in record.message:
                events_seen.add("failed_unexpectedly")
        assert events_seen == {"started", "completed"}, events_seen

    def test_request_failed_emitted_on_handler_exception(
        self,
        client: TestClient,
        caplog,
    ):
        """The structured request_failed event fires when an unhandled
        exception escapes the handler. The middleware re-raises so Starlette's
        ServerErrorMiddleware (outside our middleware) generates the 500
        response.

        KNOWN LIMITATION (deferred to P0-6 error-contract work): the 500
        response generated by ServerErrorMiddleware does NOT carry the
        X-Request-ID header. We deliberately do not assert it here. Responses
        produced via HTTPException (4xx + the common 5xx path) DO carry the
        header because FastAPI's ExceptionMiddleware returns a Response that
        this middleware sees on its way out.
        """
        caplog.set_level(logging.INFO)
        r = client.get("/boom")
        assert r.status_code == 500

        failed = [r for r in caplog.records if "request_failed" in r.message]
        assert len(failed) == 1, f"expected exactly 1 request_failed record, got {len(failed)}"
        payload = json.loads(failed[0].message)
        assert payload["event"] == "request_failed"
        assert payload["error_class"] == "RuntimeError"
        # request_id appears in the structured log even though it's not on
        # the response — the correlation lives server-side.
        rid = payload["request_id"]
        parsed = uuid.UUID(rid)
        assert parsed.version == 4
        # The actual exception message MUST NOT appear in the structured
        # log record (it may contain sensitive details).
        assert "simulated handler failure" not in failed[0].message
        assert "sensitive details xyz123" not in failed[0].message
        assert payload["safe_message"] == "Request handler raised an unhandled exception"

    def test_request_completed_includes_status_and_latency(
        self,
        client: TestClient,
        caplog,
    ):
        caplog.set_level(logging.INFO)
        client.get("/ping")
        completed = next(r for r in caplog.records if "request_completed" in r.message)
        payload = json.loads(completed.message)
        assert payload["status_code"] == 200
        assert isinstance(payload["latency_ms"], (int, float))
        assert payload["latency_ms"] >= 0
