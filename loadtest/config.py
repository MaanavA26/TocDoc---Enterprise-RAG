"""Runtime configuration for the load-test suite, read lazily from the env.

Every value here is resolved **at call time**, never at import time, so that:

* ``python -c "import locustfile"`` and CI's ``pytest`` import succeed with no
  environment configured (no module-level ``os.environ[...]`` that would raise);
* credentials and the target URL are supplied only when an actual run starts.

Nothing in this module is committed with a default secret or default URL. The
operator exports the variables below before invoking Locust.
"""

from __future__ import annotations

import os

# Environment variable names (documented in README.md). Centralized so the
# locustfile and any tooling reference one source of truth.
ENV_BASE_URL = "TOCDOC_BASE_URL"
ENV_TOKEN = "TOCDOC_TOKEN"  # nosec B105 - this is an env-var NAME, not a secret
ENV_ADMIN_TOKEN = "TOCDOC_ADMIN_TOKEN"  # nosec B105 - env-var NAME, not a secret

# Optional overrides — paths and tags. Defaults follow the task's wire contract
# (POST /qna, POST /upload, GET /admin/*). Operators behind a proxy that mounts
# the services under a different prefix override these.
ENV_QNA_PATH = "TOCDOC_QNA_PATH"
ENV_UPLOAD_PATH = "TOCDOC_UPLOAD_PATH"
ENV_ADMIN_DOCS_PATH = "TOCDOC_ADMIN_DOCS_PATH"
ENV_ADMIN_STATS_PATH = "TOCDOC_ADMIN_STATS_PATH"
ENV_BOT_TAG = "TOCDOC_BOT_TAG"
ENV_FR_TAG = "TOCDOC_FR_TAG"
ENV_UPLOAD_FILEPATH = "TOCDOC_UPLOAD_FILEPATH"
ENV_ENABLE_UPLOAD = "TOCDOC_ENABLE_UPLOAD"

DEFAULT_QNA_PATH = "/qna"
DEFAULT_UPLOAD_PATH = "/upload"
DEFAULT_ADMIN_DOCS_PATH = "/admin/documents"
DEFAULT_ADMIN_STATS_PATH = "/admin/index/stats"
DEFAULT_BOT_TAG = "loadtest"
DEFAULT_FR_TAG = "read"


def get_token() -> str | None:
    """Return the bearer JWT for ``/qna`` from the env, or ``None`` if unset."""
    return os.environ.get(ENV_TOKEN)


def get_admin_token() -> str | None:
    """Return the ``X-Admin-Token`` value from the env, or ``None`` if unset."""
    return os.environ.get(ENV_ADMIN_TOKEN)


def get_qna_path() -> str:
    """Return the ``/qna`` route path (override-able via env)."""
    return os.environ.get(ENV_QNA_PATH, DEFAULT_QNA_PATH)


def get_upload_path() -> str:
    """Return the ``/upload`` route path (override-able via env)."""
    return os.environ.get(ENV_UPLOAD_PATH, DEFAULT_UPLOAD_PATH)


def get_admin_docs_path() -> str:
    """Return the admin documents-list route path."""
    return os.environ.get(ENV_ADMIN_DOCS_PATH, DEFAULT_ADMIN_DOCS_PATH)


def get_admin_stats_path() -> str:
    """Return the admin index-stats route path."""
    return os.environ.get(ENV_ADMIN_STATS_PATH, DEFAULT_ADMIN_STATS_PATH)


def get_bot_tag() -> str:
    """Return the ``bot_tag`` to exercise (operator-provided, neutral default)."""
    return os.environ.get(ENV_BOT_TAG, DEFAULT_BOT_TAG)


def get_fr_tag() -> str:
    """Return the ``fr_tag``/``fr_mode`` to exercise."""
    return os.environ.get(ENV_FR_TAG, DEFAULT_FR_TAG)


def get_upload_filepath() -> str | None:
    """Return the server-side filepath for ``/upload``, or ``None`` if unset.

    Upload load is opt-in: it mutates the target index and needs a path that
    exists on the server, so it is only exercised when this is configured.
    """
    return os.environ.get(ENV_UPLOAD_FILEPATH)


def upload_enabled() -> bool:
    """Whether the (mutating) upload task should run.

    Off unless ``TOCDOC_ENABLE_UPLOAD`` is truthy **and** an upload filepath is
    configured, so a plain smoke/ramp run never writes to the index by accident.
    """
    flag = os.environ.get(ENV_ENABLE_UPLOAD, "").strip().lower()
    return flag in {"1", "true", "yes", "on"} and bool(get_upload_filepath())
