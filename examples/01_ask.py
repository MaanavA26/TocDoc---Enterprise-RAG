# 01_ask.py — Basic Q&A with the synchronous TocDoc SDK.
#
# What this shows:
#   The simplest end-to-end call: construct a `TocDocClient`, ask one question
#   with `client.ask(...)`, and print the grounded answer plus its citations.
#
# Environment variables (never hardcode credentials):
#   TOCDOC_BASE_URL  Base URL of the QnA service (e.g. https://your-host/qna).
#                    The SDK POSTs to `<TOCDOC_BASE_URL>/qna`.
#   TOCDOC_TOKEN     Bearer token (Azure AD JWT). Sent as `Authorization: Bearer …`.
#
# Run:
#   export TOCDOC_BASE_URL=https://your-host/qna
#   export TOCDOC_TOKEN=eyJ...
#   python examples/01_ask.py "What is the refund policy?"
"""Basic synchronous Q&A via :class:`tocdoc_sdk.TocDocClient`."""

from __future__ import annotations

import os
import sys
import uuid

from tocdoc_sdk import ApiError, TocDocClient

# Environment-variable NAMES (not secret values). These mirror the `tocdoc` CLI.
ENV_BASE_URL = "TOCDOC_BASE_URL"
ENV_TOKEN = "TOCDOC_TOKEN"


def main(argv: list[str] | None = None) -> int:
    """Ask one question and print the answer + citations. Returns an exit code."""
    args = sys.argv[1:] if argv is None else argv
    question = args[0] if args else "What is the refund policy?"

    base_url = os.environ.get(ENV_BASE_URL)
    token = os.environ.get(ENV_TOKEN)
    if not base_url or not token:
        print(f"error: set ${ENV_BASE_URL} and ${ENV_TOKEN}", file=sys.stderr)
        return 1

    # `bot_tag` is the tenant/bot identifier; `fr_tag` is the feature/retrieval
    # tag (e.g. "read" or "layout"). Use a context manager so the connection
    # pool is closed when we are done.
    with TocDocClient(base_url, token=token) as client:
        try:
            answer = client.ask(
                session_id=str(uuid.uuid4()),
                bot_tag="acme",
                fr_tag="read",
                query=question,
            )
        except ApiError as exc:
            # The SDK raises a structured ApiError for any non-2xx response.
            print(f"error: [{exc.status_code}] {exc.code}: {exc.message}", file=sys.stderr)
            return 1

    print(answer.answer)
    if answer.citations:
        print("\nCitations:")
        for filename, filepath in answer.citations.items():
            print(f"  - {filename}: {filepath}")
    else:
        print("\nCitations: (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
