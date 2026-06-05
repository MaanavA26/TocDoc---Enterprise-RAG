# Changelog

All notable changes to `tocdoc-sdk` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Synchronous QnA client** (`TocDocClient`) — `ask(...)` against `POST /qna`,
  returning a typed `QnAAnswer` (grounded answer plus a `{filename: filepath}`
  citation map). Usable as a context manager.
- **Asynchronous QnA client** (`AsyncTocDocClient`) — an `asyncio` mirror of
  `TocDocClient` with the same models, retry policy, and `ApiError` semantics;
  `ask` is a coroutine and the client is an async context manager.
- **Multi-turn conversations** — `ask(...)` accepts a full `bot` history of
  turns in addition to a single `query`.
- **Streaming** (`stream_ask`) — consumes a Server-Sent-Events response and
  yields answer tokens; available on both the sync and async clients with no
  extra dependency. Streaming requests are not retried. The SSE parser is
  event-type aware: it yields **only** answer tokens (so `"".join(stream_ask(...))`
  is the clean answer), surfaces the server's `event: citation` payload
  out-of-band via the optional `on_citation` callback, and raises `ApiError` on
  a mid-stream `event: error` instead of swallowing it.
- **Admin client** (`AdminClient`) — read-only document and index reads
  (`list_documents`, `get_document`, `index_stats`) plus the connector sync
  control-plane (`trigger_connector_sync`, `get_connector_run`,
  `list_connector_runs`), authenticated with an `X-Admin-Token` header.
- **Command-line interface** (`tocdoc`) — a stdlib-only wrapper over the same
  clients (`tocdoc ask`, `tocdoc admin ...`); base URL and tokens resolve from a
  flag or an environment variable, and tokens are never printed.
- **LangChain integration** — `TocDocRetriever` / `AsyncTocDocRetriever` in the
  `tocdoc_sdk.langchain` submodule, behind the optional `tocdoc-sdk[langchain]`
  extra (`langchain-core` 1.x). The core package never imports `langchain`.
- **Structured error handling** (`ApiError`) — surfaces the service's error
  envelope (`code`, `message`, `request_id`, per-field `errors`), with a
  synthesized `HTTP_<status>` code for non-envelope bodies.
- **Transient-only retries** — `5xx` responses and connect/timeout errors are
  retried with exponential backoff; `4xx` responses are never retried.
- **Typed distribution** — ships a PEP 561 `py.typed` marker; core install
  pulls in only `httpx` and `pydantic`.

[Unreleased]: https://github.com/MaanavA26/TocDoc---Enterprise-RAG/commits/main
