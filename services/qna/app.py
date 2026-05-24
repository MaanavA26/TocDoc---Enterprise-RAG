import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from src.core.lifecycle import startup_event, shutdown_event
import src.pipeline.qna_pipeline
import logging
import time
from datetime import datetime
from src.utils.util import Payload, _as_turn
from src.core.auth import AuthUtils
from src.core.observability import RequestIDMiddleware
from src.core.errors import (
    register_exception_handlers,
    default_error_responses,
    raise_api_error,
    ApiErrorCode,
)


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
        return {"status": "error", "qna_module": "missing generate_answer function"}
    except Exception as e:
        # Keep shape stable for external monitors; surface error as string.
        return {"status": "error", "qna_module": str(e)}


# ---------------------------------------------------------------------------
# QnA endpoint
# ---------------------------------------------------------------------------
@app.post("/qna", responses=default_error_responses)
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

    logger.info(f"[{request_id}] QnA request received")
    logger.info(f"[{request_id}] Query: {query!r}")
    logger.info(f"[{request_id}] Bot tag: {bot_tag!r}")
    logger.info(f"[{request_id}] FR tag: {fr_tag!r}")

    # Required field validations (kept explicit for clear 400s).
    if not query:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Query cannot be empty", 400)
    if not bot_tag:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "Bot tag cannot be empty", 400)
    if not fr_tag:
        raise_api_error(ApiErrorCode.INVALID_REQUEST, "FR tag cannot be empty", 400)

    start = time.time()
    logger.info(f"[{request_id}] Calling qna.generate_answer...")

    # No local exception swallowing — any pipeline failure propagates to the
    # global handler, which produces an envelope-shaped 500 with X-Request-ID.
    # The previous `raise HTTPException(500, f"QnA processing failed: {e}")`
    # leaked exception text to the client; this path no longer does.
    ans = await src.pipeline.qna_pipeline.generate_answer(
        query=query,
        fr_mode=fr_tag,
        bot_tag=bot_tag,
        history=history,
        azure=azure,
    )

    elapsed = time.time() - start
    logger.info(f"[{request_id}] QnA processing completed in {elapsed:.4f}s")
    logger.info(f"[{request_id}] Response generated successfully")
    return ans


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