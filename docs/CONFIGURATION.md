# Configuration Reference

This document is the canonical reference for every environment variable read by
the two services in this repository:

- **Ingestion** — document connectors, parsing, chunking, embedding, and the
  read-only admin API.
- **Q&A** — retrieval and answer generation.

Each service is configured entirely through environment variables. The
`.env.example` file in each service directory is a copyable template; in a
deployed Azure Container App the same variables are supplied as container
environment variables (and, for the Q&A service, optionally hydrated from Azure
Key Vault — see [Secrets, env vars, and Key Vault](#secrets-env-vars-and-key-vault)).

## Secrets, env vars, and Key Vault

- **Secrets never live in the repository.** API keys, client secrets, and the
  admin token are supplied at runtime through the environment, or — for the
  Q&A service — fetched from Azure Key Vault at startup. The `.env.example`
  files contain only placeholder values and must never hold real credentials.
- **Three name spaces.** A single logical secret can appear under three
  different names:
  - **Environment variable** — `UPPER_SNAKE`, matching the Python convention
    and the Azure SDK `DefaultAzureCredential` defaults
    (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`). This is the
    canonical name your code and container config use, e.g. `AZURE_OPENAI_KEY`.
  - **Key Vault secret name** — `hyphenated-lowercase`, e.g.
    `azure-openai-key`. Azure Key Vault restricts secret names to
    `^[a-zA-Z0-9-]+$`; underscores are **not** permitted, so the env-var name
    cannot be used directly as a secret name. Requesting an underscore-bearing
    name returns a `400 invalid resource name`, not a "not found" error.
  - **Legacy alias** (Q&A only, deprecated) — pre-normalization `PascalCase`
    names retained for one release through a dual-read fallback. See
    [Legacy aliases and the dual-read fallback](#legacy-aliases-and-the-dual-read-fallback).
- **Where the mapping lives.** The canonical env → Key Vault secret-name
  mapping (`_KV_SECRET_NAMES`) and the canonical → legacy alias mapping
  (`_LEGACY_ENV_ALIASES`) are defined in
  `services/qna/src/config/config.py`.

## Q&A service

Source of truth: `services/qna/src/config/config.py` and
`services/qna/.env.example`.

The Q&A service validates a core set of variables **at import time** — the
process refuses to start (`ValueError: Missing required environment variable`)
if any are unset under either their canonical or legacy name. For variables
that are also Key Vault secrets, the value may be supplied either directly in
the environment or fetched from Key Vault at startup by
`Settings.load_secrets_from_keyvault`.

| Variable | Required? | Default | Purpose | Key Vault secret name |
| --- | --- | --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | Yes (import-time) | — | Azure OpenAI resource endpoint URL. | `azure-openai-endpoint` |
| `AZURE_OPENAI_KEY` | Yes (import-time) | — | Azure OpenAI API key. | `azure-openai-key` |
| `AZURE_OPENAI_VERSION` | Yes (import-time) | — | Azure OpenAI API version (e.g. `2024-02-01`). | `azure-openai-version` |
| `AZURE_OPENAI_LLM_MODEL` | No | `gpt-4o-mini` | Chat/completion model deployment name. | `azure-openai-llm-model` |
| `AZURE_OPENAI_EMBEDDING_MODEL` | Yes (import-time) | `text-embedding-3-small` (code fallback, unreachable — import check fires first) | Embedding model deployment name. | — |
| `EMBEDDING_DIMENSIONS` | No | `1536` | Embedding vector dimensionality. | — |
| `AZURE_SEARCH_ENDPOINT` | Yes (import-time) | — | Azure AI Search (Cognitive Search) endpoint URL. | `azure-search-endpoint` |
| `AZURE_SEARCH_KEY` | Yes (import-time) | — | Azure AI Search admin/query key. | `azure-search-key` |
| `INDEX_NAME` | Yes (import-time) | `vector-demo-custom-03` (code fallback, unreachable — import check fires first) | Search index to query. | — |
| `AZURE_SEARCH_SEMANTIC_CONFIG` | No | `""` (empty = disabled) | Name of the index's L2 semantic configuration. Empty disables semantic reranking (pure hybrid retrieval); set to the index's semantic config name to enable. Requires Azure AI Search Standard (S1) tier or higher; on unsupported tiers retrieval falls back to hybrid. | — |
| `AZURE_CLIENT_ID` | Required for Key Vault auth | — | Service principal (app registration) client ID, used to authenticate to Key Vault. | `azure-client-id` |
| `AZURE_CLIENT_SECRET` | Required for Key Vault auth | — | Service principal client secret. | `azure-client-secret` |
| `AZURE_TENANT_ID` | Required for Key Vault auth | — | Azure AD tenant ID. | `azure-tenant-id` |
| `AZURE_KEY_VAULT` | Required for Key Vault auth | — | Key Vault resource name (without FQDN); URL is built as `https://<name>.vault.azure.net`. | — |
| `AUDIENCE_ID` | No | — | Audience identifier for Azure AD JWT validation (e.g. app registration client ID). | — |
| `QNA_AGENT_ENABLED` | No | `false` (off) | Feature flag (read live per request, no redeploy) enabling the agentic LangGraph layer for `/qna`. Truthy values: `1`, `true`, `yes`, `on`. When off, `/qna` uses the legacy direct answer path. | — |
| `QNA_DEBUG_LOG_PREVIEW` | No | unset (off) | Debug flag: when truthy (`1`/`true`/`yes`), logs a short preview of generated content. Leave unset in production. | — |
| `CORS_ALLOWED_ORIGINS` | No | `""` (deny all) | Comma-separated list of allowed CORS origins. Empty denies all cross-origin requests (production default). | — |
| `LOG_LEVEL` | No | `INFO` | Root log level. | — |
| `LOG_FILE` | No | unset (stdout only) | Optional path for local file logging. Leave empty in containers. | — |
| `UVICORN_WORKERS` | No | `2` | Uvicorn worker count. Consumed by the container entrypoint (`Dockerfile`), not Python. | — |

> Note: `AZURE_OPENAI_EMBEDDING_MODEL` and `INDEX_NAME` appear in the import-time
> `required_env_vars` list, so the process exits if they are unset even though
> `LocalConfig` defines code-level fallback defaults. The fallbacks are therefore
> unreachable in practice; the required check wins.

### Legacy aliases and the dual-read fallback

Prior to the P0-7 naming normalization, the Q&A service used `PascalCase`
environment variable names. For one release, each canonical variable below is
read with a dual-read fallback: the canonical name is read first; if unset, the
legacy alias is read and a one-shot deprecation warning is logged. For Key Vault
lookups, the canonical hyphenated secret is tried first, then the legacy
`PascalCase` secret name (which is alphanumeric and therefore a valid Key Vault
name); whichever resolves is written back into the process under the canonical
env name so all downstream code reads the canonical name uniformly.

| Canonical env var | Legacy alias (deprecated) |
| --- | --- |
| `AZURE_OPENAI_ENDPOINT` | `AzureOpenaiAccountEndpoint` |
| `AZURE_OPENAI_KEY` | `TocdocOpenAIKey` |
| `AZURE_OPENAI_VERSION` | `AzureOpenaiApiVersion` |
| `AZURE_OPENAI_LLM_MODEL` | `AzureOpenaiLlmModel` |
| `AZURE_SEARCH_ENDPOINT` | `AzureSearchEndpoint` |
| `AZURE_SEARCH_KEY` | `AzureSearchKey` |
| `AZURE_CLIENT_ID` | `TocdocSPClientID` |
| `AZURE_CLIENT_SECRET` | `TocdocSPSecretValue` |
| `AZURE_TENANT_ID` | `TocdocSPTenantID` |

New deployments should set only the canonical `UPPER_SNAKE` names. The legacy
aliases will be removed in a later release.

## Ingestion service

Source of truth: ingestion service code (`services/ingestion/`) and
`services/ingestion/.env.example`.

Unlike the Q&A service, the ingestion service has **no import-time validation**
and **no Key Vault loader**: every variable is read with a bare `os.getenv` at
the point of use. Core Azure variables are therefore "required at runtime" — the
service starts without them, but the relevant Azure SDK call fails when that
code path executes. Connector and admin variables are only consulted when the
corresponding feature runs, so they are conditionally required.

Secrets for this service are supplied directly through the environment (or the
container's secret references); the ingestion service does not read from Key
Vault itself, so the Key Vault secret-name column does not apply here.

| Variable | Required? | Default | Purpose |
| --- | --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | Yes (at runtime) | — | Azure OpenAI endpoint for embedding generation. |
| `AZURE_OPENAI_KEY` | Yes (at runtime) | — | Azure OpenAI API key. |
| `AZURE_OPENAI_VERSION` | Yes (at runtime) | — | Azure OpenAI API version. |
| `AZURE_OPENAI_EMBEDDING_MODEL` | Yes (at runtime) | — | Embedding model deployment name. |
| `AZURE_SEARCH_ENDPOINT` | Yes (at runtime) | — | Azure AI Search endpoint (index create/upsert). |
| `AZURE_SEARCH_KEY` | Yes (at runtime) | — | Azure AI Search admin key. |
| `INDEX_NAME` | Yes (at runtime) | — | Target search index name. |
| `DOC_INTELLIGENCE_ENDPOINT` | Yes (at runtime) | — | Azure Document Intelligence endpoint for parsing. |
| `DOC_INTELLIGENCE_KEY` | Yes (at runtime) | — | Azure Document Intelligence key. |
| `AZURE_CLIENT_ID` | No (template only) | — | Service principal client ID. Present in `.env.example` for parity, but not read by ingestion code (connectors use their own credential paths). |
| `AZURE_CLIENT_SECRET` | No (template only) | — | Service principal secret. Not read by ingestion code. |
| `AZURE_TENANT_ID` | No (template only) | — | Azure AD tenant ID. Not read by ingestion code. |
| `AZURE_KEY_VAULT` | No (template only) | — | Key Vault name. Not read by ingestion code (no Key Vault loader). |
| `ADMIN_API_TOKEN` | Required for `/admin/*` | — | Static shared secret guarding admin routes via the `X-Admin-Token` header. If unset, admin routes return `503`. Interim auth, to be replaced by Azure AD. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `CONNECTOR_BOT_TAG` | Required for connector sync | — | Identifier tag applied to documents pulled by a connector sync run. The connector sync endpoint raises if unset. |
| `CONNECTOR_FR_MODE` | No | `read` | Document Intelligence ("form recognizer") mode for connector-sourced documents. |
| `BLOB_ACCOUNT_URL` | Required for Blob sync (managed identity) | — | Azure Blob Storage account URL; preferred auth path via `DefaultAzureCredential` (managed identity). |
| `BLOB_STORAGE_CONNECTION_STRING` | Required for Blob sync (fallback) | — | Connection string fallback when `BLOB_ACCOUNT_URL` / managed identity is unavailable. At least one of `BLOB_ACCOUNT_URL` or this must be set for Blob sync. |
| `BLOB_CONTAINER` | Required for Blob sync | — | Blob container to sync from. The Blob sync endpoint raises if unset. |
| `SHAREPOINT_TENANT_ID` | Required for SharePoint sync | — | Azure AD tenant ID for the SharePoint connector's `ClientSecretCredential`. |
| `SHAREPOINT_CLIENT_ID` | Required for SharePoint sync | — | App registration client ID for the SharePoint connector. |
| `SHAREPOINT_CLIENT_SECRET` | Required for SharePoint sync | — | App registration secret for the SharePoint connector. |
| `SHAREPOINT_SITE_ID` | Required for SharePoint sync | — | Target SharePoint site ID. The SharePoint sync endpoint raises if unset. |
| `SHAREPOINT_DRIVE_ID` | Required for SharePoint sync | — | Target SharePoint drive ID. The SharePoint sync endpoint raises if unset. |
| `CORS_ALLOWED_ORIGINS` | No | `""` (deny all) | Comma-separated allowed CORS origins. Empty denies all cross-origin requests (production default). |
| `LOG_LEVEL` | No | `INFO` | Root log level. |
| `LOG_FILE` | No | unset (stdout only) | Optional path for local file logging. Leave empty in containers. |
| `UVICORN_WORKERS` | No | `2` | Uvicorn worker count. Consumed by the container entrypoint (`Dockerfile`), not Python. |

## Variable count

- **Q&A service:** 20 distinct variables (plus 9 deprecated legacy aliases).
- **Ingestion service:** 28 distinct variables.
- **Total distinct variables across both services:** 34 (the shared Azure
  OpenAI/Search and CORS/log/Uvicorn variables are counted once in this union).
