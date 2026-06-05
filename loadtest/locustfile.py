"""Locust load / performance harness for the TocDoc RAG product.

Exercises the latency-sensitive ``POST /qna`` endpoint and the read-only admin
endpoints (and, opt-in, the mutating ``POST /upload``) against a **deployed**
instance. The harness is entirely config-driven: the base URL and credentials
come from the environment (see :mod:`config`), so nothing here ever points at a
real host or carries a secret.

Run (headless smoke)::

    export TOCDOC_BASE_URL="https://<your-deployment>"
    export TOCDOC_TOKEN="<jwt>"
    export TOCDOC_ADMIN_TOKEN="<admin-token>"
    locust -f loadtest/locustfile.py --headless -u 5 -r 1 -t 1m \
        --host "$TOCDOC_BASE_URL"

Importing this module performs **no** environment reads and **no** network I/O,
so ``python -c "import locustfile"`` is always safe (CI relies on that).
"""

from __future__ import annotations

import random

import config
import helpers
from locust import HttpUser, between, task

# A small pool of neutral questions so each virtual user issues varied queries
# rather than hammering one cached path. Intentionally generic — no client data.
SAMPLE_QUESTIONS = [
    "What is the document retention policy?",
    "Summarize the onboarding process.",
    "Which approvals are required for a new vendor?",
    "What are the key steps in the incident response runbook?",
    "How do I request access to a restricted workspace?",
    "What is the escalation path for a production outage?",
    "List the prerequisites for the quarterly audit.",
    "Where is the data-classification guideline defined?",
]


class _BaseTocDocUser(HttpUser):
    """Shared setup for all virtual-user classes.

    Credentials and target paths are resolved in :meth:`on_start` (per simulated
    user, at run time) rather than at import time, keeping the module importable
    without any environment configured.
    """

    abstract = True

    def on_start(self) -> None:
        # Resolve config lazily, once per simulated user.
        self.bot_tag = config.get_bot_tag()
        self.fr_tag = config.get_fr_tag()
        self._token = config.get_token()
        self._admin_token = config.get_admin_token()
        self.session_id = f"loadtest-{random.randint(0, 10_000_000)}"  # nosec B311


class QnAUser(_BaseTocDocUser):
    """Simulates an end user asking grounded questions via ``POST /qna``.

    This is the primary, latency-sensitive path, so it carries the highest task
    weight and a human-like think-time between requests.
    """

    weight = 8
    # Human reading/typing think-time between questions.
    wait_time = between(2, 8)

    @task
    def ask_question(self) -> None:
        question = random.choice(SAMPLE_QUESTIONS)  # nosec B311
        payload = helpers.build_qna_payload(
            question,
            self.bot_tag,
            self.fr_tag,
            session_id=self.session_id,
        )
        headers = {
            "Content-Type": "application/json",
            **helpers.bearer_header(self._token),
        }
        with self.client.post(
            config.get_qna_path(),
            json=payload,
            headers=headers,
            name="POST /qna",
            catch_response=True,
        ) as resp:
            ok, reason = helpers.validate_qna_response(resp)
            if ok:
                resp.success()
            else:
                resp.failure(reason)


class AdminUser(_BaseTocDocUser):
    """Simulates an operator hitting the read-only admin/management endpoints.

    Lighter weight than QnA — admin traffic is far less frequent than user Q&A —
    and a longer think-time reflects interactive dashboard usage.
    """

    weight = 2
    wait_time = between(5, 15)

    @task(3)
    def list_documents(self) -> None:
        with self.client.get(
            config.get_admin_docs_path(),
            params=helpers.build_admin_params(self.bot_tag),
            headers=helpers.admin_header(self._admin_token),
            name="GET /admin/documents",
            catch_response=True,
        ) as resp:
            ok, reason = helpers.validate_admin_list_response(resp)
            if ok:
                resp.success()
            else:
                resp.failure(reason)

    @task(1)
    def index_stats(self) -> None:
        with self.client.get(
            config.get_admin_stats_path(),
            params=helpers.build_admin_params(self.bot_tag),
            headers=helpers.admin_header(self._admin_token),
            name="GET /admin/index/stats",
            catch_response=True,
        ) as resp:
            ok, reason = helpers.validate_admin_list_response(resp)
            if ok:
                resp.success()
            else:
                resp.failure(reason)


class UploadUser(_BaseTocDocUser):
    """Simulates ingestion load via ``POST /upload`` (opt-in, mutating).

    Disabled unless ``TOCDOC_ENABLE_UPLOAD`` is truthy and ``TOCDOC_UPLOAD_FILEPATH``
    is set — uploads write to the target index, so they must never run by
    accident during a smoke/ramp pass. When disabled the task no-ops.
    """

    weight = 1
    wait_time = between(10, 30)

    @task
    def upload(self) -> None:
        if not config.upload_enabled():
            return
        filepath = config.get_upload_filepath()
        params = helpers.build_upload_params(self.bot_tag, filepath, self.fr_tag)
        with self.client.post(
            config.get_upload_path(),
            params=params,
            headers=helpers.admin_header(self._admin_token),
            name="POST /upload",
            catch_response=True,
        ) as resp:
            ok, reason = helpers.validate_accepted_response(resp)
            if ok:
                resp.success()
            else:
                resp.failure(reason)
