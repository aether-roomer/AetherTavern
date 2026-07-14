"""GLM-4.6 + AER chat-template renderer.

The ``render`` function in ``server.aer.template`` produces the string
that gets sent to the inference endpoint. It carries two AER patches on
top of the stock GLM template: strip the ``\\n`` after the initial
``<|system|>`` marker, and append a trailing ``\\n`` to the generation
prompt suffix so the model's first token is real content.

``continue_mode`` is the third behavior: render WITHOUT opening a new
assistant turn so the prompt ends mid-stream on the last bubble's
``  Emotion: …\\n`` line, ready for the model to emit another bubble.
The existing ``<|assistant|>\\n<think></think>`` marker sits at the
START of the message being continued — not at the end of the prompt.
"""
from __future__ import annotations

import pytest

from server.aer.template import GEN_SUFFIX, render


_SYS = "You are a helpful assistant."
_USER = "Hi there."
_ASSISTANT = (
    "Alice: Hello!\n  Emotion: happy\n"
    "Alice: How are you?\n  Emotion: curious\n"
)


def _basic_messages():
    return [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": _USER},
        {"role": "assistant", "content": _ASSISTANT},
    ]


def test_render_with_generation_prompt_ends_on_gen_suffix_plus_newline():
    """Normal generation: trailing ``<|assistant|>\\n<think></think>\\n``
    so the model's first emitted token is real content."""
    out = render(_basic_messages(), add_generation_prompt=True)
    assert out.endswith(GEN_SUFFIX + "\n"), repr(out[-80:])


def test_render_continue_mode_omits_gen_suffix():
    """Continue mode renders WITHOUT a new assistant turn opener. The
    last assistant turn stays open; the model continues into it."""
    out = render(_basic_messages(), continue_mode=True)
    assert not out.endswith(GEN_SUFFIX + "\n"), repr(out[-80:])
    assert not out.rstrip().endswith(GEN_SUFFIX), repr(out[-80:])


def test_render_continue_mode_ends_on_emotion_line():
    """Continue mode's tail is the LAST bubble's ``  Emotion: <name>\\n``
    line. The model continues by emitting the next ``ContactName: …``
    bubble in the same format."""
    out = render(_basic_messages(), continue_mode=True)
    assert out.endswith("  Emotion: curious\n"), repr(out[-80:])


def test_render_continue_mode_keeps_assistant_marker_inside_turn():
    """The ``<|assistant|>\\n<think></think>`` marker still appears in the
    rendered string — but as the OPENER of the existing assistant turn
    that's being continued, not as a trailing generation prompt."""
    out = render(_basic_messages(), continue_mode=True)
    # Marker is present once (opening the assistant turn).
    assert GEN_SUFFIX in out
    # And it's followed by the assistant content (not by the end of string).
    after_marker = out.split(GEN_SUFFIX, 1)[1]
    assert "Alice:" in after_marker
    assert after_marker.endswith("  Emotion: curious\n")
