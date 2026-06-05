# QnA Service

FastAPI service that answers natural-language questions over a corpus indexed in
Azure AI Search. It embeds the incoming query, runs hybrid (keyword + vector)
retrieval with optional semantic reranking, and asks Azure OpenAI to compose a
grounded answer with citations. Tenant isolation is enforced on every request via
a `bot_tag` filter.

## Key modules

| Path | Responsibility |
| --- | --- |
| `app.py` | FastAPI app: wiring, middleware, and the `/health`, `POST /qna`, and `/` routes. |
| `src/core/auth.py` | `AuthUtils.auth_middleware` — request authentication / token handling. |
| `src/services/search_service.py` | Hybrid (text + vector KNN) retrieval against Azure AI Search, with an optional L2 semantic rerank that falls back to a plain hybrid query when the Search tier does not support semantic ranking. |
| `src/pipeline/qna_pipeline.py` | `generate_answer` — orchestrates embed → retrieve → generate and returns the `{answer, citation}` contract. |
| `src/core/errors.py` | Structured error contract: `raise_api_error`, `ApiErrorCode`, and the exception handlers that envelope every 4xx/5xx with a stable `code` and `request_id`. |
| `src/core/observability.py` | `RequestIDMiddleware` and the `log_event` structured-logging helper; threads a correlation `request_id` through the request and pipeline stages. |
| `src/core/responses.py` | Response contract models (`QnASuccessResponse`, `CitationMap`). |
| `src/agents/` | Default-OFF LangGraph agentic layer (classifier → route → verify). Enabled per-request via the `QNA_AGENT_ENABLED` flag; when ON it returns the same `{answer, citation}` shape as the direct pipeline. |

Supporting modules live under `src/clients/` (Azure client construction),
`src/config/` (settings and feature flags), `src/llm/`, and `src/utils/`.

## Endpoints

- `GET /health` — liveness/readiness probe.
- `POST /qna` — answer a question. Accepts a payload with conversation history,
  `bot_tag` (tenant), and `fr_tag`; returns the `QnASuccessResponse` contract.
- `GET /` — service metadata.

## Run

```sh
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5500
```

The service listens on port `5500` (see `Dockerfile`).

## Test

```sh
pip install -r requirements.txt
pytest
```

Test discovery and markers (`auth`, `pipeline`, `services`) are configured in
`pytest.ini`.

## Configuration

All Azure endpoints, model deployments, feature flags (including
`QNA_AGENT_ENABLED`), and search settings are supplied via environment variables.
See [`../../docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md) for the full
reference.
