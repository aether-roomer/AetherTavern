"""Brain activation engine.

Decides which conditional brains fire for a given generation. A brain is
"conditional" when it has any keys or an advanced condition tree set; otherwise
it is unconditional and bypasses this engine entirely.

The engine compiles each key once (regexes via the third-party ``regex`` lib so
matching is cancellable on a deadline), runs activation in passes to support
cascade (each activated brain whose ``cascades`` is set contributes its content
to the search text used by later passes), and respects ``blocks_recursion`` so
authors can pin entries that must only react to the original chat content.

Match cost is bounded:
- Per-regex match has a hard ``REGEX_TIMEOUT_SECONDS`` deadline (5 ms).
- Pattern length is capped at ``MAX_PATTERN_LENGTH`` chars.
- Cascade is capped at ``MAX_CASCADE_PASSES`` rounds.
"""
from __future__ import annotations

import logging
import random as _random
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import regex as regex_lib

from server.models import (
    Brain,
    BrainCondition,
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


log = logging.getLogger("aether.brain.activation")


REGEX_TIMEOUT_SECONDS = 0.005   # per regex.search call
MAX_PATTERN_LENGTH = 256        # rejected on edit + on import
MAX_CASCADE_PASSES = 8          # bound the cascade fixed-point loop


# ---------------------------------------------------------------------------
# Compiled keys
# ---------------------------------------------------------------------------


@dataclass
class CompiledKey:
    """A ``BrainKey`` prepared for fast matching.

    For literal (non-regex) keys, ``literal`` carries the (possibly
    case-folded) needle and ``compiled`` is None. For regex keys, ``compiled``
    is the third-party ``regex`` ``Pattern`` and ``literal`` is None. For
    invalid / over-length keys, both are None — the key never matches.
    """

    raw: BrainKey
    literal: str | None = None
    compiled: object | None = None  # regex.Pattern when set


def compile_key(key: BrainKey) -> CompiledKey:
    pattern = key.pattern or ""
    if not pattern:
        return CompiledKey(raw=key)
    if len(pattern) > MAX_PATTERN_LENGTH:
        log.warning("Brain key pattern exceeds %d chars; treated as no-match.", MAX_PATTERN_LENGTH)
        return CompiledKey(raw=key)
    if key.is_regex:
        try:
            compiled = regex_lib.compile(pattern)
        except regex_lib.error as exc:
            log.warning("Invalid regex in brain key %r: %s", pattern, exc)
            return CompiledKey(raw=key)
        return CompiledKey(raw=key, compiled=compiled)
    needle = pattern if key.case_sensitive else pattern.casefold()
    return CompiledKey(raw=key, literal=needle)


def _scoped(text: str, key: BrainKey) -> str:
    if key.search_range and key.search_range > 0 and len(text) > key.search_range:
        return text[-key.search_range:]
    return text


def key_matches(text: str, ck: CompiledKey) -> bool:
    key = ck.raw
    if not key.pattern:
        return False
    haystack = _scoped(text, key)
    if ck.compiled is not None:
        try:
            return ck.compiled.search(haystack, timeout=REGEX_TIMEOUT_SECONDS) is not None
        except TimeoutError:
            log.warning("Regex timeout on brain key %r; treated as no-match.", key.pattern)
            return False
    if ck.literal is None:
        return False
    if not key.case_sensitive:
        haystack = haystack.casefold()
    if not key.match_whole_words:
        return ck.literal in haystack
    # Whole-word matching: scan substring positions and require word
    # boundaries on both sides. ``_is_word_char`` treats anything
    # non-alphanumeric (incl. emoji / punctuation / whitespace) as a
    # boundary, matching the common author intent of "the key as a
    # whole word, not as a substring of a longer word".
    needle = ck.literal
    if not needle:
        return False
    start = 0
    n = len(haystack)
    nn = len(needle)
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return False
        before_ok = idx == 0 or not _is_word_char(haystack[idx - 1])
        after_idx = idx + nn
        after_ok = after_idx >= n or not _is_word_char(haystack[after_idx])
        if before_ok and after_ok:
            return True
        start = idx + 1


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def any_key_matches(text: str, keys: Sequence[BrainKey]) -> bool:
    for key in keys:
        if key_matches(text, compile_key(key)):
            return True
    return False


# ---------------------------------------------------------------------------
# ChatFacts — variables exposed to advanced conditions
# ---------------------------------------------------------------------------


@dataclass
class ChatFacts:
    message_count: int = 0
    user_message_count: int = 0
    contact_message_count: int = 0
    relationship: Intimacy = Intimacy.STRANGER
    style: Style = Style.CHAT
    length: ResponseLength | None = None
    japanese: bool = False
    tags: list[str] = field(default_factory=list)
    story_text: str = ""
    memory_text: str = ""
    authors_note_text: str = ""

    def numeric(self, var: str) -> float:
        if var == "message_count":
            return float(self.message_count)
        if var == "user_message_count":
            return float(self.user_message_count)
        if var == "contact_message_count":
            return float(self.contact_message_count)
        return 0.0

    def string(self, var: str) -> str:
        if var == "story_text":
            return self.story_text
        if var == "memory_text":
            return self.memory_text
        if var == "authors_note_text":
            return self.authors_note_text
        if var == "tags":
            return ", ".join(self.tags)
        return ""


def _resolve_numeric(value: NumericValue, facts: ChatFacts) -> float:
    if value.kind == "literal":
        return float(value.literal)
    return facts.numeric(value.variable)


def _resolve_string(value: StringValue, facts: ChatFacts) -> str:
    if value.kind == "literal":
        return value.literal or ""
    return facts.string(value.variable)


# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------


def evaluate(
    cond: BrainCondition | None,
    *,
    text: str,
    facts: ChatFacts,
    active_ids: set[str],
    rng: _random.Random | None = None,
) -> bool:
    """Recursively evaluate a brain-activation condition tree."""
    if cond is None:
        return False
    if isinstance(cond, CondTrue):
        return True
    if isinstance(cond, CondKeyword):
        return any_key_matches(text, cond.keys)
    if isinstance(cond, CondBrainActive):
        return bool(cond.brain_id) and cond.brain_id in active_ids
    if isinstance(cond, CondRelationship):
        return facts.relationship == cond.relationship
    if isinstance(cond, CondStyle):
        return facts.style == cond.style
    if isinstance(cond, CondLength):
        return facts.length == cond.length
    if isinstance(cond, CondJapanese):
        return facts.japanese == cond.expected
    if isinstance(cond, CondRandomChance):
        roll = (rng or _random).random() * 100.0
        return roll < cond.percent
    if isinstance(cond, CondNumericCompare):
        lhs = _resolve_numeric(cond.lhs, facts)
        rhs = _resolve_numeric(cond.rhs, facts)
        op = cond.op
        if op == "==": return lhs == rhs
        if op == "!=": return lhs != rhs
        if op == "<":  return lhs <  rhs
        if op == ">":  return lhs >  rhs
        if op == "<=": return lhs <= rhs
        if op == ">=": return lhs >= rhs
        return False
    if isinstance(cond, CondStringCompare):
        lhs = _resolve_string(cond.lhs, facts)
        rhs = _resolve_string(cond.rhs, facts)
        if not cond.case_sensitive:
            lhs = lhs.casefold()
            rhs = rhs.casefold()
        if cond.op == "equals":
            return lhs == rhs
        if cond.op == "includes":
            return rhs in lhs
        if cond.op == "starts_with":
            return lhs.startswith(rhs)
        if cond.op == "ends_with":
            return lhs.endswith(rhs)
        return False
    if isinstance(cond, CondAnd):
        return all(evaluate(c, text=text, facts=facts, active_ids=active_ids, rng=rng) for c in cond.children) if cond.children else False
    if isinstance(cond, CondOr):
        return any(evaluate(c, text=text, facts=facts, active_ids=active_ids, rng=rng) for c in cond.children)
    if isinstance(cond, CondNot):
        return not evaluate(cond.child, text=text, facts=facts, active_ids=active_ids, rng=rng)
    return False


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def is_conditional(brain: Brain) -> bool:
    """A brain is conditional when it carries any activation keys or an
    advanced condition tree. Unconditional brains skip the engine entirely."""
    return bool(brain.keys) or brain.advanced is not None


def _brain_active(brain: Brain, search_text: str, facts: ChatFacts,
                  active_ids: set[str], rng: _random.Random | None) -> bool:
    if any_key_matches(search_text, brain.keys):
        return True
    if brain.advanced is not None and evaluate(
        brain.advanced, text=search_text, facts=facts, active_ids=active_ids, rng=rng,
    ):
        return True
    return False


def activate_brains(
    candidates: Sequence[Brain],
    *,
    base_text: str,
    facts: ChatFacts,
    rng: _random.Random | None = None,
) -> list[Brain]:
    """Run the activation engine over the candidate pool.

    Returns the activated brains in their original order. Cascade adds the
    content of any activated brain whose ``cascades`` flag is set to the
    search text for the next pass, until either a pass produces no new
    activations or ``MAX_CASCADE_PASSES`` is reached. Brains with
    ``blocks_recursion`` set can only be activated on the first pass, against
    the original ``base_text``.
    """
    facts.story_text = base_text  # tie the variable to the actual search text
    search_text = base_text
    active_ids: set[str] = set()
    activated_set: set[str] = set()

    for pass_idx in range(MAX_CASCADE_PASSES):
        changed = False
        for brain in candidates:
            if brain.id in activated_set:
                continue
            # blocks_recursion brains never see the cascade-augmented text:
            # only the original base_text. Pinning them to pass 0 alone would
            # be redundant — the test below produces the same outcome — but
            # it short-circuits the work.
            if brain.blocks_recursion:
                if pass_idx > 0:
                    continue
                text_for_brain = base_text
            else:
                text_for_brain = search_text
            if _brain_active(brain, text_for_brain, facts, active_ids, rng):
                activated_set.add(brain.id)
                active_ids.add(brain.id)
                if brain.cascades and brain.content:
                    search_text = f"{search_text}\n{brain.content}"
                changed = True
        if not changed:
            break

    return [b for b in candidates if b.id in activated_set]


# Re-exported for clarity to consumers that only want the public interface.
__all__ = [
    "REGEX_TIMEOUT_SECONDS",
    "MAX_PATTERN_LENGTH",
    "MAX_CASCADE_PASSES",
    "ChatFacts",
    "CompiledKey",
    "compile_key",
    "key_matches",
    "any_key_matches",
    "evaluate",
    "is_conditional",
    "activate_brains",
]
