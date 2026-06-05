from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from langchain_openai import AzureOpenAIEmbeddings
from openai import AzureOpenAI

from src.config.config import AzureConfig, LocalConfig
from src.core.logger import logger

# Interactive-grade per-call timeout (seconds) for all outbound Azure clients.
# Every call is dispatched through small (max_workers=2) ThreadPoolExecutors, so
# without a bounded timeout two simultaneously-hung upstream calls saturate a
# pool and wedge all QnA traffic of that type for up to the SDK default (~600s
# for the OpenAI client). A 30s ceiling fails the slow call instead of holding
# the pool slot indefinitely (M5).
_AZURE_CLIENT_TIMEOUT_SECONDS = 30.0


class AzureOpenAIHandler:
    """
    Centralized initializer for Azure OpenAI, Embeddings, and Azure Cognitive Search clients.

    This handler:
      - Validates required Azure configuration values (endpoint, keys, versions).
      - Lazily creates:
          * `embedding_client` (AzureOpenAIEmbeddings)
          * `openai_client` (AzureOpenAI)
          * `search_client` (SearchClient)

    Notes:
        - No side effects occur until `_ensure_client()` is called.
        - Logging uses the module-level `logger` for consistency with the rest of the codebase.
    """

    def __init__(self):
        """
        Initialize configuration containers and client placeholders.

        Attributes:
            azureconfig (AzureConfig): Holds Azure service configuration (keys, endpoints, versions).
            localconfig (LocalConfig): Holds local/runtime configuration (e.g., index name, model names).
            logger (logging.Logger): Process-wide logger instance.
            embedding_client (AzureOpenAIEmbeddings | None): Lazy-initialized embeddings client.
            openai_client (AzureOpenAI | None): Lazy-initialized chat/completions client.
            search_client (SearchClient | None): Lazy-initialized Azure Cognitive Search client.
        """
        self.azureconfig: AzureConfig = AzureConfig()
        self.localconfig: LocalConfig = LocalConfig()
        self.logger = logger

        self.embedding_client: AzureOpenAIEmbeddings | None = None
        self.openai_client: AzureOpenAI | None = None
        self.search_client: SearchClient | None = None

    def _ensure_client(self) -> None:
        """
        Ensure Azure OpenAI & Azure Cognitive Search clients exist; validate configuration first.

        Validation:
            - Confirms presence of required Azure settings from `AzureConfig`.
            - Raises `ValueError` if any required values are missing.

        Side Effects:
            - Creates and assigns `embedding_client`, `openai_client`, and `search_client`
              if they are currently `None`.

        Raises:
            ValueError: If one or more required Azure config values are missing.
            Exception:  Any exception raised by client constructors is logged and re-raised.
        """
        logger.info("Initializing QnA module...")

        # 1) Validate Azure config values already loaded by AzureConfig().
        required = {
            "AZURE_OPENAI_API_VERSION": self.azureconfig.AZURE_OPENAI_API_VERSION,
            "AZURE_OPENAI_ENDPOINT": self.azureconfig.AZURE_OPENAI_ENDPOINT,
            "AZURE_OPENAI_KEY": self.azureconfig.AZURE_OPENAI_KEY,
            "AZURE_SEARCH_ENDPOINT": self.azureconfig.AZURE_SEARCH_ENDPOINT,
            "AZURE_SEARCH_KEY": self.azureconfig.AZURE_SEARCH_KEY,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            logger.error(f"Missing required Azure configuration: {missing}")
            raise ValueError(f"Missing required Azure configuration: {missing}")

        logger.info("All required Azure configuration values found")

        # 2) Initialize clients if needed (lazy creation).
        if self.embedding_client is None:
            try:
                self.embedding_client = AzureOpenAIEmbeddings(
                    azure_endpoint=self.azureconfig.AZURE_OPENAI_ENDPOINT,
                    api_key=self.azureconfig.AZURE_OPENAI_KEY,
                    api_version=self.azureconfig.AZURE_OPENAI_API_VERSION,
                    model=self.localconfig.AZURE_OPENAI_EMBEDDING_MODEL,
                    # Bound embedding calls so a hung upstream cannot wedge the
                    # bounded embedding executor (M5).
                    timeout=_AZURE_CLIENT_TIMEOUT_SECONDS,
                )
                logger.info("Azure OpenAI Embeddings client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Azure OpenAI Embeddings client: {e}")
                raise

        if self.openai_client is None:
            try:
                self.openai_client = AzureOpenAI(
                    api_key=self.azureconfig.AZURE_OPENAI_KEY,
                    api_version=self.azureconfig.AZURE_OPENAI_API_VERSION,
                    azure_endpoint=self.azureconfig.AZURE_OPENAI_ENDPOINT,
                    # Bound every chat/completions call so a hung upstream call
                    # cannot occupy a bounded-executor slot indefinitely (M5).
                    timeout=_AZURE_CLIENT_TIMEOUT_SECONDS,
                )
                logger.info("Azure OpenAI client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Azure OpenAI client: {e}")
                raise

        if self.search_client is None:
            try:
                self.search_client = SearchClient(
                    endpoint=self.azureconfig.AZURE_SEARCH_ENDPOINT,
                    index_name=self.localconfig.INDEX_NAME,
                    credential=AzureKeyCredential(self.azureconfig.AZURE_SEARCH_KEY),
                    # azure-core transport timeouts (forwarded via **kwargs) bound
                    # every search call so a hung Search backend cannot wedge the
                    # bounded search executor (M5).
                    connection_timeout=_AZURE_CLIENT_TIMEOUT_SECONDS,
                    read_timeout=_AZURE_CLIENT_TIMEOUT_SECONDS,
                )
                logger.info("Azure Search client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Azure Search client: {e}")
                raise
