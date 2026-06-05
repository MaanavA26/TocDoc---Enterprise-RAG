"""Tests for the ``tocdoc`` command-line interface.

The CLI is exercised by calling ``cli.main([...])`` directly. The underlying
client classes are monkeypatched with in-memory fakes, so there is no network
and no live credentials. Assertions cover: argument parsing, env-variable
fallback for base URL / token, the ApiError -> non-zero exit path, the
missing-credential -> non-zero exit path, and that the token is NEVER printed
to stdout or stderr.
"""

from __future__ import annotations

import httpx
import pytest
from tocdoc_sdk import cli
from tocdoc_sdk.errors import ApiError
from tocdoc_sdk.models import (
    ChunkSample,
    ConnectorRunError,
    ConnectorRunListResponse,
    ConnectorRunStatusResponse,
    ConnectorSyncResponse,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentSummary,
    IndexStatsResponse,
    QnAAnswer,
)

SECRET = "super-secret-token-value"
ADMIN_SECRET = "super-secret-admin-token-value"
BASE_URL = "https://tocdoc.example.test"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeQnAClient:
    """Records constructor + ask() inputs; returns a canned answer or raises."""

    last_instance: _FakeQnAClient | None = None
    raise_error: Exception | None = None

    def __init__(self, base_url, *, token=None, **kwargs):
        self.base_url = base_url
        self.token = token
        self.ask_calls: list[dict] = []
        type(self).last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ask(self, **kwargs):
        self.ask_calls.append(kwargs)
        if type(self).raise_error is not None:
            raise type(self).raise_error
        return QnAAnswer.model_validate(
            {"answer": "Refunds take 30 days.", "citation": {"policy.md": "/docs/policy.md"}}
        )


class _FakeAdminClient:
    """Records constructor inputs; returns canned admin responses."""

    last_instance: _FakeAdminClient | None = None

    def __init__(self, base_url, *, admin_token, **kwargs):
        self.base_url = base_url
        self.admin_token = admin_token
        self.calls: list[tuple] = []
        type(self).last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def list_documents(self, *, bot_tag):
        self.calls.append(("list_documents", bot_tag))
        return DocumentListResponse(
            bot_tag=bot_tag,
            count=1,
            documents=[DocumentSummary(document_id="doc-1", chunk_count=3, source_type="blob")],
        )

    def get_document(self, *, bot_tag, document_id):
        self.calls.append(("get_document", bot_tag, document_id))
        return DocumentDetailResponse(
            bot_tag=bot_tag,
            document_id=document_id,
            chunk_count=2,
            sample_chunks=[ChunkSample(id="c-1", chunk_index=0)],
        )

    def index_stats(self, *, bot_tag):
        self.calls.append(("index_stats", bot_tag))
        return IndexStatsResponse(
            bot_tag=bot_tag,
            document_count=5,
            chunk_count=42,
            source_types={"blob": 5},
            fr_modes={"read": 5},
        )

    def trigger_connector_sync(self, source_type):
        self.calls.append(("trigger_connector_sync", source_type))
        return ConnectorSyncResponse(run_id="run-1", source_type=source_type, status="started")

    def list_connector_runs(self, *, limit=50):
        self.calls.append(("list_connector_runs", limit))
        return ConnectorRunListResponse(
            count=1,
            runs=[
                ConnectorRunStatusResponse(
                    run_id="run-1", status="completed", source_type="blob", bot_tag="acme"
                )
            ],
        )

    def get_connector_run(self, run_id):
        self.calls.append(("get_connector_run", run_id))
        return ConnectorRunStatusResponse(
            run_id=run_id,
            status="failed",
            source_type="blob",
            bot_tag="acme",
            error=ConnectorRunError(error_class="ValueError", safe_message="bad config"),
        )


@pytest.fixture
def fake_qna(monkeypatch):
    _FakeQnAClient.last_instance = None
    _FakeQnAClient.raise_error = None
    monkeypatch.setattr(cli, "TocDocClient", _FakeQnAClient)
    return _FakeQnAClient


@pytest.fixture
def fake_admin(monkeypatch):
    _FakeAdminClient.last_instance = None
    monkeypatch.setattr(cli, "AdminClient", _FakeAdminClient)
    return _FakeAdminClient


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Each test starts with no TocDoc env vars set."""
    for var in (cli.ENV_BASE_URL, cli.ENV_TOKEN, cli.ENV_ADMIN_TOKEN):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


def test_ask_happy_path_parses_args_and_prints_answer(fake_qna, capsys):
    code = cli.main(
        [
            "ask",
            "--base-url",
            BASE_URL,
            "--token",
            SECRET,
            "--bot-tag",
            "acme",
            "--session-id",
            "s-1",
            "--fr-tag",
            "read",
            "What is the refund policy?",
        ]
    )
    assert code == 0
    inst = fake_qna.last_instance
    assert inst.base_url == BASE_URL
    assert inst.ask_calls == [
        {"session_id": "s-1", "bot_tag": "acme", "fr_tag": "read", "query": "What is the refund policy?"}
    ]
    out = capsys.readouterr().out
    assert "Refunds take 30 days." in out
    assert "policy.md: /docs/policy.md" in out


def test_ask_generates_session_id_and_defaults_fr_tag(fake_qna):
    code = cli.main(["ask", "--base-url", BASE_URL, "--token", SECRET, "--bot-tag", "acme", "q?"])
    assert code == 0
    call = fake_qna.last_instance.ask_calls[0]
    assert call["fr_tag"] == "read"
    assert call["session_id"]  # a generated UUID, non-empty


def test_ask_reads_base_url_and_token_from_env(fake_qna, monkeypatch):
    monkeypatch.setenv(cli.ENV_BASE_URL, BASE_URL)
    monkeypatch.setenv(cli.ENV_TOKEN, SECRET)
    code = cli.main(["ask", "--bot-tag", "acme", "q?"])
    assert code == 0
    inst = fake_qna.last_instance
    assert inst.base_url == BASE_URL
    assert inst.token == SECRET


def test_ask_missing_token_returns_nonzero_and_clean_message(fake_qna, capsys):
    code = cli.main(["ask", "--base-url", BASE_URL, "--bot-tag", "acme", "q?"])
    assert code == 1
    err = capsys.readouterr().err
    assert "no token" in err
    assert "Traceback" not in err


def test_ask_missing_base_url_returns_nonzero(fake_qna, capsys):
    code = cli.main(["ask", "--token", SECRET, "--bot-tag", "acme", "q?"])
    assert code == 1
    assert "no base URL" in capsys.readouterr().err


def test_ask_api_error_returns_nonzero_and_clean_message(fake_qna, capsys):
    fake_qna.raise_error = ApiError(
        status_code=401, code="UNAUTHORIZED", message="bad token", request_id="r-1"
    )
    code = cli.main(["ask", "--base-url", BASE_URL, "--token", SECRET, "--bot-tag", "acme", "q?"])
    assert code == 1
    captured = capsys.readouterr()
    assert "UNAUTHORIZED" in captured.err
    assert "401" in captured.err
    assert "Traceback" not in captured.err


def test_ask_transport_error_returns_nonzero_and_clean_message(fake_qna, capsys):
    fake_qna.raise_error = httpx.ConnectError("connection refused")
    code = cli.main(["ask", "--base-url", BASE_URL, "--token", SECRET, "--bot-tag", "acme", "q?"])
    assert code == 1
    err = capsys.readouterr().err
    assert "connection failed" in err
    assert "ConnectError" in err
    assert "Traceback" not in err


def test_ask_missing_required_bot_tag_is_argparse_error(fake_qna):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["ask", "--base-url", BASE_URL, "--token", SECRET, "q?"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Token hygiene — the secret is NEVER printed, on any path.
# ---------------------------------------------------------------------------


def test_token_never_printed_on_success(fake_qna, capsys):
    cli.main(["ask", "--base-url", BASE_URL, "--token", SECRET, "--bot-tag", "acme", "q?"])
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_token_never_printed_on_error(fake_qna, capsys):
    fake_qna.raise_error = ApiError(status_code=500, code="INTERNAL", message="boom")
    cli.main(["ask", "--base-url", BASE_URL, "--token", SECRET, "--bot-tag", "acme", "q?"])
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_admin_token_never_printed(fake_admin, capsys):
    cli.main(["admin", "docs", "--base-url", BASE_URL, "--admin-token", ADMIN_SECRET, "--bot-tag", "acme"])
    captured = capsys.readouterr()
    assert ADMIN_SECRET not in captured.out
    assert ADMIN_SECRET not in captured.err


# ---------------------------------------------------------------------------
# admin subcommands
# ---------------------------------------------------------------------------


def test_admin_docs(fake_admin, capsys):
    code = cli.main(
        ["admin", "docs", "--base-url", BASE_URL, "--admin-token", ADMIN_SECRET, "--bot-tag", "acme"]
    )
    assert code == 0
    assert fake_admin.last_instance.calls == [("list_documents", "acme")]
    assert "doc-1" in capsys.readouterr().out


def test_admin_doc(fake_admin, capsys):
    code = cli.main(
        ["admin", "doc", "doc-9", "--base-url", BASE_URL, "--admin-token", ADMIN_SECRET, "--bot-tag", "acme"]
    )
    assert code == 0
    assert fake_admin.last_instance.calls == [("get_document", "acme", "doc-9")]
    assert "doc-9" in capsys.readouterr().out


def test_admin_index_stats(fake_admin, capsys):
    code = cli.main(
        ["admin", "index-stats", "--base-url", BASE_URL, "--admin-token", ADMIN_SECRET, "--bot-tag", "acme"]
    )
    assert code == 0
    assert fake_admin.last_instance.calls == [("index_stats", "acme")]
    assert "document_count=5" in capsys.readouterr().out


def test_admin_sync(fake_admin, capsys):
    code = cli.main(["admin", "sync", "blob", "--base-url", BASE_URL, "--admin-token", ADMIN_SECRET])
    assert code == 0
    assert fake_admin.last_instance.calls == [("trigger_connector_sync", "blob")]
    assert "run-1" in capsys.readouterr().out


def test_admin_runs_with_limit(fake_admin, capsys):
    code = cli.main(["admin", "runs", "--base-url", BASE_URL, "--admin-token", ADMIN_SECRET, "--limit", "10"])
    assert code == 0
    assert fake_admin.last_instance.calls == [("list_connector_runs", 10)]
    assert "count=1" in capsys.readouterr().out


def test_admin_run_shows_error_summary(fake_admin, capsys):
    code = cli.main(["admin", "run", "run-7", "--base-url", BASE_URL, "--admin-token", ADMIN_SECRET])
    assert code == 0
    assert fake_admin.last_instance.calls == [("get_connector_run", "run-7")]
    out = capsys.readouterr().out
    assert "status=failed" in out
    assert "ValueError: bad config" in out


def test_admin_reads_credentials_from_env(fake_admin, monkeypatch):
    monkeypatch.setenv(cli.ENV_BASE_URL, BASE_URL)
    monkeypatch.setenv(cli.ENV_ADMIN_TOKEN, ADMIN_SECRET)
    code = cli.main(["admin", "docs", "--bot-tag", "acme"])
    assert code == 0
    inst = fake_admin.last_instance
    assert inst.base_url == BASE_URL
    assert inst.admin_token == ADMIN_SECRET


def test_admin_missing_admin_token_returns_nonzero(fake_admin, capsys):
    code = cli.main(["admin", "docs", "--base-url", BASE_URL, "--bot-tag", "acme"])
    assert code == 1
    assert "no admin token" in capsys.readouterr().err


def test_no_subcommand_is_argparse_error():
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
