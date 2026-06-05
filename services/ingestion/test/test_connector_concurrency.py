"""Connector hardening tests: H4 + L-Conn1 + L-Conn2 (core.py).

These exercise the concurrency/robustness invariants of the source-agnostic
driver, the way PRODUCTION runs it — each connector run on its OWN thread via
``asyncio.run`` (mirroring ``admin/routes.py:_run_connector_background``).

- H4: two concurrent runs on the SAME (source_path, bot_tag) are SERIALIZED —
  run A's delete→upload window never interleaves run B's. This must be tested
  across threads/loops; a single-event-loop ``asyncio.gather`` test would pass
  with the OLD broken ``asyncio.Lock`` and so proves nothing.
- L-Conn1: the lock registry is bounded by drop-when-idle refcounting — it is
  empty once all runs finish, regardless of how many distinct keys ran.
- L-Conn2: an item failure aborts the run carrying the partial-progress count
  (ConnectorRunError.processed_count), and a fetch() failure is diagnosable
  (inside the per-item try) instead of aborting with no per-item record.

Hermetic — no Azure, no network.
"""

import asyncio
import contextlib
import os
import sys
import threading
from collections.abc import Iterator

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from connectors import (  # noqa: E402
    ConnectorConfig,
    ConnectorFile,
    ConnectorRunError,
    SourceItem,
    run_connector,
)
from connectors import core as connector_core  # noqa: E402

SOURCE_PATH = "fake://store/shared.pdf"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _OneItemConnector:
    """Yields exactly one item at a fixed source_path."""

    source_type = "fake"

    def __init__(self, config: ConnectorConfig, source_path: str = SOURCE_PATH):
        self.bot_tag = config.bot_tag
        self.fr_mode = config.fr_mode
        self._source_path = source_path

    def enumerate(self) -> Iterator[SourceItem]:
        yield SourceItem(
            identity=self._source_path,
            source_path=self._source_path,
            filename="shared.pdf",
            size=10,
        )

    def fetch(self, item: SourceItem) -> ConnectorFile:
        return ConnectorFile(filename=item.filename, content=b"%PDF-x")


class _InterleaveDetectingRag:
    """Records (run_label, op) for delete/upload, with a delay inside the window.

    A thread-safe event log + a barrier that forces both runs to enter at the
    same time. The delete sleeps briefly so that IF the lock did not serialize,
    the second run's delete/upload would interleave between this run's delete and
    upload — which the assertion forbids.
    """

    def __init__(self, barrier: threading.Barrier):
        self.events: list[tuple[str, str]] = []
        self._events_lock = threading.Lock()
        self._barrier = barrier
        self._entered_window = threading.Event()

    def _record(self, run_label: str, op: str) -> None:
        with self._events_lock:
            self.events.append((run_label, op))

    async def delete_by_source_path(self, source_path, bot_tag):
        # Force both runs to arrive together BEFORE either takes the lock, so the
        # test genuinely races the critical section rather than running serially.
        await asyncio.to_thread(self._wait_barrier_once)
        self._record(_current_run_label(), "delete")
        # Hold the window open long enough that an unserialized second run would
        # observably interleave here.
        await asyncio.sleep(0.05)
        return 0

    def _wait_barrier_once(self):
        if not self._entered_window.is_set():
            with contextlib.suppress(threading.BrokenBarrierError):
                self._barrier.wait(timeout=5)

    async def upload(self, file, tag, fr_mode, file_path, source_type="upload", request_id=None):
        self._record(_current_run_label(), "upload")
        await asyncio.sleep(0.01)
        return {"status": "successful"}


# run label is threaded via a contextvar-free thread-local set by the runner.
_run_label = threading.local()


def _current_run_label() -> str:
    return getattr(_run_label, "value", "?")


def _run_on_thread(label: str, connector, rag, results: dict) -> None:
    _run_label.value = label

    async def _go():
        return await run_connector(connector, rag, run_id=label)

    try:
        results[label] = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001 - surfaced via results for assertions
        results[label] = exc


# ---------------------------------------------------------------------------
# H4 — cross-thread serialization of the delete→upload window
# ---------------------------------------------------------------------------


def test_concurrent_same_source_runs_are_serialized(monkeypatch):
    """Two runs on the same key on separate threads/loops never interleave.

    Production drives each run via ``asyncio.run`` on its own threadpool thread.
    The fix is a threading.Lock-keyed registry; this test would FAIL on the old
    module-level asyncio.Lock (no cross-loop exclusion). The event log must show
    one full delete→upload pair before the other begins.
    """
    connector_core._locks.clear()
    cfg = ConnectorConfig(bot_tag="tenant-x")
    barrier = threading.Barrier(2)
    rag = _InterleaveDetectingRag(barrier)
    results: dict = {}

    threads = [
        threading.Thread(
            target=_run_on_thread,
            args=(label, _OneItemConnector(cfg), rag, results),
        )
        for label in ("A", "B")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Both runs succeeded.
    assert results["A"] == {"processed": 1, "items": [SOURCE_PATH]}
    assert results["B"] == {"processed": 1, "items": [SOURCE_PATH]}

    # Exactly 4 events: two delete→upload pairs, fully serialized (no interleave).
    assert len(rag.events) == 4
    first_run = rag.events[0][0]
    second_run = "B" if first_run == "A" else "A"
    assert rag.events == [
        (first_run, "delete"),
        (first_run, "upload"),
        (second_run, "delete"),
        (second_run, "upload"),
    ]


def test_lock_registry_is_empty_when_idle(monkeypatch):
    """L-Conn1: refcounting drops every key once its run finishes (no growth)."""
    connector_core._locks.clear()
    cfg = ConnectorConfig(bot_tag="tenant-x")

    class _NoopRag:
        async def delete_by_source_path(self, *a, **k):
            return 0

        async def upload(self, *a, **k):
            return {"status": "successful"}

    # Run several distinct keys sequentially; none should linger in the registry.
    for i in range(5):
        conn = _OneItemConnector(cfg, source_path=f"fake://store/{i}.pdf")
        asyncio.run(run_connector(conn, _NoopRag(), run_id=f"run-{i}"))

    assert connector_core._active_lock_keys() == 0
    assert connector_core._locks == {}


# ---------------------------------------------------------------------------
# L-Conn2 — partial-progress accounting + diagnosable fetch failure
# ---------------------------------------------------------------------------


class _MultiItemConnector:
    """Yields N items; optionally raises in fetch() for one of them."""

    source_type = "fake"

    def __init__(self, config: ConnectorConfig, source_paths, fail_fetch_on=None):
        self.bot_tag = config.bot_tag
        self.fr_mode = config.fr_mode
        self._source_paths = source_paths
        self._fail_fetch_on = fail_fetch_on
        self.fetched: list[str] = []

    def enumerate(self) -> Iterator[SourceItem]:
        for sp in self._source_paths:
            yield SourceItem(identity=sp, source_path=sp, filename="f.pdf", size=10)

    def fetch(self, item: SourceItem) -> ConnectorFile:
        self.fetched.append(item.source_path)
        if item.source_path == self._fail_fetch_on:
            raise RuntimeError("download exploded")
        return ConnectorFile(filename=item.filename, content=b"%PDF-x")


@pytest.mark.asyncio
async def test_partial_progress_count_carried_on_upload_failure():
    """A failure on the 3rd item reports processed_count=2, not 0 (L-Conn2)."""
    cfg = ConnectorConfig(bot_tag="tenant-x")
    paths = [f"fake://store/{i}.pdf" for i in range(5)]
    conn = _MultiItemConnector(cfg, paths)

    class _FailThirdUploadRag:
        def __init__(self):
            self.uploads = 0

        async def delete_by_source_path(self, *a, **k):
            return 0

        async def upload(self, *a, **k):
            self.uploads += 1
            if self.uploads == 3:
                raise RuntimeError("upsert failed")
            return {"status": "successful"}

    with pytest.raises(ConnectorRunError) as excinfo:
        await run_connector(conn, _FailThirdUploadRag(), run_id="run-x")
    # Two items fully ingested before the third failed.
    assert excinfo.value.processed_count == 2
    assert excinfo.value.failed_count == 1
    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_fetch_failure_is_diagnosable_and_carries_count():
    """A fetch() failure aborts INSIDE the per-item try (L-Conn2).

    fetch now sits inside the try, so a download failure emits
    connector_item_failed (with source_path) and carries the running count —
    rather than aborting the run with no per-item record.
    """
    cfg = ConnectorConfig(bot_tag="tenant-x")
    paths = ["fake://store/0.pdf", "fake://store/1.pdf", "fake://store/2.pdf"]
    conn = _MultiItemConnector(cfg, paths, fail_fetch_on="fake://store/1.pdf")

    class _OkRag:
        async def delete_by_source_path(self, *a, **k):
            return 0

        async def upload(self, *a, **k):
            return {"status": "successful"}

    with pytest.raises(ConnectorRunError) as excinfo:
        await run_connector(conn, _OkRag(), run_id="run-x")
    # First item processed; failure on the second item's fetch.
    assert excinfo.value.processed_count == 1
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    # fetch was attempted for the failing item (it is inside the try now).
    assert "fake://store/1.pdf" in conn.fetched
