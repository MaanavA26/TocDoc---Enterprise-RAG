"""Trend-report generator for the RAGAS eval harness (continuous-eval).

Given a directory of historical RAGAS run reports, this module renders a
per-metric *trend over time* as an HTML page and a markdown summary. It depends
only on the Python standard library — no plotting library, no pandas — so it
adds nothing to ``eval/requirements.txt``. Charts are emitted as hand-built
inline ``<svg>`` (HTML) and plain tables (markdown).

History file contract
---------------------
Each historical run is a JSON file produced by ``eval.continuous`` (which wraps
``eval.ragas_eval.run_eval``). The fields this module reads are:

* ``aggregate`` — flat ``{metric: mean}`` mapping (the stable shape that
  ``ragas_eval`` already writes and that ``--baseline`` diffs against).
* ``timestamp`` — ISO-8601 UTC string stamped by ``eval.continuous`` when it
  archives the run. **Optional**: a file without it still loads; it is ordered
  after timestamped files using its filename, then its mtime, as a fallback so
  trends never silently reorder.

Files that are not valid JSON, or that carry no usable ``aggregate`` object, are
skipped (they cannot contribute a point to any trend) rather than aborting the
whole report.

Ordering
--------
Runs are ordered oldest -> newest by ``(timestamp, filename)`` so the rendered
trend reads left-to-right in time. A missing timestamp sorts *before* any
present one (it is treated as the empty string), with the filename — which
``eval.continuous`` stamps as ``run-<ISO8601>.json`` — and finally the file
mtime as deterministic tie-breakers.

CLI
---
    python -m eval.trend_report --history eval/history --out eval/out
"""

from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass, field
from pathlib import Path

# Canonical metric names, imported from the eval harness so the trend report
# stays in lock-step with the scored metrics (single source of truth).
from eval.ragas_eval import METRIC_NAMES

# Filename glob for archived run reports. ``eval.continuous`` writes
# ``run-<ISO8601>.json``; we also accept any ``*.json`` so an externally
# produced ``ragas_report.json`` dropped into the history dir is picked up.
_RUN_GLOB = "*.json"


@dataclass
class RunPoint:
    """One historical run reduced to what the trend report needs."""

    timestamp: str  # ISO-8601 string, or "" when the file had none
    filename: str
    aggregate: dict[str, float] = field(default_factory=dict)
    # Stored only as a last-resort, deterministic ordering tie-breaker.
    _mtime: float = 0.0

    @property
    def label(self) -> str:
        """Short x-axis label: the timestamp if present, else the filename."""
        return self.timestamp or self.filename


def load_history(history_dir: str | Path) -> list[RunPoint]:
    """Load every usable run report in ``history_dir`` ordered oldest->newest.

    A file is *usable* when it is valid JSON whose ``aggregate`` is an object
    with at least one numeric metric. Unusable files (bad JSON, no aggregate)
    are skipped, never raised on, so one stray file cannot break the report.

    Ordering key is ``(timestamp, filename, mtime)`` ascending; a missing
    timestamp sorts first (treated as ""), so timestamped runs always read in
    chronological order and any untimestamped file lands deterministically.
    """
    directory = Path(history_dir)
    if not directory.exists():
        # A not-yet-created history dir simply means "no history" — the first
        # continuous run legitimately has none. Return empty rather than raise.
        return []
    if not directory.is_dir():
        raise NotADirectoryError(f"History path is not a directory: {history_dir}")

    points: list[RunPoint] = []
    for path in sorted(directory.glob(_RUN_GLOB)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        agg = data.get("aggregate")
        if not isinstance(agg, dict):
            continue
        numeric = {k: v for k, v in agg.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if not numeric:
            continue
        ts = data.get("timestamp")
        timestamp = ts if isinstance(ts, str) else ""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        points.append(RunPoint(timestamp=timestamp, filename=path.name, aggregate=numeric, _mtime=mtime))

    points.sort(key=lambda p: (p.timestamp, p.filename, p._mtime))
    return points


def _metric_series(points: list[RunPoint], metric: str) -> list[tuple[RunPoint, float]]:
    """The ``(run, value)`` pairs that have a value for ``metric``, in order."""
    return [(p, p.aggregate[metric]) for p in points if metric in p.aggregate]


# --- SVG sparkline ---------------------------------------------------------
# A tiny, self-contained line chart. RAGAS scores live in [0, 1], so the y-axis
# is fixed to that range, which keeps every metric on the same comparable scale
# and avoids a misleading auto-zoomed axis.
_SVG_W = 480
_SVG_H = 120
_SVG_PAD = 24


def _render_svg(series: list[tuple[RunPoint, float]], metric: str) -> str:
    """Render one metric's series as an inline SVG line chart (string).

    The y-axis is pinned to ``[0, 1]`` (the RAGAS score range). With zero points
    a placeholder note is returned; with one point a single marker is drawn
    (no line). All text is escaped — labels derive from file/timestamp data.
    """
    title = html.escape(metric)
    if not series:
        return f'<svg role="img" aria-label="{title}: no data" width="{_SVG_W}" height="{_SVG_H}"></svg>'

    plot_w = _SVG_W - 2 * _SVG_PAD
    plot_h = _SVG_H - 2 * _SVG_PAD
    n = len(series)

    def x_for(i: int) -> float:
        if n == 1:
            return _SVG_PAD + plot_w / 2
        return _SVG_PAD + plot_w * i / (n - 1)

    def y_for(value: float) -> float:
        clamped = min(1.0, max(0.0, value))
        return _SVG_PAD + plot_h * (1.0 - clamped)

    pts = [(x_for(i), y_for(v)) for i, (_run, v) in enumerate(series)]

    parts: list[str] = [
        f'<svg role="img" aria-label="{title} trend" width="{_SVG_W}" height="{_SVG_H}" '
        f'viewBox="0 0 {_SVG_W} {_SVG_H}" xmlns="http://www.w3.org/2000/svg">'
    ]
    # Frame (the [0,1] plot box).
    parts.append(
        f'<rect x="{_SVG_PAD}" y="{_SVG_PAD}" width="{plot_w}" height="{plot_h}" '
        'fill="none" stroke="#ccc" stroke-width="1" />'
    )
    # 0.5 gridline for visual reference.
    mid_y = y_for(0.5)
    parts.append(
        f'<line x1="{_SVG_PAD}" y1="{mid_y:.1f}" x2="{_SVG_PAD + plot_w}" y2="{mid_y:.1f}" '
        'stroke="#eee" stroke-width="1" />'
    )
    if len(pts) >= 2:
        path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<path d="{path_d}" fill="none" stroke="#2b6cb0" stroke-width="2" />')
    for (x, y), (_run, v) in zip(pts, series, strict=True):
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#2b6cb0"><title>{v:.4f}</title></circle>'
        )
    # Axis end labels (first/last value).
    parts.append(
        f'<text x="{_SVG_PAD - 4}" y="{_SVG_PAD + 4}" font-size="9" text-anchor="end" fill="#666">1.0</text>'
    )
    parts.append(
        f'<text x="{_SVG_PAD - 4}" y="{_SVG_PAD + plot_h}" font-size="9" text-anchor="end" fill="#666">0.0</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _latest_threshold_rows(
    latest: dict[str, float],
    thresholds: dict[str, float] | None,
) -> list[tuple[str, float | None, float | None, bool | None]]:
    """Rows of ``(metric, value, threshold, passed)`` for the latest run.

    ``passed`` is ``None`` for a metric that has no threshold supplied (so the
    renderer can show "-" rather than implying a pass/fail it never checked).
    """
    thresholds = thresholds or {}
    rows: list[tuple[str, float | None, float | None, bool | None]] = []
    for metric in METRIC_NAMES:
        value = latest.get(metric)
        minimum = thresholds.get(metric)
        if minimum is None:
            passed: bool | None = None
        else:
            passed = value is not None and value >= minimum
        rows.append((metric, value, minimum, passed))
    return rows


def render_markdown(
    points: list[RunPoint],
    thresholds: dict[str, float] | None = None,
) -> str:
    """Render the trend report as markdown (tables only — no images)."""
    lines: list[str] = ["# RAGAS Evaluation Trend Report", ""]
    lines.append(f"Runs analyzed: {len(points)}")
    lines.append("")

    if not points:
        lines.append("_No usable run reports found in the history directory._")
        lines.append("")
        return "\n".join(lines)

    latest = points[-1]
    lines.append(f"Latest run: `{latest.label}`")
    lines.append("")

    # Per-metric trend table: one row per run, one column per metric.
    lines.append("## Per-metric trend")
    lines.append("")
    header = "| Run | " + " | ".join(METRIC_NAMES) + " |"
    sep = "| --- | " + " | ".join(["---"] * len(METRIC_NAMES)) + " |"
    lines.append(header)
    lines.append(sep)
    for p in points:
        cells = []
        for metric in METRIC_NAMES:
            v = p.aggregate.get(metric)
            cells.append(f"{v:.4f}" if isinstance(v, (int, float)) else "-")
        lines.append(f"| {p.label} | " + " | ".join(cells) + " |")
    lines.append("")

    # Latest vs thresholds.
    lines.append("## Latest run vs thresholds")
    lines.append("")
    lines.append("| Metric | Latest | Threshold | Pass |")
    lines.append("| --- | --- | --- | --- |")
    for metric, value, minimum, passed in _latest_threshold_rows(latest.aggregate, thresholds):
        val_s = f"{value:.4f}" if isinstance(value, (int, float)) else "-"
        min_s = f"{minimum:.4f}" if isinstance(minimum, (int, float)) else "-"
        pass_s = "-" if passed is None else ("yes" if passed else "no")
        lines.append(f"| {metric} | {val_s} | {min_s} | {pass_s} |")
    lines.append("")
    return "\n".join(lines)


def render_html(
    points: list[RunPoint],
    thresholds: dict[str, float] | None = None,
) -> str:
    """Render the trend report as a self-contained HTML page with inline SVG."""
    head = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        "<title>RAGAS Evaluation Trend Report</title>"
        "<style>"
        "body{font-family:system-ui,Arial,sans-serif;margin:2rem;color:#1a202c;}"
        "table{border-collapse:collapse;margin:0.5rem 0;}"
        "th,td{border:1px solid #ddd;padding:4px 8px;text-align:right;}"
        "th:first-child,td:first-child{text-align:left;}"
        ".fail{color:#c53030;font-weight:bold;}.pass{color:#2f855a;}"
        ".chart{margin:0.5rem 0 1.5rem;}"
        "</style></head><body>"
    )
    parts: list[str] = [head, "<h1>RAGAS Evaluation Trend Report</h1>"]
    parts.append(f"<p>Runs analyzed: {len(points)}</p>")

    if not points:
        parts.append("<p><em>No usable run reports found in the history directory.</em></p>")
        parts.append("</body></html>")
        return "".join(parts)

    latest = points[-1]
    parts.append(f"<p>Latest run: <code>{html.escape(latest.label)}</code></p>")

    # One SVG chart per metric.
    parts.append("<h2>Per-metric trends</h2>")
    for metric in METRIC_NAMES:
        series = _metric_series(points, metric)
        parts.append('<div class="chart">')
        parts.append(f"<h3>{html.escape(metric)}</h3>")
        parts.append(_render_svg(series, metric))
        parts.append("</div>")

    # Per-metric trend table (numbers behind the charts).
    parts.append("<h2>Per-metric trend (values)</h2>")
    parts.append("<table><thead><tr><th>Run</th>")
    for metric in METRIC_NAMES:
        parts.append(f"<th>{html.escape(metric)}</th>")
    parts.append("</tr></thead><tbody>")
    for p in points:
        parts.append(f"<tr><td>{html.escape(p.label)}</td>")
        for metric in METRIC_NAMES:
            v = p.aggregate.get(metric)
            parts.append(f"<td>{v:.4f}</td>" if isinstance(v, (int, float)) else "<td>-</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")

    # Latest vs thresholds.
    parts.append("<h2>Latest run vs thresholds</h2>")
    parts.append(
        "<table><thead><tr><th>Metric</th><th>Latest</th><th>Threshold</th><th>Pass</th></tr></thead><tbody>"
    )
    for metric, value, minimum, passed in _latest_threshold_rows(latest.aggregate, thresholds):
        val_s = f"{value:.4f}" if isinstance(value, (int, float)) else "-"
        min_s = f"{minimum:.4f}" if isinstance(minimum, (int, float)) else "-"
        if passed is None:
            pass_cell = "<td>-</td>"
        elif passed:
            pass_cell = '<td class="pass">yes</td>'
        else:
            pass_cell = '<td class="fail">no</td>'
        parts.append(f"<tr><td>{html.escape(metric)}</td><td>{val_s}</td><td>{min_s}</td>{pass_cell}</tr>")
    parts.append("</tbody></table>")

    parts.append("</body></html>")
    return "".join(parts)


def write_trend_report(
    history_dir: str | Path,
    out_dir: str | Path,
    thresholds: dict[str, float] | None = None,
) -> tuple[Path, Path]:
    """Load history, render HTML + markdown, and write both. Returns paths.

    Files are written as ``trend_report.html`` and ``trend_report.md`` under
    ``out_dir`` (created if needed).
    """
    points = load_history(history_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    html_path = out / "trend_report.html"
    md_path = out / "trend_report.md"
    html_path.write_text(render_html(points, thresholds), encoding="utf-8")
    md_path.write_text(render_markdown(points, thresholds), encoding="utf-8")
    return html_path, md_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eval.trend_report",
        description="Render per-metric RAGAS trend reports (HTML + markdown) from a history dir.",
    )
    parser.add_argument(
        "--history",
        default=str(Path(__file__).resolve().parent / "history"),
        help="Directory of historical run JSON reports (run-<ISO8601>.json).",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "out"),
        help="Directory to write trend_report.html / trend_report.md into.",
    )
    for metric in METRIC_NAMES:
        parser.add_argument(
            f"--min-{metric.replace('_', '-')}",
            dest=f"min_{metric}",
            type=float,
            default=None,
            metavar="FLOAT",
            help=f"Threshold for '{metric}' shown in the latest-vs-thresholds block.",
        )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    thresholds = {
        metric: getattr(args, f"min_{metric}")
        for metric in METRIC_NAMES
        if getattr(args, f"min_{metric}") is not None
    }
    html_path, md_path = write_trend_report(args.history, args.out, thresholds or None)
    print(json.dumps({"html_report": str(html_path), "md_report": str(md_path)}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
