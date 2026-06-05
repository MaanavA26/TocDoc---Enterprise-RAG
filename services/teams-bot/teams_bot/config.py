"""Adapter configuration and startup guardrails.

Config is read from environment variables (UPPER_SNAKE, matching the QnA
service's P0-7 convention). No secrets are defaulted or logged here.

Env vars
--------
- ``AZURE_TENANT_ID``     — the client's concrete tenant GUID. Asserted at
  startup to never be a placeholder (``common``/``organizations``); the entire
  cross-tenant guarantee rests on the QnA issuer pin being a real tenant.
- ``AUDIENCE_ID``         — the QnA API app registration id (OBO token audience).
- ``QNA_BASE_URL``        — base URL of the (network-private) QnA service.
- ``TEAMS_FR_TAG``        — default ``fr_tag`` for QnA requests (NOT user-supplied).
- ``TENANT_BOT_TAG_MAP``  — JSON object ``{tenant_id: bot_tag}`` (admin-configured;
  the unspoofable server-side mapping).
- ``MICROSOFT_APP_ID`` / ``MICROSOFT_APP_PASSWORD`` — bot app registration creds
  (consumed by the Bot Framework adapter; never logged).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

# A concrete Azure AD tenant id is a GUID. Multi-tenant placeholders break the
# QnA issuer pin and reopen cross-tenant replay — reject them at startup.
_GUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_FORBIDDEN_TENANTS = frozenset({"common", "organizations", "consumers"})


class ConfigError(Exception):
    """Raised when adapter configuration is missing or unsafe."""


@dataclass(frozen=True)
class AdapterConfig:
    """Validated adapter configuration."""

    azure_tenant_id: str
    audience_id: str
    qna_base_url: str
    fr_tag: str
    tenant_bot_tag_map: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        assert_concrete_tenant(self.azure_tenant_id)


def assert_concrete_tenant(tenant_id: str) -> None:
    """Fail closed unless ``tenant_id`` is a concrete GUID (never a placeholder).

    Raises:
        ConfigError: if ``tenant_id`` is empty, a known multi-tenant placeholder,
            or not GUID-shaped.
    """
    value = (tenant_id or "").strip()
    if value.lower() in _FORBIDDEN_TENANTS:
        raise ConfigError(
            "AZURE_TENANT_ID must be a concrete tenant GUID, never a multi-tenant "
            "placeholder ('common'/'organizations'/'consumers'); the cross-tenant "
            "isolation guarantee depends on a real tenant in the issuer pin."
        )
    if not _GUID_PATTERN.match(value):
        raise ConfigError("AZURE_TENANT_ID must be a concrete tenant GUID.")


def parse_tenant_bot_tag_map(raw: str | None) -> dict[str, str]:
    """Parse the ``TENANT_BOT_TAG_MAP`` JSON object into a ``{str: str}`` dict.

    An empty/absent value yields an empty map (fail-closed: every turn from an
    unmapped tenant is then rejected). A non-object or non-string values raise.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("TENANT_BOT_TAG_MAP must be valid JSON.") from exc
    if not isinstance(data, dict):
        raise ConfigError("TENANT_BOT_TAG_MAP must be a JSON object {tenant_id: bot_tag}.")
    result: dict[str, str] = {}
    for key, val in data.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise ConfigError("TENANT_BOT_TAG_MAP keys and values must be strings.")
        result[key] = val
    return result


def load_config(env: dict[str, str] | None = None) -> AdapterConfig:
    """Build an :class:`AdapterConfig` from the environment.

    Args:
        env: Optional mapping to read from (defaults to ``os.environ``);
            injectable so tests don't mutate the process environment.

    Raises:
        ConfigError: on missing required values or an unsafe tenant.
    """
    src = os.environ if env is None else env

    def required(name: str) -> str:
        value = src.get(name, "").strip()
        if not value:
            raise ConfigError(f"Required env var {name} is missing or empty.")
        return value

    return AdapterConfig(
        azure_tenant_id=required("AZURE_TENANT_ID"),
        audience_id=required("AUDIENCE_ID"),
        qna_base_url=required("QNA_BASE_URL"),
        fr_tag=src.get("TEAMS_FR_TAG", "read").strip() or "read",
        tenant_bot_tag_map=parse_tenant_bot_tag_map(src.get("TENANT_BOT_TAG_MAP")),
    )
