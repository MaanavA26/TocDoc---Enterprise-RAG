# Hermetic end-to-end integration tests

These tests exercise each service's **real** FastAPI application in-process via
Starlette's `TestClient`, with **every Azure dependency mocked at the
service-entry seam**. They run with **no live Azure and no network**, so they are
safe to run in CI on any machine.

## What "end-to-end" means here

Unlike the per-service unit tests (which mount individual routers on a fresh
`FastAPI()`), this suite imports the assembled `app` objects from
`services/qna/app.py` and `services/ingestion/app.py`. That means the **full
middleware stack** is exercised: the JWT auth middleware, the request-ID
correlation middleware (registered outermost), CORS, and the structured-error
exception handlers — the cross-cutting guarantees that only hold on the real app.

## Where the mocks sit

Mocks are placed at the **service boundary**, not at the Azure-SDK edge, so the
real request-handling, validation, auth, tenant-binding, and error-envelope code
all run:

| Boundary | Mock |
| --- | --- |
| QnA JWT / JWKS validation | `validate_token` patched to return claims (or raise `TokenValidationError`) — no JWKS fetch, no RS256 |
| QnA retrieval + LLM | `qna_pipeline.generate_answer` patched to return `{answer, citation}` |
| QnA startup | Key Vault load + `AzureOpenAIHandler` patched so `app.state.azure` is set without Azure |
| Ingestion connector run | `run_connector` patched; RAG instance overridden via `dependency_overrides` |
| Ingestion `/upload` write | `rag_instance.upload` patched per-test |
| Ingestion admin Search reads | `get_admin_service` overridden with a `MagicMock(spec=SearchAdminService)` |

## Coverage

- **QnA** (`test_qna_e2e.py`): `/health`; authed `POST /qna` happy path (answer +
  citations); 401 (missing/invalid token); 403 tenant-binding fail-closed
  (bad bot_tag and missing map); 400 bad `fr_tag` / empty bot list; 422 schema;
  generic 500 that does not leak exception text or secrets.
- **Ingestion** (`test_ingestion_e2e.py`): `/health`; `/upload` requires an admin
  token (401) and succeeds with one (200) / clean 415 for unsupported types;
  admin read endpoints (auth, happy path, 422 bad bot_tag); connector sync
  trigger → run-status round-trip (completed + failed paths), unknown run 404,
  bad source-type / missing-config 400.
- **Cross-cutting** (`test_cross_cutting.py`): structured `{error: {code,
  message, request_id}}` envelope; `X-Request-ID` on every response (success and
  error) matching the body; client-supplied `X-Request-ID` echoed; no secret /
  exception-text leakage in error bodies.

## Running

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r services/qna/requirements.txt
pip install -r services/ingestion/requirements.txt
pip install -r tests/integration/requirements.txt
pytest tests/integration -q
```

The suite imports both real apps, so the service runtime deps must be installed
(see `requirements.txt` for the local dev-proxy caveat on `aiohttp` / `msal`,
which this suite does not need).

## Implementation notes

- Both services define a top-level `app.py`. `conftest.py` loads each via
  `importlib.util.spec_from_file_location` under a unique module name, with the
  service directory on `sys.path`, so the two same-named modules coexist in one
  pytest process.
- Each service mounts under a non-empty `root_path` (`/qna`,
  `/upload_pipeline`), so Starlette routes against the full prefixed path. The
  `TestClient` request method is wrapped to prepend that prefix, letting tests
  call bare route paths (`/qna`, `/health`, `/admin/...`).
- Required env vars are set before the apps are imported (QnA validates them at
  import time). All values are throwaway — nothing reaches Azure.
