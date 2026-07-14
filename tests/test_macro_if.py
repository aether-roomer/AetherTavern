"""Tests for the {{if}} / {{else}} / {{/if}} control-flow macros."""
from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from server.aer.macros import apply_macros
from server.models import Brain, Contact, User


def _c(**kwargs):
    return Contact(name="Alice", **kwargs)


def _u(**kwargs):
    return User(name="Bob", **kwargs)


# ---------------------------------------------------------------------------
# Scoped form — truthy / falsy permutations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("persona,expected", [
    ("Smart", "yes"),
    ("", ""),
    ("   ", ""),     # strip → empty → falsy
    ("\n\t", ""),    # whitespace-only → falsy
])
def test_scoped_if_truthiness_from_contact_field(persona, expected):
    c = _c(persona=persona)
    assert apply_macros("{{if contact.persona}}yes{{/if}}", c, _u()) == expected


@pytest.mark.parametrize("value,expected_truthy", [
    ("anything", True),
    ("FALSE", False),     # case-insensitive
    ("False", False),
    ("0", False),
    ("Off", False),
    ("on", True),
    ("1", True),
    ("nope", True),
])
def test_scoped_if_with_literal_falsy_words(value, expected_truthy):
    # We can't easily inject a literal — but {{contact.persona}} with the
    # literal value as the field content exercises the same code path.
    c = _c(persona=value)
    result = apply_macros("{{if contact.persona}}T{{else}}F{{/if}}", c, _u())
    assert result == ("T" if expected_truthy else "F")


def test_scoped_if_else():
    c_set = _c(persona="Smart")
    c_empty = _c()
    assert apply_macros("{{if contact.persona}}YES{{else}}NO{{/if}}", c_set, _u()) == "YES"
    assert apply_macros("{{if contact.persona}}YES{{else}}NO{{/if}}", c_empty, _u()) == "NO"


def test_scoped_if_negation():
    c_empty = _c()
    c_set = _c(persona="Smart")
    assert apply_macros("{{if !contact.persona}}empty{{/if}}", c_empty, _u()) == "empty"
    assert apply_macros("{{if !contact.persona}}empty{{/if}}", c_set, _u()) == ""


def test_scoped_if_auto_trim():
    # Default scoped form trims + dedents.
    c = _c(persona="Smart")
    out = apply_macros("{{if contact.persona}}\n  hello\n  world\n{{/if}}", c, _u())
    assert out == "hello\nworld"


def test_scoped_if_hash_flag_preserves_whitespace():
    c = _c(persona="Smart")
    out = apply_macros("A{{#if contact.persona}}\nB\n{{/if}}C", c, _u())
    # Leading \n inside body is preserved.
    assert out == "A\nB\nC"


def test_scoped_else_branches_both_trimmed_under_non_hash():
    c_set = _c(persona="X")
    c_empty = _c()
    text = "{{if contact.persona}}  yes  {{else}}  no  {{/if}}"
    assert apply_macros(text, c_set, _u()) == "yes"
    assert apply_macros(text, c_empty, _u()) == "no"


# ---------------------------------------------------------------------------
# Inline form
# ---------------------------------------------------------------------------


def test_inline_if_truthy_and_falsy():
    c_set = _c(persona="x")
    c_empty = _c()
    assert apply_macros("{{if contact.persona::YES::NO}}", c_set, _u()) == "YES"
    assert apply_macros("{{if contact.persona::YES::NO}}", c_empty, _u()) == "NO"


def test_inline_if_then_only():
    # 2-arg form: no else branch, falsy → ""
    c_set = _c(persona="x")
    c_empty = _c()
    assert apply_macros("X{{if contact.persona::YES}}Y", c_set, _u()) == "XYESY"
    assert apply_macros("X{{if contact.persona::YES}}Y", c_empty, _u()) == "XY"


def test_inline_raw_args_preserves_whitespace_newlines():
    # The seed preset's Tags-wart idiom — branches are literal "\n\n" / "\n".
    c_set = _c(persona="x")
    c_empty = _c()
    assert apply_macros(
        "A{{if contact.persona::\n\n::\n}}B", c_set, _u(),
    ) == "A\n\nB"
    assert apply_macros(
        "A{{if contact.persona::\n\n::\n}}B", c_empty, _u(),
    ) == "A\nB"


# ---------------------------------------------------------------------------
# Nested conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b,expected", [
    ("X", "Y", "AB"),
    ("X", "",  ""),
    ("",  "Y", ""),
    ("",  "",  ""),
])
def test_nested_scoped(a, b, expected):
    c = _c(persona=a, appearance=b)
    text = "{{if contact.persona}}{{if contact.appearance}}AB{{/if}}{{/if}}"
    assert apply_macros(text, c, _u()) == expected


def test_nested_macro_condition_resolves_inner_first():
    c = _c(persona="hi")
    assert apply_macros(
        "{{if {{contact.persona}}}}got{{/if}}", c, _u(),
    ) == "got"
    c2 = _c()
    assert apply_macros(
        "{{if {{contact.persona}}}}got{{/if}}", c2, _u(),
    ) == ""


def test_or_via_concatenation():
    c_both_empty = _c()
    c_persona = _c(persona="x")
    text = "{{if {{contact.persona}}{{contact.appearance}}}}either{{/if}}"
    assert apply_macros(text, c_both_empty, _u()) == ""
    assert apply_macros(text, c_persona, _u()) == "either"


def test_bare_name_auto_resolve_equivalent_to_braced():
    # ``{{if contact.persona}}`` should behave identically to
    # ``{{if {{contact.persona}}}}`` because the bare-name auto-resolve
    # path expands a registered zero-arg macro.
    c_set = _c(persona="x")
    c_empty = _c()
    bare = "{{if contact.persona}}T{{else}}F{{/if}}"
    braced = "{{if {{contact.persona}}}}T{{else}}F{{/if}}"
    assert apply_macros(bare, c_set, _u()) == apply_macros(braced, c_set, _u())
    assert apply_macros(bare, c_empty, _u()) == apply_macros(braced, c_empty, _u())


# ---------------------------------------------------------------------------
# Allowlist gating
# ---------------------------------------------------------------------------


def test_allowlist_skips_pre_pass_when_if_not_in_allowlist():
    # Chat-message persist path uses ``allowlist={"roll"}`` to keep
    # user-typed text mostly literal. {{if}} tokens must pass through.
    c = _c(persona="x")
    text = "{{if contact.persona}}body{{/if}}"
    assert apply_macros(text, c, _u(), allowlist={"roll"}) == text


def test_allowlist_inline_if_also_pass_through():
    c = _c(persona="x")
    text = "{{if X::Y}}"
    assert apply_macros(text, c, _u(), allowlist={"roll"}) == text


def test_allowlist_with_if_in_set_runs_pre_pass():
    c = _c(persona="x")
    text = "{{if contact.persona}}body{{/if}}"
    out = apply_macros(text, c, _u(), allowlist={"if", "contact.persona"})
    assert out == "body"


# ---------------------------------------------------------------------------
# Eager vs lazy semantics
# ---------------------------------------------------------------------------


def test_scoped_if_is_lazy_unselected_branch_macros_dont_run():
    # ``{{roll::1d6}}`` in the unselected branch must NOT consume entropy.
    c = _c(persona="x")  # truthy, so else branch is unselected
    with patch("server.aer.macros.random.randint", return_value=4) as m:
        out = apply_macros(
            "{{if contact.persona}}yes{{else}}{{roll::1d6}}{{/if}}",
            c, _u(),
        )
    assert out == "yes"
    assert m.call_count == 0


def test_inline_if_is_eager_both_branches_resolve():
    # The inline handler receives raw args, but the regular pass resolves
    # nested ``{{roll}}`` *before* the if handler picks. Both branches
    # therefore consume entropy.
    c = _c(persona="x")
    with patch("server.aer.macros.random.randint", return_value=4) as m:
        apply_macros(
            "{{if contact.persona::{{roll::1d6}}::{{roll::1d6}}}}",
            c, _u(),
        )
    assert m.call_count == 2


# ---------------------------------------------------------------------------
# Malformed inputs — round-trip literally
# ---------------------------------------------------------------------------


def test_unmatched_opener_stays_literal():
    c = _c(persona="x")
    out = apply_macros("{{if contact.persona}}body (no /if)", c, _u())
    assert out == "{{if contact.persona}}body (no /if)"


def test_stray_close_is_literal():
    c = _c()
    assert apply_macros("{{/if}}", c, _u()) == "{{/if}}"


def test_stray_else_is_literal():
    c = _c()
    assert apply_macros("{{else}}", c, _u()) == "{{else}}"


def test_only_inner_closes_outer_stays_literal():
    c = _c(persona="x", appearance="y")
    # Inner resolves; outer has no matching /if so stays literal.
    out = apply_macros(
        "{{if contact.persona}}{{if contact.appearance}}IN{{/if}}",
        c, _u(),
    )
    # The pre-pass abandons the unmatched outer frame, so the opener stays
    # literal but the (now-resolved) inner branch fills in.
    assert "IN" in out
    assert "{{if contact.persona}}" in out


def test_brace_balanced_scanner_handles_nested_macro_in_condition():
    # The condition contains ``}}`` from a nested macro
    # ``{{datetimeformat::HH}}`` — the brace-balanced scan must still
    # find the correct outer ``}}`` for the opener.
    c = _c(persona="x")
    text = "{{if {{contact.persona}}}}body{{/if}}"
    assert apply_macros(text, c, _u()) == "body"


# ---------------------------------------------------------------------------
# Scoped vs inline disambiguation at the opener
# ---------------------------------------------------------------------------


def test_two_args_classifies_as_inline_no_close_needed():
    c = _c(persona="x")
    # ``{{if a::b}}`` is inline. No /if needed.
    out = apply_macros("X{{if contact.persona::b}}Y", c, _u())
    assert out == "XbY"


def test_three_args_classifies_as_inline():
    c_set = _c(persona="x")
    c_empty = _c()
    text = "{{if contact.persona::T::F}}"
    assert apply_macros(text, c_set, _u()) == "T"
    assert apply_macros(text, c_empty, _u()) == "F"


def test_nested_braces_inside_args_dont_split_depth_0():
    # ``{{datetimeformat::HH}}`` inside an arg has a literal ``::`` but
    # at depth > 0, so it doesn't add a piece to the depth-0 split.
    c = _c(persona="x")
    text = "{{if contact.persona::{{datetimeformat::HH}}::otherwise}}"
    out = apply_macros(text, c, _u())
    # The output is the datetimeformat result — exact value depends on the
    # clock, but it's a non-empty 2-char string (or "otherwise" if c.persona
    # were falsy, which we ruled out).
    assert out != "otherwise"


def test_hash_flag_on_inline_form_silently_dropped():
    # ``{{#if X::Y::Z}}`` classifies as inline regardless of the flag.
    c_set = _c(persona="x")
    out = apply_macros("{{#if contact.persona::yes::no}}", c_set, _u())
    # Either: inline path returns "yes" with no auto-trim semantics, OR
    # the literal token survives. The chosen implementation treats it as
    # inline and ignores the flag.
    assert out in ("yes", "{{#if contact.persona::yes::no}}")


# ---------------------------------------------------------------------------
# Composition / sanity
# ---------------------------------------------------------------------------


def test_outer_wrapping_idiom_drops_whole_section_when_all_empty():
    # The seed preset's Scenario block wraps its content in an outer
    # {{#if {{a}}{{b}}{{c}}}}; when all three are empty the whole section
    # should be empty.
    c = _c()
    out = apply_macros(
        "before{{#if {{contact.persona}}{{contact.appearance}}{{contact.tags}}}}"
        "SECTION{{/if}}after",
        c, _u(),
    )
    assert out == "beforeafter"


def test_outer_wrapping_idiom_renders_when_any_field_present():
    c = _c(tags="t")
    out = apply_macros(
        "before{{#if {{contact.persona}}{{contact.appearance}}{{contact.tags}}}}"
        "SECTION{{/if}}after",
        c, _u(),
    )
    assert out == "beforeSECTIONafter"
