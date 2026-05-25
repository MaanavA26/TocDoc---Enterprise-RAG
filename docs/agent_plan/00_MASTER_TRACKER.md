# TocDoc — Master Plan & Agent Tracker

> **Purpose of this folder:** This is the primary planning surface for all implementation work on TocDoc.
> It is designed to be used by both human contributors and coding sub-agents (Codex, Claude).
> Every task is tied to a backlog item, a phase, specific source files, and clear acceptance criteria.
> Sub-agents should always start here, then navigate to the relevant phase document.
>
> **Tracker discipline:** entries reflect work that has been **merged to `main`**. Open PRs and in-flight workstreams are tracked through their PRs and through the spec files under `docs/architect_phase_2/`, not here. Update this file as a follow-up `chore(docs)` PR after a workstream merges — never list speculative status.

---

## Current codebase state (as of 2026-05-25)

| Service | Entry point | Key concern |
|---------|-------------|-------------|
| `services/ingestion` | `app.py` → `custom_rag.py` | PDF ingestion, chunking, Azure Search indexing |
| `services/qna` | `app.py` → `src/pipeline/qna_pipeline.py` | Hybrid retrieval, rephrasal, LLM answer generation |

All 8 original P0 blockers have shipped. Phase 2 read-only workstreams (Admin API, Observability) are on `main`. The structured error contract and env-var normalization landed in PRs #10 and #11. Active work continues on Phase 2 destructive endpoints, pipeline-stage observability events, deployment validation, and CI gates.

---

## Phase overview and status dashboard

| Phase | Description | Items | Status |
|-------|-------------|-------|--------|
| **P0** | Security, correctness, and production hardening | 8 | `8/8 SHIPPED` ✅ |
| **P1** | Enterprise feature completeness | 5 | `2/5 SHIPPED` (P1-1 read-only piece, P1-4 Bicep) |
| **Phase 2** | Operability (Admin API, Observability, Deployment Validation, bot_tag scope) | 4 | `A PR-1 + B PR-1 SHIPPED` · A PR-2 / B PR-2 / C / D pending |
| **P2** | Product differentiation and commercial packaging | 2 | `BLOCKED on P1` |
| **P3** | Agentic AI layer (LangGraph) | 6 | `PLANNED` |
| **P4** | Platform completeness (connectors, SDK, Teams bot) | 4 | `PLANNED` |

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
| P1-1 | `09_OBSERVABILITY` | Azure Monitor telemetry, audit logs, correlation IDs | `services/qna/src/core/observability.py`, `services/ingestion/observability.py` | `PARTIAL — SHIPPED (PR #8)` for request-ID middleware + `log_event` helper; pipeline-stage events still pending |
| P1-2 | `10_PRODUCT` | Admin APIs for index and tenant management | `services/ingestion/admin/` package | `PARTIAL — SHIPPED (PR #7)` for read-only endpoints; destructive endpoints + reindex still pending |
| P1-3 | `11_CONNECTORS` | Blob Storage + SharePoint connector ingestion | new `services/ingestion/connectors/` | `PENDING` |
| P1-4 | `12_PLATFORM` | Azure Bicep IaC, GitHub Actions CI/CD, ACA deployment | `infra/main.bicep`, `infra/parameters/`, `docs/deployment/INSTALLATION.md` | `PARTIAL — SHIPPED (PR #5)` for Bicep + install runbook; CI/CD GitHub Actions still pending |
| P1-5 | `13_QUALITY` | Expanded test suite, CI quality gates, release checks | `services/qna/test/`, `services/ingestion/test/`, `.github/workflows/` | `PENDING` |

See `02_P1_ENTERPRISE.md` for original implementation guides and `docs/architect_phase_2/` for the active Phase 2 specs covering P1-1 and P1-2.

---

## Phase 2 — Operability, control plane, product readiness

Active workstream specs in `docs/architect_phase_2/`. Entries appear here only when the corresponding PR merges to `main`.

| Workstream | Spec | Status |
|---|---|---|
| **A** Admin API | `01_ADMIN_API_SPEC.md` | `PR-1 SHIPPED (PR #7)` — read-only `GET /admin/documents`, `/admin/documents/{id}`, `/admin/index/stats` with `bot_tag` scope, `X-Admin-Token` interim auth, pagination via `.by_page()`, OData filter escape. PR-2 (destructive endpoints) pending. |
| **B** Observability baseline | `02_OBSERVABILITY_SPEC.md` | `PR-1 SHIPPED (PR #8)` — `RequestIDMiddleware` in both services, `log_event` helper, lifecycle events. P0-6 (PR #10) closed the deferred X-Request-ID-on-5xx gap. PR-2 (pipeline-stage events) pending. |
| **C** Deployment validation | `03_DEPLOYMENT_VALIDATION_SPEC.md` | `PENDING` (`scripts/validate_deployment.sh`) |
| **D** bot_tag scope/naming | `04_BOT_TAG_DECISION_RECORD.md` | `DECIDED` — keep `bot_tag` internally; expose as `workspace_id` in future public APIs. Validation regex enforced in admin routes (PR #7). |

---

## P2 — Product differentiation and commercial packaging

| # | Backlog ref | Title | Status |
|---|-------------|-------|--------|
| P2-1 | `14_ROADMAP` | Retrieval quality: semantic reranking, page-level citations | `BLOCKED on P1` |
| P2-2 | `15_PRODUCT` | Packaging tiers and deployment operating model | `PLANNED` |

See `03_P2_DIFFERENTIATION.md`.

---

## P3 — Agentic AI layer (LangGraph)

> This is the core product differentiator for enterprise sales. Unlocks after P0 is complete.

| # | Feature | Description | Status |
|---|---------|-------------|--------|
| P3-1 | Agent Router | LangGraph supervisor that routes queries to the correct sub-agent | `PLANNED` |
| P3-2 | Map-Reduce Summarizer | Full-document synthesis using parallel map + reduce nodes | `PLANNED` |
| P3-3 | ReAct Multi-Hop Agent | Iterative retrieval for multi-document reasoning | `PLANNED` |
| P3-4 | Self-Critique / Verifier | Hallucination detection pass after answer generation | `PLANNED` |
| P3-5 | Conversation Memory | Cosmos DB-persisted session history (replaces in-memory history) | `PLANNED` |
| P3-6 | SSE Streaming | Server-Sent Events for incremental answer delivery | `PLANNED` |

See `04_AGENTIC_ROADMAP.md` for detailed LangGraph architecture.

---

## P4 — Platform completeness

| # | Feature | Description | Status |
|---|---------|-------------|--------|
| P4-1 | Microsoft Teams Bot | Azure Bot Service adapter over QnA endpoint | `PLANNED` |
| P4-2 | RAGAS Evaluation | Automated faithfulness, relevancy, and precision scoring | `PLANNED` |
| P4-3 | Helm Chart (AKS) | Production Kubernetes packaging with HPA and PDB | `PLANNED` |
| P4-4 | Python Client SDK | `pip install tocdoc-sdk` for consumer integrations | `PLANNED` |

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
