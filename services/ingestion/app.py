import logging
import os
import sys
from contextlib import asynccontextmanager

import custom_rag
from admin.routes import router as admin_router
from errors import default_error_responses, register_exception_handlers
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from middleware import limit_upload_size
from observability import RequestIDMiddleware
from path_safety import resolve_upload_path

# ── Logging ───────────────────────────────────────────────────────────────────
# Stdout always; file logging only if LOG_FILE env var is set (local dev)
_log_handlers = [logging.StreamHandler(sys.stdout)]
_log_file = os.getenv("LOG_FILE")
if _log_file:
    _log_handlers.append(logging.FileHandler(_log_file))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=_log_handlers,
)

logger = logging.getLogger(__name__)

# ── RAG instance (module-level singleton) ─────────────────────────────────────
rag_instance = custom_rag.rag()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("TocDoc Ingestion Service starting up")
    logger.info(f"RAG instance type: {type(rag_instance).__name__}")
    yield
    logger.info("TocDoc Ingestion Service shutting down")


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="TocDoc – Enterprise RAG | Ingestion Service",
    description="Document ingestion pipeline: parse PDFs via Azure Document Intelligence, chunk, embed, and index into Azure Cognitive Search.",
    version="1.0.0",
    root_path="/upload_pipeline",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS: read from env, default to empty (restrictive) in production
# In local dev: set CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:5500
_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()] if _cors_origins_raw else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    # X-Admin-Token: header used by the interim admin auth guard (see admin/auth.py).
    #   Required for browser-based admin tooling to clear CORS preflight.
    # X-Request-ID: client-supplied correlation ID echoed back in responses
    #   by RequestIDMiddleware (see observability.py).
    allow_headers=["Authorization", "Content-Type", "X-Admin-Token", "X-Request-ID"],
)

# Admin API (read-only in PR-1; destructive endpoints follow in a later PR).
# Auth is enforced inside the router via the require_admin_token dependency.
app.include_router(admin_router, prefix="/admin")


# `limit_upload_size` is defined in `middleware.py` so it can be imported by
# tests without dragging in `custom_rag`'s heavy deps (PyMuPDF, langchain).
# Registering via the decorator factory keeps the FastAPI semantics identical
# to a top-level `@app.middleware("http")` decoration.
app.middleware("http")(limit_upload_size)


# Request-ID / correlation middleware. Registered LAST so it becomes the
# OUTERMOST layer in Starlette's stack — runs first on requests so
# `request.state.request_id` is available to all downstream middleware,
# including future auth.
app.add_middleware(RequestIDMiddleware)

# Structured error contract (P0-6). Installs three handlers:
# - HTTPException → ErrorEnvelope (back-compat with string-detail callsites)
# - RequestValidationError → 422 ErrorEnvelope with structured `errors` list
# - Exception (catch-all) → 500 ErrorEnvelope with X-Request-ID header
# New code should `raise_api_error(code, message, status_code)` from `errors`
# rather than `HTTPException(status, detail="msg")` so the `code` field stays meaningful.
register_exception_handlers(app)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health", summary="Liveness probe")
async def health_check():
    """Returns service health status."""
    logger.info("Health check requested")
    return {"status": "healthy"}


@app.post("/upload", summary="Ingest a PDF document or folder", responses=default_error_responses)
async def upload_file(
    request: Request,
    bot_tag: str = Query(..., description="Tenant / bot identifier"),
    filepath: str = Query(..., description="Absolute file or folder path on the server"),
    fr_mode: str = Query(
        "read",
        regex="^(read|layout)$",
        description="Azure Document Intelligence model: 'read' (token-chunked) or 'layout' (header-split)",
    ),
    file: UploadFile | None = File(None),
):
    """
    Ingest one PDF or an entire folder of PDFs into the Azure Cognitive Search index.

    - **bot_tag**: Logical tenant identifier stored on every indexed chunk.
    - **filepath**: Server-side path. Pass a directory to process all PDFs recursively.
    - **fr_mode**: `read` uses token-based chunking (500 tokens, 50 overlap).
      `layout` uses Markdown-header splitting, preserving document structure.
    - **file**: Required when `filepath` points to a single file uploaded by the client.
    """
    logger.info(f"Upload request — bot_tag: {bot_tag!r}, filepath: {filepath!r}, fr_mode: {fr_mode!r}")

    # Correlation ID set by RequestIDMiddleware; threaded into the ingestion
    # pipeline so stage events share the request lifecycle's request_id.
    request_id = getattr(request.state, "request_id", None)

    # Containment guard (CodeQL py/path-injection): resolve `filepath` against a
    # configured allowed root and reject traversal/absolute escapes with the
    # structured error envelope. All path sinks below operate on the validated,
    # realpath-resolved value rather than the raw query param.
    safe_filepath = resolve_upload_path(filepath)

    # ── Folder batch mode ─────────────────────────────────────────────────────
    if os.path.isdir(safe_filepath):
        logger.info(f"Folder upload mode — scanning: {safe_filepath!r}")

        pdf_files = []
        for root, _dirs, files in os.walk(safe_filepath):
            for f in files:
                if f.lower().endswith(".pdf"):
                    pdf_files.append(os.path.join(root, f))

        logger.info(f"Found {len(pdf_files)} PDF file(s) to process")

        results = []
        for i, file_path in enumerate(pdf_files, 1):
            basename = os.path.basename(file_path)
            logger.info(f"Processing {i}/{len(pdf_files)}: {basename!r}")
            try:

                class _MockFile:
                    def __init__(self, path: str):
                        self.file_path = path
                        self.filename = os.path.basename(path)

                    async def read(self) -> bytes:
                        with open(self.file_path, "rb") as fh:
                            return fh.read()

                result = await rag_instance.upload(
                    _MockFile(file_path), bot_tag, fr_mode, file_path, request_id=request_id
                )
                results.append({"file": basename, "status": "success", "result": result})
                logger.info(f"Successfully processed: {basename!r}")
            except Exception as e:
                # Do not echo raw exception text to the client (CodeQL
                # py/stack-trace-exposure). Log the detail server-side; return a
                # generic message. Keep dict keys identical to preserve shape.
                logger.error(f"Failed to process {basename!r}: {type(e).__name__}", exc_info=True)
                results.append({"file": basename, "status": "error", "error": "Failed to process file."})

        return results

    # ── Single-file mode ──────────────────────────────────────────────────────
    if file is None:
        raise HTTPException(
            status_code=400,
            detail="A file upload is required when filepath does not point to a directory.",
        )

    logger.info(
        f"Single-file upload — filename: {file.filename!r}, "
        f"content_type: {file.content_type}, size: {file.size} bytes"
    )

    try:
        if file.size and file.size > 100 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large. Maximum size is 100 MB.")

        result = await rag_instance.upload(file, bot_tag, fr_mode, filepath, request_id=request_id)
        logger.info(f"Upload completed successfully: {result}")
        return {"status": "successfully indexed", "detail": result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during upload: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ingestion service unavailable.") from e


@app.get("/", summary="Service metadata")
async def root():
    return {
        "service": "TocDoc Ingestion Service",
        "version": "1.0.0",
        "docs": "/upload_pipeline/docs",
        "health": "/upload_pipeline/health",
    }
