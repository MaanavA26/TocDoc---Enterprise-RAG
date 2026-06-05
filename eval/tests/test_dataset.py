"""Tests for the golden benchmark dataset and its loader.

Asserts the shipped ``eval/benchmark/sample.jsonl`` loads cleanly, every record
has the required fields with valid values, and the dataset stays neutral (no
real client/company/product names) so it is safe for a public repo.
"""

from pathlib import Path

from eval.ragas_eval import load_benchmark

_BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark" / "sample.jsonl"

# Allowed bot tags / retrieval modes for the shipped neutral dataset.
_ALLOWED_BOT_TAGS = {"client_a", "demo_workspace"}
_ALLOWED_FR_TAGS = {"read", "layout"}


def test_shipped_benchmark_loads_and_has_records():
    records = load_benchmark(_BENCHMARK)
    assert len(records) >= 12  # expanded golden set
    assert all(isinstance(r, dict) for r in records)


def test_every_record_has_required_fields():
    for rec in load_benchmark(_BENCHMARK):
        for key in ("question", "ground_truth", "bot_tag", "fr_tag"):
            assert key in rec, f"missing {key} in {rec}"
            assert isinstance(rec[key], str) and rec[key].strip(), f"empty {key} in {rec}"
        assert rec["bot_tag"] in _ALLOWED_BOT_TAGS
        assert rec["fr_tag"] in _ALLOWED_FR_TAGS


def test_dataset_covers_both_modes_and_tags():
    records = load_benchmark(_BENCHMARK)
    assert {r["fr_tag"] for r in records} == _ALLOWED_FR_TAGS
    assert {r["bot_tag"] for r in records} == _ALLOWED_BOT_TAGS


def test_dataset_stays_neutral():
    # Guard against accidentally seeding real names/product claims into the
    # public golden set. These are illustrative placeholders only.
    text = _BENCHMARK.read_text(encoding="utf-8").lower()
    forbidden = ["confidential", "proprietary", "@", "http://", "https://"]
    for token in forbidden:
        assert token not in text, f"unexpected token in golden dataset: {token!r}"


def test_load_benchmark_skips_blank_lines(tmp_path):
    path = tmp_path / "b.jsonl"
    path.write_text(
        '{"question": "q", "ground_truth": "g", "bot_tag": "client_a", "fr_tag": "read"}\n'
        "\n"  # blank line ignored
        "   \n",  # whitespace-only line ignored
        encoding="utf-8",
    )
    records = load_benchmark(path)
    assert len(records) == 1


def test_load_benchmark_raises_on_bad_json(tmp_path):
    path = tmp_path / "b.jsonl"
    path.write_text("{not valid json}\n", encoding="utf-8")
    try:
        load_benchmark(path)
    except ValueError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("expected ValueError on malformed JSONL")
