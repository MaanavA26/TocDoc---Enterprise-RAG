# TocDoc developer Makefile — thin wrappers over the exact commands CI runs
# (see .github/workflows/ci.yml). POSIX-make portable: no GNU-only features,
# `help` is the first/default target. Each recipe line runs in its own shell,
# so any target that CI runs from a subdirectory uses `cd <dir> && <cmd>`.
#
# Common targets:
#   make help            list targets
#   make install-qna     install a service/SDK/eval dependency set
#   make lint            ruff check + ruff format --check (matches CI lint job)
#   make format          ruff format (rewrites files)
#   make test            run every test suite
#   make run-qna         start the QnA service locally (uvicorn)

.PHONY: help \
	install-qna install-ingestion install-sdk install-eval \
	lint format \
	test test-qna test-ingestion test-sdk test-eval \
	run-qna run-ingestion

# Lint/format target set, kept identical to the CI lint job.
LINT_PATHS = services/qna services/ingestion clients/python eval

# ---------------------------------------------------------------------------
# help (default) — list targets
# ---------------------------------------------------------------------------
help:
	@echo "TocDoc dev targets:"
	@echo ""
	@echo "  Install (pip):"
	@echo "    install-qna         install QnA service deps + pytest"
	@echo "    install-ingestion   install ingestion service deps + pytest"
	@echo "    install-sdk         install Python SDK (editable, with dev extra)"
	@echo "    install-eval        install RAGAS eval harness deps + pytest"
	@echo ""
	@echo "  Lint / format (ruff):"
	@echo "    lint                ruff check + ruff format --check"
	@echo "    format              ruff format (rewrites files)"
	@echo ""
	@echo "  Test (pytest):"
	@echo "    test                run every suite below"
	@echo "    test-qna            QnA service tests"
	@echo "    test-ingestion      ingestion service tests"
	@echo "    test-sdk            Python SDK tests"
	@echo "    test-eval           eval harness tests (run from repo root)"
	@echo ""
	@echo "  Run (uvicorn):"
	@echo "    run-qna             QnA service on :5500"
	@echo "    run-ingestion       ingestion service on :5501"

# ---------------------------------------------------------------------------
# install — pip per requirements (mirrors the CI test jobs: requirements
# files do not carry pytest, so install it alongside as CI does)
# ---------------------------------------------------------------------------
install-qna:
	python -m pip install --upgrade pip
	python -m pip install -r services/qna/requirements.txt
	python -m pip install pytest pytest-asyncio pytest-cov

install-ingestion:
	python -m pip install --upgrade pip
	python -m pip install -r services/ingestion/requirements.txt
	python -m pip install -r services/ingestion/requirements-dev.txt
	python -m pip install pytest pytest-asyncio pytest-cov

# The [dev] extra pulls pytest; base deps pull httpx + pydantic.
install-sdk:
	python -m pip install --upgrade pip
	python -m pip install -e "clients/python[dev]"

install-eval:
	python -m pip install --upgrade pip
	python -m pip install -r eval/requirements.txt
	python -m pip install pytest pytest-asyncio

# ---------------------------------------------------------------------------
# lint / format — ruff, same paths as the CI lint job
# ---------------------------------------------------------------------------
lint:
	ruff check $(LINT_PATHS)
	ruff format --check $(LINT_PATHS)

format:
	ruff format $(LINT_PATHS)

# ---------------------------------------------------------------------------
# test — pytest. Service + SDK suites run from their own directory (CI uses
# `working-directory:`); the eval suite runs from the repo root so
# eval/tests/conftest.py sets the fake Azure env before import-time validation.
# ---------------------------------------------------------------------------
test: test-qna test-ingestion test-sdk test-eval

test-qna:
	cd services/qna && pytest -q

test-ingestion:
	cd services/ingestion && pytest -q

test-sdk:
	cd clients/python && pytest -q

test-eval:
	pytest eval -q

# ---------------------------------------------------------------------------
# run — uvicorn, matching each service Dockerfile ENTRYPOINT (app:app, from
# the service directory). Workers are a prod concern and omitted here; add
# --reload yourself for autoreload during development.
# ---------------------------------------------------------------------------
run-qna:
	cd services/qna && uvicorn app:app --host 0.0.0.0 --port 5500

run-ingestion:
	cd services/ingestion && uvicorn app:app --host 0.0.0.0 --port 5501
