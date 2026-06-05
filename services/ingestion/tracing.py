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
  (a) SCRUBS the query string off the server span and (b) stamps a client-sent
  `X-Request-ID` as the `tocdoc.request_id` attribute. For the common path
  (no inbound header), `SpanRequestIdMiddleware` stamps the ID that
  `observability.RequestIDMiddleware` generated, so traces join up with the
  structured logs on BOTH the header and generated-ID paths.

Security / privacy:
- The connection string is read from the environment ONLY and is never logged.
  It carries an InstrumentationKey, so it is treated like a secret.
- This module exports SPANS (traces) ONLY. `configure_azure_monitor` is called
  with `disable_logging=True` and `disable_metrics=True` so the Azure Monitor
  logging and metrics pipelines are NOT installed. That matters because the
  default logging pipeline attaches a `LoggingHandler` to the ROOT logger and
  would ship raw application log records — which carry the absolute server
  `filepath`, the tenant `bot_tag`, and full tracebacks (`exc_info=True`) — to
  App Insights through a SECOND, completely un-redacted channel, defeating the
  spans-only redaction below. Keeping logs/metrics off means the span hooks are
  the only thing that reaches App Insights, so the redaction here is the whole
  egress story. (If a logs export is ever wanted, route it through a dedicated,
  scrubbed logger via the `logger_name` kwarg — never the root logger.)
- We do not add request/response bodies or headers to spans beyond the
  request_id correlation attribute. The default ASGI instrumentation records
  the request URL on the server span — and `/upload` carries its two most
  sensitive inputs (`filepath`, an absolute server path, and `bot_tag`, the
  tenant id) as QUERY parameters — so `_server_request_hook` overwrites the
  query-bearing `http.url` / `url.full` attributes with the path-only URL and
  clears `url.query`. NO request query data lands on any span, under both the
  default and the stable HTTP semconv (`OTEL_SEMCONV_STABILITY_OPT_IN=http`)
  attribute namings. (`http.target` is already path-only on the pinned
  instrumentation; we redact the URL attributes defensively and verify in tests.)

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

# Span attributes that the default HTTP instrumentation populates with the full
# request URL INCLUDING the query string. `/upload` declares `filepath` (an
# absolute server path) and `bot_tag` (the tenant id) as query params, so the
# query string must never reach a span. `http.url` (legacy) and `url.full`
# (stable semconv) are redacted to a path-only value in `_server_request_hook`.
# `http.target` / `url.path` are already path-only on the pinned wheel.
_QUERY_BEARING_SPAN_ATTRIBUTES = ("http.url", "url.full")

# Under the stable HTTP semconv opt-in (`OTEL_SEMCONV_STABILITY_OPT_IN=http`),
# the ASGI instrumentation records the raw query string in a SEPARATE
# `url.query` attribute (NOT part of the full-URL attributes above). It is
# cleared to an empty string rather than added to the tuple above — that tuple
# is assigned the path-only full URL, which would be semantically wrong here.
_QUERY_STRING_SPAN_ATTRIBUTE = "url.query"

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


def _redact_query_string(span: Any, scope: dict) -> None:
    """Overwrite query-bearing span attributes with a path-only URL.

    The default HTTP instrumentation records the full request URL (with the
    query string) as `http.url` / `url.full`. `/upload` carries `filepath` and
    `bot_tag` as query params, so we recompute a path-only URL from the ASGI
    scope and overwrite those attributes. Under the stable HTTP semconv opt-in
    the instrumentation ALSO records the raw query string as a separate
    `url.query` attribute, so we clear that to an empty string too. `set_attribute`
    with the same key replaces the value, and this hook runs after the
    instrumentation has set the URL but before the span ends, so the redaction
    sticks on the exported span.
    """
    scheme = scope.get("scheme", "http")
    path = scope.get("path", "") or ""
    host = ""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"host":
            host = value.decode("latin-1", errors="replace")
            break
    if not host:
        server = scope.get("server") or ("", None)
        host = server[0] or ""
        if server[1]:
            host = f"{host}:{server[1]}"
    safe_url = f"{scheme}://{host}{path}" if host else path
    for attr in _QUERY_BEARING_SPAN_ATTRIBUTES:
        # Overwrite unconditionally: the path-only URL is always safe, and we do
        # not depend on which attribute the installed wheel happens to populate.
        span.set_attribute(attr, safe_url)
    # Stable-semconv mode (`OTEL_SEMCONV_STABILITY_OPT_IN=http`/`http/dup`) records
    # the raw query string in a separate `url.query` attribute. Clear it to empty
    # so `filepath`/`bot_tag` cannot leak there either. Set separately (NOT via the
    # tuple above) because that path assigns the full path-only URL, not a query.
    span.set_attribute(_QUERY_STRING_SPAN_ATTRIBUTE, "")


def _server_request_hook(span: Any, scope: dict) -> None:
    """Scrub the query string and stamp an inbound X-Request-ID on the span.

    Called by the ASGI/FastAPI instrumentation for each server span. Best-effort
    and defensive: it must never raise into the request path.

    Two jobs:
    1. Privacy: redact the query string off the URL attribute(s) so the
       sensitive `/upload` query params (`filepath`, `bot_tag`) never reach a
       span. Done for EVERY request.
    2. Correlation: when the client SENT an `X-Request-ID` header, copy it
       verbatim onto the span (after the same conservative validation the app
       applies elsewhere, so it can never carry a log/trace-injection payload).
       For the common case where no header is sent, `SpanRequestIdMiddleware`
       stamps the generated ID instead — that ID is not available at this layer.
    """
    try:
        if span is None or not span.is_recording():
            return

        _redact_query_string(span, scope)

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


def _stamp_request_id_on_current_span(request_id: str | None) -> None:
    """Set `tocdoc.request_id` on the active server span (generated-ID path).

    `_server_request_hook` only sees a CLIENT-SENT header — it fires at span
    creation, before `RequestIDMiddleware` runs, so the GENERATED id (the common
    case) is not yet on `request.state`. This helper is called from
    `SpanRequestIdMiddleware`, which runs INNER of `RequestIDMiddleware` (so the
    id is set) but still inside the OTel server-span context, and stamps it.
    Defensive: never raises into the request path.
    """
    if not request_id:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute(_REQUEST_ID_SPAN_ATTRIBUTE, request_id)
    except Exception:  # noqa: BLE001 - telemetry must never break a request
        return


def install_request_id_span_middleware(app: Any) -> None:
    """Register the generated-ID span-stamping middleware (tracing-ON only).

    Imported lazily and registered ONLY when tracing is enabled so the
    default-OFF path stays byte-for-byte unchanged (no extra middleware in the
    stack). Must be added BEFORE `RequestIDMiddleware` in app.py so it runs
    inner of it — i.e. after `request.state.request_id` is populated.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    class SpanRequestIdMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # request.state.request_id is set by the (outer) RequestIDMiddleware
            # before this runs. Stamp it on the live server span so the
            # generated-ID path correlates spans with logs too.
            _stamp_request_id_on_current_span(getattr(request.state, "request_id", None))
            return await call_next(request)

    app.add_middleware(SpanRequestIdMiddleware)


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
    #
    # SPANS-ONLY: disable the logging and metrics pipelines. The default logging
    # pipeline attaches a LoggingHandler to the ROOT logger and would ship raw app
    # log records (carrying the absolute server `filepath`, the tenant `bot_tag`,
    # and full `exc_info` tracebacks) to App Insights through an UN-redacted
    # channel — defeating the span redaction above. Only redacted spans (traces)
    # are exported.
    configure_azure_monitor(disable_logging=True, disable_metrics=True)
    FastAPIInstrumentor.instrument_app(app, server_request_hook=_server_request_hook)

    _configured = True
    # Log that tracing is ON, but NEVER the connection string.
    logger.info("OpenTelemetry tracing enabled (Azure Monitor exporter configured)")
    return True
