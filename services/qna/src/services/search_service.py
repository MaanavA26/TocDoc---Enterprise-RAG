import asyncio
import time
from typing import List, Dict, Any
from azure.search.documents.models import VectorizedQuery
from src.config.config import LocalConfig
from src.core.logger import logger
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import partial

# ---------------------------------------------------------------------------
# Executors / local config
# ---------------------------------------------------------------------------
search_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="search")
localconfig = LocalConfig()


async def perform_search(azure, query: str, vector: List[float], fr_mode: str, bot_tag: str) -> List[Dict[str, Any]]:
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


def _search_sync(azure, query: str, vector: List[float], fr_mode: str, bot_tag: str, top: int) -> List[Dict[str, Any]]:
    """
    Synchronous helper that performs the actual Azure Cognitive Search call.

    Args:
        azure: Holder with an initialized `search_client`.
        query (str): Text query for semantic/keyword search.
        vector (List[float]): Embedding vector for vector KNN search.
        fr_mode (str): Retrieval mode tag used in filter expression.
        bot_tag (str): Bot/tenant identifier used in filter expression.
        top (int): Maximum number of results to return.

    Returns:
        List[Dict[str, Any]]: Search results materialized into a list.
    """
    vector_query = VectorizedQuery(
        vector=vector,
        k_nearest_neighbors=top,
        fields="content_vector",
    )

    filter_expr = f"fr_tag eq '{fr_mode}' and bot_tag eq '{bot_tag}'"
    logger.debug(f"Filter expression: {filter_expr}")

    results = azure.search_client.search(
        search_text=query,
        vector_queries=[vector_query],
        select=["id", "content", "section_header", "filename", "filepath"],
        filter=filter_expr,
        top=top,
    )

    return list(results)
