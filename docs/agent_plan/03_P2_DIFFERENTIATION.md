# Phase P2 — Product Differentiation and Commercial Packaging

> **Prerequisite:** All P1 items complete.
> These are the features that turn TocDoc from "an enterprise-ready tool" into
> "a product with a defined market position and pricing structure."

---

## P2-1 | Retrieval Quality: Reranking, Metadata, Page-Level Citations
**Backlog:** `14_ROADMAP_Retrieval_quality_upgrades_reranking_metadata_page_citations.md`
**Status:** `BLOCKED on P1`

### Current retrieval stack
```
User query
  → embedding (text-embedding-3-small)
  → hybrid search (HNSW vector + BM25 keyword, top-k=10)
  → chunks returned directly to LLM
```

### What to add

**Step 1 — Azure Semantic Ranker (L2 re-ranking)**
Azure Cognitive Search has a built-in semantic ranker that scores results by
relevance to the query using a cross-encoder. Requires:
- Search tier S1 or higher (already recommended)
- A named semantic configuration on the index
- `query_type=QueryType.SEMANTIC` in the search call

Change in `services/qna/src/services/search_service.py` (`_search_sync`):
```python
results = azure.search_client.search(
    search_text=query,
    vector_queries=[vector_query],
    query_type=QueryType.SEMANTIC,
    semantic_configuration_name="tocdoc-semantic-config",
    select=[...],
    filter=filter_expr,
    top=top,
)
```

New env var: `AZURE_SEARCH_SEMANTIC_CONFIG=tocdoc-semantic-config`
Index must have a semantic configuration defined with `content` as the content field.

**Step 2 — Page-level citations**
Currently citations are filename-level. Add `page_number: int` to indexed chunk metadata.
Azure Document Intelligence `prebuilt-layout` returns page numbers per paragraph.

Change in ingestion: store `page_number` field on each chunk.
Change in search: include `page_number` in `select` fields.
Change in response: `CitationMap` model becomes:
```python
class CitationMap(BaseModel):
    filename: str
    filepath: str
    page_number: Optional[int] = None
```

**Step 3 — Retrieval diagnostics (dev/debug only)**
When `RETRIEVAL_DEBUG=1`, include in the response:
```json
"retrieval_debug": {
  "chunks_retrieved": 10,
  "reranking_applied": true,
  "top_chunk_score": 0.94,
  "fr_mode": "fr_read"
}
```
Never expose this field in production by default.

### Acceptance criteria
- Semantic ranker is applied when configured; falls back gracefully if not configured
- Page numbers appear in citations when available
- Retrieval debug mode works in local dev but is gated behind an env flag
- A set of benchmark queries shows measurably improved result ordering

---

## P2-2 | Packaging Tiers and Deployment Operating Model
**Backlog:** `15_PRODUCT_Define_packaging_tiers_and_deployment_operating_model.md`
**Status:** `PLANNED`

### Recommended commercial model

**Deployment posture (non-negotiable):**
TocDoc deploys into the CLIENT's Azure resource group.
The client owns all data, all Azure resources, and all compute.
TocDoc is a deployable product, not a shared SaaS.

**Packaging tiers:**

| Tier | What's included | Target buyer |
|------|----------------|--------------|
| **Starter** | Manual upload + QnA + basic monitoring + Bicep install | Small team, proof-of-value |
| **Enterprise** | Starter + connector ingestion + admin APIs + audit logs + RBAC + SLA | Mid-market enterprise |
| **Premium** | Enterprise + semantic reranking + page citations + LangGraph agentic layer + Teams bot | Large enterprise, complex doc workflows |

**Hosting targets per tier:**
- Starter: Azure Container Apps (simplest, auto-scaling, no Kubernetes)
- Enterprise: Azure Container Apps or App Service (customer preference)
- Premium: AKS (if customer needs GPU or complex networking) or Container Apps

**Delivery model:**
- One-time deployment engagement: install + configure + smoke test
- Optional managed support tier: monitoring alerts, version upgrades, incident response
- Self-serve upgrade path via `helm upgrade` (AKS) or image re-deploy (ACA)

### Deliverable for this item
Create `docs/PRODUCT_TIERS.md` with:
- Tier feature matrix
- Required Azure resource list per tier (SKUs, costs)
- Installation time estimate per tier
- Support boundary definitions
- Upgrade path documentation

### Acceptance criteria
- Engineering can map any backlog item to a tier requirement
- Sales can explain the product without a technical demo
- A client's procurement team can understand what they're buying from the docs alone
