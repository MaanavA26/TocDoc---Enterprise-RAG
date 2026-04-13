# 04 — Ingestion: Add deterministic chunk IDs and full reindex/delete lifecycle

**Priority:** P0  
**Type:** Ingestion architecture / Data lifecycle / Production blocker

## Problem

The ingestion flow currently generates fresh UUIDs for chunks every time a document is processed. That means the same file can be uploaded repeatedly and produce duplicate indexed records. There is also no first-class lifecycle model for replacing, deleting, or refreshing a document’s indexed representation.

## Why this matters

- Enterprise clients need controlled document lifecycle behavior, not append-only indexing.
- Duplicate chunks degrade retrieval quality and inflate search/storage cost.
- Without deterministic identities, it is hard to support update, delete, and audit operations cleanly.
- A product that cannot safely reindex client content will create operational pain very quickly.

## Desired outcome

TocDoc should treat ingestion as a managed lifecycle. Each indexed chunk should have a stable deterministic identity derived from document identity plus chunk identity. The platform should support create, update/reindex, and delete semantics.

## Scope

- Define a stable document identity model.
- Define a stable chunk identity model.
- Store enough metadata to support cleanup and refresh.
- Add explicit deletion or replacement behavior for re-uploaded files.
- Decide whether deduplication will be content-hash based, path based, or a combination.

## Implementation guidance

- Consider a document fingerprint derived from file bytes or canonical source metadata.
- Consider chunk IDs such as `<document_id>:<mode>:<chunk_number>`.
- Add metadata fields such as `document_id`, `content_hash`, `ingestion_timestamp`, `source_type`, and `source_path` or `source_url`.
- Design an admin-safe way to delete all chunks for a document or tenant.

## Deliverables

- deterministic ID scheme for documents and chunks
- ingestion update logic that avoids duplicates on reprocessing
- delete/reindex utility path or admin API hook
- tests for repeated ingestion of the same file and deletion behavior

## Acceptance criteria

- Re-ingesting the same document does not create duplicate active chunks.
- A document can be refreshed in place.
- A document or tenant dataset can be deleted cleanly from the index.
- Metadata supports auditability and future admin operations.

## Non-goals

- Full workflow orchestration from SharePoint or Logic Apps. That belongs to connector and platform backlog items.

## Notes for Codex / Claude

Do not solve this with ad hoc pre-delete logic only. Establish a durable data lifecycle model that later admin tooling can build on.