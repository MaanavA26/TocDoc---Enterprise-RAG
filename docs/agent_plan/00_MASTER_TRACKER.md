# TocDoc — Master Plan & Agent Tracker

> **Purpose of this folder:** This is the primary planning surface for all implementation work on TocDoc.
> It is designed to be used by both human contributors and coding sub-agents (Codex, Claude).
> Every task is tied to a backlog item, a phase, specific source files, and clear acceptance criteria.
> Sub-agents should always start here, then navigate to the relevant phase document.
>
> **Tracker discipline:** entries reflect work that has been **merged to `main`**. Open PRs and in-flight workstreams are tracked through their PRs and through the spec files under `docs/architect_phase_2/`, not here. Update this file as a follow-up `chore(docs)` PR after a workstream merges — never list speculative status.

---

## Current codebase state (as of 2026-06-05)

> **Project status: entering dormancy.** For a resume-from-cold guide — open decisions, what's behind flags, Dependabot triage, how to run tests — see [`docs/RESUME.md`](../RESUME.md).

| Service | Entry point | Key concern |
|---------|-------------|-------------|
| `services/ingestion` | `app.py` → `custom_rag.py` | PDF ingestion, chunking, Azure Search indexing |
| `services/qna` | `app.py` → `src/pipeline/qna_pipeline.py` | Hybrid retrieval, rephrasal, LLM answer generation |

All 8 original P0 blockers have shipped, and **all of P1 (5/5) is shipped.** The production CI gate (ruff · bandit · pip-audit · bicep · shellcheck · pytest+coverage) is on `main` (PR #13), as is deployment validation (PR #14) and a `/health` liveness-probe auth hotfix (PR #17). P1 delivered: pipeline-stage observability (PR #22), destructive admin endpoints (PR #21), connector ingestion — Blob + core (PR #24), SharePoint + operator sync trigger (PR #26), and persisted connector run status + `run_id` correlation (PR #32).

**P2 is underway:** config-gated semantic reranking (PR #25), the typed `CitationMap` success contract (PR #28), the backward-compatible `page_citations` contract + retrieval groundwork (PR #76, byte-identical until `page_number` is populated), and the packaging-tiers doc (PR #27, `docs/PRODUCT_TIERS.md`). Page-level **ingestion** is still pending a reindex window + read-mode content-format spike (ADR `09_P2_1_PAGE_CITATIONS_ADR.md`, PR #36).

**P3 (LangGraph) is scaffolded but inert:** the PR0 scaffold (PR #74) and PR1 structured-output router (PR #79) are merged **behind the default-OFF `QNA_AGENT_ENABLED` flag** — not enabled. Enabling needs explicit architect sign-off on `07_P3_LANGGRAPH_ADR.md`; PR2+ (map-reduce, ReAct, self-critique, memory, SSE) are not built.

**P4 is partial:** the Python SDK shipped in-repo unpublished (PR #31, `clients/python`) and the RAGAS eval harness shipped (PR #33, `eval/`). The Teams bot (ADR `10_P4_1_TEAMS_BOT_ADR.md`, PR #73) and connectors (ADR `08`) are designed; Helm is not built.

**Supply chain:** Dependabot is live (PR #35), CI covers the SDK + eval harness (PR #34), and the repo is now BSL 1.1 source-available (PR #72, pending confirmation of Licensor entity + Change Date). The LangChain-1.x / FastAPI-Starlette-1.x / pandas-3 / openai-2 / etc. major bumps remain a **deferred coordinated cascade** (see `docs/RESUME.md`); pip-audit stays report-only until it lands.

---

## Phase overview and status dashboard

| Phase | Description | Items | Status |
|-------|-------------|-------|--------|
| **P0** | Security, correctness, and production hardening | 8 | `8/8 SHIPPED` ✅ |
| **P1** | Enterprise feature completeness | 5 | `5/5 SHIPPED` ✅ |
| **Phase 2** | Operability (Admin API, Observability, Deployment Validation, bot_tag scope) | 4 | `A + B + C SHIPPED · D DECIDED` ✅ |
| **P2** | Product differentiation and commercial packaging | 2 | `IN PROGRESS` — P2-1 semantic rerank + `page_citations` contract + P2-2 tiers shipped; page-level ingestion pending |
| **P3** | Agentic AI layer (LangGraph) | 6 | `SCAFFOLDED, INERT` — PR0+PR1 merged behind default-OFF `QNA_AGENT_ENABLED`; needs ADR sign-off to enable |
| **P4** | Platform completeness (connectors, SDK, Teams bot) | 4 | `PARTIAL` — SDK + RAGAS shipped in-repo; Teams bot designed; Helm not built |

Phase 2 workstream specs live under `docs/architect_phase_2/`; entries appear in this tracker only after the corresponding PR merges to `main`.

---

## P0 — Production blockers (fix before any client delivery)

**8 of 8 shipped.** ✅

| # | Backlog ref | Title | Primary files | Status |
|---|-------------|-------|---------------|--------|
| P0-1 | `01_SECURITY` | JWT RS256 signature validation | `services/qna/src/core/auth.py`, `src/core/token_validator.py` | `SHIPPED (PR #4)` |
| P0-2 | `02_ISOLATION` | bot_tag tenant filter in retrieval | `services/qna/src/services/search_service.py`, `qna_pipeline.py`, `app.py` | `SHIPPED (PR #2)` |
| P0-3 | `03_CONCURRENCY` | Remove global `bot_queries` request state | `services/qna/src/pipeline/qna_pipeline.py`, `app.py` | `SHIPPED (PR #2)` |
| P0-4 | `04_INGESTION` | Deterministic chunk IDs and document lifecycle | `services/ingestion/custom_rag.py` | `SHIPPED (PR #1)` |
| P0-5 | `05_RETRIEVAL` | True token-aware chunking (replace word-count) | `services/ingestion/custom_rag.py` | `SHIPPED (PR #1)` |
| P0-6 | `06_API` | Structured error envelope, X-Request-ID on every error, no exception text leaked | `services/qna/src/core/errors.py`, `services/ingestion/errors.py`, `app.py` in both services | `SHIPPED (PR #10)` |
| P0-7 | `07_CONFIG` | Canonical UPPER_SNAKE env vars + legacy dual-read + KV secret-name mapping | `services/qna/src/config/config.py`, `infra/main.bicep`, `.env.example` files | `SHIPPED (PR #11)` |
| P0-8 | `08_RUNTIME` | Production-safe CORS, logging, container defaults | `services/qna/app.py`, both `Dockerfile`s | `SHIPPED (PR #3)` |

See `01_P0_HARDENING.md` for the original planning detail.

---

## P1 — Enterprise feature completeness (required for repeatable client delivery)

| # | Backlog ref | Title | Primary files | Status |
|---|-------------|-------|---------------|--------|
| P1-1 | `09_OBSERVABILITY` | Azure Monitor telemetry, audit logs, correlation IDs | `services/qna/src/core/observability.py`, `services/ingestion/observability.py` | `SHIPPED` — request-ID middleware + `log_event` (PR #8), pipeline stage-level events (PR #22) |
| P1-2 | `10_PRODUCT` | Admin APIs for index and tenant management | `services/ingestion/admin/` package | `SHIPPED` — read-only endpoints (PR #7), destructive delete + reindex stub, bot_tag-scoped & paginated (PR #21) |
| P1-3 | `11_CONNECTORS` | Blob Storage + SharePoint connector ingestion | `services/ingestion/connectors/` | `SHIPPED` — connector core + Blob (PR #24), SharePoint + operator sync trigger (PR #26), persisted run status + `run_id` correlation (PR #32). |
| P1-4 | `12_PLATFORM` | Azure Bicep IaC, GitHub Actions CI/CD, ACA deployment | `infra/main.bicep`, `infra/parameters/`, `docs/deployment/INSTALLATION.md`, `.github/workflows/ci.yml` | `SHIPPED` — Bicep + install runbook (PR #5), GitHub Actions CI gate (PR #13) |
| P1-5 | `13_QUALITY` | Expanded test suite, CI quality gates, release checks | `services/qna/test/`, `services/ingestion/test/`, `.github/workflows/ci.yml`, `pyproject.toml` | `SHIPPED (PR #13)` — ruff/bandit/bicep/shellcheck/pytest+coverage gate. pip-audit report-only; coverage threshold not gated yet (both tracked follow-ups). |

See `02_P1_ENTERPRISE.md` for original implementation guides and `docs/architect_phase_2/` for the active Phase 2 specs covering P1-1 and P1-2.

---

## Phase 2 — Operability, control plane, product readiness

Active workstream specs in `docs/architect_phase_2/`. Entries appear here only when the corresponding PR merges to `main`.

| Workstream | Spec | Status |
|---|---|---|
| **A** Admin API | `01_ADMIN_API_SPEC.md` | `SHIPPED` — read-only endpoints (PR #7) + destructive delete-document / delete-tenant(confirm) / reindex-stub, `bot_tag`-scoped, paginated, OData-escaped (PR #21). Operator connector-sync trigger (PR #26). |
| **B** Observability baseline | `02_OBSERVABILITY_SPEC.md` | `SHIPPED` — `RequestIDMiddleware` + `log_event` (PR #8), P0-6 X-Request-ID-on-5xx (PR #10), pipeline stage-level events for QnA + ingestion (PR #22). |
| **C** Deployment validation | `03_DEPLOYMENT_VALIDATION_SPEC.md` | `SHIPPED (PR #14)` — `scripts/validate_deployment.sh` post-deploy checks (resource existence, Container App health probes, Key Vault wiring); shellcheck-clean under the CI gate. |
| **D** bot_tag scope/naming | `04_BOT_TAG_DECISION_RECORD.md` | `DECIDED` — keep `bot_tag` internally; expose as `workspace_id` in future public APIs. Validation regex enforced in admin routes (PR #7). |

---

## P2 — Product differentiation and commercial packaging

| # | Backlog ref | Title | Status |
|---|-------------|-------|--------|
| P2-1 | `14_ROADMAP` | Retrieval quality: semantic reranking, page-level citations | `PARTIAL` — config-gated semantic reranking shipped (PR #25); typed `CitationMap` success contract shipped (PR #28); backward-compatible `page_citations` contract + retrieval groundwork shipped (PR #76, byte-identical until `page_number` is populated). Page-level **ingestion** still pending a reindex window + read-mode content-format spike (ADR `09_P2_1_PAGE_CITATIONS_ADR.md`, PR #36). |
| P2-2 | `15_PRODUCT` | Packaging tiers and deployment operating model | `SHIPPED (PR #27)` — `docs/PRODUCT_TIERS.md` (Starter/Standard/Enterprise, shipped-vs-planned tagging, deploy-into-client-subscription model). |

See `03_P2_DIFFERENTIATION.md`.

---

## P3 — Agentic AI layer (LangGraph)

> This is the core product differentiator for enterprise sales. Unlocks after P0 is complete.
>
> **Merged but inert.** The PR0 scaffold and PR1 router are on `main` behind the default-OFF `QNA_AGENT_ENABLED` flag — the `/qna` contract is byte-identical until the flag is flipped. Enabling the flag and building PR2+ requires explicit architect sign-off on `docs/architect_phase_2/07_P3_LANGGRAPH_ADR.md`.

| # | Feature | Description | Status |
|---|---------|-------------|--------|
| P3-1 | Agent Router | LangGraph supervisor that routes queries to the correct sub-agent | `MERGED, INERT` — scaffold (PR #74) + structured-output router/classifier (PR #79) behind default-OFF `QNA_AGENT_ENABLED` |
| P3-2 | Map-Reduce Summarizer | Full-document synthesis using parallel map + reduce nodes | `PLANNED` |
| P3-3 | ReAct Multi-Hop Agent | Iterative retrieval for multi-document reasoning | `PLANNED` |
| P3-4 | Self-Critique / Verifier | Hallucination detection pass after answer generation | `PLANNED` |
| P3-5 | Conversation Memory | Cosmos DB-persisted session history (replaces in-memory history) | `PLANNED` |
| P3-6 | SSE Streaming | Server-Sent Events for incremental answer delivery | `PLANNED` |

See `04_AGENTIC_ROADMAP.md` for detailed LangGraph architecture and `07_P3_LANGGRAPH_ADR.md` for the decision record.

---

## P4 — Platform completeness

| # | Feature | Description | Status |
|---|---------|-------------|--------|
| P4-1 | Microsoft Teams Bot | Azure Bot Service adapter over QnA endpoint | `DESIGNED` — ADR `10_P4_1_TEAMS_BOT_ADR.md` (PR #73); not built |
| P4-2 | RAGAS Evaluation | Automated faithfulness, relevancy, and precision scoring | `SHIPPED (PR #33)` — `eval/` harness, mocked hard gate in CI |
| P4-3 | Helm Chart (AKS) | Production Kubernetes packaging with HPA and PDB | `PLANNED` — demand-driven, not built |
| P4-4 | Python Client SDK | `pip install tocdoc-sdk` for consumer integrations | `SHIPPED IN-REPO (PR #31)` — `clients/python`, unpublished |

---

## Supply chain & repo governance

| Item | Status |
|---|---|
| Dependabot | `SHIPPED (PR #35)` — weekly grouped updates across both services, SDK, eval, GitHub Actions, Docker base images |
| CI coverage of SDK + eval | `SHIPPED (PR #34)` — `test (sdk)` + `test (eval)` jobs in `.github/workflows/ci.yml` |
| License | `SHIPPED (PR #72)` — BSL 1.1 source-available (`LICENSE`); Licensor entity + Change Date still placeholder, confirm before public release |
| Coordinated dependency cascade | `DEFERRED` — LangChain-1.x / FastAPI-Starlette-1.x / pandas-3 / openai-2 / cryptography-48 / langgraph-1.0rc / pypdf-6 / python-3.14 base bumps need runtime-validated upgrade PRs (see `docs/RESUME.md`) |

---

## Dependency graph

```
P0 (all 8 blockers)
│
├── P1-4 (IaC/CI) ←── can start in parallel with P0 in a separate branch
├── P1-5 (Quality)  ←── regression tests built alongside P0 fixes
│
└── [all P0 done]
        │
        ├── P1-1 (Observability)
        ├── P1-2 (Admin APIs) ←── requires P0-4 (deterministic IDs)
        └── P1-3 (Connectors) ←── requires P0-4
                │
                └── [all P1 done]
                        │
                        ├── P2-1 (Retrieval quality)
                        ├── P2-2 (Packaging tiers)
                        ├── P3 (Agentic layer) ←── can start after P0-3 (concurrency fix)
                        └── P4 (Platform)
```

---

## PR conventions for sub-agents

- One backlog item = one PR. Reference the backlog file name in the PR description.
- Every PR must update: source code, tests, `.env.example` if env vars changed, README if behavior changed.
- PRs that touch auth, isolation, or data lifecycle require explicit test coverage of negative cases.
- Never mix P0 fixes with P1 features in the same PR.
- Commit message format: `fix(scope): description` for P0, `feat(scope): description` for P1+, `chore(scope): description` for housekeeping, `docs(scope): description` for docs-only.
- Dual-persona commit footer:
  - **`Co-Authored by Maanav's Mac-Pro`** — developer / tech lead / Claude / Codex / implementation PR / operating-model content.
  - **`Co-Authored by Maanav's Mac-Air`** — architect / ChatGPT review messages and architect-authored spec docs.

---

## Quick file map (for sub-agent orientation)

See `05_CODEBASE_CONTEXT.md` for a complete file-by-file guide.

| Path | What it does |
|------|-------------|
| `services/qna/src/core/auth.py` | JWT middleware (RS256 validation after P0-1; envelope shape after P0-6) |
| `services/qna/src/core/errors.py` / `services/ingestion/errors.py` | Structured error contract — `ErrorEnvelope`, `ApiErrorCode`, `raise_api_error`, `build_error_response`, exception handlers (P0-6) |
| `services/qna/src/core/observability.py` / `services/ingestion/observability.py` | `RequestIDMiddleware` + `log_event` (Phase 2 B PR-1) |
| `services/qna/src/config/config.py` | Canonical UPPER_SNAKE env vars + legacy dual-read + KV secret-name mapping (P0-7) |
| `services/qna/src/pipeline/qna_pipeline.py` | Main QnA orchestration (request-scoped after P0-3; raises on internal failure after P0-6) |
| `services/qna/src/services/search_service.py` | Hybrid Azure Search call (filters by `bot_tag` after P0-2) |
| `services/ingestion/custom_rag.py` | Chunking + indexing (deterministic IDs + token chunking after P0-4 / P0-5) |
| `services/ingestion/admin/` | Read-only admin API (Phase 2 A PR-1) — routes, models, service layer, X-Admin-Token auth |
| `services/ingestion/middleware.py` | Upload size limit middleware (extracted from app.py during P0-6 for testability) |
| `services/qna/app.py` / `services/ingestion/app.py` | FastAPI apps — full middleware stack: CORS → auth (qna) / upload-limit (ingestion) → RequestIDMiddleware (outermost) → routes; error handlers registered via `register_exception_handlers(app)` |
| `infra/main.bicep` | Container Apps + supporting Azure resources; env vars wire to canonical names |
