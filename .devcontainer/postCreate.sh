#!/usr/bin/env bash
# Dev container provisioning — runs once after the container is created.
# Idempotent enough to re-run by hand if needed.
set -euo pipefail

echo "==> TocDoc dev container: installing toolchain (Python $(python --version 2>&1))"

# CI-pinned ruff first, so the editor (ruff.importStrategy=fromEnvironment) and
# `make lint` use the exact version CI runs (.github/workflows/ci.yml RUFF_VERSION).
python -m pip install --upgrade pip
python -m pip install "ruff==0.15.7"

# Service + SDK deps via the Makefile install targets. These three share the
# langchain 1.x cascade and co-resolve cleanly in one environment.
make install-qna
make install-ingestion
make install-sdk

# Eval harness: pinned to an INCOMPATIBLE langchain 0.3.x / pandas 2.x stack
# (see eval/requirements.txt). It MUST live in its own venv — installing it into
# the main environment would clobber the services' langchain/openai/pandas/numpy.
# We therefore do NOT run `make install-eval` (that pips into the active env);
# we build the dedicated .venv-eval the eval harness expects instead.
echo "==> Building isolated eval venv (.venv-eval) for the langchain 0.3.x stack"
python -m venv .venv-eval
.venv-eval/bin/python -m pip install --upgrade pip
.venv-eval/bin/python -m pip install -r eval/requirements.txt

cat <<'EOF'

==> Dev container ready.

  Lint / format : make lint   |  make format
  Tests         : make test   (or test-qna / test-ingestion / test-sdk / test-eval)
  Run services  : make run-qna (:5500)  |  make run-ingestion (:5501)
  Eval harness  : . .venv-eval/bin/activate && pytest eval -q

  Before running a service against real Azure, create per-service .env files:
    cp services/qna/.env.example       services/qna/.env
    cp services/ingestion/.env.example services/ingestion/.env
  (Tests are hermetic and need no Azure account — see docs/LOCAL_DEV.md.)
EOF
