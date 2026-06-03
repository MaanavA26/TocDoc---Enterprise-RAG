> **Status:** DRAFT — produced by a multi-agent design council (3 proposals: thin-adapter / pluggable-framework / ops-first; 3 judges). Pending architect review. Not yet implemented.

# P1-3 Connector Ingestion — Architecture Decision Record

**Status:** Proposed (council-approved) · **Supersedes:** none · **Depends on:** P0-4, P0-5, P0-6, P0-7

## Context & constraints

TocDoc ingests documents into an Azure Cognitive Search index exclusively through one
function: `custom_rag.rag.upload(file, tag, fr_mode, file_path)` (`custom_rag.py:294`). That
single write path already enforces every invariant the product depends on:

- **Deterministic content hash** — `document_id = sha256(file_content)[:16]` (`custom_rag.py:306`).
- **Canonical chunk ID (P0-4)** — `id = f"{tag}_{document_id}_{fr_mode}_{i:05d}"` (`custom_rag.py:399`, `:451`).
- **Token-aware chunking (P0-5)** — tiktoken `cl100k_base`, 500-token max / 50-token overlap, applied inside `upload()`.
- **Idempotent re-ingest** — stale chunks for `(document_id, bot_tag)` are deleted before
  `merge_or_upload_documents()` (`custom_rag.py:320-332`, `:475`).
- **Audit stamps** — `ingestion_timestamp` (`:411`), `fr_tag = f"fr_{fr_mode}"` (`:401`).

The index schema (`custom_rag.py:104-292`) is **immutable post-deployment** — fields can only be
added at `create_search_index()` time, and altering it later forces a full reindex. Two fields we
need already exist as free-form filterable strings: `source_type` (`:202`) and `source_path`
(`:207`). The admin layer already reads both verbatim (`search_admin_service.py:38-53`, `:148`,
`:218`) and aggregates arbitrary `source_type` values for stats — **so Blob and SharePoint need
zero schema change.**

The one place the code contradicts the brief: `source_type` is **hardcoded to `"upload"`**
(`custom_rag.py:412`, `:464`), and `file_path` is written through to both `filepath` and
`source_path`. The brief assumes connectors "set `source_type='blob'|'sharepoint'`" — that is not
true in the code yet and is the single required seam change.

**Binding constraints (council-ratified):**

- Connectors **MUST** route through `upload()`; they never hash content, mint chunk IDs, chunk,
  embed, or write to the index directly. P0-4 and P0-5 stay enforced in exactly one place.
- Every chunk **MUST** carry a `bot_tag` matching `^[A-Za-z0-9_-]{1,128}$` (the existing
  `BOT_TAG_PATTERN`, `routes.py:49`). Source→bot_tag binding is **1:1 or N:1, never cross-tag**,
  and is **immutable** once written.
- `source_type ∈ {'blob','sharepoint'}` and `source_path` (`blob://{container}/{blob_name}` or
  `sharepoint://{site}/{drive}/{item_id}`) are immutable audit/reindex anchors.
- Per-source isolated auth; secrets resolved through the **P0-7 env/Key Vault path**, never in a
  request payload, never in `source_path`, never logged.
- Per-file ceiling **100 MB**; request ceiling **300 MB**.
- All errors use the P0-6 `ErrorEnvelope` (`errors.py`); all logs correlate via `X-Request-ID`
  (`observability.py`).
- **v1 is PDF-only.** `upload()` is hard-wired to `fitz.open(filetype="pdf")` (`custom_rag.py:311`)
  and Document Intelligence `prebuilt-{fr_mode}` (`:342`). Non-PDF items are filtered at
  enumeration; expanding the loader is out of scope.

Environment note: per project policy, there is no local runtime — tests are deliverables that
CI/the architect executes. No live Azure calls are made from this design work.

## Decision (the connector abstraction: interface, document model, sync orchestrator)

A connector is **not a subsystem** — it is a thin enumerator + downloader that feeds bytes into the
one existing ingestion path. We add the minimum new surface the current code physically forces, and
nothing more.

### The connector interface

A minimal `SourceConnector` Protocol (`typing.Protocol`/ABC), deliberately mirroring the only
contract `upload()` actually consumes:

```python
class SourceConnector(Protocol):
    source_type: str            # class attribute: "blob" | "sharepoint"
    bot_tag: str                # bound at init, validated against BOT_TAG_PATTERN
    fr_mode: str                # "read" | "layout"

    def enumerate(self) -> Iterator[SourceItem]: ...
        # lazy/streaming; pagination owned internally (continuation tokens / @odata.nextLink).
        # never materializes the full listing in memory. filters to the PDF allowlist here.

    def fetch(self, item: SourceItem) -> ConnectorFile: ...
        # downloads COMPLETE bytes (deterministic hash requires whole content),
        # with timeout + bounded retry, size pre-validated against the 100 MB ceiling.
```

### The document model

`ConnectorFile` is the **single hand-off type** — exactly the duck type `upload()` already accepts:
`.filename: str` plus `async def read() -> bytes`. This is the inline `_MockFile` shape
(`app.py:147-156`) promoted to **one shared class** so Blob and SharePoint do not each reinvent it.
`SourceItem` carries opaque identity plus the canonical `source_path` string and the remote
validator (etag / last-modified / size) used later for change detection.

Critically, neither `ConnectorFile` nor `SourceItem` carries `document_id` or chunk IDs — those are
derived downstream inside `upload()`, so there is no second place to get them wrong.

### The sync orchestrator

A source-agnostic driver loop. `bot_tag` and `fr_mode` are **connector-instance config, not
per-item**, which is how source→bot_tag binding is enforced and cross-tagging is made structurally
impossible:

```python
for item in connector.enumerate():
    cfile = connector.fetch(item)
    async with single_flight(item.source_path, connector.bot_tag):  # orchestrator-owned lock
        await delete_by_source_path(item.source_path, connector.bot_tag)  # edited-file cleanup (see below)
        await rag_instance.upload(
            cfile, connector.bot_tag, connector.fr_mode,
            file_path=item.source_path,
            source_type=connector.source_type,   # NEW param, defaults to "upload"
        )
```

`upload()` then mints the canonical chunk IDs, stamps `bot_tag` / `source_path` / `source_type` /
`ingestion_timestamp` / `fr_tag`, embeds, and merge-or-uploads — all unchanged.

### Execution model — decided explicitly

The driver runs as an **in-process background task launched from an in-stack FastAPI trigger
endpoint**, *not* as a separate out-of-process worker. This is a deliberate choice grafted from the
ops review: a detached worker calling `upload()` directly sits **outside** the FastAPI stack and
silently sheds three P0 guarantees — the 100 MB route guard (`app.py:178`), the P0-6 `ErrorEnvelope`
handlers, and the `X-Request-ID` middleware. By keeping the trigger endpoint in-stack behind
`require_admin_token` and launching the loop as a background task, the endpoint inherits all three.
Two residual obligations remain first-class deliverables regardless: (1) the per-file 100 MB
pre-validation lives in `fetch()`/`read()` because the bulk loop bypasses the per-request route
guard, and (2) each run self-generates a run/correlation id threaded into structured logs alongside
the inherited `X-Request-ID`.

## Blob Storage connector

`source_type = "blob"`, `source_path = "blob://{container}/{blob_name}"`.

- **`enumerate()`** — paginated container listing via continuation tokens (Blob listing can time out
  on 100k+ blob containers). Filters to the PDF allowlist so non-PDF blobs never reach the loader.
  Reads each blob's size and ETag/Last-Modified into the `SourceItem` without downloading bytes;
  skips/flags anything over 100 MB so it never buffers in memory.
- **`fetch()`** — downloads complete bytes with a timeout and bounded exponential backoff, then
  validates PDF **magic bytes** (`%PDF`) post-download before the content can reach Document
  Intelligence — a partial/interrupted read must not feed a corrupt PDF to the loader.
- **Auth** — prefer `DefaultAzureCredential` (managed identity) against `BLOB_ACCOUNT_URL`; fall
  back to `BLOB_STORAGE_CONNECTION_STRING`. **Avoid SAS URLs** as the primary path: a SAS can expire
  between `enumerate()` and `fetch()`, causing 403 mid-ingest. If SAS is unavoidable, mint it
  per-file inside `fetch()`, never at enumeration.

## SharePoint connector

`source_type = "sharepoint"`, `source_path = "sharepoint://{site_id}/{drive_id}/{item_id}"`.

- **`enumerate()`** — Microsoft Graph drive enumeration with explicit `@odata.nextLink` pagination
  (Graph's `PageIterator` is callback-driven; mishandling silently drops files, so pagination gets
  dedicated tests). Captures Graph `eTag`/`cTag` + size per item; PDF allowlist filtering applied
  here; oversized items skipped.
- **`fetch()`** — download with timeout + backoff and PDF magic-byte validation, identical guard to
  Blob. Honors Graph throttling: exponential backoff on 429 with `Retry-After` (Graph caps ~1000
  req/min).
- **Auth** — `ClientSecretCredential` via Graph using `SHAREPOINT_TENANT_ID` /
  `SHAREPOINT_CLIENT_ID` / `SHAREPOINT_CLIENT_SECRET`, scoped to `SHAREPOINT_SITE_ID` /
  `SHAREPOINT_DRIVE_ID`.

## Auth & secrets (per source, via the P0-7 KeyVault path)

Each connector owns **independent** credentials. Nothing is shared across connectors, nothing is
placed in an ingestion payload, and nothing appears in `source_path` or logs.

- Secrets resolve through the **P0-7 convention** (normalized `UPPER_SNAKE` env var → hyphenated
  Key Vault secret name, injected as env at deploy; downstream reads one canonical name via
  `os.getenv`). Connectors call `os.getenv` only — identical to how `upload()` already reads
  `DOC_INTELLIGENCE_KEY` etc. — and validate credential presence at init.
- **`source_path` normalization is a hard rule.** It uses opaque IDs only and **MUST NEVER** embed
  credentials, SAS query strings, or `user:pass@` forms — because the admin API surfaces
  `source_path` **verbatim** (`search_admin_service.py:148`, `:218`). The normalizer rejects any
  credential-bearing URI.
- All connector endpoint errors flow through `raise_api_error()` / `build_error_response()`
  (`errors.py`) with the `ApiErrorCode` enum, so raw exception text, tokens, and connection strings
  never reach a response or a log line.
- Connector trigger endpoints reuse the existing **`require_admin_token`** guard (`X-Admin-Token`,
  constant-time compare) — they are operator-facing. Unifying with QnA's AAD JWT (a `TocDoc.Ingest`
  scope) is a documented **P1-2** follow-up.

## Incremental sync, dedup & lifecycle (reusing P0-4 deterministic IDs)

Three distinct mechanisms — the brief conflates them; we separate them deliberately.

**(A) Exact-duplicate idempotency — already free and correct.** Re-ingesting identical bytes yields
an identical `document_id` (`sha256[:16]`) and identical chunk IDs (P0-4); the existing stale-delete
keyed on `(document_id, bot_tag)` (`custom_rag.py:320-332`) removes the prior copy before
`merge_or_upload`. Duplicate triggers, 5xx retries, and overlapping runs converge. This is the
correctness backstop that lets change-detection ship later without risking index bloat.

**(B) Edited-file cleanup — NOT free; requires a seam helper, shipped early.** This is a correctness
issue, not an optimization. When a file's content changes at a stable `source_path`, its `sha256`
changes, so `document_id` changes. The existing delete filter
`document_id eq '{new_id}' and bot_tag eq '{tag}'` then matches **nothing** — the old version's
chunks **orphan**, and `list_documents` (`search_admin_service.py:131`, grouped by `document_id`)
double-counts the logical file. Content-hash dedups identical *bytes*; only `source_path` is stable
across *edits*. We therefore add a `delete_by_source_path(source_path, bot_tag)` helper next to the
existing stale-delete block and invoke it **before upload** in the connector path, so an edited file
fully replaces its prior chunks under the same `source_path`. This lands in the core sequence (PR-1
/ PR-3 driver), **not** deferred — Blob and SharePoint must not ship to production with this gap
open.

**(C) Change detection — deferred optimization, kept OUT of the index.** Because the index schema is
immutable, sync-state (etag / last-modified / last-seen `document_id`) lives in an **external store
keyed by `(source_type, source_path, bot_tag)`** — never in new index fields. `enumerate()` compares
the remote validator against stored state and skips unchanged items, cutting egress and embedding
spend. Without it the connector is still correct (it re-processes everything and relies on
(A)+(B)); with it, large unchanged corpora are skipped. The store degrades to full reprocess if
wiped — never to silent skips.

**Lifecycle.** Re-ingestion automatically refreshes `ingestion_timestamp` (`custom_rag.py:411`),
which the admin service surfaces as `last_ingested_at`. `bot_tag` is assigned at **source level and
immutable**: because stale-delete has no cross-tag delete, reassigning a source to a new `bot_tag`
orphans its old chunks — handled by an admin `DELETE` + re-ingest **runbook**, not a code path.
Soft-delete of source-deleted files (chunks linger when the source file is gone) is a later PR: a
periodic audit job diffing live source enumeration against indexed `(source_path, bot_tag)`.

## Preserving tenant isolation (bot_tag on every chunk)

Isolation is preserved **by construction**, not by policing:

- `bot_tag` is connector-instance config, applied by the orchestrator from `connector.bot_tag` on
  every `upload()` call — never read from the document payload, never per-item. Cross-tagging is
  structurally impossible.
- Connector config validates `bot_tag` against the **exact existing** `^[A-Za-z0-9_-]{1,128}$`
  pattern (`routes.py:49`) **at init**, rejecting invalid tags before any network call. Note
  `upload()` itself does not validate `bot_tag`, so this validation is the connector's
  responsibility.
- Connectors never construct chunk IDs and never touch the index, so the P0-4 `id` scheme — which
  embeds `bot_tag` as its leading segment — keeps tenants partitioned exactly as `/upload` does
  today.
- No connector feature may bypass or weaken the bot_tag/document_id retrieval filters; connector
  trigger endpoints introduce no cross-tenant read surface.

## Rejected alternatives (the other two philosophies)

**Pluggable-framework (Connector protocol + registry + connector-agnostic orchestrator).** Elegant,
and it makes the *Nth* connector cheap (one class + `@register` + a config block). But the brief
asks for exactly **two** connectors. A registry/orchestrator framework is speculative generality
(YAGNI) that front-loads scaffolding — a protocol PR plus a registry+orchestrator PR — before any
connector can ship. Its claim that "only the registry preserves isolation and P0-4" is overreach:
isolation and determinism are preserved by *routing through `upload()`*, which the thin-adapter
design does identically with less surface. **We grafted its two genuinely superior ideas** — the
early `delete_by_source_path` edited-file fix, and the external out-of-index sync-state store — into
the chosen design.

**Ops-first in-process worker outside the FastAPI stack.** Correctly OPS-branded and it surfaced the
real execution-model question, but its central architectural choice is a **false dichotomy**: it
runs the driver as a worker *outside* FastAPI, sheds the 100 MB route guard / `ErrorEnvelope` /
`X-Request-ID` middleware, then re-implements all three — adding blast radius on exactly the P0-6
and observability guarantees the constraints emphasize. An in-stack trigger endpoint that launches a
background task keeps those guarantees for free. It also **mischaracterizes edited files as "safe
via the content-hash backstop"** (they are not — the hash changes and old chunks orphan), and its
PR-1 wants to "finalize a `source_type` enum at `create_search_index()` time," but the field is a
free-form filterable string — new *values* need no schema change. **We grafted its real catches** —
making the execution model explicit, the SAS-expiry warning, and post-download magic-byte
validation.

## Sequenced delivery plan (PR-sized increments)

Each PR is independently shippable and unit-testable with fakes; no live Azure. Per project policy,
tests are deliverables run by CI/the architect, and every PR body discloses that.

- **PR-1 (keystone — shared seam).** Add `source_type: str = "upload"` parameter to
  `custom_rag.upload()`, replacing the two hardcoded `"upload"` literals (`custom_rag.py:412`,
  `:464`). Default preserves 100% of current `/upload` and folder-batch behavior. Add the
  `delete_by_source_path(source_path, bot_tag)` helper beside the existing stale-delete block
  (`:320-332`). No index change — both fields already exist. Tests: existing callers still write
  `source_type='upload'`; an explicit value threads to the chunk dict; source_path delete removes
  prior chunks. Touches only `custom_rag.py`.
- **PR-2 (connector core).** `SourceConnector` Protocol, shared `ConnectorFile` (promoted from inline
  `_MockFile`), `SourceItem`, the source→bot_tag config loader with `BOT_TAG_PATTERN` validation at
  init, and the source-agnostic `enumerate → fetch → (lock → delete_by_source_path → upload)` driver
  loop. A `FakeConnector` proves correct bot_tag / source_type / source_path propagation and re-run
  idempotency. No Azure.
- **PR-3 (Blob connector).** `DefaultAzureCredential` / connection-string auth via the P0-7 KV path,
  paginated continuation-token listing + PDF allowlist, streaming `fetch()` with 100 MB
  pre-validation, timeout/backoff, and magic-byte validation. Ships Blob end-to-end. `source_path =
  blob://{container}/{blob_name}`.
- **PR-4 (SharePoint connector).** Graph `ClientSecretCredential`, `@odata.nextLink` pagination with
  explicit pagination tests, 429/`Retry-After` backoff. Reuses PR-1/PR-2 unchanged. `source_path =
  sharepoint://{site_id}/{drive_id}/{item_id}`.
- **PR-5 (operator trigger endpoint).** In-stack FastAPI endpoint(s) behind `require_admin_token`,
  launching the driver as a background task — inheriting `ErrorEnvelope` (P0-6) and `X-Request-ID`,
  with a self-generated run id in structured logs.
- **PR-6 (deferred — incremental change detection).** External sync-state store keyed by
  `(source_type, source_path, bot_tag)` (etag/last-modified/last-seen document_id); `enumerate()`
  skips unchanged items. Pure efficiency layer; correctness already holds via (A)+(B).
- **PR-7 (deferred — soft-delete + runbook).** Periodic audit job diffing live source vs indexed
  `(source_path, bot_tag)`; bot_tag-reassignment cleanup procedure; quota/backpressure and
  concurrency-locking guidance.

## Open questions for the architect

1. **Concurrency primitive.** The driver's `delete_by_source_path` + `merge_or_upload` window is
   non-atomic; two overlapping runs on the same `(source_path, bot_tag)` can race. Is an in-process
   `asyncio` lock sufficient for v1 (single-replica ingestion), or do we need a distributed lock
   (e.g., a Blob lease) because ingestion runs multi-replica? Azure Search offers no document-level
   transaction to fall back on.
2. **Content-hash collision across distinct source_paths.** Two byte-identical PDFs under the same
   `(bot_tag, fr_mode)` mint identical chunk IDs and overwrite each other regardless of
   `source_path` — conflicting with "`source_path` immutable for audit." Do we accept this for v1,
   or does the council want a future ID/dedup story keyed on `source_path`? Changing the P0-4 ID
   format would require a council decision **and** a reindex.
3. **Backpressure on shared quota.** Each chunk costs an Azure OpenAI embedding + a Document
   Intelligence call. For bulk runs, do we want a global concurrency/rate limiter and a
   pause-on-429 policy in PR-3/PR-4, or is that acceptable to defer to PR-6?
4. **Sync-state store backend.** For PR-6, what is the preferred external store — Table Storage,
   Cosmos, a dedicated Blob, or a small Postgres — given the existing client-RG deployment topology?
5. **bot_tag reassignment automation.** Is the admin `DELETE` + re-ingest runbook acceptable for v1,
   or should PR-7 ship an operator command that moves a source's chunks between bot_tags (delete-old
   + re-ingest-new) as a first-class action?
6. **fr_mode per source.** `fr_mode` is bound per connector instance. If a single source needs mixed
   `read`/`layout` documents, is per-item `fr_mode` override in scope, or do we model that as two
   connector instances over the same source?
