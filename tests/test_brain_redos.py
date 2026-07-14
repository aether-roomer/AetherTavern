"""ReDoS bounds: adversarial regex patterns must complete within the per-match
timeout instead of hanging the request thread."""
from __future__ import annotations

import time

import pytest

from server.aer.activation import (
    REGEX_TIMEOUT_SECONDS,
    activate_brains,
    compile_key,
    key_matches,
    ChatFacts,
)
from server.importers import _import_brains
from server.models import Brain, BrainKey


def _facts():
    return ChatFacts()


@pytest.mark.parametrize(
    "pattern,target",
    [
        (r"(a+)+$", "a" * 30 + "b"),
        (r"(a|aa)+$", "a" * 30 + "b"),
    ],
)
def test_pathological_regex_bounded_latency(pattern, target):
    """Adversarial regex patterns that would catastrophically backtrack on the
    given inputs must abort within the per-match deadline rather than hanging.

    Only the timing matters here — the false-vs-true match result depends on
    the specific pattern + input pair and isn't what we're testing.
    """
    key = BrainKey(pattern=pattern, is_regex=True)
    ck = compile_key(key)
    t0 = time.perf_counter()
    key_matches(target, ck)
    elapsed = time.perf_counter() - t0
    assert elapsed < REGEX_TIMEOUT_SECONDS * 100, (
        f"pattern {pattern!r} ran {elapsed*1000:.2f} ms (cap {REGEX_TIMEOUT_SECONDS*1000:.2f} ms)"
    )


def test_pathological_regex_across_full_activation():
    """Activation engine survives multiple pathological keys."""
    brains = [
        Brain(
            id=f"b{i}", name=f"B{i}", content="",
            keys=[BrainKey(pattern=r"(a+)+$", is_regex=True)],
        )
        for i in range(10)
    ]
    target = "a" * 50 + "b"
    t0 = time.perf_counter()
    out = activate_brains(brains, base_text=target, facts=_facts())
    elapsed = time.perf_counter() - t0
    assert out == []
    # 10 brains × 1 key × ~5 ms cap = ~50 ms worst case; allow generous slack.
    assert elapsed < 1.0, f"activation pass took {elapsed:.3f}s"


def test_import_preserves_brain_when_regex_is_pathological():
    """A bad-but-valid regex still imports — it just never matches."""
    raw = [
        {"name": "Pathological", "content": "lorebook entry",
         "keys": [{"pattern": r"(a+)+$", "is_regex": True}]},
    ]
    out = _import_brains(raw)
    assert len(out) == 1
    assert out[0].name == "Pathological"
    assert out[0].keys[0].is_regex is True


def test_import_drops_invalid_optional_fields_keeps_brain():
    """Unparseable advanced-condition data shouldn't break the import."""
    raw = [
        {"name": "X", "content": "C",
         "advanced": "this is not a condition dict at all"},
    ]
    out = _import_brains(raw)
    assert len(out) == 1
    assert out[0].name == "X"
    assert out[0].advanced is None
