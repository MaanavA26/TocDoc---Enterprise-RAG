"""Optional LangChain integration for the TocDoc SDK.

This module is intentionally **not** imported by :mod:`tocdoc_sdk` itself, so the
core SDK (``httpx`` + ``pydantic`` only) imports and runs with no LangChain
installed. Import it explicitly after installing the optional extra::

    pip install "tocdoc-sdk[langchain]"

    from tocdoc_sdk.langchain import TocDocRetriever, AsyncTocDocRetriever

It provides LangChain ``BaseRetriever`` implementations backed by the SDK's QnA
clients:

- :class:`TocDocRetriever` — synchronous, wraps :class:`tocdoc_sdk.TocDocClient`.
- :class:`AsyncTocDocRetriever` — async, wraps
  :class:`tocdoc_sdk.AsyncTocDocClient` and implements the native async path.

Document mapping (deliberate, documented limitation)
----------------------------------------------------
The TocDoc ``/qna`` endpoint returns one grounded ``answer`` plus a flat
``{filename: filepath}`` citation map — it does **not** expose per-chunk source
text. So each returned :class:`~langchain_core.documents.Document` carries the
*answer* as ``page_content`` and one cited source in ``metadata`` (``source`` =
filepath, ``filename`` = filename). One Document is emitted per citation (or a
single Document with empty source metadata when the answer has no citations), so
a RAG chain still sees the grounded text and its provenance. This is a retrieval
*view* over a QnA endpoint, not a raw vector-store retriever.

Compatible with ``langchain-core`` 1.x (the version the services run on).
"""

from __future__ import annotations

import uuid

try:
    from langchain_core.callbacks import (
        AsyncCallbackManagerForRetrieverRun,
        CallbackManagerForRetrieverRun,
    )
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
    from pydantic import ConfigDict
except ImportError as exc:  # pragma: no cover - exercised via the guard test
    raise ImportError(
        "tocdoc_sdk.langchain requires the optional 'langchain' extra. "
        "Install it with:  pip install 'tocdoc-sdk[langchain]'"
    ) from exc

# Imported at runtime (not under TYPE_CHECKING) so pydantic can resolve the
# field annotations on the retriever models below. This is safe: the core
# clients never import this optional module, so there is no import cycle.
from .async_client import AsyncTocDocClient
from .client import TocDocClient
from .models import QnAAnswer


def _answer_to_documents(answer: QnAAnswer) -> list[Document]:
    """Map a :class:`~tocdoc_sdk.models.QnAAnswer` to LangChain Documents.

    Emits one Document per citation, each with the grounded answer as
    ``page_content`` and ``metadata={"source": filepath, "filename": filename}``.
    When the answer has no citations, a single Document with empty source
    metadata is returned so the grounded text is never dropped.
    """
    citations = answer.citations
    if not citations:
        return [Document(page_content=answer.answer, metadata={"source": "", "filename": ""})]
    return [
        Document(page_content=answer.answer, metadata={"source": filepath, "filename": filename})
        for filename, filepath in citations.items()
    ]


class TocDocRetriever(BaseRetriever):
    """A synchronous LangChain retriever backed by :class:`TocDocClient`.

    Example:
        >>> from tocdoc_sdk import TocDocClient
        >>> from tocdoc_sdk.langchain import TocDocRetriever
        >>> client = TocDocClient("https://your-tocdoc-host", token="...")
        >>> retriever = TocDocRetriever(client=client, bot_tag="acme", fr_tag="read")
        >>> docs = retriever.invoke("What is the refund policy?")
        >>> docs[0].metadata["source"]
        '/docs/policy.md'

    Attributes:
        client: A configured :class:`tocdoc_sdk.TocDocClient`.
        bot_tag: Bot/tenant identifier passed to every query.
        fr_tag: Feature/retrieval tag passed to every query (default ``"read"``).
        session_id: Optional fixed correlation id. When ``None`` (default) a
            fresh UUID is generated per retrieval call, mirroring the CLI.
    """

    # BaseRetriever is a pydantic model in langchain-core 1.x; the SDK client is
    # an arbitrary (non-pydantic) type, so allow it as a field value.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: TocDocClient
    bot_tag: str
    fr_tag: str = "read"
    session_id: str | None = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        """Query QnA via the SDK and return citation-tagged Documents."""
        answer = self.client.ask(
            session_id=self.session_id or str(uuid.uuid4()),
            bot_tag=self.bot_tag,
            fr_tag=self.fr_tag,
            query=query,
        )
        return _answer_to_documents(answer)


class AsyncTocDocRetriever(BaseRetriever):
    """An async LangChain retriever backed by :class:`AsyncTocDocClient`.

    Mirrors :class:`TocDocRetriever` but implements the native async retrieval
    path (``_aget_relevant_documents``) over :class:`AsyncTocDocClient`, so it
    never blocks the event loop on a sync client.

    Example:
        >>> from tocdoc_sdk import AsyncTocDocClient
        >>> from tocdoc_sdk.langchain import AsyncTocDocRetriever
        >>> client = AsyncTocDocClient("https://your-tocdoc-host", token="...")
        >>> retriever = AsyncTocDocRetriever(client=client, bot_tag="acme")
        >>> docs = await retriever.ainvoke("What is the refund policy?")  # doctest: +SKIP
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: AsyncTocDocClient
    bot_tag: str
    fr_tag: str = "read"
    session_id: str | None = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        """Synchronous retrieval is unsupported on the async retriever.

        ``BaseRetriever`` declares ``_get_relevant_documents`` abstract, so it
        must be defined; this retriever is async-only. Use :meth:`ainvoke` (or
        the LangChain async APIs), which route to :meth:`_aget_relevant_documents`.
        """
        raise NotImplementedError(
            "AsyncTocDocRetriever is async-only; use `ainvoke` / the async APIs. "
            "For synchronous retrieval use TocDocRetriever with a TocDocClient."
        )

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        """Query QnA via the async SDK and return citation-tagged Documents."""
        answer = await self.client.ask(
            session_id=self.session_id or str(uuid.uuid4()),
            bot_tag=self.bot_tag,
            fr_tag=self.fr_tag,
            query=query,
        )
        return _answer_to_documents(answer)


__all__ = ["AsyncTocDocRetriever", "TocDocRetriever"]
