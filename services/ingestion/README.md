# Ingestion Service

FastAPI service that turns PDFs into a searchable corpus: extract text, chunk it,
embed each chunk, and upsert the chunks into an Azure AI Search index. Every chunk
is stamped with a `bot_tag` (tenant) and a deterministic `document_id` so re-ingesting
the same document replaces its prior chunks instead of duplicating them.

## Key modules

| Path | Responsibility |
| --- | --- |
| `custom_rag.py` | Core ingestion pipeline (`rag.upload`): content-hash `document_id` (SHA-256, deterministic per file), stale-chunk cleanup, chunking, embedding, and index upsert. `read` mode uses token-based chunking (`tiktoken`, `cl100k_base`); `layout` mode uses Markdown-header splitting to preserve structure. |
| `admin/` | Read-mostly control plane: `routes.py` (document/stats listing, deletes, connector-sync triggers), `auth.py`, `models.py`, `search_admin_service.py`. Mounted under the `/admin` prefix. |
| `connectors/` | Pluggable source connectors: `blob.py` (Azure Blob Storage), `sharepoint.py` (SharePoint via Graph), `core.py` (shared `SourceItem`/`ConnectorFile` contracts), and `run_status.py` (in-process sync-run status store). Connectors feed bytes into `rag.upload`; they never mint chunk IDs themselves. |
| `observability.py` | Request-ID middleware and structured logging; threads a correlation `request_id` through ingestion stage events. |
| `middleware.py` | Request middleware wiring. |
| `errors.py` | Structured error responses shared across routes. |

## Endpoints

- `GET /health` — liveness probe.
- `POST /upload` — ingest a single PDF or a folder of PDFs. Query params:
  `bot_tag` (tenant), `filepath` (server-side file or directory), and
  `fr_mode` (`read` for token chunking, `layout` for header splitting). A
  directory path triggers recursive batch mode.
- `GET /` — service metadata.
- `GET|DELETE /admin/...` — list/inspect indexed documents and stats by `bot_tag`,
  and delete chunks for a document or an entire tenant (destructive deletes require
  explicit confirmation).
- `POST /admin/connectors/{source_type}/sync` — trigger a `blob` or `sharepoint`
  sync as a background task; `GET /admin/connectors/...` reports recent run status.

See [`../../docs/API.md`](../../docs/API.md) for the full endpoint reference.

## Run

```sh
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5501
```

The service listens on port `5501` (see `Dockerfile`).

## Test

```sh
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest
```

Test discovery and markers (`admin`, `ingestion`) are configured in `pytest.ini`.
