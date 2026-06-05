# TocDoc — Operations Runbook

> Day-2 operations for the two TocDoc services running in a client's Azure
> subscription on Azure Container Apps. This is the operator's companion to the
> architecture and packaging docs:
>
> - System tour and request/ingestion flows: [`ARCHITECTURE.md`](ARCHITECTURE.md)
> - Cold-start / resume-from-dormancy context: [`RESUME.md`](RESUME.md)
> - Packaging tiers and deployment operating model: [`PRODUCT_TIERS.md`](PRODUCT_TIERS.md)
>
> Every procedure here is grounded in the shipped code. Where a capability is
> documented-but-not-implemented (the reindex endpoint) or has a known
> limitation (in-process connector run status), this runbook says so rather than
> describing an aspiration as a feature.

---

## 0. Topology at a glance

TocDoc runs as two stateless FastAPI services, each an Azure Container App in the
client's resource group (`infra/main.bicep`):

| Service | Container App (example) | Ingress target port | Public path prefix (`root_path`) |
|---|---|---|---|
| Ingestion | `tocdoc-ingestion-<env>` | 5501 | `/upload_pipeline` |
| QnA | `tocdoc-qna-<env>` | 5500 | `/qna` |

Both apps front Azure OpenAI, Azure AI Search, Document Intelligence, and Key
Vault. **No durable state lives in the services** — the only persistent state is
the AI Search index, Key Vault secrets, and the upstream document sources
(Blob / SharePoint). This shapes the entire scaling and DR story below.

### Path-prefix convention (read this before copy-pasting any curl)

Each service mounts its routes under its `root_path`, so the public URL prefix is
**baked into the FQDN path**:

- Ingestion health: `https://<ingestion-fqdn>/upload_pipeline/health`
- Ingestion admin API: `https://<ingestion-fqdn>/upload_pipeline/admin/...`
- QnA health: `https://<qna-fqdn>/qna/health`
- QnA answer endpoint: `https://<qna-fqdn>/qna` (`POST`)

These are exactly the paths `scripts/validate_deployment.sh` probes
(`/upload_pipeline/health`, `/qna/health`). Every example below uses the same
prefixes.

Resolve the FQDNs from the Bicep deployment outputs:

```bash
az deployment group show -g "$RG" --name main \
  --query 'properties.outputs.ingestionAppFqdn.value' -o tsv
az deployment group show -g "$RG" --name main \
  --query 'properties.outputs.qnaAppFqdn.value' -o tsv
```

---

## 1. Monitoring & key log events

### Where logs go

Each Container App writes stdout to the Container Apps Environment's
Log Analytics workspace (`infra/main.bicep`: `appLogsConfiguration.destination =
'log-analytics'`). Application logs land in the **`ContainerAppConsoleLogs_CL`**
table. There is **no Application Insights telemetry wired in**: App Insights is
provisioned and its connection string is exposed as the Bicep output
`appInsightsConnectionString`, but it is *not* injected into the container env,
so no SDK traces/metrics are emitted today. Query **Log Analytics**, not App
Insights, for the structured events below. (The connection string is there if a
future change wants to wire App Insights in — but don't go looking for traces
that aren't being sent.)

### Tail logs live

```bash
# Live stream from a Container App (pick the container name with --container)
az containerapp logs show --name tocdoc-qna-prod -g "$RG" --container qna --follow
az containerapp logs show --name tocdoc-ingestion-prod -g "$RG" --container ingestion --follow
```

### Query structured events (KQL against Log Analytics)

`log_event` (`observability.py`, duplicated in both services) emits **single-line
JSON** on stdout. Each line always carries `event` and `request_id`, drops
`None` fields, and truncates string values at 200 chars (so full answers,
document text, JWTs, and secrets are never logged).

```kusto
// All structured events for one correlation ID, oldest first
ContainerAppConsoleLogs_CL
| where Log_s has "request_id"
| where Log_s contains "<the-request-id>"
| project TimeGenerated, ContainerAppName_s, Log_s
| sort by TimeGenerated asc
```

```kusto
// Error rate / failed requests in the last hour
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1h)
| where Log_s contains '"event": "request_failed"'
| summarize count() by ContainerAppName_s, bin(TimeGenerated, 5m)
```

> The events are JSON; if your workspace parses it, use `extend p =
> parse_json(Log_s)` and filter on `p.event`, `p.request_id`, `p.latency_ms`,
> etc. The raw `Log_s contains` form above works regardless of parsing.

### Key log events (event → meaning → notable fields)

Request lifecycle (emitted by `RequestIDMiddleware` on every request, both
services):

| Event | Meaning | Notable fields |
|---|---|---|
| `request_started` | Request received; correlation ID minted/propagated | `request_id`, `path`, `method` |
| `request_completed` | Request finished | `request_id`, `status_code`, `latency_ms` |
| `request_failed` | Handler raised an unhandled exception | `request_id`, `error_class`, `safe_message` (a generic category, never `str(exc)`), `latency_ms` |
| `invalid_request_id_rejected` | Client-supplied `X-Request-ID` failed the safe-charset check and was replaced | `request_id` |

QnA pipeline (`services/qna/src/pipeline/qna_pipeline.py`):

| Event | Meaning | Notable fields |
|---|---|---|
| `query_rephrased` | Follow-up rephrased to a standalone query | `history_turns_used`, `latency_ms` |
| `retrieval_completed` | Tenant-scoped hybrid search returned chunks | `bot_tag`, `fr_tag`, `retrieved_chunk_count`, `top_k`, `latency_ms`, `source_document_ids`, `source_paths` |
| `answer_generated` | Grounded answer produced (body NOT logged) | `model`, `latency_ms`, `citation_count`, `answer_length_chars` |

Ingestion pipeline (`services/ingestion/custom_rag.py`) — one run is fully
traceable end-to-end by `request_id`:

| Event | Meaning |
|---|---|
| `ingestion_started` | A document/batch entered the single write path |
| `chunking_completed` | Parsed text chunked (token-aware for `read`, header-split for `layout`) |
| `embeddings_completed` | Chunk embeddings computed (`text-embedding-3-small`) |
| `index_upsert_completed` | Chunks merged/uploaded into AI Search |

Connector sync (`services/ingestion/connectors/`, surfaced via admin routes):

| Event | Meaning | Notable fields |
|---|---|---|
| `connector_sync_triggered` | Operator kicked off a sync | `request_id` (the trigger request), `run_id`, `source_type`, `bot_tag` |
| Connector start/complete/failed events | Background run progress, keyed on `run_id` | `run_id`, `source_type` |

> A QnA answer preview is logged **only** when `QNA_DEBUG_LOG_PREVIEW` is
> explicitly truthy (off by default; capped at 200 chars). Leave it off in
> production.

---

## 2. Scaling (Container Apps replicas)

Both apps ship with `scale: { minReplicas: 0, maxReplicas: 3 }`
(`infra/main.bicep`) and `cpu: 0.5 / memory: 1Gi` per replica.

- **`minReplicas: 0` means scale-to-zero.** With no traffic, replicas are
  removed and the next request pays a **cold start** (can take up to ~60s).
  `validate_deployment.sh` already accounts for this with a 45s curl timeout and
  treats `Idle`/`Provisioned` revision states as healthy.
- Inspect current scale / revision state:

  ```bash
  az containerapp revision list --name tocdoc-qna-prod -g "$RG" \
    --query "[?properties.active].{rev:name, state:properties.runningState, replicas:properties.replicas}" -o table
  ```

- Adjust scale bounds (e.g., keep one warm replica to kill cold starts, or raise
  the ceiling for heavier ingestion bursts):

  ```bash
  # Keep one replica warm (removes cold-start latency; costs an always-on replica)
  az containerapp update --name tocdoc-qna-prod -g "$RG" --min-replicas 1 --max-replicas 5

  # Bump CPU/memory for large ingestion batches
  az containerapp update --name tocdoc-ingestion-prod -g "$RG" --cpu 1.0 --memory 2Gi
  ```

- **Statelessness makes horizontal scale safe for the request paths.** QnA and
  the ingestion write path hold no module-level request state, so adding
  replicas never risks cross-request contamination.
- **Exception: connector run-status is per-replica (see §3).** The in-process
  run store is *not* shared across replicas — this is the one place where
  `maxReplicas > 1` changes operator behavior. Tier guidance for replica sizing
  is in [`PRODUCT_TIERS.md`](PRODUCT_TIERS.md) (per-tier Azure footprint).

---

## 3. Triggering & observing a connector sync

Connectors (Blob, SharePoint) are thin enumerator+downloader drivers that route
every document through the single `upload()` write path. The sync is an
**in-stack background task** behind the admin guard, so it inherits the error
envelope and request-ID middleware.

### Trigger a sync

```bash
INGEST_FQDN=$(az deployment group show -g "$RG" --name main \
  --query 'properties.outputs.ingestionAppFqdn.value' -o tsv)

# source_type is 'blob' or 'sharepoint'. Connector config (bot_tag, container,
# etc.) comes from the service's env — the trigger takes no body.
curl -sS -X POST \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  "https://${INGEST_FQDN}/upload_pipeline/admin/connectors/blob/sync"
# -> 202 Accepted: { "run_id": "<hex>", "source_type": "blob" }
```

The run is recorded as `started` **synchronously before the 202 returns**, so an
immediate status poll on the `run_id` can never 404 a just-created run.

### Observe a run

```bash
# One run's status
curl -sS -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  "https://${INGEST_FQDN}/upload_pipeline/admin/connectors/runs/<run_id>"

# Recent runs (newest first; limit 1..200, default 50)
curl -sS -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  "https://${INGEST_FQDN}/upload_pipeline/admin/connectors/runs?limit=50"
```

Status transitions: `started` → `completed` | `failed`. A failed run carries
`error_class` and a `safe_message` (never raw exception text). Correlate the
deep run detail in logs via the `run_id` (the background run logs
start/complete/failed events keyed on `run_id`, distinct from the trigger
request's `X-Request-ID`).

### Run-status limitations (operationally important)

The run store is **in-process v1 — deliberately not a durable or distributed
job store** (`connectors/run_status.py`):

1. **Lost on restart.** A revision restart/redeploy wipes run history. A 404
   from the status endpoint means unknown / evicted / lost-on-restart — never
   "just created."
2. **Per-replica, not shared.** With `maxReplicas: 3`, a run triggered on one
   replica is **invisible to a status poll routed to a different replica**. For
   reliable polling during a sync, scale ingestion to a single replica
   (`--min-replicas 1 --max-replicas 1`) for the duration, or rely on the
   `run_id`-keyed log events in Log Analytics (which are durable) instead of the
   status endpoint.
3. **Interruptible by scale-down.** With `minReplicas: 0`, a long background sync
   can be cut off if the replica scales to zero after the 202. For large initial
   syncs, pin a warm replica first (see §2).
4. **Bounded.** Only the most recent ~200 runs are retained in-process.

Because every connector document goes through the idempotent `upload()` path
(deterministic `document_id`, stale-chunk delete before upsert), **re-running an
interrupted sync is safe** — already-ingested unchanged documents are no-ops.

---

## 4. Deleting & reindexing tenant data

All admin endpoints are mounted under `/upload_pipeline/admin`, require the
`X-Admin-Token` header, are `bot_tag`-scoped, and OData-escape inputs.

### Inspect before you act

```bash
# List documents in a tenant scope
curl -sS -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  "https://${INGEST_FQDN}/upload_pipeline/admin/documents?bot_tag=<bot_tag>"

# Aggregate index stats for a tenant (doc/chunk counts, per-source-type/per-mode)
curl -sS -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  "https://${INGEST_FQDN}/upload_pipeline/admin/index/stats?bot_tag=<bot_tag>"
```

### Delete one document

```bash
curl -sS -X DELETE -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  "https://${INGEST_FQDN}/upload_pipeline/admin/documents/<document_id>?bot_tag=<bot_tag>"
# Idempotent: deleting a non-existent document returns 200 with deleted_chunks: 0.
# Both bot_tag and document_id filters always apply — other tenants are untouched.
```

### Delete an entire tenant (destructive — requires confirm)

```bash
curl -sS -X DELETE -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  "https://${INGEST_FQDN}/upload_pipeline/admin/bots/<bot_tag>/documents?confirm=true"
# Without ?confirm=true this returns 400 and deletes nothing — the gate runs
# before the search service is ever called.
```

### Reindexing — use re-ingestion, not the reindex endpoint

> **`POST /upload_pipeline/admin/documents/{document_id}/reindex` is a documented
> 501 stub** — there is no server-side source persistence to reindex from, so it
> validates auth/inputs and returns `501 Not Implemented`. Do **not** build a
> procedure around it.

The real reindex/lifecycle mechanism is **re-ingestion through the single write
path**, which is idempotent by construction (`custom_rag.py`):

- `document_id = sha256(content)[:16]` — identical bytes always map to the same
  document ID, and chunk IDs are deterministic
  (`{bot_tag}_{document_id}_{fr_mode}_{i:05d}`).
- Before upsert, **stale chunks for `(document_id, bot_tag)` are deleted**, so
  re-ingesting a document cleanly replaces its chunks with no orphans.
- Connector edits handle the content-changed case: a changed file produces a new
  `document_id`, and the connector calls `delete_by_source_path` before
  re-upload so the old chunks don't orphan.

**To reindex a tenant or document:** re-run the connector sync for its source
(§3), or re-`POST` the PDF to `/upload_pipeline/upload`. To force a full,
clean rebuild, delete the tenant scope (above) and then re-ingest from source.

> A schema-changing reindex (e.g., the pending page-level citations work that
> derives `page_number` during chunking) requires a **full reindex window** —
> see [`RESUME.md`](RESUME.md) ("Page-citation reindex window") and
> [`ARCHITECTURE.md`](ARCHITECTURE.md) for the decision context.

---

## 5. Health & liveness

Each service exposes an unauthenticated `GET /health` (auth middleware bypasses
`/health`, CORS preflight, and Swagger assets):

```bash
curl -sS "https://<ingestion-fqdn>/upload_pipeline/health"   # {"status":"healthy"}
curl -sS "https://<qna-fqdn>/qna/health"                      # {"status":"healthy"}
```

These are the same probes `scripts/validate_deployment.sh` runs. Run the full
post-deploy validation after any deploy or config change:

```bash
scripts/validate_deployment.sh \
  --resource-group "$RG" \
  --ingestion-app tocdoc-ingestion-prod \
  --qna-app tocdoc-qna-prod \
  --environment prod
# Add --skip-health-checks when on a network without egress to the app FQDNs,
# or for a known cold-start window. Exit codes: 0 pass, 1 a required check
# failed, 2 usage/preflight error (az missing or not logged in).
```

The script is read-only (never calls `az ... create/update/set`) and never
prints secret values — it validates resource existence, Container App revision
state, env-var **names**, Key Vault wiring, the Search service, and the `/health`
probes. A `[FAIL]` line includes a `Remedy:` hint.

On a "health is failing" page:

1. `validate_deployment.sh` (or `--skip-health-checks` if egress is the issue) to
   localize the failure.
2. Check revision state — a `Failed`/`Stopped`/`Degraded` running state is a
   real problem; `Idle`/`Provisioned` with `minReplicas: 0` is just scaled-to-zero:
   ```bash
   az containerapp revision list --name tocdoc-qna-prod -g "$RG" \
     --query "[?properties.active] | [0].properties.runningState" -o tsv
   ```
3. Tail logs (§1). Startup failures often mean missing env vars or Key Vault
   access — both are exactly what the validation script flags.

---

## 6. Incident triage via `request_id`

Every response carries `X-Request-ID` in **both the header and the error-envelope
body** (`{ "error": { "code", "message", "request_id", "errors?" } }`). This is
the backbone of triage.

1. **Get the ID from the reporter.** From a failing client call, read the
   `X-Request-ID` response header, or `error.request_id` from the JSON body. If a
   client supplied its own `X-Request-ID` (safe charset: `[A-Za-z0-9_-]`, ≤128
   chars), it is propagated; otherwise the middleware mints a UUID4.
2. **Pull the full trail from Log Analytics** using the §1 KQL `request_id`
   query. Because every stage event in both pipelines carries the same
   `request_id`, you get the end-to-end story: `request_started` →
   (`query_rephrased` / ingestion stage events) → `retrieval_completed` /
   `index_upsert_completed` → `answer_generated` → `request_completed` /
   `request_failed`.
3. **For a 500 with no useful body:** `request_failed` carries `error_class` and
   the generic `safe_message` — raw exception text is never in the response or
   the structured event. The full stack trace **is** in the server logs (logged
   via `logger.exception`), so look for the standard exception log line at the
   same `request_id`.
4. **For a connector failure:** triage by `run_id` (§3) rather than the trigger
   request's `X-Request-ID`.

> Known gap (documented in `observability.py`): a 500 generated by Starlette's
> `ServerErrorMiddleware` for an *unhandled* exception may not carry
> `X-Request-ID` on the response header — but `request_failed` is still logged
> with the `request_id`, so log-side triage is unaffected. The common
> `HTTPException` 4xx/5xx path does carry the header.

---

## 7. Backup & disaster recovery

There is **no native index-snapshot/backup feature in this codebase** — do not
assume one. The DR posture follows directly from the stateless design:

**What is durable (the only things to protect):**

- **Azure AI Search index** — the materialized knowledge base. The
  authoritative copy of the *content* is the upstream source (Blob / SharePoint),
  not the index.
- **Azure Key Vault secrets** — provisioned with `softDeleteRetentionInDays: 90`
  (`infra/main.bicep`), so deleted secrets are recoverable within the
  soft-delete window.
- **Upstream document sources** — Blob Storage / SharePoint. These are the system
  of record for ingested content and are backed up by the client's own data
  protection for those stores (outside TocDoc's scope).

**What is disposable:** the two Container Apps themselves. They hold no durable
state, so recovery is redeploy-and-rebuild:

1. **Redeploy the services** from `infra/main.bicep` plus the container images
   (the Bicep deploys placeholder images; the operator's real images are set via
   `az containerapp update --image ...`). See `scripts/validate_deployment.sh`
   for the post-deploy gate.
2. **Restore secrets** — from Key Vault (recover soft-deleted secrets if needed),
   or re-populate from the client's secret source.
3. **Rebuild the index** — re-ingest from the upstream sources via connector
   syncs (§3). This is safe and idempotent: the AI Search index is **created
   lazily on first ingestion if absent** (noted in `validate_deployment.sh`), and
   deterministic IDs + stale-chunk cleanup mean a full re-ingest reproduces the
   index without duplicates.

**RPO/RTO framing:** because the index is reconstructible from sources, the
effective RPO is bounded by the upstream sources' own backup policy, and RTO is
"redeploy Bicep + re-ingest" — proportional to corpus size, not to any TocDoc
internal state. Treat regular connector syncs as both a freshness mechanism and a
DR rehearsal. DR expectations and any contractual SLAs are a per-tier concern —
see [`PRODUCT_TIERS.md`](PRODUCT_TIERS.md).

---

## Appendix — quick command reference

```bash
# Resolve FQDNs
az deployment group show -g "$RG" --name main --query 'properties.outputs.ingestionAppFqdn.value' -o tsv
az deployment group show -g "$RG" --name main --query 'properties.outputs.qnaAppFqdn.value' -o tsv

# Health
curl -sS "https://<ingestion-fqdn>/upload_pipeline/health"
curl -sS "https://<qna-fqdn>/qna/health"

# Validate a deployment
scripts/validate_deployment.sh --resource-group "$RG" --ingestion-app <name> --qna-app <name>

# Logs
az containerapp logs show --name <app> -g "$RG" --container <name> --follow

# Scale
az containerapp update --name <app> -g "$RG" --min-replicas 1 --max-replicas 5

# Connector sync + status (admin token required)
curl -sS -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" "https://<ingestion-fqdn>/upload_pipeline/admin/connectors/<blob|sharepoint>/sync"
curl -sS      -H "X-Admin-Token: $ADMIN_API_TOKEN" "https://<ingestion-fqdn>/upload_pipeline/admin/connectors/runs/<run_id>"

# Tenant data (admin token required)
curl -sS      -H "X-Admin-Token: $ADMIN_API_TOKEN" "https://<ingestion-fqdn>/upload_pipeline/admin/index/stats?bot_tag=<bot_tag>"
curl -sS -X DELETE -H "X-Admin-Token: $ADMIN_API_TOKEN" "https://<ingestion-fqdn>/upload_pipeline/admin/bots/<bot_tag>/documents?confirm=true"
```
