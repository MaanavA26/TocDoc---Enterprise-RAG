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

Migration mapping (legacy → canonical):

    AzureOpenaiAccountEndpoint → AZURE_OPENAI_ENDPOINT
    TocdocOpenAIKey            → AZURE_OPENAI_KEY
    AzureOpenaiApiVersion      → AZURE_OPENAI_VERSION
    AzureOpenaiLlmModel        → AZURE_OPENAI_LLM_MODEL
    AzureSearchEndpoint        → AZURE_SEARCH_ENDPOINT
    AzureSearchKey             → AZURE_SEARCH_KEY
    TocdocSPClientID           → AZURE_CLIENT_ID
    TocdocSPSecretValue        → AZURE_CLIENT_SECRET
    TocdocSPTenantID           → AZURE_TENANT_ID

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
from typing import Optional

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
    "AZURE_OPENAI_ENDPOINT":    "AzureOpenaiAccountEndpoint",
    "AZURE_OPENAI_KEY":         "TocdocOpenAIKey",
    "AZURE_OPENAI_VERSION":     "AzureOpenaiApiVersion",
    "AZURE_OPENAI_LLM_MODEL":   "AzureOpenaiLlmModel",
    "AZURE_SEARCH_ENDPOINT":    "AzureSearchEndpoint",
    "AZURE_SEARCH_KEY":         "AzureSearchKey",
    "AZURE_CLIENT_ID":          "TocdocSPClientID",
    "AZURE_CLIENT_SECRET":      "TocdocSPSecretValue",
    "AZURE_TENANT_ID":          "TocdocSPTenantID",
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
        location, legacy, canonical,
    )


def _get_env(canonical: str, default: Optional[str] = None) -> Optional[str]:
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

        Dual-read behavior:
            - Fetches each `secret_names` entry under its canonical name first.
            - If `ResourceNotFoundError` is raised (KV secret doesn't exist),
              tries the pre-P0-7 legacy alias instead.
            - On either path, writes the fetched value into `os.environ`
              under the **canonical** name. Downstream readers see the
              canonical name uniformly; legacy KV secrets are silently
              upgraded as long as both names live in the migration table.
            - Other AzureErrors (network, auth) are recorded as `False`
              without re-trying — this matches the pre-P0-7 coarse failure
              signal and avoids masking transient infra problems with an
              incorrect "secret missing" reading.

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
                try:
                    secret = await client.get_secret(canonical)
                    os.environ[canonical] = secret.value
                    results[canonical] = True
                    return
                except ResourceNotFoundError:
                    # Canonical secret doesn't exist; try the legacy alias.
                    pass
                except AzureError:
                    results[canonical] = False
                    return
                except Exception:
                    results[canonical] = False
                    return

                legacy = _LEGACY_ENV_ALIASES.get(canonical)
                if not legacy:
                    results[canonical] = False
                    return
                try:
                    secret = await client.get_secret(legacy)
                    # CRITICAL: write under canonical name so downstream
                    # code reads consistently. The legacy KV secret name is
                    # only honored at this fetch site.
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
        INDEX_NAME (str): Default Azure Cognitive Search index name.
        EMBEDDING_DIMENSIONS (int): Vector size for embeddings.
        AZURE_OPENAI_EMBEDDING_MODEL (str): Embedding model deployment/name.
    """

    def __init__(self, template_name: str = "general") -> None:
        # `_get_env` returns None when the var isn't set; preserve the
        # existing fallback values when that happens.
        self.AZURE_LLM_MODEL = _get_env("AZURE_OPENAI_LLM_MODEL") or "gpt-4o-mini"
        self.TOP_K = 20
        self.INDEX_NAME: str = os.getenv("INDEX_NAME", "vector-demo-custom-03")
        self.EMBEDDING_DIMENSIONS: int = os.getenv("EMBEDDING_DIMENSIONS", 1536)
        self.AZURE_OPENAI_EMBEDDING_MODEL = os.getenv(
            "AZURE_OPENAI_EMBEDDING_MODEL",
            "text-embedding-3-small",
        )
