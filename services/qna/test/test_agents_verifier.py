"""Tests for P3 — the self-critique / verifier node (default-OFF).

Covers:
- Default-OFF inertness: with QNA_AGENT_VERIFY off the node is a byte-identical
  pass-through no-op (writes {}), even with chunks + answer present. This is
  the merge gate.
- No-op when there is nothing to grade (no retrieved_chunks — the standard
  route in v1 — or no answer).
- Accept: a grounded answer (supported + score >= bar) is marked verified with
  no refine call.
- Reject → refine → accept: a failing grade triggers exactly ONE refine pass;
  when the refine clears the bar the answer/citations are replaced.
- Reject → refine → still failing: the ORIGINAL answer is kept (non-destructive)
  and verified=False with the unsupported claims surfaced.
- Best-effort: a grader exception never fails the request — the answer is left
  unchanged and no keys are written.
- Exactly one refine (bounded): the refine generation is called at most once.

Env is set BEFORE importing app/config (validated at import time).
"""

import os

import pytest

os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake-openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-06-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake-search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience-id")


class _FakeAzure:
    """Sentinel azure holder; the LLM helpers are monkeypatched."""


_CHUNKS = [
    {"id": "0", "content": "Project X is owned by Vendor A.", "filename": "f0.md", "filepath": "/d/f0.md"},
    {"id": "1", "content": "Vendor A scored 92 on compliance.", "filename": "f1.md", "filepath": "/d/f1.md"},
]


def _state(**over):
    base = {
        "query": "who owns project x and their score",
        "fr_mode": "read",
        "bot_tag": "tenant-a",
        "azure": _FakeAzure(),
        "request_id": "r1",
        "route": "react",
        "retrieved_chunks": list(_CHUNKS),
        "final_answer": "Vendor A owns Project X and scored 92.",
    }
    base.update(over)
    return base


# ===========================================================================
# Default-OFF inertness (the merge gate)
# ===========================================================================
@pytest.mark.asyncio
async def test_verifier_noop_when_flag_off(monkeypatch):
    """QNA_AGENT_VERIFY off → byte-identical pass-through no-op, even with a
    full answer + chunks present. No grader is ever called."""
    from src.agents import verifier as v

    monkeypatch.delenv("QNA_AGENT_VERIFY", raising=False)

    async def boom_grade(*a, **k):
        raise AssertionError("grader must not run with the flag OFF")

    monkeypatch.setattr(v, "_grade", boom_grade, raising=True)

    out = await v.verifier(_state())
    assert out == {}


@pytest.mark.asyncio
async def test_verifier_noop_preserved_for_scaffold_state(monkeypatch):
    """The original pass-through contract (writes {}) holds for a minimal
    standard-route-style state with the flag off."""
    from src.agents.verifier import verifier

    monkeypatch.delenv("QNA_AGENT_VERIFY", raising=False)
    out = await verifier({"request_id": "r1", "route": "standard", "final_answer": "x"})
    assert out == {}


# ===========================================================================
# No-op when nothing to grade (flag ON)
# ===========================================================================
@pytest.mark.asyncio
async def test_verifier_noop_without_chunks(monkeypatch):
    """Flag ON but no retrieved_chunks (standard route in v1) → no-op."""
    from src.agents import verifier as v

    monkeypatch.setenv("QNA_AGENT_VERIFY", "true")

    async def boom_grade(*a, **k):
        raise AssertionError("grader must not run without chunks")

    monkeypatch.setattr(v, "_grade", boom_grade, raising=True)

    out = await v.verifier(_state(retrieved_chunks=[], final_answer="some answer"))
    assert out == {}


@pytest.mark.asyncio
async def test_verifier_noop_without_answer(monkeypatch):
    """Flag ON, chunks present but empty answer → no-op."""
    from src.agents import verifier as v

    monkeypatch.setenv("QNA_AGENT_VERIFY", "true")
    out = await v.verifier(_state(final_answer="   "))
    assert out == {}


# ===========================================================================
# Accept (no refine)
# ===========================================================================
@pytest.mark.asyncio
async def test_verifier_accepts_grounded_answer(monkeypatch):
    """A grounded answer (supported + score >= bar) is verified with NO refine."""
    from src.agents import verifier as v

    monkeypatch.setenv("QNA_AGENT_VERIFY", "true")
    monkeypatch.setattr(v.localconfig, "VERIFY_MIN_SCORE", 70, raising=False)

    async def fake_grade(azure, *, query, answer, chunks):
        return {"supported": True, "score": 95, "unsupported_claims": []}

    monkeypatch.setattr(v, "_grade", fake_grade, raising=True)

    refine_calls = {"n": 0}

    async def fake_generate(**kwargs):
        refine_calls["n"] += 1
        return "refined\n**Sources:** None"

    monkeypatch.setattr(v, "generate_openai_response", fake_generate, raising=True)

    out = await v.verifier(_state())
    assert out == {"verified": True, "unsupported_claims": []}
    assert refine_calls["n"] == 0  # accepted → no refine


# ===========================================================================
# Reject → refine → accept
# ===========================================================================
@pytest.mark.asyncio
async def test_verifier_refines_and_accepts(monkeypatch):
    """A failing grade triggers exactly ONE refine; when the refine clears the
    bar the answer + citations are replaced."""
    from src.agents import verifier as v

    monkeypatch.setenv("QNA_AGENT_VERIFY", "true")
    monkeypatch.setattr(v.localconfig, "VERIFY_MIN_SCORE", 70, raising=False)

    grades = iter(
        [
            {"supported": False, "score": 40, "unsupported_claims": ["fabricated score"]},
            {"supported": True, "score": 90, "unsupported_claims": []},
        ]
    )

    async def fake_grade(azure, *, query, answer, chunks):
        return next(grades)

    monkeypatch.setattr(v, "_grade", fake_grade, raising=True)

    refine_calls = {"n": 0}

    async def fake_generate(**kwargs):
        refine_calls["n"] += 1
        return "Vendor A owns Project X.\n**Sources:** [f0.md]"

    monkeypatch.setattr(v, "generate_openai_response", fake_generate, raising=True)

    out = await v.verifier(_state())

    assert refine_calls["n"] == 1  # exactly one bounded refine
    assert out["verified"] is True
    assert out["unsupported_claims"] == []
    assert out["final_answer"] == "Vendor A owns Project X."
    # Citations recomputed against the chunk file_map via tolerant matching.
    assert out["citations"] == {"f0.md": "/d/f0.md"}


# ===========================================================================
# Reject → refine → still failing (keep original, non-destructive)
# ===========================================================================
@pytest.mark.asyncio
async def test_verifier_keeps_original_when_refine_still_fails(monkeypatch):
    """If both the original and the single refine fail the bar, the ORIGINAL
    answer is kept (non-destructive) and verified=False with claims surfaced."""
    from src.agents import verifier as v

    monkeypatch.setenv("QNA_AGENT_VERIFY", "true")

    async def fake_grade(azure, *, query, answer, chunks):
        return {"supported": False, "score": 30, "unsupported_claims": ["claim z"]}

    monkeypatch.setattr(v, "_grade", fake_grade, raising=True)

    refine_calls = {"n": 0}

    async def fake_generate(**kwargs):
        refine_calls["n"] += 1
        return "still ungrounded\n**Sources:** None"

    monkeypatch.setattr(v, "generate_openai_response", fake_generate, raising=True)

    out = await v.verifier(_state())

    assert refine_calls["n"] == 1  # exactly one refine, bounded
    assert out["verified"] is False
    assert out["unsupported_claims"] == ["claim z"]
    # The original answer/citations are NOT overwritten (no such keys returned).
    assert "final_answer" not in out
    assert "citations" not in out


# ===========================================================================
# Best-effort: grader failure never fails the request
# ===========================================================================
@pytest.mark.asyncio
async def test_verifier_grader_exception_is_best_effort(monkeypatch):
    """A grader exception is caught; the node writes no keys (answer unchanged)
    and never raises."""
    from src.agents import verifier as v

    monkeypatch.setenv("QNA_AGENT_VERIFY", "true")

    async def boom_grade(azure, *, query, answer, chunks):
        raise RuntimeError("verifier LLM down")

    monkeypatch.setattr(v, "_grade", boom_grade, raising=True)

    out = await v.verifier(_state())
    assert out == {}


@pytest.mark.asyncio
async def test_verifier_empty_refine_keeps_original(monkeypatch):
    """If the single refine produces an empty answer, keep the original and flag
    unverified (never blank the answer)."""
    from src.agents import verifier as v

    monkeypatch.setenv("QNA_AGENT_VERIFY", "true")

    async def fail_grade(azure, *, query, answer, chunks):
        return {"supported": False, "score": 10, "unsupported_claims": ["c"]}

    monkeypatch.setattr(v, "_grade", fail_grade, raising=True)

    async def empty_generate(**kwargs):
        return "\n**Sources:** None"

    monkeypatch.setattr(v, "generate_openai_response", empty_generate, raising=True)

    out = await v.verifier(_state())
    assert out["verified"] is False
    assert "final_answer" not in out
