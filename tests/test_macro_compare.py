"""Tests for the comparison + boolean macros ({{eq}} / {{neq}} / {{lt}} /
{{gt}} / {{lte}} / {{gte}} / {{and}} / {{or}})."""
from __future__ import annotations

from datetime import datetime

import pytest

from server.aer.macros import apply_macros
from server.models import Contact, User


def _c():
    return Contact(name="Alice")


def _u():
    return User(name="Bob")


# ---------------------------------------------------------------------------
# eq / neq — string equality, case-sensitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b,expected", [
    ("a", "a", "true"),
    ("a", "b", ""),
    ("A", "a", ""),    # case-sensitive
    ("", "", "true"),
    ("0", "0", "true"),
    ("a a", "a a", "true"),
])
def test_eq(a, b, expected):
    assert apply_macros(f"{{{{eq::{a}::{b}}}}}", _c(), _u()) == expected


@pytest.mark.parametrize("a,b,expected", [
    ("a", "a", ""),
    ("a", "b", "true"),
    ("A", "a", "true"),
    ("", "", ""),
])
def test_neq(a, b, expected):
    assert apply_macros(f"{{{{neq::{a}::{b}}}}}", _c(), _u()) == expected


# ---------------------------------------------------------------------------
# Numeric comparisons — float-parsed; non-numeric → ""
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b,expected", [
    ("1", "2", "true"),
    ("2", "1", ""),
    ("1", "1", ""),
    ("1.5", "1.6", "true"),
    ("-1", "0", "true"),
    ("a", "1", ""),       # non-numeric → ""
    ("1", "a", ""),
])
def test_lt(a, b, expected):
    assert apply_macros(f"{{{{lt::{a}::{b}}}}}", _c(), _u()) == expected


@pytest.mark.parametrize("a,b,expected", [
    ("2", "1", "true"),
    ("1", "2", ""),
    ("1", "1", ""),
    ("1.5", "1", "true"),
])
def test_gt(a, b, expected):
    assert apply_macros(f"{{{{gt::{a}::{b}}}}}", _c(), _u()) == expected


@pytest.mark.parametrize("a,b,expected", [
    ("1", "1", "true"),
    ("0", "1", "true"),
    ("2", "1", ""),
])
def test_lte(a, b, expected):
    assert apply_macros(f"{{{{lte::{a}::{b}}}}}", _c(), _u()) == expected


@pytest.mark.parametrize("a,b,expected", [
    ("1", "1", "true"),
    ("2", "1", "true"),
    ("0", "1", ""),
])
def test_gte(a, b, expected):
    assert apply_macros(f"{{{{gte::{a}::{b}}}}}", _c(), _u()) == expected


# ---------------------------------------------------------------------------
# Boolean combinators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("args,expected", [
    (("a",), "true"),
    (("a", "b"), "true"),
    (("a", "b", "c"), "true"),
    (("a", ""), ""),
    (("a", "false"), ""),
    (("a", "0"), ""),
    (("a", "off"), ""),
    (("a", "OFF"), ""),       # case-insensitive
    ((), ""),
])
def test_and(args, expected):
    body = "::".join(args)
    text = f"{{{{and::{body}}}}}" if args else "{{and}}"
    assert apply_macros(text, _c(), _u()) == expected


@pytest.mark.parametrize("args,expected", [
    (("a",), "true"),
    (("", ""), ""),
    (("", "a"), "true"),
    (("false", "0", "off"), ""),
    (("false", "0", "x"), "true"),
])
def test_or(args, expected):
    body = "::".join(args)
    assert apply_macros(f"{{{{or::{body}}}}}", _c(), _u()) == expected


# ---------------------------------------------------------------------------
# Composition with {{if}}
# ---------------------------------------------------------------------------


def test_if_with_eq_inline():
    out = apply_macros("{{if {{eq::a::a}}}}yes{{else}}no{{/if}}", _c(), _u())
    assert out == "yes"


def test_if_with_neq_against_field():
    c = Contact(name="Alice", species="elf")
    out = apply_macros(
        "{{if {{neq::{{contact.species}}::human}}}}not-human{{/if}}", c, _u(),
    )
    assert out == "not-human"


def test_if_with_gt_against_history_length():
    # {{lastmessageid}} is "" with no active path, parsed as non-numeric.
    out = apply_macros("{{if {{gt::5::1}}}}go{{/if}}", _c(), _u())
    assert out == "go"


def test_if_with_clock_via_datetimeformat():
    # Mock the clock by passing ``now``. Verify {{if {{eq::HH::12}}}}
    # fires only at noon.
    noon = datetime(2026, 1, 1, 12, 0, 0)
    one_pm = datetime(2026, 1, 1, 13, 0, 0)
    text = "{{if {{eq::{{datetimeformat::HH}}::12}}}}noon{{else}}not-noon{{/if}}"
    assert apply_macros(text, _c(), _u(), now=noon) == "noon"
    assert apply_macros(text, _c(), _u(), now=one_pm) == "not-noon"


def test_and_inside_if():
    c1 = Contact(name="Alice", persona="x", tags="t")
    c2 = Contact(name="Alice", persona="x", tags="ignore")
    text = (
        "{{if {{and::{{contact.persona}}::{{neq::{{contact.tags}}::ignore}}}}}}"
        "ok{{/if}}"
    )
    assert apply_macros(text, c1, _u()) == "ok"
    assert apply_macros(text, c2, _u()) == ""


def test_or_inside_if():
    # Either intimacy value triggers the warm-tone block.
    from server.models import Chat, Intimacy, Style

    def chat(intimacy):
        return Chat(contact_id="x", user_id="y", intimacy=intimacy, style=Style.CHAT)

    text = (
        "{{if {{or::{{eq::{{chat.intimacy}}::close}}"
        "::{{eq::{{chat.intimacy}}::romantic}}}}}}warm{{/if}}"
    )
    assert apply_macros(text, _c(), _u(), chat=chat(Intimacy.CLOSE)) == "warm"
    assert apply_macros(text, _c(), _u(), chat=chat(Intimacy.ROMANTIC)) == "warm"
    assert apply_macros(text, _c(), _u(), chat=chat(Intimacy.STRANGER)) == ""
