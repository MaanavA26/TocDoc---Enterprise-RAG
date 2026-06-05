# 02_streaming.py — Token streaming with the async TocDoc SDK.
#
# What this shows:
#   Streaming a grounded answer token-by-token over Server-Sent Events using
#   `AsyncTocDocClient.stream_ask(...)`. Each yielded payload is a token/chunk;
#   we print them as they arrive (no newline) for a live "typing" effect. The
#   SDK posts to `<TOCDOC_BASE_URL>/qna/stream` and never yields the `[DONE]`
#   sentinel that terminates the stream.
#
# Note: this mirrors the SDK's `stream_ask` method. The `/qna/stream` route is
# forward-looking — it is not (yet) part of the documented REST surface in
# docs/API.md, so it requires a deployment that serves the SSE endpoint.
#
# Environment variables (never hardcode credentials):
#   TOCDOC_BASE_URL  Base URL of the QnA service (e.g. https://your-host/qna).
#   TOCDOC_TOKEN     Bearer token (Azure AD JWT). Sent as `Authorization: Bearer …`.
#
# Run:
#   export TOCDOC_BASE_URL=https://your-host/qna
#   export TOCDOC_TOKEN=eyJ...
#   python examples/02_streaming.py "Summarize the onboarding guide."
"""Async token streaming via :meth:`tocdoc_sdk.AsyncTocDocClient.stream_ask`."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from tocdoc_sdk import ApiError, AsyncTocDocClient

ENV_BASE_URL = "TOCDOC_BASE_URL"
ENV_TOKEN = "TOCDOC_TOKEN"


async def main(argv: list[str] | None = None) -> int:
    """Stream one answer token-by-token to stdout. Returns an exit code."""
    args = sys.argv[1:] if argv is None else argv
    question = args[0] if args else "Summarize the onboarding guide."

    base_url = os.environ.get(ENV_BASE_URL)
    token = os.environ.get(ENV_TOKEN)
    if not base_url or not token:
        print(f"error: set ${ENV_BASE_URL} and ${ENV_TOKEN}", file=sys.stderr)
        return 1

    async with AsyncTocDocClient(base_url, token=token) as client:
        try:
            # `stream_ask` is an async generator: consume it promptly so the
            # underlying HTTP connection is released when the stream ends.
            async for token_chunk in client.stream_ask(
                session_id=str(uuid.uuid4()),
                bot_tag="acme",
                fr_tag="read",
                query=question,
            ):
                print(token_chunk, end="", flush=True)
        except ApiError as exc:
            # A non-2xx status raises before any token is yielded.
            print(f"\nerror: [{exc.status_code}] {exc.code}: {exc.message}", file=sys.stderr)
            return 1

    print()  # final newline after the streamed answer
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
