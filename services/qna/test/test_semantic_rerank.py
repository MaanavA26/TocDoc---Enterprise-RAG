"""
Tests for P2-1 Step 1: config-gated Azure AI Search L2 semantic reranking.

Covers `_search_sync` in `src/services/search_service.py`:

- With `AZURE_SEARCH_SEMANTIC_CONFIG` set, the search call receives
  `query_type=SEMANTIC` + `semantic_configuration_name`.
- Unset (empty), no semantic params are sent — identical to the prior
  pure-hybrid behavior.
- A semantic call that raises `HttpResponseError` (e.g. an unsupported
  Search tier) falls back to a hybrid retry, still returns results, and
  logs a single warning.
- The bot_tag/fr_tag filter and `top` are unchanged in both modes.
"""

import logging
import os

import pytest
from azure.core.exceptions import HttpResponseError
from azure.search.documents.models import QueryType

# ---------------------------------------------------------------------------
# Required env vars must be set before any local imports
# ---------------------------------------------------------------------------
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake-openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-06-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake-search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience-id")


# ---------------------------------------------------------------------------
# Minimal Azure fake
# ---------------------------------------------------------------------------
class _FakeSearchResultItem(dict):
    """dict subclass that also looks like an Azure SDK result row."""

    pass


class _FakeSearchClient:
    """Records every search call's kwargs and replays results lazily.

    Iteration is what produces rows — mirroring the real SDK, whose HTTP
    request (and any `HttpResponseError`) fires on iteration, not at the
    `.search(...)` call site.
    """

    def __init__(self, *, raise_on_semantic: bool = False):
        self._calls: list[dict] = []
        self._raise_on_semantic = raise_on_semantic

    def search(self, **kwargs):
        self._calls.append(kwargs)
        return self._results(kwargs)

    def _results(self, kwargs):
        # Lazy generator: raise / yield only on iteration, like the real pager.
        if self._raise_on_semantic and "query_type" in kwargs:
            raise HttpResponseError("Semantic search is not supported on this tier")
        score = 2.5 if "query_type" in kwargs else None
        row = {
            "id": "1",
            "content": "fake content",
            "section_header": "sec",
            "filename": "doc.md",
            "filepath": "/docs/doc.md",
        }
        if score is not None:
            row["@search.reranker_score"] = score
        yield _FakeSearchResultItem(row)


class FakeAzure:
    def __init__(self, *, raise_on_semantic: bool = False):
        self.search_client = _FakeSearchClient(raise_on_semantic=raise_on_semantic)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _set_semantic_config(monkeypatch, value: str) -> None:
    """Point the module-level LocalConfig at a semantic configuration name."""
    from src.services import search_service as ss

    monkeypatch.setattr(ss.localconfig, "AZURE_SEARCH_SEMANTIC_CONFIG", value)


# ===========================================================================
# Semantic enabled
# ===========================================================================


def test_semantic_config_sends_semantic_params(monkeypatch):
    """When the config is set, the search call carries query_type=SEMANTIC and
    the semantic configuration name."""
    from src.services.search_service import _search_sync

    _set_semantic_config(monkeypatch, "mySemanticConfig")
    fake_azure = FakeAzure()

    results = _search_sync(
        azure=fake_azure,
        query="what is procurement?",
        vector=[0.1] * 3,
        fr_mode="fr_read",
        bot_tag="tenant-a",
        top=20,
    )

    assert len(fake_azure.search_client._calls) == 1
    call = fake_azure.search_client._calls[0]
    assert call["query_type"] == QueryType.SEMANTIC
    assert call["semantic_configuration_name"] == "mySemanticConfig"
    # Filter + top preserved exactly.
    assert "fr_tag eq 'fr_read'" in call["filter"]
    assert "bot_tag eq 'tenant-a'" in call["filter"]
    assert call["top"] == 20
    # Results materialized; reranker score surfaced when present.
    assert results
    assert results[0]["@search.reranker_score"] == 2.5


def test_semantic_disabled_sends_no_semantic_params(monkeypatch):
    """Empty config = pure hybrid: no semantic params on the search call,
    identical to the prior behavior."""
    from src.services.search_service import _search_sync

    _set_semantic_config(monkeypatch, "")
    fake_azure = FakeAzure()

    results = _search_sync(
        azure=fake_azure,
        query="what is procurement?",
        vector=[0.1] * 3,
        fr_mode="fr_layout",
        bot_tag="tenant-b",
        top=20,
    )

    assert len(fake_azure.search_client._calls) == 1
    call = fake_azure.search_client._calls[0]
    assert "query_type" not in call
    assert "semantic_configuration_name" not in call
    assert "fr_tag eq 'fr_layout'" in call["filter"]
    assert "bot_tag eq 'tenant-b'" in call["filter"]
    assert call["top"] == 20
    # No reranker score in pure-hybrid mode.
    assert results
    assert "@search.reranker_score" not in results[0]


# ===========================================================================
# Graceful fallback on unsupported tier
# ===========================================================================


def test_semantic_unsupported_falls_back_to_hybrid(monkeypatch, caplog):
    """If Azure rejects the semantic query (HttpResponseError), the call
    retries WITHOUT semantic params and still returns hybrid results, logging
    a single warning."""
    from src.services.search_service import _search_sync

    _set_semantic_config(monkeypatch, "mySemanticConfig")
    fake_azure = FakeAzure(raise_on_semantic=True)

    with caplog.at_level(logging.WARNING):
        results = _search_sync(
            azure=fake_azure,
            query="what is procurement?",
            vector=[0.1] * 3,
            fr_mode="fr_read",
            bot_tag="tenant-a",
            top=20,
        )

    calls = fake_azure.search_client._calls
    # Two attempts: the semantic try, then the hybrid retry.
    assert len(calls) == 2
    assert calls[0]["query_type"] == QueryType.SEMANTIC
    assert "query_type" not in calls[1]
    assert "semantic_configuration_name" not in calls[1]
    # The retry preserved filter + top.
    assert "fr_tag eq 'fr_read'" in calls[1]["filter"]
    assert "bot_tag eq 'tenant-a'" in calls[1]["filter"]
    assert calls[1]["top"] == 20
    # Retrieval still produced results.
    assert results
    assert results[0]["filename"] == "doc.md"
    # Exactly one warning logged about the fallback.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "hybrid" in warnings[0].getMessage().lower()


def test_non_semantic_error_is_not_swallowed(monkeypatch):
    """A non-semantic search failure must still propagate (P0-6 behavior) —
    only the unsupported-tier HttpResponseError triggers the silent fallback,
    and even that is preserved; other errors raise."""
    from src.services.search_service import _search_sync

    _set_semantic_config(monkeypatch, "")  # pure hybrid; should not swallow
    fake_azure = FakeAzure()

    def boom(**kwargs):
        raise RuntimeError("search backend exploded")

    fake_azure.search_client.search = boom

    with pytest.raises(RuntimeError, match="search backend exploded"):
        _search_sync(
            azure=fake_azure,
            query="q",
            vector=[0.1] * 3,
            fr_mode="fr_read",
            bot_tag="tenant-a",
            top=20,
        )
