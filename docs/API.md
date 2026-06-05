# TocDoc REST API Reference

This document is the REST API reference for the two TocDoc services:

- **QnA service** — answers grounded questions over an indexed corpus.
- **Ingestion service** — ingests documents into the search index and exposes a read-only/destructive **Admin API** plus operator connector-sync triggers.

It is generated from the route definitions in the source tree and describes the
on-the-wire contract: methods, paths, authentication, parameters/bodies, example
requests and responses, and error codes.

---

## Conventions

### Base URLs

Each service is mounted behind a gateway prefix (FastAPI `root_path`). Route
paths below are written **relative to the service base URL**.

| Service     | Base URL (gateway prefix) |
|-------------|---------------------------|
| QnA         | `…/qna`                   |
| Ingestion   | `…/upload_pipeline`       |

The Admin API is a router mounted under the ingestion service at the
`…/upload_pipeline/admin` prefix.

Replace the leading `…` with the host the service is deployed behind.

### Content type

All request and response bodies are JSON (`application/json`), with two
exceptions: `POST /upload` accepts `multipart/form-data` for the file part with
the remaining inputs as query parameters; and `POST /qna/stream` returns a
`text/event-stream` (SSE) response body (its request body is still JSON).

### Request correlation

Every response — success or error — carries an `X-Request-ID` response header.
If the client supplies an `X-Request-ID` request header it is echoed back;
otherwise the service generates one. On error responses the same value also
appears in the body at `error.request_id`.

### Error envelope (P0-6)

Every `4xx`/`5xx` response uses a single structured envelope:

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "Human-readable, safe message",
    "request_id": "0f1c…",
    "errors": [
      { "loc": ["body", "session_id"], "type": "missing", "msg": "Field required" }
    ]
  }
}
```

- `error.code` — a stable code from the table below.
- `error.message` — a safe, human-readable message. Never contains secrets,
  tokens, raw exception text, or echoed user input.
- `error.request_id` — matches the `X-Request-ID` response header.
- `error.errors` — present **only** on validation failures (`VALIDATION_ERROR`,
  HTTP 422); a list of per-field problems. The offending `input` value is never
  echoed back.

#### Stable error codes

| `code`                 | Typical HTTP status | Meaning |
|------------------------|---------------------|---------|
| `INVALID_REQUEST`      | 400 (also 409, 413, 415, 429) | The request was understood but rejected (empty required field, missing confirmation, file too large, unsupported file type, rate/concurrency limit exceeded). |
| `UNAUTHORIZED`         | 401, 403            | Authentication missing, malformed, expired, or invalid. |
| `NOT_FOUND`            | 404                 | The addressed resource does not exist in the requested scope. |
| `VALIDATION_ERROR`     | 422                 | Request body/params failed schema validation; see `error.errors`. |
| `UPSTREAM_UNAVAILABLE` | 503                 | A required dependency (JWKS, search index, server config) is unavailable. |
| `INTERNAL_ERROR`       | 500                 | Unhandled server-side failure. The detail is logged server-side only. |

---

## Authentication

There are three distinct auth regimes. They are **not** interchangeable.

### 1. QnA — Azure AD JWT (RS256, Bearer)

All QnA routes require an `Authorization: Bearer <jwt>` header **except** the
public routes: `GET /health`, the Swagger assets (`/docs`, `/redoc`,
`/openapi.json`), and CORS preflight (`OPTIONS`).

The token is verified cryptographically (RS256) against the Azure AD JWKS
endpoint for the configured tenant and audience. A user email is extracted from
the `upn`, `preferred_username`, or `email` claim.

Auth failures return the standard error envelope:

| Condition                                  | HTTP | `code`                 |
|--------------------------------------------|------|------------------------|
| Missing / non-`Bearer` Authorization header | 401  | `UNAUTHORIZED`         |
| Token expired / wrong issuer / malformed   | 401  | `UNAUTHORIZED`         |
| No email claim in token                    | 401  | `UNAUTHORIZED`         |
| JWKS endpoint unavailable                  | 503  | `UPSTREAM_UNAVAILABLE` |

The raw token value is never logged.

### 2. Ingestion `/health` — unauthenticated

The ingestion `GET /health` endpoint is **not** authenticated. It is reachable
without any credential. (`POST /upload` is **no longer** unauthenticated — it
now uses the `X-Admin-Token` scheme below.)

### 3. Admin API and `/upload` — `X-Admin-Token` header

All `…/upload_pipeline/admin/*` routes **and** `POST /upload` require an
`X-Admin-Token` header whose value matches the server's configured
`ADMIN_API_TOKEN` (compared in constant time). This is an interim shared-secret
scheme.

| Condition                                  | HTTP | Note |
|--------------------------------------------|------|------|
| Server has no `ADMIN_API_TOKEN` configured | 503  | Refuses rather than bypass auth. |
| Header missing **or** value wrong          | 401  | Deliberately indistinguishable, to prevent token probing. |

---

# QnA Service

Base URL: `…/qna`

## `POST /qna`

Answer a question grounded in the indexed corpus for a given bot/tenant.

- **Auth:** Bearer JWT (required).
- **Content type:** `application/json`.

### Request body (`Payload`)

| Field        | Type                | Required | Description |
|--------------|---------------------|----------|-------------|
| `session_id` | string              | yes      | Correlation / session identifier. |
| `bot`        | array of turns      | yes      | Ordered conversation history, oldest → newest. The query answered is the `user_query` of the **last** turn. |
| `fr_tag`     | string              | yes      | Feature / retrieval tag (e.g. `read` or `layout`). Must be non-empty. |
| `bot_tag`    | string              | yes      | Bot / tenant identifier. Enforces tenant isolation in the search layer. Must be non-empty. |

Each entry in `bot` (a conversation turn) has:

| Field          | Type             | Required | Description |
|----------------|------------------|----------|-------------|
| `user_query`   | string           | yes      | The user's input for the turn. |
| `bot_response` | string \| null   | no       | The bot's prior response for the turn. |
| `answer`       | string \| null   | no       | Alternate field for bot response content; used as a fallback when `bot_response` is absent. |

Extra fields on a turn are accepted and ignored.

### Response `200`

The success payload is byte-stable with the historical `{answer, citation}`
shape. Optional fields are omitted (never serialized as `null`) when unset.

| Field            | Type                                | Description |
|------------------|-------------------------------------|-------------|
| `answer`         | string                              | The grounded answer text. |
| `citation`       | object `{filename: filepath}`       | Flat map of cited filename → filepath. May be empty. |
| `page_citations` | object `{filename: [page, …]}` \| omitted | Optional; cited filename → ordered-unique page strings. Omitted until ingestion populates page numbers. |

### Example

Request:

```http
POST /qna/qna HTTP/1.1
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "session_id": "sess-abc-123",
  "bot_tag": "tenant-123",
  "fr_tag": "read",
  "bot": [
    { "user_query": "What is the refund window?", "bot_response": "It is 30 days." },
    { "user_query": "And for digital goods?" }
  ]
}
```

Response:

```json
{
  "answer": "Digital goods may be refunded within 14 days of purchase.",
  "citation": {
    "refund-policy.md": "/docs/refund-policy.md"
  }
}
```

With page-level citations populated:

```json
{
  "answer": "Digital goods may be refunded within 14 days of purchase.",
  "citation": { "refund-policy.md": "/docs/refund-policy.md" },
  "page_citations": { "refund-policy.md": ["3", "7"] }
}
```

### Errors

| HTTP | `code`                 | When |
|------|------------------------|------|
| 400  | `INVALID_REQUEST`      | A required field is present but empty (empty `bot` list, empty `user_query`, empty `bot_tag`, or empty `fr_tag`), or `fr_tag` is not one of `read`/`layout`. |
| 401  | `UNAUTHORIZED`         | JWT missing, malformed, expired, or lacking an email claim. |
| 403  | `UNAUTHORIZED`         | Tenant binding rejected the request — see note below. |
| 422  | `VALIDATION_ERROR`     | Body fails schema validation (e.g. missing `session_id`/`bot`/`fr_tag`/`bot_tag`, or wrong types). See `error.errors`. |
| 429  | `INVALID_REQUEST`      | Per-key rate limit or the global in-flight concurrency cap was exceeded. Carries a `Retry-After` header. See *Rate limiting* below. |
| 500  | `INTERNAL_ERROR`       | Pipeline failure. Detail is logged server-side only; no exception text is returned. |
| 503  | `UPSTREAM_UNAVAILABLE` | JWKS endpoint unavailable during token validation. |

> Note the 400-vs-422 split: a **missing or mistyped** field is a 422
> (`VALIDATION_ERROR`) raised by schema validation; a field that is **present
> but blank** is a 400 (`INVALID_REQUEST`) raised by the handler's explicit
> checks.

#### Tenant binding (403)

A within-tenant binding guard ties the requested `bot_tag` to the token's
validated `tid` claim. It is **on by default** and controlled by
`QNA_ENFORCE_TENANT_BINDING` (set to a falsy value — `false`/`0`/`no`/`off` —
to opt out) plus a `QNA_TENANT_BOT_TAG_MAP` JSON allowlist mapping each `tid`
to its permitted `bot_tag` values.

When enforcement is on, the request is rejected **before any retrieval** with a
`403` / `UNAUTHORIZED` envelope if the token has no `tid`, the `tid` is unmapped,
the `bot_tag` is not in that tenant's allowlist, or the allowlist map is
missing/unparseable (fail-closed). The rejection message is generic and never
echoes the `bot_tag`, `tid`, or allowlist contents. This guard applies to both
`POST /qna` and `POST /qna/stream`.

#### Rate limiting (429)

`POST /qna` and `POST /qna/stream` are throttled by a per-key sliding-window
rate limiter (`QNA_RATE_LIMIT_PER_MIN`, default 120 requests / 60s window) and a
global in-flight concurrency cap (`QNA_MAX_CONCURRENCY`, default 16). Exceeding
either returns `429` with a `Retry-After` header (an `INVALID_REQUEST`
envelope). The key is the validated tenant id when available, else the client
IP. This is per-process defense-in-depth; ingress-level rate limiting is still
required for multi-replica deployments.

## `POST /qna/stream`

Server-Sent-Events (SSE) streaming variant of `POST /qna`. Reuses the **same**
auth (Bearer JWT), tenant binding, `bot_tag`/`fr_tag` validation, rate limiting,
and retrieval as `/qna` — only the answer-generation step streams
token-by-token. The agentic dark seam is intentionally **not** consulted here;
streaming always uses the standard pipeline.

- **Auth:** Bearer JWT (required).
- **Request content type:** `application/json` — identical `Payload` body to
  `/qna` (`session_id`, `bot`, `fr_tag`, `bot_tag`).
- **Response content type:** `text/event-stream`.

### Response `200` — event stream

The response is an SSE stream. Response headers include
`Content-Type: text/event-stream`, `Cache-Control: no-cache`,
`X-Accel-Buffering: no` (disables proxy buffering), and `X-Request-ID`.

The wire format, in order:

1. **Token events** — one per answer token, in order. Each is a bare `data:`
   line carrying the raw token text (no event name):

   ```
   data: <token text>

   ```

   The full answer is reconstructed by **concatenating the token payloads** in
   order. There is no separate `answer` field anywhere in the stream.

2. **Citation event** — exactly one, tagged `event: citation`, after the last
   token. Its `data:` line is a JSON object matching the `/qna` citation shape:
   `{ "citation": {filename: filepath}, "page_citations"?: {filename: [page, …]} }`.
   `page_citations` is present only when page numbers are available.

3. **Done sentinel** — the stream always ends with the OpenAI-style sentinel:

   ```
   data: [DONE]

   ```

### Error contract

- A failure **before the first token** (validation, tenant binding, rephrasal,
  embedding, search) is raised before the stream opens and returned as the
  normal structured **error envelope** (e.g. `400`, `401`, `403`, `422`, `429`,
  `503`) — exactly as for `/qna`. No partial stream is sent.
- A failure **after streaming has begun** (headers already sent) cannot use the
  envelope. Instead the stream emits one terminal `event: error` event whose
  `data:` is `{ "error": { "code": "INTERNAL_ERROR", "message": "Streaming failed", "request_id": "…" } }`,
  followed by the `data: [DONE]` sentinel.

### Example

Request:

```http
POST /qna/qna/stream HTTP/1.1
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "session_id": "sess-abc-123",
  "bot_tag": "tenant-123",
  "fr_tag": "read",
  "bot": [ { "user_query": "What is the refund window?" } ]
}
```

Response (`Content-Type: text/event-stream`):

```
data: Digital goods

data:  may be refunded

data:  within 14 days.

event: citation
data: {"citation": {"refund-policy.md": "/docs/refund-policy.md"}, "page_citations": {"refund-policy.md": ["3"]}}

data: [DONE]

```

## `GET /health`

Liveness / readiness probe.

- **Auth:** none (public route).

### Response `200`

```json
{
  "status": "ok",
  "qna_module": "loaded",
  "timestamp": "2026-06-01T12:00:00.000000"
}
```

If the answer entrypoint is not importable, the response stays HTTP 200 with a
degraded body, e.g. `{ "status": "error", "qna_module": "missing generate_answer function" }`.

> The QnA service also exposes `GET /` returning static service metadata; it is
> not part of the functional API surface.

---

# Ingestion Service

Base URL: `…/upload_pipeline`

## `POST /upload`

Ingest a single supported document, or every supported document under a
server-side folder, into the search index.

- **Auth:** `X-Admin-Token` header (required — same scheme as the Admin API).
- **Content type:** `multipart/form-data` (file part) with query parameters.

### Supported formats

PDFs are parsed via Azure Document Intelligence; the other formats are extracted
to plain text by the loader registry. All formats then feed the same
chunk → embed → index pipeline. The supported file extensions are:

`.pdf`, `.docx`, `.pptx`, `.html`, `.htm`, `.md`, `.txt`

In folder/batch mode, files with an unsupported extension are silently skipped
(never an error). In single-file mode an unsupported extension returns `415`.

### Query parameters

| Param      | Type   | Required | Default | Description |
|------------|--------|----------|---------|-------------|
| `bot_tag`  | string | yes      | —       | Tenant / bot identifier stored on every indexed chunk. Pattern-validated against `^[A-Za-z0-9_-]{1,128}$`; a non-match returns `422`. |
| `filepath` | string | yes      | —       | Absolute file **or folder** path on the server (resolved against a configured allowed root; traversal/escape is rejected). A directory triggers recursive batch mode over all supported documents. |
| `fr_mode`  | string | no       | `read`  | Document Intelligence model. One of `read` (token-chunked, 500 tokens / 50 overlap) or `layout` (Markdown-header split). |

### Form part

| Part   | Type | Required | Description |
|--------|------|----------|-------------|
| `file` | file | conditional | Required in single-file mode (when `filepath` is **not** a directory). Ignored in folder/batch mode. |

### Responses

Single-file mode, `200` (full index write):

```json
{ "status": "successfully indexed", "detail": { } }
```

Single-file mode, `207` (Multi-Status) — a **partial / degraded** index write
(some chunks failed to index). The response is **not** an error envelope; the
per-chunk detail is carried in `detail`:

```json
{ "status": "partially indexed", "detail": { "status": "degraded", "failed_chunks": 2 } }
```

Folder/batch mode, `200` — one result entry per discovered document (per-file
failures are reported inline, not as a request-level error). A file whose index
write was partial reports `status: "degraded"`:

```json
[
  { "file": "a.pdf",  "status": "success",  "result": { } },
  { "file": "b.docx", "status": "degraded", "result": { } },
  { "file": "c.pptx", "status": "error",    "error": "Failed to process file." }
]
```

### Example

```http
POST /upload_pipeline/upload?bot_tag=tenant-123&filepath=/data/a.pdf&fr_mode=read HTTP/1.1
X-Admin-Token: <admin-token>
Content-Type: multipart/form-data; boundary=----X

------X
Content-Disposition: form-data; name="file"; filename="a.pdf"
Content-Type: application/pdf

<binary PDF bytes>
------X--
```

### Errors

| HTTP | `code`                 | When |
|------|------------------------|------|
| 400  | `INVALID_REQUEST`      | `filepath` is not a directory and no `file` part was supplied. |
| 401  | `UNAUTHORIZED`         | `X-Admin-Token` header missing or wrong (deliberately indistinguishable). |
| 413  | `INVALID_REQUEST`      | Uploaded file exceeds the 100 MB limit, or a folder holds more than the per-request file cap (`INGESTION_MAX_FOLDER_FILES`, default 500). |
| 415  | `INVALID_REQUEST`      | Single-file mode with an unsupported file extension. |
| 422  | `VALIDATION_ERROR`     | Missing required query param, `bot_tag` violates its pattern, or `fr_mode` not in `{read, layout}`. |
| 429  | `INTERNAL_ERROR`       | The in-flight upload concurrency cap (`INGESTION_MAX_CONCURRENT_UPLOADS`, default 4) is reached. Carries a `Retry-After` header. See *Rate limiting* below. |
| 500  | `INTERNAL_ERROR`       | Unexpected ingestion failure (returned as "Ingestion service unavailable."). |
| 503  | `UPSTREAM_UNAVAILABLE` | The server has no `ADMIN_API_TOKEN` configured (refuses rather than bypass auth). |

> **429 envelope `code`:** the `/upload` 429 is raised as a bare HTTP 429, which
> the error handler maps to `INTERNAL_ERROR` (429 is not in its status→code
> table) — unlike the QnA 429s, which are explicitly `INVALID_REQUEST`. The
> `Retry-After` header is present in both cases.

#### Rate limiting (429)

`POST /upload` bounds concurrent in-flight uploads with a non-blocking slot
counter (`INGESTION_MAX_CONCURRENT_UPLOADS`, default 4). When all slots are
taken, additional requests get a `429` with a `Retry-After` header rather than
queueing onto the expensive Document-Intelligence / embedding / index path.

## `GET /health`

Liveness probe.

- **Auth:** none.

### Response `200`

```json
{ "status": "healthy" }
```

> The ingestion service also exposes `GET /` returning static service metadata;
> it is not part of the functional API surface.

---

# Admin API

Base URL: `…/upload_pipeline/admin`

- **Auth (all routes):** `X-Admin-Token` header (see Authentication above).
- All routes share the standard error envelope. Beyond per-route errors, any
  route may return:
  - `401 UNAUTHORIZED` — missing/wrong admin token.
  - `503 UPSTREAM_UNAVAILABLE` — admin token not configured on the server, or
    (for index-backed routes) the search index is temporarily unavailable.
  - `422 VALIDATION_ERROR` — a path/query value violates its allowed pattern
    (`bot_tag`: `^[A-Za-z0-9_-]{1,128}$`; `document_id`: `^[A-Za-z0-9_.:-]{1,256}$`;
    `run_id`: `^[A-Za-z0-9_-]{1,128}$`).

## `GET /documents`

List indexed documents (one row per document, aggregated from chunk metadata)
for a bot/tenant scope.

- **Query:** `bot_tag` (string, required, pattern-validated).

### Response `200`

```json
{
  "bot_tag": "tenant-123",
  "count": 1,
  "documents": [
    {
      "document_id": "doc-1",
      "source_path": "/data/a.pdf",
      "source_type": "blob",
      "fr_tag": "read",
      "chunk_count": 12,
      "first_ingested_at": "2026-06-01T10:00:00Z",
      "last_ingested_at": "2026-06-01T10:05:00Z"
    }
  ]
}
```

`source_path`, `source_type`, `fr_tag`, and the timestamps may be `null` for
older chunks that predate that metadata.

## `GET /documents/{document_id}`

Get one document's summary within a bot/tenant scope.

- **Path:** `document_id` (string, pattern-validated).
- **Query:** `bot_tag` (string, required, pattern-validated).

### Response `200`

```json
{
  "bot_tag": "tenant-123",
  "document_id": "doc-1",
  "source_path": "/data/a.pdf",
  "source_type": "blob",
  "fr_tag": "read",
  "chunk_count": 12,
  "ingestion_timestamps": ["2026-06-01T10:00:00Z"],
  "sample_chunks": [ { "id": "doc-1::0", "chunk_index": 0 } ]
}
```

### Errors

| HTTP | `code`     | When |
|------|------------|------|
| 404  | `NOT_FOUND`| No chunks exist for this document in this `bot_tag` scope. |

## `GET /index/stats`

Aggregate statistics for one bot/tenant scope.

- **Query:** `bot_tag` (string, required, pattern-validated).

### Response `200`

```json
{
  "bot_tag": "tenant-123",
  "document_count": 5,
  "chunk_count": 240,
  "source_types": { "blob": 4, "sharepoint": 1 },
  "fr_modes": { "read": 3, "layout": 2 }
}
```

## `DELETE /documents/{document_id}`

Delete every chunk of one document within a bot/tenant scope. Idempotent:
deleting a non-existent document returns `200` with `deleted_chunks: 0`. The
`bot_tag` filter is always applied, so chunks under other tenants are never
affected.

- **Path:** `document_id` (string, pattern-validated).
- **Query:** `bot_tag` (string, required, pattern-validated).

### Response `200`

```json
{
  "bot_tag": "tenant-123",
  "document_id": "doc-1",
  "deleted_chunks": 12,
  "status": "deleted"
}
```

## `DELETE /bots/{bot_tag}/documents`

Delete **all** chunks for one bot/tenant (every document). Destructive —
requires an explicit `confirm=true`. Other tenants are never affected.

- **Path:** `bot_tag` (string, pattern-validated).
- **Query:** `confirm` (boolean, default `false`). Must be `true`.

### Response `200`

```json
{
  "bot_tag": "tenant-123",
  "deleted_chunks": 240,
  "deleted_documents": 5,
  "status": "deleted"
}
```

### Errors

| HTTP | `code`            | When |
|------|-------------------|------|
| 400  | `INVALID_REQUEST` | `confirm` is absent or not `true`. Nothing is deleted. |

### Example

```http
DELETE /upload_pipeline/admin/bots/tenant-123/documents?confirm=true HTTP/1.1
X-Admin-Token: <admin-token>
```

## `POST /documents/{document_id}/reindex`

Reindex a document. **Not implemented** — there is no source persistence yet.
Auth and input patterns are still validated. The body is a normal payload
(returned with HTTP **501**), **not** an error envelope.

- **Path:** `document_id` (string, pattern-validated).
- **Query:** `bot_tag` (string, required, pattern-validated).

### Response `501`

```json
{
  "status": "not_implemented",
  "reason": "Reindex requires source persistence or connector metadata. Use delete + ingest for now."
}
```

## `POST /connectors/{source_type}/sync`

Trigger a connector sync as an in-process background task. The connector is
built entirely from server-side environment configuration (the source → bot_tag
binding is fixed by config, never by the request). Returns immediately with a
generated `run_id`; the enumerate → fetch → upload loop runs in the background.

- **Path:** `source_type` — one of `blob` or `sharepoint`.
- **Body / query:** none.

### Response `202`

```json
{
  "run_id": "9f8c7b6a5d4e3f2a1b0c9d8e7f6a5b4c",
  "source_type": "blob",
  "status": "started"
}
```

### Errors

| HTTP | `code`            | When |
|------|-------------------|------|
| 400  | `INVALID_REQUEST` | `source_type` is not `blob`/`sharepoint`, or the connector is misconfigured on the server (e.g. missing `CONNECTOR_BOT_TAG`, `BLOB_CONTAINER`, or the SharePoint site/drive IDs). |

### Example

```http
POST /upload_pipeline/admin/connectors/blob/sync HTTP/1.1
X-Admin-Token: <admin-token>
```

## `GET /connectors/runs`

List recent connector-sync runs, newest first. Admin-wide (bot_tag-agnostic).
Backed by an in-process store — state is **lost on restart**.

- **Query:** `limit` (integer, `1`–`200`, default `50`).

### Response `200`

```json
{
  "count": 1,
  "runs": [
    {
      "run_id": "9f8c7b6a5d4e3f2a1b0c9d8e7f6a5b4c",
      "status": "completed",
      "source_type": "blob",
      "bot_tag": "tenant-123",
      "started_at": "2026-06-01T10:00:00Z",
      "finished_at": "2026-06-01T10:02:00Z",
      "processed_count": 8,
      "failed_count": 0,
      "error": null
    }
  ]
}
```

## `GET /connectors/runs/{run_id}`

Get one connector-sync run's status. `status` is one of `started`,
`completed`, or `failed`. `error` (an `{ error_class, safe_message }` object) is
present only on failure.

- **Path:** `run_id` (string, pattern-validated).

### Response `200`

```json
{
  "run_id": "9f8c7b6a5d4e3f2a1b0c9d8e7f6a5b4c",
  "status": "failed",
  "source_type": "blob",
  "bot_tag": "tenant-123",
  "started_at": "2026-06-01T10:00:00Z",
  "finished_at": "2026-06-01T10:01:00Z",
  "processed_count": 0,
  "failed_count": 0,
  "error": { "error_class": "ConnectorError", "safe_message": "Connector sync run failed" }
}
```

### Errors

| HTTP | `code`     | When |
|------|------------|------|
| 404  | `NOT_FOUND`| Unknown `run_id` — never issued, evicted from the bounded store, or lost on restart. |

---

## Endpoint index

| # | Method   | Path (relative to base URL)               | Service    | Auth          |
|---|----------|-------------------------------------------|------------|---------------|
| 1 | `POST`   | `/qna`                                    | QnA        | Bearer JWT    |
| 2 | `POST`   | `/qna/stream`                             | QnA        | Bearer JWT    |
| 3 | `GET`    | `/health`                                 | QnA        | none          |
| 4 | `POST`   | `/upload`                                 | Ingestion  | X-Admin-Token |
| 5 | `GET`    | `/health`                                 | Ingestion  | none          |
| 6 | `GET`    | `/admin/documents`                        | Ingestion  | X-Admin-Token |
| 7 | `GET`    | `/admin/documents/{document_id}`          | Ingestion  | X-Admin-Token |
| 8 | `GET`    | `/admin/index/stats`                      | Ingestion  | X-Admin-Token |
| 9 | `DELETE` | `/admin/documents/{document_id}`          | Ingestion  | X-Admin-Token |
| 10| `DELETE` | `/admin/bots/{bot_tag}/documents`         | Ingestion  | X-Admin-Token |
| 11| `POST`   | `/admin/documents/{document_id}/reindex`  | Ingestion  | X-Admin-Token |
| 12| `POST`   | `/admin/connectors/{source_type}/sync`    | Ingestion  | X-Admin-Token |
| 13| `GET`    | `/admin/connectors/runs`                  | Ingestion  | X-Admin-Token |
| 14| `GET`    | `/admin/connectors/runs/{run_id}`         | Ingestion  | X-Admin-Token |

**14 documented endpoints** (excluding the two `GET /` service-metadata roots).
