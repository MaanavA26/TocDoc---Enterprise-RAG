"""PR-2 connector-core tests: config validation + FakeConnector driver.

- ConnectorConfig rejects invalid bot_tags at init (before any network call).
- FakeConnector drives enumerate → fetch → (delete_by_source_path → upload) with
  correct bot_tag / source_type / source_path propagation.
- A re-run is idempotent: same source_path is delete-then-uploaded again, never
  cross-tagged.

Hermetic — no Azure, no network.
"""

import os
import sys
from collections.abc import Iterator

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from connectors import (  # noqa: E402
    ConnectorConfig,
    ConnectorFile,
    InvalidBotTagError,
    SourceConnector,
    SourceItem,
    run_connector,
)
from connectors.core import ConnectorError, ConnectorRunError  # noqa: E402

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeConnector:
    """In-memory SourceConnector test double — no Azure.

    Holds a list of (filename, source_path, content) and proves the driver
    propagates bot_tag / fr_mode / source_type and never reads them per-item.
    """

    source_type = "fake"

    def __init__(self, config: ConnectorConfig, items):
        self.bot_tag = config.bot_tag
        self.fr_mode = config.fr_mode
        self._items = items
        self.fetched: list[str] = []

    def enumerate(self) -> Iterator[SourceItem]:
        for fname, source_path, _content in self._items:
            yield SourceItem(identity=source_path, source_path=source_path, filename=fname, size=10)

    def fetch(self, item: SourceItem) -> ConnectorFile:
        self.fetched.append(item.source_path)
        content = next(c for f, sp, c in self._items if sp == item.source_path)
        return ConnectorFile(filename=item.filename, content=content)


class _RecordingRag:
    """Records delete_by_source_path + upload calls in order."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def delete_by_source_path(self, source_path, bot_tag):
        self.calls.append(("delete", source_path, bot_tag))
        return 0

    async def upload(self, file, tag, fr_mode, file_path, source_type="upload", request_id=None):
        content = await file.read()
        self.calls.append(("upload", file_path, tag, fr_mode, source_type, file.filename, content))
        return {"status": "successful"}


# ---------------------------------------------------------------------------
# ConnectorConfig validation
# ---------------------------------------------------------------------------


def test_config_accepts_valid_bot_tag():
    cfg = ConnectorConfig(bot_tag="tenant-A_1", fr_mode="layout")
    assert cfg.bot_tag == "tenant-A_1"
    assert cfg.fr_mode == "layout"


@pytest.mark.parametrize("bad", ["", "has space", "has/slash", "a" * 129, "tag!"])
def test_config_rejects_invalid_bot_tag(bad):
    with pytest.raises(InvalidBotTagError):
        ConnectorConfig(bot_tag=bad)


def test_config_rejects_invalid_fr_mode():
    with pytest.raises(ConnectorError):
        ConnectorConfig(bot_tag="ok", fr_mode="bogus")


def test_fake_connector_satisfies_protocol():
    cfg = ConnectorConfig(bot_tag="t")
    conn = FakeConnector(cfg, [])
    assert isinstance(conn, SourceConnector)


# ---------------------------------------------------------------------------
# Driver propagation + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_driver_propagates_bot_tag_source_type_source_path():
    cfg = ConnectorConfig(bot_tag="tenant-x", fr_mode="read")
    items = [
        ("a.pdf", "fake://store/a.pdf", b"%PDF-a"),
        ("b.pdf", "fake://store/b.pdf", b"%PDF-b"),
    ]
    conn = FakeConnector(cfg, items)
    recorder = _RecordingRag()

    summary = await run_connector(conn, recorder, run_id="run-1")

    assert summary == {"processed": 2, "items": ["fake://store/a.pdf", "fake://store/b.pdf"]}

    # delete precedes upload for each item; per-item order delete→upload.
    assert recorder.calls == [
        ("delete", "fake://store/a.pdf", "tenant-x"),
        ("upload", "fake://store/a.pdf", "tenant-x", "read", "fake", "a.pdf", b"%PDF-a"),
        ("delete", "fake://store/b.pdf", "tenant-x"),
        ("upload", "fake://store/b.pdf", "tenant-x", "read", "fake", "b.pdf", b"%PDF-b"),
    ]
    # Every upload carries the connector's bot_tag + source_type, never per-item.
    for call in recorder.calls:
        if call[0] == "upload":
            assert call[2] == "tenant-x"
            assert call[4] == "fake"


@pytest.mark.asyncio
async def test_driver_rerun_is_idempotent():
    """Re-running the same connector deletes-then-uploads each source_path again,
    converging on the same (bot_tag, source_path) — no orphaning, no cross-tag."""
    cfg = ConnectorConfig(bot_tag="tenant-x", fr_mode="read")
    items = [("a.pdf", "fake://store/a.pdf", b"%PDF-a")]
    conn = FakeConnector(cfg, items)
    recorder = _RecordingRag()

    await run_connector(conn, recorder, run_id="run-1")
    await run_connector(conn, recorder, run_id="run-2")

    deletes = [c for c in recorder.calls if c[0] == "delete"]
    uploads = [c for c in recorder.calls if c[0] == "upload"]
    assert len(deletes) == 2
    assert len(uploads) == 2
    # Both runs target the identical (source_path, bot_tag).
    assert all(d[1:] == ("fake://store/a.pdf", "tenant-x") for d in deletes)
    assert all(u[1:3] == ("fake://store/a.pdf", "tenant-x") for u in uploads)


@pytest.mark.asyncio
async def test_driver_propagates_errors_not_swallowed():
    """A failing upload must propagate (P0-6), not be silently swallowed.

    The item failure is wrapped in a ConnectorRunError (L-Conn2) carrying the
    partial-progress count, with the original exception chained as __cause__ so
    the underlying error class is never hidden.
    """
    cfg = ConnectorConfig(bot_tag="tenant-x")
    conn = FakeConnector(cfg, [("a.pdf", "fake://store/a.pdf", b"%PDF-a")])

    class _BoomRag(_RecordingRag):
        async def upload(self, *a, **k):
            raise RuntimeError("boom")

    with pytest.raises(ConnectorRunError) as excinfo:
        await run_connector(conn, _BoomRag(), run_id="run-x")
    # Nothing was successfully processed before the first item failed.
    assert excinfo.value.processed_count == 0
    # Original exception is preserved, not hidden.
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "boom"
