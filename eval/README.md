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
(per-record scores, aggregate means, and a per-metric mean/min/max summary).

A per-record failure (pipeline, retrieval, or sample assembly) is **caught and
recorded** on that record; the run continues and still produces a report.

## Report contents

The JSON report always carries:

* `aggregate` — flat `{metric: mean}` over scored records. **Stable shape**: the
  `--baseline` reader and downstream tooling depend on it, so min/max are kept
  out of this key.
* `summary` — per-metric `{mean, min, max, count}` (the richer view, also
  rendered as a "Summary (mean / min / max)" table in the markdown report).

## Baseline comparison (`--baseline`, off by default)

Diff the current run's `aggregate` against a prior `ragas_report.json`:

```bash
python -m eval.ragas_eval --benchmark eval/benchmark/sample.jsonl \
    --out eval/out --baseline path/to/previous/ragas_report.json
```

This adds a `comparison` object (`{metric: {baseline, current, delta,
regressed}}`) to the JSON and a "Baseline comparison" table to the markdown. A
metric is flagged `regressed` only when it dropped by more than a small epsilon
(`1e-4`, to swallow float noise) and exists in **both** runs; a metric present
in only one run reports `delta=null` and is never flagged. Baseline comparison
is **informational only** — it does **not** change the process exit code.

## Threshold gating (`--min-<metric>`, off by default)

Enforce an absolute floor on any metric's mean. There is one auto-generated flag
per metric (derived from the scored metric names):

```bash
python -m eval.ragas_eval --benchmark eval/benchmark/sample.jsonl \
    --out eval/out \
    --min-faithfulness 0.70 \
    --min-answer-relevancy 0.60 \
    --min-llm-context-precision-with-reference 0.50
```

Each supplied flag adds an entry to the `threshold_gate` object and the
"Threshold gate" markdown table. If **any** gated metric's mean is below its
floor (or has no value to check), the process **exits non-zero (1)** so CI can
gate on quality. With no `--min-*` flags the run always exits `0`, and the
default report shape is unchanged. Thresholds and `--baseline` are independent
and may be combined.

## Continuous eval: regression gate + trend report (`python -m eval.continuous`)

`eval.continuous` is the CI-facing wrapper that turns a single run into a
**regression gate** and accumulates **trends over time**. In one invocation it:

1. runs the RAGAS harness over the benchmark (`run_eval`);
2. **archives** the run into a history dir as `run-<ISO8601>.json` (timestamp
   stamped both in the filename and in the payload);
3. renders an **HTML + markdown trend report** over the whole history; and
4. **exits non-zero** if any metric regressed beyond `--tolerance`, or if an
   optional `--min-<metric>` floor is breached.

```bash
python -m eval.continuous \
    --benchmark eval/benchmark/sample.jsonl \
    --history eval/history --out eval/out \
    --tolerance 0.02 \
    --min-faithfulness 0.70
```

The baseline defaults to **the most recent archived run before this one**, so
the gate answers "did this run regress versus the last run" with no manual
baseline wiring (pass `--baseline path/to/ragas_report.json` to override). The
first ever run has no prior history and so cannot regress — it can only fail on
an explicit `--min-<metric>` floor.

> **Exit-code contract differs from `ragas_eval`.** `ragas_eval.main` keeps
> baseline comparison **informational** (exit 0) and gates only on `--min-*`
> floors. `eval.continuous` is the opposite: a **regression is a failure**. That
> inversion is the point of the continuous gate. A threshold breach also fails
> the gate, so the run exits non-zero if **either** a regression or a floor
> breach occurs.

### Trend report (`python -m eval.trend_report`)

The trend generator can also be run on its own against a history dir:

```bash
python -m eval.trend_report --history eval/history --out eval/out \
    --min-faithfulness 0.70
```

It writes `eval/out/trend_report.html` and `eval/out/trend_report.md`:

* **HTML** — one inline `<svg>` line chart per metric (y-axis pinned to the
  RAGAS `[0, 1]` range), a values table, and a "latest run vs thresholds" block.
* **Markdown** — the per-metric trend table and the latest-vs-thresholds table.

It is **pure standard library** (no matplotlib/plotly/pandas), so it adds
**nothing** to `eval/requirements.txt`. Runs are ordered oldest→newest by the
embedded `timestamp` (filename, then mtime, as fallbacks); files that are not
valid JSON or carry no usable `aggregate` are skipped rather than aborting the
report.

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

CI runs `eval/` as its own leg (see the `test (eval)` job in
`.github/workflows/ci.yml`): it installs this standalone requirements set and
runs `pytest eval -q`, kept off the fast `{qna, ingestion}` critical path so the
heavier ragas/datasets install never blocks them. `eval/` is also linted and
format-checked by the `lint (ruff)` job. The eval tests are a **real gate** (no
`continue-on-error`): a failing eval test fails the run.
