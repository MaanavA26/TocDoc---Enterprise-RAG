import asyncio
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from src.core.logger import logger

# ---------------------------------------------------------------------------
# Thread pool for CPU/IO-bound embedding calls
# ---------------------------------------------------------------------------
embedding_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embedding")


async def get_embedding(azure, text: str) -> list[float]:
    """
    Generate an embedding vector for the provided text using the configured Azure client.

    Executes `azure.embedding_client.embed_query(text)` in a thread pool via
    `run_in_executor` to avoid blocking the event loop.

    Args:
        azure: Object expected to expose `embedding_client.embed_query(str) -> List[float]`.
        text (str): Input text to embed.

    Returns:
        List[float]: The embedding vector for the input text.

    Raises:
        Exception: Any error raised by the underlying client is logged and re-raised.
    """
    logger.debug(f"Generating embedding for text: {text[:100]}...")

    try:
        start_time = time.time()

        loop = asyncio.get_running_loop()
        embedding = await loop.run_in_executor(
            embedding_executor,
            azure.embedding_client.embed_query,
            text,
        )

        embedding_time = time.time() - start_time
        logger.info(f"Embedding generated successfully in {embedding_time:.4f}s")
        logger.debug(f"Embedding dimensions: {len(embedding)}")

        return embedding

    except Exception as e:
        logger.error(f"Error generating embedding: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
