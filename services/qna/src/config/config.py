"""QnA service configuration with normalized env var naming (P0-7).

Canonical env vars are UPPER_SNAKE — matching the ingestion service, the
Python convention, and Azure SDK `DefaultAzureCredential` defaults
(`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`). Pre-P0-7
deployments using the legacy PascalCase names continue to work for one
release through a dual-read fallback that:

- Reads the canonical name first.
- Falls back to the legacy alias if the canonical is unset.
- Emits a one-shot deprecation warning per legacy alias hit
  (operators see it in container logs after the first request).
- For Key Vault secrets specifically, the legacy KV secret value is
  rewritten into `os.environ` under the **canonical** name so downstream
  code reads the canonical name uniformly.

Migration mapping (legacy env → canonical env → Key Vault secret name):

    AzureOpenaiAccountEndpoint → AZURE_OPENAI_ENDPOINT    (KV: azure-openai-endpoint)
    TocdocOpenAIKey            → AZURE_OPENAI_KEY         (KV: azure-openai-key)
    AzureOpenaiApiVersion      → AZURE_OPENAI_VERSION     (KV: azure-openai-version)
    AzureOpenaiLlmModel        → AZURE_OPENAI_LLM_MODEL   (KV: azure-openai-llm-model)
    AzureSearchEndpoint        → AZURE_SEARCH_ENDPOINT    (KV: azure-search-endpoint)
    AzureSearchKey             → AZURE_SEARCH_KEY         (KV: azure-search-key)
    TocdocSPClientID           → AZURE_CLIENT_ID          (KV: azure-client-id)
    TocdocSPSecretValue        → AZURE_CLIENT_SECRET      (KV: azure-client-secret)
    TocdocSPTenantID           → AZURE_TENANT_ID          (KV: azure-tenant-id)

Why three name spaces:

- Env var names use UPPER_SNAKE per the Python / Azure SDK
  `DefaultAzureCredential` convention (`AZURE_OPENAI_KEY`).
- Key Vault secret names are restricted to `^[a-zA-Z0-9-]+$` — underscores
  are NOT permitted — so the KV form is hyphenated-lowercase
  (`azure-openai-key`). Calling `client.get_secret("AZURE_OPENAI_KEY")`
  would return a 400 (invalid resource name), not `ResourceNotFoundError`.
- The legacy PascalCase form (`TocdocOpenAIKey`) is alphanumeric so it
  remains a valid KV secret name. Pre-P0-7 vaults storing PascalCase
  secret names continue to work via the loader's fallback path.

Already-canonical (unchanged):

    AZURE_OPENAI_EMBEDDING_MODEL
    INDEX_NAME
    AZURE_KEY_VAULT
    AUDIENCE_ID

## Scope boundary

P0-7 normalizes naming and adds the dual-read fallback. It does NOT move
the required-env validation out of import time — that bootstrap-order
refactor (so Key Vault loading can fill required values before
validation) is a separate workstream. The import-time check stays as-is
but now uses the canonical-aware resolver.
"""

from __future__ import annotations

import asyncio
import logging
import os

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity.aio import ClientSecretCredential
from azure.keyvault.secrets.aio import SecretClient
from dotenv import load_dotenv

# Load .env values; Key Vault population happens later (see
# Settings.load_secrets_from_keyvault).
load_dotenv(override=True)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Migration: legacy → canonical env var name aliases
# ---------------------------------------------------------------------------
# Maps the new canonical env name to the pre-P0-7 legacy alias. Update this
# table to drop a legacy name (after the deprecation window). DO NOT use
# this dict to remap CANONICAL → CANONICAL; it's a one-way migration aid.
_LEGACY_ENV_ALIASES: dict[str, str] = {
    "AZURE_OPENAI_ENDPOINT": "AzureOpenaiAccountEndpoint",
    "AZURE_OPENAI_KEY": "TocdocOpenAIKey",
    "AZURE_OPENAI_VERSION": "AzureOpenaiApiVersion",
    "AZURE_OPENAI_LLM_MODEL": "AzureOpenaiLlmModel",
    "AZURE_SEARCH_ENDPOINT": "AzureSearchEndpoint",
    "AZURE_SEARCH_KEY": "AzureSearchKey",
    "AZURE_CLIENT_ID": "TocdocSPClientID",
    "AZURE_CLIENT_SECRET": "TocdocSPSecretValue",
    "AZURE_TENANT_ID": "TocdocSPTenantID",
}

# Maps canonical env name → Azure Key Vault secret name.
#
# Key Vault secret names are restricted to `^[a-zA-Z0-9-]+$` — underscores
# are NOT permitted. The canonical env var names contain underscores
# (`AZURE_OPENAI_KEY`), so we cannot use them as KV secret names. The
# hyphenated-lowercase form matches Container Apps secret naming and is
# the recommended convention for KV secrets backing UPPER_SNAKE env vars.
#
# Lookup precedence inside `load_secrets_from_keyvault`:
#   1. Canonical KV secret (hyphenated-lowercase, e.g., "azure-openai-key")
#   2. Legacy KV secret  (PascalCase from `_LEGACY_ENV_ALIASES`, e.g., "TocdocOpenAIKey")
#   3. Recorded as not-found
#
# Whichever path resolves, the value is written into `os.environ[canonical]`
# so downstream code reads canonical env names uniformly.
_KV_SECRET_NAMES: dict[str, str] = {
    "AZURE_OPENAI_ENDPOINT": "azure-openai-endpoint",
    "AZURE_OPENAI_KEY": "azure-openai-key",
    "AZURE_OPENAI_VERSION": "azure-openai-version",
    "AZURE_OPENAI_LLM_MODEL": "azure-openai-llm-model",
    "AZURE_SEARCH_ENDPOINT": "azure-search-endpoint",
    "AZURE_SEARCH_KEY": "azure-search-key",
    "AZURE_CLIENT_ID": "azure-client-id",
    "AZURE_CLIENT_SECRET": "azure-client-secret",
    "AZURE_TENANT_ID": "azure-tenant-id",
}

# One-shot guard so the same deprecation message doesn't flood the logs on
# every request. Reset only when tests need to reassert; production keeps
# this module-level for the process lifetime.
_warned_aliases: set[str] = set()


def _warn_deprecated_alias(legacy: str, canonical: str, *, kv_secret: bool = False) -> None:
    """Emit a one-shot WARNING per legacy alias hit."""
    key = f"{legacy}->{canonical}:{'kv' if kv_secret else 'env'}"
    if key in _warned_aliases:
        return
    _warned_aliases.add(key)
    location = "Key Vault secret" if kv_secret else "environment variable"
    logger.warning(
        "Deprecated %s %r in use; rename to %r. "
        "Legacy name will be removed in a later release. "
        "See docs/deployment/INSTALLATION.md for the full migration table.",
        location,
        legacy,
        canonical,
    )


def _get_env(canonical: str, default: str | None = None) -> str | None:
    """Resolve an env value, preferring the canonical name; fall back to the
    pre-P0-7 legacy alias if present (with a one-shot deprecation warning).

    Returns the resolved value or `default` (None unless overridden).
    """
    value = os.getenv(canonical)
    if value:
        return value
    legacy = _LEGACY_ENV_ALIASES.get(canonical)
    if legacy:
        legacy_value = os.getenv(legacy)
        if legacy_value:
            _warn_deprecated_alias(legacy, canonical)
            return legacy_value
    return default


def _int_env(name: str, default: int) -> int:
    """Read a positive-int env var (canonical-aware), falling back to default.

    A malformed or non-positive value falls back to ``default`` with a warning
    rather than raising at import — a bad knob must never crash the process.
    """
    raw = _get_env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid int for %s=%r; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Non-positive value for %s=%d; using default %d", name, value, default)
        return default
    return value


# ---------------------------------------------------------------------------
# Minimal runtime guardrails: ensure critical env vars exist at process start.
# NOTE: These checks run BEFORE any Key Vault fetch (preserved P0-7 behavior).
# A given var passes the check if EITHER the canonical name OR the legacy
# alias is set. Container Apps deployed pre-P0-7 still boot; new deployments
# should set canonical names (legacy emits deprecation warnings).
# ---------------------------------------------------------------------------
required_env_vars: list[str] = [
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_KEY",
    "AZURE_OPENAI_VERSION",
    "AZURE_OPENAI_EMBEDDING_MODEL",
    "AZURE_SEARCH_ENDPOINT",
    "AZURE_SEARCH_KEY",
    "INDEX_NAME",
]

for var in required_env_vars:
    if not _get_env(var):
        raise ValueError(f"Missing required environment variable: {var}")


class Settings:
    """Application configuration holder with optional lazy secret loading.

    Internal Python attribute names are UPPER_SNAKE and have been stable
    since pre-P0-7; only the underlying env var names changed. Downstream
    code that reads `settings.AZURE_TENANT_ID` etc. needs no update.

    Attributes:
        AZURE_CLIENT_ID (str): App registration client ID.
        AZURE_CLIENT_SECRET (str): Client secret for the above app.
        AZURE_TENANT_ID (str): Azure AD tenant ID.
        AZURE_KEY_VAULT (str): Name of the Key Vault resource (without FQDN).
        AUDIENCE_ID (str): Audience identifier for token validation.
    """

    # Static env values (set in App Service / Container App / .env).
    # Each goes through `_get_env` so the legacy alias is honored during the
    # deprecation window.
    AZURE_CLIENT_ID: str = _get_env("AZURE_CLIENT_ID")
    AZURE_CLIENT_SECRET: str = _get_env("AZURE_CLIENT_SECRET")
    AZURE_TENANT_ID: str = _get_env("AZURE_TENANT_ID")
    AZURE_KEY_VAULT: str = _get_env("AZURE_KEY_VAULT")
    AUDIENCE_ID: str = _get_env("AUDIENCE_ID")

    # Key Vault secret names — canonical-named. The loader below falls back
    # to legacy KV secret names if a canonical secret doesn't exist in the
    # vault, so pre-P0-7 vaults keep working without operator action.
    secret_names: list[str] = [
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_KEY",
        "AZURE_OPENAI_VERSION",
        "AZURE_OPENAI_LLM_MODEL",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_SEARCH_ENDPOINT",
        "AZURE_SEARCH_KEY",
    ]

    @classmethod
    async def load_secrets_from_keyvault(cls) -> dict[str, bool]:
        """Populate process env vars from Azure Key Vault.

        Lookup precedence per canonical env name (e.g., `AZURE_OPENAI_KEY`):

            1. Canonical KV secret name (hyphenated-lowercase, e.g.,
               `azure-openai-key`) — the recommended P0-7 form for new
               deployments. Mapped via `_KV_SECRET_NAMES`.
            2. Legacy KV secret name (PascalCase from `_LEGACY_ENV_ALIASES`,
               e.g., `TocdocOpenAIKey`) — preserved so pre-P0-7 vaults
               work without operator action. A one-shot deprecation
               warning fires per legacy secret hit.
            3. Recorded as not-found.

        Whichever path resolves, the value is written into
        `os.environ[canonical]` so downstream code reads the canonical
        env name uniformly.

        Why two name spaces (canonical env vs KV secret):
            Azure Key Vault secret names are restricted to `^[a-zA-Z0-9-]+$`
            — underscores are NOT permitted. `AZURE_OPENAI_KEY` is therefore
            not a valid KV secret name. Calling `get_secret` with an
            underscore-containing name returns a 400 (invalid resource
            name), NOT `ResourceNotFoundError`. So the loader uses a
            separate hyphenated mapping for KV lookups; env vars stay
            UPPER_SNAKE.

        Error handling:
            - `ResourceNotFoundError` on either lookup falls through to the
              next step in precedence.
            - Other `AzureError` (network, auth) records `False` without
              re-trying. Preserves the pre-P0-7 coarse failure signal and
              avoids masking transient infra problems with an incorrect
              "secret missing" reading.
            - Any other exception type also records `False`.

        Returns:
            dict[str, bool]: Per-canonical-name success flags.
        """
        vault_url = f"https://{cls.AZURE_KEY_VAULT}.vault.azure.net"
        credential = ClientSecretCredential(
            cls.AZURE_TENANT_ID,
            cls.AZURE_CLIENT_ID,
            cls.AZURE_CLIENT_SECRET,
        )
        client = SecretClient(vault_url=vault_url, credential=credential)

        results: dict[str, bool] = {}
        semaphore = asyncio.Semaphore(5)

        async def fetch_secret(canonical: str) -> None:
            async with semaphore:
                # Step 1: try the canonical KV secret name (hyphenated).
                canonical_kv_name = _KV_SECRET_NAMES.get(canonical)
                if canonical_kv_name:
                    try:
                        secret = await client.get_secret(canonical_kv_name)
                        os.environ[canonical] = secret.value
                        results[canonical] = True
                        return
                    except ResourceNotFoundError:
                        # Fall through to legacy lookup.
                        pass
                    except AzureError:
                        # Network / auth / other Azure failure — don't try
                        # the legacy path either; the failure is upstream,
                        # not a missing secret.
                        results[canonical] = False
                        return
                    except Exception:
                        results[canonical] = False
                        return

                # Step 2: try the legacy PascalCase KV secret name.
                legacy = _LEGACY_ENV_ALIASES.get(canonical)
                if not legacy:
                    results[canonical] = False
                    return
                try:
                    secret = await client.get_secret(legacy)
                    # CRITICAL: write under canonical env name so downstream
                    # code reads consistently. The legacy KV secret name is
                    # honored only at this fetch site.
                    os.environ[canonical] = secret.value
                    _warn_deprecated_alias(legacy, canonical, kv_secret=True)
                    results[canonical] = True
                except (ResourceNotFoundError, AzureError):
                    results[canonical] = False
                except Exception:
                    results[canonical] = False

        await asyncio.gather(*(fetch_secret(name) for name in cls.secret_names))

        await client.close()
        await credential.close()
        return results


# ---------------------------------------------------------------------------
# Feature flags (read at request time so they are togglable without redeploy)
# ---------------------------------------------------------------------------
# Canonical UPPER_SNAKE per the P0-7 convention. No legacy alias (new in P3).
_TRUTHY = {"1", "true", "yes", "on"}


def is_map_reduce_enabled() -> bool:
    """Whether the P3-2 map-reduce summariser node is live (default OFF).

    A **sub-flag** under the master ``QNA_AGENT_ENABLED``: the map-reduce route
    only runs when BOTH this flag and the master flag are on. Read live from the
    environment on every call so it is a no-redeploy kill-switch and tests can
    toggle it with ``monkeypatch.setenv``. Parsed explicitly so the literal
    string ``"false"`` is correctly falsy.

    With this flag unset/empty/falsy, a ``map_reduce`` classification collapses
    to ``standard_route`` — behaviour is byte-for-byte identical to today.
    """
    return (os.getenv("QNA_AGENT_MAP_REDUCE") or "").strip().lower() in _TRUTHY


def is_tenant_binding_enforced() -> bool:
    """Whether within-tenant bot_tag<->tid binding is enforced (default OFF).

    Addresses the threat-model **R1** gap: today a caller authenticated for
    tenant ``T`` can pass any ``bot_tag`` in the request body, so they can
    query another workspace's ``bot_tag`` that happens to live under the same
    tenant. When this flag is OFF (default), behaviour is byte-for-byte
    identical to today — the guard is fully inert and the map below is never
    even parsed. When ON, the request-path guard validates the requested
    ``bot_tag`` against an allowlist keyed by the token's ``tid`` and fails
    closed on any mismatch / unmapped tid (see ``src/core/tenant_binding.py``).

    Read live from the environment on every call so it is a no-redeploy
    kill-switch and tests can toggle it with ``monkeypatch.setenv``. Parsed
    explicitly so the literal string ``"false"`` is correctly falsy.
    """
    return (os.getenv("QNA_ENFORCE_TENANT_BINDING") or "").strip().lower() in _TRUTHY


def is_agent_enabled() -> bool:
    """Whether the P3 LangGraph agentic layer handles ``/qna`` (default OFF).

    Read live from the environment on every call so the flag is a no-redeploy
    kill-switch: flipping ``QNA_AGENT_ENABLED`` takes effect on the next
    request, and tests can toggle it with ``monkeypatch.setenv``. Parsed
    explicitly so the literal string ``"false"`` is correctly falsy (a bare
    ``bool(os.getenv(...))`` would treat any non-empty string as True).

    With the flag unset/empty/falsy, ``/qna`` retains the legacy direct
    ``generate_answer`` call — byte-for-byte identical behaviour.
    """
    return (os.getenv("QNA_AGENT_ENABLED") or "").strip().lower() in _TRUTHY


def run_async(coro):
    """Execute an async coroutine from both async and sync contexts.

    If already running inside an event loop, schedules the coroutine and
    returns the created task. Otherwise, runs the coroutine to completion
    using `asyncio.run`.
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
    """Container for Azure service configuration values sourced from env.

    Attributes:
        AZURE_OPENAI_API_VERSION (str | None): OpenAI API version.
        AZURE_OPENAI_KEY (str | None): Azure OpenAI API key.
        AZURE_OPENAI_ENDPOINT (str | None): Azure OpenAI endpoint URL.
        AZURE_SEARCH_ENDPOINT (str | None): Azure Cognitive Search endpoint URL.
        AZURE_SEARCH_KEY (str | None): Azure Cognitive Search API key.
    """

    def __init__(self) -> None:
        # Each field uses the canonical-aware resolver.
        self.AZURE_OPENAI_API_VERSION = _get_env("AZURE_OPENAI_VERSION")
        self.AZURE_OPENAI_KEY = _get_env("AZURE_OPENAI_KEY")
        self.AZURE_OPENAI_ENDPOINT = _get_env("AZURE_OPENAI_ENDPOINT")
        self.AZURE_SEARCH_ENDPOINT = _get_env("AZURE_SEARCH_ENDPOINT")
        self.AZURE_SEARCH_KEY = _get_env("AZURE_SEARCH_KEY")


class LocalConfig:
    """Local runtime defaults and template-bound settings.

    Args:
        template_name (str): Logical template selector; stored but not used here.

    Attributes:
        AZURE_LLM_MODEL (str): Default LLM deployment/model name.
        TOP_K (int): Default top-k retrieval limit.
        AZURE_SEARCH_SEMANTIC_CONFIG (str): Name of the Azure AI Search
            semantic configuration to apply for L2 semantic reranking.
            Empty string (default) disables semantic ranking — retrieval
            runs as a pure hybrid query, unchanged. Set to the index's
            semantic configuration name (e.g. ``mySemanticConfig``) to
            enable. Requires Azure AI Search Standard (S1) tier or higher;
            on unsupported tiers the search layer falls back to hybrid.
        INDEX_NAME (str): Default Azure Cognitive Search index name.
        EMBEDDING_DIMENSIONS (int): Vector size for embeddings.
        AZURE_OPENAI_EMBEDDING_MODEL (str): Embedding model deployment/name.
    """

    def __init__(self, template_name: str = "general") -> None:
        # `_get_env` returns None when the var isn't set; preserve the
        # existing fallback values when that happens.
        self.AZURE_LLM_MODEL = _get_env("AZURE_OPENAI_LLM_MODEL") or "gpt-4o-mini"
        self.TOP_K = 20

        # --- P3-2 map-reduce summariser knobs (canonical UPPER_SNAKE; new in
        # P3 so no legacy alias). All optional with sensible defaults so parity
        # and tests work without any new env. ---
        # Chunks per map (extract) LLM call.
        self.MAP_REDUCE_BATCH_SIZE: int = _int_env("MAP_REDUCE_BATCH_SIZE", 20)
        # Max concurrent in-flight map calls (semaphore bound). Sized small to
        # respect the synchronous Azure OpenAI client + bounded executor.
        self.MAP_REDUCE_CONCURRENCY: int = _int_env("MAP_REDUCE_CONCURRENCY", 4)
        # Hard ceiling on how many chunks "retrieve all" pulls in one search, so
        # fetch_all is bounded (Azure caps a single query near ~1000 anyway).
        self.MAP_REDUCE_MAX_CHUNKS: int = _int_env("MAP_REDUCE_MAX_CHUNKS", 1000)
        # Reduce-step model: a separate (typically larger) deployment for the
        # final synthesis. Falls back to the standard LLM model so the node
        # works without new env in tests/parity.
        self.AZURE_OPENAI_REDUCE_MODEL: str = _get_env("AZURE_OPENAI_REDUCE_MODEL") or self.AZURE_LLM_MODEL
        # Empty string = semantic reranking disabled (default, no behavior
        # change). Canonical UPPER_SNAKE name; no legacy alias (new in P2-1).
        self.AZURE_SEARCH_SEMANTIC_CONFIG: str = _get_env("AZURE_SEARCH_SEMANTIC_CONFIG") or ""
        self.INDEX_NAME: str = os.getenv("INDEX_NAME", "vector-demo-custom-03")
        self.EMBEDDING_DIMENSIONS: int = os.getenv("EMBEDDING_DIMENSIONS", 1536)
        self.AZURE_OPENAI_EMBEDDING_MODEL = os.getenv(
            "AZURE_OPENAI_EMBEDDING_MODEL",
            "text-embedding-3-small",
        )
