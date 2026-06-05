# 03_admin.py — Admin API: documents, index stats, and a connector sync.
#
# What this shows:
#   Using `AdminClient` against the ingestion service's read-only admin API and
#   connector control-plane:
#     1. list indexed documents for a bot_tag scope,
#     2. read aggregate index stats,
#     3. trigger a connector sync (HTTP 202, returns a run handle), then poll
#        `get_connector_run` until the run reaches a terminal state.
#
# The admin API authenticates with a static `X-Admin-Token` header (NOT the QnA
# bearer token), so it uses a separate token. If both services sit behind one
# gateway you can pass the same base URL; if they are on separate hosts, point
# TOCDOC_BASE_URL at the ingestion service.
#
# Environment variables (never hardcode credentials):
#   TOCDOC_BASE_URL    Base URL of the ingestion service (admin routes live here).
#   TOCDOC_ADMIN_TOKEN Static admin token. Sent as the `X-Admin-Token` header.
#
# Run:
#   export TOCDOC_BASE_URL=https://your-host/upload_pipeline
#   export TOCDOC_ADMIN_TOKEN=...
#   python examples/03_admin.py            # uses default bot_tag/source_type
#   python examples/03_admin.py acme blob  # bot_tag=acme, connector source_type=blob
"""Admin reads + connector sync/poll via :class:`tocdoc_sdk.AdminClient`."""

from __future__ import annotations

import os
import sys
import time

from tocdoc_sdk import AdminClient, ApiError

ENV_BASE_URL = "TOCDOC_BASE_URL"
ENV_ADMIN_TOKEN = "TOCDOC_ADMIN_TOKEN"

# Run is terminal once the server reports one of these statuses.
TERMINAL_STATUSES = {"completed", "failed"}


def _poll_run(admin: AdminClient, run_id: str, *, attempts: int = 10, delay: float = 1.0) -> None:
    """Poll a connector run until it reaches a terminal status (or attempts run out)."""
    for _ in range(attempts):
        run = admin.get_connector_run(run_id)
        print(
            f"  run {run.run_id}: status={run.status} processed={run.processed_count} failed={run.failed_count}"
        )
        if run.status in TERMINAL_STATUSES:
            if run.error is not None:
                print(f"  run failed: {run.error.error_class}: {run.error.safe_message}")
            return
        time.sleep(delay)
    print("  run did not reach a terminal status within the poll budget")


def main(argv: list[str] | None = None) -> int:
    """List docs, show index stats, trigger a sync, and poll it. Returns an exit code."""
    args = sys.argv[1:] if argv is None else argv
    bot_tag = args[0] if len(args) > 0 else "acme"
    source_type = args[1] if len(args) > 1 else "blob"

    base_url = os.environ.get(ENV_BASE_URL)
    admin_token = os.environ.get(ENV_ADMIN_TOKEN)
    if not base_url or not admin_token:
        print(f"error: set ${ENV_BASE_URL} and ${ENV_ADMIN_TOKEN}", file=sys.stderr)
        return 1

    with AdminClient(base_url, admin_token=admin_token) as admin:
        try:
            # 1. List documents in this bot_tag scope.
            docs = admin.list_documents(bot_tag=bot_tag)
            print(f"documents (bot_tag={docs.bot_tag}, count={docs.count}):")
            for doc in docs.documents:
                print(f"  - {doc.document_id}  chunks={doc.chunk_count}  source_type={doc.source_type}")

            # 2. Aggregate index stats.
            stats = admin.index_stats(bot_tag=bot_tag)
            print(
                f"\nindex stats: documents={stats.document_count} chunks={stats.chunk_count} "
                f"source_types={stats.source_types} fr_modes={stats.fr_modes}"
            )

            # 3. Trigger a connector sync (202 Accepted) and poll the run.
            #    The connector's bot_tag/location are bound server-side from env,
            #    so source_type is the only input.
            print(f"\ntriggering connector sync for source_type={source_type} ...")
            sync = admin.trigger_connector_sync(source_type)
            print(f"  started run_id={sync.run_id} status={sync.status}")
            _poll_run(admin, sync.run_id)
        except ApiError as exc:
            print(f"error: [{exc.status_code}] {exc.code}: {exc.message}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
