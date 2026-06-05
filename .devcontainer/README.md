# Dev Container

A ready-to-code environment for TocDoc, usable two ways:

- **VS Code** — install the *Dev Containers* extension, open the repo, and run
  **Dev Containers: Reopen in Container**.
- **GitHub Codespaces** — *Code → Codespaces → Create codespace*.

It targets **Python 3.12**, matching CI (`.github/workflows/ci.yml`
`PYTHON_VERSION`) and the service runtime image, so what passes locally matches
what passes in CI.

## What it sets up

| | |
| --- | --- |
| Base image | `mcr.microsoft.com/devcontainers/python:3.12` (prebuilt; no Dockerfile) |
| Linter / formatter | `ruff==0.15.7` — the exact CI pin (`RUFF_VERSION`) |
| Test runner | `pytest` + `pytest-asyncio` (+ `pytest-cov`), as CI installs them |
| Deps installed | QnA + Ingestion service deps, the Python SDK (editable), and the eval harness |
| Forwarded ports | `5500` (QnA), `5501` (Ingestion) |
| Extensions | Python, Ruff, Docker, YAML, Bicep |

Provisioning runs from [`postCreate.sh`](./postCreate.sh) via
`postCreateCommand`. It reuses the repo `Makefile` install targets
(`install-qna`, `install-ingestion`, `install-sdk`) so the dependency sets stay
in lockstep with CI.

## Why the eval harness gets its own venv

The eval harness (`eval/requirements.txt`) is **intentionally pinned to a
different, incompatible dependency stack** from the services: the langchain
0.3.x family (plus pandas 2.x / numpy 2.2.x) because `ragas` cannot yet run on
the langchain 1.x cascade the services use. Installing it into the main
environment would downgrade and break the services' `langchain` / `openai` /
`pandas` / `numpy`.

So `postCreate.sh` deliberately **does not** call `make install-eval` (that
target pips into the active environment). Instead it builds the dedicated
`.venv-eval` virtualenv the harness expects:

```bash
. .venv-eval/bin/activate
pytest eval -q
deactivate
```

This is the one place the container diverges from the Makefile install targets,
and it is on purpose.

## Common commands

```bash
make lint          # ruff check + ruff format --check  (matches CI lint job)
make format        # ruff format (rewrites files)
make test          # all four suites (qna, ingestion, sdk, eval)
make run-qna       # QnA service on :5500
make run-ingestion # Ingestion service on :5501
```

VS Code's Test Explorer is wired for pytest but, because each suite has its own
`pytest.ini` in its own directory, single-root discovery is pointed at
`services/qna`. Use `make test` for the full set.

## Secrets / `.env`

No secrets are baked into the container. Both services read configuration from a
per-service `.env`; create them from the checked-in placeholder examples when
you need to serve real (Azure-backed) requests:

```bash
cp services/qna/.env.example       services/qna/.env
cp services/ingestion/.env.example services/ingestion/.env
```

The test suites are hermetic and need **no** Azure account or network access.
See [`docs/LOCAL_DEV.md`](../docs/LOCAL_DEV.md) for the full local-dev guide.
