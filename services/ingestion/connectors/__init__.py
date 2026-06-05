"""Connector ingestion package (P1-3).

A connector is NOT a subsystem — it is a thin enumerator + downloader that feeds
bytes into the one existing ingestion write path, `custom_rag.rag.upload(...)`.
Connectors never hash content, mint chunk IDs, chunk, embed, or write the index
directly, so the P0-4 deterministic-ID and P0-5 chunking guarantees stay
enforced in exactly one place.

Public surface:
- `SourceConnector`  — the connector Protocol (enumerate + fetch).
- `SourceItem`       — opaque remote identity + canonical source_path + validator.
- `ConnectorFile`    — the single hand-off type upload() consumes (.filename + read()).
- `ConnectorConfig`  — source→bot_tag binding, bot_tag validated at init.
- `run_connector`    — the source-agnostic enumerate → fetch → delete → upload driver.
- error types        — typed, P0-6-friendly connector errors.
"""

from .core import (
    BOT_TAG_PATTERN,
    MAX_FILE_BYTES,
    ConnectorConfig,
    ConnectorError,
    ConnectorFile,
    ConnectorRunError,
    InvalidBotTagError,
    NotAPdfError,
    SourceConnector,
    SourceItem,
    is_pdf_name,
    run_connector,
)

__all__ = [
    "BOT_TAG_PATTERN",
    "MAX_FILE_BYTES",
    "ConnectorConfig",
    "ConnectorError",
    "ConnectorFile",
    "ConnectorRunError",
    "InvalidBotTagError",
    "NotAPdfError",
    "SourceConnector",
    "SourceItem",
    "is_pdf_name",
    "run_connector",
]
