"""Pytest path setup so the helper modules import without an installed package.

The load-test code lives in ``loadtest/`` as flat modules (``helpers``,
``config``) imported by ``locustfile`` the same way Locust imports them (cwd on
``sys.path``). Tests add the parent ``loadtest/`` dir to ``sys.path`` so
``import helpers`` resolves identically here, with no packaging step.
"""

from __future__ import annotations

import os
import sys

_LOADTEST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LOADTEST_DIR not in sys.path:
    sys.path.insert(0, _LOADTEST_DIR)
