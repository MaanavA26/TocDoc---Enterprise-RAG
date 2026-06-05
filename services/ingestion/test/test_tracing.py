"""Tests for the default-OFF OpenTelemetry / Azure Monitor tracing wiring.

These tests exercise `tracing.configure_tracing` directly against a minimal
FastAPI app — they deliberately do NOT import `app.py` / `custom_rag`, which
pull heavy deps (PyMuPDF, langchain) not needed here.

The contract under test:
- Tracing is a strict no-op unless `APPLICATIONINSIGHTS_CONNECTION_STRING` is
  set: `configure_azure_monitor` is NEVER called and the app is NOT instrumented.
- When the env var IS set, `configure_azure_monitor` is called exactly once and
  the FastAPI app is instrumented (verified with a mocked exporter so no real
  Azure Monitor / network setup happens).
- The connection string value is never logged.
"""

import importlib
import logging
import pathlib
import sys

import pytest

# Make the per-service modules importable when running pytest from
# `services/ingestion/`.
_INGESTION_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

from fastapi import FastAPI  # noqa: E402

_CONN_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
# A realistic-looking (but fake) connection string; carries an InstrumentationKey.
_FAKE_CONN = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://example.in.applicationinsights.azure.com/"
)


@pytest.fixture
def fresh_tracing(monkeypatch):
    """Import a fresh copy of the tracing module with its module-level guard reset.

    `configure_tracing` keeps a `_configured` flag so it never double-registers
    the exporter; reloading the module per test gives each test a clean slate.
    """
    monkeypatch.delenv(_CONN_ENV, raising=False)
    if "tracing" in sys.modules:
        del sys.modules["tracing"]
    import tracing  # noqa: PLC0415

    importlib.reload(tracing)
    return tracing


@pytest.fixture
def app() -> FastAPI:
    return FastAPI()


# ---------------------------------------------------------------------------
# Default-OFF behavior (env var unset)
# ---------------------------------------------------------------------------


class TestDefaultOff:
    def test_is_tracing_enabled_false_when_unset(self, fresh_tracing, monkeypatch):
        monkeypatch.delenv(_CONN_ENV, raising=False)
        assert fresh_tracing.is_tracing_enabled() is False

    def test_is_tracing_enabled_false_when_empty(self, fresh_tracing, monkeypatch):
        # An empty / whitespace value must NOT enable tracing.
        monkeypatch.setenv(_CONN_ENV, "   ")
        assert fresh_tracing.is_tracing_enabled() is False

    def test_configure_tracing_is_noop_when_unset(self, fresh_tracing, app, monkeypatch):
        monkeypatch.delenv(_CONN_ENV, raising=False)

        called = {"configure": 0, "instrument": 0}

        # Patch the lazily-imported symbols at their source so we can assert the
        # no-op path never touches them. If configure_tracing tried to import or
        # call them, these counters would tick.
        import azure.monitor.opentelemetry as amo
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        monkeypatch.setattr(
            amo,
            "configure_azure_monitor",
            lambda *a, **k: called.__setitem__("configure", called["configure"] + 1),
        )
        monkeypatch.setattr(
            FastAPIInstrumentor,
            "instrument_app",
            staticmethod(lambda *a, **k: called.__setitem__("instrument", called["instrument"] + 1)),
        )

        result = fresh_tracing.configure_tracing(app)

        assert result is False
        assert called["configure"] == 0, "configure_azure_monitor must NOT be called when disabled"
        assert called["instrument"] == 0, "the app must NOT be instrumented when disabled"


# ---------------------------------------------------------------------------
# Enabled behavior (env var set) — with a mocked exporter
# ---------------------------------------------------------------------------


class TestEnabled:
    def test_configure_tracing_wires_up_when_set(self, fresh_tracing, app, monkeypatch, caplog):
        monkeypatch.setenv(_CONN_ENV, _FAKE_CONN)
        caplog.set_level(logging.INFO)

        calls = {"configure": 0}
        instrumented_apps = []

        import azure.monitor.opentelemetry as amo
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        def fake_configure(*args, **kwargs):
            calls["configure"] += 1

        monkeypatch.setattr(amo, "configure_azure_monitor", fake_configure)
        monkeypatch.setattr(
            FastAPIInstrumentor,
            "instrument_app",
            staticmethod(lambda app, **kwargs: instrumented_apps.append((app, kwargs))),
        )

        assert fresh_tracing.is_tracing_enabled() is True
        result = fresh_tracing.configure_tracing(app)

        assert result is True
        assert calls["configure"] == 1, "configure_azure_monitor must be called exactly once"
        assert len(instrumented_apps) == 1, "the app must be instrumented exactly once"
        instrumented_app, kwargs = instrumented_apps[0]
        assert instrumented_app is app
        # The X-Request-ID correlation hook must be wired up.
        assert "server_request_hook" in kwargs
        assert kwargs["server_request_hook"] is fresh_tracing._server_request_hook

    def test_connection_string_never_logged(self, fresh_tracing, app, monkeypatch, caplog):
        monkeypatch.setenv(_CONN_ENV, _FAKE_CONN)
        caplog.set_level(logging.DEBUG)

        import azure.monitor.opentelemetry as amo
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        monkeypatch.setattr(amo, "configure_azure_monitor", lambda *a, **k: None)
        monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", staticmethod(lambda *a, **k: None))

        fresh_tracing.configure_tracing(app)

        full_output = " ".join(r.message for r in caplog.records)
        assert _FAKE_CONN not in full_output
        assert "00000000-0000-0000-0000-000000000000" not in full_output

    def test_configure_tracing_is_idempotent(self, fresh_tracing, app, monkeypatch):
        monkeypatch.setenv(_CONN_ENV, _FAKE_CONN)

        calls = {"configure": 0, "instrument": 0}
        import azure.monitor.opentelemetry as amo
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        monkeypatch.setattr(
            amo,
            "configure_azure_monitor",
            lambda *a, **k: calls.__setitem__("configure", calls["configure"] + 1),
        )
        monkeypatch.setattr(
            FastAPIInstrumentor,
            "instrument_app",
            staticmethod(lambda *a, **k: calls.__setitem__("instrument", calls["instrument"] + 1)),
        )

        assert fresh_tracing.configure_tracing(app) is True
        # Second call must not re-register the exporter / re-instrument.
        assert fresh_tracing.configure_tracing(app) is True
        assert calls["configure"] == 1
        assert calls["instrument"] == 1

    def test_real_instrument_app_accepts_our_kwargs(self, fresh_tracing, app, monkeypatch):
        """Run the REAL FastAPIInstrumentor.instrument_app against a real app.

        The other enabled-path tests mock instrument_app, so they would not
        catch a signature mismatch (e.g. if the installed instrumentation no
        longer accepts `server_request_hook`). This test mocks ONLY the Azure
        Monitor exporter (so no network/exporter setup happens) and lets the
        real instrumentation run, asserting it accepts our hook kwarg and marks
        the app instrumented. instrument_app is per-app, so this does not leak
        global OTel state into other tests.
        """
        monkeypatch.setenv(_CONN_ENV, _FAKE_CONN)

        import azure.monitor.opentelemetry as amo
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        monkeypatch.setattr(amo, "configure_azure_monitor", lambda *a, **k: None)

        # Real instrument_app runs here — would raise on an unexpected kwarg.
        assert fresh_tracing.configure_tracing(app) is True
        assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True

        # Clean up so we don't leave global instrumentation state set.
        FastAPIInstrumentor.uninstrument_app(app)


# ---------------------------------------------------------------------------
# server_request_hook correlation behavior
# ---------------------------------------------------------------------------


class _FakeSpan:
    def __init__(self, recording=True):
        self._recording = recording
        self.attributes = {}

    def is_recording(self):
        return self._recording

    def set_attribute(self, key, value):
        self.attributes[key] = value


class TestServerRequestHook:
    def test_sets_request_id_attribute_from_valid_header(self, fresh_tracing):
        span = _FakeSpan()
        scope = {"headers": [(b"x-request-id", b"valid-correlation-123")]}
        fresh_tracing._server_request_hook(span, scope)
        assert span.attributes.get("tocdoc.request_id") == "valid-correlation-123"

    def test_skips_invalid_request_id_header(self, fresh_tracing):
        span = _FakeSpan()
        # Contains characters disallowed by the shared validator → must be skipped.
        scope = {"headers": [(b"x-request-id", b"bad value; DROP")]}
        fresh_tracing._server_request_hook(span, scope)
        assert "tocdoc.request_id" not in span.attributes

    def test_noop_when_no_header(self, fresh_tracing):
        span = _FakeSpan()
        scope = {"headers": [(b"content-type", b"application/json")]}
        fresh_tracing._server_request_hook(span, scope)
        assert span.attributes == {}

    def test_noop_when_span_not_recording(self, fresh_tracing):
        span = _FakeSpan(recording=False)
        scope = {"headers": [(b"x-request-id", b"valid-correlation-123")]}
        fresh_tracing._server_request_hook(span, scope)
        assert span.attributes == {}

    def test_never_raises_on_malformed_scope(self, fresh_tracing):
        # Must be defensive: a malformed scope must not raise into the request path.
        fresh_tracing._server_request_hook(_FakeSpan(), {})
        fresh_tracing._server_request_hook(None, {"headers": []})
        fresh_tracing._server_request_hook(_FakeSpan(), {"headers": None})
