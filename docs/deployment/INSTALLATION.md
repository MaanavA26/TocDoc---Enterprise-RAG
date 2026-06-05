# TocDoc — Client Installation Guide

This guide covers a complete, runnable TocDoc deployment into a new Azure
resource group. Following all steps in order results in both services starting
successfully after the container image swap.

> To validate a deployment end-to-end against live Azure (deploy → configure →
> ingest → ask → admin API → connector sync), see the
> [live-deployment smoke runbook](./SMOKE_TEST.md).

## What Bicep wires automatically vs what you provide

| Value | How it is set |
|---|---|
| OpenAI / Search / Doc Intelligence endpoints | Computed from deployed resources → wired as plain env vars |
| API version, model names, index name | Parameters with sensible defaults → wired as plain env vars |
| `AUDIENCE_ID`, `TocdocSPTenantID`, `AZURE_KEY_VAULT` | Parameters → wired as plain env vars |
| API keys (`openAiApiKey`, `searchApiKey`, `docIntelApiKey`) | Passed as `@secure()` params at deploy time → stored as Container App secrets, never in plain config |
| SP credentials (`spClientId`, `spClientSecret`) | Passed as `@secure()` params → stored as Container App secrets (current QnA auth path for Key Vault) |
| `adminApiToken` (interim admin auth) | Passed as `@secure()` param → stored as Container App secret on the ingestion app; injected as `ADMIN_API_TOKEN` env var. See *Step 5: Admin API verification* for usage. |

> **Auth model note**: The QnA service currently uses `ClientSecretCredential`
> (service principal) to load secrets from Key Vault at startup. System-assigned
> managed identities are provisioned and granted Key Vault Secrets User in this
> template as infrastructure preparation for a future migration to
> `ManagedIdentityCredential`. They do not affect the current startup path.

## Prerequisites

- Azure subscription with Contributor access on the target resource group
- Azure CLI ≥ 2.50 logged in (`az login`)
- Docker (for building container images)
- Service principal with Key Vault access created for the QnA service

## Step 1: Provision Azure resources

Gather these values before running the command:
- `<tenant-id>` — Azure AD tenant ID
- `<audience-client-id>` — App registration client ID for JWT audience
- `<openai-key>` — Azure OpenAI API key (from the resource after deploy, or use an existing one)
- `<search-key>` — Azure Cognitive Search admin key
- `<doc-intel-key>` — Document Intelligence API key
- `<sp-client-id>` — Service principal client ID for QnA Key Vault access
- `<sp-client-secret>` — Service principal client secret
- `<admin-api-token>` — Strong random token for the interim admin API guard. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Store the generated value securely — operators will need it to call `/admin/*` endpoints.

```bash
az group create --name rg-tocdoc-<client-name> --location <region>

az deployment group create \
  --resource-group rg-tocdoc-<client-name> \
  --template-file infra/main.bicep \
  --parameters infra/parameters/prod.bicepparam \
  --parameters \
      tenantId=<tenant-id> \
      audienceClientId=<audience-client-id> \
      openAiApiKey=<openai-key> \
      searchApiKey=<search-key> \
      docIntelApiKey=<doc-intel-key> \
      spClientId=<sp-client-id> \
      spClientSecret=<sp-client-secret> \
      adminApiToken=<admin-api-token>
```

The `@secure()` parameters are not logged or shown in Azure deployment history.

Save the deployment outputs — you will need the FQDNs for smoke testing:

```bash
az deployment group show \
  --resource-group rg-tocdoc-<client-name> \
  --name main \
  --query "properties.outputs" -o json
```

## Step 2: Verify container app configuration

Confirm both apps have the expected env/secret names configured before swapping
the image. A missing env var here means the app will fail at startup.

```bash
# Check ingestion env vars
az containerapp show \
  --name tocdoc-ingestion-prod \
  --resource-group rg-tocdoc-<client-name> \
  --query "properties.template.containers[0].env[].name" -o tsv

# Expected output should include:
# AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_VERSION, AZURE_OPENAI_EMBEDDING_MODEL,
# AZURE_SEARCH_ENDPOINT, INDEX_NAME, DOC_INTELLIGENCE_ENDPOINT, LOG_LEVEL,
# AZURE_OPENAI_KEY, AZURE_SEARCH_KEY, DOC_INTELLIGENCE_KEY, ADMIN_API_TOKEN

# Check QnA env vars
az containerapp show \
  --name tocdoc-qna-prod \
  --resource-group rg-tocdoc-<client-name> \
  --query "properties.template.containers[0].env[].name" -o tsv

# Expected output should include:
# AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_VERSION, AZURE_OPENAI_LLM_MODEL,
# AZURE_OPENAI_EMBEDDING_MODEL, AZURE_SEARCH_ENDPOINT, INDEX_NAME,
# AUDIENCE_ID, AZURE_KEY_VAULT, AZURE_TENANT_ID, LOG_LEVEL,
# AZURE_OPENAI_KEY, AZURE_SEARCH_KEY, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
```

## Migrating from a pre-P0-7 deployment

If you deployed TocDoc before the P0-7 env-var normalization landed, your
Container App's QnA service may still have the legacy PascalCase env var
names. The service code accepts both forms during the deprecation window —
the legacy name will work but emit a one-shot WARNING log per name when
the value is first resolved. For most variables this happens at app
**import time** (the `required_env_vars` check and the `Settings` class
attribute initialization run when `src.config.config` is imported), so
warnings typically appear in container startup logs rather than per-request.

To clear the warnings and align with the new contract:

1. **Re-deploy with the updated Bicep template**, which now wires the
   canonical UPPER_SNAKE names automatically. The
   `az deployment group create` command in Step 1 above is unchanged —
   only the Bicep template internals have moved to canonical names.

2. **Or, patch the existing Container App in place** (no redeploy):
   ```bash
   az containerapp update \
     --name tocdoc-qna-prod \
     --resource-group rg-tocdoc-<client-name> \
     --set-env-vars \
       AZURE_OPENAI_ENDPOINT=<openai-endpoint> \
       AZURE_OPENAI_VERSION=2024-02-01 \
       AZURE_OPENAI_LLM_MODEL=gpt-4o-mini \
       AZURE_SEARCH_ENDPOINT=<search-endpoint> \
       AZURE_TENANT_ID=<tenant-id> \
     --remove-env-vars \
       AzureOpenaiAccountEndpoint AzureOpenaiApiVersion AzureOpenaiLlmModel \
       AzureSearchEndpoint TocdocSPTenantID
   ```
   Mirror the same pattern for the secret-backed env vars
   (`TocdocOpenAIKey` → `AZURE_OPENAI_KEY`, etc.) via
   `az containerapp secret set` + `az containerapp update`.

3. **If you store secrets in Key Vault**, the loader's dual-read handles
   them transparently. **Key Vault secret naming is different from env-var
   naming** — Azure Key Vault only allows `^[a-zA-Z0-9-]+$` in secret
   names, so the canonical KV form is hyphenated-lowercase, not the
   UPPER_SNAKE env-var form. The loader looks up each canonical env var
   under the following KV name precedence:

   | Canonical env var       | Canonical KV secret name  | Legacy KV secret name (P0-7 fallback) |
   |---                       |---                         |---                                     |
   | `AZURE_OPENAI_ENDPOINT`  | `azure-openai-endpoint`    | `AzureOpenaiAccountEndpoint`           |
   | `AZURE_OPENAI_KEY`       | `azure-openai-key`         | `TocdocOpenAIKey`                      |
   | `AZURE_OPENAI_VERSION`   | `azure-openai-version`     | `AzureOpenaiApiVersion`                |
   | `AZURE_OPENAI_LLM_MODEL` | `azure-openai-llm-model`   | `AzureOpenaiLlmModel`                  |
   | `AZURE_SEARCH_ENDPOINT` | `azure-search-endpoint`    | `AzureSearchEndpoint`                  |
   | `AZURE_SEARCH_KEY`       | `azure-search-key`         | `AzureSearchKey`                       |
   | `AZURE_CLIENT_ID`        | `azure-client-id`          | `TocdocSPClientID`                     |
   | `AZURE_CLIENT_SECRET`    | `azure-client-secret`      | `TocdocSPSecretValue`                  |
   | `AZURE_TENANT_ID`        | `azure-tenant-id`          | `TocdocSPTenantID`                     |

   The loader tries the hyphenated canonical name first, then the legacy
   PascalCase name on `ResourceNotFoundError`, then records the secret as
   missing. The resolved value is written into `os.environ` under the
   canonical UPPER_SNAKE name regardless of which KV name matched.

   **Do not create Key Vault secrets with underscores** (e.g.,
   `AZURE_OPENAI_KEY` as the KV secret name) — Azure will reject the
   request with a 400, and the loader will not see the value. Use the
   hyphenated canonical form when creating new KV secrets; existing
   PascalCase secrets keep working without rename.

Full rename table (legacy → canonical):

| Pre-P0-7 (legacy)              | P0-7 canonical              |
|---                              |---                          |
| `AzureOpenaiAccountEndpoint`    | `AZURE_OPENAI_ENDPOINT`     |
| `TocdocOpenAIKey`               | `AZURE_OPENAI_KEY`          |
| `AzureOpenaiApiVersion`         | `AZURE_OPENAI_VERSION`      |
| `AzureOpenaiLlmModel`           | `AZURE_OPENAI_LLM_MODEL`    |
| `AzureSearchEndpoint`           | `AZURE_SEARCH_ENDPOINT`     |
| `AzureSearchKey`                | `AZURE_SEARCH_KEY`          |
| `TocdocSPClientID`              | `AZURE_CLIENT_ID`           |
| `TocdocSPSecretValue`           | `AZURE_CLIENT_SECRET`       |
| `TocdocSPTenantID`              | `AZURE_TENANT_ID`           |

The `AZURE_CLIENT_*` / `AZURE_TENANT_ID` names align with Azure SDK
`DefaultAzureCredential` conventions, so a future switch from
`ClientSecretCredential` to `DefaultAzureCredential` will require no
further rename.

## Step 3: Build and deploy container images

```bash
# Build and push (replace <your-registry> with your container registry)
docker build -t <your-registry>/tocdoc-ingestion:latest ./services/ingestion
docker push <your-registry>/tocdoc-ingestion:latest

docker build -t <your-registry>/tocdoc-qna:latest ./services/qna
docker push <your-registry>/tocdoc-qna:latest

# Swap the placeholder image — all env vars and secrets are already configured
az containerapp update \
  --name tocdoc-ingestion-prod \
  --resource-group rg-tocdoc-<client-name> \
  --image <your-registry>/tocdoc-ingestion:latest

az containerapp update \
  --name tocdoc-qna-prod \
  --resource-group rg-tocdoc-<client-name> \
  --image <your-registry>/tocdoc-qna:latest
```

After the image swap the real services start immediately — all required env vars
and secret values are already present in the container app configuration.

## Step 4: Smoke test

```bash
INGESTION_FQDN=$(az deployment group show \
  --resource-group rg-tocdoc-<client-name> --name main \
  --query "properties.outputs.ingestionAppFqdn.value" -o tsv)

QNA_FQDN=$(az deployment group show \
  --resource-group rg-tocdoc-<client-name> --name main \
  --query "properties.outputs.qnaAppFqdn.value" -o tsv)

curl https://${INGESTION_FQDN}/upload_pipeline/health
curl https://${QNA_FQDN}/qna/health

# Expected: {"status":"healthy"} and {"status":"ok","qna_module":"loaded",...}
```

## Step 5: Admin API verification

The ingestion service exposes a read-only admin API under `/admin/*` for inspecting
indexed documents within a `bot_tag` (workspace) scope. Auth is **interim** — a
static `X-Admin-Token` header compared against the `ADMIN_API_TOKEN` env var on
the ingestion app. Operators replace this with full Azure AD auth in a later release.

> **Route prefix**: the ingestion FastAPI app sets `root_path="/upload_pipeline"`,
> and the Container Apps ingress in `infra/main.bicep` forwards the path unchanged
> to the container. Public admin URLs therefore include the prefix
> (`/upload_pipeline/admin/...`), matching the existing health pattern
> (`/upload_pipeline/health`) used in Step 4. If a future deployment introduces
> ingress-level path rewriting that strips `/upload_pipeline`, update both the
> health and admin URL examples accordingly.

```bash
INGESTION_FQDN=$(az deployment group show \
  --resource-group rg-tocdoc-<client-name> --name main \
  --query "properties.outputs.ingestionAppFqdn.value" -o tsv)

ADMIN_TOKEN=<the-admin-api-token-you-passed-at-deploy>

# List documents in a workspace (groups chunks by document_id)
curl -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "https://${INGESTION_FQDN}/upload_pipeline/admin/documents?bot_tag=client_a_hr"

# Get one document's summary (chunk_count + sample chunks)
curl -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "https://${INGESTION_FQDN}/upload_pipeline/admin/documents/<document_id>?bot_tag=client_a_hr"

# Aggregate stats for a workspace (document count, chunk count, breakdowns)
curl -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "https://${INGESTION_FQDN}/upload_pipeline/admin/index/stats?bot_tag=client_a_hr"
```

Expected response codes:
- `200` — successful read
- `401` — missing or wrong `X-Admin-Token`
- `404` — document does not exist in this `bot_tag` scope (cross-tenant lookups also return 404; the API never reveals foreign-scope data)
- `422` — invalid `bot_tag` or `document_id` format (regex-validated at the boundary)
- `503` — admin service misconfigured (missing `ADMIN_API_TOKEN` or required search env vars on the container)

Store `ADMIN_TOKEN` in your operator secret manager. **Do not** commit it, share it over chat, or check it into any infrastructure repo — rotate via `az containerapp secret set` if exposed.

### Connector sync control plane

The same admin API also exposes a small control plane for triggering connector
syncs and checking their status. A sync runs the configured source's
enumerate→fetch→upload loop as an in-process background task; the source→`bot_tag`
binding and per-source location come entirely from env vars on the ingestion app
(`CONNECTOR_BOT_TAG`, `BLOB_CONTAINER`, `SHAREPOINT_SITE_ID`/`SHAREPOINT_DRIVE_ID`),
never from the request, so a sync can never write into a foreign workspace.

```bash
# Trigger a sync (source_type is `blob` or `sharepoint`).
# Returns 202 with a run_id; the sync runs in the background.
curl -X POST -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "https://${INGESTION_FQDN}/upload_pipeline/admin/connectors/blob/sync"
# → {"run_id":"<hex>","source_type":"blob"}

# Get one run's status by run_id (status: started | completed | failed).
curl -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "https://${INGESTION_FQDN}/upload_pipeline/admin/connectors/runs/<run_id>"

# List recent runs, newest first (optional ?limit=N, 1–200, default 50).
curl -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "https://${INGESTION_FQDN}/upload_pipeline/admin/connectors/runs?limit=20"
```

Expected response codes for the control plane:
- `202` — sync accepted and scheduled; the body carries the `run_id` to poll
- `200` — run status returned (single run or list)
- `400` — unsupported `source_type`, or the connector is misconfigured (missing required env vars)
- `401` — missing or wrong `X-Admin-Token`
- `404` — no run found for that `run_id`

**Run status is in-process and lost on restart.** The run-status store lives in
the ingestion process's memory — it is **not** durable and is **not** shared
across replicas. A restart, redeploy, or scale event clears it, and a run_id is
only visible on the replica that served the trigger. It is also bounded (oldest
runs are evicted once the cap is reached). Treat it as a best-effort live view of
recent activity, not an audit log; durable run history is a documented follow-up.

Because of this, a `404` on `…/connectors/runs/<run_id>` means the run is
**unknown, evicted, or lost on a restart** — it does **not** mean a just-created
run is still pending. A run_id returned by a `202` is recorded as `started`
synchronously before the response, so an immediate status poll for that id will
return `200` (`started`), never `404`, on the replica that issued it.

## Operations: request correlation and observability

Both services emit a correlation ID on every request. Use this to trace a single
client interaction end-to-end across services and Application Insights logs.

### How `X-Request-ID` works

- **2xx, 3xx, 4xx, and `HTTPException`-derived 5xx responses** carry an
  `X-Request-ID` header. This covers the vast majority of error paths a
  well-coded service produces, including `HTTPException(status_code=500, ...)`.
- **Server-generated IDs are UUID4**.
- **Clients may supply their own `X-Request-ID`**. The middleware validates it
  against `^[A-Za-z0-9_-]{1,128}$` (defense against log injection); valid values
  are reused unchanged, anything malformed or oversize is replaced with a fresh
  UUID4. A structured `invalid_request_id_rejected` event is logged when this
  happens — the bad value is **never** written to logs.
- Inside server code, the current request's ID is available via
  `request.state.request_id` and via the module-level `get_current_request_id()`
  helper (which reads a `ContextVar`).

> **Known limitation (deferred to P0-6):** a 500 response generated by
> Starlette's `ServerErrorMiddleware` for an *unhandled* exception (i.e., a
> non-`HTTPException` that escapes the route handler) does **not** carry
> `X-Request-ID`. The structured `request_failed` log event still fires with
> the correct `request_id`, so server-side correlation is intact. The
> error-contract workstream (P0-6) will add a dedicated handler that ensures
> every error response, including unhandled-exception 500s, carries the
> header.

### Smoke checks after deploy

Run these against the deployed app URLs and verify the listed expectations.
These checks compensate for the lack of full-app integration tests in CI
(the unit tests use a minimal FastAPI app to avoid importing heavy service
dependencies — see the observability PR for the rationale).

```bash
QNA_FQDN=$(az deployment group show \
  --resource-group rg-tocdoc-<client-name> --name main \
  --query "properties.outputs.qnaAppFqdn.value" -o tsv)

# 1. Both /health endpoints return an X-Request-ID generated server-side.
curl -i "https://${INGESTION_FQDN}/upload_pipeline/health" | grep -i 'x-request-id'
curl -i "https://${QNA_FQDN}/qna/health"                   | grep -i 'x-request-id'
#  → expect: one `X-Request-ID: <uuid4>` header on each response.

# 2. Client-supplied X-Request-ID is echoed unchanged.
curl -i -H "X-Request-ID: smoke-test-001" \
  "https://${INGESTION_FQDN}/upload_pipeline/health" | grep -i 'x-request-id'
#  → expect: `X-Request-ID: smoke-test-001`.

# 3. Malformed X-Request-ID is rejected and replaced.
curl -i -H "X-Request-ID: ;bad;value" \
  "https://${INGESTION_FQDN}/upload_pipeline/health" | grep -i 'x-request-id'
#  → expect: a fresh UUID4 (NOT `;bad;value`).
```

A direct check that `X-Request-ID` rides 4xx/5xx is not always trivial
post-deploy (the public endpoints require auth tokens for non-`/health`
paths), so the 2xx smoke checks above plus inspection of `request_started`
/ `request_completed` log records in Log Analytics are the canonical
verification path. With P0-6 (error-contract layer) shipped, every error
response — including unhandled-exception 500s — carries `X-Request-ID` in
both the body (`error.request_id`) and the response header.

### Querying logs by request ID

Logs are structured single-line JSON. Common events:

| Event | Where emitted | Use for |
|---|---|---|
| `request_started` | Middleware, on every request | Confirming a request reached the service |
| `request_completed` | Middleware, on successful response | Status code, latency_ms |
| `request_failed` | Middleware, on unhandled exception | error_class + safe_message, latency_ms |
| `invalid_request_id_rejected` | Middleware, when client sends a malformed header | Detecting misbehaving clients |

To trace one request end-to-end in Log Analytics:

```kusto
ContainerAppConsoleLogs_CL
| where Log_s contains "<request-id-from-x-request-id-header>"
| order by TimeGenerated asc
```

### What does NOT appear in logs

By design, the structured events do not contain:
- The full request body, conversation history, or document content
- Azure AD tokens / API keys / any secret values
- The user's email or other identity claims
- The raw text of unhandled exceptions (only `error_class` + a safe category)

If you need richer per-request debugging in development, set `LOG_LEVEL=DEBUG`
on the container app — but **never** in production.

## Error responses

Every 4xx and 5xx response from both services follows a single envelope:

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "Human-readable safe message",
    "request_id": "0d9c8d0e-8f7b-4a4e-9f6f-2a8a3e6f4d12"
  }
}
```

The accompanying response also includes `X-Request-ID` in the headers,
with the same value as `error.request_id`. Operators correlate a client
report with server logs by grepping Log Analytics for that ID.

### Error codes

The `code` field is part of the public API contract. The following codes
are returned today; new codes are added only when concrete callsites need
to distinguish a new failure category:

| Code | Typical status | Meaning |
|---|---|---|
| `INVALID_REQUEST` | 400, 413 | User-supplied input violates a required constraint (empty bot_tag, file too large, etc.) |
| `UNAUTHORIZED` | 401, 403 | Missing/invalid authentication credentials (JWT, admin token) |
| `NOT_FOUND` | 404 | The requested resource does not exist in the caller's scope |
| `VALIDATION_ERROR` | 422 | Request body or query parameters failed Pydantic validation. Includes a structured `errors` field with per-field detail. |
| `UPSTREAM_UNAVAILABLE` | 503 | An Azure dependency (OpenAI, Cognitive Search, Document Intelligence) is unreachable or returning errors |
| `INTERNAL_ERROR` | 500 | Unhandled server-side failure. Raw exception text is never returned; the structured `request_failed` log event in Log Analytics carries the matching `request_id` for server-side debugging. |

### Validation-error shape

For 422 responses only, the envelope includes a structured `errors` field:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "request_id": "...",
    "errors": [
      {
        "loc": ["body", "bot_tag"],
        "type": "string_type",
        "msg": "Input should be a valid string"
      }
    ]
  }
}
```

The per-field messages are length-capped (200 chars) and **never echo
back the input value** — only the field location, error type, and a
safe message.

### What does NOT appear in error responses

- Raw exception text or stack traces (those go to server logs only)
- Azure AD tokens, API keys, or any secret values
- The user's request body, conversation history, or document content
- The user's identity claims (`upn`, `email`, etc.)

If client tooling treated the pre-P0-6 QnA response as a success on
internal failure (a 200 with an `error` field embedded in the answer
shape), it must now handle a 500 with the envelope above — that is a
deliberate contract correction.

## Automated post-deploy validation

For a single-command sanity check across all of the above, run
`scripts/validate_deployment.sh` against your deployment. The script is
read-only — it never modifies Azure resources — and validates resource
presence, Container App revision state, env-var name coverage (canonical
+ legacy-deprecation warnings), Key Vault readiness, Cognitive Search
service presence, Bicep deployment outputs, and HTTP health endpoints.

```bash
./scripts/validate_deployment.sh \
  --resource-group rg-tocdoc-<client-name> \
  --ingestion-app tocdoc-ingestion-prod \
  --qna-app tocdoc-qna-prod
```

Optional flags:
- `--environment <env>` — environment tag (default `prod`)
- `--deployment-name <name>` — Bicep deployment name (default `main`)
- `--expected-index-name <name>` — Cognitive Search index name (default `tocdoc-index`)
- `--skip-health-checks` — useful when the operator is on a network without egress to the Container App FQDNs, or when revisions are scaled to zero with a long cold-start
- `--output text|json` — JSON output is convenient for piping into a runbook automation step; default is human-readable text

Exit codes: `0` all required checks passed (warnings allowed), `1` at least one required check failed, `2` script usage / preflight error (`az` missing, not logged in, etc.).

The script never prints secret values — it validates env var **names** only via `az containerapp show --query ...env[].name`. It is safe to run from an operator's workstation or to wire into a CI job after a deployment step.

## Estimated installation time

| Step | Time |
|---|---|
| Infrastructure provisioning (Step 1) | 10–15 min |
| Config verification (Step 2) | 2 min |
| Image build + push + update (Step 3) | 5–10 min |
| Smoke test (Step 4) | 2 min |
| Admin API verification (Step 5) | 2 min |
| Automated validation (`scripts/validate_deployment.sh`) | 1–2 min |
| **Total** | **~25–37 min** |
