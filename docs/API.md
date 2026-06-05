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

All request and response bodies are JSON (`application/json`), except
`POST /upload`, which accepts `multipart/form-data` for the file part with the
remaining inputs as query parameters.

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
| `INVALID_REQUEST`      | 400 (also 409, 413) | The request was understood but rejected (empty required field, missing confirmation, file too large, unsupported value). |
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

### 2. Ingestion `/upload` and `/health` — unauthenticated

The ingestion `POST /upload` and `GET /health` endpoints are **not**
authenticated. They are reachable without any credential.

### 3. Admin API — `X-Admin-Token` header

All `…/upload_pipeline/admin/*` routes require an `X-Admin-Token` header whose
value matches the server's configured `ADMIN_API_TOKEN` (compared in constant
time). This is an interim shared-secret scheme.

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
| 400  | `INVALID_REQUEST`      | A required field is present but empty: empty `bot` list, empty `user_query`, empty `bot_tag`, or empty `fr_tag`. |
| 401  | `UNAUTHORIZED`         | JWT missing, malformed, expired, or lacking an email claim. |
| 422  | `VALIDATION_ERROR`     | Body fails schema validation (e.g. missing `session_id`/`bot`/`fr_tag`/`bot_tag`, or wrong types). See `error.errors`. |
| 500  | `INTERNAL_ERROR`       | Pipeline failure. Detail is logged server-side only; no exception text is returned. |
| 503  | `UPSTREAM_UNAVAILABLE` | JWKS endpoint unavailable during token validation. |

> Note the 400-vs-422 split: a **missing or mistyped** field is a 422
> (`VALIDATION_ERROR`) raised by schema validation; a field that is **present
> but blank** is a 400 (`INVALID_REQUEST`) raised by the handler's explicit
> checks.

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

Ingest a single PDF, or every PDF under a server-side folder, into the search
index.

- **Auth:** none.
- **Content type:** `multipart/form-data` (file part) with query parameters.

### Query parameters

| Param      | Type   | Required | Default | Description |
|------------|--------|----------|---------|-------------|
| `bot_tag`  | string | yes      | —       | Tenant / bot identifier stored on every indexed chunk. |
| `filepath` | string | yes      | —       | Absolute file **or folder** path on the server. A directory triggers recursive batch mode over all `.pdf` files. |
| `fr_mode`  | string | no       | `read`  | Document Intelligence model. One of `read` (token-chunked, 500 tokens / 50 overlap) or `layout` (Markdown-header split). |

### Form part

| Part   | Type | Required | Description |
|--------|------|----------|-------------|
| `file` | file | conditional | Required in single-file mode (when `filepath` is **not** a directory). Ignored in folder/batch mode. |

### Responses

Single-file mode, `200`:

```json
{ "status": "successfully indexed", "detail": { } }
```

Folder/batch mode, `200` — one result entry per discovered PDF (per-file
failures are reported inline, not as a request-level error):

```json
[
  { "file": "a.pdf", "status": "success", "result": { } },
  { "file": "b.pdf", "status": "error", "error": "…" }
]
```

### Example

```http
POST /upload_pipeline/upload?bot_tag=tenant-123&filepath=/data/a.pdf&fr_mode=read HTTP/1.1
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
| 413  | `INVALID_REQUEST`      | Uploaded file exceeds the 100 MB limit. |
| 422  | `VALIDATION_ERROR`     | Missing required query param, or `fr_mode` not in `{read, layout}`. |
| 500  | `INTERNAL_ERROR`       | Unexpected ingestion failure (returned as "Ingestion service unavailable."). |

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
| 2 | `GET`    | `/health`                                 | QnA        | none          |
| 3 | `POST`   | `/upload`                                 | Ingestion  | none          |
| 4 | `GET`    | `/health`                                 | Ingestion  | none          |
| 5 | `GET`    | `/admin/documents`                        | Ingestion  | X-Admin-Token |
| 6 | `GET`    | `/admin/documents/{document_id}`          | Ingestion  | X-Admin-Token |
| 7 | `GET`    | `/admin/index/stats`                      | Ingestion  | X-Admin-Token |
| 8 | `DELETE` | `/admin/documents/{document_id}`          | Ingestion  | X-Admin-Token |
| 9 | `DELETE` | `/admin/bots/{bot_tag}/documents`         | Ingestion  | X-Admin-Token |
| 10| `POST`   | `/admin/documents/{document_id}/reindex`  | Ingestion  | X-Admin-Token |
| 11| `POST`   | `/admin/connectors/{source_type}/sync`    | Ingestion  | X-Admin-Token |
| 12| `GET`    | `/admin/connectors/runs`                  | Ingestion  | X-Admin-Token |
| 13| `GET`    | `/admin/connectors/runs/{run_id}`         | Ingestion  | X-Admin-Token |

**13 documented endpoints** (excluding the two `GET /` service-metadata roots).
