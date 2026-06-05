"""Continuous-eval regression gate (``python -m eval.continuous``).

This is the CI-facing entrypoint that ties the pieces together:

1. Run the RAGAS harness over the benchmark (``eval.ragas_eval.run_eval``).
2. Archive the run into a timestamped history file so trends accumulate.
3. Render the HTML + markdown **trend report** over the whole history
   (``eval.trend_report.write_trend_report``).
4. Compare the run's aggregate to a baseline and **exit non-zero** when any
   metric regressed beyond ``--tolerance``.

Exit-code semantics (deliberately different from ``eval.ragas_eval.main``)
-------------------------------------------------------------------------
``ragas_eval.main`` keeps baseline comparison *informational* — only its
``--min-*`` floors gate. This CLI is the opposite: a **baseline regression is a
failure**. That inversion is the whole point of the continuous gate. Optional
``--min-*`` floors still gate too (a quality floor breach is also a failure), so
the run exits non-zero if EITHER a regression OR a threshold breach occurs.

The baseline defaults to the most recent archived run *before* this one, so the
gate is "did this run regress vs the last run" with no manual baseline wiring.
A regression with no prior history is impossible (nothing to compare), so the
first ever run can only fail on an explicit ``--min-*`` floor.

CLI
---
    python -m eval.continuous \\
        --benchmark eval/benchmark/sample.jsonl \\
        --history eval/history --out eval/out --tolerance 0.02 \\
        --min-faithfulness 0.70
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.ragas_eval import METRIC_NAMES, compare_aggregates, load_baseline_aggregate, run_eval
from eval.trend_report import load_history, write_trend_report


def _utc_now_iso() -> str:
    """Current UTC time as a filesystem-safe ISO-8601 string.

    Colons are replaced with hyphens so the value is usable verbatim in a
    filename across platforms while staying lexically sortable.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def archive_run(payload: dict[str, Any], history_dir: str | Path, timestamp: str | None = None) -> Path:
    """Write ``payload`` (a run report) into the history dir, timestamped.

    Stamps a ``timestamp`` field onto a *copy* of the payload (so the caller's
    dict is untouched) and writes it as ``run-<timestamp>.json``. Returns the
    path written. The timestamp is both embedded and in the filename so
    ``trend_report`` can order runs deterministically.
    """
    ts = timestamp or _utc_now_iso()
    directory = Path(history_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamped = dict(payload)
    stamped["timestamp"] = ts
    path = directory / f"run-{ts}.json"
    path.write_text(json.dumps(stamped, indent=2), encoding="utf-8")
    return path


def _latest_baseline_aggregate(history_dir: str | Path) -> dict[str, float] | None:
    """Aggregate of the most recent archived run, or ``None`` if none exist.

    Used as the implicit baseline so the gate compares each run against the
    previous one without manual baseline wiring.
    """
    points = load_history(history_dir)
    if not points:
        return None
    return dict(points[-1].aggregate)


def detect_regressions(comparison: dict[str, dict[str, Any]]) -> list[str]:
    """Names of metrics flagged ``regressed`` in a comparison mapping."""
    return [metric for metric, c in comparison.items() if c.get("regressed")]


async def run_gate(
    benchmark_path: str | Path,
    out_dir: str | Path,
    history_dir: str | Path,
    *,
    tolerance: float,
    baseline_path: str | Path | None = None,
    thresholds: dict[str, float] | None = None,
    azure: Any | None = None,
    ragas_llm: Any | None = None,
    ragas_embeddings: Any | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Run eval, archive it, render the trend report, and gate.

    Args:
        benchmark_path / out_dir: passed through to ``run_eval``.
        history_dir: directory of archived runs (read for the implicit
            baseline, then the new run is appended to it).
        tolerance: regression epsilon — a metric must drop by MORE than this to
            count as a regression (used as ``compare_aggregates`` epsilon).
        baseline_path: optional explicit prior ``ragas_report.json`` to gate
            against; when omitted the most recent archived run is used.
        thresholds: optional ``{metric: floor}`` quality gate (breach -> fail).
        azure / ragas_llm / ragas_embeddings: injected for tests; built from
            env when omitted (a real run needs live Azure).
        timestamp: injected for deterministic tests; ``utcnow`` when omitted.

    Returns a result dict with ``passed`` (bool), ``regressions`` (list),
    ``threshold_passed`` (bool | None), ``comparison`` (or None), the archived
    run path, and the trend report paths.
    """
    # Resolve the baseline BEFORE archiving the new run, so we compare against
    # prior history, not against the run we are about to write.
    if baseline_path is not None:
        baseline_aggregate = load_baseline_aggregate(baseline_path)
    else:
        baseline_aggregate = _latest_baseline_aggregate(history_dir)

    # Do NOT hand the baseline to run_eval: its comparison uses the default
    # epsilon, which would write a `regressed` flag at a different threshold than
    # the gate uses into the archived run. The gate owns the comparison below at
    # the requested --tolerance, so run_eval stays a pure single-run report.
    payload = await run_eval(
        benchmark_path,
        out_dir,
        azure=azure,
        ragas_llm=ragas_llm,
        ragas_embeddings=ragas_embeddings,
        thresholds=thresholds or None,
    )

    # Compute the regression decision at the requested tolerance.
    comparison: dict[str, dict[str, Any]] | None = None
    regressions: list[str] = []
    if baseline_aggregate is not None:
        comparison = compare_aggregates(payload["aggregate"], baseline_aggregate, epsilon=tolerance)
        regressions = detect_regressions(comparison)

    threshold_passed = payload.get("threshold_passed")

    # Archive THIS run, then render the trend over the full (now-inclusive)
    # history so the latest point appears in the report.
    archived = archive_run(payload, history_dir, timestamp=timestamp)
    html_path, md_path = write_trend_report(history_dir, out_dir, thresholds or None)

    passed = not regressions and threshold_passed is not False
    return {
        "passed": passed,
        "regressions": regressions,
        "threshold_passed": threshold_passed,
        "comparison": comparison,
        "aggregate": payload["aggregate"],
        "archived_run": str(archived),
        "html_report": str(html_path),
        "md_report": str(md_path),
        "json_report": payload.get("json_report"),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog="eval.continuous",
        description="Continuous-eval regression gate: run eval, archive, trend report, gate on regression.",
    )
    parser.add_argument(
        "--benchmark",
        default=str(here / "benchmark" / "sample.jsonl"),
        help="Path to the JSONL benchmark file.",
    )
    parser.add_argument(
        "--out",
        default=str(here / "out"),
        help="Directory for the run report + trend report.",
    )
    parser.add_argument(
        "--history",
        default=str(here / "history"),
        help="Directory of archived runs (implicit baseline source + trend input).",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Explicit prior ragas_report.json to gate against (default: most recent archived run).",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.01,
        metavar="FLOAT",
        help="Max tolerated drop per metric before it counts as a regression (default 0.01).",
    )
    for metric in METRIC_NAMES:
        parser.add_argument(
            f"--min-{metric.replace('_', '-')}",
            dest=f"min_{metric}",
            type=float,
            default=None,
            metavar="FLOAT",
            help=f"Minimum acceptable mean for '{metric}' (breach also fails the gate).",
        )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    thresholds = {
        metric: getattr(args, f"min_{metric}")
        for metric in METRIC_NAMES
        if getattr(args, f"min_{metric}") is not None
    }

    result = asyncio.run(
        run_gate(
            args.benchmark,
            args.out,
            args.history,
            tolerance=args.tolerance,
            baseline_path=args.baseline,
            thresholds=thresholds or None,
        )
    )

    print(json.dumps(result, indent=2))

    # Non-zero on regression OR threshold breach — this is the gate.
    return 0 if result["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
