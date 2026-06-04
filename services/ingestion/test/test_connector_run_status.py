"""Unit tests for the in-process connector run-status store (P1-3 follow-up).

Covers:
- started → completed / failed transitions.
- processed/failed count capture on completion.
- safe error summary on failure (class + safe message; no raw exception text).
- eviction at the cap (oldest evicted; in-place updates don't resurrect).
- reads return COPIES (caller cannot mutate the live store).
- thread-safety smoke: concurrent writes don't corrupt or lose records.
"""

import os
import sys
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from connectors.run_status import (  # noqa: E402
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_STARTED,
    _RunStatusStore,
)


def _now():
    return datetime.now(timezone.utc)


def test_started_then_completed_captures_counts():
    store = _RunStatusStore()
    store.record_started("r1", source_type="blob", bot_tag="tenant-x", started_at=_now())

    rec = store.get("r1")
    assert rec["status"] == STATUS_STARTED
    assert rec["source_type"] == "blob"
    assert rec["bot_tag"] == "tenant-x"
    assert rec["processed_count"] == 0
    assert rec["finished_at"] is None

    store.record_completed("r1", processed_count=7, failed_count=0, finished_at=_now())
    rec = store.get("r1")
    assert rec["status"] == STATUS_COMPLETED
    assert rec["processed_count"] == 7
    assert rec["failed_count"] == 0
    assert rec["finished_at"] is not None
    assert rec["error"] is None


def test_started_then_failed_records_safe_error():
    store = _RunStatusStore()
    store.record_started("r1", source_type="sharepoint", bot_tag="tenant-x", started_at=_now())
    store.record_failed(
        "r1",
        error_class="ConnectorError",
        safe_message="Connector sync run failed",
        finished_at=_now(),
    )
    rec = store.get("r1")
    assert rec["status"] == STATUS_FAILED
    assert rec["error"] == {"error_class": "ConnectorError", "safe_message": "Connector sync run failed"}
    assert rec["finished_at"] is not None


def test_unknown_run_id_returns_none():
    store = _RunStatusStore()
    assert store.get("nope") is None


def test_completed_and_failed_are_noops_for_unknown_run():
    store = _RunStatusStore()
    # Must not raise or create a phantom record.
    store.record_completed("ghost", processed_count=1, finished_at=_now())
    store.record_failed("ghost", error_class="X", safe_message="y", finished_at=_now())
    assert store.get("ghost") is None


def test_eviction_at_cap_drops_oldest():
    store = _RunStatusStore(max_runs=3)
    for i in range(5):
        store.record_started(f"r{i}", source_type="blob", bot_tag="t", started_at=_now())
    # Only the 3 most-recent survive; the two oldest were evicted.
    assert store.get("r0") is None
    assert store.get("r1") is None
    assert store.get("r2") is not None
    assert store.get("r3") is not None
    assert store.get("r4") is not None


def test_in_place_update_does_not_resurrect_evicted_run():
    store = _RunStatusStore(max_runs=2)
    store.record_started("r0", source_type="blob", bot_tag="t", started_at=_now())
    store.record_started("r1", source_type="blob", bot_tag="t", started_at=_now())
    # Completing r0 must NOT bring it back (it's still present here)...
    store.record_completed("r0", processed_count=1, finished_at=_now())
    assert store.get("r0")["status"] == STATUS_COMPLETED
    # ...now push it out by inserting two newer runs.
    store.record_started("r2", source_type="blob", bot_tag="t", started_at=_now())
    store.record_started("r3", source_type="blob", bot_tag="t", started_at=_now())
    assert store.get("r0") is None
    # A late completion for the evicted run is a no-op, not a resurrection.
    store.record_completed("r0", processed_count=99, finished_at=_now())
    assert store.get("r0") is None


def test_get_returns_copy_not_live_reference():
    store = _RunStatusStore()
    store.record_started("r1", source_type="blob", bot_tag="t", started_at=_now())
    store.record_failed("r1", error_class="E", safe_message="m", finished_at=_now())
    snap = store.get("r1")
    snap["status"] = "tampered"
    snap["error"]["safe_message"] = "tampered"
    # The live store is unaffected by mutating the snapshot.
    fresh = store.get("r1")
    assert fresh["status"] == STATUS_FAILED
    assert fresh["error"]["safe_message"] == "m"


def test_list_recent_newest_first_and_limit():
    store = _RunStatusStore()
    for i in range(5):
        store.record_started(f"r{i}", source_type="blob", bot_tag="t", started_at=_now())
    recent = store.list_recent(limit=3)
    assert [r["run_id"] for r in recent] == ["r4", "r3", "r2"]


def test_concurrent_writes_do_not_corrupt_store():
    """Thread-safety smoke: many threads writing distinct run_ids concurrently
    all land intact and none are lost or corrupted."""
    store = _RunStatusStore(max_runs=1000)
    n = 200

    def worker(i):
        rid = f"run-{i}"
        store.record_started(rid, source_type="blob", bot_tag="t", started_at=_now())
        store.record_completed(rid, processed_count=i, finished_at=_now())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(n):
        rec = store.get(f"run-{i}")
        assert rec is not None
        assert rec["status"] == STATUS_COMPLETED
        assert rec["processed_count"] == i
