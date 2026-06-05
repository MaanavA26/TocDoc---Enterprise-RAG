"""OpenTelemetry / Azure Monitor tracing for the ingestion service (default-OFF).

This module is the single entry point for distributed tracing. It is wired so
that tracing is a **strict no-op unless** the environment variable
`APPLICATIONINSIGHTS_CONNECTION_STRING` is set:

- Unset (the default, including all current deployments and CI): no exporter is
  created, no background telemetry threads start, no network egress occurs, and
  the FastAPI app behaves byte-for-byte as before. This is the safe posture for
  air-gapped / client-managed resource groups where App Insights may not exist.
- Set: `configure_azure_monitor()` installs the Azure Monitor span exporter and
  we instrument the FastAPI app so each inbound request (and outbound HTTP calls
  made via `requests`/`urllib3`) produces a span. A `server_request_hook`
  stamps the existing `X-Request-ID` correlation ID onto the server span as the
  `tocdoc.request_id` attribute, so traces join up with the structured logs
  emitted by `observability.RequestIDMiddleware`.

Security / privacy:
- The connection string is read from the environment ONLY and is never logged.
  It carries an InstrumentationKey, so it is treated like a secret.
- We do not add request/response bodies, headers, or query strings to spans
  beyond the request_id correlation attribute; the default ASGI instrumentation
  captures only method/route/status, consistent with the no-secrets logging bar.

Kept separate from `observability.py` deliberately: that module is duplicated
verbatim in `services/qna/src/core/observability.py` and must stay in sync, so
tracing lives here to avoid diverging the synced file.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_CONNECTION_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"

# Span attribute used to correlate a trace with the X-Request-ID surfaced by
# RequestIDMiddleware and the structured `log_event` records.
_REQUEST_ID_SPAN_ATTRIBUTE = "tocdoc.request_id"

# Header carrying the correlation ID (read/validated by RequestIDMiddleware).
_REQUEST_ID_HEADER = b"x-request-id"

logger = logging.getLogger("observability.tracing")

# Module-level guard so a double call (e.g. tests, or an accidental second
# import) does not register the Azure Monitor exporter twice.
_configured = False


def is_tracing_enabled() -> bool:
    """Return True iff the App Insights connection string is configured.

    Tracing is OFF by default. Presence of a non-empty
    `APPLICATIONINSIGHTS_CONNECTION_STRING` is the sole switch.
    """
    return bool(os.getenv(_CONNECTION_STRING_ENV, "").strip())


def _server_request_hook(span: Any, scope: dict) -> None:
    """Stamp the inbound X-Request-ID onto the server span for correlation.

    Called by the ASGI/FastAPI instrumentation for each server span. Best-effort
    and defensive: it must never raise into the request path. The raw header is
    only copied verbatim if it passes the same conservative validation the app
    applies elsewhere; otherwise it is skipped (RequestIDMiddleware will have
    generated a fresh ID, but that value is not available at this layer, so we
    simply omit the attribute rather than log anything unsafe).
    """
    try:
        if span is None or not span.is_recording():
            return
        for key, value in scope.get("headers") or []:
            if key.lower() == _REQUEST_ID_HEADER:
                # Lazy import keeps this module importable even if observability
                # changes shape; we reuse its validator so the attribute can
                # never carry a log/trace-injection payload.
                from observability import _validate_request_id

                candidate = value.decode("latin-1", errors="replace")
                validated = _validate_request_id(candidate)
                if validated:
                    span.set_attribute(_REQUEST_ID_SPAN_ATTRIBUTE, validated)
                return
    except Exception:  # noqa: BLE001 - telemetry must never break a request
        return


def configure_tracing(app: Any) -> bool:
    """Initialize Azure Monitor + FastAPI instrumentation, if enabled.

    No-op (returns False) when `APPLICATIONINSIGHTS_CONNECTION_STRING` is unset
    or empty — no exporter, no network, no behavior change. When set, configures
    the Azure Monitor exporter once and instruments the given FastAPI app.

    Args:
        app: The FastAPI application instance to instrument.

    Returns:
        True if tracing was configured (now or on a prior call), else False.
    """
    global _configured

    if not is_tracing_enabled():
        # Default path: stay completely inert.
        return False

    if _configured:
        return True

    # Imported lazily so the dependency is only loaded when tracing is on. This
    # keeps import-time cost and surface area off the default (tracing-OFF) path.
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # The connection string is read from the env by configure_azure_monitor
    # itself; we pass nothing secret here and never log its value.
    configure_azure_monitor()
    FastAPIInstrumentor.instrument_app(app, server_request_hook=_server_request_hook)

    _configured = True
    # Log that tracing is ON, but NEVER the connection string.
    logger.info("OpenTelemetry tracing enabled (Azure Monitor exporter configured)")
    return True
