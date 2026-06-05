"""Tests for adaptive-card rendering (filenames as text, no link to filepath)."""

from __future__ import annotations

from teams_bot.cards import render_answer_card, render_error_card


def test_answer_card_shape():
    card = render_answer_card("hello", {})
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.4"
    assert card["body"][0]["text"] == "hello"


def test_answer_card_renders_filename_as_text_not_link():
    card = render_answer_card("ans", {"policy.md": "/internal/blobs/policy.md"})
    serialized = str(card)
    assert "policy.md" in serialized
    # The internal filepath must never appear, nor any navigating action.
    assert "/internal/blobs/policy.md" not in serialized
    assert "Action.OpenUrl" not in serialized


def test_answer_card_no_sources_block_when_empty():
    card = render_answer_card("ans", {})
    texts = [b.get("text", "") for b in card["body"]]
    assert "Sources" not in texts


def test_answer_card_iterates_all_citations():
    card = render_answer_card("ans", {"a.md": "/x/a", "b.md": "/x/b"})
    texts = " ".join(b.get("text", "") for b in card["body"])
    assert "a.md" in texts
    assert "b.md" in texts


def test_error_card_surfaces_request_id():
    card = render_error_card("Something went wrong.", "req-123")
    texts = " ".join(b.get("text", "") for b in card["body"])
    assert "Something went wrong." in texts
    assert "req-123" in texts


def test_error_card_without_request_id():
    card = render_error_card("oops", None)
    texts = " ".join(b.get("text", "") for b in card["body"])
    assert "oops" in texts
    assert "Reference ID" not in texts
