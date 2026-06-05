"""Hermetic tests for the trend-report generator (continuous-eval).

These tests write fake run-report JSON files into a tmp history dir (no Azure,
no RAGAS) and assert ``trend_report`` loads them in chronological order, renders
HTML + markdown with per-metric trends, surfaces the latest-vs-thresholds block,
and tolerates malformed / aggregate-less files by skipping them.
"""

import json

from eval import trend_report


def _write_run(history_dir, name, aggregate, timestamp=None):
    payload = {"aggregate": aggregate}
    if timestamp is not None:
        payload["timestamp"] = timestamp
    path = history_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_history_orders_by_timestamp(tmp_path):
    # Written out of chronological order; loader must sort oldest -> newest.
    _write_run(tmp_path, "run-b.json", {"faithfulness": 0.6}, timestamp="2026-02-01T00-00-00Z")
    _write_run(tmp_path, "run-a.json", {"faithfulness": 0.5}, timestamp="2026-01-01T00-00-00Z")
    _write_run(tmp_path, "run-c.json", {"faithfulness": 0.7}, timestamp="2026-03-01T00-00-00Z")

    points = trend_report.load_history(tmp_path)
    assert [p.timestamp for p in points] == [
        "2026-01-01T00-00-00Z",
        "2026-02-01T00-00-00Z",
        "2026-03-01T00-00-00Z",
    ]
    assert [p.aggregate["faithfulness"] for p in points] == [0.5, 0.6, 0.7]


def test_load_history_tolerates_missing_timestamp(tmp_path):
    # A file with no timestamp loads (ordered by filename fallback), not crash.
    _write_run(tmp_path, "run-x.json", {"faithfulness": 0.8})  # no timestamp
    points = trend_report.load_history(tmp_path)
    assert len(points) == 1
    assert points[0].timestamp == ""
    assert points[0].label == "run-x.json"  # falls back to filename


def test_load_history_skips_malformed_and_aggregateless(tmp_path):
    _write_run(tmp_path, "good.json", {"faithfulness": 0.5}, timestamp="2026-01-01T00-00-00Z")
    (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")
    (tmp_path / "noagg.json").write_text(json.dumps({"records": []}), encoding="utf-8")
    (tmp_path / "emptyagg.json").write_text(json.dumps({"aggregate": {}}), encoding="utf-8")
    # An aggregate with only non-numeric values contributes no point.
    (tmp_path / "stragg.json").write_text(json.dumps({"aggregate": {"faithfulness": "x"}}), encoding="utf-8")

    points = trend_report.load_history(tmp_path)
    assert len(points) == 1
    assert points[0].filename == "good.json"


def test_load_history_missing_dir_is_empty(tmp_path):
    # A not-yet-created history dir means "no history", not an error.
    assert trend_report.load_history(tmp_path / "nope") == []


def test_load_history_rejects_file_path(tmp_path):
    # A path that exists but is a file (not a directory) is an error.
    a_file = tmp_path / "afile.json"
    a_file.write_text("{}", encoding="utf-8")
    try:
        trend_report.load_history(a_file)
    except NotADirectoryError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("expected NotADirectoryError")


def test_write_trend_report_emits_html_and_markdown(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    out = tmp_path / "out"
    _write_run(history, "run-a.json", {"faithfulness": 0.5, "answer_relevancy": 0.6}, "2026-01-01T00-00-00Z")
    _write_run(history, "run-b.json", {"faithfulness": 0.7, "answer_relevancy": 0.4}, "2026-02-01T00-00-00Z")

    html_path, md_path = trend_report.write_trend_report(history, out, thresholds={"faithfulness": 0.6})
    assert html_path.exists()
    assert md_path.exists()

    html_text = html_path.read_text(encoding="utf-8")
    assert "<svg" in html_text  # inline SVG trend chart present
    assert "RAGAS Evaluation Trend Report" in html_text
    assert "Latest run vs thresholds" in html_text
    # Latest faithfulness 0.7 >= 0.6 -> pass marker rendered.
    assert "pass" in html_text

    md_text = md_path.read_text(encoding="utf-8")
    assert "Per-metric trend" in md_text
    assert "0.7000" in md_text  # latest faithfulness value rendered in the table


def test_write_trend_report_handles_empty_history(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    out = tmp_path / "out"
    html_path, md_path = trend_report.write_trend_report(history, out)
    assert "No usable run reports" in html_path.read_text(encoding="utf-8")
    assert "No usable run reports" in md_path.read_text(encoding="utf-8")


def test_render_svg_single_point_has_no_line(tmp_path):
    # One point should render a marker but no <path> polyline.
    from eval.trend_report import RunPoint, _render_svg

    series = [(RunPoint(timestamp="t", filename="f"), 0.5)]
    svg = _render_svg(series, "faithfulness")
    assert "<circle" in svg
    assert "<path" not in svg


def test_render_svg_clamps_out_of_range_values():
    from eval.trend_report import RunPoint, _render_svg

    series = [
        (RunPoint(timestamp="t1", filename="f1"), -0.5),  # below 0
        (RunPoint(timestamp="t2", filename="f2"), 1.5),  # above 1
    ]
    svg = _render_svg(series, "faithfulness")
    # Two clamped points -> a connecting polyline is drawn; no crash on range.
    assert "<path" in svg
    assert "<svg" in svg


def test_markdown_threshold_pass_and_fail_markers(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    out = tmp_path / "out"
    # Latest run: faithfulness 0.5 (below 0.6 -> no), answer_relevancy 0.9 (>=0.8 -> yes).
    _write_run(
        history,
        "run-a.json",
        {"faithfulness": 0.5, "answer_relevancy": 0.9},
        "2026-01-01T00-00-00Z",
    )
    _, md_path = trend_report.write_trend_report(
        history, out, thresholds={"faithfulness": 0.6, "answer_relevancy": 0.8}
    )
    md = md_path.read_text(encoding="utf-8")
    # The threshold table contains both a "no" and a "yes" outcome.
    assert "| no |" in md
    assert "| yes |" in md
