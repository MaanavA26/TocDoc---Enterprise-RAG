"""Tests for the P0-7 env-var migration helper.

Exercises the canonical/legacy dual-read logic in
`services/qna/src/config/config.py`:

- Canonical-only set → resolved, no deprecation warning.
- Legacy-only set → resolved, one-shot deprecation warning emitted.
- Both set → canonical wins, no warning.
- Mix of canonical and legacy → all resolve; warnings only for the legacy ones.
- Key Vault dual-read writes resolved value under the **canonical** env name.

The required-env import-time validation is not re-exercised here — it
runs once at module load and cannot be triggered again without a full
reimport (which would require subprocess gymnastics that exceed P0-7's
scope). The other tests in this suite effectively cover the validation
path by virtue of importing the module successfully under the canonical
env-var setup.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure required canonical env vars exist BEFORE importing the config
# module — the module validates them at import time.
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake.openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-openai-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-02-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake.search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience")

_QNA_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_QNA_ROOT) not in sys.path:
    sys.path.insert(0, str(_QNA_ROOT))

# Import lazily to allow above env_setdefaults to take effect.
from src.config import config as cfg  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_warned_aliases():
    """The deprecation guard is module-level; reset between tests so each
    case can independently verify whether a warning fired."""
    cfg._warned_aliases.clear()
    yield
    cfg._warned_aliases.clear()


# ---------------------------------------------------------------------------
# _get_env resolver
# ---------------------------------------------------------------------------


class TestGetEnvResolver:
    def test_canonical_only_resolves_silently(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog,
    ):
        monkeypatch.setenv("AZURE_OPENAI_KEY", "from-canonical")
        # Legacy name is unset
        monkeypatch.delenv("TocdocOpenAIKey", raising=False)

        caplog.set_level(logging.WARNING, logger="src.config.config")
        value = cfg._get_env("AZURE_OPENAI_KEY")

        assert value == "from-canonical"
        # No deprecation warning emitted on the canonical path
        assert not any("Deprecated" in r.message for r in caplog.records)

    def test_legacy_only_resolves_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog,
    ):
        monkeypatch.delenv("AZURE_OPENAI_KEY", raising=False)
        monkeypatch.setenv("TocdocOpenAIKey", "from-legacy")

        caplog.set_level(logging.WARNING, logger="src.config.config")
        value = cfg._get_env("AZURE_OPENAI_KEY")

        assert value == "from-legacy"
        # Exactly one deprecation warning, naming both names
        warnings = [r.message for r in caplog.records if "Deprecated" in r.message]
        assert len(warnings) == 1
        # The message mentions BOTH the legacy and canonical name so an
        # operator scanning logs sees the rename mapping in one place.
        msg = warnings[0]
        assert "TocdocOpenAIKey" in msg
        assert "AZURE_OPENAI_KEY" in msg

    def test_canonical_wins_when_both_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog,
    ):
        monkeypatch.setenv("AZURE_OPENAI_KEY", "from-canonical")
        monkeypatch.setenv("TocdocOpenAIKey", "from-legacy")

        caplog.set_level(logging.WARNING, logger="src.config.config")
        value = cfg._get_env("AZURE_OPENAI_KEY")

        # Canonical wins, legacy is silently ignored
        assert value == "from-canonical"
        assert not any("Deprecated" in r.message for r in caplog.records)

    def test_neither_set_returns_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog,
    ):
        monkeypatch.delenv("AZURE_OPENAI_KEY", raising=False)
        monkeypatch.delenv("TocdocOpenAIKey", raising=False)

        caplog.set_level(logging.WARNING, logger="src.config.config")
        assert cfg._get_env("AZURE_OPENAI_KEY") is None
        assert cfg._get_env("AZURE_OPENAI_KEY", default="fallback") == "fallback"
        # No deprecation warning when neither name is set — only the
        # legacy-hit path warns.
        assert not any("Deprecated" in r.message for r in caplog.records)

    def test_warning_is_one_shot_per_alias(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog,
    ):
        monkeypatch.delenv("AZURE_OPENAI_KEY", raising=False)
        monkeypatch.setenv("TocdocOpenAIKey", "from-legacy")

        caplog.set_level(logging.WARNING, logger="src.config.config")
        # Call twice — second call should not re-emit the warning
        cfg._get_env("AZURE_OPENAI_KEY")
        cfg._get_env("AZURE_OPENAI_KEY")

        warnings = [r.message for r in caplog.records if "Deprecated" in r.message]
        assert len(warnings) == 1


class TestMixedCanonicalAndLegacy:
    def test_mix_resolves_all_with_warnings_only_for_legacy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog,
    ):
        # Canonical: AZURE_OPENAI_KEY. Legacy: AzureSearchEndpoint.
        monkeypatch.setenv("AZURE_OPENAI_KEY", "canonical-1")
        monkeypatch.delenv("TocdocOpenAIKey", raising=False)
        monkeypatch.delenv("AZURE_SEARCH_ENDPOINT", raising=False)
        monkeypatch.setenv("AzureSearchEndpoint", "legacy-2")

        caplog.set_level(logging.WARNING, logger="src.config.config")

        assert cfg._get_env("AZURE_OPENAI_KEY") == "canonical-1"
        assert cfg._get_env("AZURE_SEARCH_ENDPOINT") == "legacy-2"

        warnings = [r.message for r in caplog.records if "Deprecated" in r.message]
        # Exactly one warning — for the legacy hit only
        assert len(warnings) == 1
        assert "AzureSearchEndpoint" in warnings[0]


# ---------------------------------------------------------------------------
# Key Vault dual-read — legacy KV secret writes to canonical os.environ key
# ---------------------------------------------------------------------------


class TestKeyVaultDualRead:
    """Verify the loader's three-step KV lookup contract:

    1. Try the canonical KV secret name (hyphenated-lowercase).
    2. On ResourceNotFoundError, try the legacy PascalCase secret name.
    3. Write the resolved value into `os.environ[canonical]` (the
       UPPER_SNAKE env name).

    Critically, the canonical KV name is NOT the canonical env var name
    — Key Vault rejects underscores, so `azure-openai-key` is queried,
    not `AZURE_OPENAI_KEY`. If the loader used the env-var name directly,
    KV would return a 400 (not a ResourceNotFoundError), the fallback
    would never trigger, and PascalCase-only vaults would silently fail.
    """

    @pytest.fixture(autouse=True)
    def _clean_envs(self, monkeypatch: pytest.MonkeyPatch):
        """Strip both names so the test starts from a known baseline."""
        for canonical in ("AZURE_OPENAI_KEY", "AZURE_SEARCH_KEY"):
            monkeypatch.delenv(canonical, raising=False)
        for legacy in ("TocdocOpenAIKey", "AzureSearchKey"):
            monkeypatch.delenv(legacy, raising=False)

    def test_keyvault_loader_uses_valid_kv_secret_name_not_env_name(self):
        """The loader must request `azure-openai-key` (valid KV name),
        NOT `AZURE_OPENAI_KEY` (invalid — underscores rejected by KV)."""
        from azure.core.exceptions import ResourceNotFoundError

        secret = MagicMock()
        secret.value = "from-canonical-kv"

        async def fake_get_secret(name: str):
            if name == "azure-openai-key":
                return secret
            raise ResourceNotFoundError(f"not found: {name}")

        mock_client = MagicMock()
        mock_client.get_secret = AsyncMock(side_effect=fake_get_secret)
        mock_client.close = AsyncMock()

        mock_credential = MagicMock()
        mock_credential.close = AsyncMock()

        with patch.object(cfg.Settings, "secret_names", ["AZURE_OPENAI_KEY"]):
            with patch("src.config.config.SecretClient", return_value=mock_client):
                with patch("src.config.config.ClientSecretCredential", return_value=mock_credential):
                    import asyncio

                    results = asyncio.run(cfg.Settings.load_secrets_from_keyvault())

        # Resolved via the hyphenated canonical KV name
        assert results == {"AZURE_OPENAI_KEY": True}
        assert os.environ["AZURE_OPENAI_KEY"] == "from-canonical-kv"

        # CRITICAL: verify the loader called get_secret with the
        # hyphenated form, NOT the env-var form. If this assertion ever
        # starts failing, it means someone "fixed" the loader by feeding
        # it the env-var name directly — which would 400 in production
        # and silently break the legacy fallback for all PascalCase vaults.
        called_names = {c.args[0] for c in mock_client.get_secret.call_args_list}
        assert "azure-openai-key" in called_names, (
            f"loader did not query hyphenated KV name; got {called_names}"
        )
        assert "AZURE_OPENAI_KEY" not in called_names, (
            "loader queried env-var name as KV secret — KV would reject "
            "this with a 400 (underscores disallowed). Use the hyphenated "
            "form from _KV_SECRET_NAMES."
        )

    def test_legacy_kv_fallback_runs_when_canonical_kv_secret_missing(self):
        """When the canonical hyphenated KV secret returns ResourceNotFoundError,
        the loader falls back to the legacy PascalCase KV secret name and
        writes the value under the canonical env name."""
        from azure.core.exceptions import ResourceNotFoundError

        legacy_secret = MagicMock()
        legacy_secret.value = "legacy-kv-value"

        async def fake_get_secret(name: str):
            if name == "azure-openai-key":
                raise ResourceNotFoundError("not found")
            if name == "TocdocOpenAIKey":
                return legacy_secret
            raise ResourceNotFoundError(f"not found: {name}")

        mock_client = MagicMock()
        mock_client.get_secret = AsyncMock(side_effect=fake_get_secret)
        mock_client.close = AsyncMock()

        mock_credential = MagicMock()
        mock_credential.close = AsyncMock()

        with patch.object(cfg.Settings, "secret_names", ["AZURE_OPENAI_KEY"]):
            with patch("src.config.config.SecretClient", return_value=mock_client):
                with patch("src.config.config.ClientSecretCredential", return_value=mock_credential):
                    import asyncio

                    results = asyncio.run(cfg.Settings.load_secrets_from_keyvault())

        assert results == {"AZURE_OPENAI_KEY": True}
        # Value landed under the canonical env name (the load-bearing bit).
        assert os.environ["AZURE_OPENAI_KEY"] == "legacy-kv-value"

        # The loader queried both names — hyphenated first, then legacy.
        called_names = [c.args[0] for c in mock_client.get_secret.call_args_list]
        assert "azure-openai-key" in called_names
        assert "TocdocOpenAIKey" in called_names
        # Order: hyphenated FIRST, legacy SECOND.
        assert called_names.index("azure-openai-key") < called_names.index("TocdocOpenAIKey")

    def test_legacy_only_kv_does_not_query_canonical_env_name_form(self):
        """Sanity check: even when only the legacy PascalCase secret
        exists, the loader never tries `AZURE_OPENAI_KEY` as a KV name."""
        from azure.core.exceptions import ResourceNotFoundError

        async def fake_get_secret(name: str):
            if name == "TocdocOpenAIKey":
                s = MagicMock()
                s.value = "x"
                return s
            raise ResourceNotFoundError(f"not found: {name}")

        mock_client = MagicMock()
        mock_client.get_secret = AsyncMock(side_effect=fake_get_secret)
        mock_client.close = AsyncMock()

        mock_credential = MagicMock()
        mock_credential.close = AsyncMock()

        with patch.object(cfg.Settings, "secret_names", ["AZURE_OPENAI_KEY"]):
            with patch("src.config.config.SecretClient", return_value=mock_client):
                with patch("src.config.config.ClientSecretCredential", return_value=mock_credential):
                    import asyncio

                    asyncio.run(cfg.Settings.load_secrets_from_keyvault())

        called_names = {c.args[0] for c in mock_client.get_secret.call_args_list}
        # AZURE_OPENAI_KEY would 400 in real KV; the loader must never query it.
        assert "AZURE_OPENAI_KEY" not in called_names

    def test_neither_kv_secret_present_records_false(self):
        from azure.core.exceptions import ResourceNotFoundError

        async def always_not_found(name: str):
            raise ResourceNotFoundError(f"not found: {name}")

        mock_client = MagicMock()
        mock_client.get_secret = AsyncMock(side_effect=always_not_found)
        mock_client.close = AsyncMock()

        mock_credential = MagicMock()
        mock_credential.close = AsyncMock()

        with patch.object(cfg.Settings, "secret_names", ["AZURE_OPENAI_KEY"]):
            with patch("src.config.config.SecretClient", return_value=mock_client):
                with patch("src.config.config.ClientSecretCredential", return_value=mock_credential):
                    import asyncio

                    results = asyncio.run(cfg.Settings.load_secrets_from_keyvault())

        assert results == {"AZURE_OPENAI_KEY": False}
        # The loader attempted both lookups before recording False.
        called_names = {c.args[0] for c in mock_client.get_secret.call_args_list}
        assert "azure-openai-key" in called_names
        assert "TocdocOpenAIKey" in called_names


# ---------------------------------------------------------------------------
# Settings + AzureConfig + LocalConfig — sanity-check the public API
# ---------------------------------------------------------------------------


class TestSettingsApiSurface:
    """The internal Python attribute names didn't change in P0-7. Downstream
    code reads `settings.AZURE_TENANT_ID`, `AzureConfig().AZURE_OPENAI_KEY`,
    etc. — those should keep working regardless of which env-var name form
    the operator used."""

    def test_settings_internal_attrs_unchanged(self):
        # Even though the underlying env var name changed, the Python
        # attribute names are stable.
        assert hasattr(cfg.settings, "AZURE_CLIENT_ID")
        assert hasattr(cfg.settings, "AZURE_CLIENT_SECRET")
        assert hasattr(cfg.settings, "AZURE_TENANT_ID")
        assert hasattr(cfg.settings, "AZURE_KEY_VAULT")
        assert hasattr(cfg.settings, "AUDIENCE_ID")

    def test_azure_config_reads_canonical(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Make sure neither name is set first to avoid carryover.
        for n in ("AZURE_OPENAI_VERSION", "AzureOpenaiApiVersion"):
            monkeypatch.delenv(n, raising=False)
        monkeypatch.setenv("AZURE_OPENAI_VERSION", "from-canonical")

        ac = cfg.AzureConfig()
        assert ac.AZURE_OPENAI_API_VERSION == "from-canonical"

    def test_azure_config_falls_back_to_legacy_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog,
    ):
        for n in ("AZURE_OPENAI_VERSION", "AzureOpenaiApiVersion"):
            monkeypatch.delenv(n, raising=False)
        monkeypatch.setenv("AzureOpenaiApiVersion", "from-legacy")

        caplog.set_level(logging.WARNING, logger="src.config.config")
        ac = cfg.AzureConfig()
        assert ac.AZURE_OPENAI_API_VERSION == "from-legacy"
        warnings = [r.message for r in caplog.records if "Deprecated" in r.message]
        assert any("AzureOpenaiApiVersion" in m for m in warnings)

    def test_local_config_llm_model_canonical(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        for n in ("AZURE_OPENAI_LLM_MODEL", "AzureOpenaiLlmModel"):
            monkeypatch.delenv(n, raising=False)
        monkeypatch.setenv("AZURE_OPENAI_LLM_MODEL", "gpt-test-model")

        lc = cfg.LocalConfig()
        assert lc.AZURE_LLM_MODEL == "gpt-test-model"

    def test_local_config_llm_model_default_when_neither_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        for n in ("AZURE_OPENAI_LLM_MODEL", "AzureOpenaiLlmModel"):
            monkeypatch.delenv(n, raising=False)
        lc = cfg.LocalConfig()
        # Falls back to the hardcoded default when neither form is set.
        assert lc.AZURE_LLM_MODEL == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Migration table integrity
# ---------------------------------------------------------------------------


class TestMigrationTable:
    """Guard against accidental drift in the migration mappings."""

    def test_required_canonical_names_have_legacy_aliases(self):
        # Every renamed env var must have an entry in the legacy table.
        renamed_canonicals = {
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_KEY",
            "AZURE_OPENAI_VERSION",
            "AZURE_OPENAI_LLM_MODEL",
            "AZURE_SEARCH_ENDPOINT",
            "AZURE_SEARCH_KEY",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "AZURE_TENANT_ID",
        }
        assert renamed_canonicals.issubset(set(cfg._LEGACY_ENV_ALIASES.keys()))

    def test_already_canonical_vars_have_no_legacy_alias(self):
        # These were already UPPER_SNAKE pre-P0-7 — no legacy alias needed.
        already_canonical = {
            "AZURE_OPENAI_EMBEDDING_MODEL",
            "INDEX_NAME",
            "AZURE_KEY_VAULT",
            "AUDIENCE_ID",
        }
        for name in already_canonical:
            assert name not in cfg._LEGACY_ENV_ALIASES, (
                f"{name} was always canonical; no legacy alias should exist"
            )

    def test_every_secret_name_has_kv_mapping(self):
        """Every entry in Settings.secret_names must have a hyphenated KV
        secret name mapping, because the canonical UPPER_SNAKE env-var name
        is not a valid KV secret name (KV rejects underscores)."""
        for canonical in cfg.Settings.secret_names:
            assert canonical in cfg._KV_SECRET_NAMES, (
                f"{canonical!r} is in Settings.secret_names but has no "
                f"_KV_SECRET_NAMES entry — KV lookup would 400 on the "
                f"underscore-containing env-var name."
            )

    def test_kv_secret_names_are_valid_kv_format(self):
        """KV secret names must match `^[a-zA-Z0-9-]+$`. The hyphenated-
        lowercase form satisfies this; the UPPER_SNAKE env-var form does
        NOT (underscores)."""
        import re

        pattern = re.compile(r"^[a-zA-Z0-9-]+$")
        for canonical, kv_name in cfg._KV_SECRET_NAMES.items():
            assert pattern.match(kv_name), (
                f"_KV_SECRET_NAMES[{canonical!r}] = {kv_name!r} is not a valid Azure Key Vault secret name."
            )
            assert "_" not in kv_name, (
                f"_KV_SECRET_NAMES[{canonical!r}] contains an underscore — "
                f"KV rejects underscores in secret names."
            )

    def test_legacy_kv_aliases_are_valid_kv_format(self):
        """Legacy PascalCase secret names must also be valid KV names
        (they always have been — KV permits alphanumerics)."""
        import re

        pattern = re.compile(r"^[a-zA-Z0-9-]+$")
        for canonical, legacy in cfg._LEGACY_ENV_ALIASES.items():
            assert pattern.match(legacy), (
                f"_LEGACY_ENV_ALIASES[{canonical!r}] = {legacy!r} is not a "
                f"valid Azure Key Vault secret name (loader would 400 in "
                f"the fallback path)."
            )
