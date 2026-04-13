# Phase P1 — Enterprise Feature Completeness

> **Prerequisite:** All 8 P0 items must be `DONE` before starting P1.
> Exception: P1-4 (IaC/CI) and P1-5 (Quality) can be developed in parallel
> branches during P0, as they are additive rather than dependent on P0 code changes.

---

## P1-1 | Observability: Telemetry, Audit Logs, Operational Metrics
**Backlog:** `09_OBSERVABILITY_Add_telemetry_audit_logs_and_operational_metrics.md`
**Status:** `BLOCKED on P0`

### What to build

**New file: `services/qna/src/core/telemetry.py`**

A thin wrapper over `azure-monitor-opentelemetry` (or Python's `logging` with structured
fields if OTLP is not in scope yet):

```python
class RequestTelemetry:
    def __init__(self, request_id: str, user: str, tenant: str):
        self.request_id = request_id
        self.user = user
        self.tenant = tenant
        self.start_time = time.time()
        self.stages: dict = {}

    def record_stage(self, name: str, duration_ms: float, metadata: dict = None): ...
    def record_error(self, stage: str, error: str): ...
    def emit(self): ...  # sends structured JSON to stdout / Azure Monitor
```

**Instrumentation points (QnA pipeline):**
- Request received (user, tenant, query hash — NOT raw query)
- Embedding generation (latency_ms)
- Search (latency_ms, result_count, fr_mode, bot_tag)
- LLM generation (latency_ms, prompt_tokens, completion_tokens)
- Total request latency

**Audit log events (separate from request telemetry):**
```json
{
  "event_type": "qna_query | doc_ingested | doc_deleted | auth_failure",
  "timestamp": "ISO8601",
  "request_id": "...",
  "user": "jane@corp.com",
  "tenant": "acme",
  "outcome": "success | failure",
  "metadata": {}
}
```

**New env vars:**
```
APPLICATIONINSIGHTS_CONNECTION_STRING=<optional, for Azure Monitor>
AUDIT_LOG_LEVEL=INFO   # controls audit event verbosity
```

### Acceptance criteria
- Every QnA request produces a structured telemetry event
- Auth failures produce an audit log entry (no token content in logs)
- Ingestion events (document indexed, document deleted) are auditable
- Correlation ID threads through all log lines for a single request
- README documents how to wire logs to Azure Log Analytics

---

## P1-2 | Admin APIs for Index and Tenant Management
**Backlog:** `10_PRODUCT_Add_admin_APIs_for_index_management_and_tenant_operations.md`
**Status:** `BLOCKED on P0-4 (deterministic chunk IDs)`

### What to build

**New router: `services/ingestion/src/admin/router.py`**
Mount at `/admin/` with its own auth scope check (`require_admin_scope` dependency).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/documents` | GET | List all indexed documents for a `bot_tag` |
| `/admin/documents/{document_id}` | GET | Stats for one document (chunk count, timestamps) |
| `/admin/documents/{document_id}` | DELETE | Remove all chunks for a document |
| `/admin/tenants/{bot_tag}` | DELETE | Remove all documents for a tenant/bot |
| `/admin/tenants/{bot_tag}/reindex` | POST | Trigger re-ingestion from last known source |
| `/admin/index/stats` | GET | Total doc count, chunk count, storage used |

**Authorization model:**
- Require a JWT claim `roles: ["TocDoc.Admin"]` (Azure AD App Role)
- Non-admin tokens get HTTP 403 even if their `bot_tag` matches
- Audit all admin operations (delete, reindex) via the telemetry system from P1-1

**Implementation note:**
The admin endpoints rely on `document_id` and `bot_tag` metadata fields
added in P0-4. Do not implement this before P0-4 is done.

### Acceptance criteria
- Admin can list, inspect, and delete documents by ID
- Admin can wipe an entire tenant's corpus
- Non-admin tokens receive 403
- All destructive operations emit audit log events
- OpenAPI docs describe the admin router with auth requirements

---

## P1-3 | Connectors: Blob Storage and SharePoint Ingestion
**Backlog:** `11_CONNECTORS_Add_connector_based_ingestion_for_blob_sharepoint_upload.md`
**Status:** `BLOCKED on P0-4 (deterministic IDs for source tracking)`

### What to build

**New package: `services/ingestion/src/connectors/`**

```
connectors/
├── __init__.py
├── base.py           # Abstract connector interface
├── blob_connector.py # Azure Blob Storage
└── sharepoint_connector.py  # SharePoint / Graph API
```

**`base.py`** — Abstract interface:
```python
class ConnectorBase(ABC):
    source_type: str

    @abstractmethod
    async def list_documents(self, bot_tag: str) -> list[SourceDocument]: ...

    @abstractmethod
    async def download(self, doc: SourceDocument) -> bytes: ...
```

**`blob_connector.py`**:
- Authenticates via `DefaultAzureCredential` or connection string
- Lists blobs in a configured container filtered by prefix (`bot_tag/`)
- Downloads blob bytes for ingestion
- Source path becomes `blob://{container}/{blob_name}`
- New env vars: `BLOB_STORAGE_CONNECTION_STRING`, `BLOB_CONTAINER_NAME`

**`sharepoint_connector.py`**:
- Authenticates via Microsoft Graph API (`ClientSecretCredential`)
- Lists files in a configured SharePoint document library
- Downloads files via Graph drive item download URL
- New env vars: `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID`

**New ingestion trigger endpoint:**
`POST /upload_pipeline/ingest-from-connector`
Body: `{ "connector_type": "blob" | "sharepoint", "bot_tag": "...", "fr_mode": "read" | "layout" }`

### Acceptance criteria
- Documents can be ingested from Azure Blob without a local file path
- SharePoint connector downloads files and passes them through the existing pipeline
- Source metadata (`source_type`, `source_path`) is stored on every chunk
- Adding a new connector requires only implementing `ConnectorBase`

---

## P1-4 | Platform: IaC, CI/CD, and Deployment Assets
**Backlog:** `12_PLATFORM_Add_IaC_CI_CD_and_repeatable_client_installation_assets.md`
**Status:** `CAN START IN PARALLEL with P0`

### What to build

**Infrastructure-as-Code (`infra/` folder):**
Use Azure Bicep (preferred for Azure-native clients over Terraform):

```
infra/
├── main.bicep              # Orchestration template
├── modules/
│   ├── openai.bicep        # Azure OpenAI account + deployments
│   ├── search.bicep        # Azure Cognitive Search (S1 tier)
│   ├── doc_intelligence.bicep
│   ├── key_vault.bicep
│   ├── container_apps.bicep  # Azure Container Apps environment + apps
│   └── monitoring.bicep    # Log Analytics + App Insights
└── parameters/
    ├── dev.bicepparam
    └── prod.bicepparam
```

**GitHub Actions CI/CD (`.github/workflows/`):**
```
.github/workflows/
├── ci.yml          # On PR: lint, test, build images
├── cd-staging.yml  # On merge to main: push to staging
└── cd-prod.yml     # On tag v*: push to production
```

`ci.yml` steps:
1. Python lint (`ruff` or `flake8`)
2. Type check (`mypy`)
3. Unit tests (`pytest services/qna/test/ services/ingestion/test/`)
4. Docker build (validate Dockerfiles compile)

**Deployment runbook (`docs/deployment/`):**
```
docs/deployment/
├── INSTALLATION.md    # Step-by-step for a new client Azure environment
├── UPGRADE.md         # How to update an existing deployment
└── ARCHITECTURE.md    # Reference diagram (ACA topology)
```

### Recommended hosting target (v1): Azure Container Apps
Simpler than AKS, no Kubernetes expertise required, scales to zero.
Each TocDoc deployment = one Container Apps Environment + two apps (ingestion, qna).

### Acceptance criteria
- `az deployment group create -f infra/main.bicep -p parameters/prod.bicepparam`
  provisions a complete working environment
- CI runs on every PR and blocks merge on test failures
- CD deploys to staging on main-branch merge
- Installation runbook can be followed by a technical resource with no tribal knowledge

---

## P1-5 | Quality: Test Strategy, Coverage, and Release Gates
**Backlog:** `13_QUALITY_Expand_test_strategy_coverage_and_release_gates.md`
**Status:** `CAN START IN PARALLEL with P0`

### Current test gap analysis
- QnA service has `test/test.py` but coverage is limited (mostly happy-path)
- Ingestion service has NO tests
- No concurrency tests
- No auth failure tests
- No negative retrieval tests (wrong bot_tag returns empty, not other tenant's data)

### Test layers to build

**Layer 1 — Unit tests (fast, no Azure dependencies):**
```
services/qna/test/
├── test_auth.py              # P0-1: all JWT validation cases
├── test_isolation.py         # P0-2: bot_tag filter behavior (mock search client)
├── test_concurrency.py       # P0-3: concurrent requests don't share history
├── test_pipeline.py          # generate_answer() with mocked services
├── test_text_processor.py    # citation extraction, filename normalization
└── test_config.py            # config loads correctly, missing vars raise clearly

services/ingestion/test/
├── test_chunking.py          # P0-5: token-aware chunking boundaries
├── test_chunk_ids.py         # P0-4: deterministic ID generation
└── test_ingestion_lifecycle.py  # upsert, re-ingest, delete behavior
```

**Layer 2 — Contract tests (mock Azure SDK, test API shapes):**
```
services/qna/test/
└── test_api_contracts.py     # P0-6: response schemas, error status codes
```

**Layer 3 — Integration tests (flagged, require real Azure creds, skipped in CI):**
```
services/qna/test/integration/
└── test_e2e_query.py         # live search + live LLM (skipped unless INTEGRATION=1)
```

### CI quality gates (in `ci.yml`):
- All Layer 1 tests must pass
- Coverage must not drop below 70% for `src/core/`, `src/services/`
- No new bare `except Exception` without a re-raise or explicit log

### Acceptance criteria
- Auth failure test cases exist for all P0-1 negative paths
- Isolation test proves bot_tag filter works (mock search client)
- Concurrency test proves no shared state between simultaneous requests
- Chunking tests prove token count boundaries
- CI enforces all of the above on every PR
