"""SharePoint document-library connector (P1-3, ADR PR-4).

source_type = "sharepoint",
source_path = "sharepoint://{site_id}/{drive_id}/{item_id}".

Routes bytes into custom_rag.rag.upload() via the source-agnostic driver — it
never hashes content, mints chunk ids, chunks, embeds, or writes the index, so
P0-4 deterministic IDs and P0-5 chunking stay enforced in one place.

Reaches Microsoft Graph via raw httpx against the REST API rather than pulling
in the heavy msgraph-sdk dependency (httpx and azure-identity are already deps,
so this adds NO new dependency). enumerate()/fetch() are SYNCHRONOUS to match
the driver contract (run_connector calls them synchronously, exactly like the
Blob connector's sync Azure SDK calls).

Auth (per the P0-7 KeyVault env path — connectors call os.getenv only and never
take secrets in a request, in source_path, or in logs):
  - ClientSecretCredential(SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID,
    SHAREPOINT_CLIENT_SECRET) acquiring a token for the Graph
    `https://graph.microsoft.com/.default` scope.
  - Site/drive selection via SHAREPOINT_SITE_ID / SHAREPOINT_DRIVE_ID.
Credentials and the Graph bearer token are NEVER logged and NEVER placed in
source_path; only the opaque site/drive/item IDs are.

Graph throttling (caps ~1000 req/min): a 429 is retried with a backoff that
HONORS the `Retry-After` header (seconds) when present, falling back to bounded
exponential backoff otherwise. The pre-authenticated `@microsoft.graph.downloadUrl`
returned by Graph is credential-bearing — it is used transiently inside fetch()
ONLY and never reaches source_path or a log line.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator

import httpx
from observability import log_event

from .core import (
    MAX_FILE_BYTES,
    ConnectorConfig,
    ConnectorError,
    ConnectorFile,
    SourceItem,
    is_pdf_name,
    validate_pdf_magic,
)

logger = logging.getLogger(__name__)

# Graph REST base. The drive-children listing endpoint and per-item content
# endpoint are derived from the configured site/drive IDs.
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Request/download tuning. Bounded so a slow/hung Graph call cannot stall a run
# indefinitely, and so retries do not hammer the service.
_HTTP_TIMEOUT_SECONDS = 300
_MAX_RETRIES = 5
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 60.0
# Defensive cap on a Retry-After value so a hostile/huge header cannot park a
# run for an absurd duration; honors the header but never beyond the cap.
_RETRY_AFTER_CAP_SECONDS = 120.0

# Graph `$select` keeps the listing payload small: we only need id, name, size,
# the change validators (eTag/cTag), and the `file` facet (to confirm it is a
# file, not a folder). `$top` requests larger pages to cut round-trips.
_LIST_QUERY = "?$select=id,name,size,eTag,cTag,file&$top=200"


class SharePointConnector:
    """Enumerate + fetch PDFs from one SharePoint document library via Graph.

    bot_tag / fr_mode come from ConnectorConfig (bot_tag validated against
    BOT_TAG_PATTERN at init). The site + drive are fixed per connector instance,
    so source→bot_tag binding is 1:1/N:1 and never cross-tag.
    """

    source_type = "sharepoint"
    # Set by run_connector before enumerate(); None when used outside a run.
    # Included as request_id on inner log events so they correlate with the run.
    run_id: str | None = None

    def __init__(
        self,
        config: ConnectorConfig,
        site_id: str,
        drive_id: str,
        *,
        http_client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        """Build a SharePoint connector.

        Args:
            config: validated source→bot_tag binding (bot_tag, fr_mode).
            site_id: opaque Graph site id; part of the source_path anchor.
            drive_id: opaque Graph drive id; part of the source_path anchor.
            http_client: optional pre-built (and pre-authorized) httpx.Client.
                Primarily for tests (inject a mock transport). When None, one is
                built with a Graph bearer token from env-sourced credentials via
                the P0-7 path.
            sleep: injectable sleep used by the retry/Retry-After backoff (tests
                pass a recording no-op to assert a 429 backoff occurred).
        """
        if not site_id or not drive_id:
            raise ConnectorError("SharePoint connector misconfigured: site_id and drive_id are required")
        self.bot_tag = config.bot_tag
        self.fr_mode = config.fr_mode
        self.site_id = site_id
        self.drive_id = drive_id
        self._sleep = sleep
        self._client = http_client or self._build_client()

    # -- auth / client construction -----------------------------------------

    def _build_client(self) -> httpx.Client:
        """Build an httpx.Client carrying a Graph bearer token (P0-7 path).

        Reads canonical env var names via os.getenv — identical to how upload()
        reads DOC_INTELLIGENCE_KEY etc. Validates credential presence at init so
        a misconfigured connector fails fast, before any listing. The acquired
        token is set as the Authorization header on the client and is NEVER
        logged or placed in source_path.
        """
        tenant_id = os.getenv("SHAREPOINT_TENANT_ID")
        client_id = os.getenv("SHAREPOINT_CLIENT_ID")
        client_secret = os.getenv("SHAREPOINT_CLIENT_SECRET")
        if not (tenant_id and client_id and client_secret):
            raise ConnectorError(
                "SharePoint connector misconfigured: set SHAREPOINT_TENANT_ID, "
                "SHAREPOINT_CLIENT_ID and SHAREPOINT_CLIENT_SECRET"
            )

        # Imported lazily so the connector core / tests do not require live
        # azure-identity credentials to import this module.
        from azure.identity import ClientSecretCredential

        credential = ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        token = credential.get_token(_GRAPH_SCOPE).token
        return httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )

    # -- helpers -------------------------------------------------------------

    def _source_path(self, item_id: str) -> str:
        # Opaque IDs only. NEVER a downloadUrl, SAS, or credential-bearing form.
        return f"sharepoint://{self.site_id}/{self.drive_id}/{item_id}"

    def _request_with_backoff(self, method: str, url: str) -> httpx.Response:
        """Issue one Graph request, retrying on 429/5xx with bounded backoff.

        On HTTP 429 the `Retry-After` header (seconds) is honored when present
        (capped); otherwise exponential backoff is used. Transport exceptions
        and 5xx are also retried. Raises ConnectorError after the retry budget
        is exhausted. Response bodies/headers are NEVER logged (may carry tokens).
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.request(method, url)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES - 1:
                    raise ConnectorError(f"Graph request failed after {_MAX_RETRIES} attempts") from exc
                self._sleep(self._exp_backoff(attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _MAX_RETRIES - 1:
                    raise ConnectorError(
                        f"Graph request returned {response.status_code} after {_MAX_RETRIES} attempts"
                    )
                delay = self._retry_delay(response, attempt)
                log_event(
                    logger,
                    "connector_graph_throttled",
                    request_id=self.run_id,
                    level=logging.WARNING,
                    source_type=self.source_type,
                    bot_tag=self.bot_tag,
                    status_code=response.status_code,
                    attempt=attempt + 1,
                    backoff_seconds=delay,
                )
                self._sleep(delay)
                continue

            if response.status_code >= 400:
                # A non-retryable client error (401/403/404 …). Do not echo the
                # body — it can include identifiers/tokens.
                raise ConnectorError(f"Graph request failed with status {response.status_code}")

            return response

        # pragma: no cover - loop always returns or raises above
        raise ConnectorError("Graph request failed") from last_exc

    @staticmethod
    def _exp_backoff(attempt: int) -> float:
        return min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_CAP_SECONDS)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        """Pick the backoff for a 429/5xx: honor Retry-After (seconds) if valid."""
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    seconds = float(retry_after)
                except ValueError:
                    seconds = -1.0
                if seconds >= 0:
                    return min(seconds, _RETRY_AFTER_CAP_SECONDS)
        return self._exp_backoff(attempt)

    # -- SourceConnector protocol -------------------------------------------

    def enumerate(self) -> Iterator[SourceItem]:
        """Lazily yield PDF SourceItems, following @odata.nextLink pagination.

        Graph's drive-children listing is paginated; mishandling nextLink
        silently drops files, so we follow it explicitly until it is absent.
        Each item's size + change validator (eTag/cTag) are read WITHOUT
        downloading bytes. Non-PDF items and items over 100 MB are SKIPPED
        (logged, not yielded, never raised) so they never buffer in memory or
        reach the PDF-only loader. Folders (no `file` facet) are skipped.
        """
        url: str | None = f"{_GRAPH_BASE}/drives/{self.drive_id}/root/children{_LIST_QUERY}"
        while url:
            response = self._request_with_backoff("GET", url)
            payload = response.json()
            for entry in payload.get("value", []):
                # Skip folders / non-file entries — only driveItems with a
                # `file` facet are downloadable content.
                if "file" not in entry:
                    continue
                name = entry.get("name", "")
                if not is_pdf_name(name):
                    logger.debug("Skipping non-PDF SharePoint item: %r", name)
                    continue
                item_id = entry.get("id", "")
                if not item_id:
                    # Defensive: a driveItem without an id would yield a
                    # malformed source_path (sharepoint://site/drive/) and a bad
                    # /items//content fetch. Skip it rather than ingest garbage.
                    logger.debug("Skipping SharePoint item with missing id: name=%r", name)
                    continue
                size = entry.get("size")
                if isinstance(size, int) and size > MAX_FILE_BYTES:
                    log_event(
                        logger,
                        "connector_item_skipped",
                        request_id=self.run_id,
                        source_type=self.source_type,
                        bot_tag=self.bot_tag,
                        source_path=self._source_path(item_id),
                        reason="exceeds_max_file_bytes",
                        size=size,
                    )
                    continue
                yield SourceItem(
                    identity=item_id,
                    source_path=self._source_path(item_id),
                    filename=name.rsplit("/", 1)[-1],
                    size=size if isinstance(size, int) else None,
                    # eTag changes on metadata too; cTag changes on content —
                    # prefer cTag for change detection, fall back to eTag.
                    validator=entry.get("cTag") or entry.get("eTag"),
                )
            # Follow Graph pagination. Absent nextLink => last page.
            url = payload.get("@odata.nextLink")

    def fetch(self, item: SourceItem) -> ConnectorFile:
        """Download the COMPLETE bytes for one item, then validate the PDF magic.

        Downloads via the item-id `/content` endpoint (Graph 302-redirects to a
        transient pre-authorized URL; httpx follows it). Uses the shared
        timeout + 429/Retry-After-honoring backoff. Size is re-validated against
        the 100 MB ceiling (defense in depth — the bulk loop bypasses the
        per-request route guard). PDF magic bytes (%PDF) are validated AFTER
        download so a partial/interrupted read cannot feed a corrupt PDF to
        Document Intelligence — that RAISES NotAPdfError.
        """
        if item.size is not None and item.size > MAX_FILE_BYTES:
            # Should have been skipped at enumerate; guard anyway.
            raise ConnectorError(f"SharePoint item {item.source_path!r} exceeds the 100 MB per-file ceiling")

        url = f"{_GRAPH_BASE}/drives/{self.drive_id}/items/{item.identity}/content"
        response = self._request_with_backoff("GET", url)
        content = response.content

        if len(content) > MAX_FILE_BYTES:
            raise ConnectorError(f"SharePoint item {item.source_path!r} exceeds the 100 MB per-file ceiling")

        # Post-download integrity gate. Raises NotAPdfError if not a real PDF.
        validate_pdf_magic(content)

        return ConnectorFile(filename=item.filename, content=content)
