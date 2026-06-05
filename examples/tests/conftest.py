"""Shared helpers for the examples test-suite.

The example scripts have digit-prefixed filenames (``01_ask.py``), which are not
importable with a normal ``import`` statement (``import 01_ask`` is a
SyntaxError). So we load each one by path with ``importlib`` and hand tests a
fresh module object they can monkeypatch (e.g. swap ``TocDocClient`` for a fake)
before calling its ``main()``. No network is ever touched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# .../examples/tests/conftest.py -> .../examples
EXAMPLES_DIR = Path(__file__).resolve().parent.parent


def load_example(filename: str) -> ModuleType:
    """Load an example script (by filename) as a standalone module object."""
    path = EXAMPLES_DIR / filename
    # A safe module name derived from the file stem (e.g. "01_ask" -> "ex_01_ask").
    mod_name = "ex_" + path.stem
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses/typing lookups inside the module resolve.
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def example():
    """Fixture returning the :func:`load_example` loader."""
    return load_example
