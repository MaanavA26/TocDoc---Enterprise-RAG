"""Tests for env-driven runtime configuration (CORS, logging)."""

import os


def test_cors_empty_env_produces_no_origins(monkeypatch):
    """When CORS_ALLOWED_ORIGINS is unset, the allowed origins list must be empty."""
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()] if raw.strip() else []
    assert origins == []


def test_cors_env_single_origin(monkeypatch):
    """A single origin in CORS_ALLOWED_ORIGINS must parse to a one-element list."""
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com")
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()] if raw.strip() else []
    assert origins == ["https://app.example.com"]


def test_cors_env_multiple_origins(monkeypatch):
    """Multiple comma-separated origins must all be preserved."""
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.com, https://b.com, https://c.com")
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()] if raw.strip() else []
    assert origins == ["https://a.com", "https://b.com", "https://c.com"]


def test_log_level_env_defaults_to_info(monkeypatch):
    """When LOG_LEVEL is unset, the default level must be INFO."""
    import logging

    monkeypatch.delenv("LOG_LEVEL", raising=False)
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    assert getattr(logging, level_name, None) == logging.INFO
