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
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from .auth import require_admin_token
from .models import (
    DocumentDetailResponse,
    DocumentListResponse,
    IndexStatsResponse,
)
from .search_admin_service import SearchAdminService, get_admin_service

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
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
        return await loop.run_in_executor(
            _executor, partial(svc.list_documents, bot_tag)
        )
    except AzureError as e:
        logger.error(
            "Azure Search failure in list_documents for bot_tag=%r: %s",
            bot_tag, e, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index temporarily unavailable",
        )


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
        result = await loop.run_in_executor(
            _executor, partial(svc.get_document, bot_tag, document_id)
        )
    except AzureError as e:
        logger.error(
            "Azure Search failure in get_document for bot_tag=%r document_id=%r: %s",
            bot_tag, document_id, e, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index temporarily unavailable",
        )

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
        return await loop.run_in_executor(
            _executor, partial(svc.get_index_stats, bot_tag)
        )
    except AzureError as e:
        logger.error(
            "Azure Search failure in index_stats for bot_tag=%r: %s",
            bot_tag, e, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index temporarily unavailable",
        )
