"""Command-line interface for the TocDoc SDK (``tocdoc``).

A thin ``argparse`` wrapper over the existing clients — :class:`TocDocClient`
(QnA) and :class:`AdminClient` (admin reads + connector control-plane). It adds
no new runtime dependencies: only the stdlib plus the SDK itself.

Design notes (load-bearing):

- :func:`main` *returns* an ``int`` exit code; the console entry point is wired
  as ``sys.exit(main())`` by setuptools, so returning non-zero yields the right
  shell status and keeps the function trivially testable.
- Credentials resolve from a flag OR an environment variable *after* parsing
  (never as an argparse ``default=``), so a token can never leak into ``--help``
  output. Tokens are sent to the clients as headers and are NEVER printed.
- The client classes are referenced as module-level names so tests can
  ``monkeypatch.setattr(cli, "TocDocClient", Fake)`` and run with no network.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Sequence

import httpx

from .admin import AdminClient
from .client import TocDocClient
from .errors import ApiError
from .models import (
    ConnectorRunListResponse,
    ConnectorRunStatusResponse,
    ConnectorSyncResponse,
    DocumentDetailResponse,
    DocumentListResponse,
    IndexStatsResponse,
    QnAAnswer,
)

PROG = "tocdoc"

# Environment-variable fallbacks for credentials and base URL. A CLI flag, when
# given, always wins over the corresponding variable.
# These are environment-variable NAMES (not secret values).
ENV_BASE_URL = "TOCDOC_BASE_URL"
ENV_TOKEN = "TOCDOC_TOKEN"
ENV_ADMIN_TOKEN = "TOCDOC_ADMIN_TOKEN"

EXIT_OK = 0
EXIT_ERROR = 1


class _CliError(Exception):
    """A user-facing CLI error (e.g. missing base URL/token).

    Raised inside command handlers and turned into a clean stderr message plus a
    non-zero exit code by :func:`main` — never a traceback.
    """


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser and its subcommands."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Command-line interface for the TocDoc QnA and admin APIs.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    _add_ask_parser(sub)
    _add_admin_parser(sub)

    return parser


def _add_base_url(p: argparse.ArgumentParser) -> None:
    """Add the shared ``--base-url`` flag (falls back to ``TOCDOC_BASE_URL``)."""
    p.add_argument(
        "--base-url",
        default=None,
        help=f"Base URL of the service. Falls back to ${ENV_BASE_URL}.",
    )


def _add_ask_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("ask", help="Ask the QnA service a question.")
    _add_base_url(p)
    p.add_argument(
        "--token",
        default=None,
        help=f"Bearer token (sent as Authorization). Falls back to ${ENV_TOKEN}. Never printed.",
    )
    p.add_argument("question", help="The question to ask.")
    p.add_argument(
        "--session-id",
        default=None,
        help="Correlation/session id. Defaults to a generated UUID.",
    )
    p.add_argument("--bot-tag", required=True, help="Bot/tenant identifier (required).")
    p.add_argument("--fr-tag", default="read", help="Feature/retrieval tag (default: read).")
    p.set_defaults(func=_cmd_ask)


def _add_admin_parser(sub: argparse._SubParsersAction) -> None:
    admin = sub.add_parser("admin", help="Admin API (read-only reads + connector control-plane).")
    asub = admin.add_subparsers(dest="admin_command", metavar="<admin-command>")
    asub.required = True

    def _admin_common(p: argparse.ArgumentParser) -> None:
        _add_base_url(p)
        p.add_argument(
            "--admin-token",
            default=None,
            help=f"Admin token (sent as X-Admin-Token). Falls back to ${ENV_ADMIN_TOKEN}. Never printed.",
        )

    docs = asub.add_parser("docs", help="List indexed documents in a bot_tag scope.")
    _admin_common(docs)
    docs.add_argument("--bot-tag", required=True, help="Bot/tenant identifier (required).")
    docs.set_defaults(func=_cmd_admin_docs)

    doc = asub.add_parser("doc", help="Show one document's detail.")
    _admin_common(doc)
    doc.add_argument("document_id", help="Document id.")
    doc.add_argument("--bot-tag", required=True, help="Bot/tenant identifier (required).")
    doc.set_defaults(func=_cmd_admin_doc)

    stats = asub.add_parser("index-stats", help="Show aggregate index stats for a bot_tag scope.")
    _admin_common(stats)
    stats.add_argument("--bot-tag", required=True, help="Bot/tenant identifier (required).")
    stats.set_defaults(func=_cmd_admin_index_stats)

    sync = asub.add_parser("sync", help="Trigger a connector sync for a source type.")
    _admin_common(sync)
    sync.add_argument("source_type", help="Connector source type (e.g. blob).")
    sync.set_defaults(func=_cmd_admin_sync)

    runs = asub.add_parser("runs", help="List recent connector sync runs (newest first).")
    _admin_common(runs)
    runs.add_argument("--limit", type=int, default=50, help="Max records to return (default: 50).")
    runs.set_defaults(func=_cmd_admin_runs)

    run = asub.add_parser("run", help="Show one connector sync run's status by run_id.")
    _admin_common(run)
    run.add_argument("run_id", help="Run id.")
    run.set_defaults(func=_cmd_admin_run)


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def _resolve_base_url(args: argparse.Namespace) -> str:
    base_url = args.base_url or os.environ.get(ENV_BASE_URL)
    if not base_url:
        raise _CliError(f"no base URL: pass --base-url or set ${ENV_BASE_URL}")
    return base_url


def _resolve_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get(ENV_TOKEN)
    if not token:
        raise _CliError(f"no token: pass --token or set ${ENV_TOKEN}")
    return token


def _resolve_admin_token(args: argparse.Namespace) -> str:
    token = args.admin_token or os.environ.get(ENV_ADMIN_TOKEN)
    if not token:
        raise _CliError(f"no admin token: pass --admin-token or set ${ENV_ADMIN_TOKEN}")
    return token


# ---------------------------------------------------------------------------
# Command handlers — each returns an exit code.
# ---------------------------------------------------------------------------


def _cmd_ask(args: argparse.Namespace) -> int:
    base_url = _resolve_base_url(args)
    token = _resolve_token(args)
    session_id = args.session_id or str(uuid.uuid4())

    with TocDocClient(base_url, token=token) as client:
        answer = client.ask(
            session_id=session_id,
            bot_tag=args.bot_tag,
            fr_tag=args.fr_tag,
            query=args.question,
        )
    _print_answer(answer)
    return EXIT_OK


def _cmd_admin_docs(args: argparse.Namespace) -> int:
    with _admin_client(args) as admin:
        result = admin.list_documents(bot_tag=args.bot_tag)
    _print_document_list(result)
    return EXIT_OK


def _cmd_admin_doc(args: argparse.Namespace) -> int:
    with _admin_client(args) as admin:
        result = admin.get_document(bot_tag=args.bot_tag, document_id=args.document_id)
    _print_document_detail(result)
    return EXIT_OK


def _cmd_admin_index_stats(args: argparse.Namespace) -> int:
    with _admin_client(args) as admin:
        result = admin.index_stats(bot_tag=args.bot_tag)
    _print_index_stats(result)
    return EXIT_OK


def _cmd_admin_sync(args: argparse.Namespace) -> int:
    with _admin_client(args) as admin:
        result = admin.trigger_connector_sync(args.source_type)
    _print_sync(result)
    return EXIT_OK


def _cmd_admin_runs(args: argparse.Namespace) -> int:
    with _admin_client(args) as admin:
        result = admin.list_connector_runs(limit=args.limit)
    _print_run_list(result)
    return EXIT_OK


def _cmd_admin_run(args: argparse.Namespace) -> int:
    with _admin_client(args) as admin:
        result = admin.get_connector_run(args.run_id)
    _print_run(result)
    return EXIT_OK


def _admin_client(args: argparse.Namespace) -> AdminClient:
    """Construct an :class:`AdminClient` from resolved base URL + admin token."""
    base_url = _resolve_base_url(args)
    admin_token = _resolve_admin_token(args)
    return AdminClient(base_url, admin_token=admin_token)


# ---------------------------------------------------------------------------
# Output formatting — plain text to stdout; never includes credentials.
# ---------------------------------------------------------------------------


def _print_answer(answer: QnAAnswer) -> None:
    print(answer.answer)
    citations = answer.citations
    if citations:
        print("\nCitations:")
        for filename, filepath in citations.items():
            print(f"  - {filename}: {filepath}")
    else:
        print("\nCitations: (none)")


def _print_document_list(result: DocumentListResponse) -> None:
    print(f"bot_tag={result.bot_tag} count={result.count}")
    for doc in result.documents:
        print(f"  - {doc.document_id}  chunks={doc.chunk_count}  source_type={doc.source_type}")


def _print_document_detail(result: DocumentDetailResponse) -> None:
    print(f"document_id={result.document_id}")
    print(f"bot_tag={result.bot_tag}")
    print(f"source_path={result.source_path}")
    print(f"source_type={result.source_type}")
    print(f"fr_tag={result.fr_tag}")
    print(f"chunk_count={result.chunk_count}")
    if result.sample_chunks:
        print("sample_chunks:")
        for chunk in result.sample_chunks:
            print(f"  - {chunk.id}  chunk_index={chunk.chunk_index}")


def _print_index_stats(result: IndexStatsResponse) -> None:
    print(f"bot_tag={result.bot_tag}")
    print(f"document_count={result.document_count}")
    print(f"chunk_count={result.chunk_count}")
    print(f"source_types={result.source_types}")
    print(f"fr_modes={result.fr_modes}")


def _print_sync(result: ConnectorSyncResponse) -> None:
    print(f"run_id={result.run_id}  source_type={result.source_type}  status={result.status}")


def _print_run_list(result: ConnectorRunListResponse) -> None:
    print(f"count={result.count}")
    for run in result.runs:
        print(f"  - {run.run_id}  status={run.status}  source_type={run.source_type}")


def _print_run(result: ConnectorRunStatusResponse) -> None:
    print(f"run_id={result.run_id}")
    print(f"status={result.status}")
    print(f"source_type={result.source_type}")
    print(f"bot_tag={result.bot_tag}")
    print(f"processed_count={result.processed_count}")
    print(f"failed_count={result.failed_count}")
    if result.error is not None:
        print(f"error={result.error.error_class}: {result.error.safe_message}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``tocdoc`` CLI and return a process exit code.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, non-zero on a handled error. Argument-parsing errors
        raise ``SystemExit(2)`` from argparse, as usual.

    Errors from the API (:class:`ApiError`), transport failures (an
    ``httpx.HTTPError`` such as a connection refusal after retries), and
    missing-credential conditions are all caught here, reported as a clean
    one-line message on stderr, and turned into a non-zero exit code — never a
    traceback. The token is never printed.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except ApiError as exc:
        print(f"error: [{exc.status_code}] {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except _CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except httpx.HTTPError as exc:
        # Transport-level failure (e.g. connect refused/timed out after retries).
        # Report the exception CLASS, not str(exc), to avoid echoing the URL.
        print(f"error: connection failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
