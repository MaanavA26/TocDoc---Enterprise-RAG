# Codebase Context for Sub-Agents

> **Read this document first** before touching any code in this repo.
> It explains the project structure, conventions, key patterns, and things to avoid.
> It is written for coding agents (Claude, Codex) but is equally useful for new human contributors.

---

## What this project is

TocDoc is a two-service enterprise RAG (Retrieval-Augmented Generation) system.
It lets enterprise clients upload business documents (PDFs) and ask natural-language
questions, receiving grounded, cited answers backed by their own document corpus.

**Deployment model**: installed into a client's Azure resource group.
The client owns all data and compute. TocDoc is a deployable product.

---

## Service map

```
TocDoc - Enterprise RAG/
├── services/
│   ├── ingestion/          Service 1: PDF upload, chunking, Azure Search indexing
│   │   ├── app.py          FastAPI entrypoint (port 5501)
│   │   ├── custom_rag.py   All chunking and indexing logic
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── .env.example
│   │
│   └── qna/                Service 2: query answering
│       ├── app.py          FastAPI entrypoint (port 5500)
│       ├── src/
│       │   ├── clients/    azure_clients.py — Azure SDK client initialization
│       │   ├── config/     config.py — Pydantic Settings
│       │   ├── core/       auth.py, lifecycle.py, logger.py
│       │   ├── llm/        prompts.py — system prompt, rephrase prompt
│       │   ├── pipeline/   qna_pipeline.py — main orchestration
│       │   ├── services/   embedding_service.py, search_service.py,
│       │   │               openai_service.py, text_processor.py
│       │   └── utils/      util.py — citation helpers, history helpers
│       ├── test/
│       │   └── test.py
│       ├── Dockerfile
│       ├── requirements.txt
│       └── .env.example
│
├── docs/
│   ├── productization_backlog/   ← reviewer's original backlog (15 items)
│   └── agent_plan/               ← this folder (planning + scaffolding)
│
├── docker-compose.yml
├── .env.example                  (root, combined reference)
└── README.md
```

---

## Key flows

### Ingestion flow
```
POST /upload_pipeline/docs
  │
  ├── File received (multipart or path)
  ├── Azure Document Intelligence → extract text
  │   - fr_mode="read"   → prebuilt-read + token-window chunking
  │   - fr_mode="layout" → prebuilt-layout + MarkdownHeaderTextSplitter
  ├── AzureOpenAIEmbeddings → embed each chunk (text-embedding-3-small)
  └── Azure Cognitive Search → index chunks with metadata
```

### QnA flow
```
POST /qna/answer
  │
  ├── JWT auth middleware (auth.py)
  ├── Load conversation history from request body
  ├── qna_pipeline.generate_answer()
  │   ├── Rephrase query using LLM (openai_service.rephrase_queries)
  │   ├── Embed rephrased query (embedding_service.get_embedding)
  │   ├── Hybrid search (search_service.perform_search)
  │   │   └── filter: fr_tag eq 'fr_{mode}'  ← MISSING bot_tag (P0-2)
  │   ├── Build prompt with retrieved chunks
  │   ├── LLM generation (openai_service.generate_openai_response)
  │   └── Extract answer + citations (text_processor, util)
  └── Return {answer, citation}
```

---

## Patterns and conventions in use

### Async + thread pool for sync Azure SDKs
Azure SDK calls are synchronous. They are offloaded to a `ThreadPoolExecutor`
using `loop.run_in_executor()`:
```python
loop = asyncio.get_running_loop()   # use get_running_loop(), NOT get_event_loop()
result = await loop.run_in_executor(executor, sync_fn)
```
Do not change this pattern. Do not add `await` to sync SDK calls directly.
Do not use `asyncio.to_thread()` unless you verify it's available in the Python version.

### Pydantic Settings (QnA)
`services/qna/src/config/config.py` uses `pydantic_settings.BaseSettings`.
All config is loaded from environment variables at import time.
Do not do `os.environ.get()` inside business logic — use `from src.config.config import settings`.

### Azure client holder pattern (QnA)
`services/qna/src/clients/azure_clients.py` initializes Azure SDK clients once at startup
and passes them around as an `azure` object:
- `azure.openai_client` → `AzureChatOpenAI`
- `azure.search_client` → `SearchClient`
- `azure.embedding_client` → `AzureOpenAIEmbeddings`

Do not create new SDK clients per request. Pass the `azure` holder through function arguments.

### LangChain integration
The ingestion service uses LangChain:
- `AzureOpenAIEmbeddings` for embedding (from `langchain_openai`)
- `MarkdownHeaderTextSplitter` for layout-mode chunking (from `langchain_text_splitters`)

Current pin: `langchain==0.3.25`. Do not upgrade to 0.4.x — it breaks splitter APIs.
When adding LangGraph, use `langgraph>=0.2.0,<0.3.0` and pin it explicitly.

### Logging
Both services use Python's standard `logging` module.
QnA: `from src.core.logger import logger`
Ingestion: `import logging; logger = logging.getLogger(__name__)`

Do not use `print()` anywhere. Use `logger.info/warning/error`.
All log lines in the QnA pipeline include `[request_id]` prefix for traceability.

### Error handling in pipeline
The QnA pipeline has a top-level try/except that returns an error dict on failure.
**This is a P0-6 known issue** — the error dict is mixed into the success response shape.
When fixing P0-6, replace with proper HTTP error responses and Pydantic models.
Until then, do not add more bare except blocks that swallow errors silently.

---

## Things to avoid

1. **Do not use `asyncio.get_event_loop()`** — it is deprecated in Python 3.10+.
   Use `asyncio.get_running_loop()` inside coroutines.

2. **Do not add module-level mutable state** that holds per-request data.
   The global `bot_queries` in `qna_pipeline.py` is a known bug (P0-3) — do not replicate this pattern.

3. **Do not commit `.env` files** — they are in `.gitignore`. Use `.env.example` for documentation.

4. **Do not use PascalCase env var names** — they are being normalized to UPPER_SNAKE_CASE (P0-7).
   All new env vars must use UPPER_SNAKE_CASE.

5. **Do not leak exception text in client-facing responses** — especially for auth or upstream errors.

6. **Do not create new Azure SDK clients inside request handlers** — expensive and connection-leaking.
   Use the `azure` holder from the lifespan context.

7. **Do not break the existing `/health` endpoint behavior** — health checks must remain ultra-lightweight.

8. **Do not upgrade LangChain to 0.4.x** during P0/P1 work — save that for a dedicated migration PR.

---

## Running the services locally

### Prerequisites
- Python 3.10+
- Azure credentials (fill in `.env` from `.env.example`)
- Docker (optional, for containerized local dev)

### QnA service
```bash
cd services/qna
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5500 --reload
```

### Ingestion service
```bash
cd services/ingestion
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5501 --reload
```

### Running tests (QnA service)
```bash
cd services/qna
pip install pytest pytest-asyncio pytest-cov
pytest test/ -v --cov=src --cov-report=term-missing
```

### Docker Compose (both services)
```bash
# Ensure services/ingestion/.env and services/qna/.env exist
docker compose up --build
```

---

## Index schema (Azure Cognitive Search)

Fields on every indexed chunk:
| Field | Type | Notes |
|-------|------|-------|
| `id` | string (key) | Currently random UUID → will be deterministic after P0-4 |
| `content` | string | Chunk text |
| `content_vector` | Collection(Single) | Embedding (1536 dims, HNSW) |
| `filename` | string | Source file name |
| `filepath` | string | Source file path |
| `section_header` | string | Layout-mode section header (nullable) |
| `fr_tag` | string | "fr_read" or "fr_layout" |
| `bot_tag` | string | Tenant/bot identifier — **MUST be added as a search filter (P0-2)** |

Fields to add in P0-4:
- `document_id` — deterministic document-level hash
- `content_hash` — chunk text hash (for change detection)
- `ingestion_timestamp` — ISO 8601 datetime
- `source_type` — "upload" | "blob" | "sharepoint"
- `source_path` — canonical source identifier

Fields to add in P3-2 / P2-1:
- `page_number` — from Document Intelligence layout output
- `token_count` — real tiktoken count for the chunk

---

## Environment variables reference

See `.env.example` files for the full list. Key variables:

| Variable | Service | Purpose |
|----------|---------|---------|
| `AZURE_OPENAI_ENDPOINT` | Both | Azure OpenAI account URL |
| `AZURE_OPENAI_KEY` | Both | API key |
| `AZURE_OPENAI_EMBEDDING_MODEL` | Both | `text-embedding-3-small` |
| `AZURE_OPENAI_LLM_MODEL` | QnA | `gpt-4o-mini` |
| `AZURE_SEARCH_ENDPOINT` | Both | Azure Cognitive Search URL |
| `AZURE_SEARCH_KEY` | Both | Admin key |
| `INDEX_NAME` | Both | Search index name |
| `DOC_INTELLIGENCE_ENDPOINT` | Ingestion | Document Intelligence URL |
| `DOC_INTELLIGENCE_KEY` | Ingestion | Document Intelligence key |
| `AZURE_KEY_VAULT_NAME` | QnA | Key Vault for secret loading at startup |
| `AZURE_TENANT_ID` | QnA | Azure AD tenant ID |
| `AUDIENCE_ID` | QnA | JWT audience (app registration client ID) |

> Note: QnA currently uses PascalCase names for the above. After P0-7, all names
> will be UPPER_SNAKE_CASE as shown in this table.
