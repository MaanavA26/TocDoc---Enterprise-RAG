"""Standalone HTTP middleware for the ingestion service.

Lives outside `app.py` so it can be imported by tests without triggering
`custom_rag`'s heavy import chain (PyMuPDF, langchain, openai). Both
runtime (`app.py` mounts via `app.middleware("http")(limit_upload_size)`)
and tests import the same function.
"""

from __future__ import annotations

import logging

from errors import ApiErrorCode, build_error_response
from fastapi import Request

logger = logging.getLogger(__name__)

# 300 MB ceiling on the upload request size, applied BEFORE the body is
# read. Counterpart per-file ceiling in app.py /upload (100 MB) catches
# multipart bodies whose declared content-length is small but where the
# extracted file is still large.
MAX_UPLOAD_BYTES = 300 * 1024 * 1024


async def limit_upload_size(request: Request, call_next):
    """Reject requests with a content-length exceeding `MAX_UPLOAD_BYTES`.

    Returns a structured `ErrorEnvelope` 413 response via
    `build_error_response`. We do NOT `raise HTTPException` from this
    middleware — exceptions raised inside HTTP middleware can bypass
    FastAPI's HTTPException handler and fall through to Starlette's
    `ServerErrorMiddleware`, producing a non-enveloped 500.

    Defensive: a malformed numeric content-length header is ignored
    (the request is passed through; the route layer / framework will
    reject the body if needed). This avoids crashing the middleware on
    a misbehaving client.
    """
    content_length_header = request.headers.get("content-length")
    if not content_length_header:
        return await call_next(request)

    try:
        content_length = int(content_length_header)
    except ValueError:
        return await call_next(request)

    logger.info(f"Request content-length: {content_length} bytes")
    if content_length > MAX_UPLOAD_BYTES:
        logger.warning(f"Request too large: {content_length} bytes exceeds {MAX_UPLOAD_BYTES} bytes")
        return build_error_response(
            request,
            code=ApiErrorCode.INVALID_REQUEST,
            message=f"File too large. Maximum size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            status_code=413,
        )

    return await call_next(request)
