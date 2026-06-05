# 04_langchain_retriever.py — TocDocRetriever in a minimal LangChain chain.
#
# What this shows:
#   Wrapping the SDK as a LangChain `BaseRetriever` (`TocDocRetriever`) and
#   composing it into a tiny LCEL chain WITHOUT any LLM. The chain is just:
#       retriever | RunnableLambda(format_docs)
#   The retriever queries `/qna` under the hood and returns one LangChain
#   Document per citation (the grounded answer as `page_content`, the cited
#   source in `metadata`). We then format those Documents into plain text.
#
# Requires the optional extra (langchain-core only — no LLM provider needed):
#   pip install "tocdoc-sdk[langchain]"
#
# Environment variables (never hardcode credentials):
#   TOCDOC_BASE_URL  Base URL of the QnA service (e.g. https://your-host/qna).
#   TOCDOC_TOKEN     Bearer token (Azure AD JWT). Sent as `Authorization: Bearer …`.
#
# Run:
#   export TOCDOC_BASE_URL=https://your-host/qna
#   export TOCDOC_TOKEN=eyJ...
#   python examples/04_langchain_retriever.py "What is the refund policy?"
"""Use :class:`tocdoc_sdk.langchain.TocDocRetriever` in a minimal LCEL chain."""

from __future__ import annotations

import os
import sys

from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda
from tocdoc_sdk import TocDocClient
from tocdoc_sdk.langchain import TocDocRetriever

ENV_BASE_URL = "TOCDOC_BASE_URL"
ENV_TOKEN = "TOCDOC_TOKEN"


def format_docs(docs: list[Document]) -> str:
    """Render retrieved Documents (answer + provenance) as plain text."""
    if not docs:
        return "(no documents returned)"
    # Every Document carries the same grounded answer as page_content; the
    # citations differ in metadata. Show the answer once, then list sources.
    answer = docs[0].page_content
    sources = [d.metadata.get("source", "") for d in docs if d.metadata.get("source")]
    lines = [answer]
    if sources:
        lines.append("\nSources:")
        lines.extend(f"  - {src}" for src in sources)
    return "\n".join(lines)


def build_chain(client: TocDocClient) -> RunnableLambda:
    """Build `retriever | format_docs` — a Runnable taking a query string -> text.

    Factored out so tests can build the chain with an injected client and invoke
    it without any network or LLM.
    """
    retriever = TocDocRetriever(client=client, bot_tag="acme", fr_tag="read")
    # The retriever is itself a Runnable, so `|` composes it with our formatter.
    return retriever | RunnableLambda(format_docs)


def main(argv: list[str] | None = None) -> int:
    """Run the retriever chain for one query and print the result. Returns an exit code."""
    args = sys.argv[1:] if argv is None else argv
    question = args[0] if args else "What is the refund policy?"

    base_url = os.environ.get(ENV_BASE_URL)
    token = os.environ.get(ENV_TOKEN)
    if not base_url or not token:
        print(f"error: set ${ENV_BASE_URL} and ${ENV_TOKEN}", file=sys.stderr)
        return 1

    with TocDocClient(base_url, token=token) as client:
        chain = build_chain(client)
        result = chain.invoke(question)

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
