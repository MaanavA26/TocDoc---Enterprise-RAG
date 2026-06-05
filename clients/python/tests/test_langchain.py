"""Tests for the optional LangChain retriever integration.

The whole module is SKIPPED when langchain-core is not installed (the core
``[dev]`` install), so it never errors on collection there. It runs only in the
``[dev,langchain]`` install. The SDK client is mocked via ``httpx.MockTransport``
so there is no live server.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

# Skip the entire module (cleanly, not an error) when langchain-core is absent.
pytest.importorskip("langchain_core")

from langchain_core.documents import Document  # noqa: E402
from langchain_core.retrievers import BaseRetriever  # noqa: E402
from tocdoc_sdk import AsyncTocDocClient, TocDocClient  # noqa: E402
from tocdoc_sdk.langchain import AsyncTocDocRetriever, TocDocRetriever  # noqa: E402

BASE_URL = "https://qna.example.test"


def _answer_handler(answer: str, citation: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/qna"
        return httpx.Response(200, json={"answer": answer, "citation": citation})

    return handler


def _sync_client(handler) -> TocDocClient:
    return TocDocClient(BASE_URL, transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def test_retriever_is_a_base_retriever():
    retriever = TocDocRetriever(client=_sync_client(_answer_handler("a", {})), bot_tag="acme")
    assert isinstance(retriever, BaseRetriever)


def test_retriever_returns_documents_with_citation_metadata():
    handler = _answer_handler(
        "Refunds take 30 days.",
        {"policy.md": "/docs/policy.md", "terms.md": "/docs/terms.md"},
    )
    retriever = TocDocRetriever(client=_sync_client(handler), bot_tag="acme", fr_tag="read")

    docs = retriever.invoke("What is the refund policy?")

    assert len(docs) == 2
    assert all(isinstance(d, Document) for d in docs)
    assert all(d.page_content == "Refunds take 30 days." for d in docs)
    sources = {d.metadata["source"] for d in docs}
    filenames = {d.metadata["filename"] for d in docs}
    assert sources == {"/docs/policy.md", "/docs/terms.md"}
    assert filenames == {"policy.md", "terms.md"}


def test_retriever_no_citations_returns_single_document():
    retriever = TocDocRetriever(client=_sync_client(_answer_handler("No sources.", {})), bot_tag="acme")
    docs = retriever.invoke("anything?")
    assert len(docs) == 1
    assert docs[0].page_content == "No sources."
    assert docs[0].metadata == {"source": "", "filename": ""}


def test_retriever_forwards_bot_tag_and_fr_tag():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"answer": "ok", "citation": {}})

    retriever = TocDocRetriever(client=_sync_client(handler), bot_tag="tenant-x", fr_tag="deep")
    retriever.invoke("q")

    assert captured["bot_tag"] == "tenant-x"
    assert captured["fr_tag"] == "deep"
    assert captured["bot"][0]["user_query"] == "q"


def test_async_retriever_returns_documents_with_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "async ans", "citation": {"a.md": "/a.md"}})

    async def run() -> list[Document]:
        client = AsyncTocDocClient(BASE_URL, transport=httpx.MockTransport(handler))
        retriever = AsyncTocDocRetriever(client=client, bot_tag="acme")
        try:
            return await retriever.ainvoke("q")
        finally:
            await client.aclose()

    docs = asyncio.run(run())
    assert len(docs) == 1
    assert docs[0].page_content == "async ans"
    assert docs[0].metadata["source"] == "/a.md"
    assert docs[0].metadata["filename"] == "a.md"
