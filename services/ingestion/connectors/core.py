"""Connector core: the interface, document model, config loader, and driver.

This is the source-agnostic spine shared by every connector (Blob today,
SharePoint later). It deliberately mirrors the only contract `upload()` actually
consumes and adds the minimum new surface the current code physically forces.

Tenant isolation is preserved **by construction**, not by policing:
- `bot_tag` / `fr_mode` / `source_type` are connector-instance config, applied
  by the driver from the connector — never read from a document payload, never
  per-item. Cross-tagging is structurally impossible.
- `ConnectorConfig` validates `bot_tag` against the exact existing
  `BOT_TAG_PATTERN` at init, rejecting invalid tags before any network call.
  (`upload()` itself does not validate bot_tag, so this is the connector's job.)
- Connectors never mint chunk IDs and never touch the index, so the P0-4 id
  scheme (which embeds bot_tag as its leading segment) keeps tenants partitioned
  exactly as /upload does today.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from observability import log_event

logger = logging.getLogger(__name__)

# The exact bot_tag pattern enforced in admin/routes.py:53. Duplicated here as a
# module constant so the connector layer validates identically without importing
# the FastAPI route module. Keep in sync with admin/routes.py.
BOT_TAG_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
_BOT_TAG_RE = re.compile(BOT_TAG_PATTERN)

# Per-file ceiling (ADR: 100 MB). Oversized items are skipped at enumerate so
# they never buffer in memory; fetch() re-validates as defense in depth.
MAX_FILE_BYTES = 100 * 1024 * 1024

# PDF magic bytes. v1 is PDF-only (upload() is hard-wired to PDF + Document
# Intelligence), so connectors filter to this allowlist at enumerate and
# validate the downloaded bytes in fetch().
_PDF_MAGIC = b"%PDF"


# ---------------------------------------------------------------------------
# Typed errors (P0-6 friendly — carry a safe class name, never secret content)
# ---------------------------------------------------------------------------


class ConnectorError(Exception):
    """Base class for connector-layer failures.

    Connector code raises typed errors and never swallows them, so the caller
    (and the future trigger endpoint's P0-6 ErrorEnvelope handlers) own the
    response. Messages must stay safe — no secrets, tokens, or connection
    strings — because they may surface in logs.
    """


class InvalidBotTagError(ConnectorError):
    """A connector was configured with a bot_tag that fails BOT_TAG_PATTERN."""


class NotAPdfError(ConnectorError):
    """Downloaded bytes did not start with the PDF magic header (%PDF).

    Raised in fetch() — a partial/interrupted download or a misfiltered item
    must not feed a corrupt PDF to Document Intelligence.
    """


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


class ConnectorFile:
    """The single hand-off type that `upload()` consumes.

    Exactly the duck type upload() already accepts: a `.filename: str` plus an
    `async def read() -> bytes`. This is the inline `_MockFile` shape from
    app.py promoted to one shared class so Blob and SharePoint do not each
    reinvent it.

    Bytes-backed: connectors download the COMPLETE content before constructing a
    ConnectorFile, because the deterministic document_id is sha256 over the whole
    file. `read()` returns those buffered bytes.

    Critically, ConnectorFile carries NO document_id or chunk ids — those are
    derived downstream inside upload(), so there is no second place to get the
    P0-4 id scheme wrong.
    """

    __slots__ = ("filename", "_content")

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


class SourceItem:
    """Opaque remote identity + the canonical source_path + a change validator.

    `source_path` is the immutable audit/reindex anchor written to every chunk
    (e.g. `blob://{container}/{blob_name}`). It MUST use opaque ids only and
    NEVER embed credentials, SAS query strings, or `user:pass@` forms — the
    admin API surfaces source_path verbatim.

    `validator` is the remote change-detection token (etag / last-modified /
    size), captured at enumerate without downloading bytes. Unused by the v1
    driver (which relies on content-hash idempotency + delete_by_source_path),
    but carried now so the deferred change-detection layer (ADR PR-6) needs no
    model change. `size` is the pre-download size used for the 100 MB skip.
    """

    __slots__ = ("identity", "source_path", "filename", "size", "validator")

    def __init__(
        self,
        *,
        identity: str,
        source_path: str,
        filename: str,
        size: int | None = None,
        validator: str | None = None,
    ) -> None:
        self.identity = identity
        self.source_path = source_path
        self.filename = filename
        self.size = size
        self.validator = validator

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceItem(source_path={self.source_path!r}, size={self.size})"


# ---------------------------------------------------------------------------
# Connector interface
# ---------------------------------------------------------------------------


@runtime_checkable
class SourceConnector(Protocol):
    """Thin enumerator + downloader feeding bytes into upload().

    Class/instance attributes:
        source_type: "blob" | "sharepoint" — stamped on every chunk.
        bot_tag:     bound at init, validated against BOT_TAG_PATTERN.
        fr_mode:     "read" | "layout" — bound per connector instance.

    Optional run correlation: ``run_connector`` sets a ``run_id`` attribute on
    the connector before enumerate() so the connector's own inner log events
    carry it as ``request_id``. It is NOT part of this structural (runtime_
    checkable) contract — connectors that never run inside the driver need not
    declare it — so it is documented here rather than annotated above.
    """

    source_type: str
    bot_tag: str
    fr_mode: str

    def enumerate(self) -> Iterator[SourceItem]:
        """Lazily yield items to ingest.

        Pagination is owned internally (continuation tokens / @odata.nextLink);
        the full listing is never materialized in memory. Filters to the PDF
        allowlist here and SKIPS (does not raise on) non-PDF and >100 MB items.
        """
        ...

    def fetch(self, item: SourceItem) -> ConnectorFile:
        """Download the COMPLETE bytes for one item.

        Uses a timeout + bounded retry; size is pre-validated against the 100 MB
        ceiling; PDF magic bytes are validated post-download. RAISES NotAPdfError
        on a corrupt/mis-typed download — that is an error, not a skip.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers shared by connector implementations
# ---------------------------------------------------------------------------


def is_pdf_name(name: str) -> bool:
    """Return True if `name` looks like a PDF by extension (case-insensitive)."""
    return name.lower().endswith(".pdf")


def validate_pdf_magic(content: bytes) -> None:
    """Raise NotAPdfError unless `content` starts with the %PDF magic header."""
    if not content[:4] == _PDF_MAGIC:
        raise NotAPdfError("Downloaded content is not a valid PDF (missing %PDF header)")


# ---------------------------------------------------------------------------
# Config loader (source → bot_tag binding)
# ---------------------------------------------------------------------------


class ConnectorConfig:
    """Validated connector-instance config binding a source to one bot_tag.

    bot_tag and fr_mode are connector-instance config, NOT per-item — this is
    how source→bot_tag binding (1:1 or N:1, never cross-tag) is enforced and
    cross-tagging is made structurally impossible. bot_tag is validated against
    BOT_TAG_PATTERN at init, rejecting invalid tags before any network call.
    """

    __slots__ = ("bot_tag", "fr_mode")

    def __init__(self, *, bot_tag: str, fr_mode: str = "read") -> None:
        if not isinstance(bot_tag, str) or not _BOT_TAG_RE.match(bot_tag):
            # Do not echo the raw value into the message — it may be long/garbage
            # and could end up in a log line.
            raise InvalidBotTagError(
                "bot_tag must match ^[A-Za-z0-9_-]{1,128}$ (connector config rejected before any network call)"
            )
        if fr_mode not in ("read", "layout"):
            raise ConnectorError("fr_mode must be 'read' or 'layout'")
        self.bot_tag = bot_tag
        self.fr_mode = fr_mode


# ---------------------------------------------------------------------------
# Per-(source_path, bot_tag) single-flight lock
# ---------------------------------------------------------------------------

# The driver's delete_by_source_path + upload window is non-atomic; two
# overlapping runs on the same (source_path, bot_tag) can race. ADR open-Q1
# ratifies an in-process asyncio lock for v1 (single-replica ingestion). A
# distributed lock (e.g. a Blob lease) is a documented follow-up if ingestion
# goes multi-replica.
_locks: dict[tuple[str, str], asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _single_flight(source_path: str, bot_tag: str) -> asyncio.Lock:
    """Return the process-wide lock for one (source_path, bot_tag) key."""
    key = (source_path, bot_tag)
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock


# ---------------------------------------------------------------------------
# The source-agnostic driver
# ---------------------------------------------------------------------------


async def run_connector(connector: SourceConnector, rag_instance, *, run_id: str | None = None) -> dict:
    """Drive one connector: enumerate → fetch → (lock → delete → upload).

    bot_tag / fr_mode / source_type come from the connector instance on every
    upload() call — never per-item — so tenant isolation holds by construction.
    The connector only feeds bytes; upload() mints the canonical chunk IDs and
    stamps bot_tag / source_path / source_type, so P0-4 + P0-5 stay in one place.

    Per (source_path, bot_tag) the driver takes a single-flight lock, runs
    delete_by_source_path (edited-file cleanup, ADR B) and then upload(). Errors
    propagate (P0-6): a failing item is logged and re-raised, not silently
    swallowed.

    Args:
        connector: a SourceConnector (enumerate + fetch + bot_tag/fr_mode/source_type).
        rag_instance: the custom_rag.rag() with upload + delete_by_source_path.
        run_id: optional correlation id threaded into structured logs alongside
            the inherited X-Request-ID.

    Returns:
        A summary dict: {"processed": int, "items": [source_path, ...]}.
    """
    # Thread the run_id onto the connector instance BEFORE enumerate() runs so
    # the connectors' own inner log events (e.g. connector_graph_throttled in
    # the SharePoint connector) carry it. Backward-compatible: connectors built
    # directly without a run keep their `run_id` class-attribute default (None).
    # Best-effort: run_id is an optional correlation field, so a connector using
    # __slots__ without a `run_id` slot (or a read-only attribute) simply won't
    # carry it rather than failing the whole run.
    with contextlib.suppress(AttributeError, TypeError):
        connector.run_id = run_id

    log_event(
        logger,
        "connector_run_started",
        request_id=run_id,
        service="ingestion",
        source_type=connector.source_type,
        bot_tag=connector.bot_tag,
        fr_mode=connector.fr_mode,
    )

    processed = 0
    items: list[str] = []
    for item in connector.enumerate():
        cfile = connector.fetch(item)
        lock = await _single_flight(item.source_path, connector.bot_tag)
        async with lock:
            try:
                # Edited-file cleanup BEFORE upload: a changed file gets a new
                # document_id, so the document_id-keyed stale-delete inside
                # upload() would miss the old chunks. This removes them first.
                await rag_instance.delete_by_source_path(item.source_path, connector.bot_tag)
                await rag_instance.upload(
                    cfile,
                    connector.bot_tag,
                    connector.fr_mode,
                    file_path=item.source_path,
                    source_type=connector.source_type,
                    request_id=run_id,
                )
            except Exception as exc:
                log_event(
                    logger,
                    "connector_item_failed",
                    request_id=run_id,
                    level=logging.ERROR,
                    source_type=connector.source_type,
                    bot_tag=connector.bot_tag,
                    source_path=item.source_path,
                    error_class=type(exc).__name__,
                    safe_message="Connector item ingestion failed",
                )
                raise
        processed += 1
        items.append(item.source_path)
        log_event(
            logger,
            "connector_item_completed",
            request_id=run_id,
            source_type=connector.source_type,
            bot_tag=connector.bot_tag,
            source_path=item.source_path,
        )

    log_event(
        logger,
        "connector_run_completed",
        request_id=run_id,
        source_type=connector.source_type,
        bot_tag=connector.bot_tag,
        processed=processed,
    )
    return {"processed": processed, "items": items}
