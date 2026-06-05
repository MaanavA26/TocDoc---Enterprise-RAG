"""Path-containment guard for the `/upload` endpoint (CodeQL `py/path-injection`).

The `/upload` route accepts a caller-supplied `filepath` and feeds it into
`os.path.isdir`, `os.walk`, and `open`. Without a containment check this is a
path-traversal / arbitrary-read primitive (CodeQL alerts 5–7). This module
resolves the candidate path with `os.path.realpath` (symlink-safe) and verifies
it stays inside a configured allowed root before any path operation runs.

Kept as a standalone module — like `middleware.py` — so it can be imported and
unit-tested without dragging in `custom_rag`'s heavy deps (PyMuPDF, langchain)
via `app.py`.

Config:
- `INGESTION_ALLOWED_UPLOAD_ROOT` — base directory under which `filepath` must
  resolve. Defaults to the service working directory (the Dockerfile `WORKDIR`,
  `/app`) so existing in-root behavior is preserved. Read per-call (not at
  import) so it can be pointed at a test directory.
"""

from __future__ import annotations

import os

from errors import ApiErrorCode, raise_api_error

ALLOWED_UPLOAD_ROOT_ENV = "INGESTION_ALLOWED_UPLOAD_ROOT"


def _allowed_root() -> str:
    """Resolve the configured allowed upload root (realpath, symlink-safe).

    Read per-call so tests can override the env var at runtime. Defaults to the
    process working directory, which in the container is the Dockerfile WORKDIR.
    """
    root = os.getenv(ALLOWED_UPLOAD_ROOT_ENV) or os.getcwd()
    return os.path.realpath(root)


def resolve_upload_path(filepath: str) -> str:
    """Resolve `filepath` and confirm it is contained within the allowed root.

    Returns the realpath-resolved, validated path. Callers MUST use the
    returned value for all subsequent path operations (`os.path.isdir`,
    `os.walk`, `open`) — passing the validated path (not the raw input) is what
    breaks the taint flow.

    Raises an enveloped `INVALID_REQUEST` (400) via `raise_api_error` when the
    candidate is empty or resolves outside the allowed root (absolute paths
    outside root, `..` traversal, or symlinks escaping the root). No raw input
    or exception text is echoed back to the caller.
    """
    if not filepath or not filepath.strip():
        raise_api_error(
            ApiErrorCode.INVALID_REQUEST,
            "filepath must be a non-empty path within the allowed upload root.",
            400,
        )

    root = _allowed_root()
    candidate = os.path.realpath(os.path.join(root, filepath))

    # Symlink-safe containment: the resolved candidate must be the root itself
    # or sit strictly beneath it. `commonpath` avoids the `/app` vs `/app-evil`
    # prefix bug that a bare `startswith` would introduce.
    try:
        contained = os.path.commonpath([root, candidate]) == root
    except ValueError:
        # Raised when paths are on different drives (Windows) or mix
        # absolute/relative — treat as not contained.
        contained = False

    if not contained:
        raise_api_error(
            ApiErrorCode.INVALID_REQUEST,
            "filepath resolves outside the allowed upload root.",
            400,
        )

    return candidate
