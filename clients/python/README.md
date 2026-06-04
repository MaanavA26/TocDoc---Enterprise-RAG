# tocdoc-sdk

A typed, dependency-light Python client for the TocDoc **QnA** HTTP API.

It mirrors the service's request/response/error contracts with Pydantic v2
models and wraps `POST /qna` behind a small, retrying `httpx` client. The
package is **standalone** — it does not import any service code, only `httpx`
and `pydantic`.

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

The models mirror the live server contract:

- **Request** (`QnARequest`): `{session_id, bot: [{user_query, bot_response?, answer?}], fr_tag, bot_tag}`
- **Success** (`QnAAnswer`): `{answer, citation: {filename: filepath}}`
- **Error** (`ApiError`): `{error: {code, message, request_id, errors?}}`
