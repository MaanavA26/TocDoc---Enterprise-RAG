"""In-process connector-run status store (P1-3 follow-up).

A thread-safe, module-level singleton recording the lifecycle of each connector
sync run keyed by ``run_id``. The trigger endpoint schedules the sync as a SYNC
background task on Starlette's threadpool (see ``admin/routes.py``), so the run's
status writes happen on a worker THREAD while the ``GET`` query is served on the
event loop. A ``threading.Lock`` (not an ``asyncio.Lock``) guards the store so
those concurrent accesses cannot corrupt it.

Scope / caveats (v1):
- **In-process only.** State lives in this module's memory and is LOST on
  restart. This is intentionally not a durable or distributed job store; a
  Blob/Cosmos-backed store is a documented follow-up if ingestion goes
  multi-replica or needs run history to survive restarts.
- **Bounded.** Only the most recent ``MAX_RUNS`` runs are retained; the oldest
  is evicted on insert so the store cannot grow without limit.
- **Safe metadata only.** The store holds run_id / status / source_type /
  bot_tag / timestamps / counts and, on failure, an error CLASS name plus a
  generic safe message. It NEVER holds raw exception text, secrets, tokens, or
  document content.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any

# Retain the most recent N runs. Oldest is evicted on insert (see record_started)
# so the store stays bounded regardless of how many syncs are triggered.
MAX_RUNS = 200

# Lifecycle states a run can be in.
STATUS_STARTED = "started"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


class _RunStatusStore:
    """Thread-safe, bounded store of connector-run status records.

    Records are plain dicts of safe metadata. All access is guarded by a
    ``threading.Lock`` because writes happen on a Starlette threadpool thread
    and reads on the event-loop thread. Reads return a shallow COPY so callers
    never hold a reference into the live store (and so the GET handler does not
    need to hold the lock while serializing).
    """

    def __init__(self, max_runs: int = MAX_RUNS) -> None:
        self._max_runs = max_runs
        self._lock = threading.Lock()
        # Insertion-ordered so we can evict the oldest run cheaply.
        self._runs: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def record_started(
        self,
        run_id: str,
        *,
        source_type: str,
        bot_tag: str,
        started_at: datetime,
    ) -> None:
        """Record a run entering the ``started`` state.

        Inserts a fresh record and evicts the oldest run(s) if the store is at
        capacity. Timestamps are passed in by the caller (never read from a
        clock here) so the observe point stays deterministic and testable.
        """
        record = {
            "run_id": run_id,
            "status": STATUS_STARTED,
            "source_type": source_type,
            "bot_tag": bot_tag,
            "started_at": started_at,
            "finished_at": None,
            "processed_count": 0,
            "failed_count": 0,
            "error": None,
        }
        with self._lock:
            # If this run_id already exists, drop it so the re-insert lands at
            # the most-recent end (move_to_end semantics without resurrecting).
            self._runs.pop(run_id, None)
            self._runs[run_id] = record
            while len(self._runs) > self._max_runs:
                # Evict the oldest inserted run.
                self._runs.popitem(last=False)

    def record_completed(
        self,
        run_id: str,
        *,
        processed_count: int,
        failed_count: int = 0,
        finished_at: datetime,
    ) -> None:
        """Mark a known run ``completed`` with its processed/failed counts.

        Updates the existing record in place (preserving its position in the
        eviction order) so a completion never reorders or resurrects an already
        evicted run. A no-op if the run_id is unknown (e.g. already evicted).
        """
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            record["status"] = STATUS_COMPLETED
            record["processed_count"] = processed_count
            record["failed_count"] = failed_count
            record["finished_at"] = finished_at

    def record_failed(
        self,
        run_id: str,
        *,
        error_class: str,
        safe_message: str,
        finished_at: datetime,
        processed_count: int = 0,
        failed_count: int = 0,
    ) -> None:
        """Mark a known run ``failed`` with a SAFE error summary.

        ``error_class`` is the exception class name and ``safe_message`` is a
        generic category — NEVER raw exception text, which may carry secrets or
        document content. Updates in place; a no-op for an unknown run_id.
        """
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            record["status"] = STATUS_FAILED
            record["finished_at"] = finished_at
            record["processed_count"] = processed_count
            record["failed_count"] = failed_count
            record["error"] = {"error_class": error_class, "safe_message": safe_message}

    def get(self, run_id: str) -> dict[str, Any] | None:
        """Return a COPY of one run's record, or None if unknown.

        A shallow copy of a deeper copy: the top-level dict and the nested
        ``error`` dict are both copied so the caller can never mutate the live
        store. All values are immutable scalars / datetimes otherwise.
        """
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            return self._copy(record)

    def list_recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return COPIES of recent run records, newest first.

        Bounded by the store cap already; ``limit`` further trims the result.
        """
        with self._lock:
            records = list(self._runs.values())
        records.reverse()  # newest first (insertion order is oldest→newest)
        if limit is not None:
            records = records[:limit]
        return [self._copy(r) for r in records]

    @staticmethod
    def _copy(record: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(record)
        if isinstance(snapshot.get("error"), dict):
            snapshot["error"] = dict(snapshot["error"])
        return snapshot

    def clear(self) -> None:
        """Drop all records — test hook so suites don't leak state across cases."""
        with self._lock:
            self._runs.clear()


# Module-level singleton — one store per process. The trigger background task
# writes to it; the GET endpoint reads from it.
run_status_store = _RunStatusStore()
