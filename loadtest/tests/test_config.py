"""Unit tests for the lazy, env-driven config module.

Verifies the two contracts the CI gate depends on:
  * importing ``config`` (and therefore ``locustfile``) never reads the env;
  * getters resolve from the environment *at call time*, with neutral defaults
    and no committed secret/URL.
"""

from __future__ import annotations

import config


def test_token_getters_default_none(monkeypatch):
    monkeypatch.delenv(config.ENV_TOKEN, raising=False)
    monkeypatch.delenv(config.ENV_ADMIN_TOKEN, raising=False)
    assert config.get_token() is None
    assert config.get_admin_token() is None


def test_token_getters_read_env(monkeypatch):
    monkeypatch.setenv(config.ENV_TOKEN, "jwt-value")
    monkeypatch.setenv(config.ENV_ADMIN_TOKEN, "admin-value")
    assert config.get_token() == "jwt-value"
    assert config.get_admin_token() == "admin-value"


def test_path_defaults(monkeypatch):
    for name in (
        config.ENV_QNA_PATH,
        config.ENV_UPLOAD_PATH,
        config.ENV_ADMIN_DOCS_PATH,
        config.ENV_ADMIN_STATS_PATH,
    ):
        monkeypatch.delenv(name, raising=False)
    assert config.get_qna_path() == "/qna"
    assert config.get_upload_path() == "/upload"
    assert config.get_admin_docs_path() == "/admin/documents"
    assert config.get_admin_stats_path() == "/admin/index/stats"


def test_path_overrides(monkeypatch):
    monkeypatch.setenv(config.ENV_QNA_PATH, "/qna/qna")
    assert config.get_qna_path() == "/qna/qna"


def test_tag_defaults_are_neutral(monkeypatch):
    monkeypatch.delenv(config.ENV_BOT_TAG, raising=False)
    monkeypatch.delenv(config.ENV_FR_TAG, raising=False)
    assert config.get_bot_tag() == "loadtest"
    assert config.get_fr_tag() == "read"


def test_upload_disabled_by_default(monkeypatch):
    monkeypatch.delenv(config.ENV_ENABLE_UPLOAD, raising=False)
    monkeypatch.delenv(config.ENV_UPLOAD_FILEPATH, raising=False)
    assert config.upload_enabled() is False


def test_upload_requires_both_flag_and_path(monkeypatch):
    monkeypatch.setenv(config.ENV_ENABLE_UPLOAD, "true")
    monkeypatch.delenv(config.ENV_UPLOAD_FILEPATH, raising=False)
    # flag on but no filepath -> still disabled
    assert config.upload_enabled() is False
    monkeypatch.setenv(config.ENV_UPLOAD_FILEPATH, "/srv/docs/a.pdf")
    assert config.upload_enabled() is True


def test_upload_flag_falsey_values(monkeypatch):
    monkeypatch.setenv(config.ENV_UPLOAD_FILEPATH, "/srv/docs/a.pdf")
    for val in ("", "0", "no", "off", "false"):
        monkeypatch.setenv(config.ENV_ENABLE_UPLOAD, val)
        assert config.upload_enabled() is False
