"""Tests for config loading and the concrete-tenant startup guardrail."""

from __future__ import annotations

import pytest
from teams_bot.config import (
    ConfigError,
    assert_concrete_tenant,
    load_config,
    parse_tenant_bot_tag_map,
)

GUID = "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize("placeholder", ["common", "organizations", "consumers", "COMMON"])
def test_placeholder_tenant_rejected(placeholder):
    with pytest.raises(ConfigError):
        assert_concrete_tenant(placeholder)


@pytest.mark.parametrize("bad", ["", "not-a-guid", "1234"])
def test_non_guid_tenant_rejected(bad):
    with pytest.raises(ConfigError):
        assert_concrete_tenant(bad)


def test_concrete_guid_accepted():
    assert_concrete_tenant(GUID)  # no raise


def test_parse_map_valid():
    assert parse_tenant_bot_tag_map('{"t1": "bot_a"}') == {"t1": "bot_a"}


def test_parse_map_empty_is_empty_dict():
    assert parse_tenant_bot_tag_map(None) == {}
    assert parse_tenant_bot_tag_map("") == {}


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", '{"k": 5}'])
def test_parse_map_invalid_raises(bad):
    with pytest.raises(ConfigError):
        parse_tenant_bot_tag_map(bad)


def test_load_config_happy_path():
    env = {
        "AZURE_TENANT_ID": GUID,
        "AUDIENCE_ID": "api-app-id",
        "QNA_BASE_URL": "https://qna.internal",
        "TEAMS_FR_TAG": "read",
        "TENANT_BOT_TAG_MAP": f'{{"{GUID}": "client_a_hr"}}',
    }
    config = load_config(env)
    assert config.azure_tenant_id == GUID
    assert config.tenant_bot_tag_map == {GUID: "client_a_hr"}
    assert config.fr_tag == "read"


def test_load_config_rejects_placeholder_tenant():
    env = {
        "AZURE_TENANT_ID": "common",
        "AUDIENCE_ID": "api-app-id",
        "QNA_BASE_URL": "https://qna.internal",
    }
    with pytest.raises(ConfigError):
        load_config(env)


def test_load_config_missing_required_raises():
    with pytest.raises(ConfigError):
        load_config({"AZURE_TENANT_ID": GUID})
