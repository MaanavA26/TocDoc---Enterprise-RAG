"""Guard tests: the core SDK is standalone (httpx + pydantic only).

These run in BOTH installs (``[dev]`` and ``[dev,langchain]``). They assert that:

1. ``import tocdoc_sdk`` and building/using a client work with NO langchain in
   the import graph — i.e. the core package never imports ``langchain_core``.
2. Importing the optional ``tocdoc_sdk.langchain`` submodule when langchain-core
   is unavailable raises a clear, actionable ``ImportError`` (pointing at the
   extra) rather than an obscure failure.

To make assertion (2) verifiable even in the ``[dev,langchain]`` install (where
langchain-core *is* importable), we simulate its absence with
``sys.modules["langchain_core"] = None`` (which makes ``import langchain_core``
raise ``ImportError``) and evict any cached submodule before re-importing.
"""

from __future__ import annotations

import importlib
import sys


def test_core_sdk_imports_without_langchain_in_graph():
    """`import tocdoc_sdk` must not pull in the optional langchain submodule.

    Importing the core package must not, as a side effect, import
    ``tocdoc_sdk.langchain`` (which is what would drag in ``langchain_core``).
    We force a fresh import of the package with both the optional submodule and
    langchain_core evicted from the module cache: if ``import tocdoc_sdk`` pulled
    the submodule in, it would re-import it and fail in the [dev] env. The
    assertion holds in BOTH installs.
    """
    for name in ("tocdoc_sdk", "tocdoc_sdk.langchain", "langchain_core"):
        sys.modules.pop(name, None)

    import tocdoc_sdk

    # Importing the core package must not have loaded the optional submodule.
    assert "tocdoc_sdk.langchain" not in sys.modules
    assert "TocDocClient" in tocdoc_sdk.__all__

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "ok", "citation": {}})

    with tocdoc_sdk.TocDocClient(
        "https://x.test", transport=httpx.MockTransport(handler), sleep=lambda _s: None
    ) as client:
        result = client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")
    assert result.answer == "ok"


def test_langchain_submodule_raises_clear_error_when_unavailable(monkeypatch):
    """Importing tocdoc_sdk.langchain without langchain-core gives a clear error."""
    # Force `import langchain_core` to fail, and evict any cached copies so the
    # submodule is re-imported fresh against the simulated absence.
    for name in list(sys.modules):
        if name.startswith("langchain_core."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "langchain_core", None)
    monkeypatch.delitem(sys.modules, "tocdoc_sdk.langchain", raising=False)

    try:
        importlib.import_module("tocdoc_sdk.langchain")
    except ImportError as exc:
        assert "tocdoc-sdk[langchain]" in str(exc)
    else:  # pragma: no cover - the import must fail under simulated absence
        raise AssertionError("expected ImportError when langchain_core is unavailable")
