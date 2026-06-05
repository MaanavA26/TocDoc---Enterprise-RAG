> **Status:** DRAFT — produced by a multi-agent design council (3 page-provenance approaches: char-offset / split-per-page / layout-only; 3 judges). Pending architect sign-off on the reindex window + the layout-marker spike. Not yet implemented.

# P2-1 Step 2 — Page-Level Citations: Architecture Decision Record

**Status:** Proposed — pending architect sign-off on the reindex window and the spike gates in this document.
**Will supersede (once accepted & implemented):** the "Out of scope: page_number" note in `services/qna/src/core/responses.py` (lines 25–27). That note stays accurate until the optional `page_citations` field actually ships — this ADR is the *decision record* for that workstream, not yet in effect.

## Context & constraints (read-mode provenance is the hard part)

We want answers to cite not just the source file but the **page** a passage came from. The index schema already reserves the field: `page_number` is defined in `create_search_index()` (`services/ingestion/custom_rag.py:173`, typed `String`) and is already wired into the semantic keyword fields (`custom_rag.py:253`). It is simply **never populated** by either chunk builder today — layout (`~428–469`) and read (`~493–534`).

The hard constraint is read-mode provenance. Read mode token-splits a *single concatenated string*, `docs[0].page_content`, via `_chunk_text_by_tokens` (`custom_rag.py:679–725`). The tokenizer walks token windows with overlap and returns only decoded, stripped strings — so the character→page relationship is destroyed before a chunk is ever built. `total_pages` exists from `fitz` (`custom_rag.py:329`), but that is a document-level count with **no per-chunk page mapping**. There is no surviving offset to reconstruct one after the fact.

A correctness landmine worth stating up front: **`fitz` must never become a content source.** `fitz.get_text()` returns only the embedded text layer and yields blank/garbage on scanned/OCR PDFs. Read mode sources OCR'd content from Azure Document Intelligence (`custom_rag.py:369`); swapping in `fitz` would silently index empty content for scanned tenants — a severe retrieval regression. `fitz` stays exactly where it is: `page_count` only (`custom_rag.py:328–330`).

Downstream, citations are **filename-keyed**, not chunk-keyed. The model emits filenames; `qna_pipeline.py` collapses many chunks to one filename via `file_map` (`qna_pipeline.py:193`) and dedups citations by real name (`qna_pipeline.py:258–293`) before building `CitationMap` at `qna_pipeline.py:330`. The finest attribution physically available is therefore *the set of pages of the retrieved chunks for each cited file* — useful for a jump-to-page UX, but not per-claim accuracy. This bounds the contract shape (see below).

Finally, the response contract is pinned. `CitationMap` is `RootModel[dict[str, str]]` serializing flat `{filename: filepath}` (`responses.py:37–50`); the route uses `response_model_exclude_none=True` so optional fields never serialize, keeping the success JSON byte-identical to the historical `{answer, citation}` (`responses.py:14–21`). `test_responses_contract.py` (backward-compat assertions at `37–42`) locks this. Any page extension must not disturb it.

## Decision (recommended approach, with read-mode + layout-mode handling)

We adopt a **mirror-image merge**: the read-mode and layout-mode page sources are different mechanisms because the two modes are physically different, and a single trick cannot serve both faithfully.

### Read mode — split-per-page-before-chunking (the centerpiece)

Restructure the read branch (`custom_rag.py:474–536`) so page provenance is **intrinsic** to every chunk rather than reconstructed. Load read-mode content through Azure Document Intelligence's **`mode="page"`** loader, which yields one `Document` per page with `metadata["page"]` (1-based) and OCR-preserved content. This shape is verified against the installed langchain parser (`AzureAIDocumentIntelligenceLoader` / `_generate_docs_page` in the installed `langchain_community` document-loaders parser `doc_intelligence.py`), not assumed.

Then token-chunk **within each page**:

- maintain a single running emitted-chunk counter `i` across all pages (do **not** reset per page);
- for each page Document, run the existing `_chunk_text_by_tokens` on that page's content;
- for each non-empty chunk, build the `azure_doc` exactly as today (`516–534`) plus `"page_number": str(page_no)`, using the running `i` for the ID and incrementing only on emitted (non-empty) chunks.

No chunk straddles a page boundary, so `page_number` is exact and trivially correct — the strongest possible read-mode fidelity. The cost (chunk-boundary and content churn) is disclosed in the next two sections.

**Gate (blocking) — read mode is also a retrieval-behaviour migration, not just a field add.** DI `mode="page"` emits line-joined *text*, whereas read mode today indexes markdown-like content; switching changes the *indexed representation* and the chunk boundaries, which can degrade retrieval for tables, headings, and bullets. Read-mode page citations are therefore gated on a **content-format spike + retrieval-quality QA** (Spike A below): (a) check whether DI/langchain can yield page-level content that preserves useful markdown/table structure; (b) benchmark current read-mode markdown chunks vs proposed page-mode text chunks on a small query set; (c) proceed only if quality is acceptable or the architect explicitly accepts the tradeoff. If page-mode text is selected, it ships as a **planned retrieval-behaviour migration**, not silent page-citation work — page citations must never quietly reduce answer quality.

### Layout mode — overlay, never re-split

Layout mode exists to produce header-based `section_header` / `sub_section` via `MarkdownHeaderTextSplitter` over the concatenated markdown (`custom_rag.py:419–423`). We **must not** switch the layout loader to `mode="page"`: doing so would churn the P0-4 chunk IDs *and* destroy the header structure layout exists to provide.

Instead, **overlay** page numbers onto the existing header-split chunks without re-splitting: build a char-offset→page transition table from the DI markdown once, then for each existing chunk read the page(s) its span covers and set `page_number` (single page `"3"`, or a range `"3-4"` when a header section straddles a page break). Boundaries, the `i` counter, and the empty-skip (`432–434`) are untouched, so layout IDs stay byte-identical and the layout reindex is a pure field-backfill.

**This layout overlay rests on an unverified premise and is gated behind a blocking spike** (see Open questions Q1 / delivery plan): no `<!-- PageBreak -->` / `<!-- PageNumber -->` fixture or extraction code exists in-repo, and there is no runtime/Azure here to confirm the markers survive into `docs[0].page_content`. If the spike shows markers are absent or unreliable, the fallback is a **page-aware DI pass** producing page *ranges* per header section — a char-offset find-cursor will not work there because `mode="page"` text (line-joined) does not align char-for-char with the markdown header chunks. Layout attribution may therefore be ranges, not exact pages; that is acceptable and honest.

### Contract handling (both modes)

`CitationMap` stays untouched. Pages ride a **new optional sibling** on `QnASuccessResponse`:

```python
page_citations: dict[str, list[str]] | None = Field(default=None)
```

filename → ordered-unique list of cited page strings. It is `list[str]`, **not** a scalar — because citations dedup by filename, a file cited from pages 3 and 7 must surface `["3", "7"]`, not collapse to one. Pages are captured in the retrieval loop (`qna_pipeline.py:189–201`) **before** the filename dedup, then filtered to the cited subset. When empty/all-empty it stays `None`, and `response_model_exclude_none=True` drops it — the historical `{answer, citation}` payload stays byte-for-byte identical on every read-mode-unindexed, layout-unindexed, or no-page answer.

## Index & deterministic-ID impact (page_number population; P0-4 stability)

`page_number` **stays typed `String`** (`custom_rag.py:173`). Do not retype to `Int32`: it is a second breaking index change for zero functional gain, and `String` is required anyway to carry ranges like `"3-4"`. The field already exists and is already in the semantic keyword config (`253`) — we are populating it, not adding it.

Deterministic chunk IDs are `{tag}_{document_id}_{fr_mode}_{i:05d}` (`custom_rag.py:454, 518`). `page_number` is a separate field and **must never be encoded into the ID**.

The two modes differ sharply on ID stability:

- **Layout (overlay):** IDs are **byte-identical**. The overlay adds one key to the existing chunk dict; `MarkdownHeaderTextSplitter` boundaries, the `i` counter, the empty-skip, and the chunk count are all unchanged. The reindex is a pure field-backfill on stable IDs.
- **Read (`mode="page"`):** the ID **format** is unchanged and `i` stays a dense 0-based sequence, but the **ID→content mapping changes**. This is not merely a new field on the same chunks. Two compounding changes:
  1. Per-page chunking drops the cross-page token overlap window, so chunk boundaries and counts shift; `..._read_00007` maps to different text after reindex.
  2. **Content-format change (the most under-weighted fact in this decision):** the current default load is markdown (`output_content_format="markdown"`); `mode="page"` emits line-joined **text** (`output_content_format="text"`). The *indexed text itself* changes — markdown/table structure is flattened. This is a genuine retrieval-behavior shift, not cosmetic.

So read-mode reindex is a **semantic re-chunk**, not a backfill. This is expected and is precisely what makes the window the architect's call (next section). No index *schema* migration is needed in either mode — the field already exists.

## Extending the CitationMap contract backward-compatibly (#28)

Decision: **do not touch `CitationMap`; add an optional sibling field.** This is the entire backward-compat story — execute it, do not redesign the `RootModel`.

- `CitationMap` stays `RootModel[dict[str, str]]` (`responses.py:37–50`). Every `test_responses_contract.py` assertion — flat dump, byte-identical historical round-trip, wrong-type rejection — stays green **unchanged**, because the type is unchanged.
- Add `page_citations: dict[str, list[str]] | None = Field(default=None)` to `QnASuccessResponse` (`responses.py:53–93`), keyed on filename to join against `citation`.
- Backward-compat proof: the field defaults to `None`; the route's `response_model_exclude_none=True` (and the pipeline's `model_dump(exclude_none=True)`) drop it on every path where it is `None` — read-mode-unindexed, layout-unindexed, mixed-fleet pre-reindex, or no-page answers. JSON stays byte-identical to `{answer, citation}`.
- Update the `responses.py:25–27` "Out of scope" docstring to describe the new optional field (this ADR supersedes that note).
- This answers planning-doc **Q3**: **defer** a typed/versioned `CitationMap`; add the optional sibling now. New tests assert (a) `None` → excluded (byte-identity preserved), (b) populated → flat `filename → list` object, (c) `CitationMap` itself still dumps flat.

Also add `"page_number"` to the `search_service.py` select list (`_search_sync`, `118–126`) — currently absent. This is free and backward-compatible: Azure returns an empty string for unpopulated/old chunks, the `fr_tag + bot_tag` filter is unaffected, and the semantic-ranking fallback (`search_service.py:144–151`) is untouched. The pipeline must treat empty/`None` `page_number` as "no page" so a stray empty string never leaks into the JSON and defeats `exclude_none`.

## Reindex & migration implications (window is the architect's call)

A full reindex of all existing tenants is **already mandated** by constraint #7 / P0-4 — `page_number` cannot backfill onto chunks ingested before it was populated. This work **rides that mandatory window; it does not introduce a new one.** Re-ingestion is idempotent: `upload()` already deletes stale chunks for `document_id + bot_tag` before re-writing (`custom_rag.py:337–353`), and `merge_or_upload_documents` (`~568`) overwrites in place.

The honest, decision-relevant framing for the architect: **this window contains two different reindex classes.**

- **Layout corpora → field-backfill.** Stable IDs, unchanged content and boundaries; lowest-risk. Gains pages on reindex.
- **Read corpora → semantic re-chunk.** Boundaries, chunk counts, *and* the indexed text change (markdown→text, dropped cross-page overlap). This needs a retrieval-quality QA pass, not just a field check.

**The reindex WINDOW — when it runs, how it is staged, and whether read and layout are scoped together or separately — is the architect's decision, not ours.** We are deliberately not setting it. We surface the inputs: layout can ship as a backfill independently of read; the read re-chunk is the heavier, QA-gated half; read-mode tenants gain nothing from a layout-only window. This is planning-doc **Q2**. We recommend confirming which path each corpus takes *before* scheduling its QA.

## Rejected alternatives (the other two approaches)

**Char→page offset tracking (recover provenance after token-chunking).** Scan `docs[0].page_content` for `<!-- PageBreak -->` markers into an offset table, then map each chunk's char span back via the tokenizer's start/end token indices. Rejected for three concrete reasons, not mere preference:
1. **Unverified marker survival** — it stakes read-mode provenance on `<!-- PageBreak -->` surviving langchain's markdown passthrough into `docs[0].page_content`, which cannot be confirmed here and which the proposal itself flagged as its "single residual unknown." Its own fallback for marker-loss is to load `mode="page"` — i.e., it collapses into our chosen read-mode path anyway.
2. **Shared-helper blast radius** — it requires changing `_chunk_text_by_tokens` (`679–725`) to surface token indices, touching a shared helper whose current behavior (`.strip()` at `711`, returns strings only) the read and layout token-count events depend on.
3. **Scalar page bug** — it modeled `page_citations` as `filename → str`, which silently collapses/overwrites when one file is cited from multiple pages (3 and 7 → one page lost), given the filename dedup at `qna_pipeline.py:193, 258–293`.

**Layout-mode-only v1 (defer read entirely).** Smallest blast radius and gentlest reindex (overlay → byte-stable IDs, field-backfill). Rejected as the *whole* decision because it delivers **zero read-mode provenance** — the brief explicitly names read-mode provenance "the hard part," and the Decision section requires both modes. Its overlay technique is not discarded, though: we **graft it as our layout-mode mechanism** above, including its hard rule against switching the layout loader to page-mode.

## Sequenced delivery plan (PR-sized increments)

**Rollout sequencing — three independently-gated tracks (read must NOT block the other two):**
1. *Contract field + search select-list* are backward-compatible and land first (steps 3–4) — safe before anything populates `page_number`.
2. *Layout* page citations ship after **Spike B** (marker survival) as a pure field-backfill on byte-identical IDs.
3. *Read-mode* page citations are a **retrieval-behaviour migration**: they require **Spike A's content-format + retrieval-quality QA gate** AND a planned reindex window before they ship.

1. **Spike A (blocking, read) — shape AND content-format / retrieval-quality gate:** (i) confirm the installed DI `mode="page"` Document shape (one Document/page, 1-based `metadata["page"]`, OCR content) against a real read-mode response + capture a fixture; (ii) check whether a page-level source can preserve useful markdown/table structure; (iii) **benchmark retrieval quality** of current read-mode markdown chunks vs proposed page-mode text chunks on a small query set. Read-mode implementation proceeds ONLY if quality is acceptable or the architect explicitly accepts the markdown→text tradeoff. (Planning-doc Q1.)
2. **Spike B (blocking, layout):** confirm whether `<!-- PageBreak -->` / `<!-- PageNumber -->` markers survive into `docs[0].page_content`; capture a fixture. If absent, adopt the page-aware-DI-pass / page-range fallback for layout. Gates the layout PR only; blocks no design.
3. **Search select (independent, backward-compatible):** add `"page_number"` to `search_service.py:118–126`; verify the semantic fallback is unaffected. Ships safely before anything populates the field (old chunks return empty).
4. **Contract (additive):** add optional `page_citations: dict[str, list[str]] | None` to `QnASuccessResponse`; update the out-of-scope docstring; extend `test_responses_contract.py` to prove `None`→excluded byte-identity and populated→flat-list, with `CitationMap` unchanged.
5. **Read ingestion:** switch the read branch to DI `mode="page"`, per-page token-chunking with a single running emitted-counter, populate `page_number = str(page_no)`; keep ID format and empty-skip. Unit-test over a fake multi-page DI result: one `page_number` per chunk, correct 1-based pages, ID format unchanged, empty pages skipped.
6. **Layout ingestion (after Spike B):** overlay char-offset→page (or page-range fallback) onto existing header-split chunks; populate `page_number` (single or `"3-4"`); leave `MarkdownHeaderTextSplitter`, the loop, IDs, and empty-skip untouched. Overlay unit test on synthetic marked markdown.
7. **Pipeline:** accumulate `file_pages[filename]` (ordered-unique, non-empty) in the retrieval loop (`189–201`) **before** dedup; build `page_citations` only for filenames surviving into `extracted_filepath` (`264–293`); thread into `QnASuccessResponse` (`328–331`); `None` when empty. Tests for multi-page-same-file → list, and `None` when no pages.
8. **Reindex rollout:** coordinate within the architect's window (Q2). Layout = field-backfill; read = semantic re-chunk with a retrieval-quality QA pass; idempotent via existing stale-chunk cleanup (`337–353`).

Note: planning-doc **Q4** (semantic ranker enabled-by-default vs opt-in, gated on S1+ tier) is independent of this Step-2 work and must not block these steps.

## Open questions for the architect

1. **Read-mode mechanism & content change (Q1).** Do you accept switching read mode to DI `mode="page"`, given it changes the *indexed text* (markdown→line-joined text) and chunk boundaries, not just adds a field? This is the read-mode-provenance decision the planning doc asks us to make explicit. Our recommendation: yes — it is the only verified, straddle-free read-mode source.
2. **Reindex window (Q2) — explicitly yours.** When is the mandatory reindex viable, and do you scope read (semantic re-chunk + QA) and layout (field-backfill) into one window or stage them? We provide the inputs; the window and staging are your call.
3. **Contract timing (Q3).** Confirm: add the optional `page_citations` sibling now and **defer** a typed/versioned `CitationMap` to a later response-model workstream. Our recommendation: yes.
4. **Layout marker availability (Spike B outcome).** If `<!-- PageBreak -->` markers do not survive into `docs[0].page_content`, do you accept layout attribution as page **ranges** via a page-aware DI pass, rather than exact single pages?
5. **Multi-page granularity.** Confirm `page_citations` as `filename → list[pages]` (jump-to-page UX), accepting it is per-retrieved-chunk, not per-claim, accuracy.
6. **Semantic-ranker default (Q4).** Independent of this work and not on this critical path — flag only so it is not conflated with page-citation delivery.

