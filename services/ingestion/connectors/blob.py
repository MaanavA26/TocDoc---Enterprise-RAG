"""Azure Blob Storage connector (P1-3, ADR PR-3).

source_type = "blob", source_path = "blob://{container}/{blob_name}".

Routes bytes into custom_rag.rag.upload() via the source-agnostic driver — it
never hashes content, mints chunk ids, chunks, embeds, or writes the index, so
P0-4 deterministic IDs and P0-5 chunking stay enforced in one place.

Auth (per the P0-7 KeyVault env path — connectors call os.getenv only and never
take secrets in a request, in source_path, or in logs):
  1. Preferred: DefaultAzureCredential (managed identity) against BLOB_ACCOUNT_URL.
  2. Fallback: BLOB_STORAGE_CONNECTION_STRING (account key / connection string).
SAS URLs are deliberately avoided as the primary path: a SAS can expire between
enumerate() and fetch(), causing a 403 mid-ingest.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator

from observability import log_event

from .core import (
    MAX_FILE_BYTES,
    ConnectorConfig,
    ConnectorError,
    ConnectorFile,
    SourceItem,
    is_supported_name,
    validate_content_magic,
)

logger = logging.getLogger(__name__)

# Streaming-download tuning. Bounded so a slow/hung blob read cannot stall a run
# indefinitely, and so retries do not hammer the service.
_DOWNLOAD_TIMEOUT_SECONDS = 300
_MAX_RETRIES = 4
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 30.0


class BlobConnector:
    """Enumerate + fetch PDFs from one Azure Blob Storage container.

    bot_tag / fr_mode come from ConnectorConfig (bot_tag validated against
    BOT_TAG_PATTERN at init). The container is fixed per connector instance.
    """

    source_type = "blob"
    # Set by run_connector before enumerate(); None when used outside a run.
    # Included as request_id on inner log events so they correlate with the run.
    run_id: str | None = None

    def __init__(
        self,
        config: ConnectorConfig,
        container: str,
        *,
        container_client=None,
        sleep=time.sleep,
    ) -> None:
        """Build a Blob connector.

        Args:
            config: validated source→bot_tag binding (bot_tag, fr_mode).
            container: the blob container name; part of the source_path anchor.
            container_client: optional pre-built ContainerClient. Primarily for
                tests (inject a mock). When None, one is built from env-sourced
                credentials via the P0-7 path.
            sleep: injectable sleep used by the retry backoff (tests pass a no-op).
        """
        self.bot_tag = config.bot_tag
        self.fr_mode = config.fr_mode
        self.container = container
        self._sleep = sleep
        self._container_client = container_client or self._build_container_client(container)

    @staticmethod
    def _build_container_client(container: str):
        """Build a ContainerClient from env-sourced credentials (P0-7 path).

        Reads one canonical env var name via os.getenv — identical to how
        upload() reads DOC_INTELLIGENCE_KEY etc. Validates credential presence
        at init so a misconfigured connector fails fast, before any listing.
        Secrets are never logged, never placed in source_path.
        """
        # Imported lazily so the connector core / tests do not require the
        # azure-storage-blob SDK to be installed.
        from azure.storage.blob import ContainerClient

        account_url = os.getenv("BLOB_ACCOUNT_URL")
        connection_string = os.getenv("BLOB_STORAGE_CONNECTION_STRING")

        if account_url:
            # Preferred: managed identity via DefaultAzureCredential.
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
            return ContainerClient(
                account_url=account_url,
                container_name=container,
                credential=credential,
            )
        if connection_string:
            return ContainerClient.from_connection_string(
                conn_str=connection_string,
                container_name=container,
            )
        raise ConnectorError(
            "Blob connector misconfigured: set BLOB_ACCOUNT_URL (managed identity) "
            "or BLOB_STORAGE_CONNECTION_STRING"
        )

    def _source_path(self, blob_name: str) -> str:
        return f"blob://{self.container}/{blob_name}"

    def enumerate(self) -> Iterator[SourceItem]:
        """Lazily yield PDF SourceItems, paginating via continuation tokens.

        Blob listing can time out on 100k+ blob containers, so we walk pages
        explicitly via `.by_page()` (continuation tokens) rather than draining
        the whole listing into memory. Each blob's size + ETag are read WITHOUT
        downloading bytes. Unsupported-format blobs and blobs over 100 MB are
        SKIPPED (logged, not yielded, never raised) so they never buffer in
        memory or reach a loader that cannot parse them.
        """
        pager = self._container_client.list_blobs().by_page()
        for page in pager:
            for blob in page:
                name = _blob_name(blob)
                if not is_supported_name(name):
                    logger.debug("Skipping unsupported-format blob: %r", name)
                    continue
                size = _blob_size(blob)
                if size is not None and size > MAX_FILE_BYTES:
                    log_event(
                        logger,
                        "connector_item_skipped",
                        request_id=self.run_id,
                        source_type=self.source_type,
                        bot_tag=self.bot_tag,
                        source_path=self._source_path(name),
                        reason="exceeds_max_file_bytes",
                        size=size,
                    )
                    continue
                yield SourceItem(
                    identity=name,
                    source_path=self._source_path(name),
                    filename=name.rsplit("/", 1)[-1],
                    size=size,
                    validator=_blob_etag(blob),
                )

    def fetch(self, item: SourceItem) -> ConnectorFile:
        """Download the COMPLETE bytes for one blob, then validate content magic.

        Chunked streaming download with a timeout and bounded exponential
        backoff. The 100 MB ceiling is enforced DURING the download (L-Conn3)
        via a running byte counter over ``chunks()``: a size-less blob (size
        metadata absent) can never buffer an unbounded body into memory before
        the check — the transfer aborts the moment the running total crosses the
        ceiling. Content magic bytes are validated AFTER the full download
        per format (PDF → %PDF, DOCX/PPTX → zip/OOXML) so a partial/interrupted
        read cannot feed a corrupt file downstream — that RAISES NotAPdfError /
        InvalidContentError. Text formats (HTML/HTM/MD/TXT) are not gated.
        """
        if item.size is not None and item.size > MAX_FILE_BYTES:
            # Should have been skipped at enumerate; guard anyway.
            raise ConnectorError(f"Blob {item.source_path!r} exceeds the 100 MB per-file ceiling")

        blob_client = self._container_client.get_blob_client(item.identity)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                downloader = blob_client.download_blob(timeout=_DOWNLOAD_TIMEOUT_SECONDS)
                content = _read_capped(downloader, item.source_path)
                break
            except ConnectorError:
                # An over-ceiling abort is terminal, not a transient transport
                # error — do not retry (it would just re-download and re-abort).
                raise
            except Exception as exc:  # noqa: BLE001 - retry transient transport errors
                last_exc = exc
                if attempt == _MAX_RETRIES - 1:
                    raise ConnectorError(
                        f"Blob download failed for {item.source_path!r} after {_MAX_RETRIES} attempts"
                    ) from exc
                delay = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_CAP_SECONDS)
                logger.warning(
                    "Blob download attempt %d/%d failed for %r (%s); backing off %.1fs",
                    attempt + 1,
                    _MAX_RETRIES,
                    item.source_path,
                    type(exc).__name__,
                    delay,
                )
                self._sleep(delay)
        else:  # pragma: no cover - loop always breaks or raises
            raise ConnectorError(f"Blob download failed for {item.source_path!r}") from last_exc

        # Post-download integrity gate, dispatched by extension. Raises
        # NotAPdfError / InvalidContentError on a corrupt/mis-typed download.
        validate_content_magic(item.filename, content)

        return ConnectorFile(filename=item.filename, content=content)


# ---------------------------------------------------------------------------
# Capped chunked read — enforce the size ceiling DURING download (L-Conn3)
# ---------------------------------------------------------------------------


def _read_capped(downloader, source_path: str) -> bytes:
    """Read a blob downloader chunk-by-chunk, aborting past the 100 MB ceiling.

    The Azure ``StorageStreamDownloader`` exposes ``chunks()`` — an iterator of
    byte ranges that pulls each range from the service lazily. We accumulate
    into a running counter and raise ``ConnectorError`` the moment the total
    crosses MAX_FILE_BYTES, so a size-less blob never buffers an unbounded body
    into memory before the check. Falls back to ``readall()`` for downloader
    shapes without ``chunks()`` (older SDKs / simple test doubles), applying the
    same post-read ceiling so the guard is never silently skipped.
    """
    chunks = getattr(downloader, "chunks", None)
    if callable(chunks):
        buf = bytearray()
        for chunk in chunks():
            buf.extend(chunk)
            if len(buf) > MAX_FILE_BYTES:
                raise ConnectorError(f"Blob {source_path!r} exceeds the 100 MB per-file ceiling")
        return bytes(buf)

    content = downloader.readall()
    if len(content) > MAX_FILE_BYTES:
        raise ConnectorError(f"Blob {source_path!r} exceeds the 100 MB per-file ceiling")
    return content


# ---------------------------------------------------------------------------
# Blob-metadata accessors — tolerate both SDK BlobProperties objects and the
# dict-like shapes used in tests/mocks, without assuming SDK attribute names.
# ---------------------------------------------------------------------------


def _blob_name(blob) -> str:
    if isinstance(blob, dict):
        return blob.get("name", "")
    return getattr(blob, "name", "")


def _blob_size(blob) -> int | None:
    if isinstance(blob, dict):
        return blob.get("size")
    # SDK BlobProperties exposes `.size`.
    return getattr(blob, "size", None)


def _blob_etag(blob) -> str | None:
    if isinstance(blob, dict):
        return blob.get("etag")
    return getattr(blob, "etag", None)
