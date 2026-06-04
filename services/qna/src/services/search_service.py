import asyncio
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from azure.core.exceptions import HttpResponseError
from azure.search.documents.models import QueryType, VectorizedQuery

from src.config.config import LocalConfig
from src.core.logger import logger

# ---------------------------------------------------------------------------
# Executors / local config
# ---------------------------------------------------------------------------
search_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="search")
localconfig = LocalConfig()


async def perform_search(
    azure, query: str, vector: list[float], fr_mode: str, bot_tag: str
) -> list[dict[str, Any]]:
    """
    Execute a hybrid (text + vector) search against Azure Cognitive Search.

    Offloads the synchronous SDK call to a thread pool via `run_in_executor`
    to avoid blocking the event loop. Applies a filter on `fr_tag` and
    `bot_tag` using the provided `fr_mode` and `bot_tag` to enforce tenant
    isolation.

    Args:
        azure: Object expected to expose `search_client.search(...)`.
        query (str): The user's textual query.
        vector (List[float]): Embedding vector for KNN search.
        fr_mode (str): Retrieval mode tag (e.g., "fr_read" or "fr_layout").
        bot_tag (str): Bot/tenant identifier used for search isolation. Must
            be non-empty; an empty value is rejected before any search runs.

    Returns:
        List[Dict[str, Any]]: Materialized list of search results.

    Raises:
        ValueError: If bot_tag is empty or whitespace-only.
        Exception: Any SDK or runtime error is logged and re-raised.
    """
    if not bot_tag or not bot_tag.strip():
        raise ValueError("bot_tag is required for search isolation — empty bot_tag rejected")

    logger.info(f"Performing search with query: '{query}', fr_mode: '{fr_mode}', bot_tag: '{bot_tag}'")

    try:
        start_time = time.time()

        loop = asyncio.get_running_loop()
        search_fn = partial(
            _search_sync,
            azure=azure,
            query=query,
            vector=vector,
            fr_mode=fr_mode,
            bot_tag=bot_tag,
            top=localconfig.TOP_K,
        )
        results = await loop.run_in_executor(search_executor, search_fn)

        search_time = time.time() - start_time
        logger.info(f"Search completed in {search_time:.4f}s, found {len(results)} results")

        return results

    except Exception as e:
        logger.error(f"Error performing search: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise


def _search_sync(
    azure, query: str, vector: list[float], fr_mode: str, bot_tag: str, top: int
) -> list[dict[str, Any]]:
    """
    Synchronous helper that performs the actual Azure Cognitive Search call.

    Args:
        azure: Holder with an initialized `search_client`.
        query (str): Text query for semantic/keyword search.
        vector (List[float]): Embedding vector for vector KNN search.
        fr_mode (str): Retrieval mode tag used in filter expression.
        bot_tag (str): Bot/tenant identifier used in filter expression.
        top (int): Maximum number of results to return.

    When ``AZURE_SEARCH_SEMANTIC_CONFIG`` is set, an L2 semantic rerank is
    layered on the hybrid query. If the configured Search tier does not
    support semantic ranking (Azure raises ``HttpResponseError``), the call
    falls back to a plain hybrid query so a misconfigured tier never breaks
    retrieval. When the config is empty, behavior is identical to a pure
    hybrid query (no semantic params are sent).

    Returns:
        List[Dict[str, Any]]: Search results materialized into a list. When
        semantic ranking ran, each result includes its
        ``@search.reranker_score`` when Azure returns one.
    """
    vector_query = VectorizedQuery(
        vector=vector,
        k_nearest_neighbors=top,
        fields="content_vector",
    )

    safe_bot_tag = bot_tag.replace("'", "''")
    safe_fr_mode = fr_mode.replace("'", "''")
    filter_expr = f"fr_tag eq '{safe_fr_mode}' and bot_tag eq '{safe_bot_tag}'"
    logger.debug(f"Filter expression: {filter_expr}")

    base_kwargs: dict[str, Any] = {
        "search_text": query,
        "vector_queries": [vector_query],
        "select": [
            "id",
            "content",
            "section_header",
            "filename",
            "filepath",
            "document_id",
            "source_path",
        ],
        "filter": filter_expr,
        "top": top,
    }

    semantic_config = localconfig.AZURE_SEARCH_SEMANTIC_CONFIG
    if semantic_config:
        # NOTE: Azure Search is lazy — the HTTP request fires on iteration,
        # so an unsupported-tier HttpResponseError surfaces at list(), not at
        # .search(). Materialize inside the try so the fallback actually
        # triggers against real Azure.
        try:
            results = azure.search_client.search(
                **base_kwargs,
                query_type=QueryType.SEMANTIC,
                semantic_configuration_name=semantic_config,
            )
            return _materialize(results)
        except HttpResponseError as exc:
            logger.warning(
                "Semantic ranking unavailable for configuration %r (likely an "
                "unsupported Search tier — Standard S1+ is required); falling "
                "back to hybrid retrieval. Detail: %s",
                semantic_config,
                exc.message if hasattr(exc, "message") else str(exc),
            )

    results = azure.search_client.search(**base_kwargs)
    return _materialize(results)


def _materialize(results: Any) -> list[dict[str, Any]]:
    """Force the lazy Azure Search pager into a list of result dicts.

    Iterating the pager is what issues the HTTP request, so callers must do
    this inside the semantic-fallback try block. Azure SDK result rows are
    dict-like and already carry ``@search.reranker_score`` as a key when
    semantic ranking ran, so it flows through to downstream observability
    without special handling. Never logs chunk content.
    """
    return [dict(item) for item in results]
