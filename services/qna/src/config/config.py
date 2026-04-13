import os
import asyncio
from dotenv import load_dotenv
from azure.identity.aio import ClientSecretCredential
from azure.keyvault.secrets.aio import SecretClient
from azure.core.exceptions import AzureError

# Load .env values; Key Vault population happens later (see Settings.load_secrets_from_keyvault).
load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Minimal runtime guardrails: ensure critical env vars exist at process start.
# NOTE: These checks run BEFORE any Key Vault fetch. If you intend Key Vault to
# populate these names, make sure the same exact keys are present there too,
# or call Settings.load_secrets_from_keyvault early in your boot sequence.
# ---------------------------------------------------------------------------
required_env_vars = [
    "AzureOpenaiAccountEndpoint",
    "TocdocOpenAIKey",
    "AzureOpenaiApiVersion",
    "AZURE_OPENAI_EMBEDDING_MODEL",
    "AzureSearchEndpoint",
    "AzureSearchKey",
    "INDEX_NAME",
]

for var in required_env_vars:
    if not os.getenv(var):
        raise ValueError(f"Missing required environment variable: {var}")


class Settings:
    """
    Application configuration holder with optional lazy secret loading.

    This class exposes static environment values immediately after import and
    can populate additional secrets from Azure Key Vault on demand using
    `load_secrets_from_keyvault`.

    Attributes:
        AZURE_CLIENT_ID (str): App registration client ID.
        AZURE_CLIENT_SECRET (str): Client secret for the above app.
        AZURE_TENANT_ID (str): Azure AD tenant ID.
        AZURE_KEY_VAULT (str): Name of the Key Vault resource (without FQDN).
        AUDIENCE_ID (str): Audience identifier for token validation (if used).

        secret_names (list[str]): Key Vault secret names to fetch; their values
            will be written into process env using the SAME names.
    """

    # Static env values (set in App Service / Container App / .env)
    AZURE_CLIENT_ID: str = os.getenv("TocdocSPClientID")
    AZURE_CLIENT_SECRET: str = os.getenv("TocdocSPSecretValue")
    AZURE_TENANT_ID: str = os.getenv("TocdocSPTenantID")
    AZURE_KEY_VAULT: str = os.getenv("AZURE_KEY_VAULT")
    AUDIENCE_ID: str = os.getenv("AUDIENCE_ID")

    # These are the Key Vault secret names to pull and mirror into os.environ.
    secret_names = [
        "AzureOpenaiAccountEndpoint",
        "TocdocOpenAIKey",
        "AzureOpenaiApiVersion",
        "AzureOpenaiLlmModel",
        "TocdocSPTenantID",
        "TocdocSPClientID",
        "TocdocSPSecretValue",
        "AzureSearchEndpoint",
        "AzureSearchKey",
    ]

    @classmethod
    async def load_secrets_from_keyvault(cls):
        """
        Populate process environment variables from Azure Key Vault.

        Behavior:
            - Authenticates to Key Vault via ClientSecretCredential.
            - Fetches all `secret_names` concurrently (bounded by a semaphore).
            - Writes each fetched secret into `os.environ` under the SAME name.
            - Returns a mapping of secret name -> bool (success/failure).

        Notes:
            - This method does not alter `required_env_vars`. If you depend on
              uppercase env keys (e.g., AZURE_OPENAI_KEY), ensure the Vault
              contains the same names or normalize externally in your startup.
            - Exceptions from Key Vault are caught per-secret; failures are
              recorded as False without raising, to preserve original behavior.

        Returns:
            dict[str, bool]: Per-secret success flags.
        """
        vault_url = f"https://{cls.AZURE_KEY_VAULT}.vault.azure.net"
        credential = ClientSecretCredential(
            cls.AZURE_TENANT_ID,
            cls.AZURE_CLIENT_ID,
            cls.AZURE_CLIENT_SECRET,
        )
        client = SecretClient(vault_url=vault_url, credential=credential)

        results = {}
        semaphore = asyncio.Semaphore(5)

        async def fetch_secret(name):
            async with semaphore:
                try:
                    secret = await client.get_secret(name)
                    os.environ[name] = secret.value
                    results[name] = True
                except AzureError:
                    # Keep a coarse failure signal; do not raise to avoid
                    # changing control flow. Details can be logged upstream.
                    results[name] = False
                except Exception:
                    results[name] = False

        await asyncio.gather(*(fetch_secret(name) for name in cls.secret_names))

        await client.close()
        await credential.close()
        return results


def run_async(coro):
    """
    Execute an async coroutine from both async and sync contexts.

    If already running inside an event loop, schedules the coroutine and
    returns the created task (caller can await/track it). Otherwise, runs
    the coroutine to completion using `asyncio.run`.

    Args:
        coro: The coroutine object to execute.

    Returns:
        Any: The coroutine result (when run in a fresh loop) or an asyncio.Task
        (when scheduled within an existing loop).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        return loop.create_task(coro)


# Singleton instance (import elsewhere as `settings`).
settings = Settings()


class AzureConfig:
    """
    Container for Azure service configuration values sourced from environment.

    Attributes:
        AZURE_OPENAI_API_VERSION (str | None): OpenAI API version value.
        AZURE_OPENAI_KEY (str | None): Azure OpenAI API key.
        AZURE_OPENAI_ENDPOINT (str | None): Azure OpenAI endpoint URL.
        AZURE_SEARCH_ENDPOINT (str | None): Azure Cognitive Search endpoint URL.
        AZURE_SEARCH_KEY (str | None): Azure Cognitive Search API key.
    """

    def __init__(self) -> None:
        # NOTE: Keys below intentionally read the *PascalCase* variants, matching
        # the Key Vault secret_names above. This preserves behavior without
        # normalizing to the UPPERCASE names used in `required_env_vars`.
        self.AZURE_OPENAI_API_VERSION = os.getenv("AzureOpenaiApiVersion")
        self.AZURE_OPENAI_KEY = os.getenv("TocdocOpenAIKey")
        self.AZURE_OPENAI_ENDPOINT = os.getenv("AzureOpenaiAccountEndpoint")
        self.AZURE_SEARCH_ENDPOINT = os.getenv("AzureSearchEndpoint")
        self.AZURE_SEARCH_KEY = os.getenv("AzureSearchKey")


class LocalConfig:
    """
    Local runtime defaults and template-bound settings.

    Responsibilities (documentary only; no logic changed):
      - Provide global defaults (e.g., model names, index name, limits).
      - Serve as a place-holder for template-level configuration that may be
        swapped at runtime via constructor argument.

    Args:
        template_name (str): Logical template selector; stored but not used here.

    Attributes:
        AZURE_LLM_MODEL (str): Default LLM deployment/model name.
        TOP_K (int): Default top-k retrieval limit.
        INDEX_NAME (str): Default Azure Cognitive Search index name.
        EMBEDDING_DIMENSIONS (int): Vector size for embeddings.
        AZURE_OPENAI_EMBEDDING_MODEL (str): Embedding model deployment/name.
    """

    # ---------------------
    # Global defaults
    # ---------------------
    def __init__(self, template_name: str = "general") -> None:
        # Global defaults to be read from env along with fallbacks.
        self.AZURE_LLM_MODEL = os.getenv("AzureOpenaiLlmModel", "gpt-4o-mini")
        self.TOP_K = 20
        self.INDEX_NAME: str = os.getenv("INDEX_NAME", "vector-demo-custom-03")
        self.EMBEDDING_DIMENSIONS: int = os.getenv("EMBEDDING_DIMENSIONS", 1536)
        self.AZURE_OPENAI_EMBEDDING_MODEL = os.getenv(
            "AZURE_OPENAI_EMBEDDING_MODEL",
            "text-embedding-3-small",
        )