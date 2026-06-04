# RAGAS evaluation harness (P4-2)

Offline RAGAS quality evaluation for the TocDoc QnA service. It scores the
real QnA pipeline's answers against a small benchmark and emits a JSON +
markdown report.

This is a **separate, top-level `eval/` package**. It imports the QnA service
code read-only and never modifies `services/` or `clients/`. The RAGAS and
`datasets` dependencies live in `eval/requirements.txt` so they stay **out of
the QnA runtime image**.

## What it measures

For each benchmark record the harness assembles a RAGAS sample and scores three
metrics (RAGAS **0.4.3**):

| Metric (reported name)                  | RAGAS class                       | Meaning |
| --------------------------------------- | --------------------------------- | --- |
| `faithfulness`                          | `Faithfulness`                    | Is the answer grounded in the retrieved contexts (no hallucination)? |
| `answer_relevancy`                      | `ResponseRelevancy`               | Does the answer actually address the question? |
| `llm_context_precision_with_reference`  | `LLMContextPrecisionWithReference`| Are the retrieved contexts relevant to the reference answer (ranking quality)? |

Each metric is in `[0, 1]`, higher is better. `faithfulness` and
`llm_context_precision_with_reference` use the `reference` (ground-truth)
field, so benchmark records must include a meaningful `ground_truth`.

## Why contexts come from direct retrieval

The public `/qna` JSON response returns only **citations**
(`{filename: filepath}`) — it deliberately never returns chunk text. RAGAS
faithfulness and context-precision need the actual retrieved chunk *text*, so
the harness re-runs retrieval directly via
`src.services.search_service.perform_search` (the same call the pipeline makes
internally: embed the question, then search with the `fr_<mode>` tag). The
contexts scored are therefore the contexts the answer was grounded on.

The answer itself comes from the real pipeline
(`src.pipeline.qna_pipeline.generate_answer`).

> **Assumption (retrieval approximation, v1).** Because `/qna` returns
> citations only, the scored contexts are obtained by **re-running retrieval**
> rather than capturing the exact chunks used during answer generation. If the
> retrieval configuration, semantic rerank, or fallback behavior differs
> between answer generation and this direct retrieval call, the scored contexts
> may not be byte-identical to those the answer was actually grounded on. This
> is accepted for v1 and documented here as a known approximation.

## Sample / dataset types (RAGAS 0.4.3)

* Sample:  `ragas.SingleTurnSample` — fields `user_input`, `response`,
  `retrieved_contexts`, `reference`.
* Dataset: `ragas.EvaluationDataset`.
* Scoring: `ragas.evaluate(dataset, metrics, llm, embeddings)` returns an
  `EvaluationResult` whose `.scores` is a per-record `list[dict]` aligned with
  the dataset order.

RAGAS is driven by Azure OpenAI via `LangchainLLMWrapper` /
`LangchainEmbeddingsWrapper`, built from the same env vars the QnA service
reads (`AzureConfig` / `LocalConfig`).

## Running it

This is an **offline, CI-controlled, non-blocking** gate. A real run needs
live Azure OpenAI + Cognitive Search env (it calls the model and the index):

```bash
# Canonical UPPER_SNAKE env (same names the QnA service uses):
export AZURE_OPENAI_ENDPOINT=...        AZURE_OPENAI_KEY=...
export AZURE_OPENAI_VERSION=...         AZURE_OPENAI_LLM_MODEL=...
export AZURE_OPENAI_EMBEDDING_MODEL=... AZURE_SEARCH_ENDPOINT=...
export AZURE_SEARCH_KEY=...             INDEX_NAME=...

pip install -r eval/requirements.txt
python -m eval.ragas_eval --benchmark eval/benchmark/sample.jsonl --out eval/out
```

Output: `eval/out/ragas_report.json` and `eval/out/ragas_report.md`
(per-record scores + aggregate means).

A per-record failure (pipeline, retrieval, or sample assembly) is **caught and
recorded** on that record; the run continues and still produces a report.

## Benchmark format

`eval/benchmark/sample.jsonl` — one JSON object per line:

```json
{"question": "...", "ground_truth": "...", "bot_tag": "client_a", "fr_tag": "read"}
```

* `fr_tag` is the bare retrieval mode (`read` or `layout`); the harness builds
  the `fr_<mode>` tag for `perform_search` itself.
* The shipped benchmark is **neutral only** — no real client/company/document
  names.

> **The shipped `eval/benchmark/sample.jsonl` is illustrative only.** Its
> records are synthetic placeholders that make **no claims** about file-size
> limits, supported formats, retention, permissions, or any other product
> behavior — they exist purely to exercise the harness. They are **not
> documentation** and the scores they would produce are meaningless. Replace
> them with real client/workspace-specific eval records (questions paired with
> verified ground-truth answers) before any meaningful scoring.

## Tests

`eval/tests/` are hermetic: `generate_answer`, `get_embedding`,
`perform_search` and `ragas.evaluate` are mocked, so **no live Azure and no
real RAGAS LLM calls** happen. `eval/tests/conftest.py` sets fake Azure env so
importing the QnA config/pipeline (validated at import time) does not fail.

```bash
pip install -r eval/requirements.txt
pytest eval -q
```

## CI note

The CI test/lint matrix currently covers only `services/qna` and
`services/ingestion` (see `.github/workflows/ci.yml`) — it does **not** run
`eval/`. Adding an `eval` leg to the matrix is a follow-up.
