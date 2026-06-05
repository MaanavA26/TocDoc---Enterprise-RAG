"""Import each example module and exercise its ``main()`` against mocked SDKs.

No live calls: every example's SDK class is monkeypatched to an in-memory fake
(see ``fakes.py``). The tests also assert each example reads its credentials
from the documented env vars (``TOCDOC_BASE_URL`` / ``TOCDOC_TOKEN`` /
``TOCDOC_ADMIN_TOKEN``) by setting those vars and checking the fake client was
constructed with their values.
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeAdminClient, FakeAsyncTocDocClient, FakeTocDocClient

BASE_URL = "https://test.example/qna"
ADMIN_BASE_URL = "https://test.example/upload_pipeline"
TOKEN = "test-bearer-token"
ADMIN_TOKEN = "test-admin-token"


# ---------------------------------------------------------------------------
# 01_ask.py
# ---------------------------------------------------------------------------


def test_01_ask_main_runs_and_reads_env(example, monkeypatch, capsys):
    monkeypatch.setenv("TOCDOC_BASE_URL", BASE_URL)
    monkeypatch.setenv("TOCDOC_TOKEN", TOKEN)
    captured: dict[str, FakeTocDocClient] = {}

    def factory(base_url, **kwargs):
        client = FakeTocDocClient(base_url, **kwargs)
        captured["client"] = client
        return client

    mod = example("01_ask.py")
    monkeypatch.setattr(mod, "TocDocClient", factory)

    rc = mod.main(["What is the refund policy?"])

    assert rc == 0
    # Wiring proof: the env base_url/token reached the SDK constructor.
    assert captured["client"].base_url == BASE_URL
    assert captured["client"].token == TOKEN
    out = capsys.readouterr().out
    assert "Refunds are available within 30 days." in out
    assert "policy.md: /docs/policy.md" in out


def test_01_ask_missing_env_returns_error(example, monkeypatch):
    monkeypatch.delenv("TOCDOC_BASE_URL", raising=False)
    monkeypatch.delenv("TOCDOC_TOKEN", raising=False)
    mod = example("01_ask.py")
    assert mod.main(["q"]) == 1


# ---------------------------------------------------------------------------
# 02_streaming.py
# ---------------------------------------------------------------------------


def test_02_streaming_main_streams_tokens(example, monkeypatch, capsys):
    monkeypatch.setenv("TOCDOC_BASE_URL", BASE_URL)
    monkeypatch.setenv("TOCDOC_TOKEN", TOKEN)
    captured: dict[str, FakeAsyncTocDocClient] = {}

    def factory(base_url, **kwargs):
        client = FakeAsyncTocDocClient(base_url, **kwargs)
        captured["client"] = client
        return client

    mod = example("02_streaming.py")
    monkeypatch.setattr(mod, "AsyncTocDocClient", factory)

    rc = asyncio.run(mod.main(["Summarize the guide."]))

    assert rc == 0
    assert captured["client"].base_url == BASE_URL
    assert captured["client"].token == TOKEN
    out = capsys.readouterr().out
    assert "Refunds are available." in out  # tokens concatenated


def test_02_streaming_missing_env_returns_error(example, monkeypatch):
    monkeypatch.delenv("TOCDOC_BASE_URL", raising=False)
    monkeypatch.delenv("TOCDOC_TOKEN", raising=False)
    mod = example("02_streaming.py")
    assert asyncio.run(mod.main(["q"])) == 1


# ---------------------------------------------------------------------------
# 03_admin.py
# ---------------------------------------------------------------------------


def test_03_admin_main_lists_stats_and_polls(example, monkeypatch, capsys):
    monkeypatch.setenv("TOCDOC_BASE_URL", ADMIN_BASE_URL)
    monkeypatch.setenv("TOCDOC_ADMIN_TOKEN", ADMIN_TOKEN)
    captured: dict[str, FakeAdminClient] = {}

    def factory(base_url, **kwargs):
        client = FakeAdminClient(base_url, **kwargs)
        captured["client"] = client
        return client

    mod = example("03_admin.py")
    monkeypatch.setattr(mod, "AdminClient", factory)
    # Don't actually sleep between poll iterations.
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    rc = mod.main(["acme", "blob"])

    assert rc == 0
    assert captured["client"].base_url == ADMIN_BASE_URL
    assert captured["client"].admin_token == ADMIN_TOKEN
    out = capsys.readouterr().out
    assert "doc-1" in out
    assert "index stats" in out
    assert "started run_id=run-1" in out
    assert "status=completed" in out  # poll reached a terminal state


def test_03_admin_missing_env_returns_error(example, monkeypatch):
    monkeypatch.delenv("TOCDOC_BASE_URL", raising=False)
    monkeypatch.delenv("TOCDOC_ADMIN_TOKEN", raising=False)
    mod = example("03_admin.py")
    assert mod.main([]) == 1


# ---------------------------------------------------------------------------
# 04_langchain_retriever.py
# ---------------------------------------------------------------------------


def test_04_langchain_chain_runs(example, monkeypatch, capsys):
    monkeypatch.setenv("TOCDOC_BASE_URL", BASE_URL)
    monkeypatch.setenv("TOCDOC_TOKEN", TOKEN)
    captured: dict[str, FakeTocDocClient] = {}

    def factory(base_url, **kwargs):
        client = FakeTocDocClient(base_url, **kwargs)
        captured["client"] = client
        return client

    mod = example("04_langchain_retriever.py")
    monkeypatch.setattr(mod, "TocDocClient", factory)

    rc = mod.main(["What is the refund policy?"])

    assert rc == 0
    assert captured["client"].base_url == BASE_URL
    assert captured["client"].token == TOKEN
    out = capsys.readouterr().out
    assert "Refunds are available within 30 days." in out
    assert "/docs/policy.md" in out  # source rendered by format_docs


def test_04_format_docs_handles_empty(example):
    mod = example("04_langchain_retriever.py")
    assert mod.format_docs([]) == "(no documents returned)"


def test_04_missing_env_returns_error(example, monkeypatch):
    monkeypatch.delenv("TOCDOC_BASE_URL", raising=False)
    monkeypatch.delenv("TOCDOC_TOKEN", raising=False)
    mod = example("04_langchain_retriever.py")
    assert mod.main(["q"]) == 1


# ---------------------------------------------------------------------------
# Every example module imports cleanly (no top-level side effects / env reads).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["01_ask.py", "02_streaming.py", "03_admin.py", "04_langchain_retriever.py"],
)
def test_example_imports_without_side_effects(example, filename, monkeypatch):
    # No TOCDOC_* env set: importing must still succeed (work happens in main()).
    monkeypatch.delenv("TOCDOC_BASE_URL", raising=False)
    monkeypatch.delenv("TOCDOC_TOKEN", raising=False)
    monkeypatch.delenv("TOCDOC_ADMIN_TOKEN", raising=False)
    mod = example(filename)
    assert callable(mod.main)
