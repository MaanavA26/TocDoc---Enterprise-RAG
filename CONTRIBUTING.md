# Contributing to TocDoc

Thanks for your interest in improving TocDoc. This guide covers the essentials.

## Development setup

The repository is organized as independent services, each with its own
dependencies. Install the requirements for the service you are working on:

```bash
# Q&A service
pip install -r services/qna/requirements.txt

# Ingestion service
pip install -r services/ingestion/requirements.txt
```

> **Intel-Mac note:** `cryptography` ships **arm64-only** macOS wheels, so an Intel
> (x86_64) Mac builds it from source (needs a Rust toolchain + OpenSSL). Apple
> Silicon, Linux, and the container images are unaffected. See
> [`docs/LOCAL_DEV.md`](docs/LOCAL_DEV.md) → Prerequisites for the workaround.

Install the CI tooling used for local checks:

```bash
pip install ruff bandit pip-audit pytest
```

Run the same checks CI runs before opening a PR:

```bash
ruff check .          # lint
ruff format --check . # formatting
pytest                # tests for the affected service(s)
```

## Commit conventions

Use Conventional Commit prefixes. The prefixes this repository uses are:

- `feat` — new functionality
- `fix` — bug fix
- `docs` — documentation only
- `chore` — tooling / housekeeping
- `deps` — dependency change

## Pull requests

- **One logical change per PR.** Keep changes focused and reviewable.
- PRs must pass the CI gate before merge. The gate runs **ruff** (lint +
  format), **bandit** (security scan), **pip-audit** (dependency CVE check),
  and **pytest**.
- Fill out the PR template, including how you verified the change and whether
  it is backward-compatible.

## Reporting security issues

Do not open a public issue for vulnerabilities. Report them privately via the
repository's Security Advisories tab (see `SECURITY.md`).

## Licensing of contributions

TocDoc is **source-available** software licensed under the **Business Source
License 1.1 (BSL 1.1)**. It is not an open-source (e.g. Apache or MIT) license.
By submitting a contribution, you agree that it is provided under, and may be
distributed under, the repository's BSL 1.1 license terms.
