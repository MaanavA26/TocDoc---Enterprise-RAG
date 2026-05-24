from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
import custom_rag
import os
import logging
import sys
from typing import Optional

from observability import RequestIDMiddleware

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
    allow_methods=["GET", "POST", "OPTIONS"],
    # X-Request-ID: allow clients to pass a correlation ID through (browser
    # tooling needs it accepted by CORS preflight). The RequestIDMiddleware
    # below also echoes this header in every response.
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """Reject requests that exceed the configured upload size limit."""
    max_size = 300 * 1024 * 1024  # 300 MB

    if request.headers.get("content-length"):
        content_length = int(request.headers["content-length"])
        logger.info(f"Request content-length: {content_length} bytes")
        if content_length > max_size:
            logger.warning(
                f"Request too large: {content_length} bytes exceeds {max_size} bytes"
            )
            raise HTTPException(
                status_code=413,
                detail="File too large. Maximum size is 300 MB.",
            )

    return await call_next(request)


# Request-ID / correlation middleware. Registered LAST so it becomes the
# OUTERMOST layer in Starlette's stack — runs first on requests so
# `request.state.request_id` is available to all downstream middleware,
# including future auth.
app.add_middleware(RequestIDMiddleware)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", summary="Liveness probe")
async def health_check():
    """Returns service health status."""
    logger.info("Health check requested")
    return {"status": "healthy"}


@app.post("/upload", summary="Ingest a PDF document or folder")
async def upload_file(
    bot_tag: str = Query(..., description="Tenant / bot identifier"),
    filepath: str = Query(..., description="Absolute file or folder path on the server"),
    fr_mode: str = Query(
        "read",
        regex="^(read|layout)$",
        description="Azure Document Intelligence model: 'read' (token-chunked) or 'layout' (header-split)",
    ),
    file: Optional[UploadFile] = File(None),
):
    """
    Ingest one PDF or an entire folder of PDFs into the Azure Cognitive Search index.

    - **bot_tag**: Logical tenant identifier stored on every indexed chunk.
    - **filepath**: Server-side path. Pass a directory to process all PDFs recursively.
    - **fr_mode**: `read` uses token-based chunking (500 tokens, 50 overlap).
      `layout` uses Markdown-header splitting, preserving document structure.
    - **file**: Required when `filepath` points to a single file uploaded by the client.
    """
    logger.info(
        f"Upload request — bot_tag: {bot_tag!r}, filepath: {filepath!r}, fr_mode: {fr_mode!r}"
    )

    # ── Folder batch mode ─────────────────────────────────────────────────────
    if os.path.isdir(filepath):
        logger.info(f"Folder upload mode — scanning: {filepath!r}")

        pdf_files = []
        for root, _dirs, files in os.walk(filepath):
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

                result = await rag_instance.upload(_MockFile(file_path), bot_tag, fr_mode, file_path)
                results.append({"file": basename, "status": "success", "result": result})
                logger.info(f"Successfully processed: {basename!r}")
            except Exception as e:
                results.append({"file": basename, "status": "error", "error": str(e)})
                logger.error(f"Failed to process {basename!r}: {e}")

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

        result = await rag_instance.upload(file, bot_tag, fr_mode, filepath)
        logger.info(f"Upload completed successfully: {result}")
        return {"status": "successfully indexed", "detail": result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during upload: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ingestion service unavailable.")


@app.get("/", summary="Service metadata")
async def root():
    return {
        "service": "TocDoc Ingestion Service",
        "version": "1.0.0",
        "docs": "/upload_pipeline/docs",
        "health": "/upload_pipeline/health",
    }
