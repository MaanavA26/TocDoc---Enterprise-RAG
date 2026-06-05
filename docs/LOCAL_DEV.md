# Local Development Quickstart

This guide gets the two TocDoc services — **Ingestion** and **QnA** — running on
your machine, either directly with `uvicorn` or via Docker Compose, and shows how
to run the test suites.

> **Azure dependency.** Both services are thin RAG layers over Azure OpenAI,
> Azure AI Search, and (Ingestion only) Azure Document Intelligence. Serving a
> **real** request requires live Azure resources and valid credentials. The
> **test suites do not** — they are hermetic: each test seeds fake environment
> values and mocks the Azure clients, so no network or Azure account is needed to
> run `pytest`. See [Running the test suites](#running-the-test-suites).

---

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.10 | Matches the container base image (`python:3.10-slim`). |
| pip / venv | bundled with Python | A per-service virtualenv is recommended. |
| Docker + Docker Compose | 24.x / v2 | Only needed for the Compose workflow. |
| Azure resources | — | Only needed to serve real requests, not to run tests. |

The two services have **separate** dependency sets and are run independently:

```text
services/
├── ingestion/   # document upload + parse + embed + index  (port 5501)
└── qna/         # retrieval + answer generation            (port 5500)
```

---

## Configuring `.env`

Each service reads its configuration from a `.env` file in its own directory.
Every service ships a checked-in `.env.example` containing **placeholders only**
(no real values). Treat these per-service files as the source of truth:

```bash
# from the repository root
cp services/ingestion/.env.example services/ingestion/.env
cp services/qna/.env.example       services/qna/.env
```

Then open each `.env` and replace the `<...>` placeholders with your own values.
The variable names follow Azure SDK conventions and use `UPPER_SNAKE_CASE`
throughout. The QnA service requires its core Azure variables to be present at
process start — see the note below.

> `.env` files are git-ignored and must never be committed. Keep secrets out of
> the repo; for shared/deployed environments these values come from Azure Key
> Vault rather than a file.

### Required variables at a glance

Both services need Azure OpenAI and Azure AI Search settings. Ingestion
additionally needs Azure Document Intelligence. The full, annotated list (with
optional Key Vault, CORS, logging, and admin-token settings) lives in each
service's `.env.example`; below is the minimum to boot.

**Ingestion** (`services/ingestion/.env`):

```dotenv
AZURE_OPENAI_ENDPOINT=https://<your-openai-resource>.openai.azure.com/
AZURE_OPENAI_KEY=<your-azure-openai-api-key>
AZURE_OPENAI_VERSION=2024-02-01
AZURE_OPENAI_EMBEDDING_MODEL=text-embedding-3-small
AZURE_SEARCH_ENDPOINT=https://<your-search-resource>.search.windows.net
AZURE_SEARCH_KEY=<your-azure-search-admin-key>
INDEX_NAME=<your-index-name>
DOC_INTELLIGENCE_ENDPOINT=https://<your-doc-intelligence-resource>.cognitiveservices.azure.com/
DOC_INTELLIGENCE_KEY=<your-doc-intelligence-key>
```

**QnA** (`services/qna/.env`):

```dotenv
AZURE_OPENAI_ENDPOINT=https://<your-openai-resource>.openai.azure.com/
AZURE_OPENAI_KEY=<your-azure-openai-api-key>
AZURE_OPENAI_VERSION=2024-02-01
AZURE_OPENAI_EMBEDDING_MODEL=text-embedding-3-small
AZURE_SEARCH_ENDPOINT=https://<your-search-resource>.search.windows.net
AZURE_SEARCH_KEY=<your-azure-search-admin-key>
INDEX_NAME=<your-index-name>
```

> **Boot-time validation (QnA).** The QnA config module validates its required
> Azure variables when the app is imported. If any are missing the process exits
> with `Missing required environment variable: <NAME>` before it serves a single
> request — so populate `.env` (or export the variables) before starting it.
> This is exactly why the tests inject fake values up front (see below).

---

## Running a service locally (uvicorn)

Run each service from its own directory in its own virtualenv. Both expose their
app as `app:app`.

### Ingestion

```bash
cd services/ingestion
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5501 --reload
```

### QnA

```bash
cd services/qna
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5500 --reload
```

`--reload` runs a single auto-reloading worker, which is convenient for local
iteration. (The container entrypoint instead runs `--workers ${UVICORN_WORKERS}`;
`--reload` and multiple workers are mutually exclusive, so do not combine them.)

> The QnA `Dockerfile` installs the Microsoft ODBC driver (`msodbcsql18`). Bare
> uvicorn runs do not need it unless your local code path actually hits SQL; the
> Compose workflow below covers it for you.

Once running:

| | URL |
| --- | --- |
| Ingestion API docs | http://localhost:5501/upload_pipeline/docs |
| Ingestion health | http://localhost:5501/upload_pipeline/health |
| QnA API docs | http://localhost:5500/qna/docs |
| QnA health | http://localhost:5500/qna/health |

---

## Running via Docker Compose

The root `docker-compose.yml` builds and runs both services together. It wires
each container to its per-service `.env` via `env_file` and defines container
healthchecks against the `/health` endpoints.

1. Create both `.env` files as described in
   [Configuring `.env`](#configuring-env).
2. Build and start:

   ```bash
   docker compose up --build          # foreground, streams logs
   docker compose up --build -d       # detached
   ```

3. Inspect and stop:

   ```bash
   docker compose ps                  # see status + health
   docker compose logs -f             # follow logs
   docker compose down                # stop and remove containers
   ```

Endpoints are the same as the local-uvicorn table above
(`localhost:5501` / `localhost:5500`).

---

## Running the test suites

The suites are **hermetic** — they seed fake Azure settings and mock the Azure
clients, so they run fully offline with **no Azure account or network access**.
Each service has its own `pytest.ini` (test path `test/`, async mode auto).

Run each service's suite from its own directory:

```bash
# Ingestion
cd services/ingestion
pip install -r requirements.txt
pytest

# QnA
cd services/qna
pip install -r requirements.txt
pytest
```

Useful flags:

```bash
pytest -vv                 # verbose
pytest test/test_auth.py   # a single file
pytest -k semantic         # match by name
```

If you have not installed dev tooling separately, the linters used in CI can be
run from the repo root once their packages are available:

```bash
ruff check .
bandit -c pyproject.toml -r services
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `Missing required environment variable: <NAME>` on startup | A required Azure variable is unset. Populate the service `.env` (or export it) before launching. |
| Container marked `unhealthy` in `docker compose ps` | The service failed to boot or the `/health` endpoint isn't reachable yet — check `docker compose logs <service>`; healthchecks allow a short start period. |
| Real requests fail with auth/connection errors but tests pass | Tests are mocked; real traffic needs valid, reachable Azure OpenAI / AI Search / Document Intelligence resources and credentials. |
| `--reload` won't start alongside multiple workers | Use `--reload` (single worker) for local dev; multi-worker is for the container entrypoint only. |
