"""
Tests for concurrent request isolation and bot_tag tenant scoping.

P0-3: Verifies that generate_answer() no longer uses a module-level global
      for conversation history; history is passed explicitly as a parameter.

P0-2: Verifies that perform_search() enforces bot_tag filtering and rejects
      empty bot_tag values before any search executes.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    """dict subclass that also looks like an Azure SDK result object."""

    pass


class _FakeSearchClient:
    def __init__(self):
        self._calls = []

    def search(self, **kwargs):
        self._calls.append(kwargs)
        yield _FakeSearchResultItem(
            {
                "id": "1",
                "content": "fake content",
                "section_header": "sec",
                "filename": "doc.md",
                "filepath": "/docs/doc.md",
            }
        )


class FakeAzure:
    def __init__(self):
        self.search_client = _FakeSearchClient()
        self.embedding_client = MagicMock()
        self.openai_client = MagicMock()


# ===========================================================================
# P0-2: search_service tests
# ===========================================================================


@pytest.mark.asyncio
async def test_search_filter_includes_bot_tag():
    """
    The filter expression passed to the Azure Search client must contain
    both fr_tag and bot_tag constraints so that one tenant cannot retrieve
    another tenant's documents.
    """
    from src.services import search_service as ss

    fake_azure = FakeAzure()

    # Patch _search_sync so we can capture the filter expression it builds
    captured = {}

    def fake_search_sync(azure, query, vector, fr_mode, bot_tag, top):
        filter_expr = f"fr_tag eq '{fr_mode}' and bot_tag eq '{bot_tag}'"
        captured["filter_expr"] = filter_expr
        captured["bot_tag"] = bot_tag
        captured["fr_mode"] = fr_mode
        return []

    with patch.object(ss, "_search_sync", side_effect=fake_search_sync):
        await ss.perform_search(
            fake_azure,
            "what is procurement?",
            [0.1] * 3,
            "fr_read",
            "tenant-a",
        )

    assert captured["bot_tag"] == "tenant-a"
    assert captured["fr_mode"] == "fr_read"
    assert "bot_tag eq 'tenant-a'" in captured["filter_expr"]
    assert "fr_tag eq 'fr_read'" in captured["filter_expr"]


@pytest.mark.asyncio
async def test_empty_bot_tag_raises_value_error():
    """
    An empty bot_tag must be rejected with ValueError before any search
    is attempted, preventing accidental cross-tenant data access.
    """
    from src.services.search_service import perform_search

    fake_azure = FakeAzure()

    with pytest.raises(ValueError, match="bot_tag is required"):
        await perform_search(fake_azure, "query", [0.1] * 3, "fr_read", "")


@pytest.mark.asyncio
async def test_whitespace_only_bot_tag_raises_value_error():
    """
    A whitespace-only bot_tag is treated the same as empty and must be
    rejected before any search executes.
    """
    from src.services.search_service import perform_search

    fake_azure = FakeAzure()

    with pytest.raises(ValueError, match="bot_tag is required"):
        await perform_search(fake_azure, "query", [0.1] * 3, "fr_read", "   ")


@pytest.mark.asyncio
async def test_search_sync_filter_expression():
    """
    _search_sync must build a compound filter expression that combines both
    fr_tag and bot_tag so Azure Search enforces tenant isolation server-side.
    """
    from src.services.search_service import _search_sync

    fake_azure = FakeAzure()

    _search_sync(
        azure=fake_azure,
        query="test query",
        vector=[0.1] * 3,
        fr_mode="fr_layout",
        bot_tag="tenant-b",
        top=5,
    )

    assert len(fake_azure.search_client._calls) == 1
    search_call = fake_azure.search_client._calls[0]
    filter_expr = search_call["filter"]
    assert "fr_tag eq 'fr_layout'" in filter_expr
    assert "bot_tag eq 'tenant-b'" in filter_expr


@pytest.mark.asyncio
async def test_different_bot_tags_produce_different_filters():
    """
    Two calls with different bot_tags must produce different filter expressions,
    confirming that tenant isolation is enforced per-request.
    """
    from src.services.search_service import _search_sync

    azure_a = FakeAzure()
    azure_b = FakeAzure()

    _search_sync(azure=azure_a, query="q", vector=[0.1] * 3, fr_mode="fr_read", bot_tag="tenant-a", top=3)
    _search_sync(azure=azure_b, query="q", vector=[0.1] * 3, fr_mode="fr_read", bot_tag="tenant-b", top=3)

    filter_a = azure_a.search_client._calls[0]["filter"]
    filter_b = azure_b.search_client._calls[0]["filter"]
    assert "tenant-a" in filter_a
    assert "tenant-b" not in filter_a
    assert "tenant-b" in filter_b
    assert "tenant-a" not in filter_b


# ===========================================================================
# P0-3: qna_pipeline tests
# ===========================================================================


@pytest.mark.asyncio
async def test_generate_answer_accepts_history_parameter():
    """
    generate_answer() must accept a `history` parameter and use it for
    conversation context — the module-level bot_queries global must no longer
    exist in qna_pipeline.
    """
    from src.pipeline import qna_pipeline

    # The global bot_queries must have been removed
    assert not hasattr(qna_pipeline, "bot_queries"), (
        "bot_queries module-level global still exists in qna_pipeline — "
        "it must be removed to prevent concurrent request contamination"
    )


@pytest.mark.asyncio
async def test_generate_answer_uses_provided_history_not_global():
    """
    Two concurrent calls with different histories must each receive their own
    history without cross-contamination. Verifies that the history parameter
    passed to generate_answer() is actually forwarded to _latest_three_and_reply.
    """
    from src.pipeline import qna_pipeline

    captured_histories = []

    original_latest_three = qna_pipeline._latest_three_and_reply

    def capturing_latest_three(hist):
        captured_histories.append(list(hist))
        return original_latest_three(hist)

    history_a = [{"user_query": "question from tenant A", "bot_response": None}]
    history_b = [{"user_query": "question from tenant B", "bot_response": None}]

    fake_azure = FakeAzure()

    with (
        patch.object(qna_pipeline, "_latest_three_and_reply", side_effect=capturing_latest_three),
        patch.object(
            qna_pipeline,
            "rephrase_queries",
            new=AsyncMock(
                return_value={
                    "rephrased_query": "rephrased",
                    "is_greeting": True,  # skip retrieval to avoid embedding calls
                    "extracted_snippet": "",
                    "is_followup": False,
                    "was_rephrased": False,
                }
            ),
        ),
        patch.object(
            qna_pipeline,
            "generate_openai_response",
            new=AsyncMock(return_value="Answer text.\n\n**Sources:\n"),
        ),
        patch.object(
            qna_pipeline,
            "extract_answer_and_filenames_from_text",
            new=AsyncMock(return_value=("Answer text.", [])),
        ),
    ):
        # Call generate_answer with two distinct histories sequentially
        await qna_pipeline.generate_answer(
            query="question from tenant A",
            fr_mode="read",
            bot_tag="tenant-a",
            history=history_a,
            azure=fake_azure,
        )
        await qna_pipeline.generate_answer(
            query="question from tenant B",
            fr_mode="read",
            bot_tag="tenant-b",
            history=history_b,
            azure=fake_azure,
        )

    assert len(captured_histories) == 2
    # First call must have received tenant A's history
    assert captured_histories[0][0]["user_query"] == "question from tenant A"
    # Second call must have received tenant B's history
    assert captured_histories[1][0]["user_query"] == "question from tenant B"


@pytest.mark.asyncio
async def test_generate_answer_none_history_treated_as_empty():
    """
    If history=None is passed (defensive call), generate_answer must not crash;
    it must treat it as an empty list.
    """
    from src.pipeline import qna_pipeline

    fake_azure = FakeAzure()

    with (
        patch.object(
            qna_pipeline,
            "rephrase_queries",
            new=AsyncMock(
                return_value={
                    "rephrased_query": "hi",
                    "is_greeting": True,
                    "extracted_snippet": "",
                    "is_followup": False,
                    "was_rephrased": False,
                }
            ),
        ),
        patch.object(
            qna_pipeline, "generate_openai_response", new=AsyncMock(return_value="Hello!\n\n**Sources:\n")
        ),
        patch.object(
            qna_pipeline, "extract_answer_and_filenames_from_text", new=AsyncMock(return_value=("Hello!", []))
        ),
    ):
        result = await qna_pipeline.generate_answer(
            query="hi",
            fr_mode="read",
            bot_tag="tenant-x",
            history=None,  # type: ignore[arg-type]
            azure=fake_azure,
        )

    assert "answer" in result
    assert "citation" in result


@pytest.mark.asyncio
async def test_generate_answer_passes_bot_tag_to_search():
    """
    bot_tag passed into generate_answer() must be forwarded to perform_search()
    so the search layer enforces tenant isolation.
    """
    from src.pipeline import qna_pipeline

    fake_azure = FakeAzure()
    captured_search_calls = []

    async def fake_perform_search(azure, query, vector, fr_mode, bot_tag):
        captured_search_calls.append({"fr_mode": fr_mode, "bot_tag": bot_tag})
        return []

    with (
        patch.object(
            qna_pipeline,
            "rephrase_queries",
            new=AsyncMock(
                return_value={
                    "rephrased_query": "what is procurement?",
                    "is_greeting": False,
                    "extracted_snippet": "",
                    "is_followup": False,
                    "was_rephrased": True,
                }
            ),
        ),
        patch.object(qna_pipeline, "get_embedding", new=AsyncMock(return_value=[0.1, 0.2, 0.3])),
        patch.object(qna_pipeline, "perform_search", side_effect=fake_perform_search),
        patch.object(
            qna_pipeline, "generate_openai_response", new=AsyncMock(return_value="Answer.\n\n**Sources:\n")
        ),
        patch.object(
            qna_pipeline,
            "extract_answer_and_filenames_from_text",
            new=AsyncMock(return_value=("Answer.", [])),
        ),
    ):
        await qna_pipeline.generate_answer(
            query="what is procurement?",
            fr_mode="read",
            bot_tag="my-tenant",
            history=[{"user_query": "what is procurement?", "bot_response": None}],
            azure=fake_azure,
        )

    assert len(captured_search_calls) == 1
    assert captured_search_calls[0]["bot_tag"] == "my-tenant"
    assert captured_search_calls[0]["fr_mode"] == "fr_read"


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_cross_contaminate():
    """
    Two concurrent calls with different bot_tags must each receive independent
    results without cross-contamination. Patches are set up once outside
    asyncio.gather to avoid race conditions on module-level symbols.
    """
    from src.pipeline import qna_pipeline

    fake_azure = FakeAzure()
    results = {}

    async def fake_rephrase(
        azure, current_query, prev_query, prev_prev_query, latest_bot_reply, full_history
    ):
        return {
            "rephrased_query": current_query,
            "is_greeting": True,
            "extracted_snippet": "",
            "is_followup": False,
            "was_rephrased": False,
        }

    async def fake_openai_response(query, knowledge_source, *, is_greeting, is_follow_up, azure):
        # Return a response that embeds the query so we can assert no cross-contamination
        return f"Answer for '{query}'.\n\n**Sources:\n"

    async def fake_extract(text):
        answer = text.split("\n\n")[0]
        return (answer, [])

    with (
        patch.object(qna_pipeline, "rephrase_queries", side_effect=fake_rephrase),
        patch.object(qna_pipeline, "generate_openai_response", side_effect=fake_openai_response),
        patch.object(qna_pipeline, "extract_answer_and_filenames_from_text", side_effect=fake_extract),
    ):

        async def run_request(tenant_id: str, user_q: str):
            hist = [{"user_query": user_q, "bot_response": None}]
            result = await qna_pipeline.generate_answer(
                query=user_q,
                fr_mode="read",
                bot_tag=tenant_id,
                history=hist,
                azure=fake_azure,
            )
            results[tenant_id] = result

        await asyncio.gather(
            run_request("tenant-x", "What is the budget?"),
            run_request("tenant-y", "Who is the supplier?"),
        )

    assert "Answer for 'What is the budget?'" in results["tenant-x"]["answer"]
    assert "Answer for 'Who is the supplier?'" in results["tenant-y"]["answer"]
    assert "tenant-y" not in results["tenant-x"]["answer"]
    assert "tenant-x" not in results["tenant-y"]["answer"]
