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
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Annotated

# Absolute imports — these are top-level modules under services/ingestion/, not
# inside the `admin/` package. Works in both runtime (uvicorn cwd =
# services/ingestion/) and tests (sys.path includes services/ingestion/).
import custom_rag
from azure.core.exceptions import AzureError
from connectors import ConnectorConfig, ConnectorError, run_connector
from connectors.blob import BlobConnector
from connectors.sharepoint import SharePointConnector
from errors import ApiErrorCode, default_error_responses, raise_api_error
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from observability import log_event

from .auth import require_admin_token
from .models import (
    ConnectorSyncResponse,
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

# Source types the operator trigger endpoint can launch a sync for.
_SUPPORTED_SOURCE_TYPES = ("blob", "sharepoint")

# Module-level RAG singleton, accessed via the `get_rag_instance` dependency so
# tests can override it (FastAPI dependency_overrides) without a live index and
# without importing app.py (which would create a circular import).
_rag_singleton: custom_rag.rag | None = None


def get_rag_instance() -> custom_rag.rag:
    """Return the process-wide RAG instance used by connector syncs.

    Lazily constructed so importing this module never builds Azure clients.
    Overridable in tests via `app.dependency_overrides[get_rag_instance]`.
    """
    global _rag_singleton
    if _rag_singleton is None:
        _rag_singleton = custom_rag.rag()
    return _rag_singleton


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


# ---------------------------------------------------------------------------
# Operator connector sync trigger (PR-5) — in-stack, behind require_admin_token
# ---------------------------------------------------------------------------


def _build_connector(source_type: str):
    """Construct a connector from env config (P0-7) for the given source_type.

    bot_tag / fr_mode and the per-source location (container, or site/drive) all
    come from env — never from the request — so source→bot_tag binding stays
    immutable and cross-tagging is structurally impossible. ConnectorConfig
    validates bot_tag against BOT_TAG_PATTERN at init. Secrets are read by the
    connector itself via os.getenv and are never logged or echoed.

    Raises ConnectorError (mapped to a 400 envelope by the caller) on any
    missing/invalid config, before any network call.
    """
    bot_tag = os.getenv("CONNECTOR_BOT_TAG")
    if not bot_tag:
        raise ConnectorError("Connector sync misconfigured: CONNECTOR_BOT_TAG is required")
    fr_mode = os.getenv("CONNECTOR_FR_MODE", "read")
    # ConnectorConfig validates bot_tag (pattern) and fr_mode at init.
    config = ConnectorConfig(bot_tag=bot_tag, fr_mode=fr_mode)

    if source_type == "blob":
        container = os.getenv("BLOB_CONTAINER")
        if not container:
            raise ConnectorError("Blob sync misconfigured: BLOB_CONTAINER is required")
        return BlobConnector(config, container)

    if source_type == "sharepoint":
        site_id = os.getenv("SHAREPOINT_SITE_ID")
        drive_id = os.getenv("SHAREPOINT_DRIVE_ID")
        if not (site_id and drive_id):
            raise ConnectorError(
                "SharePoint sync misconfigured: SHAREPOINT_SITE_ID and SHAREPOINT_DRIVE_ID are required"
            )
        return SharePointConnector(config, site_id, drive_id)

    # Unreachable: source_type is validated against the allowlist by the caller.
    raise ConnectorError(f"Unsupported source_type: {source_type!r}")


async def _run_connector_background(connector, rag_instance, *, run_id: str) -> None:
    """Drive one connector run, logging its own start/complete/failed events.

    run_connector emits run_started/run_completed and re-raises on a failing
    item (logging connector_item_failed but NO run-level failed event). As a
    detached background task there is no caller to observe that exception, so we
    wrap it and emit a connector_run_failed event — keeping the run's lifecycle
    fully greppable by run_id.
    """
    try:
        await run_connector(connector, rag_instance, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - background task must not propagate
        log_event(
            logger,
            "connector_run_failed",
            request_id=run_id,
            level=logging.ERROR,
            source_type=getattr(connector, "source_type", None),
            bot_tag=getattr(connector, "bot_tag", None),
            error_class=type(exc).__name__,
            safe_message="Connector sync run failed",
        )


@router.post(
    "/connectors/{source_type}/sync",
    response_model=ConnectorSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a connector sync (blob | sharepoint) as a background task",
)
async def trigger_connector_sync(
    request: Request,
    source_type: Annotated[
        str,
        Path(..., description="Connector source type: 'blob' or 'sharepoint'"),
    ],
    background_tasks: BackgroundTasks,
    rag_instance: Annotated[custom_rag.rag, Depends(get_rag_instance)],
) -> ConnectorSyncResponse:
    """Kick off a connector sync without blocking on the full run.

    In-stack (behind require_admin_token) so it inherits the P0-6 ErrorEnvelope
    handlers and X-Request-ID middleware. The connector is built from env config
    (validating bot_tag); construction/validation errors return the P0-6
    envelope (400 for bad source_type / missing config). On success the
    enumerate→fetch→upload loop is scheduled as a background task and the handler
    returns 202 with a generated run_id. The trigger event carries BOTH the
    request's X-Request-ID and the run_id; the background run logs its own
    start/complete/failed events keyed on run_id.
    """
    if source_type not in _SUPPORTED_SOURCE_TYPES:
        raise_api_error(
            code=ApiErrorCode.INVALID_REQUEST,
            message=f"Unsupported source_type; expected one of {list(_SUPPORTED_SOURCE_TYPES)}.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        connector = _build_connector(source_type)
    except ConnectorError as exc:
        # Missing/invalid env config (incl. an invalid bot_tag) → 400 envelope.
        # The ConnectorError message is safe (no secrets); surface it.
        raise_api_error(
            code=ApiErrorCode.INVALID_REQUEST,
            message=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    run_id = uuid.uuid4().hex
    request_id = getattr(request.state, "request_id", None)

    # Correlate the run with the inherited X-Request-ID AND the generated run_id.
    # The RequestIDMiddleware resets the request_id ContextVar before background
    # tasks run, so we pass run_id explicitly into the background loop below.
    log_event(
        logger,
        "connector_sync_triggered",
        request_id=request_id,
        run_id=run_id,
        source_type=source_type,
        bot_tag=connector.bot_tag,
    )

    background_tasks.add_task(_run_connector_background, connector, rag_instance, run_id=run_id)

    return ConnectorSyncResponse(run_id=run_id, source_type=source_type)
