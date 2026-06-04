"""Unit tests for the typed QnA success-response contract.

These cover the models in `src.core.responses` in isolation (no FastAPI/HTTP):
- `CitationMap` serializes to a flat `{filename: filepath}` dict.
- `QnASuccessResponse` round-trips the historical `{answer, citation}` JSON.
- Optional defensive fields are excluded with `exclude_none`.
- A wrong type for `citation` is rejected.

HTTP-level byte-identity is covered by test.py
(`test_qna_response_model_keeps_json_byte_identical`).
"""

import pytest
from pydantic import ValidationError
from src.core.responses import CitationMap, QnASuccessResponse


def test_citation_map_serializes_flat():
    """CitationMap must dump to a plain dict, never a wrapped {"root": ...}."""
    cm = CitationMap({"a.md": "/docs/a.md", "b.md": "/docs/b.md"})
    dumped = cm.model_dump()
    assert dumped == {"a.md": "/docs/a.md", "b.md": "/docs/b.md"}
    assert "root" not in dumped


def test_citation_map_empty_serializes_to_empty_object():
    assert CitationMap({}).model_dump() == {}
    # Default-constructed is also an empty mapping.
    assert CitationMap().model_dump() == {}


def test_citation_map_json_is_flat_object():
    cm = CitationMap({"a.md": "/docs/a.md"})
    assert cm.model_dump_json() == '{"a.md":"/docs/a.md"}'


def test_success_response_roundtrips_historical_shape():
    """The model must accept and re-emit the exact historical payload."""
    historical = {"answer": "the answer", "citation": {"a.md": "/docs/a.md"}}
    model = QnASuccessResponse.model_validate(historical)
    # exclude_none mirrors the route config; dumped must equal the input.
    assert model.model_dump(exclude_none=True) == historical


def test_success_response_excludes_optional_none_fields():
    """request_id/error default to None and must not appear with exclude_none."""
    model = QnASuccessResponse(answer="a", citation={"f.md": "/p/f.md"})
    dumped = model.model_dump(exclude_none=True)
    assert dumped == {"answer": "a", "citation": {"f.md": "/p/f.md"}}
    assert "request_id" not in dumped
    assert "error" not in dumped


def test_success_response_accepts_plain_dict_for_citation():
    model = QnASuccessResponse(answer="a", citation={"f.md": "/p/f.md"})
    assert model.citation.model_dump() == {"f.md": "/p/f.md"}


def test_success_response_empty_citation_survives_exclude_none():
    """Empty citation is an empty dict, NOT None — must not be dropped."""
    model = QnASuccessResponse(answer="hi", citation={})
    assert model.model_dump(exclude_none=True) == {"answer": "hi", "citation": {}}


def test_success_response_rejects_wrong_citation_type():
    """citation must be a mapping; a string/list is a validation error."""
    with pytest.raises(ValidationError):
        QnASuccessResponse(answer="a", citation="not-a-map")
    with pytest.raises(ValidationError):
        QnASuccessResponse(answer="a", citation=["a.md"])


def test_success_response_requires_answer():
    with pytest.raises(ValidationError):
        QnASuccessResponse(citation={})
