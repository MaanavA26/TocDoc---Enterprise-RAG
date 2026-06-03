"""FastAPI router for admin endpoints.

Handlers are deliberately thin: validate inputs (FastAPI does most of this
via `Annotated` patterns), call into the service layer, map service errors
to HTTP errors. The service layer (`search_admin_service.py`) owns the
Azure Search interaction.

Validation patterns are sourced from
`docs/architect_phase_2/04_BOT_TAG_DECISION_RECORD.md`.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Annotated

from azure.core.exceptions import AzureError

# Absolute import — `errors` is a top-level module under services/ingestion/,
# not inside the `admin/` package. Works in both runtime (uvicorn cwd =
# services/ingestion/) and tests (sys.path includes services/ingestion/).
from errors import ApiErrorCode, default_error_responses, raise_api_error
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse

from .auth import require_admin_token
from .models import (
    DeleteDocumentResponse,
    DeleteTenantResponse,
    DocumentDetailResponse,
    DocumentListResponse,
    IndexStatsResponse,
    ReindexResponse,
)
from .search_admin_service import SearchAdminService, get_admin_service

logger = logging.getLogger(__name__)

# `responses=default_error_responses` documents the standard ErrorEnvelope
# shape for every admin route in OpenAPI. Per-route decorators inherit it
# automatically (FastAPI merges router-level responses with route-level).
router = APIRouter(
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
    responses=default_error_responses,
)

# Bounded executor — admin operations are read-heavy but not request-rate-bound.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="admin")

# Validation patterns (per architect bot_tag decision record).
BOT_TAG_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
DOCUMENT_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,256}$"


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="List indexed documents in a bot_tag scope",
)
async def list_documents(
    bot_tag: Annotated[
        str,
        Query(
            ...,
            pattern=BOT_TAG_PATTERN,
            description="Tenant/workspace identifier (alphanumeric, dash, underscore; max 128 chars)",
        ),
    ],
    svc: Annotated[SearchAdminService, Depends(get_admin_service)],
) -> DocumentListResponse:
    """List one row per indexed document, grouped from chunk-level data."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, partial(svc.list_documents, bot_tag))
    except AzureError as e:
        logger.error(
            "Azure Search failure in list_documents for bot_tag=%r: %s",
            bot_tag,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index temporarily unavailable",
        ) from e


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetailResponse,
    summary="Get one document's summary in a bot_tag scope",
)
async def get_document(
    document_id: Annotated[
        str,
        Path(..., pattern=DOCUMENT_ID_PATTERN, description="Document identifier"),
    ],
    bot_tag: Annotated[
        str,
        Query(..., pattern=BOT_TAG_PATTERN, description="Tenant/workspace identifier"),
    ],
    svc: Annotated[SearchAdminService, Depends(get_admin_service)],
) -> DocumentDetailResponse:
    """Return summary for one document; 404 if not in this bot_tag scope."""
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(_executor, partial(svc.get_document, bot_tag, document_id))
    except AzureError as e:
        logger.error(
            "Azure Search failure in get_document for bot_tag=%r document_id=%r: %s",
            bot_tag,
            document_id,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index temporarily unavailable",
        ) from e

    if result is None:
        # Spec: 404 when no chunks exist for this document in this bot scope.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found in this scope",
        )
    return result


@router.get(
    "/index/stats",
    response_model=IndexStatsResponse,
    summary="Aggregate stats for one bot_tag scope",
)
async def index_stats(
    bot_tag: Annotated[
        str,
        Query(..., pattern=BOT_TAG_PATTERN, description="Tenant/workspace identifier"),
    ],
    svc: Annotated[SearchAdminService, Depends(get_admin_service)],
) -> IndexStatsResponse:
    """Return document and chunk counts plus per-source-type/per-mode breakdowns."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, partial(svc.get_index_stats, bot_tag))
    except AzureError as e:
        logger.error(
            "Azure Search failure in index_stats for bot_tag=%r: %s",
            bot_tag,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index temporarily unavailable",
        ) from e


# ---------------------------------------------------------------------------
# Destructive endpoints (PR-2)
# ---------------------------------------------------------------------------


@router.delete(
    "/documents/{document_id}",
    response_model=DeleteDocumentResponse,
    summary="Delete all chunks for one document in a bot_tag scope",
)
async def delete_document(
    document_id: Annotated[
        str,
        Path(..., pattern=DOCUMENT_ID_PATTERN, description="Document identifier"),
    ],
    bot_tag: Annotated[
        str,
        Query(..., pattern=BOT_TAG_PATTERN, description="Tenant/workspace identifier"),
    ],
    svc: Annotated[SearchAdminService, Depends(get_admin_service)],
) -> DeleteDocumentResponse:
    """Delete every chunk for one (bot_tag, document_id) pair.

    Idempotent: deleting a document that does not exist returns 200 with
    `deleted_chunks: 0`. Both filters are always applied, so chunks under a
    different bot_tag are never deleted.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, partial(svc.delete_document, bot_tag, document_id))
    except AzureError as e:
        logger.error(
            "Azure Search failure in delete_document for bot_tag=%r document_id=%r: %s",
            bot_tag,
            document_id,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index temporarily unavailable",
        ) from e


@router.delete(
    "/bots/{bot_tag}/documents",
    response_model=DeleteTenantResponse,
    summary="Delete all chunks for one bot/tenant (requires confirm=true)",
)
async def delete_tenant_documents(
    bot_tag: Annotated[
        str,
        Path(..., pattern=BOT_TAG_PATTERN, description="Tenant/workspace identifier"),
    ],
    svc: Annotated[SearchAdminService, Depends(get_admin_service)],
    confirm: Annotated[
        bool,
        Query(description="Must be explicitly true to perform this destructive operation"),
    ] = False,
) -> DeleteTenantResponse:
    """Delete every chunk for one bot_tag scope (all documents).

    Requires an explicit `confirm=true` query parameter. Without it we return
    400 and delete nothing — the confirmation gate runs before the service is
    ever called, so a missing/false confirm structurally cannot delete data.
    Other bot_tags are never affected.
    """
    if not confirm:
        # Genuine client error → P0-6 error envelope.
        raise_api_error(
            code=ApiErrorCode.INVALID_REQUEST,
            message="Refusing to delete tenant data without explicit confirm=true.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, partial(svc.delete_tenant, bot_tag))
    except AzureError as e:
        logger.error(
            "Azure Search failure in delete_tenant_documents for bot_tag=%r: %s",
            bot_tag,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index temporarily unavailable",
        ) from e


@router.post(
    "/documents/{document_id}/reindex",
    response_model=ReindexResponse,
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    summary="Reindex a document (not implemented — no source persistence yet)",
)
async def reindex_document(
    document_id: Annotated[
        str,
        Path(..., pattern=DOCUMENT_ID_PATTERN, description="Document identifier"),
    ],
    bot_tag: Annotated[
        str,
        Query(..., pattern=BOT_TAG_PATTERN, description="Tenant/workspace identifier"),
    ],
) -> JSONResponse:
    """Documented 501 stub.

    Inputs and auth are still validated (auth via the router-level dependency,
    inputs via the path/query patterns above). The body is the documented
    payload from the spec, NOT an error envelope — reindex is a known-missing
    capability, not a failure of this request.
    """
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content=ReindexResponse().model_dump(),
    )
