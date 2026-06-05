# tocdoc-sdk

A typed, dependency-light Python client for the TocDoc HTTP APIs.

It mirrors the services' request/response/error contracts with Pydantic v2
models and wraps the endpoints behind small, retrying `httpx` clients. The
package is **standalone** — it does not import any service code, only `httpx`
and `pydantic`. It provides:

- **`TocDocClient`** — synchronous QnA client (`POST /qna`).
- **`AsyncTocDocClient`** — `asyncio` mirror of the QnA client.
- **`AdminClient`** — admin API client (ingestion service, `X-Admin-Token`
  auth): read-only document/index reads plus the connector sync control-plane.

## Install

```bash
pip install tocdoc-sdk
# or, from a checkout:
pip install -e clients/python
```

Requires Python 3.10+.

## Quickstart

```python
from tocdoc_sdk import TocDocClient

client = TocDocClient(
    base_url="https://your-tocdoc-host",  # any host/proxy prefix; client POSTs to <base_url>/qna
    token="YOUR_BEARER_TOKEN",            # sent as `Authorization: Bearer ...`, never logged
    timeout=30.0,
    max_retries=2,                        # transient-only retries (5xx + connect/timeout)
)

answer = client.ask(
    session_id="session-123",
    bot_tag="acme",       # tenant/bot identifier
    fr_tag="read",        # feature/retrieval tag
    query="What is the refund policy?",
)

print(answer.answer)        # -> the grounded answer text
print(answer.citations)     # -> {"policy.md": "/docs/policy.md"}  (flat filename -> filepath)

client.close()  # or use `with TocDocClient(...) as client:`
```

### Multi-turn conversations

Pass a full history instead of a single `query`. The last turn's `user_query`
is the question that gets answered:

```python
answer = client.ask(
    session_id="session-123",
    bot_tag="acme",
    fr_tag="read",
    bot=[
        {"user_query": "What is the refund policy?", "bot_response": "Refunds take 30 days."},
        {"user_query": "And for digital goods?"},
    ],
)
```

## Async client

`AsyncTocDocClient` is a drop-in `asyncio` mirror of `TocDocClient`: same
models, same retry policy, same `ApiError` semantics. `ask` is a coroutine and
the client is an async context manager (`async with` / `await client.aclose()`).
The backoff sleep is awaited (defaults to `asyncio.sleep`) so it yields the
event loop instead of blocking it.

```python
import asyncio
from tocdoc_sdk import AsyncTocDocClient

async def main():
    async with AsyncTocDocClient(
        base_url="https://your-tocdoc-host",
        token="YOUR_BEARER_TOKEN",   # never logged
        timeout=30.0,
        max_retries=2,
    ) as client:
        answer = await client.ask(
            session_id="session-123",
            bot_tag="acme",
            fr_tag="read",
            query="What is the refund policy?",
        )
        print(answer.answer)

asyncio.run(main())
```

## Admin API

The admin endpoints live on the **ingestion** service and authenticate with a
static `X-Admin-Token` header (not the QnA bearer token), so `AdminClient` is a
separate client with its own `base_url` and `admin_token`. If your deployment
fronts both services behind one proxy, pass it the same URL as the QnA client;
if they are separate hosts, pass the ingestion host. The admin token is sent as
a header and is **never logged**.

The reads below are scoped by `bot_tag` (tenant isolation is enforced
server-side):

```python
from tocdoc_sdk import AdminClient

with AdminClient(
    base_url="https://your-ingestion-host",
    admin_token="YOUR_ADMIN_TOKEN",  # sent as `X-Admin-Token`, never logged
    timeout=30.0,
    max_retries=2,
) as admin:
    # GET /admin/documents?bot_tag=acme
    docs = admin.list_documents(bot_tag="acme")
    print(docs.count, [d.document_id for d in docs.documents])

    # GET /admin/documents/{document_id}?bot_tag=acme
    detail = admin.get_document(bot_tag="acme", document_id="doc-1")
    print(detail.chunk_count, detail.sample_chunks)

    # GET /admin/index/stats?bot_tag=acme
    stats = admin.index_stats(bot_tag="acme")
    print(stats.document_count, stats.chunk_count, stats.source_types)
```

The same `ApiError` and retry behavior apply (a 404 when a document is not in
scope raises `ApiError`; non-envelope error bodies degrade to a synthesized
`HTTP_<status>` code).

### Connector sync control-plane

The same `AdminClient` can trigger connector syncs and read their run status.
These are **admin-wide** (not `bot_tag`-scoped): the connector's `bot_tag` and
per-source location are bound server-side from environment config, so the source
-> `bot_tag` binding is immutable and the trigger takes only a `source_type`.

```python
# POST /admin/connectors/blob/sync  ->  202 Accepted (runs in the background)
run = admin.trigger_connector_sync("blob")
print(run.run_id, run.status)  # e.g. "<hex>" "started"

# GET /admin/connectors/runs/{run_id}  (poll for the terminal status)
status = admin.get_connector_run(run.run_id)
print(status.status, status.processed_count, status.failed_count)
if status.error is not None:
    print(status.error.error_class, status.error.safe_message)

# GET /admin/connectors/runs?limit=20  (recent runs, newest first)
recent = admin.list_connector_runs(limit=20)
print(recent.count, [r.run_id for r in recent.runs])
```

An unsupported `source_type` or missing server-side connector config raises
`ApiError` (400); a `run_id` that is unknown, evicted, or lost on a server
restart raises `ApiError` (404). Run state is in-process server-side and is not
durable across restarts.

## Error handling

Every non-2xx response is raised as `ApiError`, carrying the fields from the
service's structured error envelope (`{"error": {"code", "message", "request_id"}}`):

```python
from tocdoc_sdk import ApiError, TocDocClient

try:
    answer = client.ask(session_id="s", bot_tag="acme", fr_tag="read", query="...")
except ApiError as e:
    print(e.status_code)  # e.g. 401
    print(e.code)         # e.g. "UNAUTHORIZED"
    print(e.message)      # safe, human-readable message
    print(e.request_id)   # correlation ID (matches the X-Request-ID header)
    print(e.errors)       # structured per-field errors on VALIDATION_ERROR (else None)
```

## Retry behavior

Only **transient** failures are retried: `5xx` responses and
connect/timeout errors, up to `max_retries` times with exponential backoff. A
`4xx` is never retried. If a non-envelope body is returned (e.g. an HTML error
page from a proxy), `ApiError` is still raised with a synthesized
`code = "HTTP_<status>"`.

## Contract

The models mirror the live server contracts:

**QnA**

- **Request** (`QnARequest`): `{session_id, bot: [{user_query, bot_response?, answer?}], fr_tag, bot_tag}`
- **Success** (`QnAAnswer`): `{answer, citation: {filename: filepath}}`
- **Error** (`ApiError`): `{error: {code, message, request_id, errors?}}`

**Admin** — mirrors `services/ingestion/admin`:

- `GET /admin/documents` -> `DocumentListResponse` (`{bot_tag, count, documents: [DocumentSummary]}`)
- `GET /admin/documents/{id}` -> `DocumentDetailResponse`
- `GET /admin/index/stats` -> `IndexStatsResponse` (`{bot_tag, document_count, chunk_count, source_types, fr_modes}`)
- `POST /admin/connectors/{source_type}/sync` -> `ConnectorSyncResponse` (`{run_id, source_type, status}`)
- `GET /admin/connectors/runs` -> `ConnectorRunListResponse` (`{count, runs: [ConnectorRunStatusResponse]}`)
- `GET /admin/connectors/runs/{run_id}` -> `ConnectorRunStatusResponse` (`{run_id, status, source_type, bot_tag, started_at, finished_at, processed_count, failed_count, error?}`)
