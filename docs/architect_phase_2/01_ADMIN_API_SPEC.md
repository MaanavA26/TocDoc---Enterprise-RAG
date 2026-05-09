# Phase 2 Workstream A — Admin API Specification

## Objective

Add a secure admin/control-plane API so operators can inspect and manage indexed documents and tenant/bot scoped data without manually querying Azure AI Search.

This is the highest-priority Phase 2 workstream because TocDoc is currently deployable but not easily operable.

## Backlog mapping

- `docs/productization_backlog/10_PRODUCT_Add_admin_APIs_for_index_management_and_tenant_operations.md`
- `docs/productization_backlog/04_INGESTION_Add_deterministic_chunk_ids_and_reindex_delete_lifecycle.md`
- `docs/productization_backlog/02_ISOLATION_Enforce_bot_tag_tenant_scoping_in_retrieval.md`

## Current architectural assumption

The Azure Search index already contains or should contain these fields:
- `id`
- `bot_tag`
- `fr_tag`
- `document_id`
- `ingestion_timestamp`
- `source_type`
- `source_path`
- content/chunk fields already used by QnA

Admin APIs must use `bot_tag` filtering for every read/write operation unless an explicitly privileged cross-tenant operation is implemented later.

## API location

Preferred location:
- Add admin endpoints to the ingestion service first, because ingestion owns indexing lifecycle.

Suggested route prefix:
- `/admin`

Do not expose these endpoints without authentication. If full admin RBAC is not implemented in this PR, use the same auth mechanism already used by the service or add a clearly documented temporary admin token guard through environment configuration.

## Required endpoints — PR 1 read-only scope

### 1. List indexed documents

`GET /admin/documents?bot_tag={bot_tag}`

Purpose:
Return one row per indexed document, not one row per chunk.

Required behavior:
- `bot_tag` is required.
- Filter Azure Search by `bot_tag`.
- Group chunks by `document_id`.
- Return document-level metadata.

Response shape:

```json
{
  "bot_tag": "client_a_hr",
  "count": 2,
  "documents": [
    {
      "document_id": "abc123",
      "source_path": "handbook.pdf",
      "source_type": "upload",
      "fr_tag": "layout",
      "chunk_count": 24,
      "first_ingested_at": "2026-05-09T09:00:00Z",
      "last_ingested_at": "2026-05-09T09:00:00Z"
    }
  ]
}
```

### 2. Get one document summary

`GET /admin/documents/{document_id}?bot_tag={bot_tag}`

Purpose:
Return metadata for a single document and optionally a compact chunk summary.

Required behavior:
- `bot_tag` is required.
- `document_id` is required.
- Must filter using both `bot_tag` and `document_id`.
- Must return 404 if no chunks exist for that document in that bot scope.

Response shape:

```json
{
  "bot_tag": "client_a_hr",
  "document_id": "abc123",
  "source_path": "handbook.pdf",
  "source_type": "upload",
  "fr_tag": "layout",
  "chunk_count": 24,
  "ingestion_timestamps": ["2026-05-09T09:00:00Z"],
  "sample_chunks": [
    {
      "id": "client_a_hr_abc123_layout_00000",
      "chunk_index": 0
    }
  ]
}
```

### 3. Index stats

`GET /admin/index/stats?bot_tag={bot_tag}`

Purpose:
Return high-level operational stats.

Required behavior:
- For now, require `bot_tag`.
- Return document count, chunk count, source types, and modes.

Response shape:

```json
{
  "bot_tag": "client_a_hr",
  "document_count": 12,
  "chunk_count": 540,
  "source_types": {
    "upload": 12
  },
  "fr_modes": {
    "layout": 8,
    "read": 4
  }
}
```

## Required endpoints — PR 2 write/destructive scope

### 4. Delete one document

`DELETE /admin/documents/{document_id}?bot_tag={bot_tag}`

Purpose:
Delete all chunks for one document in one bot/tenant scope.

Required behavior:
- Must filter by both `bot_tag` and `document_id`.
- Must not delete across tenants.
- Must paginate through all matching chunks, not only first 1000 results.
- Return number of deleted chunks.
- Return idempotent success if document does not exist, but include `deleted_chunks: 0`.

Response shape:

```json
{
  "bot_tag": "client_a_hr",
  "document_id": "abc123",
  "deleted_chunks": 24,
  "status": "deleted"
}
```

### 5. Delete all documents for a bot/tenant

`DELETE /admin/bots/{bot_tag}/documents`

Purpose:
Clear all indexed chunks for one bot/tenant.

Required behavior:
- Must require `confirm=true` query parameter or equivalent explicit confirmation.
- Must paginate through all chunks.
- Must not affect other `bot_tag` values.

Response shape:

```json
{
  "bot_tag": "client_a_hr",
  "deleted_chunks": 540,
  "deleted_documents": 12,
  "status": "deleted"
}
```

### 6. Reindex document

`POST /admin/documents/{document_id}/reindex`

Recommendation:
Do not implement full reindex until source connectors or source persistence are defined. If source files are not persisted, this endpoint should return `501 Not Implemented` with a clear message.

Acceptable Phase 2 behavior:

```json
{
  "status": "not_implemented",
  "reason": "Reindex requires source persistence or connector metadata. Use delete + ingest for now."
}
```

## Security requirements

- Admin endpoints must not be public.
- Every endpoint must enforce `bot_tag` scoping.
- Do not allow raw OData filter injection from user input.
- Escape single quotes in OData values or validate `bot_tag` and `document_id` with a strict regex.
- Recommended regex:
  - `bot_tag`: `^[A-Za-z0-9_-]{1,128}$`
  - `document_id`: `^[A-Za-z0-9_.:-]{1,256}$`

## Implementation guidance

Create a small service layer rather than putting all Azure Search logic in route handlers.

Suggested structure:

```text
services/ingestion/
  admin/
    __init__.py
    routes.py
    models.py
    search_admin_service.py
```

Route handlers should only:
- validate request
- call service methods
- map service errors to HTTP errors

Service should own:
- Azure Search queries
- pagination
- grouping chunks by document
- delete batching

## Testing requirements

Add unit tests for:
- missing `bot_tag` returns 422 or 400
- invalid `bot_tag` rejected
- list documents groups chunks correctly
- get document filters by both `bot_tag` and `document_id`
- delete document deletes only matching bot/document chunks
- delete tenant requires explicit confirmation
- pagination does not stop at first page
- OData escaping or validation prevents filter injection

## Acceptance criteria

This workstream is accepted when:
- read-only admin APIs are usable and documented
- destructive APIs are safe, scoped, and tested
- no endpoint can operate without tenant/bot scope
- Azure Search deletion handles more than 1000 chunks
- API responses are stable and documented
- README or deployment docs mention admin route availability and auth expectations

## Non-goals

- Full admin UI
- Cross-tenant super-admin operations
- Full reindex from original source unless source persistence exists
- Replacing `bot_tag` naming in the same PR

## Architect note

This is the most important next implementation area. Without these APIs, TocDoc remains a deployable RAG backend, not an operable product.

Co-Authored by Maanav's Mac-Air
