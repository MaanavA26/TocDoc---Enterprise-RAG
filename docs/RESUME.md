# Picking TocDoc back up

> A resume-from-cold guide written as the project enters dormancy (2026-06-05).
> Read this first, then `docs/agent_plan/00_MASTER_TRACKER.md` for the per-item
> status and `docs/ARCHITECTURE.md` for the system tour.

## Status in one paragraph

TocDoc is a multi-tenant, document-grounded RAG platform for enterprise Q&A over
PDF corpora, deployed **into each client's own Azure subscription** (Azure
OpenAI + AI Search + Document Intelligence + Key Vault), split into two stateless
FastAPI services — **ingestion** (parse → chunk → embed → index) and **QnA**
(authenticate → retrieve → rephrase → answer with citations), with `bot_tag`
tenant isolation enforced at the retrieval layer. Maturity: **P0 (security
hardening) and P1 (enterprise completeness) are fully shipped**; **P2 (retrieval
quality / packaging) is underway** — semantic reranking and the `page_citations`
contract are shipped, page-level ingestion is pending; **P3 (the LangGraph
agentic layer) is scaffolded but inert behind a default-OFF flag**; **P4 is
partial** — the Python SDK and RAGAS eval harness shipped in-repo, the Teams bot
is designed-not-built, Helm is not built. The repo is BSL 1.1 source-available
and ready to go public pending the open decisions below.

## What's behind flags / not live

- **`QNA_AGENT_ENABLED` — default-OFF.** The P3 LangGraph scaffold (PR #74) and
  the structured-output router/classifier (PR #79) are merged to `main` but
  **inert**: when the flag is unset/falsy the `/qna` request path is
  byte-for-byte the existing pipeline. Flipping the flag is **not** a routine
  config change — it requires architect sign-off on the P3 ADR (see Open
  decisions). The flag is read in `services/qna/src/config/config.py`
  (`is_agent_enabled()`); the dark seam is in `services/qna/app.py`.
- **Semantic reranking** is config-gated on `AZURE_SEARCH_SEMANTIC_CONFIG`; with
  it unset, retrieval is plain hybrid (BM25 + vector). Safe either way — it
  falls back gracefully on Search tiers that don't support L2 rerank.
- **`page_citations`** ships as a backward-compatible contract (PR #76) that is
  byte-identical to the old response until ingestion actually populates
  `page_number` — which it does not yet.
- **pip-audit** runs in CI but is **report-only** (does not fail the build), and
  the coverage number is computed but **not threshold-gated**.

## Open decisions blocking the next builds

These are the things a future session needs a human to decide before the next
units of work can start.

1. **P3 architect sign-off.** `docs/architect_phase_2/07_P3_LANGGRAPH_ADR.md`
   still needs explicit architect approval. Sign-off is the gate that (a) lets
   `QNA_AGENT_ENABLED` be turned on and (b) unblocks P3 PR2+ — map-reduce
   summarizer, ReAct multi-hop, self-critique verifier, Cosmos-backed memory,
   SSE streaming. Until then the agentic layer stays dark.
2. **Page-citation reindex window + read-mode content-format spike.** Per
   `docs/architect_phase_2/09_P2_1_PAGE_CITATIONS_ADR.md`, page-level ingestion
   needs `page_number` derived during chunking (a read-mode change), which
   requires a **full reindex** and a spike on the Document Intelligence
   read-mode content format. Schedule the reindex window and run the spike
   before building page-level ingestion; the response contract is already in
   place and waiting.
3. **Governance overlap — merge exactly one.** Two open PRs both add a
   `CODEOWNERS` file and will collide: **#64** (owner's bot-governance baseline:
   CODEOWNERS + CodeRabbit + CodeQL) and **#75** (CODEOWNERS + PR/issue templates
   + CONTRIBUTING). Pick one as the source of truth, fold any wanted pieces of
   the other into it, and close the loser. Do not merge both.
4. **Confirm BSL legal parameters before going public.** `LICENSE` (BSL 1.1, PR
   #72) still carries placeholder values: **Licensor** is `TocDoc` with a "replace
   with the legal entity" note, and **Change Date** is `2030-06-05` with a
   "confirm/adjust" note. Both must be confirmed by a human before the repo is
   truly public.

## Dependabot triage (so the open dep PRs aren't a mystery)

Dependabot is live (PR #35) and has opened roughly **thirty** dependency PRs plus
the two governance PRs above. They fall into two buckets. **Always check the
live CI status at merge time** — Dependabot rebases shift PRs between buckets,
and they conflict on the same `requirements.txt`, so merge one per file and let
Dependabot rebase the rest.

- **Green CI → safe to merge.** Single-package or grouped bumps whose checks
  pass can be bulk-merged (one per `requirements.txt` at a time). Examples that
  have already merged green: GitHub Actions group (#37) and the SDK `pydantic`
  bump (#38). Other green-passing groups have included aiohttp, pillow, the
  azure groups, attrs, regex, and idna.
- **Red CI → the deferred coordinated cascade. Do NOT merge piecemeal.** These
  break tests and must each land as a coordinated, runtime-validated upgrade PR
  (and are the reason pip-audit stays report-only). The families:
  - **LangChain 1.x** — `langchain` / `langchain-core` / `langchain-community` /
    `langchain-text-splitters` / `langsmith`
  - **FastAPI / Starlette 1.x stack** (`fastapi-stack` group, plus standalone
    `starlette` bumps)
  - **`langgraph` 1.0rc**, **`openai` 2.x**, **`pypdf` 6**
  - **`pandas` 3**, **`marshmallow` 4**, **`cryptography` 48**
  - **Docker base image `python:3.10-slim` → `3.14-slim`** (both services)

  Treat the list by **family**, not by a fixed PR-number color map: a given PR
  number can flip green/red after a rebase. The rule is what's stable — major
  version jumps in these ecosystems need a coordinated upgrade with the test
  suite green before merge.

## Branch-protection gotcha

`main` requires a review from a **code owner**. The current CODEOWNERS resolves
to the repo owner, who also authors PRs via their own `gh` credential — so GitHub
**blocks self-approval** and owner-authored PRs cannot satisfy the rule normally.
Today these are merged with an `--admin` bypass. Before relying on the
protection long-term, either add a documented admin/bot bypass exception or bring
in a **second reviewer** so the gate is real rather than routinely overridden.

## How to run tests locally

Each component has its own dependencies; install per component and run `pytest`
from that component's directory.

```bash
# QnA service
pip install -r services/qna/requirements.txt
pytest services/qna/test

# Ingestion service
pip install -r services/ingestion/requirements.txt -r services/ingestion/requirements-dev.txt
pytest services/ingestion/test

# Python SDK (light: httpx + pydantic, mocked HTTP)
pip install -e clients/python
pytest clients/python/tests

# RAGAS eval harness (heavy: ragas + datasets + qna deps; mocked, hard gate)
pip install -r eval/requirements.txt
pytest eval/tests
```

- The **agentic path** additionally needs `langgraph` (pinned in
  `services/qna/requirements.txt`); the agent tests run regardless because the
  scaffold is import-safe with the flag off.
- CI is defined in **`.github/workflows/ci.yml`**: jobs are `lint (ruff)`,
  `security (bandit + pip-audit)`, `bicep`, `shellcheck`, `test` (matrix over
  qna + ingestion), `test (sdk)`, and `test (eval)`. Dependabot config is in
  `.github/dependabot.yml`.

## Pointers

- **Architecture tour:** `docs/ARCHITECTURE.md` (request flow, ingestion flow,
  multi-tenancy, operability, supply chain, roadmap). *Note: a couple of its
  roadmap lines predate PRs #36 and #73 and say "no standalone ADR file" for
  page-citations / Teams — both ADRs now exist; see below. Refresh out of scope
  for this snapshot.*
- **Live status board:** `docs/agent_plan/00_MASTER_TRACKER.md`.
- **ADRs / specs** (`docs/architect_phase_2/`):
  - `07_P3_LANGGRAPH_ADR.md` — agentic layer (needs sign-off)
  - `08_P1_3_CONNECTORS_ADR.md` — Blob + SharePoint connectors (shipped)
  - `09_P2_1_PAGE_CITATIONS_ADR.md` — page-level citations (reindex pending)
  - `10_P4_1_TEAMS_BOT_ADR.md` — Teams bot (designed, not built)
  - `00_PHASE_2_EXECUTION_PLAN.md`, `01`–`06` — Phase 2 specs and decisions
- **Product packaging:** `docs/PRODUCT_TIERS.md`.
- **Refreshed roadmap:** `docs/agent_plan/07_P2_P4_REFRESHED_PLAN.md`.
