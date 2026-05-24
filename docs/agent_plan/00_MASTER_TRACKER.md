# TocDoc — Master Plan & Agent Tracker

> **Purpose of this folder:** This is the primary planning surface for all implementation work on TocDoc.
> It is designed to be used by both human contributors and coding sub-agents (Codex, Claude).
> Every task is tied to a backlog item, a phase, specific source files, and clear acceptance criteria.
> Sub-agents should always start here, then navigate to the relevant phase document.
>
> **Tracker discipline:** entries reflect work that has been **merged to `main`**. Open PRs and in-flight workstreams are tracked through their PRs and through the spec files under `docs/architect_phase_2/`, not here. Update this file as a follow-up `chore(docs)` PR after a workstream merges — never list speculative status.

---

## Current codebase state (as of 2026-05-09)

| Service | Entry point | Key concern |
|---------|-------------|-------------|
| `services/ingestion` | `app.py` → `custom_rag.py` | PDF ingestion, chunking, Azure Search indexing |
| `services/qna` | `app.py` → `src/pipeline/qna_pipeline.py` | Hybrid retrieval, rephrasal, LLM answer generation |

Both services are functional and have been hardened against most of the original P0 blockers (6 of 8 shipped). Two P0 items (P0-6 API hardening, P0-7 env-naming normalization) and the P1 workstreams below remain open.

---

## Phase overview and status dashboard

| Phase | Description | Items | Status |
|-------|-------------|-------|--------|
| **P0** | Security, correctness, and production hardening | 8 | `6/8 SHIPPED` (P0-6, P0-7 remaining) |
| **P1** | Enterprise feature completeness | 5 | `1/5 SHIPPED` (P1-4 IaC) |
| **P2** | Product differentiation and commercial packaging | 2 | `BLOCKED on P1` |
| **P3** | Agentic AI layer (LangGraph) | 6 | `PLANNED` |
| **P4** | Platform completeness (connectors, SDK, Teams bot) | 4 | `PLANNED` |

Phase 2 productization workstreams (Admin API, Observability, Deployment Validation, scoping decisions) are specified under `docs/architect_phase_2/` and are tracked through their individual PRs while open. They will appear on this dashboard once merged.

---

## P0 — Production blockers (fix before any client delivery)

**6 of 8 shipped. P0-6 and P0-7 remain.**

| # | Backlog ref | Title | Primary files | Status |
|---|-------------|-------|---------------|--------|
| P0-1 | `01_SECURITY` | JWT RS256 signature validation | `services/qna/src/core/auth.py`, `src/core/token_validator.py` | `SHIPPED (PR #4)` |
| P0-2 | `02_ISOLATION` | bot_tag tenant filter in retrieval | `services/qna/src/services/search_service.py`, `qna_pipeline.py`, `app.py` | `SHIPPED (PR #2)` |
| P0-3 | `03_CONCURRENCY` | Remove global `bot_queries` request state | `services/qna/src/pipeline/qna_pipeline.py`, `app.py` | `SHIPPED (PR #2)` |
| P0-4 | `04_INGESTION` | Deterministic chunk IDs and document lifecycle | `services/ingestion/custom_rag.py` | `SHIPPED (PR #1)` |
| P0-5 | `05_RETRIEVAL` | True token-aware chunking (replace word-count) | `services/ingestion/custom_rag.py` | `SHIPPED (PR #1)` |
| P0-6 | `06_API` | Pydantic response contracts, structured errors | `services/qna/app.py`, `services/ingestion/app.py` | `TODO` |
| P0-7 | `07_CONFIG` | Normalize env var naming across both services | `services/qna/src/config/config.py`, both `.env.example` files | `TODO` |
| P0-8 | `08_RUNTIME` | Production-safe CORS, logging, container defaults | `services/qna/app.py`, both `Dockerfile`s | `SHIPPED (PR #3)` |

See `01_P0_HARDENING.md` for exact file locations, line numbers, and implementation detail.

---

## P1 — Enterprise feature completeness (required for repeatable client delivery)

| # | Backlog ref | Title | Primary files | Status |
|---|-------------|-------|---------------|--------|
| P1-1 | `09_OBSERVABILITY` | Azure Monitor telemetry, audit logs, correlation IDs | new module in each service | `TODO` |
| P1-2 | `10_PRODUCT` | Admin APIs for index and tenant management | new `services/ingestion/admin/` package | `TODO` |
| P1-3 | `11_CONNECTORS` | Blob Storage + SharePoint connector ingestion | new `services/ingestion/connectors/` | `PENDING` |
| P1-4 | `12_PLATFORM` | Azure Bicep IaC, GitHub Actions CI/CD, ACA deployment | `infra/main.bicep`, `infra/parameters/`, `docs/deployment/INSTALLATION.md` | `PARTIAL — SHIPPED (PR #5)` for Bicep + install runbook; CI/CD GitHub Actions still pending |
| P1-5 | `13_QUALITY` | Expanded test suite, CI quality gates, release checks | `services/qna/test/`, `services/ingestion/test/` | `PENDING` (per-PR tests added on the merged P0 work; no CI quality gates yet) |

See `02_P1_ENTERPRISE.md` for implementation guides and `docs/architect_phase_2/` for the active Phase 2 specs covering P1-1 and P1-2.

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
| `services/qna/src/core/auth.py` | JWT middleware (RS256 validation lives here after P0-1) |
| `services/qna/src/pipeline/qna_pipeline.py` | Main QnA orchestration (request-scoped after P0-3) |
| `services/qna/src/services/search_service.py` | Hybrid Azure Search call (filters by `bot_tag` after P0-2) |
| `services/qna/src/config/config.py` | Pydantic Settings — env var naming still inconsistent (P0-7 open) |
| `services/ingestion/custom_rag.py` | Chunking + indexing (deterministic IDs + token chunking after P0-4 / P0-5) |
| `services/qna/app.py` | FastAPI app — Pydantic response models + structured errors still open (P0-6) |
| `services/ingestion/app.py` | FastAPI app — Pydantic response models + structured errors still open (P0-6) |
