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
import threading
from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from loaders import get_extension
from loaders import is_supported_name as _registry_is_supported_name
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

# Content magic bytes used as a post-download integrity gate (defense in depth
# against a partial/interrupted download or a mis-filtered item). Only formats
# with a stable, cheap magic signature are gated:
#   - PDF          → b"%PDF"
#   - DOCX / PPTX  → b"PK" (OOXML files are zip containers)
# Text formats (HTML/HTM/MD/TXT) have no reliable magic header, so they are not
# gated here — the loader registry surfaces any genuinely-malformed content.
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK"
# Extensions whose bytes are validated as a zip/OOXML container post-download.
_OOXML_EXTENSIONS = frozenset({".docx", ".pptx"})


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

    Raised in fetch() for a ``.pdf`` item — a partial/interrupted download or a
    misfiltered item must not feed a corrupt PDF to Document Intelligence.
    """


class InvalidContentError(ConnectorError):
    """Downloaded bytes failed a non-PDF format's post-download magic-byte gate.

    Raised in fetch() for a gated non-PDF format (DOCX/PPTX) whose bytes do not
    look like the expected container (an OOXML file that is not a zip). Like
    NotAPdfError, this is an error (corrupt/mis-typed download), not a skip.
    """


class ConnectorRunError(ConnectorError):
    """A connector run aborted mid-flight on an item failure (L-Conn2).

    ``run_connector`` is fail-fast: the first failing item aborts the run. The
    original implementation re-raised the bare item exception, so the running
    ``processed`` count (e.g. 199/200 already-ingested files) was lost — the
    background driver then recorded ``processed_count=0``, misreporting a
    near-complete run as zero progress.

    This wrapper carries that partial-progress accounting across the abort
    boundary so a consumer can record an accurate count. The triggering
    exception is chained (``raise ... from exc``) and exposed as ``__cause__``
    so the underlying error class is never hidden. The message stays SAFE — no
    secrets, no raw exception text, no source content.
    """

    def __init__(self, *, processed_count: int, failed_count: int = 1) -> None:
        super().__init__(f"Connector run aborted after {processed_count} item(s) ingested")
        self.processed_count = processed_count
        self.failed_count = failed_count


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
        the full listing is never materialized in memory. Filters to the
        supported-format allowlist (PDF + DOCX/PPTX/HTML/HTM/MD/TXT) here and
        SKIPS (does not raise on) unsupported and >100 MB items.
        """
        ...

    def fetch(self, item: SourceItem) -> ConnectorFile:
        """Download the COMPLETE bytes for one item.

        Uses a timeout + bounded retry; size is pre-validated against the 100 MB
        ceiling; content magic bytes are validated post-download per format
        (PDF → %PDF, DOCX/PPTX → zip; text formats are not gated). RAISES
        NotAPdfError / InvalidContentError on a corrupt/mis-typed download — that
        is an error, not a skip.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers shared by connector implementations
# ---------------------------------------------------------------------------


def is_pdf_name(name: str) -> bool:
    """Return True if `name` looks like a PDF by extension (case-insensitive)."""
    return name.lower().endswith(".pdf")


def is_supported_name(name: str) -> bool:
    """Return True if `name`'s extension is an ingestion-supported format.

    Delegates to the loader registry's single source of truth (PDF +
    DOCX/PPTX/HTML/HTM/MD/TXT) so connectors filter on the exact same allowlist
    the /upload route and upload() use — no duplicated extension list.
    """
    return _registry_is_supported_name(name)


def validate_pdf_magic(content: bytes) -> None:
    """Raise NotAPdfError unless `content` starts with the %PDF magic header."""
    if not content[:4] == _PDF_MAGIC:
        raise NotAPdfError("Downloaded content is not a valid PDF (missing %PDF header)")


def validate_content_magic(filename: str, content: bytes) -> None:
    """Post-download integrity gate dispatched by `filename`'s extension.

    - ``.pdf``         → must start with ``%PDF`` (raises NotAPdfError).
    - ``.docx``/``.pptx`` → must start with ``PK`` (zip/OOXML; raises
      InvalidContentError).
    - text formats (HTML/HTM/MD/TXT) → no reliable magic header, so no gate;
      the loader registry surfaces any genuinely-malformed content downstream.

    A mis-typed/corrupt download is an error, not a skip — consistent with the
    PDF-only behavior connectors had before multi-format support.
    """
    ext = get_extension(filename)
    if ext == ".pdf":
        validate_pdf_magic(content)
    elif ext in _OOXML_EXTENSIONS and content[:2] != _ZIP_MAGIC:
        raise InvalidContentError(
            f"Downloaded content is not a valid {ext} container (missing zip/OOXML header)"
        )


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
# Per-(source_path, bot_tag) single-flight lock (H4 / L-Conn1)
# ---------------------------------------------------------------------------

# The driver's delete_by_source_path + upload window is non-atomic; two
# overlapping runs on the same (source_path, bot_tag) MUST be serialized or a
# run A `delete` can interleave run B `upload`, leaving a tenant's chunks
# deleted-but-not-reuploaded.
#
# H4: the previous implementation used a module-level ``asyncio.Lock``. But the
# trigger (`admin/routes.py:_run_connector_background`) drives each run via
# ``asyncio.run(...)`` on a SEPARATE Starlette threadpool THREAD, each with its
# own fresh event loop. ``asyncio.Lock`` is not thread-safe and binds to one
# loop, so it provided NO mutual exclusion across runs (and raises on 3.13 when
# waiters bind to a different loop). We serialize at the layer that actually
# crosses threads/loops: a ``threading.Lock``-keyed registry, mirroring the
# thread-safe pattern in ``run_status.py``. A distributed lock (e.g. a Blob
# lease) remains the documented follow-up if ingestion goes multi-replica.
#
# L-Conn1: the registry is bounded by DROP-WHEN-IDLE refcounting rather than an
# LRU cap. LRU eviction is unsafe for locks — evicting a key whose lock is
# currently held/awaited would let the next run mint a fresh lock and run
# concurrently with the holder, silently re-breaking exclusion. Instead each
# acquirer increments a waiter count under the guard; the last releaser (count
# back to 0) deletes the key. The registry is thus bounded to the number of
# CONCURRENTLY-ACTIVE keys, never growing with the total corpus size.

_locks_guard = threading.Lock()
# key -> [lock, waiter_count]. The waiter_count is guarded by _locks_guard, not
# by the per-key lock, so it stays consistent across acquire/release.
_locks: dict[tuple[str, str], list] = {}


def _acquire_single_flight(source_path: str, bot_tag: str) -> threading.Lock:
    """Register interest in one (source_path, bot_tag) key and return its lock.

    Increments the key's waiter count under the registry guard, creating the
    entry on first interest. The CALLER must then ``lock.acquire()`` (blocking)
    and pair every call with exactly one ``_release_single_flight`` in a
    ``finally`` — even if the acquire is interrupted — so the refcount never
    leaks and the key is dropped once idle.
    """
    key = (source_path, bot_tag)
    with _locks_guard:
        entry = _locks.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _locks[key] = entry
        entry[1] += 1
        return entry[0]


def _release_single_flight(source_path: str, bot_tag: str) -> None:
    """Drop one waiter's interest; delete the key's lock once nobody wants it.

    Decrements the waiter count under the registry guard. When it reaches zero
    the lock is no longer held OR awaited by anyone, so removing it cannot race
    a holder — the next interested run simply recreates a fresh, uncontended
    lock. This is the bound: idle keys leave no residue.
    """
    key = (source_path, bot_tag)
    with _locks_guard:
        entry = _locks.get(key)
        if entry is None:  # pragma: no cover - defensive; release always paired
            return
        entry[1] -= 1
        if entry[1] <= 0:
            del _locks[key]


def _active_lock_keys() -> int:
    """Number of keys currently tracked in the registry (test/inspection hook)."""
    with _locks_guard:
        return len(_locks)


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
        # Register interest in the per-key lock BEFORE acquiring so the registry
        # never drops a key another run is waiting on (L-Conn1 refcounting).
        lock = _acquire_single_flight(item.source_path, connector.bot_tag)
        try:
            # Acquire the cross-thread lock OFF the event loop so this run's
            # loop stays responsive while it waits for a concurrent run on the
            # same key to finish. Holding it across the delete→upload await is
            # the whole point: it serializes that non-atomic window across the
            # separate loops/threads each run executes on (H4).
            await asyncio.to_thread(lock.acquire)
            try:
                # fetch() is inside the try so a download failure is diagnosable
                # (emits connector_item_failed with source_path) instead of
                # aborting the run with no per-item record (L-Conn2).
                cfile = connector.fetch(item)
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
            finally:
                lock.release()
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
            # Carry the partial-progress count across the abort boundary so the
            # driver records an accurate processed_count instead of 0 (L-Conn2).
            # A ConnectorRunError just re-states existing accounting — pass it
            # through; any other item failure is wrapped (cause chained).
            if isinstance(exc, ConnectorRunError):
                raise
            raise ConnectorRunError(processed_count=processed) from exc
        finally:
            _release_single_flight(item.source_path, connector.bot_tag)
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
