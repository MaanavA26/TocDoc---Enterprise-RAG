"""run_id correlation in connector inner logs (P1-3 follow-up, Feature 2).

When a connector is driven via ``run_connector(..., run_id=...)``, its OWN inner
log events (notably ``connector_graph_throttled`` in the SharePoint connector)
must carry that run_id as ``request_id`` so the whole run is greppable.

Strategy: drive a SharePoint connector whose Graph listing returns a 429 (then
an empty page so no items are fetched/uploaded — no rag needed). We monkeypatch
``connectors.sharepoint.log_event`` (imported by name in that module) to capture
the kwargs, and assert the throttle event carries request_id == run_id.

Also verifies backward-compat: a connector built directly (no run) defaults
run_id to None, and the throttle event then carries no run_id.
"""

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import connectors.sharepoint as sharepoint_module  # noqa: E402
from connectors import ConnectorConfig  # noqa: E402
from connectors.core import run_connector  # noqa: E402
from connectors.sharepoint import SharePointConnector  # noqa: E402

SITE_ID = "site-1"
DRIVE_ID = "drive-1"


class _StubRag:
    """Never called in these tests (the 429-then-empty page yields no items)."""

    async def upload(self, *a, **k):  # pragma: no cover - not reached
        raise AssertionError("upload should not be called")

    async def delete_by_source_path(self, *a, **k):  # pragma: no cover - not reached
        raise AssertionError("delete should not be called")


def _connector_429_then_empty():
    """A connector whose listing returns one 429 then an empty page."""
    state = {"hits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["hits"] += 1
        if state["hits"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})
        return httpx.Response(200, json={"value": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = ConnectorConfig(bot_tag="tenant-x", fr_mode="read")
    # Recording no-op sleep so the test doesn't actually back off.
    conn = SharePointConnector(cfg, SITE_ID, DRIVE_ID, http_client=client, sleep=lambda s: None)
    return conn


def _capture_throttle_events(monkeypatch):
    """Patch the module-level log_event in sharepoint.py; capture throttle calls."""
    events = []
    real_log_event = sharepoint_module.log_event

    def _spy(logger, event, **kwargs):
        if event == "connector_graph_throttled":
            events.append(kwargs)
        return real_log_event(logger, event, **kwargs)

    monkeypatch.setattr(sharepoint_module, "log_event", _spy)
    return events


def test_throttle_event_carries_run_id_when_driven(monkeypatch):
    events = _capture_throttle_events(monkeypatch)
    conn = _connector_429_then_empty()

    result = asyncio.run(run_connector(conn, _StubRag(), run_id="run-abc123"))

    assert result == {"processed": 0, "items": []}
    assert events, "expected a connector_graph_throttled event"
    assert events[0]["request_id"] == "run-abc123"
    # The driver also stamped the run_id onto the connector instance.
    assert conn.run_id == "run-abc123"


def test_throttle_event_has_no_run_id_when_built_directly(monkeypatch):
    """Backward-compat: a connector used outside a run keeps run_id=None."""
    events = _capture_throttle_events(monkeypatch)
    conn = _connector_429_then_empty()
    assert conn.run_id is None

    # Drive enumerate directly (no run_connector) — exercises the throttle path.
    items = list(conn.enumerate())
    assert items == []
    assert events
    assert events[0].get("request_id") is None
