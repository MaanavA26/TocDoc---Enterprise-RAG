> **Status:** DRAFT planning doc — refreshed against the current codebase by an automated pass; pending architect review. Supersedes the stale assumptions in `03_P2_DIFFERENTIATION.md` for sequencing purposes. P3 is owned by a separate ADR (`docs/architect_phase_2/07_P3_LANGGRAPH_ADR.md`) and is out of scope here.

# P2 & P4 — Refreshed Specs and Sequenced Delivery Plan

## Current position

P0 is now **8/8 shipped** — the master tracker still reads `6/8`, but recent commits land P0-6 (structured error contract with `X-Request-ID` on every response, #10) and P0-7 (env-var normalization to UPPER_SNAKE, #11); the tracker dashboard should be corrected in a follow-up `chore(docs)` PR. P1 is in progress: P1-4 IaC (Bicep + install runbook) is partially shipped with CI/CD still pending, a lint/security CI gate is in flight, and observability/admin-API/connectors/quality-gate workstreams are open (admin API package already exists under `services/ingestion/admin/`). P3 (the LangGraph agentic layer) is owned by a **separate in-flight ADR/design council** and is explicitly **out of scope for this document** — it is referenced here only for sequencing.

---

## P2-1 — Retrieval quality: semantic reranking + page-level citations

This item is less greenfield than the stale `03_P2_DIFFERENTIATION.md` implies. Much of the index scaffolding already exists; the real work is on the ingestion side and in typing the QnA success contract.

### Grounded state of the code

- **Retrieval** lives in `services/qna/src/services/search_service.py::_search_sync`. It issues a hybrid query (HNSW vector `content_vector` + keyword) with `select=["id","content","section_header","filename","filepath"]`, filtered on `fr_tag` + `bot_tag`, `top=TOP_K` (config `TOP_K=20`). It does **not** pass `query_type=SEMANTIC` and does **not** select `page_number`.
- **The semantic configuration already exists on the index**, named `mySemanticConfig` (`services/ingestion/custom_rag.py:248-257`) — note: not the placeholder `tocdoc-semantic-config` the old spec guessed. Title field `section_header`, content field `content`, keyword fields `filename` + `page_number`.
- **`page_number` is defined in the index schema** (`custom_rag.py:171`, typed `String`) **but is never populated.** Both chunk-builder paths (layout mode ~398-414, read mode ~450-466) omit it. The field is effectively dead today.
- **The QnA success response is still untyped.** P0-6 structured the *error* envelope only; the success path returns citations as a loose `extracted_filepath` dict built in `qna_pipeline.py` (`file_map`, lines ~167-286). There is no `CitationMap` model.

### Step 1 — Semantic reranking (low effort, largely unblocked)

Add an L2 semantic rerank in `_search_sync`:
```python
query_type=QueryType.SEMANTIC,
semantic_configuration_name=config.AZURE_SEARCH_SEMANTIC_CONFIG,  # default "mySemanticConfig"
```
Gate behind a config knob (`AZURE_SEARCH_SEMANTIC_CONFIG`, empty = disabled) so it falls back gracefully to the current hybrid query when unset or on an unsupported Search tier (S1+ required). Because the index config already exists, this is a small, isolated change plus an env addition in `config.py` and both `.env.example` files. Optionally surface `@search.reranker_score` for the diagnostics block below.

### Step 2 — Page-level citations (the real work — ingestion-side + reindex)

Page citations are blocked on **deriving** `page_number` during ingestion, not on schema or `select`:

- **Layout mode** (Document Intelligence `prebuilt-layout`) carries per-paragraph page numbers, so the layout path can attach a page to each chunk with moderate effort.
- **Read mode is the hard case.** It token-splits a single concatenated `docs[0].page_content` (`custom_rag.py:422-427`), so per-chunk page provenance is **lost**. `total_pages` (from `fitz`, line 312) exists but there is no per-chunk page mapping. Recovering it requires splitting per-page before chunking (or tracking character→page offsets), which changes the chunking flow and warrants its own review.
- Consider re-typing `page_number` to `Int32` (it is `String` today); reconcile against the deterministic chunk-ID scheme from P0-4 so reindexed chunk IDs stay stable.
- **A full reindex of existing tenants is required** — empty `page_number` won't backfill itself.

Then: add `page_number` to the `select` list in `_search_sync`, and **introduce a typed `CitationMap` model** (`filename`, `filepath`, `page_number: Optional[int]`) to replace the loose dict in `qna_pipeline.py`. This typing is shared load-bearing work for the SDK (P4-4) and RAGAS (P4-2).

### Step 3 — Retrieval diagnostics (dev-only)

Behind `RETRIEVAL_DEBUG=1`, attach `{chunks_retrieved, reranking_applied, top_chunk_score, fr_mode}` to the response. Never emit by default.

### Acceptance criteria

- Semantic rerank applied when `AZURE_SEARCH_SEMANTIC_CONFIG` is set; clean fallback otherwise.
- `page_number` populated at ingestion for **both** layout and read modes; appears in typed citations when available.
- Citations returned via a typed `CitationMap`, not a loose dict.
- Diagnostics gated behind the env flag; a benchmark query set shows improved ordering.

---

## P2-2 — Packaging tiers & deployment operating model

**Deployment posture (non-negotiable):** the product deploys **into the client's own Azure resource group / subscription**. The client owns all data, Azure resources, and compute. This is a deployable product, not shared multi-tenant SaaS — `bot_tag` isolation (P0-2) is the within-deployment tenancy boundary.

Tiers map to shipped vs planned capability so engineering can trace any backlog item to a tier:

| Tier | Included (shipped today) | Adds (planned) | Hosting |
|------|--------------------------|----------------|---------|
| **Starter** | Manual upload + QnA, hybrid retrieval, JWT/RS256 auth (P0-1), tenant isolation (P0-2), structured errors (P0-6), Bicep install (P1-4) | — | Azure Container Apps |
| **Standard** | Starter + read-only admin API (shipped), request-ID observability | Connector ingestion (P1-3), audit logs, full observability (P1-1) | ACA or App Service |
| **Enterprise** | Standard + SLA/support | Semantic rerank + page citations (P2-1), agentic layer (P3, separate ADR), Teams bot (P4-1), AKS/Helm (P4-3) | AKS or ACA |

**Delivery model:** one-time deployment engagement (install → configure → smoke test, leveraging the deployment-validation spec under `docs/architect_phase_2/`); optional managed-support tier; self-serve upgrade via image redeploy (ACA) or `helm upgrade` (AKS).

**Deliverable:** `docs/PRODUCT_TIERS.md` — tier feature matrix, per-tier Azure resource/SKU list, install-time estimate, support boundaries, upgrade path. **Acceptance:** any backlog item maps cleanly to a tier; sales can explain the product without a demo; procurement can understand the purchase from docs alone.

---

## P4 — Platform completeness

### P4-1 — Microsoft Teams bot
**What:** an Azure Bot Service adapter (Bot Framework) fronting the QnA `/answer` endpoint, delivering retrieval-grounded answers in Teams chat.
**Integrates:** a thin new adapter service that calls the existing QnA HTTP API; maps the Teams identity/tenant to a `bot_tag` and forwards the JWT or a service credential. Citations render as Teams adaptive-card links (page numbers from P2-1 improve this).
**Risks:** identity mapping (Teams AAD identity → `bot_tag`) and token flow; Bot Framework messaging-endpoint hosting; per-deployment registration in the client subscription. Latency makes SSE streaming (a P3 item) desirable but not required.
**Effort:** Medium.

### P4-2 — RAGAS evaluation
**What:** automated faithfulness / answer-relevancy / context-precision scoring over a fixed benchmark query set.
**Integrates:** an offline eval harness (likely under `services/qna/test/` or a new `eval/` dir) that drives the QnA pipeline and scores `(question, answer, contexts, ground_truth)`. Hooks into the P1-5 CI quality gate as a non-blocking report initially.
**Risks:** RAGAS makes its own LLM calls (Azure OpenAI cost/quota in CI); no live Azure in this environment, so it runs in CI/architect-controlled runs only. **Needs a stable QnA contract** — typed citations/contexts (P2-1) directly feed context-precision.
**Effort:** Medium.

### P4-3 — Helm chart (AKS)
**What:** production Kubernetes packaging (Deployments, Services, Ingress, HPA, PDB, secrets) for both services.
**Integrates:** an **alternative** deployment path to the shipped Bicep + ACA path (P1-4), targeting the Enterprise tier where the client needs AKS/GPU/complex networking — not additive to ACA. Reuses the same container images and env contract.
**Risks:** duplicate maintenance of two deployment surfaces (Bicep/ACA + Helm/AKS); secret management (Key Vault CSI driver); env-var parity with the normalized P0-7 names.
**Effort:** Medium–High.

### P4-4 — Python client SDK
**What:** `pip install`-able client wrapping the QnA (and optionally ingestion/admin) HTTP API with typed models and retries.
**Integrates:** generated/hand-written client over the public API surface. The **error-envelope dependency (P0-6) is now satisfied** — the SDK can deserialize a stable error shape and map codes to exceptions. **However the success response is still an untyped dict**, so the SDK is gated on typing the QnA success contract (`CitationMap`, success envelope) — a sharper dependency than "error envelope."
**Risks:** API surface churn before it's frozen; auth/token handling in the client; versioning the contract.
**Effort:** Low–Medium (after the contract is typed).

---

## Dependencies & sequencing

Reasoning from the actual code, not the tracker's blanket `BLOCKED on P1`:

1. **P0 (8/8) is done** — unblocks all downstream work; correct the tracker first.
2. **P2-1 Step 1 (semantic rerank) is largely unblocked now** — index config exists; it's a small search-side + config change. Can land independently of P1.
3. **P2-1 Step 2 (page citations) gates on an ingestion change + full reindex**, and on typing `CitationMap`. The read-mode page-provenance fix is the real risk; sequence it as its own reviewed change. Best done alongside or just after P1-3 connectors (both touch ingestion) to share a single reindex.
4. **Typed QnA success contract (`CitationMap`/success envelope)** is a shared prerequisite — it falls out of P2-1 Step 2 and unblocks both P4-4 (SDK) and clean P4-2 (RAGAS) contexts.
5. **P4-2 RAGAS** needs the stable QnA contract above; page citations improve context-precision. Runs in CI only (no live Azure here). Naturally pairs with the P1-5 quality gate.
6. **P4-4 SDK** — error half unblocked (P0-6); waits on the typed success contract (step 4), not on errors.
7. **P4-3 Helm** is an alternative to the shipped Bicep/ACA path (P1-4), pursued per Enterprise-tier demand; not a blocker for anything else.
8. **P4-1 Teams bot** depends only on a stable QnA endpoint + identity→`bot_tag` mapping; benefits from P2-1 citations and (later) P3 streaming.
9. **P3 agentic layer** — out of scope here; owned by the in-flight P3 ADR/design council; sequences after P0 (already met) and the P1 observability/contract work.

Suggested order: correct tracker → P2-1 rerank → type success contract + P2-1 page citations (with reindex) → P4-2 RAGAS + P4-4 SDK in parallel → P4-1 Teams bot / P4-3 Helm per tier demand. P2-2 packaging doc can be authored anytime (docs-only).

---

## Open questions for the architect

1. **Read-mode page provenance:** acceptable to re-architect read-mode chunking to split per-page (or track char→page offsets) before token-splitting, accepting a reindex? Or scope page citations to layout mode only in v1?
2. **`page_number` typing:** re-type the index field from `String` to `Int32`? This is a breaking index change requiring reindex of all tenants — when is the reindex window?
3. **Success-response contract:** approve introducing a typed `CitationMap` + success envelope now (it unblocks SDK and RAGAS), or defer until P3's response-model additions to avoid double-touching the contract?
4. **Semantic ranker default:** ship `query_type=SEMANTIC` enabled-by-default (requires S1+) or opt-in via config to avoid breaking smaller-tier deployments?
5. **Tier boundaries:** confirm the Starter/Standard/Enterprise split above and which features are contractual vs best-effort per tier.
6. **Helm investment:** is AKS/Helm demand-driven (build on first Enterprise ask) or do we pre-build to de-risk sales?
7. **RAGAS in CI:** acceptable Azure OpenAI cost/quota budget for eval runs, and blocking vs non-blocking gate?
