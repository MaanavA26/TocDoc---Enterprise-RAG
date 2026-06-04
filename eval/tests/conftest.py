"""Hermetic test setup for the RAGAS eval harness.

The QnA service config validates required Azure env vars at IMPORT time
(``services/qna/src/config/config.py``), so they must exist before the harness
module is imported — otherwise collection fails. Set fake values here (the same
set the QnA service tests use). No live Azure or RAGAS LLM call happens in
these tests; everything is mocked.
"""

import os

# Fake Azure env so importing the QnA pipeline/config (and thus the harness)
# does not raise at import time. Mirrors services/qna/test fixtures.
_FAKE_ENV = {
    "AZURE_OPENAI_ENDPOINT": "https://fake-openai.example.com",
    "AZURE_OPENAI_KEY": "fake-key",
    "AZURE_OPENAI_VERSION": "2024-06-01",
    "AZURE_OPENAI_EMBEDDING_MODEL": "text-embedding-3-small",
    "AZURE_OPENAI_LLM_MODEL": "gpt-4o-mini",
    "AZURE_SEARCH_ENDPOINT": "https://fake-search.example.com",
    "AZURE_SEARCH_KEY": "fake-search-key",
    "INDEX_NAME": "fake-index",
    "AZURE_KEY_VAULT": "fakevault",
    "AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "AZURE_CLIENT_SECRET": "fake-secret",
    "AUDIENCE_ID": "api://fake-audience-id",
}

for _k, _v in _FAKE_ENV.items():
    os.environ.setdefault(_k, _v)
