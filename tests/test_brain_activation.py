"""Unit tests for the brain activation engine.

Covers literal + regex key matching, case sensitivity, search range, the
per-brain cascade + block-recursion flags, and the recursive condition
evaluator (true / keyword / brain_active / relationship / style / length /
japanese / random_chance / numeric_compare / string_compare / and / or / not).
"""
from __future__ import annotations

import random

import pytest

from server.aer.activation import (
    ChatFacts,
    MAX_CASCADE_PASSES,
    activate_brains,
    any_key_matches,
    compile_key,
    evaluate,
    is_conditional,
    key_matches,
)
from server.models import (
    Brain,
    BrainKey,
    CondAnd,
    CondBrainActive,
    CondJapanese,
    CondKeyword,
    CondLength,
    CondNot,
    CondNumericCompare,
    CondOr,
    CondRandomChance,
    CondRelationship,
    CondStringCompare,
    CondStyle,
    CondTrue,
    Intimacy,
    NumericValue,
    ResponseLength,
    Style,
    StringValue,
)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_literal_key_case_insensitive_by_default():
    ck = compile_key(BrainKey(pattern="banana"))
    assert key_matches("I love BANANAS", ck)
    assert not key_matches("I love apples", ck)


def test_literal_key_case_sensitive_when_flag_set():
    ck = compile_key(BrainKey(pattern="banana", case_sensitive=True))
    assert key_matches("ripe banana on the counter", ck)
    assert not key_matches("BANANA REPUBLIC", ck)


def test_search_range_scopes_to_tail():
    ck = compile_key(BrainKey(pattern="needle", search_range=10))
    assert not key_matches("needle was at the very start of this sentence", ck)
    assert key_matches("a long preamble then ends with needle", ck)


def test_regex_key_matches_with_word_boundary():
    ck = compile_key(BrainKey(pattern=r"\bfoo\b", is_regex=True))
    assert key_matches("the foo bar", ck)
    assert not key_matches("foobar", ck)


def test_regex_inline_flag_supersedes_case_sensitive_toggle():
    ck = compile_key(BrainKey(pattern=r"(?i)apple", is_regex=True, case_sensitive=True))
    assert key_matches("Apple of my eye", ck)


def test_bad_regex_compiles_to_inert_key():
    ck = compile_key(BrainKey(pattern=r"(unclosed", is_regex=True))
    assert ck.compiled is None
    assert ck.literal is None
    assert not key_matches("anything at all", ck)


def test_empty_pattern_never_matches():
    ck = compile_key(BrainKey(pattern=""))
    assert not key_matches("anything", ck)


def test_oversize_pattern_silently_drops():
    ck = compile_key(BrainKey(pattern="x" * 1000))
    assert not key_matches("xxxxxxxxxxxxxxxxxx", ck)


def test_any_key_matches_short_circuit():
    keys = [
        BrainKey(pattern="apple"),
        BrainKey(pattern="banana"),
        BrainKey(pattern="cherry"),
    ]
    assert any_key_matches("I bought a banana", keys)
    assert not any_key_matches("I bought a kumquat", keys)


# ---------------------------------------------------------------------------
# Logic tree
# ---------------------------------------------------------------------------


def _facts(**kw):
    base = dict(
        message_count=0, user_message_count=0, contact_message_count=0,
        relationship=Intimacy.STRANGER, style=Style.CHAT,
        length=ResponseLength.MEDIUM, japanese=False,
        tags=[], story_text="", memory_text="", authors_note_text="",
    )
    base.update(kw)
    return ChatFacts(**base)


def test_cond_true():
    assert evaluate(CondTrue(), text="", facts=_facts(), active_ids=set())


def test_cond_keyword_inside_tree():
    cond = CondKeyword(keys=[BrainKey(pattern="dragon")])
    assert evaluate(cond, text="here be dragons", facts=_facts(), active_ids=set())
    assert not evaluate(cond, text="here be lizards", facts=_facts(), active_ids=set())


def test_cond_brain_active_lookup():
    cond = CondBrainActive(brain_id="abc")
    assert evaluate(cond, text="", facts=_facts(), active_ids={"abc"})
    assert not evaluate(cond, text="", facts=_facts(), active_ids={"xyz"})


def test_cond_relationship_style_length_japanese():
    facts = _facts(relationship=Intimacy.ROMANTIC, style=Style.NOVEL,
                   length=ResponseLength.LONG, japanese=True)
    assert evaluate(CondRelationship(relationship=Intimacy.ROMANTIC), text="", facts=facts, active_ids=set())
    assert not evaluate(CondRelationship(relationship=Intimacy.STRANGER), text="", facts=facts, active_ids=set())
    assert evaluate(CondStyle(style=Style.NOVEL), text="", facts=facts, active_ids=set())
    assert evaluate(CondLength(length=ResponseLength.LONG), text="", facts=facts, active_ids=set())
    assert evaluate(CondJapanese(expected=True), text="", facts=facts, active_ids=set())
    assert not evaluate(CondJapanese(expected=False), text="", facts=facts, active_ids=set())


def test_cond_numeric_compare_with_variable():
    cond = CondNumericCompare(
        lhs=NumericValue(kind="variable", variable="user_message_count"),
        op=">", rhs=NumericValue(kind="literal", literal=3),
    )
    assert evaluate(cond, text="", facts=_facts(user_message_count=5), active_ids=set())
    assert not evaluate(cond, text="", facts=_facts(user_message_count=2), active_ids=set())


def test_cond_string_compare_includes_with_variable():
    cond = CondStringCompare(
        lhs=StringValue(kind="variable", variable="tags"),
        op="includes",
        rhs=StringValue(kind="literal", literal="action"),
    )
    facts = _facts(tags=["adventure", "action", "drama"])
    assert evaluate(cond, text="", facts=facts, active_ids=set())
    facts2 = _facts(tags=["adventure", "drama"])
    assert not evaluate(cond, text="", facts=facts2, active_ids=set())


def test_cond_string_compare_case_sensitive():
    cond = CondStringCompare(
        lhs=StringValue(kind="literal", literal="HELLO"),
        op="equals",
        rhs=StringValue(kind="literal", literal="hello"),
        case_sensitive=True,
    )
    assert not evaluate(cond, text="", facts=_facts(), active_ids=set())
    cond.case_sensitive = False
    assert evaluate(cond, text="", facts=_facts(), active_ids=set())


def test_cond_random_chance_seeded():
    cond = CondRandomChance(percent=50.0)
    rng = random.Random(42)
    hits = sum(1 for _ in range(2000) if evaluate(cond, text="", facts=_facts(), active_ids=set(), rng=rng))
    assert 900 < hits < 1100   # ~50% give or take


def test_cond_random_chance_extremes():
    rng = random.Random(0)
    for _ in range(50):
        assert evaluate(CondRandomChance(percent=100.0), text="", facts=_facts(), active_ids=set(), rng=rng)
        assert not evaluate(CondRandomChance(percent=0.0), text="", facts=_facts(), active_ids=set(), rng=rng)


def test_cond_and_or_not():
    a = CondTrue()
    b = CondNumericCompare(
        lhs=NumericValue(kind="variable", variable="message_count"),
        op=">", rhs=NumericValue(kind="literal", literal=5),
    )
    facts = _facts(message_count=3)
    and_node = CondAnd(children=[a, b])
    or_node = CondOr(children=[a, b])
    not_node = CondNot(child=b)
    assert not evaluate(and_node, text="", facts=facts, active_ids=set())
    assert evaluate(or_node, text="", facts=facts, active_ids=set())
    assert evaluate(not_node, text="", facts=facts, active_ids=set())
    # Empty AND group never matches (no positive evidence).
    assert not evaluate(CondAnd(children=[]), text="", facts=facts, active_ids=set())


# ---------------------------------------------------------------------------
# Activation flow: cascade + blocks_recursion
# ---------------------------------------------------------------------------


def test_unconditional_brain_short_circuits():
    b = Brain(name="X", content="c")
    assert not is_conditional(b)
    b2 = Brain(name="Y", content="c", keys=[BrainKey(pattern="x")])
    assert is_conditional(b2)


def test_cascade_chains_activation():
    a = Brain(id="a", name="A", content="ELEPHANT TRUMPETS LOUDLY",
              keys=[BrainKey(pattern="elephant")], cascades=True)
    b = Brain(id="b", name="B", content="",
              keys=[BrainKey(pattern="trumpet")])
    out = activate_brains([a, b], base_text="I saw an elephant", facts=_facts())
    assert [x.id for x in out] == ["a", "b"]


def test_blocks_recursion_pins_brain_to_base_text():
    a = Brain(id="a", name="A", content="ELEPHANT TRUMPETS LOUDLY",
              keys=[BrainKey(pattern="elephant")], cascades=True)
    c = Brain(id="c", name="C", content="",
              keys=[BrainKey(pattern="trumpet")], blocks_recursion=True)
    out = activate_brains([a, c], base_text="I saw an elephant", facts=_facts())
    assert [x.id for x in out] == ["a"]


def test_cascade_chain_three_deep():
    a = Brain(id="a", name="A", content="bridge to B",
              keys=[BrainKey(pattern="start")], cascades=True)
    b = Brain(id="b", name="B", content="bridge to C",
              keys=[BrainKey(pattern="bridge")], cascades=True)
    c = Brain(id="c", name="C", content="",
              keys=[BrainKey(pattern="C")])
    out = activate_brains([a, b, c], base_text="we start here", facts=_facts())
    assert [x.id for x in out] == ["a", "b", "c"]


def test_cascade_pass_cap_terminates():
    # Pathological: every brain cascades into every other. Cap should bound work.
    brains = []
    for i in range(20):
        brains.append(Brain(
            id=f"b{i}", name=f"B{i}", content=f"trigger{i+1}",
            keys=[BrainKey(pattern=f"trigger{i}")],
            cascades=True,
        ))
    out = activate_brains(brains, base_text="trigger0", facts=_facts())
    # We activate at most MAX_CASCADE_PASSES brains in a single chain.
    assert 1 <= len(out) <= MAX_CASCADE_PASSES * len(brains)


def test_random_chance_advanced_activates_brain():
    rng = random.Random(123)
    b = Brain(id="b", name="B", content="",
              advanced=CondRandomChance(percent=100.0))
    out = activate_brains([b], base_text="", facts=_facts(), rng=rng)
    assert out == [b]
    b2 = Brain(id="b2", name="B2", content="",
               advanced=CondRandomChance(percent=0.0))
    assert activate_brains([b2], base_text="", facts=_facts(), rng=rng) == []


def test_brain_active_lookup_through_activation():
    # B requires A to be active.
    a = Brain(id="a", name="A", content="A-content",
              keys=[BrainKey(pattern="aaa")])
    b = Brain(id="b", name="B", content="",
              advanced=CondBrainActive(brain_id="a"))
    out = activate_brains([a, b], base_text="aaa", facts=_facts())
    assert [x.id for x in out] == ["a", "b"]
    out2 = activate_brains([a, b], base_text="bbb", facts=_facts())
    assert out2 == []
