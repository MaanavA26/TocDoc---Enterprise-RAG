"""Hermetic tests for the continuous-eval regression gate.

The real ``run_eval`` is exercised with the QnA pipeline / retrieval / RAGAS
``evaluate`` mocked on the ``ragas_eval`` module (no Azure, no LLM), so the gate
runs end to end against deterministic scores. Tests assert: archiving stamps a
timestamp, the implicit baseline is the previous archived run, regression and
threshold breaches drive a non-zero exit, and clean runs exit zero.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from eval import continuous, ragas_eval


def _run(coro):
    return asyncio.run(coro)


def _records():
    return [
        {"question": "q1", "ground_truth": "g1", "bot_tag": "client_a", "fr_tag": "read"},
        {"question": "q2", "ground_truth": "g2", "bot_tag": "demo_workspace", "fr_tag": "layout"},
    ]


def _benchmark_file(tmp_path, records=None):
    records = records or _records()
    path = tmp_path / "bench.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _fake_search_results(text):
    return [{"content": text, "filename": "doc.md", "filepath": "/d/doc.md"}]


def _scores(faithfulness_pair):
    """Per-record scores; only faithfulness varies for the assertions."""
    a, b = faithfulness_pair
    return [
        {"faithfulness": a, "answer_relevancy": 0.8, "llm_context_precision_with_reference": 0.6},
        {"faithfulness": b, "answer_relevancy": 0.8, "llm_context_precision_with_reference": 0.6},
    ]


def _patch_pipeline(faithfulness_pair):
    """Context-manager bundle mocking the pipeline + RAGAS evaluate."""
    gen = AsyncMock(side_effect=[{"answer": "A1"}, {"answer": "A2"}])
    emb = AsyncMock(return_value=[0.1])
    search = AsyncMock(side_effect=[_fake_search_results("c1"), _fake_search_results("c2")])
    result = MagicMock()
    result.scores = _scores(faithfulness_pair)
    fake_eval = MagicMock(return_value=result)
    return gen, emb, search, fake_eval


def _gate(
    tmp_path, *, faithfulness_pair, tolerance=0.01, thresholds=None, baseline_path=None, timestamp=None
):
    bench = _benchmark_file(tmp_path)
    gen, emb, search, fake_eval = _patch_pipeline(faithfulness_pair)
    with (
        patch.object(ragas_eval, "generate_answer", gen),
        patch.object(ragas_eval, "get_embedding", emb),
        patch.object(ragas_eval, "perform_search", search),
        patch.object(ragas_eval, "evaluate", fake_eval),
    ):
        return _run(
            continuous.run_gate(
                bench,
                tmp_path / "out",
                tmp_path / "history",
                tolerance=tolerance,
                baseline_path=baseline_path,
                thresholds=thresholds,
                azure=MagicMock(),
                ragas_llm=MagicMock(),
                ragas_embeddings=MagicMock(),
                timestamp=timestamp,
            )
        )


def test_archive_run_stamps_timestamp_and_filename(tmp_path):
    payload = {"aggregate": {"faithfulness": 0.5}}
    path = continuous.archive_run(payload, tmp_path / "history", timestamp="2026-01-01T00-00-00Z")
    assert path.name == "run-2026-01-01T00-00-00Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["timestamp"] == "2026-01-01T00-00-00Z"
    # Caller's dict is not mutated.
    assert "timestamp" not in payload


def test_first_run_no_baseline_passes(tmp_path):
    # No prior history -> no comparison -> cannot regress; clean exit.
    result = _gate(tmp_path, faithfulness_pair=(0.5, 0.5), timestamp="2026-01-01T00-00-00Z")
    assert result["comparison"] is None
    assert result["regressions"] == []
    assert result["passed"] is True
    # The run was archived and the trend report rendered.
    assert (tmp_path / "history" / "run-2026-01-01T00-00-00Z.json").exists()
    assert (tmp_path / "out" / "trend_report.html").exists()


def test_regression_vs_previous_run_fails(tmp_path):
    # Run 1: faithfulness mean 0.9. Run 2: mean 0.5 -> big drop -> regression.
    _gate(tmp_path, faithfulness_pair=(0.9, 0.9), timestamp="2026-01-01T00-00-00Z")
    result2 = _gate(tmp_path, faithfulness_pair=(0.5, 0.5), timestamp="2026-02-01T00-00-00Z")
    assert "faithfulness" in result2["regressions"]
    assert result2["passed"] is False


def test_drop_within_tolerance_is_not_regression(tmp_path):
    # Run 1 mean 0.90, run 2 mean 0.895 -> 0.005 drop, under tolerance 0.02.
    _gate(tmp_path, faithfulness_pair=(0.90, 0.90), timestamp="2026-01-01T00-00-00Z")
    result2 = _gate(
        tmp_path, faithfulness_pair=(0.895, 0.895), tolerance=0.02, timestamp="2026-02-01T00-00-00Z"
    )
    assert result2["regressions"] == []
    assert result2["passed"] is True


def test_improvement_is_not_regression(tmp_path):
    _gate(tmp_path, faithfulness_pair=(0.5, 0.5), timestamp="2026-01-01T00-00-00Z")
    result2 = _gate(tmp_path, faithfulness_pair=(0.9, 0.9), timestamp="2026-02-01T00-00-00Z")
    assert result2["regressions"] == []
    assert result2["passed"] is True


def test_threshold_breach_fails_even_without_regression(tmp_path):
    # Single run (no baseline), faithfulness mean 0.5, floor 0.6 -> breach.
    result = _gate(
        tmp_path,
        faithfulness_pair=(0.5, 0.5),
        thresholds={"faithfulness": 0.6},
        timestamp="2026-01-01T00-00-00Z",
    )
    assert result["regressions"] == []
    assert result["threshold_passed"] is False
    assert result["passed"] is False


def test_explicit_baseline_path_is_used(tmp_path):
    # An explicit prior ragas_report.json (mean 0.9) beats the empty history.
    baseline = tmp_path / "prev.json"
    baseline.write_text(json.dumps({"aggregate": {"faithfulness": 0.9}}), encoding="utf-8")
    result = _gate(
        tmp_path,
        faithfulness_pair=(0.5, 0.5),
        baseline_path=baseline,
        timestamp="2026-01-01T00-00-00Z",
    )
    assert "faithfulness" in result["regressions"]
    assert result["passed"] is False


def test_detect_regressions_pure():
    comparison = {
        "faithfulness": {"regressed": True},
        "answer_relevancy": {"regressed": False},
    }
    assert continuous.detect_regressions(comparison) == ["faithfulness"]


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------
def _cli(tmp_path, argv_extra, faithfulness_pair):
    bench = _benchmark_file(tmp_path)
    gen, emb, search, fake_eval = _patch_pipeline(faithfulness_pair)
    argv = [
        "--benchmark",
        str(bench),
        "--out",
        str(tmp_path / "out"),
        "--history",
        str(tmp_path / "history"),
        *argv_extra,
    ]
    with (
        patch.object(ragas_eval, "generate_answer", gen),
        patch.object(ragas_eval, "get_embedding", emb),
        patch.object(ragas_eval, "perform_search", search),
        patch.object(ragas_eval, "evaluate", fake_eval),
    ):
        return continuous.main(argv)


def test_cli_clean_run_exits_zero(tmp_path):
    rc = _cli(tmp_path, [], faithfulness_pair=(0.8, 0.8))
    assert rc == 0


def test_cli_threshold_breach_exits_nonzero(tmp_path):
    rc = _cli(tmp_path, ["--min-faithfulness", "0.9"], faithfulness_pair=(0.5, 0.5))
    assert rc == 1
