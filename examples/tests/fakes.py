"""In-memory fakes for the SDK clients used across the example tests.

Each fake records the ``base_url`` / token it was constructed with (so a test
can assert the example wired the documented env vars through correctly) and
returns real SDK models, so ``.citations`` / ``.run_id`` / etc. behave exactly
as in production. No HTTP is performed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from tocdoc_sdk import QnAAnswer, TocDocClient
from tocdoc_sdk.models import (
    ConnectorRunStatusResponse,
    ConnectorSyncResponse,
    DocumentListResponse,
    IndexStatsResponse,
)

# A canned grounded answer + citation map, validated through the real model.
SAMPLE_ANSWER = QnAAnswer.model_validate(
    {"answer": "Refunds are available within 30 days.", "citation": {"policy.md": "/docs/policy.md"}}
)


class FakeTocDocClient(TocDocClient):
    """Stand-in for :class:`tocdoc_sdk.TocDocClient` (sync QnA).

    Subclasses the real client so it passes ``TocDocRetriever``'s pydantic
    ``isinstance`` field check, but overrides ``__init__`` (no httpx transport
    built) and ``ask`` (canned answer) so no network is ever touched.
    """

    def __init__(self, base_url: str, *, token: str | None = None, **_: Any) -> None:
        self.base_url = base_url
        self.token = token
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> FakeTocDocClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None

    def ask(self, **kwargs: Any) -> QnAAnswer:
        self.calls.append(kwargs)
        return SAMPLE_ANSWER


class FakeAsyncTocDocClient:
    """Stand-in for :class:`tocdoc_sdk.AsyncTocDocClient` (async streaming)."""

    def __init__(self, base_url: str, *, token: str | None = None, **_: Any) -> None:
        self.base_url = base_url
        self.token = token
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeAsyncTocDocClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def stream_ask(self, **kwargs: Any) -> AsyncIterator[str]:
        self.calls.append(kwargs)
        for token in ["Refunds ", "are ", "available."]:
            yield token


class FakeAdminClient:
    """Stand-in for :class:`tocdoc_sdk.AdminClient`.

    ``trigger_connector_sync`` returns a ``started`` handle; ``get_connector_run``
    reports ``started`` once then ``completed`` so an example's poll loop runs at
    least one real iteration before terminating.
    """

    def __init__(self, base_url: str, *, admin_token: str, **_: Any) -> None:
        self.base_url = base_url
        self.admin_token = admin_token
        self._run_polls = 0

    def __enter__(self) -> FakeAdminClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None

    def list_documents(self, *, bot_tag: str) -> DocumentListResponse:
        return DocumentListResponse.model_validate(
            {
                "bot_tag": bot_tag,
                "count": 1,
                "documents": [{"document_id": "doc-1", "source_type": "blob", "chunk_count": 12}],
            }
        )

    def index_stats(self, *, bot_tag: str) -> IndexStatsResponse:
        return IndexStatsResponse.model_validate(
            {
                "bot_tag": bot_tag,
                "document_count": 1,
                "chunk_count": 12,
                "source_types": {"blob": 1},
                "fr_modes": {"read": 1},
            }
        )

    def trigger_connector_sync(self, source_type: str) -> ConnectorSyncResponse:
        return ConnectorSyncResponse.model_validate(
            {"run_id": "run-1", "source_type": source_type, "status": "started"}
        )

    def get_connector_run(self, run_id: str) -> ConnectorRunStatusResponse:
        self._run_polls += 1
        status = "completed" if self._run_polls >= 2 else "started"
        return ConnectorRunStatusResponse.model_validate(
            {
                "run_id": run_id,
                "status": status,
                "source_type": "blob",
                "bot_tag": "acme",
                "processed_count": 8 if status == "completed" else 0,
                "failed_count": 0,
            }
        )
