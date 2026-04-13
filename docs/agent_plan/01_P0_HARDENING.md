# Phase P0 — Security, Correctness, and Production Hardening

> **For sub-agents:** Complete all 8 tasks in this document before starting any P1 work.
> Each task below maps 1:1 to a backlog file in `../productization_backlog/`.
> Exact file paths, line numbers, and implementation notes are provided.

---

## P0-1 | JWT RS256 Signature Validation
**Backlog:** `01_SECURITY_Enable_strict_Azure_AD_JWT_validation.md`
**Status:** `TODO`

### The problem (exact location)
`services/qna/src/core/auth.py`, lines 71–78:
```python
decoded = jwt.decode(
    token,
    key="",                              # ← no key
    options={"verify_signature": False}, # ← disabled
    ...
)
```
The token's cryptographic signature is never verified. Any well-formed JWT passes.

### What to build
Create a new module `services/qna/src/core/token_validator.py` with:
1. An async JWKS fetcher that pulls Azure AD signing keys from:
   `https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys`
2. In-memory key cache with a TTL of 3600 seconds (handles key rotation).
3. A `validate_token(token: str) -> dict` function that:
   - Fetches the correct `kid` from the token header
   - Validates RS256 signature using the matching public key
   - Validates `iss`, `aud`, `exp`, `nbf`
   - Returns the decoded claims dict on success
   - Raises `AuthenticationError` (custom exception) on any failure
4. Update `auth.py` middleware to call `validate_token()` instead of the current inline decode.

### New env vars required
```
AZURE_TENANT_ID=<tenant-id>   # already partially present; make it canonical
```

### Acceptance criteria
- `test_auth_valid_token` — passes with a correctly signed mock token
- `test_auth_expired_token` — rejected with 401
- `test_auth_wrong_audience` — rejected with 401
- `test_auth_wrong_issuer` — rejected with 401
- `test_auth_invalid_signature` — rejected with 401
- `test_auth_missing_email_claim` — rejected with 401
- Auth failure logs do not include raw token contents

---

## P0-2 | bot_tag Tenant Scoping in Retrieval
**Backlog:** `02_ISOLATION_Enforce_bot_tag_tenant_scoping_in_retrieval.md`
**Status:** `TODO`

### The problem (exact location)
`services/qna/src/services/search_service.py`, line 85:
```python
filter_expr = f"fr_tag eq '{fr_mode}'"  # ← bot_tag never applied
```
`services/qna/src/pipeline/qna_pipeline.py`, line 32:
```python
async def generate_answer(query: str, fr_mode: str, azure) -> Dict[str, Any]:
    # ← bot_tag not even in the function signature
```

### What to build
1. **`search_service.py`** — change `_search_sync` filter to:
   ```python
   filter_expr = f"fr_tag eq '{fr_mode}' and bot_tag eq '{bot_tag}'"
   ```
   Add `bot_tag: str` parameter to both `perform_search()` and `_search_sync()`.

2. **`qna_pipeline.py`** — add `bot_tag: str` to `generate_answer()` signature.
   Pass it through to `perform_search()`.

3. **`services/qna/app.py`** — extract `bot_tag` from `request.state` (set by auth
   middleware from the JWT claim) or from the request body. Pass to `generate_answer()`.
   Reject requests where `bot_tag` is empty or missing with HTTP 400.

4. **Auth middleware** — extract `bot_tag` (or `tid` / a custom claim) from the
   decoded JWT and attach to `request.state.bot_tag`.

### Acceptance criteria
- A search with `bot_tag='tenant-a'` returns only documents indexed under `tenant-a`
- A search with a mismatched `bot_tag` returns zero results (not another tenant's docs)
- Missing `bot_tag` on a request returns HTTP 400, not a silent full-corpus search
- Both `fr_read` and `fr_layout` modes respect the isolation filter

---

## P0-3 | Remove Global Request State from QnA Pipeline
**Backlog:** `03_CONCURRENCY_Remove_global_request_state_from_qna_pipeline.md`
**Status:** `TODO`

### The problem (exact location)
`services/qna/src/pipeline/qna_pipeline.py`, line 29:
```python
bot_queries: Optional[List[Dict[str, Optional[str]]]] = None  # ← module-level mutable global
```
`services/qna/app.py` sets this global before calling `generate_answer()`.
Two concurrent requests will overwrite each other's conversation history.

### What to build
1. **`qna_pipeline.py`** — remove the module-level `bot_queries` global entirely.
   Change `generate_answer()` signature to:
   ```python
   async def generate_answer(
       query: str,
       fr_mode: str,
       bot_tag: str,
       history: List[Dict[str, Optional[str]]],
       azure,
   ) -> Dict[str, Any]:
   ```
   Replace all internal references to `bot_queries` with the `history` parameter.

2. **`services/qna/app.py`** — stop setting the global. Build the `history` list
   from the request body and pass it directly into `generate_answer()`.

3. Audit all other modules for similar module-level mutable state:
   - `search_service.py` — `localconfig = LocalConfig()` is fine (read-only config)
   - `embedding_service.py` — check for any mutable globals
   - `openai_service.py` — check for any mutable globals

### Acceptance criteria
- Two concurrent requests with different conversation histories return independent answers
- Follow-up rephrasing still works after refactor (history is passed, not lost)
- No module-level variable holds per-request data anywhere in the QnA service
- Existing API contract (`query`, `history`, `fr_mode` fields) preserved

---

## P0-4 | Deterministic Chunk IDs and Document Lifecycle
**Backlog:** `04_INGESTION_Add_deterministic_chunk_ids_and_reindex_delete_lifecycle.md`
**Status:** `TODO`

### The problem (exact location)
`services/ingestion/custom_rag.py` — chunks are assigned random UUIDs via `str(uuid.uuid4())`.
Re-uploading the same file creates duplicate indexed records because IDs never collide.

### What to build
1. **Document identity**: derive a stable `document_id` from `sha256(file_bytes)[:16]`
   or from `f"{bot_tag}:{source_path}"` (canonical path-based ID).

2. **Chunk identity**: derive `chunk_id` as:
   ```python
   chunk_id = f"{document_id}:{mode}:{chunk_index:05d}"
   ```

3. **Upsert semantics**: when indexing, use Azure Search's merge-or-upload action
   (`IndexAction.merge_or_upload`) instead of plain upload. This means re-ingesting
   the same file replaces existing chunks in-place.

4. **Delete-by-document**: add a helper `delete_document_chunks(document_id: str)`
   that calls `search_client.delete_documents()` for all chunks matching the document_id.
   This becomes the foundation for the admin API in P1-2.

5. **Metadata fields to add to every indexed chunk**:
   - `document_id: str`
   - `content_hash: str` (sha256 of chunk text, for change detection)
   - `ingestion_timestamp: str` (ISO 8601)
   - `source_type: str` ("upload" | "blob" | "sharepoint")
   - `source_path: str`

### Acceptance criteria
- Re-ingesting the same file produces the same chunk IDs (deterministic)
- Re-ingestion updates existing records, not duplicate them
- Azure Search document count does not grow on repeated uploads of the same file
- `delete_document_chunks()` removes exactly the chunks for that document

---

## P0-5 | True Token-Aware Chunking
**Backlog:** `05_RETRIEVAL_Implement_true_token_aware_chunking_and_eval.md`
**Status:** `TODO`

### The problem (exact location)
`services/ingestion/custom_rag.py` — the read-mode chunker splits on whitespace words,
not real model tokens. A "500-token chunk" is actually 500 words, which for complex
documents can be significantly more or fewer than 500 real tokens.

### What to build
1. Add `tiktoken` to `services/ingestion/requirements.txt`.
2. Create a tokenizer instance aligned to the embedding model:
   ```python
   import tiktoken
   # text-embedding-3-small uses cl100k_base encoding
   tokenizer = tiktoken.get_encoding("cl100k_base")
   ```
3. Replace the word-split chunker with a real token-counting loop:
   - Encode the full text to tokens
   - Slice windows of `TOKEN_SIZE` tokens with `OVERLAP` token overlap
   - Decode each window back to string for indexing
4. Add metadata field `token_count: int` to each indexed chunk.
5. Add a simple test fixture: ingest a known document and assert every chunk
   has `token_count <= TOKEN_SIZE`.

### Acceptance criteria
- Chunks are bounded by actual tiktoken counts, not word counts
- Overlap is token-accurate
- No chunk exceeds the configured token limit
- Test proves token count accuracy

---

## P0-6 | API Contract Hardening
**Backlog:** `06_API_Harden_error_contracts_request_validation_and_response_schema.md`
**Status:** `TODO`

### The problem
Both services return ad-hoc dicts from error paths. The QnA pipeline returns
`{"answer": "An error occurred...", "error": str(e), "request_id": ...}` from
the except block — mixing error state into a "success-shaped" response.

### What to build

**QnA service (`services/qna/`):**
```python
# New Pydantic models in a new file: src/models/responses.py
class CitationMap(BaseModel):
    filename: str
    filepath: str

class QnASuccessResponse(BaseModel):
    answer: str
    citations: list[CitationMap]
    request_id: str

class ErrorResponse(BaseModel):
    error: str
    code: str
    request_id: str
```

Use `response_model=QnASuccessResponse` on the answer endpoint.
Return `JSONResponse(status_code=4xx/5xx, content=ErrorResponse(...).dict())` on errors.
Never return `error` inside a 200 response body.

**Ingestion service (`services/ingestion/`):**
```python
class IngestionSuccessResponse(BaseModel):
    message: str
    document_id: str
    chunks_indexed: int

class ErrorResponse(BaseModel):
    error: str
    code: str
```

**HTTP status code mapping** (both services):
- Missing/invalid request fields → 422 (FastAPI auto-handles Pydantic)
- Auth failure → 401
- Permission/scoping failure → 403
- Upstream Azure failure (Search, OpenAI) → 502
- Internal processing error → 500
- Never return raw exception text in `detail` for 5xx responses

### Acceptance criteria
- Success responses follow documented Pydantic schemas
- Error responses are never 200 with embedded error fields
- 4xx vs 5xx boundary is intentional and consistent
- OpenAPI docs (`/docs`) reflect actual response shapes

---

## P0-7 | Config Normalization
**Backlog:** `07_CONFIG_Normalize_env_secret_bootstrap_and_deployment_profiles.md`
**Status:** `TODO`

### The problem
QnA uses PascalCase env vars (`AzureOpenaiAccountEndpoint`, `TocdocOpenAIKey`)
while ingestion uses UPPER_SNAKE_CASE (`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`).
The inconsistency makes cross-service deployment error-prone.

### Canonical naming (UPPER_SNAKE_CASE throughout)
| Old QnA name | Canonical name |
|---|---|
| `AzureOpenaiAccountEndpoint` | `AZURE_OPENAI_ENDPOINT` |
| `TocdocOpenAIKey` | `AZURE_OPENAI_KEY` |
| `AzureOpenaiApiVersion` | `AZURE_OPENAI_API_VERSION` |
| `AzureOpenaiLlmModel` | `AZURE_OPENAI_LLM_MODEL` |
| `AzureSearchEndpoint` | `AZURE_SEARCH_ENDPOINT` |
| `AzureSearchKey` | `AZURE_SEARCH_KEY` |
| `TocdocSPClientID` | `AZURE_SP_CLIENT_ID` |
| `TocdocSPSecretValue` | `AZURE_SP_CLIENT_SECRET` |
| `TocdocSPTenantID` | `AZURE_TENANT_ID` |
| `AZURE_KEY_VAULT` | `AZURE_KEY_VAULT_NAME` |

### Files to update
1. `services/qna/src/config/config.py` — update `Settings` field names
2. `services/qna/src/clients/azure_clients.py` — update any direct `os.environ` reads
3. `services/qna/.env.example` — use canonical names
4. Root `.env.example` — align QnA section
5. `services/qna/src/core/lifecycle.py` — check Key Vault loading
6. Both `Dockerfile`s — no hardcoded env var names, but verify comments

### Acceptance criteria
- Both services use identical env var names for shared Azure resources
- Old PascalCase names are gone (no backward-compat aliases needed — this is pre-v1)
- Both `.env.example` files are self-consistent with the code
- A fresh deployment using only the `.env.example` template works without guessing

---

## P0-8 | Runtime Hardening
**Backlog:** `08_RUNTIME_Harden_containers_cors_logging_and_cloud_native_defaults.md`
**Status:** `TODO`

### The problem areas

**CORS**: both services likely have `allow_origins=["*"]` or overly permissive defaults.
Make CORS configurable via `CORS_ALLOWED_ORIGINS` env var, defaulting to `[]` (deny all)
in production.

**Logging**: currently logs to file in some paths. Containers should log to stdout only.
Structured JSON logging is preferred for Azure Monitor ingestion.

**Uvicorn workers**: the `CMD ["--workers", "2"]` in Dockerfiles is a good start.
Make worker count configurable via `UVICORN_WORKERS` env var.

**Dockerfile hardening**:
- Run as non-root user (add `RUN useradd -m appuser && USER appuser`)
- Pin base image to a specific digest or minor version (`python:3.10.14-slim`)
- Add `.dockerignore` files to exclude `.env`, `test/`, `*.md`, `__pycache__`

**Health endpoints**: ensure they are truly lightweight (no DB ping, no Azure call).
Current `/health` endpoints should return `{"status": "ok"}` only.

### Files to update
- `services/ingestion/Dockerfile`
- `services/qna/Dockerfile`
- `services/ingestion/app.py` — CORS config
- `services/qna/app.py` — CORS config
- `services/qna/src/core/logger.py` — JSON structured output
- New: `services/ingestion/.dockerignore`
- New: `services/qna/.dockerignore`

### Acceptance criteria
- Production images run as non-root
- CORS is configurable and defaults to restrictive
- Logs are JSON-structured on stdout in production mode
- Local dev can still use `allow_origins=["*"]` via explicit env var override
- Health endpoints never call external Azure resources
