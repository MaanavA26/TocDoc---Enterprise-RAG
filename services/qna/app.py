import json
import logging
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

import src.pipeline.qna_pipeline
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from src.config.config import is_agent_enabled
from src.core.auth import AuthUtils
from src.core.errors import (
    ApiErrorCode,
    default_error_responses,
    raise_api_error,
    register_exception_handlers,
)
from src.core.lifecycle import shutdown_event, startup_event
from src.core.observability import RequestIDMiddleware, log_event
from src.core.responses import QnASuccessResponse
from src.core.tenant_binding import enforce_tenant_bot_tag_binding
from src.utils.util import Payload, _as_turn
from starlette.responses import JSONResponse, StreamingResponse

# Allowed retrieval modes accepted at the request boundary. Enforced once here
# (audit L-Q3) so BOTH the legacy pipeline and the agentic map-reduce path
# converge on the same allow-list instead of validating only in the legacy path.
_ALLOWED_FR_TAGS = ("read", "layout")

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
if not logger.handlers:
    _console = logging.StreamHandler()
    _console.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(_console)
logger.propagate = False


# ---------------------------------------------------------------------------
# Rate limiting + concurrency cap (audit M7)
# ---------------------------------------------------------------------------
# Lightweight, dependency-free, in-process throttling on the expensive `/qna`
# endpoint. `/qna` fans out to LLM + embedding + search (and map-reduce when the
# agent flags are on), so an unthrottled caller can drive unbounded Azure spend
# (denial-of-wallet) and saturate the 2-worker executors. We deliberately avoid
# slowapi (an extra dependency) — a sliding-window counter plus a concurrency
# gate covers the request path and is fully unit-testable.
#
# NOTE: this is per-process. For a multi-replica deployment, ingress-level
# (Container Apps / APIM / Front Door) rate limiting is still REQUIRED; this is
# defense-in-depth, not a substitute. The concurrency gate uses a plain int
# guarded by a threading.Lock rather than an asyncio.Semaphore on purpose: a
# module-level asyncio primitive binds to the import-time event loop and breaks
# under per-request / per-test loops (same hazard documented in agents/map_reduce.py).


def _rate_limit_per_min() -> int:
    """Requests allowed per client key per 60s window. <=0 disables limiting."""
    try:
        return int(os.getenv("QNA_RATE_LIMIT_PER_MIN", "120"))
    except ValueError:
        return 120


def _max_concurrency() -> int:
    """Max simultaneous in-flight /qna requests. <=0 disables the gate."""
    try:
        return int(os.getenv("QNA_MAX_CONCURRENCY", "16"))
    except ValueError:
        return 16


class _SlidingWindowRateLimiter:
    """Per-key fixed-cost sliding-window limiter (60s window).

    Keyed by client IP (and, when present, the validated tenant via bot_tag is
    folded in by the caller). Thread-safe via a single lock; timestamps older
    than the window are evicted lazily on each check so memory tracks only
    recently-active keys.
    """

    _WINDOW_S = 60.0

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, now: float | None = None) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds). retry_after is 0 when allowed."""
        if limit <= 0:
            return True, 0
        ts = now if now is not None else time.monotonic()
        cutoff = ts - self._WINDOW_S
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                # Retry after the oldest hit ages out of the window.
                retry_after = max(1, int(dq[0] + self._WINDOW_S - ts) + 1)
                return False, retry_after
            dq.append(ts)
            if not dq:
                self._hits.pop(key, None)
            return True, 0

    def reset(self) -> None:
        """Clear all state (test helper)."""
        with self._lock:
            self._hits.clear()


class _ConcurrencyGate:
    """Loop-agnostic in-flight counter guarded by a threading.Lock."""

    def __init__(self) -> None:
        self._inflight = 0
        self._lock = threading.Lock()

    def acquire(self, limit: int) -> bool:
        if limit <= 0:
            return True
        with self._lock:
            if self._inflight >= limit:
                return False
            self._inflight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1

    def reset(self) -> None:
        with self._lock:
            self._inflight = 0


_rate_limiter = _SlidingWindowRateLimiter()
_concurrency_gate = _ConcurrencyGate()


def _client_key(request: Request) -> str:
    """Derive the throttle key: validated tenant id if available, else client IP.

    The token's validated `tid` (set by the auth middleware on request.state)
    is preferred so a single tenant cannot be throttled by another's traffic and
    so NAT'd clients sharing an IP are not collapsed. Falls back to the peer IP."""
    tid = getattr(request.state, "tid", None) or getattr(request.state, "tenant_id", None)
    if tid:
        return f"tid:{tid}"
    client = request.client
    return f"ip:{client.host}" if client and client.host else "ip:unknown"


def rate_limit_dependency(request: Request) -> None:
    """FastAPI dependency: enforce per-key sliding-window rate limiting.

    Raises 429 (INVALID_REQUEST envelope) with a `Retry-After` header when the
    caller exceeds `QNA_RATE_LIMIT_PER_MIN` within the 60s window."""
    key = _client_key(request)
    allowed, retry_after = _rate_limiter.check(key, _rate_limit_per_min())
    if not allowed:
        log_event(
            logger,
            "rate_limited",
            request_id=getattr(request.state, "request_id", None),
            level=logging.WARNING,
            retry_after_s=retry_after,
        )
        raise_api_error(
            ApiErrorCode.INVALID_REQUEST,
            "Rate limit exceeded. Retry after the indicated interval.",
            429,
            headers={"Retry-After": str(retry_after)},
        )


async def concurrency_gate_dependency(request: Request):
    """FastAPI yield-dependency: cap simultaneous in-flight requests.

    Acquires a slot before the handler runs and releases it after the response
    is produced (the `finally` runs post-yield). Returns 429 with a small
    `Retry-After` when the in-flight cap is reached."""
    if not _concurrency_gate.acquire(_max_concurrency()):
        log_event(
            logger,
            "concurrency_rejected",
            request_id=getattr(request.state, "request_id", None),
            level=logging.WARNING,
        )
        raise_api_error(
            ApiErrorCode.INVALID_REQUEST,
            "Server is at capacity. Retry shortly.",
            429,
            headers={"Retry-After": "1"},
        )
    try:
        yield
    finally:
        _concurrency_gate.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifecycle: startup and shutdown.

    This context manager delegates to `startup_event` and `shutdown_event`
    defined in `src.lifecycle`. It ensures that startup completes before the
    application begins serving, and that shutdown always runs even if an
    exception occurs during request handling.

    Args:
        app: FastAPI application instance.

    Yields:
        None. Control returns to FastAPI once startup is complete.
    """
    await startup_event(app)
    try:
        yield
    finally:
        await shutdown_event(app)


# ---------------------------------------------------------------------------
# FastAPI application setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="TocDoc QnA",
    version="1.0.0",
    root_path="/qna",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

_cors_raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()] if _cors_raw.strip() else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Authorization middleware.

    Delegates authentication/authorization to `AuthUtils.auth_middleware`.
    Pre-flights (OPTIONS) are passed through unmodified.

    Args:
        request: Incoming HTTP request.
        call_next: Next ASGI callable in the middleware stack.

    Returns:
        The downstream response object.
    """
    # Preserve preflight behavior; do not block CORS preflight requests.
    if request.method == "OPTIONS":
        return await call_next(request)
    return await AuthUtils.auth_middleware(request, call_next)


# Request-ID / correlation middleware. Registered LAST so it becomes the
# OUTERMOST layer in Starlette's stack — that way it runs first on incoming
# requests and `request.state.request_id` is set before auth runs (auth-failure
# logs can include the request_id).
app.add_middleware(RequestIDMiddleware)

# Structured error contract (P0-6). Installs three handlers:
# - HTTPException → ErrorEnvelope (back-compat with string-detail callsites)
# - RequestValidationError → 422 ErrorEnvelope with structured `errors` list
# - Exception (catch-all) → 500 ErrorEnvelope with X-Request-ID header
# All error responses include `request_id` in the body AND `X-Request-ID` header.
# New code should `raise_api_error(code, message, status_code)` from
# `src.core.errors` rather than `HTTPException(status, detail="msg")` so the
# `code` field stays meaningful.
register_exception_handlers(app)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    """
    Liveness/readiness probe.

    Returns a simple JSON payload indicating service status and whether the
    `generate_answer` entrypoint is available in `src.qna_pipeline`.

    Returns:
        dict: Health status with timestamp if available.
    """
    try:
        if hasattr(src.pipeline.qna_pipeline, "generate_answer"):
            return {
                "status": "ok",
                "qna_module": "loaded",
                "timestamp": datetime.now().isoformat(),
            }
        # Unhealthy: return HTTP 503 so Kubernetes/Azure probes (which key on
        # the status code, not the body) pull the instance from rotation
        # (audit L-Q5). Body shape is preserved for existing monitors.
        return JSONResponse(
            status_code=503,
            content={"status": "error", "qna_module": "missing generate_answer function"},
        )
    except Exception as e:
        # Unhealthy branch — return 503 so probes act on it (audit L-Q5) while
        # keeping the {status, qna_module} body shape stable for monitors.
        # Do NOT echo `str(e)` — exception text can leak internal detail
        # (CodeQL py/stack-trace-exposure). Log the exception class server-side
        # and return a fixed, safe message — same pattern as the auth
        # middleware (src/core/auth.py).
        logger.error("Health check failed: %s", type(e).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "qna_module": "unavailable"},
        )


# ---------------------------------------------------------------------------
# QnA endpoint
# ---------------------------------------------------------------------------
@app.post(
    "/qna",
    response_model=QnASuccessResponse,
    # Keep the wire payload byte-identical to the historical `{answer, citation}`
    # shape: drop the model's defensive optional fields (request_id/error) so
    # they never serialize as `null` on the success path.
    response_model_exclude_none=True,
    responses=default_error_responses,
    # Throttle the expensive endpoint (audit M7): per-key sliding-window rate
    # limit + a global in-flight concurrency cap, both returning 429 +
    # Retry-After. Dependencies run before the handler body; the concurrency
    # gate releases its slot after the response (yield-dependency `finally`).
    dependencies=[Depends(rate_limit_dependency), Depends(concurrency_gate_dependency)],
)
async def custom_rag_qna(payload: Payload, request: Request):
    """
    QnA handler endpoint.

    Accepts a `Payload` (see `src.utils.util.Payload`) and uses the normalized
    conversation history to invoke `src.pipeline.qna_pipeline.generate_answer`.

    Behavior:
        - Validates presence of required fields (query, bot_tag, fr_tag).
        - Normalizes history via `_as_turn` and passes it explicitly to
          `generate_answer()` as a `history` parameter.
        - Passes `bot_tag` explicitly so the search layer enforces tenant isolation.
        - Calls the pipeline to obtain an answer and returns it verbatim.

    Error contract (P0-6): all 4xx/5xx responses follow the `ErrorEnvelope`
    shape with a stable `code` field. Internal pipeline failures now produce
    a 500 with `code=INTERNAL_ERROR` — previously they leaked a 200 response
    containing an `error` field. See `src.core.errors`.

    Raises:
        HTTPException: 400 on user errors; the global handler envelopes it.
    """
    request_id = getattr(request.state, "request_id", None) or f"qna_{int(time.time() * 1000)}"

    # Retrieve activated Azure clients (OpenAI, Embeddings, AI Search).
    azure = request.app.state.azure

    # Basic payload presence validation (shape checked later).
    if payload is None:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Missing request body", 400)

    bot_tag = (payload.bot_tag or "").strip()
    fr_tag = (payload.fr_tag or "").strip()

    # Normalize history to a stable shape; ignore None entries defensively.
    raw_history = payload.bot or []
    history = [_as_turn(t) for t in raw_history if t is not None]

    if not history:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Bot list cannot be empty", 400)

    query = history[-1]["user_query"]

    # Metadata-only request log (audit L-Q6). NEVER log the raw user query —
    # it routinely carries PII/confidential content and persists in log sinks.
    # bot_tag/fr_tag are bounded identifiers and safe to log. A short query
    # preview is emitted only when QNA_DEBUG_LOG_PREVIEW is explicitly enabled
    # (off by default), routed through log_event so truncation applies.
    _query_preview = None
    if os.getenv("QNA_DEBUG_LOG_PREVIEW", "").lower() in ("1", "true", "yes"):
        _query_preview = (query or "")[:200]
    log_event(
        logger,
        "qna_request_received",
        request_id=request_id,
        query_length=len(query or ""),
        bot_tag=bot_tag,
        fr_tag=fr_tag,
        query_preview=_query_preview,
    )

    # Required field validations (kept explicit for clear 400s).
    if not query:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Query cannot be empty", 400)
    if not bot_tag:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Bot tag cannot be empty", 400)
    if not fr_tag:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "FR tag cannot be empty", 400)

    # Allow-list fr_tag at the request boundary (audit L-Q3) so the agentic
    # map-reduce path and the legacy pipeline converge on the same validation
    # instead of relying on the legacy path's later check. Reject unknown modes
    # with a 400 before any retrieval / agent fork runs.
    if fr_tag not in _ALLOWED_FR_TAGS:
        raise_api_error(
            ApiErrorCode.INVALID_REQUEST,
            f"Invalid fr_tag. Must be one of: {', '.join(_ALLOWED_FR_TAGS)}.",
            400,
        )

    # Within-tenant bot_tag<->tid binding guard (threat-model R1), DEFAULT-OFF.
    # When QNA_ENFORCE_TENANT_BINDING is unset/falsy this is fully inert — zero
    # behaviour change. When ON it validates the requested bot_tag against the
    # allowlist for the token's validated `tid` and fails closed (envelope 403,
    # no search) on any mismatch. Placed here — after the non-empty field checks
    # and before the agent/legacy fork — so it guards both QnA paths and rejects
    # before any retrieval. See src/core/tenant_binding.py.
    enforce_tenant_bot_tag_binding(request, bot_tag)

    start = time.time()

    # P3 dark seam (default-OFF). When QNA_AGENT_ENABLED is unset/falsy the
    # legacy direct call below runs verbatim — byte-identical behaviour and
    # the #28 CitationMap contract hold. When ON, the LangGraph agentic layer
    # handles the request and returns the SAME {answer, citation} shape. The
    # router is imported lazily inside the ON branch so the off-path import
    # surface (and thus its behaviour) is literally unchanged and cannot break
    # on a langgraph import error. The flag is read per-request (no redeploy
    # to flip the kill-switch). See docs/architect_phase_2/07_P3_LANGGRAPH_ADR.md.
    #
    # No local exception swallowing on either path — any pipeline/node failure
    # propagates to the global handler, which produces an envelope-shaped 500
    # with X-Request-ID (the P0-6 contract). The previous
    # `raise HTTPException(500, f"QnA processing failed: {e}")` leaked exception
    # text to the client; this path no longer does.
    if is_agent_enabled():
        logger.info(f"[{request_id}] Calling agents.router.agentic_generate_answer...")
        from src.agents.router import agentic_generate_answer

        ans = await agentic_generate_answer(
            query,
            fr_tag,
            bot_tag=bot_tag,
            history=history,
            azure=azure,
            request_id=getattr(request.state, "request_id", None),
        )
    else:
        logger.info(f"[{request_id}] Calling qna.generate_answer...")
        ans = await src.pipeline.qna_pipeline.generate_answer(
            query=query,
            fr_mode=fr_tag,
            bot_tag=bot_tag,
            history=history,
            azure=azure,
            # Thread the middleware correlation ID so pipeline stage events
            # share the same request_id as request_started/request_completed.
            request_id=getattr(request.state, "request_id", None),
        )

    elapsed = time.time() - start
    logger.info(f"[{request_id}] QnA processing completed in {elapsed:.4f}s")
    logger.info(f"[{request_id}] Response generated successfully")
    return ans


# ---------------------------------------------------------------------------
# SSE streaming QnA endpoint (/qna/stream)
# ---------------------------------------------------------------------------
# Wire format (matches the SDK's tocdoc_sdk._sse parser):
#   - Each answer token is one event: `data: <token>\n\n`.
#   - The final citation map is one event tagged `event: citation`:
#       `event: citation\ndata: <json>\n\n`
#   - The stream ends with the OpenAI-style sentinel: `data: [DONE]\n\n`.
#   - A mid-stream failure (after the first token, when no error envelope can be
#     sent) emits a terminal `event: error\ndata: <json>\n\n` then `[DONE]`.
# The SDK parser ignores the `event:` field and yields each `data:` payload, so a
# consumer sees: token, token, ..., <citation-json>, then stops at [DONE].

# Heartbeat suffix per SSE spec — a blank line terminates each event.
_SSE_DONE = "data: [DONE]\n\n"


def _sse_event(data: str, event: str | None = None) -> str:
    """Frame one SSE event. Multi-line ``data`` is split into ``data:`` lines."""
    lines = []
    if event is not None:
        lines.append(f"event: {event}")
    for chunk in data.split("\n"):
        lines.append(f"data: {chunk}")
    return "\n".join(lines) + "\n\n"


@app.post(
    "/qna/stream",
    responses=default_error_responses,
    # SAME throttling as /qna: per-key sliding-window rate limit + global
    # in-flight concurrency cap. The concurrency gate is a yield-dependency whose
    # `finally` releases the slot when the request scope is torn down — i.e.
    # after the StreamingResponse body has been fully sent, so the slot is not
    # leaked across a stream (covered by
    # test_stream_concurrency_slot_released_after_stream).
    dependencies=[Depends(rate_limit_dependency), Depends(concurrency_gate_dependency)],
)
async def custom_rag_qna_stream(payload: Payload, request: Request):
    """Server-Sent-Events streaming variant of ``/qna``.

    Reuses the SAME auth (RS256 JWT middleware), tenant-binding enforcement,
    bot_tag/fr_tag validation, rate limiting, and retrieval as ``/qna`` — only
    the answer-generation step streams token-by-token.

    Error contract:
        - Any failure BEFORE the first token (validation, tenant-binding,
          rephrasal, embedding, search) is raised here and returned as the
          normal structured error envelope — no stream is opened.
        - A failure AFTER streaming has begun (headers already sent) cannot use
          the envelope, so it emits a terminal ``event: error`` SSE event
          followed by ``[DONE]``.

    The agentic dark seam is intentionally NOT consulted here: streaming applies
    to the standard route only.
    """
    request_id = getattr(request.state, "request_id", None) or f"qna_{int(time.time() * 1000)}"
    azure = request.app.state.azure

    if payload is None:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Missing request body", 400)

    bot_tag = (payload.bot_tag or "").strip()
    fr_tag = (payload.fr_tag or "").strip()

    raw_history = payload.bot or []
    history = [_as_turn(t) for t in raw_history if t is not None]
    if not history:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Bot list cannot be empty", 400)

    query = history[-1]["user_query"]

    _query_preview = None
    if os.getenv("QNA_DEBUG_LOG_PREVIEW", "").lower() in ("1", "true", "yes"):
        _query_preview = (query or "")[:200]
    log_event(
        logger,
        "qna_stream_request_received",
        request_id=request_id,
        query_length=len(query or ""),
        bot_tag=bot_tag,
        fr_tag=fr_tag,
        query_preview=_query_preview,
    )

    # Identical request-boundary validations to /qna (kept explicit for 400s).
    if not query:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Query cannot be empty", 400)
    if not bot_tag:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Bot tag cannot be empty", 400)
    if not fr_tag:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "FR tag cannot be empty", 400)
    if fr_tag not in _ALLOWED_FR_TAGS:
        raise_api_error(
            ApiErrorCode.INVALID_REQUEST,
            f"Invalid fr_tag. Must be one of: {', '.join(_ALLOWED_FR_TAGS)}.",
            400,
        )

    # Within-tenant bot_tag<->tid binding guard — same call-site position as
    # /qna (before any retrieval). Fails closed via the 403 envelope when ON.
    enforce_tenant_bot_tag_binding(request, bot_tag)

    # Prime the pipeline generator BEFORE returning the StreamingResponse so the
    # "errors before the first token" half of the contract holds: rephrasal,
    # embedding, search, AND the first streamed token all run on this first
    # `__anext__`. Any failure here is raised synchronously (relative to the
    # handler) and returned as the normal structured error envelope — no stream
    # is opened, no partial response is sent. Only failures AFTER this first
    # item become mid-stream terminal error events.
    stream = src.pipeline.qna_pipeline.generate_answer_stream(
        query=query,
        fr_mode=fr_tag,
        bot_tag=bot_tag,
        history=history,
        azure=azure,
        request_id=request_id,
    )
    try:
        first_item = await stream.__anext__()
    except StopAsyncIteration:
        first_item = None
    # Note: any other exception propagates to the global handler -> envelope.

    async def event_source():
        try:
            if first_item is not None:
                kind, payload_value = first_item
                if kind == "token":
                    yield _sse_event(payload_value)
                elif kind == "citation":
                    yield _sse_event(json.dumps(payload_value), event="citation")
            async for kind, payload_value in stream:
                if kind == "token":
                    yield _sse_event(payload_value)
                elif kind == "citation":
                    yield _sse_event(json.dumps(payload_value), event="citation")
        except Exception as e:
            # Mid-stream failure: headers are already sent, so we cannot emit the
            # structured envelope. Surface a terminal error event (no raw
            # exception text — same hygiene as the envelope path) then [DONE].
            logger.error(f"[{request_id}] Mid-stream failure: {type(e).__name__}")
            error_payload = {
                "error": {
                    "code": ApiErrorCode.INTERNAL_ERROR,
                    "message": "Streaming failed",
                    "request_id": request_id,
                }
            }
            yield _sse_event(json.dumps(error_payload), event="error")
        finally:
            yield _SSE_DONE

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-ID": request_id,
        },
    )


# ---------------------------------------------------------------------------
# Root endpoint (service metadata)
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    """
    Root endpoint with basic service metadata and helpful links.
    """
    logger.info("Root endpoint accessed")
    return {
        "message": "QnA API Service",
        "version": "1.0.0",
        "docs": "/api/v1/docs",  # Note: see suggestion below re: actual docs_url
        "health": "/health",
    }
