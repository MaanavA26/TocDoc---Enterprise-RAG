# Live-Azure Deployment Smoke Runbook

End-to-end validation of a real TocDoc deployment against **live Azure** — not
mocks. Following this runbook from a clean resource group, an owner can deploy
both services, configure the secret/binding contract, ingest a document, ask a
question, exercise the admin API, and confirm "healthy" in roughly 15–30 minutes
(most of it Azure provisioning time).

Everything below uses placeholders (`<...>`). **Never** paste real keys, tokens,
tenant IDs, or FQDNs into shared documents or commit history.

> Companion docs: [`INSTALLATION.md`](./INSTALLATION.md) (full install guide),
> [`../CONFIGURATION.md`](../CONFIGURATION.md) (every environment variable),
> and `scripts/validate_deployment.sh` (automated post-deploy checks).

---

## 0. How the service URLs are built (read this first)

Both FastAPI apps are mounted under a non-empty `root_path` set in the app
constructor:

- QnA — `root_path="/qna"`  (`services/qna/app.py`), container port **5500**
- Ingestion — `root_path="/upload_pipeline"`  (`services/ingestion/app.py`), container port **5501**

The container entrypoint runs `uvicorn app:app` (no `--root-path` flag); the
prefix lives in the app object. The practical rule for every external call is:

```
<external path> = <root_path> + <route path>
```

So the routes you will exercise resolve to:

| Service | Route in code | External path |
|---|---|---|
| QnA | `GET /health` | `/qna/health` |
| QnA | `POST /qna` | `/qna/qna` |
| QnA | `POST /qna/stream` | `/qna/qna/stream` |
| Ingestion | `GET /health` | `/upload_pipeline/health` |
| Ingestion | `POST /upload` | `/upload_pipeline/upload` |
| Ingestion | `GET /admin/index/stats` | `/upload_pipeline/admin/index/stats` |
| Ingestion | `GET /admin/documents` | `/upload_pipeline/admin/documents` |
| Ingestion | `POST /admin/connectors/{type}/sync` | `/upload_pipeline/admin/connectors/{type}/sync` |
| Ingestion | `GET /admin/connectors/runs/{run_id}` | `/upload_pipeline/admin/connectors/runs/{run_id}` |

The doubled `/qna/qna` is expected — it is the same `root_path + route` rule that
makes `/qna/health` work. This matches the health paths probed by
`scripts/validate_deployment.sh` (`/qna/health`, `/upload_pipeline/health`).

---

## 1. Prerequisites

### Tooling

- An **Azure subscription** and a target **resource group** with Contributor access.
- **Azure CLI** ≥ 2.50, logged in:
  ```bash
  az login
  az account set --subscription <subscription-id>
  ```
- The `containerapp` extension (installed on demand by recent CLIs; otherwise
  `az extension add --name containerapp`).
- **One of**: the Bicep toolchain (bundled with `az`) **or** Terraform ≥ 1.5 with
  the `azurerm` provider (for the Terraform path in Section 2).
- `curl` and `python3` (token generation and SSE).
- Your own **container images** for both services pushed to a registry the
  Container Apps environment can pull from. The IaC ships with the public
  `mcr.microsoft.com/azuredocs/containerapps-helloworld:latest` placeholder; the
  smoke test only passes once the real images are in place (see Section 2,
  "Swap the images").

### Azure resources the IaC provisions

Both `infra/main.bicep` and `infra/terraform/` provision the full set, named
`<prefix>-<role>-<environment>` (default `prefix=tocdoc`):

| Resource | Default name (`prefix=tocdoc`, `environment=prod`) | Purpose |
|---|---|---|
| Azure OpenAI | `tocdoc-openai-prod` | Chat + embedding models |
| Cognitive (AI) Search | `tocdoc-search-prod` | Vector/hybrid index `tocdoc-index` |
| Document Intelligence | `tocdoc-docintel-prod` | PDF parsing (FormRecognizer) |
| Key Vault | `tocdoc-kv-prod` | QnA secret store (purge-protected) |
| Log Analytics | `tocdoc-logs-prod` | Container + platform logs |
| Application Insights | `tocdoc-appinsights-prod` | Distributed traces (opt-in) |
| Container Apps env | `tocdoc-containerenv-prod` | Hosts both apps |
| Ingestion Container App | `tocdoc-ingestion-prod` | `/upload`, admin, connectors (port 5501) |
| QnA Container App | `tocdoc-qna-prod` | `/qna`, `/qna/stream` (port 5500) |

> **Model deployments**: the IaC provisions the OpenAI **account**, not the model
> **deployments**. Before smoke-testing, create the deployments whose names match
> the env values — by default a chat deployment named `gpt-4o-mini`
> (`AZURE_OPENAI_LLM_MODEL`) and an embedding deployment named
> `text-embedding-3-small` (`AZURE_OPENAI_EMBEDDING_MODEL`). Mismatched
> deployment names surface as OpenAI `DeploymentNotFound` errors at first use.

### Secret / env-var contract

The services read **canonical `UPPER_SNAKE`** environment variables. The QnA
service can additionally hydrate them from Key Vault, where the same secrets use
**`hyphenated-lowercase`** names (Key Vault forbids underscores). The mapping
(`docs/CONFIGURATION.md` is the canonical reference):

| Canonical env var | Key Vault secret name | Used by |
|---|---|---|
| `AZURE_OPENAI_KEY` | `azure-openai-key` | both |
| `AZURE_SEARCH_KEY` | `azure-search-key` | both |
| `DOC_INTELLIGENCE_KEY` | `doc-intel-key` | ingestion |
| `AZURE_CLIENT_ID` | `azure-client-id` | QnA (Key Vault auth) |
| `AZURE_CLIENT_SECRET` | `azure-client-secret` | QnA (Key Vault auth) |
| `ADMIN_API_TOKEN` | `admin-api-token` | ingestion (`/admin/*`, `/upload`) |

> The Bicep path injects these as **Container App secrets** at deploy time. The
> Terraform path writes them to **Key Vault** and references them from there via
> a user-assigned identity. Either way the container sees the canonical
> `UPPER_SNAKE` env var at runtime.

Pre-P0-7 deployments using `PascalCase` names (e.g. `TocdocOpenAIKey`) still
work for the QnA service via a dual-read fallback, but **new deployments should
use canonical names only** (see CONFIGURATION.md → "Legacy aliases").

---

## 2. Deploy

The IaC ships **hardened defaults** you should understand before deploying:

- **Ingestion ingress is internal** (`ingestionIngressExternal=false` /
  `ingestion_ingress_external=false`). `/upload` is admin-token-authed but drives
  metered Document Intelligence + OpenAI + Search writes, so it is not reachable
  from the public internet by default. **This blocks the ingestion half of the
  smoke test from your laptop** — see "Reaching the ingestion app" below.
- **QnA ingress is external** — every request is Azure AD JWT-authed at the app
  layer, so `/qna` is reachable directly.
- **Data-plane key auth stays on** (`disableLocalAuth=false`): the services
  currently authenticate to OpenAI/Search/Document Intelligence with API keys.
- **Tracing is off** (`enableAppInsightsTracing=false`): App Insights is
  provisioned but the connection string is only injected when you opt in.

### Path A — Bicep

```bash
az group create --name rg-tocdoc-<client> --location <region>

az deployment group create \
  --resource-group rg-tocdoc-<client> \
  --name main \
  --template-file infra/main.bicep \
  --parameters infra/parameters/prod.bicepparam \
  --parameters \
      tenantId=<tenant-id> \
      audienceClientId=<app-registration-client-id> \
      openAiApiKey=<openai-key> \
      searchApiKey=<search-admin-key> \
      docIntelApiKey=<doc-intel-key> \
      spClientId=<sp-client-id> \
      spClientSecret=<sp-client-secret> \
      adminApiToken=<admin-api-token>
```

To expose ingestion for the smoke window and turn on tracing, add:

```bash
      ingestionIngressExternal=true \
      enableAppInsightsTracing=true
```

Read the wired endpoints/FQDNs back from the deployment outputs:

```bash
az deployment group show -g rg-tocdoc-<client> --name main \
  --query "properties.outputs.{qna:qnaAppFqdn.value, ingestion:ingestionAppFqdn.value, kv:keyVaultName.value}" -o json
```

### Path B — Terraform

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # then fill in non-secret values

terraform init
terraform plan  \
  -var 'resource_group_name=rg-tocdoc-<client>' \
  -var 'location=<region>' \
  -var 'tenant_id=<tenant-id>' \
  -var 'audience_client_id=<app-registration-client-id>' \
  -var 'openai_api_key=<openai-key>' \
  -var 'search_api_key=<search-admin-key>' \
  -var 'doc_intel_api_key=<doc-intel-key>' \
  -var 'sp_client_id=<sp-client-id>' \
  -var 'sp_client_secret=<sp-client-secret>' \
  -var 'admin_api_token=<admin-api-token>'

terraform apply   # same -var set, or supply secrets via TF_VAR_* env / a tfvars file
```

To expose ingestion, add `-var 'ingestion_ingress_external=true'`. Note the
Terraform module **provisions** App Insights but, unlike the Bicep, does **not**
inject `APPLICATIONINSIGHTS_CONNECTION_STRING` into the apps and has no tracing
flag — so the trace-reading step in Section 5 applies to Bicep deployments with
`enableAppInsightsTracing=true`. The deploying principal needs **Key Vault
Secrets Officer** on the vault to write the secrets — see
`infra/terraform/README.md`.

Read outputs:

```bash
terraform output
```

### Swap the images

The IaC deploys a public hello-world placeholder. Point both apps at your real
images before smoke-testing:

```bash
az containerapp update -g rg-tocdoc-<client> -n tocdoc-ingestion-prod --image <registry>/<ingestion-image>:<tag>
az containerapp update -g rg-tocdoc-<client> -n tocdoc-qna-prod       --image <registry>/<qna-image>:<tag>
```

(Terraform: set `-var 'container_image=<registry>/<image>:<tag>'` and re-apply.)

Each `update` / re-apply spins a new revision. Container Apps default to
`minReplicas=0`, so the **first** request after an idle period incurs a cold
start (up to ~60s).

### Reaching the ingestion app

If you left ingestion ingress internal (the default), its FQDN does not resolve
from outside the Container Apps environment. For the smoke test, either:

1. **Temporarily expose it** (deploy with `ingestionIngressExternal=true`, run the
   ingestion steps, then redeploy with it back to `false`); or
2. **Exec from inside the environment** (e.g. `az containerapp exec` into the QnA
   app and `curl http://<ingestion-internal-fqdn>/upload_pipeline/health`).

The examples below assume option 1 (a public ingestion FQDN) for copy-paste
simplicity. QnA is external regardless.

---

## 3. Configure

### 3.1 Set the Key Vault secrets (QnA reads from here)

If you deployed via Terraform the secrets are already in Key Vault. For the
Bicep path (or to rotate), populate the canonical hyphenated names:

```bash
KV=tocdoc-kv-prod   # or the keyVaultName output
az keyvault secret set --vault-name "$KV" --name azure-openai-key    --value <openai-key>
az keyvault secret set --vault-name "$KV" --name azure-search-key    --value <search-admin-key>
az keyvault secret set --vault-name "$KV" --name azure-client-id     --value <sp-client-id>
az keyvault secret set --vault-name "$KV" --name azure-client-secret --value <sp-client-secret>
```

(You need **Key Vault Secrets Officer** on the vault to write. The apps' identity
holds **Key Vault Secrets User** to read.)

### 3.2 Admin token

`ADMIN_API_TOKEN` guards `/upload` and every `/admin/*` route on the ingestion
service via the `X-Admin-Token` header. It is set by the deploy parameter above.
Generate a strong value with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Keep this value — you will send it as `X-Admin-Token` in Section 4.

### 3.3 Tenant-binding map (REQUIRED — binding is default-ON)

The QnA `/qna` request body carries a `bot_tag` selecting which workspace's
documents are retrieved. The within-tenant binding guard
(`services/qna/src/core/tenant_binding.py`) is **fail-closed and default-ON**:
with `QNA_ENFORCE_TENANT_BINDING` unset, enforcement is **ON**, and an
unset/empty `QNA_TENANT_BOT_TAG_MAP` makes **every `/qna` request fail closed
with 403**.

Neither the Bicep nor the Terraform IaC sets these, so you **must** configure the
map after deploy. It is a JSON object keyed by the caller's tenant id (the JWT
`tid` claim — never the request body), valued by the list of `bot_tag`s that
tenant may query:

```bash
az containerapp update -g rg-tocdoc-<client> -n tocdoc-qna-prod \
  --set-env-vars \
    'QNA_TENANT_BOT_TAG_MAP={"<your-tenant-id>":["smoke-workspace"]}'
```

The value is read live per request, but a Container Apps env change spins a new
revision (give it a few seconds). The `bot_tag` you ingest with and query with
must appear in this list for the caller's `tid`.

> Single-workspace deployment alternative: set
> `QNA_ENFORCE_TENANT_BINDING=false` to opt out entirely (only when `bot_tag` is
> scoped some other way). For a real smoke test, prefer configuring the map.

### 3.4 Semantic config (optional)

`AZURE_SEARCH_SEMANTIC_CONFIG` is empty by default (pure hybrid retrieval). To
enable L2 semantic reranking, set it to the index's semantic configuration name
(the ingestion pipeline creates one named `mySemanticConfig`). Requires Search
Standard (S1) tier or higher; on an unsupported tier retrieval falls back to
hybrid automatically.

```bash
az containerapp update -g rg-tocdoc-<client> -n tocdoc-qna-prod \
  --set-env-vars 'AZURE_SEARCH_SEMANTIC_CONFIG=mySemanticConfig'
```

---

## 4. Smoke test

Set up shell variables (use the FQDNs from your deploy outputs):

```bash
QNA_FQDN=<qna-app-fqdn>            # e.g. tocdoc-qna-prod.<region>.azurecontainerapps.io
ING_FQDN=<ingestion-app-fqdn>     # only resolvable if ingestion ingress is external
ADMIN_TOKEN=<admin-api-token>
BOT_TAG=smoke-workspace
```

### 4.1 Health probes (no auth)

```bash
curl -sS  "https://$QNA_FQDN/qna/health"
curl -sS  "https://$ING_FQDN/upload_pipeline/health"
```

### 4.2 Ingest a sample document (`/upload`, admin-token)

`/upload` requires the `X-Admin-Token` header and reads `filepath` as a path that
must resolve **inside** the ingestion container's allowed root
(`INGESTION_ALLOWED_UPLOAD_ROOT`, default `/app`). For a client-side file, send
the multipart `file` and a `filepath` under that root:

```bash
curl -sS -X POST \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -F "file=@./sample.pdf" \
  "https://$ING_FQDN/upload_pipeline/upload?bot_tag=$BOT_TAG&filepath=/app/sample.pdf&fr_mode=read"
```

- `fr_mode` is `read` (token-chunked) or `layout` (header-split).
- Supported types: PDF, DOCX, PPTX, HTML, MD, TXT. Unsupported → **415**.
- Success → `{"status": "successfully indexed", "detail": {...}}`.
- Partial index write → **207** `{"status": "partially indexed", ...}`.

### 4.3 Obtain a JWT for the QnA call

QnA validates Azure AD v2.0 Bearer tokens (RS256, JWKS from
`https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys`). The token's
`aud` must equal `AUDIENCE_ID`, and it must carry a `tid` claim (the binding guard
needs it). The simplest first-party token:

```bash
JWT=$(az account get-access-token \
  --resource "api://<app-registration-client-id>" \
  --query accessToken -o tsv)
```

> The app registration must expose that audience (`api://<client-id>` or its
> client-id GUID), and the calling principal must be authorized. If `aud` does
> not match `AUDIENCE_ID`, you get **401 `UNAUTHORIZED`**. The `tid` in the token
> must be a key in `QNA_TENANT_BOT_TAG_MAP` (Section 3.3) or the request fails
> closed with **403**.

### 4.4 Ask a question (`/qna`)

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d "{\"bot\":[{\"user_query\":\"What is in the sample document?\"}],\"fr_tag\":\"read\",\"bot_tag\":\"$BOT_TAG\"}" \
  "https://$QNA_FQDN/qna/qna"
```

Request body shape (`services/qna/src/utils/util.py` → `Payload`):
- `bot` — ordered list of turns, each `{"user_query": "...", "bot_response": <optional>}`.
- `fr_tag` — `read` or `layout` only (else 400).
- `bot_tag` — must be allowed for the caller's `tid`.

Success → `{"answer": "...", "citation": {...}}`.

### 4.5 Stream the answer (`/qna/stream`, SSE)

```bash
curl -sS -N -X POST \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d "{\"bot\":[{\"user_query\":\"Summarize the sample document.\"}],\"fr_tag\":\"read\",\"bot_tag\":\"$BOT_TAG\"}" \
  "https://$QNA_FQDN/qna/qna/stream"
```

`-N` disables curl buffering so you see events live. Wire format: each token is a
`data: <token>` event; the citation map arrives as `event: citation\ndata: <json>`;
the stream ends with `data: [DONE]`.

### 4.6 Exercise the admin API

```bash
# Per-bot index statistics
curl -sS -H "X-Admin-Token: $ADMIN_TOKEN" \
  "https://$ING_FQDN/upload_pipeline/admin/index/stats?bot_tag=$BOT_TAG"

# List indexed documents in the bot_tag scope
curl -sS -H "X-Admin-Token: $ADMIN_TOKEN" \
  "https://$ING_FQDN/upload_pipeline/admin/documents?bot_tag=$BOT_TAG"
```

- `index/stats` → `{"bot_tag": "...", "document_count": N, "chunk_count": M}`.
  After 4.2 both counts should be ≥ 1.
- `documents` → `{"bot_tag": "...", "count": N, "documents": [...]}`.

### 4.7 Trigger a connector sync + poll run status

Connector syncs need source-specific config on the **ingestion** app. For a Blob
sync: `CONNECTOR_BOT_TAG` (the tag applied to synced docs) plus
`BLOB_ACCOUNT_URL` (managed-identity path) or `BLOB_STORAGE_CONNECTION_STRING`
(fallback) and `BLOB_CONTAINER`. SharePoint needs the `SHAREPOINT_*` set (see
CONFIGURATION.md → Ingestion service). Set them before triggering, e.g.:

```bash
az containerapp update -g rg-tocdoc-<client> -n tocdoc-ingestion-prod \
  --set-env-vars CONNECTOR_BOT_TAG=$BOT_TAG BLOB_ACCOUNT_URL=<blob-account-url> BLOB_CONTAINER=<container>
```

Trigger the sync (returns **202** with a `run_id`):

```bash
RUN=$(curl -sS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
  "https://$ING_FQDN/upload_pipeline/admin/connectors/blob/sync" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")

# Poll status (status ∈ started | completed | failed)
curl -sS -H "X-Admin-Token: $ADMIN_TOKEN" \
  "https://$ING_FQDN/upload_pipeline/admin/connectors/runs/$RUN"
```

The run is recorded as `started` synchronously before the 202 returns, so an
immediate poll never 404s. Counts (`processed_count`, `failed_count`) populate on
completion. Run state is **in-process and lost on app restart** (v1 store).

### 4.8 Automated validation script

```bash
scripts/validate_deployment.sh \
  --resource-group rg-tocdoc-<client> \
  --ingestion-app tocdoc-ingestion-prod \
  --qna-app tocdoc-qna-prod \
  --environment prod
```

Read-only and secret-safe (validates env-var **names**, never values). It checks
the resource group, expected resources, active revisions, required env vars on
both apps, Key Vault, the Search service, the two `/health` endpoints, and the
Bicep deployment outputs. Exit `0` = all required checks passed (warnings OK),
`1` = a required check failed, `2` = preflight error. Add `--output json` for a
machine-readable report, or `--skip-health-checks` when on a network without
egress to the app FQDNs (e.g. ingestion left internal).

---

## 5. "Healthy looks like"

| Step | Healthy result |
|---|---|
| `GET /qna/health` | `200` `{"status":"ok","qna_module":"loaded","timestamp":...}` |
| `GET /upload_pipeline/health` | `200` (ingestion liveness) |
| `POST /upload` | `200` `{"status":"successfully indexed",...}` (or `207` partial) |
| `POST /qna` | `200` `{"answer":"...","citation":{...}}` |
| `POST /qna/stream` | SSE stream of `data:` token events, an `event: citation`, then `data: [DONE]` |
| `GET /admin/index/stats` | `200`, `document_count`/`chunk_count` ≥ 1 after an upload |
| `POST /admin/connectors/blob/sync` | `202` with a `run_id` |
| `GET /admin/connectors/runs/{run_id}` | `200`, `status` transitions `started`→`completed` |
| `validate_deployment.sh` | exit `0`, all required checks `[PASS]` |

### Reading App Insights traces

Distributed tracing is **default-OFF**: traces only exist if you deployed with
`enableAppInsightsTracing=true` (Section 2), which injects
`APPLICATIONINSIGHTS_CONNECTION_STRING` into both apps. Once enabled and after a
few requests, query the App Insights resource (`tocdoc-appinsights-prod`):

```bash
az monitor app-insights query \
  --app tocdoc-appinsights-prod -g rg-tocdoc-<client> \
  --analytics-query "requests | where timestamp > ago(15m) | project timestamp, name, resultCode, duration | order by timestamp desc"
```

A healthy run shows `requests` rows for the `/qna` and `/upload` calls with
`resultCode` 200/202 and end-to-end `dependencies` to OpenAI / Search /
Document Intelligence. You can also read live container logs:

```bash
az containerapp logs show -g rg-tocdoc-<client> -n tocdoc-qna-prod --tail 100
```

### Optional: answer quality (RAGAS)

If your repo checkout includes an evaluation harness, you can run it against the
**real** answers produced above (rather than the mocked-Azure fixtures used in
CI) to get groundedness / answer-relevance numbers on live retrieval. Treat this
as an optional quality gate, not a deployment gate — keep it scoped to a small,
known-answer question set so a single bad answer does not block the smoke sign-off.

---

## 6. Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `/qna` returns **403 UNAUTHORIZED** ("bot_tag not permitted") | Binding is default-ON and `QNA_TENANT_BOT_TAG_MAP` is unset/empty, or the caller's `tid` / `bot_tag` is not in the map | Configure the map (Section 3.3) keyed by the token `tid`; or set `QNA_ENFORCE_TENANT_BINDING=false` for single-workspace |
| `/qna` returns **401** | Missing/malformed `Authorization: Bearer`, wrong `aud` (≠ `AUDIENCE_ID`), bad issuer, or expired token | Re-mint with the correct `--resource api://<audience>`; verify `AUDIENCE_ID` and `AZURE_TENANT_ID` on the app |
| `/qna` returns **503** | JWKS endpoint unreachable from the app | Check egress to `login.microsoftonline.com`; retry |
| `/upload` or `/admin/*` returns **401** | Missing or wrong `X-Admin-Token` header | Send the exact `ADMIN_API_TOKEN` value as `X-Admin-Token` |
| `/upload` or `/admin/*` returns **503** | `ADMIN_API_TOKEN` not configured on the ingestion app | Set the secret/env var and redeploy the revision |
| Ingestion FQDN does not resolve / curl hangs | Ingestion ingress is internal by default | Deploy with `ingestionIngressExternal=true` for the smoke window, or exec from inside the environment (Section 2) |
| `/upload` returns **400** ("path") or rejects the file | `filepath` resolves outside `INGESTION_ALLOWED_UPLOAD_ROOT` (default `/app`) | Use a `filepath` under that root (e.g. `/app/sample.pdf`) |
| `/upload` returns **415** | Unsupported file extension | Use PDF/DOCX/PPTX/HTML/MD/TXT |
| `/qna` / `/qna/stream` returns **429** | Per-key rate limit or global concurrency cap hit | Honor `Retry-After` and retry |
| First request after idle is slow / times out | Cold start with `minReplicas=0` | Allow up to ~60s; retry |
| OpenAI `DeploymentNotFound` | Model deployment names don't match `AZURE_OPENAI_LLM_MODEL` / `AZURE_OPENAI_EMBEDDING_MODEL` | Create deployments with the matching names |
| Connector sync `run_id` poll returns **404** after a restart | Run-status store is in-process and lost on restart | Re-trigger; treat as v1 limitation |

---

## 7. Teardown

```bash
az group delete --name rg-tocdoc-<client> --yes --no-wait
```

> **Key Vault purge-protection gotcha**: the vault is provisioned with
> `enablePurgeProtection=true` and 90-day soft delete. Deleting the resource
> group leaves a **soft-deleted vault that cannot be purged** within the retention
> window. Re-deploying with the same `prefix`/`environment` (and therefore the
> same vault name) inside 90 days will conflict on the vault name. For repeat
> smoke runs, vary the `environment` (e.g. `dev`) or `prefix`, or wait out the
> retention period.

For Terraform deployments, prefer `terraform destroy` (same `-var` set) so state
stays consistent; the same Key Vault soft-delete caveat applies.
