# Picking TocDoc back up

> A resume-from-cold guide. Last refreshed 2026-06-05 after a large hardening +
> modernization pass. Read this first, then `docs/agent_plan/00_MASTER_TRACKER.md`
> for per-item status and `docs/ARCHITECTURE.md` for the system tour.

## Status in one paragraph

TocDoc is a multi-tenant, document-grounded RAG platform for enterprise Q&A,
deployed **into each client's own Azure subscription** (Azure OpenAI + AI Search
+ Document Intelligence + Key Vault), split into two stateless FastAPI services —
**ingestion** (parse → chunk → embed → index) and **QnA** (authenticate →
retrieve → rephrase → answer with citations), with `bot_tag` tenant isolation
enforced at the retrieval layer. Maturity: **P0 and P1 fully shipped**; **P2**
— semantic reranking + the `page_citations` contract shipped, page-level
ingestion still pending; **P3 (LangGraph agentic layer)** — scaffold, router,
and map-reduce node shipped (ReAct + self-critique nodes added more recently),
all **dark behind default-OFF flags** pending sign-off; **P4** — Python SDK
(+CLI, +optional LangChain retriever), RAGAS eval (+continuous-eval), **Teams
bot**, and a **Helm chart** all shipped (a Terraform module + an admin web UI
were the most recent additions). The runtime is now **Python 3.12** and both
services run **langchain 1.x** (the old deferred dependency cascade is resolved).
A full security audit was run and **all 36 findings (1 critical, 6 high, 8
medium, 21 low) are fixed and on green `main`.** Repo is BSL 1.1 source-available,
ready to go public pending the open decisions below.

## Operator-critical default behaviors (these CHANGED — read before deploying)

The hardening pass changed three shipped defaults toward fail-closed. A naive
deploy that ignores them will refuse to serve or reject calls — by design:

- **Tenant binding is ON by default.** `QNA_ENFORCE_TENANT_BINDING` now defaults
  true. The QnA service requires `QNA_TENANT_BOT_TAG_MAP` (JSON `{tid: [bot_tag,…]}`)
  and **fails closed** (403, no search) if a request's `bot_tag` isn't allowed
  for the token's tenant, or if the map is missing/unparseable. Single-workspace
  deployments must either configure the map or explicitly set the flag false.
  Closes the within-tenant workspace-isolation gap (audit H1 / threat-model R1).
- **Ingestion `/upload` now requires `X-Admin-Token`** (same dependency as the
  admin API). The `bot_tag` query param is pattern-validated and OData-escaped
  (closes the critical unauthenticated OData-injection → mass-delete, audit C1).
- **Ingestion Container App ingress is internal by default** (`ingestionIngressExternal`
  / `ingestion_ingress_external` = false). QnA stays external (JWT-authed). Flip
  the param only behind an authenticated, rate-limited ingress.

`/qna` and `/upload` also now enforce app-level rate limiting (429 + `Retry-After`).

## What's behind flags / not live

- **`QNA_AGENT_ENABLED` — default-OFF**, plus per-node flags `QNA_AGENT_MAP_REDUCE`,
  `QNA_AGENT_REACT`, `QNA_AGENT_VERIFY` (all default-OFF). With the master flag
  off, the `/qna` path is byte-for-byte the existing pipeline. Flipping it on is
  **not** routine — it needs architect sign-off on the P3 ADR. Flags live in
  `services/qna/src/config/config.py`; the dark seam routes through
  `src/agents/router.py`.
- **Semantic reranking** is config-gated on `AZURE_SEARCH_SEMANTIC_CONFIG`; unset
  ⇒ plain hybrid (BM25 + vector). Falls back gracefully on tiers without L2 rerank.
- **`page_citations`** is a backward-compatible contract (PR #76), byte-identical
  to the old response until ingestion populates `page_number` — which it does not
  yet (see open decision below).

## CI gates (now enforced)

- **pip-audit is a HARD gate** (no longer report-only), with a documented
  `--ignore-vuln` allowlist for two `aiohttp` advisories — these are no-ops now
  that `aiohttp==3.14.0` is pinned; the allowlist can be dropped as cleanup.
- **Coverage floor `--cov-fail-under=60`** on the qna + ingestion suites (a
  regression floor to ratchet upward, not a target).
- **Two CodeQL setups run.** The advanced `codeql.yml` (`analyze (python)`) is
  the real one and passes. GitHub's **default** code-scanning setup also runs and
  **fails in ~3 s on every PR** (it conflicts with the advanced setup). It's
  redundant and harmless — **disable the default setup** in repo *Settings → Code
  security → Code scanning* (it can't be disabled via API). Until then, `--admin`
  merges step past that one check; the real analysis still gates.

## Open decisions blocking the next builds

1. **P3 architect sign-off.** `docs/architect_phase_2/07_P3_LANGGRAPH_ADR.md`
   still needs approval before `QNA_AGENT_ENABLED` (and the per-node flags) can be
   turned on. The nodes (map-reduce, ReAct, self-critique verifier) are built and
   dark; enabling is a behavior + cost change, hence the gate.
2. **Page-citation reindex window + read-mode content-format spike**
   (`09_P2_1_PAGE_CITATIONS_ADR.md`). Page-level ingestion needs `page_number`
   derived during chunking → a **full reindex** + a Document Intelligence
   read-mode spike. The response contract is already in place and waiting.
3. **Confirm BSL legal parameters before going public.** `LICENSE` (BSL 1.1)
   still has placeholders: **Licensor** = `TocDoc` ("replace with the legal
   entity") and **Change Date** = `2030-06-05` ("confirm/adjust"). A human must
   confirm both.
4. **THREAT_MODEL exposure call (PR #90, held).** A code-grounded security doc
   exists but enumerates residual risks; on a public repo that reads as an
   attacker checklist. Decide: merge as-is / keep internal / trim to
   controls-in-place. (Most of its original residual risks are now FIXED, so a
   refreshed version is far safer to publish.)

> Resolved since the last snapshot: the Python-3.12 runtime bump + the langchain/
> fastapi/pandas/openai dependency cascade (no longer deferred), and the
> governance overlap (#64 + #75 both merged — CODEOWNERS, CodeRabbit, CodeQL,
> templates, CONTRIBUTING are all in).

## Dependabot

Fully reconciled — **0 open Dependabot PRs** at last pass. The old "deferred
cascade" is resolved: the runtime is on Python 3.12 and both services on
langchain 1.x, so pandas-3 / numpy / scipy / openai-2 / langchain-1.x all
installed and validated. When Dependabot opens new PRs: green CI ⇒ merge (one per
`requirements.txt` at a time, let it rebase the rest); anything requiring a major
coordinated jump ⇒ do it as a runtime-validated upgrade PR with the suite green.
Note the dev pip proxy mirror can lag public PyPI (it once froze `aiohttp` to
3.13.5) — **CI (public PyPI) is the source of truth, not a local proxy venv.**

## Branch-protection gotcha

`main` requires a code-owner review. CODEOWNERS (added via #64) resolves to the
repo owner, who also authors PRs — so GitHub blocks self-approval and
owner-authored PRs are merged with an `--admin` bypass. For a real gate long-term,
add a documented admin/bot bypass exception or bring in a second reviewer.

## How to run tests locally

Each component has its own deps; install per component and run `pytest`. **Use
Python 3.12.**

```bash
# QnA service
pip install -r services/qna/requirements.txt
pytest services/qna/test

# Ingestion service
pip install -r services/ingestion/requirements.txt -r services/ingestion/requirements-dev.txt
pytest services/ingestion/test

# Python SDK (core is httpx + pydantic only; LangChain integration is an extra)
pip install -e clients/python            # core
pip install -e "clients/python[langchain]"  # + TocDocRetriever
pytest clients/python/tests

# RAGAS eval harness (decoupled: its own ragas + langchain 0.3.x pins)
pip install -r eval/requirements.txt
pytest eval/tests
```

- CI is **`.github/workflows/ci.yml`**: `lint (ruff)`, `security (bandit +
  pip-audit, hard gate)`, `bicep build`, `helm lint`, `shellcheck`, `test`
  (matrix qna + ingestion, with the coverage floor), `test (sdk)`,
  `test (teams-bot)`, `test (eval)`, `analyze (python)` (CodeQL). Dependabot
  config in `.github/dependabot.yml`.
- **eval is decoupled** from the services: it pins its own ragas + langchain
  0.3.x stack (ragas can't run on langchain 1.x yet — it imports a removed
  `ChatVertexAI`). Don't re-add the `-r ../services/qna/requirements.txt` include.

## Pointers

- **Architecture tour:** `docs/ARCHITECTURE.md`. **API / config / ops:**
  `docs/API.md`, `docs/CONFIGURATION.md`, `docs/OPERATIONS.md`, `docs/LOCAL_DEV.md`.
- **Docs index:** `docs/README.md`. **Changelog:** `CHANGELOG.md`.
- **Live status board:** `docs/agent_plan/00_MASTER_TRACKER.md`.
- **ADRs / specs** (`docs/architect_phase_2/`):
  - `07_P3_LANGGRAPH_ADR.md` — agentic layer (needs sign-off to enable)
  - `08_P1_3_CONNECTORS_ADR.md` — Blob + SharePoint connectors (shipped)
  - `09_P2_1_PAGE_CITATIONS_ADR.md` — page-level citations (reindex pending)
  - `10_P4_1_TEAMS_BOT_ADR.md` — Teams bot (**shipped**, `services/teams-bot/`)
  - `00_PHASE_2_EXECUTION_PLAN.md`, `01`–`06` — Phase 2 specs and decisions
- **Packaging:** `docs/PRODUCT_TIERS.md`. **Deploy:** `infra/main.bicep` (Bicep)
  or `infra/terraform/` (Terraform); `charts/tocdoc/` (Helm).
- **Security:** the full audit report from the hardening pass was kept **out of
  the repo** (a live-findings doc is an attacker checklist on a public repo); all
  36 findings are fixed in `main`. `docs/security/CODEQL_TRIAGE.md` is the
  committed scan triage.
