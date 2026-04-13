# 05 — Retrieval: Implement true token-aware chunking and evaluation

**Priority:** P0  
**Type:** Retrieval quality / Cost control / Production blocker

## Problem

The read-mode chunker is named as token-based chunking, but the implementation currently splits by whitespace words rather than real model token counts. That means chunk sizes and overlaps do not truly match the stated token budget.

## Why this matters

- Retrieval quality depends heavily on chunk boundaries.
- Cost and latency planning depend on predictable chunk sizes.
- Incorrect chunking weakens the product claim around a deliberate retrieval strategy.
- If the chunking contract is inaccurate, later tuning efforts become noisy and misleading.

## Desired outcome

TocDoc should have a real token-aware chunking implementation for read-mode ingestion and a lightweight evaluation framework to compare chunking outcomes and retrieval quality over representative enterprise documents.

## Scope

- Replace word-count chunking with actual tokenizer-based chunking.
- Preserve overlap semantics using real token windows.
- Review whether layout mode also needs token guards for oversized sections.
- Add a small retrieval evaluation harness or benchmark dataset for regression checking.

## Implementation guidance

- Use the same tokenizer family aligned to the embedding / LLM model assumptions where practical.
- Keep chunking implementation deterministic.
- Capture useful metadata such as token count, character count, and maybe chunk ordinal.
- Create a reproducible evaluation script or test fixture that compares retrieval performance for a fixed set of queries.

## Deliverables

- real token-aware chunker for read mode
- tests proving chunk sizes obey configured token limits
- evaluation harness or documented benchmark flow
- updated README and ingestion docs

## Acceptance criteria

- Read-mode chunks are bounded by actual token counts, not word counts.
- Overlap behavior is deterministic and documented.
- Oversized or edge-case text is handled safely.
- Retrieval regression checks exist for future tuning.

## Non-goals

- Full ML-based reranking. That belongs to a later quality-improvement issue.

## Notes for Codex / Claude

Do not only rename functions or comments. Fix the algorithm and add a simple way to verify that future changes do not silently degrade retrieval quality.