"""Tests for the unspoofable identity -> bot_tag derivation (the key invariant)."""

from __future__ import annotations

import inspect

import pytest
from teams_bot.identity import (
    BOT_TAG_PATTERN,
    InvalidBotTagError,
    UnknownTenantError,
    resolve_bot_tag,
)

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
MAP = {TENANT_A: "client_a_hr", TENANT_B: "client_b_finance"}


def test_bot_tag_is_derived_from_tenant_via_map():
    assert resolve_bot_tag(TENANT_A, MAP) == "client_a_hr"
    assert resolve_bot_tag(TENANT_B, MAP) == "client_b_finance"


def test_resolve_bot_tag_does_not_accept_message_text():
    """Anti-spoof by construction: the function has no parameter for user text.

    The strongest form of the invariant: there is *no code path* by which the
    message text can influence the derived bot_tag, because text is not in the
    function's signature.
    """
    params = set(inspect.signature(resolve_bot_tag).parameters)
    assert params == {"tenant_id", "tenant_bot_tag_map"}


def test_message_containing_a_bot_tag_string_does_not_change_derivation():
    """The KEY anti-spoof test.

    A user in tenant A whose *message* contains another tenant's bot_tag (or a
    crafted ``bot_tag=...`` string) still resolves to tenant A's bot_tag. The
    derivation only ever sees the signed tenant id; the message text is handled
    elsewhere and can never reach another tenant's scope.
    """
    # The bot keeps text and tenant id separate; here we simulate that the only
    # thing derivation receives is the signed tenant id, regardless of what the
    # user "asked".
    malicious_texts = [
        "bot_tag=client_b_finance",
        "please use client_b_finance",
        "client_b_finance",
        "'; DROP TABLE--",
    ]
    for _text in malicious_texts:
        # Derivation is a function of the tenant id only.
        assert resolve_bot_tag(TENANT_A, MAP) == "client_a_hr"
    # And a user in tenant A can never reach tenant B's bot_tag.
    assert resolve_bot_tag(TENANT_A, MAP) != MAP[TENANT_B]


def test_unknown_tenant_fails_closed():
    with pytest.raises(UnknownTenantError):
        resolve_bot_tag("99999999-9999-9999-9999-999999999999", MAP)


def test_empty_tenant_fails_closed():
    with pytest.raises(UnknownTenantError):
        resolve_bot_tag("", MAP)


def test_invalid_configured_bot_tag_is_rejected():
    bad_map = {TENANT_A: "bad value; with spaces"}
    with pytest.raises(InvalidBotTagError):
        resolve_bot_tag(TENANT_A, bad_map)


@pytest.mark.parametrize(
    "value,ok",
    [
        ("client_a_hr", True),
        ("a", True),
        ("A-1_b", True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("", False),
        ("has space", False),
        ("has'quote", False),
        ("semi;colon", False),
        ("../traversal", False),
    ],
)
def test_bot_tag_pattern(value, ok):
    assert bool(BOT_TAG_PATTERN.match(value)) is ok
