# 14 — Roadmap: Retrieval quality upgrades, reranking, metadata, and page-level citations

**Priority:** P2  
**Type:** Product quality / Differentiation / Roadmap

## Problem

The current retrieval path is functional and already useful, but it can be improved significantly for enterprise-grade answer quality and source precision. The platform does not yet include reranking, richer retrieval metadata, or page-level citation precision.

## Why this matters

- Better retrieval quality directly improves user trust.
- Source precision is a major differentiator in document-grounded products.
- Enterprise users often want to know not just which document answered a question, but where in the document the answer came from.
- These upgrades improve the premium feel of the product once the platform is already secure and operable.

## Desired outcome

Enhance TocDoc’s retrieval stack so that it returns more relevant context and more precise citations, without undermining the grounded-answer discipline already in place.

## Scope

- Evaluate reranking options after initial hybrid retrieval.
- Enrich indexed metadata for better result filtering and citation precision.
- Add page-aware or section-aware citations where feasible.
- Consider better retrieval diagnostics to understand why a result set was chosen.

## Implementation guidance

- Keep the base retrieval flow simple and measurable before layering reranking.
- Prefer incremental experimentation with benchmark queries.
- Think carefully about the output contract if page-level citation details are added.
- Make sure extra metadata remains useful for admin operations and future analytics.

## Deliverables

- proposed retrieval-upgrade design
- implementation of at least one meaningful quality improvement
- benchmark or evaluation notes
- updated docs on citation behavior

## Acceptance criteria

- Retrieval improvements are measurable or at least demonstrably useful on representative examples.
- Citation precision improves without weakening groundedness.
- Metadata additions remain consistent across ingestion and retrieval.
- Documentation explains the new retrieval behavior clearly.

## Non-goals

- Massive research experimentation before the core product is production-ready. This is a post-hardening roadmap item.

## Notes for Codex / Claude

Treat this as a quality multiplier after the P0/P1 foundation is in place. It is valuable, but it should not distract from security and productization work.