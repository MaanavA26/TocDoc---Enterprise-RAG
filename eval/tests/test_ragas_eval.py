"""Hermetic tests for the RAGAS eval harness (P4-2).

No live Azure and no real RAGAS LLM calls: ``generate_answer``,
``get_embedding``, ``perform_search`` and ``ragas.evaluate`` are mocked on the
harness module namespace. Asserts the harness:

* assembles each RAGAS sample with the right fields (question/answer/contexts/
  reference);
* calls RAGAS with those samples;
* aggregates per-record scores into means;
* writes the JSON + markdown report;
* records a single failing record and continues the run.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the harness AFTER conftest set the fake env (conftest runs at
# collection, before this module's imports execute the QnA config validation).
from eval import ragas_eval


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _benchmark_file(tmp_path, records):
    path = tmp_path / "bench.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _records():
    return [
        {
            "question": "What is the max file size?",
            "ground_truth": "50 MB per file.",
            "bot_tag": "client_a",
            "fr_tag": "read",
        },
        {
            "question": "How long are docs retained?",
            "ground_truth": "90 days.",
            "bot_tag": "demo_workspace",
            "fr_tag": "layout",
        },
    ]


def _fake_search_results(text):
    return [
        {"content": text, "filename": "doc.md", "filepath": "/d/doc.md"},
        {"content": "", "filename": "empty.md", "filepath": "/d/empty.md"},
    ]


def _fake_scores():
    """Two records' worth of per-metric scores (aligned with dataset order)."""
    return [
        {
            "faithfulness": 1.0,
            "answer_relevancy": 0.8,
            "llm_context_precision_with_reference": 0.6,
        },
        {
            "faithfulness": 0.0,
            "answer_relevancy": 0.4,
            "llm_context_precision_with_reference": 0.2,
        },
    ]


def _patch_evaluate(scores):
    """Build a MagicMock standing in for ragas.evaluate -> EvaluationResult."""
    result = MagicMock()
    result.scores = scores
    return MagicMock(return_value=result)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_assembles_samples_with_right_fields(tmp_path):
    records = _records()
    bench = _benchmark_file(tmp_path, records)

    gen = AsyncMock(side_effect=[{"answer": "A1", "citation": {}}, {"answer": "A2", "citation": {}}])
    emb = AsyncMock(return_value=[0.1, 0.2, 0.3])
    search = AsyncMock(side_effect=[_fake_search_results("ctx1"), _fake_search_results("ctx2")])
    fake_eval = _patch_evaluate(_fake_scores())

    with (
        patch.object(ragas_eval, "generate_answer", gen),
        patch.object(ragas_eval, "get_embedding", emb),
        patch.object(ragas_eval, "perform_search", search),
        patch.object(ragas_eval, "evaluate", fake_eval),
    ):
        payload = _run(
            ragas_eval.run_eval(
                bench,
                tmp_path / "out",
                azure=MagicMock(),
                ragas_llm=MagicMock(),
                ragas_embeddings=MagicMock(),
            )
        )

    # generate_answer called with bare fr_mode; perform_search with fr_<mode>.
    assert gen.await_count == 2
    first_call = gen.await_args_list[0].kwargs
    assert first_call["query"] == "What is the max file size?"
    assert first_call["fr_mode"] == "read"
    assert first_call["bot_tag"] == "client_a"

    # The dataset passed to evaluate carries the assembled samples.
    fake_eval.assert_called_once()
    dataset = fake_eval.call_args.kwargs["dataset"]
    samples = dataset.samples
    assert len(samples) == 2
    assert samples[0].user_input == "What is the max file size?"
    assert samples[0].response == "A1"
    assert samples[0].reference == "50 MB per file."
    # Empty-content chunk filtered out; only the real chunk text kept.
    assert samples[0].retrieved_contexts == ["ctx1"]

    # perform_search got the fr_<mode> tag form, not the bare mode.
    search_call = search.await_args_list[0]
    assert search_call.args[3] == "fr_read"

    assert payload["record_count"] == 2
    assert payload["scored_count"] == 2
    assert payload["error_count"] == 0


def test_aggregates_into_means(tmp_path):
    records = _records()
    bench = _benchmark_file(tmp_path, records)

    gen = AsyncMock(side_effect=[{"answer": "A1"}, {"answer": "A2"}])
    emb = AsyncMock(return_value=[0.1])
    search = AsyncMock(side_effect=[_fake_search_results("c1"), _fake_search_results("c2")])
    fake_eval = _patch_evaluate(_fake_scores())

    with (
        patch.object(ragas_eval, "generate_answer", gen),
        patch.object(ragas_eval, "get_embedding", emb),
        patch.object(ragas_eval, "perform_search", search),
        patch.object(ragas_eval, "evaluate", fake_eval),
    ):
        payload = _run(
            ragas_eval.run_eval(
                bench,
                tmp_path / "out",
                azure=MagicMock(),
                ragas_llm=MagicMock(),
                ragas_embeddings=MagicMock(),
            )
        )

    agg = payload["aggregate"]
    assert agg["faithfulness"] == pytest.approx(0.5)  # (1.0 + 0.0) / 2
    assert agg["answer_relevancy"] == pytest.approx(0.6)  # (0.8 + 0.4) / 2
    assert agg["llm_context_precision_with_reference"] == pytest.approx(0.4)  # (0.6 + 0.2) / 2


def test_writes_json_and_markdown_reports(tmp_path):
    records = _records()
    bench = _benchmark_file(tmp_path, records)
    out = tmp_path / "out"

    gen = AsyncMock(side_effect=[{"answer": "A1"}, {"answer": "A2"}])
    emb = AsyncMock(return_value=[0.1])
    search = AsyncMock(side_effect=[_fake_search_results("c1"), _fake_search_results("c2")])
    fake_eval = _patch_evaluate(_fake_scores())

    with (
        patch.object(ragas_eval, "generate_answer", gen),
        patch.object(ragas_eval, "get_embedding", emb),
        patch.object(ragas_eval, "perform_search", search),
        patch.object(ragas_eval, "evaluate", fake_eval),
    ):
        _run(
            ragas_eval.run_eval(
                bench, out, azure=MagicMock(), ragas_llm=MagicMock(), ragas_embeddings=MagicMock()
            )
        )

    json_path = out / "ragas_report.json"
    md_path = out / "ragas_report.md"
    assert json_path.exists()
    assert md_path.exists()

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["record_count"] == 2
    assert "aggregate" in data
    assert len(data["records"]) == 2

    md = md_path.read_text(encoding="utf-8")
    assert "RAGAS QnA Evaluation Report" in md
    assert "faithfulness" in md


def test_single_failing_record_is_recorded_and_run_continues(tmp_path):
    records = _records()
    bench = _benchmark_file(tmp_path, records)

    # First record's pipeline call raises; second succeeds.
    gen = AsyncMock(side_effect=[RuntimeError("boom"), {"answer": "A2"}])
    emb = AsyncMock(return_value=[0.1])
    search = AsyncMock(side_effect=[_fake_search_results("c2")])
    # Only ONE sample reaches scoring (the second record), so one score row.
    fake_eval = _patch_evaluate([_fake_scores()[1]])

    with (
        patch.object(ragas_eval, "generate_answer", gen),
        patch.object(ragas_eval, "get_embedding", emb),
        patch.object(ragas_eval, "perform_search", search),
        patch.object(ragas_eval, "evaluate", fake_eval),
    ):
        payload = _run(
            ragas_eval.run_eval(
                bench,
                tmp_path / "out",
                azure=MagicMock(),
                ragas_llm=MagicMock(),
                ragas_embeddings=MagicMock(),
            )
        )

    assert payload["record_count"] == 2
    assert payload["error_count"] == 1
    assert payload["scored_count"] == 1

    recs = payload["records"]
    # First record recorded an error and was not scored. The stored error is
    # the exception CLASS name only — never the raw str(exc) ("boom").
    assert recs[0]["error"] == "RuntimeError"
    assert "boom" not in recs[0]["error"]
    assert recs[0]["scores"] == {}
    # Second record scored fine.
    assert recs[1]["error"] is None
    assert recs[1]["scores"]["faithfulness"] == 0.0

    # Only the surviving sample was sent to evaluate.
    dataset = fake_eval.call_args.kwargs["dataset"]
    assert len(dataset.samples) == 1
    assert dataset.samples[0].response == "A2"


def test_raw_exception_text_never_leaks_into_artifacts(tmp_path):
    """A failing record must store the class name only, never str(exc).

    Raw exception text can carry search queries, document snippets, endpoints
    or deployment identifiers. We force an assembly failure whose str(exc)
    contains a sentinel and assert the sentinel is absent from the returned
    payload, the JSON report and the markdown report, while the exception class
    name is present in all three.
    """
    sentinel = "SECRET-QUERY-DETAIL"
    records = _records()
    bench = _benchmark_file(tmp_path, records)
    out = tmp_path / "out"

    # Both records' pipeline calls raise with the sentinel in the message, so
    # nothing reaches scoring and every record carries an assembly error.
    gen = AsyncMock(side_effect=RuntimeError(sentinel))
    emb = AsyncMock(return_value=[0.1])
    search = AsyncMock(return_value=_fake_search_results("ctx"))
    fake_eval = _patch_evaluate(_fake_scores())

    with (
        patch.object(ragas_eval, "generate_answer", gen),
        patch.object(ragas_eval, "get_embedding", emb),
        patch.object(ragas_eval, "perform_search", search),
        patch.object(ragas_eval, "evaluate", fake_eval),
    ):
        payload = _run(
            ragas_eval.run_eval(
                bench,
                out,
                azure=MagicMock(),
                ragas_llm=MagicMock(),
                ragas_embeddings=MagicMock(),
            )
        )

    # Every record errored; none was scored, so evaluate was never called.
    assert payload["error_count"] == 2
    assert payload["scored_count"] == 0
    for rec in payload["records"]:
        assert rec["error"] == "RuntimeError"

    json_text = (out / "ragas_report.json").read_text(encoding="utf-8")
    md_text = (out / "ragas_report.md").read_text(encoding="utf-8")
    payload_text = json.dumps(payload)

    # Sentinel (the raw str(exc)) absent everywhere; class name present.
    for surface in (payload_text, json_text, md_text):
        assert sentinel not in surface
        assert "RuntimeError" in surface


def test_scoring_failure_error_is_class_only(tmp_path):
    """A scoring-stage failure must also store the class name only."""
    sentinel = "SECRET-SCORING-DETAIL"
    records = _records()
    bench = _benchmark_file(tmp_path, records)
    out = tmp_path / "out"

    gen = AsyncMock(side_effect=[{"answer": "A1"}, {"answer": "A2"}])
    emb = AsyncMock(return_value=[0.1])
    search = AsyncMock(side_effect=[_fake_search_results("c1"), _fake_search_results("c2")])
    fake_eval = MagicMock(side_effect=ValueError(sentinel))

    with (
        patch.object(ragas_eval, "generate_answer", gen),
        patch.object(ragas_eval, "get_embedding", emb),
        patch.object(ragas_eval, "perform_search", search),
        patch.object(ragas_eval, "evaluate", fake_eval),
    ):
        payload = _run(
            ragas_eval.run_eval(
                bench,
                out,
                azure=MagicMock(),
                ragas_llm=MagicMock(),
                ragas_embeddings=MagicMock(),
            )
        )

    for rec in payload["records"]:
        assert rec["error"] == "scoring_failed: ValueError"

    json_text = (out / "ragas_report.json").read_text(encoding="utf-8")
    md_text = (out / "ragas_report.md").read_text(encoding="utf-8")
    for surface in (json.dumps(payload), json_text, md_text):
        assert sentinel not in surface
        assert "scoring_failed: ValueError" in surface


def test_metric_names_and_sample_type():
    """Pin the RAGAS 0.4.3 metric names and sample type the harness targets."""
    from ragas import SingleTurnSample

    assert ragas_eval.SingleTurnSample is SingleTurnSample
    assert ragas_eval.METRIC_NAMES == (
        "faithfulness",
        "answer_relevancy",
        "llm_context_precision_with_reference",
    )
    metrics = ragas_eval._build_metrics()
    assert sorted(m.name for m in metrics) == sorted(ragas_eval.METRIC_NAMES)


# ---------------------------------------------------------------------------
# asyncio runner
# ---------------------------------------------------------------------------
def _run(coro):
    import asyncio

    return asyncio.run(coro)
