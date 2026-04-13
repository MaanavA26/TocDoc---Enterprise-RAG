import logging
from fastapi import FastAPI
from src.config.config import settings
from src.clients.azure_clients import AzureOpenAIHandler

logger = logging.getLogger(__name__)


async def startup_event(app: FastAPI):
    """
    FastAPI startup hook.

    Responsibilities:
        - Logs application startup progress.
        - Loads secrets from Azure Key Vault (via Settings).
        - Instantiates and ensures Azure clients are ready (OpenAI, Search).
        - Attaches the Azure handler to `app.state.azure` for downstream use.

    Args:
        app (FastAPI): The FastAPI app instance (injected by FastAPI).

    Raises:
        Exception: Propagates any initialization failure after logging.
    """
    try:
        logger.info("Starting application...")

        # Load secrets from Azure Key Vault (non-blocking async)
        logger.info("Loading secrets from Azure Key Vault...")
        await settings.load_secrets_from_keyvault()

        # Create and initialize Azure clients
        azure = AzureOpenAIHandler()
        azure._ensure_client()  # Ensures embedding_client, openai_client, search_client exist

        # Attach to application state for use in endpoints/middleware
        app.state.azure = azure

        logger.info("Application startup completed successfully")

    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        # Allow FastAPI to fail-fast so container orchestrator can restart
        raise


async def shutdown_event(app: FastAPI):
    """
    FastAPI shutdown hook.

    Cleans up application state and logs shutdown events.

    Args:
        app (FastAPI): The FastAPI app instance (injected by FastAPI).
    """
    logger.info("Shutting down application...")
    try:
        # Release Azure clients (set to None; no explicit close needed for these objects)
        app.state.azure = None
        logger.info("Application shutdown completed")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
 