"""Incremental parser for LLM responses in the AER format.

The streaming inference route feeds text deltas to a parser instance and gets
back complete bubbles as they finish — so the UI can pop them in one at a
time, with the emotion known.

Boundary detection: a bubble is complete when ``  Emotion: word\\n`` lands in
the buffer (or, on stream end, an emotion word with optional trailing newline).
"""
from __future__ import annotations

import re

from server.models import EMOTION_VALUES, Emotion, SubMessage


class StreamingBubbleParser:
    """Stateful parser that yields ``SubMessage`` bubbles as they complete."""

    # Strip a leading <think>...</think> block (GLM-4.6 framing). Non-greedy.
    _LEADING_THINK_RE = re.compile(r"\A\s*(?:<think>[\s\S]*?</think>\s*)+")

    def __init__(self, char_name: str) -> None:
        self.char_name = char_name
        # ``True`` if stream ended with garbage that didn't parse as a bubble.
        self.format_error: bool = False
        # Number of properly-parsed bubbles (mid_re or end_re match).
        self.proper_count: int = 0
        name = re.escape(char_name)
        # A bubble that is definitely complete (trailing \n after Emotion line).
        # Continuation lines are 4-space-indented; a fully empty line ``\n`` is
        # accepted as if it were ``    \n`` (the formatter emits the latter, but
        # the model sometimes drops the indent on blank lines).
        self._mid_re = re.compile(
            rf"\A[ \n]*"
            rf"{name}: (?P<line0>[^\n]*)\n"
            rf"(?P<conts>(?:    [^\n]*\n|\n)*)"
            rf"  Emotion: (?P<emotion>\w+)\n",
        )
        # A bubble at end-of-stream (trailing \n optional).
        self._end_re = re.compile(
            rf"\A[ \n]*"
            rf"{name}: (?P<line0>[^\n]*)\n"
            rf"(?P<conts>(?:    [^\n]*\n|\n)*)"
            rf"  Emotion: (?P<emotion>\w+)\n?\Z",
        )
        self._buffer: str = ""
        self._closed: bool = False

    @property
    def buffer(self) -> str:
        """Raw text received so far (useful for rendering a typing indicator)."""
        return self._buffer

    def feed(self, delta: str) -> list[SubMessage]:
        """Append ``delta`` to the buffer and return any bubbles that completed."""
        if self._closed or not delta:
            return []
        self._buffer += delta
        return self._drain_mid()

    def end(self) -> list[SubMessage]:
        """Mark stream end. Drain any remaining bubble; flag malformed residuals."""
        if self._closed:
            return []
        out = self._drain_mid()
        self._strip_leading_think()
        residual = self._buffer.strip()
        if residual:
            m = self._end_re.match(self._buffer)
            if m is not None:
                out.append(self._make_bubble(m))
                self.proper_count += 1
                self._buffer = self._buffer[m.end():]
            else:
                # Malformed residual — set the flag and discard the text. The
                # caller decides how to surface this to the user.
                self.format_error = True
                self._buffer = ""
        self._closed = True
        return out

    # ---- internals -----------------------------------------------------

    def _strip_leading_think(self) -> None:
        """Drop any complete leading ``<think>...</think>`` blocks the model emitted."""
        m = self._LEADING_THINK_RE.match(self._buffer)
        if m is not None:
            self._buffer = self._buffer[m.end():]

    def _drain_mid(self) -> list[SubMessage]:
        self._strip_leading_think()
        out: list[SubMessage] = []
        while True:
            m = self._mid_re.match(self._buffer)
            if m is None:
                break
            out.append(self._make_bubble(m))
            self.proper_count += 1
            self._buffer = self._buffer[m.end():]
        return out

    def _make_bubble(self, m: re.Match[str]) -> SubMessage:
        line0 = m.group("line0")
        conts = m.group("conts") or ""
        cont_lines = [line[4:] for line in conts.splitlines()]
        text = "\n".join([line0, *cont_lines])
        emotion_word = m.group("emotion").lower()
        emotion = (
            Emotion(emotion_word)
            if emotion_word in EMOTION_VALUES
            else Emotion.NEUTRAL
        )
        return SubMessage(text=text, emotion=emotion)
