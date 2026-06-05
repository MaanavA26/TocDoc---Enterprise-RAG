import logging
import os
import sys
from contextlib import asynccontextmanager

import custom_rag
from admin.auth import require_admin_token
from admin.routes import router as admin_router
from errors import (
    ApiErrorCode,
    default_error_responses,
    raise_api_error,
    register_exception_handlers,
)
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from loaders import SUPPORTED_EXTENSIONS, is_supported_name
from middleware import limit_upload_size
from observability import RequestIDMiddleware
from path_safety import resolve_upload_path
from starlette.responses import JSONResponse

# ── Per-file / per-batch upload ceilings ──────────────────────────────────────
# 100 MB per file, enforced on actual bytes (L-X2): the declared
# UploadFile.size can be None, so we measure the real stream length rather than
# trusting it. Folder mode applies the same ceiling per file via os.path.getsize
# and caps the number of files processed in one request (L-X4) so a large
# directory cannot drive an unbounded, worker-blocking ingestion.
_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_FOLDER_FILES = int(os.getenv("INGESTION_MAX_FOLDER_FILES", "500"))

# ── /upload concurrency cap (M7) ──────────────────────────────────────────────
# A lightweight in-process concurrency limiter: each /upload acquires a slot and
# releases it in `finally`. When all slots are taken, additional requests get a
# 429 + Retry-After instead of piling onto the (expensive) DI + embedding + index
# path. Implemented as a non-blocking counter (no extra dependency) so it is
# trivially testable: set INGESTION_MAX_CONCURRENT_UPLOADS=1 and fire two
# concurrent calls. The check-and-decrement in `_try_acquire_upload_slot` runs to
# completion without an `await`, so the single-threaded event loop makes it
# atomic — no lock needed for this in-process counter.
_MAX_CONCURRENT_UPLOADS = int(os.getenv("INGESTION_MAX_CONCURRENT_UPLOADS", "4"))
_active_uploads = 0


def _try_acquire_upload_slot() -> bool:
    """Reserve one upload slot if available. Returns False when at capacity."""
    global _active_uploads
    if _active_uploads >= _MAX_CONCURRENT_UPLOADS:
        return False
    _active_uploads += 1
    return True


def _release_upload_slot() -> None:
    global _active_uploads
    _active_uploads = max(0, _active_uploads - 1)


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


def _upload_degraded(result) -> bool:
    """True if rag.upload() reported a partial/degraded index write (H3).

    upload() returns a stats dict with status "degraded" (and failed_chunks)
    when some chunks failed to index. The route MUST NOT mask that as success.
    """
    return isinstance(result, dict) and result.get("status") == "degraded"


def _is_within_root(path: str, root: str) -> bool:
    """True iff the realpath of `path` is `root` or strictly beneath it.

    L-Ing2: `resolve_upload_path` only validates the top-level folder; each file
    collected by `os.walk` must be re-validated before `open()` because a symlink
    inside the root can point out-of-root (another tenant's data, /etc/passwd).
    We reject symlinks outright AND require the resolved path to stay contained.
    """
    if os.path.islink(path):
        return False
    real = os.path.realpath(path)
    try:
        return os.path.commonpath([root, real]) == root
    except ValueError:
        return False


# bot_tag pattern mirrors admin.routes.BOT_TAG_PATTERN. Enforcing it at the
# route (422 on a non-match) is the C1 route-side defense: a payload like
# `x' or bot_tag ne 'zz` carries a quote/space and is rejected before any
# pipeline call. The sink in custom_rag also OData-escapes (defense in depth).
_BOT_TAG_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


@app.post(
    "/upload",
    summary="Ingest a document (PDF/DOCX/PPTX/HTML/MD/TXT) or folder",
    responses=default_error_responses,
    # H2: /upload was unauthenticated. Apply the same admin-token dependency
    # that guards /admin/* so the endpoint requires a valid X-Admin-Token.
    dependencies=[Depends(require_admin_token)],
)
async def upload_file(
    request: Request,
    bot_tag: str = Query(..., pattern=_BOT_TAG_PATTERN, description="Tenant / bot identifier"),
    filepath: str = Query(..., description="Absolute file or folder path on the server"),
    fr_mode: str = Query(
        "read",
        pattern="^(read|layout)$",
        description="Azure Document Intelligence model: 'read' (token-chunked) or 'layout' (header-split)",
    ),
    file: UploadFile | None = File(None),
):
    """
    Ingest one document (PDF/DOCX/PPTX/HTML/MD/TXT) or an entire folder of them
    into the Azure Cognitive Search index.

    PDFs are parsed via Azure Document Intelligence; the other formats are
    extracted to plain text by the loader registry. All formats then feed the
    same chunk → embed → index pipeline.

    - **bot_tag**: Logical tenant identifier stored on every indexed chunk.
    - **filepath**: Server-side path. Pass a directory to process all supported
      documents recursively (unsupported file types are skipped).
    - **fr_mode**: `read` uses token-based chunking (500 tokens, 50 overlap).
      `layout` uses Markdown-header splitting, preserving document structure
      (most useful for PDFs and Markdown).
    - **file**: Required when `filepath` points to a single file uploaded by the
      client. An unsupported file type returns HTTP 415.
    """
    logger.info(f"Upload request — bot_tag: {bot_tag!r}, filepath: {filepath!r}, fr_mode: {fr_mode!r}")

    # M7: bound concurrency on this expensive (DI + embedding + index) endpoint.
    # Try to acquire a slot without blocking; if none is free, reject with
    # 429 + Retry-After rather than queueing — this caps the worst-case Azure
    # spend / executor pressure a single bursty client can drive. The slot is
    # released in `finally` so a failed upload never leaks capacity.
    if not _try_acquire_upload_slot():
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent uploads. Retry shortly.",
            headers={"Retry-After": "5"},
        )

    try:
        # Correlation ID set by RequestIDMiddleware; threaded into the ingestion
        # pipeline so stage events share the request lifecycle's request_id.
        request_id = getattr(request.state, "request_id", None)

        # Containment guard (CodeQL py/path-injection): resolve `filepath` against
        # a configured allowed root and reject traversal/absolute escapes with the
        # structured error envelope. All path sinks below operate on the
        # validated, realpath-resolved value rather than the raw query param.
        safe_filepath = resolve_upload_path(filepath)

        # ── Folder batch mode ──────────────────────────────────────────────────
        if os.path.isdir(safe_filepath):
            logger.info(f"Folder upload mode — scanning: {safe_filepath!r}")
            allowed_root = os.path.realpath(safe_filepath)

            # Collect every supported document (PDF + DOCX/PPTX/HTML/MD/TXT).
            # Files with an unsupported extension are simply not collected — an
            # unknown type is a clean skip in folder mode, never a 500.
            doc_files = []
            for root, _dirs, files in os.walk(safe_filepath):
                for f in files:
                    if is_supported_name(f):
                        doc_files.append(os.path.join(root, f))

            logger.info(f"Found {len(doc_files)} supported file(s) to process")

            # L-X4: cap the number of files processed per request so a large
            # directory cannot drive an unbounded, worker-blocking ingestion run.
            if len(doc_files) > _MAX_FOLDER_FILES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Too many files in folder ({len(doc_files)}). Maximum is {_MAX_FOLDER_FILES} per request.",
                )

            results = []
            for i, file_path in enumerate(doc_files, 1):
                basename = os.path.basename(file_path)
                logger.info(f"Processing {i}/{len(doc_files)}: {basename!r}")

                # L-Ing2: re-validate each collected file before opening. os.walk
                # lists symlinked regular files, so a symlink inside the root
                # pointing out-of-root would otherwise be read and ingested.
                if not _is_within_root(file_path, allowed_root):
                    logger.warning(f"Skipping file outside allowed root or symlink: {basename!r}")
                    results.append({"file": basename, "status": "error", "error": "Failed to process file."})
                    continue

                # L-X4 / L-X2: enforce the per-file byte ceiling in folder mode
                # too (the prior 100 MB check was single-file-only).
                try:
                    if os.path.getsize(file_path) > _MAX_FILE_BYTES:
                        logger.warning(f"Skipping oversize file: {basename!r}")
                        results.append(
                            {"file": basename, "status": "error", "error": "Failed to process file."}
                        )
                        continue
                except OSError:
                    results.append({"file": basename, "status": "error", "error": "Failed to process file."})
                    continue

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
                    # H3: surface a partial index write rather than masking it.
                    file_status = "degraded" if _upload_degraded(result) else "success"
                    results.append({"file": basename, "status": file_status, "result": result})
                    logger.info(f"Processed {basename!r} — status={file_status}")
                except Exception as e:
                    # Do not echo raw exception text to the client (CodeQL
                    # py/stack-trace-exposure). Log the detail server-side; return
                    # a generic message. Keep dict keys identical to preserve shape.
                    logger.error(f"Failed to process {basename!r}: {type(e).__name__}", exc_info=True)
                    results.append({"file": basename, "status": "error", "error": "Failed to process file."})

            return results

        # ── Single-file mode ───────────────────────────────────────────────────
        if file is None:
            raise HTTPException(
                status_code=400,
                detail="A file upload is required when filepath does not point to a directory.",
            )

        # Reject an unsupported file type BEFORE the pipeline so it returns a
        # clean 415, not a 500. `upload()` also guards (UnsupportedFormatError)
        # as defense-in-depth, but the generic `except Exception → 500` below
        # would otherwise mask it — so the route owns the 4xx decision.
        if not is_supported_name(file.filename or ""):
            raise_api_error(
                code=ApiErrorCode.INVALID_REQUEST,
                message=(
                    f"Unsupported file type. Supported extensions: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
                ),
                status_code=415,
            )

        logger.info(
            f"Single-file upload — filename: {file.filename!r}, "
            f"content_type: {file.content_type}, size: {file.size} bytes"
        )

        try:
            # L-X2: enforce the 100 MB ceiling on the ACTUAL stream length, not
            # the possibly-None declared `file.size`. Seek to end / tell / rewind
            # works even when the multipart parser did not populate `.size`.
            file.file.seek(0, os.SEEK_END)
            actual_size = file.file.tell()
            file.file.seek(0)
            if actual_size > _MAX_FILE_BYTES:
                raise HTTPException(status_code=413, detail="File too large. Maximum size is 100 MB.")

            result = await rag_instance.upload(file, bot_tag, fr_mode, filepath, request_id=request_id)

            # H3: if the index write was partial, do NOT report success. Return
            # HTTP 207 (Multi-Status) with a degraded headline so a client can
            # detect the incomplete write instead of trusting "successfully
            # indexed". The full per-chunk detail (failed_chunks/failed_keys) is
            # carried in `detail`.
            if _upload_degraded(result):
                logger.warning(f"Upload completed with partial failure: {result}")
                return JSONResponse(
                    status_code=207,
                    content={"status": "partially indexed", "detail": result},
                )

            logger.info(f"Upload completed successfully: {result}")
            return {"status": "successfully indexed", "detail": result}

        except HTTPException:
            raise
        except Exception as e:
            # L-X1: log the exception CLASS only — never str(e), which may carry
            # file paths / connection detail into log sinks.
            logger.error(f"Unexpected error during upload: {type(e).__name__}", exc_info=True)
            raise HTTPException(status_code=500, detail="Ingestion service unavailable.") from e
    finally:
        _release_upload_slot()


@app.get("/", summary="Service metadata")
async def root():
    return {
        "service": "TocDoc Ingestion Service",
        "version": "1.0.0",
        "docs": "/upload_pipeline/docs",
        "health": "/upload_pipeline/health",
    }
