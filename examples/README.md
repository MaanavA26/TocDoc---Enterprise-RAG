# TocDoc examples

Runnable, self-contained quickstart examples for the TocDoc Python SDK
(`clients/python/tocdoc_sdk`) and the raw HTTP API. Each script is small,
documented, and reads its credentials from environment variables — nothing is
hardcoded.

| File | Shows |
|------|-------|
| [`01_ask.py`](01_ask.py) | Basic Q&A with the sync `TocDocClient` — print the grounded answer and its citations. |
| [`02_streaming.py`](02_streaming.py) | Token streaming with `AsyncTocDocClient.stream_ask` (async, Server-Sent Events). |
| [`03_admin.py`](03_admin.py) | `AdminClient`: list documents, read index stats, trigger a connector sync and poll its run status. |
| [`04_langchain_retriever.py`](04_langchain_retriever.py) | Use `TocDocRetriever` in a minimal LangChain (LCEL) chain — no LLM required. |
| [`05_curl.sh`](05_curl.sh) | Raw `curl` against `/qna`, `/qna/stream`, and an admin endpoint with the right headers. |

> The streaming examples (`02_streaming.py` and the `/qna/stream` call in
> `05_curl.sh`) mirror the SDK's `stream_ask` method. The `/qna/stream` SSE route
> is forward-looking — it is not yet part of the documented REST surface in
> [`docs/API.md`](../docs/API.md), so it requires a deployment that serves it.

## Prerequisites

- Python 3.10+ (the examples are exercised on 3.12).
- The SDK installed. From a repo checkout:

  ```bash
  pip install -e clients/python
  # For 04_langchain_retriever.py, install the optional extra (langchain-core only):
  pip install -e "clients/python[langchain]"
  ```

- A reachable TocDoc deployment (or your own local services) and valid
  credentials.

## Environment variables

The examples mirror the `tocdoc` CLI and use exactly these variable names. They
hold the configuration — never paste secrets into the scripts.

| Variable | Used by | Meaning |
|----------|---------|---------|
| `TOCDOC_BASE_URL` | all | Base URL of the service. The SDK appends the route, so pass the gateway prefix (e.g. `https://your-host/qna` for QnA, the ingestion host for admin). |
| `TOCDOC_TOKEN` | `01`, `02`, `04`, `05` | Bearer token (Azure AD JWT) for the QnA routes. Sent as `Authorization: Bearer …`. |
| `TOCDOC_ADMIN_TOKEN` | `03`, `05` | Static admin token. Sent as the `X-Admin-Token` header. |

The QnA endpoints and the admin endpoints live on **different** services (QnA vs
ingestion). If both sit behind one gateway, a single `TOCDOC_BASE_URL` works for
all examples; if they are on separate hosts, point `TOCDOC_BASE_URL` at the
service for the example you are running (admin examples need the ingestion host).

```bash
export TOCDOC_BASE_URL="https://your-host/qna"
export TOCDOC_TOKEN="eyJ..."
export TOCDOC_ADMIN_TOKEN="..."          # only needed for 03 / 05
```

## Running

```bash
python examples/01_ask.py "What is the refund policy?"
python examples/02_streaming.py "Summarize the onboarding guide."
python examples/03_admin.py acme blob     # bot_tag=acme, connector source_type=blob
python examples/04_langchain_retriever.py "What is the refund policy?"
bash   examples/05_curl.sh
```

## Tests

The `tests/` directory imports each example module and exercises its `main()`
against a **mocked** SDK (no live network), asserting the examples run and read
the documented env vars. They install no extra dependencies beyond `pytest` and
the SDK's `[langchain]` extra.

```bash
pip install -e "clients/python[langchain]" pytest
pytest examples/tests -q
```
