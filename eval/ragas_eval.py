"""RAGAS offline evaluation harness for the TocDoc QnA service (P4-2).

What this does
--------------
For each benchmark record it assembles a RAGAS ``SingleTurnSample``:

* ``response``  — the QnA answer text, produced by the real pipeline
  (``src.pipeline.qna_pipeline.generate_answer``).
* ``retrieved_contexts`` — the chunk **text** that grounded the answer,
  obtained by calling the retrieval layer directly
  (``src.services.search_service.perform_search``).
* ``user_input`` — the benchmark question.
* ``reference`` — the benchmark ground-truth answer.

Why contexts come from direct retrieval
---------------------------------------
The public ``/qna`` JSON response returns only **citations**
(``{filename: filepath}``) — it deliberately never returns chunk text (see the
``generate_answer`` payload contract). RAGAS faithfulness / context-precision
need the actual retrieved chunk *text*, so the harness re-runs retrieval
directly via ``perform_search`` (the same call the pipeline makes internally)
to obtain the grounding chunks. This mirrors the pipeline's own retrieval path
(``get_embedding`` -> ``perform_search`` with the ``fr_<mode>`` tag) so the
contexts scored are the contexts the answer was grounded on.

RAGAS 0.4.3 API used
--------------------
* Sample:  ``ragas.SingleTurnSample`` (fields ``user_input`` / ``response`` /
  ``retrieved_contexts`` / ``reference``).
* Dataset: ``ragas.EvaluationDataset``.
* ``ragas.evaluate(dataset, metrics, llm, embeddings)`` -> ``EvaluationResult``
  whose ``.scores`` is a per-record list of ``{metric_name: float}`` aligned
  with the dataset order.
* Metrics (the classic objects that work with ``evaluate``):
    - ``Faithfulness``                      (name ``faithfulness``)
    - ``ResponseRelevancy``                 (name ``answer_relevancy``)
    - ``LLMContextPrecisionWithReference``  (name
      ``llm_context_precision_with_reference``)
* RAGAS is driven by Azure OpenAI via ``LangchainLLMWrapper`` /
  ``LangchainEmbeddingsWrapper`` built from the same env the QnA service reads.

Offline / CI nature
-------------------
This is an **offline, CI-controlled, non-blocking** quality gate. A real run
needs live Azure OpenAI + Cognitive Search env (it calls the model and the
index). The unit tests are hermetic: they mock the pipeline, retrieval, and
``ragas.evaluate`` so no Azure or LLM call happens.

CLI
---
    python -m eval.ragas_eval --benchmark eval/benchmark/sample.jsonl \\
        --out eval/out
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Make the QnA service importable read-only.
#
# The QnA service code imports its own modules as the top-level ``src`` package
# (e.g. ``from src.config.config import ...``). To import the pipeline without
# duplicating that package under a second name (which would create divergent
# config singletons), we put ``services/qna`` on sys.path and import via
# ``src.*`` — the service's own convention. NOTHING in services/ is modified.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_QNA_ROOT = _REPO_ROOT / "services" / "qna"
if str(_QNA_ROOT) not in sys.path:
    sys.path.insert(0, str(_QNA_ROOT))

# RAGAS 0.4.3 imports plus QnA service code (imported read-only). All sit below
# the sys.path tweak above (hence E402), so isort keeps them in one block.
#
# QnA service code: the pipeline + retrieval + client builders. Tests patch
# these names on THIS module, so they're imported into the module namespace.
#
# RAGAS classic metric objects (``ragas.metrics``) are the ones compatible with
# ``ragas.evaluate``; the ``ragas.metrics.collections`` variants use a
# different ``.score()`` API that does not plug into ``evaluate``. The classic
# import emits a DeprecationWarning pointing at the collections module — we
# suppress it narrowly below because the evaluate-compatible objects are the
# documented path for batch scoring in 0.4.3.
from ragas import EvaluationDataset, SingleTurnSample, evaluate  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from src.clients.azure_clients import AzureOpenAIHandler  # noqa: E402
from src.config.config import AzureConfig, LocalConfig  # noqa: E402
from src.pipeline.qna_pipeline import generate_answer  # noqa: E402
from src.services.embedding_service import get_embedding  # noqa: E402
from src.services.search_service import perform_search  # noqa: E402

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from ragas.metrics import (  # noqa: E402
        Faithfulness,
        LLMContextPrecisionWithReference,
        ResponseRelevancy,
    )

# Canonical metric names as RAGAS reports them in EvaluationResult.scores.
METRIC_NAMES = (
    "faithfulness",
    "answer_relevancy",
    "llm_context_precision_with_reference",
)


def _build_metrics() -> list[Any]:
    """Construct the RAGAS 0.4.3 metric objects scored by this harness.

    Returns the classic metric instances that work with ``ragas.evaluate``:
    faithfulness, answer (response) relevancy, and context precision with
    reference. The LLM / embeddings are injected by ``evaluate`` at run time.
    """
    return [
        Faithfulness(),
        ResponseRelevancy(),
        LLMContextPrecisionWithReference(),
    ]


@dataclass
class RecordResult:
    """Per-record outcome: the assembled sample plus scores or an error."""

    question: str
    bot_tag: str
    fr_tag: str
    answer: str | None = None
    contexts: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "bot_tag": self.bot_tag,
            "fr_tag": self.fr_tag,
            "answer": self.answer,
            "context_count": len(self.contexts),
            "scores": self.scores,
            "error": self.error,
        }


def load_benchmark(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSONL benchmark file into a list of record dicts.

    Each line must be a JSON object with ``question``, ``ground_truth``,
    ``bot_tag`` and ``fr_tag``. Blank lines are skipped.
    """
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no} of {path}: {exc}") from exc
            records.append(rec)
    return records


async def _assemble_sample(record: dict[str, Any], azure: Any) -> tuple[SingleTurnSample, list[str]]:
    """Run the QnA pipeline + direct retrieval to build one RAGAS sample.

    ``response`` comes from ``generate_answer`` (the real pipeline answer).
    ``retrieved_contexts`` come from a direct ``perform_search`` call (the
    pipeline path: embed the question, then search with the ``fr_<mode>`` tag),
    because the public /qna payload returns citations only, not chunk text.
    """
    question = record["question"]
    ground_truth = record.get("ground_truth", "")
    bot_tag = record["bot_tag"]
    fr_mode = record.get("fr_tag", "read")

    # 1) Answer via the real pipeline. generate_answer expects the bare
    #    fr_mode ("read"/"layout").
    answer_payload = await generate_answer(
        query=question,
        fr_mode=fr_mode,
        bot_tag=bot_tag,
        history=[],
        azure=azure,
    )
    answer = answer_payload.get("answer", "")

    # 2) Contexts via direct retrieval. perform_search needs the embedding
    #    vector and the "fr_<mode>" tag form the pipeline builds internally.
    vector = await get_embedding(azure, question)
    fr_mode_tag = f"fr_{fr_mode}"
    results = await perform_search(azure, question, vector, fr_mode_tag, bot_tag)
    contexts = [r["content"] for r in results if r.get("content")]

    sample = SingleTurnSample(
        user_input=question,
        response=answer,
        retrieved_contexts=contexts,
        reference=ground_truth,
    )
    return sample, contexts


def _build_ragas_clients() -> tuple[Any, Any]:
    """Build the Azure-OpenAI-backed RAGAS LLM + embeddings wrappers.

    Mirrors how the QnA service constructs its Azure clients (same env vars via
    ``AzureConfig`` / ``LocalConfig``), wrapping LangChain Azure clients in the
    RAGAS wrappers so ``evaluate`` can drive them.
    """
    from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings

    azure_cfg = AzureConfig()
    local_cfg = LocalConfig()

    chat = AzureChatOpenAI(
        azure_endpoint=azure_cfg.AZURE_OPENAI_ENDPOINT,
        api_key=azure_cfg.AZURE_OPENAI_KEY,
        api_version=azure_cfg.AZURE_OPENAI_API_VERSION,
        azure_deployment=local_cfg.AZURE_LLM_MODEL,
        temperature=0.0,
    )
    embeddings = AzureOpenAIEmbeddings(
        azure_endpoint=azure_cfg.AZURE_OPENAI_ENDPOINT,
        api_key=azure_cfg.AZURE_OPENAI_KEY,
        api_version=azure_cfg.AZURE_OPENAI_API_VERSION,
        model=local_cfg.AZURE_OPENAI_EMBEDDING_MODEL,
    )
    return LangchainLLMWrapper(chat), LangchainEmbeddingsWrapper(embeddings)


def _score_dataset(
    samples: list[SingleTurnSample],
    ragas_llm: Any,
    ragas_embeddings: Any,
) -> list[dict[str, float]]:
    """Score the assembled samples with RAGAS and return per-record scores.

    Returns ``EvaluationResult.scores`` — a list aligned with ``samples`` where
    each entry maps metric name -> float. Returns ``[]`` for an empty input.
    """
    if not samples:
        return []
    dataset = EvaluationDataset(samples=samples)
    result = evaluate(
        dataset=dataset,
        metrics=_build_metrics(),
        llm=ragas_llm,
        embeddings=ragas_embeddings,
    )
    # EvaluationResult.scores is a list[dict] aligned with the dataset order.
    return [dict(s) for s in result.scores]


def _aggregate(results: list[RecordResult]) -> dict[str, float]:
    """Mean of each metric across records that scored successfully.

    Records with an error (no scores) are excluded from the means.
    """
    means: dict[str, float] = {}
    for metric in METRIC_NAMES:
        vals = [
            r.scores[metric]
            for r in results
            if metric in r.scores and isinstance(r.scores[metric], (int, float))
        ]
        if vals:
            means[metric] = sum(vals) / len(vals)
    return means


def _write_reports(
    results: list[RecordResult], aggregate: dict[str, float], out_dir: str | Path
) -> tuple[Path, Path]:
    """Write the JSON + markdown reports. Returns (json_path, md_path)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    json_path = out / "ragas_report.json"
    md_path = out / "ragas_report.md"

    payload = {
        "record_count": len(results),
        "scored_count": sum(1 for r in results if r.scores),
        "error_count": sum(1 for r in results if r.error),
        "aggregate": aggregate,
        "records": [r.to_dict() for r in results],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# RAGAS QnA Evaluation Report")
    lines.append("")
    lines.append(
        f"Records: {payload['record_count']} | "
        f"Scored: {payload['scored_count']} | "
        f"Errors: {payload['error_count']}"
    )
    lines.append("")
    lines.append("## Aggregate (mean over scored records)")
    lines.append("")
    if aggregate:
        lines.append("| Metric | Mean |")
        lines.append("| --- | --- |")
        for metric in METRIC_NAMES:
            if metric in aggregate:
                lines.append(f"| {metric} | {aggregate[metric]:.4f} |")
    else:
        lines.append("_No records scored successfully._")
    lines.append("")
    lines.append("## Per-record")
    lines.append("")
    lines.append("| # | bot_tag | fr_tag | " + " | ".join(METRIC_NAMES) + " | error |")
    lines.append("| --- | --- | --- | " + " | ".join(["---"] * len(METRIC_NAMES)) + " | --- |")
    for i, r in enumerate(results, start=1):
        cells = []
        for metric in METRIC_NAMES:
            val = r.scores.get(metric)
            cells.append(f"{val:.4f}" if isinstance(val, (int, float)) else "-")
        err = r.error or ""
        lines.append(f"| {i} | {r.bot_tag} | {r.fr_tag} | " + " | ".join(cells) + f" | {err} |")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    return json_path, md_path


async def run_eval(
    benchmark_path: str | Path,
    out_dir: str | Path,
    azure: Any | None = None,
    ragas_llm: Any | None = None,
    ragas_embeddings: Any | None = None,
) -> dict[str, Any]:
    """Run the full harness: assemble samples, score, aggregate, write reports.

    A failure on any single record (pipeline, retrieval, or sample assembly) is
    caught and recorded on that record; the run continues for the rest. The
    scoring step is run once over all successfully-assembled samples.

    Args:
        benchmark_path: Path to the JSONL benchmark.
        out_dir: Directory for the JSON + markdown reports.
        azure: QnA Azure client holder; built from env if omitted.
        ragas_llm / ragas_embeddings: RAGAS Azure wrappers; built from env if
            omitted.

    Returns:
        The report payload dict (also written to disk).
    """
    records = load_benchmark(benchmark_path)

    if azure is None:
        azure = AzureOpenAIHandler()
        azure._ensure_client()

    results: list[RecordResult] = []
    scorable: list[tuple[int, SingleTurnSample]] = []

    for record in records:
        if not isinstance(record, dict):
            # A valid-JSON line that isn't an object (e.g. a list/scalar) would
            # crash record.get(...); isolate it as an errored record instead of
            # aborting the whole run.
            results.append(RecordResult(question="", bot_tag="", fr_tag="read", error="invalid_record"))
            continue
        rr = RecordResult(
            question=record.get("question", ""),
            bot_tag=record.get("bot_tag", ""),
            fr_tag=record.get("fr_tag", "read"),
        )
        try:
            sample, contexts = await _assemble_sample(record, azure)
            rr.answer = sample.response
            rr.contexts = contexts
            scorable.append((len(results), sample))
        except Exception as exc:  # noqa: BLE001 - per-record isolation by design
            # Store only the exception CLASS name. Raw str(exc) can leak search
            # queries, document snippets, endpoints or deployment identifiers
            # into the JSON/markdown report artifacts, so it must never be
            # persisted or reported.
            rr.error = type(exc).__name__
        results.append(rr)

    # Score all successfully-assembled samples in one evaluate() call.
    if scorable:
        if ragas_llm is None or ragas_embeddings is None:
            ragas_llm, ragas_embeddings = _build_ragas_clients()
        try:
            per_record_scores = _score_dataset([s for _, s in scorable], ragas_llm, ragas_embeddings)
            # Defensive: if evaluate() returns fewer rows than samples, never
            # silently drop a record — mark any unscored ones with an error so
            # every scorable record ends with either scores or an error.
            for i, (idx, _sample) in enumerate(scorable):
                if i < len(per_record_scores):
                    results[idx].scores = {
                        k: v for k, v in per_record_scores[i].items() if isinstance(v, (int, float))
                    }
                elif not results[idx].error:
                    results[idx].error = "score_count_mismatch"
        except Exception as exc:  # noqa: BLE001 - scoring failure must not abort
            # Record the scoring failure on every record that was meant to be
            # scored, so the run still produces a report.
            for idx, _sample in scorable:
                if not results[idx].error:
                    # Class name only — never the raw str(exc) (see above).
                    results[idx].error = f"scoring_failed: {type(exc).__name__}"

    aggregate = _aggregate(results)
    json_path, md_path = _write_reports(results, aggregate, out_dir)

    payload = {
        "record_count": len(results),
        "scored_count": sum(1 for r in results if r.scores),
        "error_count": sum(1 for r in results if r.error),
        "aggregate": aggregate,
        "records": [r.to_dict() for r in results],
        "json_report": str(json_path),
        "md_report": str(md_path),
    }
    return payload


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eval.ragas_eval",
        description="Offline RAGAS evaluation harness for the TocDoc QnA service.",
    )
    parser.add_argument(
        "--benchmark",
        default=str(Path(__file__).resolve().parent / "benchmark" / "sample.jsonl"),
        help="Path to the JSONL benchmark file.",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "out"),
        help="Directory to write the JSON + markdown reports into.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = asyncio.run(run_eval(args.benchmark, args.out))
    print(
        json.dumps(
            {
                "record_count": payload["record_count"],
                "scored_count": payload["scored_count"],
                "error_count": payload["error_count"],
                "aggregate": payload["aggregate"],
                "json_report": payload["json_report"],
                "md_report": payload["md_report"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
