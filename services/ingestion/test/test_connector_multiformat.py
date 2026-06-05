"""Connector-core multi-format helper tests.

Covers the format-dispatch helpers added for multi-format ingestion:
- `is_supported_name` mirrors the loader registry allowlist (PDF + the new
  formats), so connectors enumerate the same set the /upload route accepts.
- `validate_content_magic` is the post-download integrity gate, dispatched by
  extension: PDF → %PDF (NotAPdfError), DOCX/PPTX → zip/OOXML (InvalidContentError),
  text formats (HTML/HTM/MD/TXT) → not gated.

Hermetic — no Azure, no network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from connectors.core import (  # noqa: E402
    InvalidContentError,
    NotAPdfError,
    is_pdf_name,
    is_supported_name,
    validate_content_magic,
)

pytestmark = pytest.mark.ingestion


def test_is_supported_name_matches_registry_allowlist():
    for name in ("a.pdf", "a.PDF", "b.docx", "c.pptx", "d.html", "e.htm", "f.md", "g.txt"):
        assert is_supported_name(name), name
    for name in ("img.png", "data.zip", "archive.tar.gz", "noext"):
        assert not is_supported_name(name), name


def test_is_pdf_name_still_pdf_specific():
    assert is_pdf_name("a.pdf")
    assert not is_pdf_name("a.docx")


def test_validate_content_magic_pdf_ok():
    # Real PDF header passes; no exception.
    validate_content_magic("doc.pdf", b"%PDF-1.7 ...")


def test_validate_content_magic_pdf_rejects_non_pdf():
    with pytest.raises(NotAPdfError):
        validate_content_magic("doc.pdf", b"GIF89a not a pdf")


def test_validate_content_magic_docx_ok():
    # OOXML files are zip containers → start with "PK".
    validate_content_magic("report.docx", b"PK\x03\x04rest-of-zip")


def test_validate_content_magic_pptx_rejects_non_zip():
    with pytest.raises(InvalidContentError):
        validate_content_magic("deck.pptx", b"not-a-zip-container")


def test_validate_content_magic_text_formats_not_gated():
    # No reliable magic header → arbitrary bytes pass; the loader surfaces any
    # genuinely-malformed content downstream.
    for name in ("notes.txt", "page.html", "legacy.htm", "guide.md"):
        validate_content_magic(name, b"\x00\x01arbitrary bytes, not a magic header")
