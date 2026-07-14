"""Tests for the streaming bubble parser."""
from __future__ import annotations

import random

from server.aer.parser import StreamingBubbleParser
from server.models import Emotion


def _feed_in_chunks(parser: StreamingBubbleParser, text: str, sizes: list[int]) -> list:
    out = []
    i = 0
    for size in sizes:
        out.extend(parser.feed(text[i : i + size]))
        i += size
    out.extend(parser.feed(text[i:]))
    out.extend(parser.end())
    return out


SAMPLE_RESPONSE = (
    "Alice: Hey Bob!\n"
    "    Come on in.\n"
    "  Emotion: happy\n"
    "Alice: I just made tea.\n"
    "  Emotion: excited\n"
)


def test_parser_single_chunk():
    p = StreamingBubbleParser("Alice")
    bubbles = p.feed(SAMPLE_RESPONSE) + p.end()
    assert len(bubbles) == 2
    assert bubbles[0].text == "Hey Bob!\nCome on in."
    assert bubbles[0].emotion == Emotion.HAPPY
    assert bubbles[1].text == "I just made tea."
    assert bubbles[1].emotion == Emotion.EXCITED


def test_parser_chunked_random():
    rng = random.Random(42)
    for _ in range(20):
        sizes = []
        remaining = len(SAMPLE_RESPONSE)
        while remaining > 0:
            n = rng.randint(1, 7)
            sizes.append(min(n, remaining))
            remaining -= sizes[-1]
        p = StreamingBubbleParser("Alice")
        bubbles = _feed_in_chunks(p, SAMPLE_RESPONSE, sizes[:-1])
        assert len(bubbles) == 2
        assert bubbles[0].emotion == Emotion.HAPPY
        assert bubbles[1].emotion == Emotion.EXCITED


def test_parser_handles_missing_trailing_newline_at_end():
    text = "Alice: Hi.\n  Emotion: happy"  # no trailing \n
    p = StreamingBubbleParser("Alice")
    bubbles = p.feed(text) + p.end()
    assert len(bubbles) == 1
    assert bubbles[0].text == "Hi."
    assert bubbles[0].emotion == Emotion.HAPPY


def test_parser_unknown_emotion_falls_back_to_neutral():
    text = "Alice: Hi.\n  Emotion: smitten\n"
    p = StreamingBubbleParser("Alice")
    bubbles = p.feed(text) + p.end()
    assert len(bubbles) == 1
    assert bubbles[0].emotion == Emotion.NEUTRAL


def test_parser_flags_format_error_for_garbage():
    """If the LLM emits non-AER text, no bubble is produced and format_error flips on."""
    text = "Just some plain text without name prefix or emotion."
    p = StreamingBubbleParser("Alice")
    bubbles = p.feed(text) + p.end()
    assert bubbles == []
    assert p.format_error is True
    assert p.proper_count == 0


def test_parser_emits_first_bubble_before_second_completes():
    p = StreamingBubbleParser("Alice")
    # First bubble + start of second.
    out = p.feed("Alice: Hello there.\n  Emotion: happy\nAlice: Wait a")
    assert len(out) == 1
    assert out[0].emotion == Emotion.HAPPY
    out2 = p.feed(" sec.\n  Emotion: thinking\n")
    assert len(out2) == 1
    assert out2[0].emotion == Emotion.THINKING
    assert p.end() == []


def test_parser_handles_leading_whitespace_between_bubbles():
    p = StreamingBubbleParser("Alice")
    text = "Alice: Hi.\n  Emotion: happy\n\nAlice: Bye.\n  Emotion: sad\n"
    bubbles = p.feed(text) + p.end()
    assert len(bubbles) == 2
    assert bubbles[0].emotion == Emotion.HAPPY
    assert bubbles[1].emotion == Emotion.SAD


def test_parser_strips_leading_thinking_block():
    """GLM-4.6 framing prepends \\n<think></think>\\n to assistant output."""
    p = StreamingBubbleParser("Alice")
    text = (
        "\n<think></think>\n"
        "Alice: Hello!\n"
        "  Emotion: happy\n"
    )
    bubbles = p.feed(text) + p.end()
    assert len(bubbles) == 1
    assert bubbles[0].text == "Hello!"
    assert bubbles[0].emotion == Emotion.HAPPY


def test_parser_strips_thinking_with_content():
    p = StreamingBubbleParser("Alice")
    text = (
        "<think>\nLet me think about this.\nWhat to say?\n</think>\n"
        "Alice: Got it.\n"
        "  Emotion: thinking\n"
    )
    bubbles = p.feed(text) + p.end()
    assert len(bubbles) == 1
    assert bubbles[0].text == "Got it."
    assert bubbles[0].emotion == Emotion.THINKING


def test_parser_waits_for_thinking_close_tag():
    """If <think> arrives but </think> hasn't, no bubbles emit yet."""
    p = StreamingBubbleParser("Alice")
    out = p.feed("<think>partial reasoning")
    assert out == []
    out = p.feed(" still going")
    assert out == []
    out = p.feed("</think>\nAlice: Done.\n  Emotion: happy\n")
    assert len(out) == 1
    assert out[0].text == "Done."
    assert out[0].emotion == Emotion.HAPPY


def test_parser_accepts_unindented_blank_continuation_line():
    """``\\n\\n`` in the body is treated as if it were ``\\n    \\n`` — the
    formatter emits the latter, but the model sometimes drops the indent
    on blank lines and we accept that."""
    p = StreamingBubbleParser("Alice")
    text = (
        "Alice: First\n"
        "    Second\n"
        "\n"
        "    Third\n"
        "  Emotion: happy\n"
    )
    bubbles = p.feed(text) + p.end()
    assert len(bubbles) == 1
    assert bubbles[0].text == "First\nSecond\n\nThird"
    assert bubbles[0].emotion == Emotion.HAPPY


def test_parser_blank_continuation_line_at_end_of_stream():
    """Same relaxation applies to the end-of-stream regex."""
    p = StreamingBubbleParser("Alice")
    # No trailing newline after the emotion word.
    text = (
        "Alice: First\n"
        "\n"
        "    Second\n"
        "  Emotion: thinking"
    )
    bubbles = p.feed(text) + p.end()
    assert len(bubbles) == 1
    assert bubbles[0].text == "First\n\nSecond"
    assert bubbles[0].emotion == Emotion.THINKING
