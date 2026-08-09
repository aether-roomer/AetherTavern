"""Pure-Python BPE encoder: parity with the reference Rust tokenizer.

The parity tests need the ``tokenizer-parity`` dependency group
(``uv sync --group dev --group tokenizer-parity``) and are skipped without
it. The golden-value tests below run everywhere, so a plain ``dev`` install
still catches an encoder regression.
"""
from __future__ import annotations

import random
import string

import pytest

from server.aer.bpe import ByteLevelBPE, UnsupportedTokenizer
from server.aer.tokenizer import load_tokenizer


@pytest.fixture(scope="module")
def tok():
    return load_tokenizer()


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

_FIXED = [
    "",
    " ",
    "\n",
    "Hello, world! This is a test of the tokenizer.",
    "don't can't it's I've we'll they'd DON'T CAN'T IT'S I'VE",
    "   leading spaces\n\n\n\ttabs\r\nCRLF\r  trailing   ",
    "*italic* **bold** _under_ <tag attr=\"v\"> &amp; </tag>",
    "```python\ndef f(x):\n    return x**2  # comment\n```",
    "12345678901234567890 3.14159 0x1F -42",
    # CJK: the pathological shape for a naive merge loop — one huge
    # \p{L}+ pre-token with no vocab shortcut.
    "こんにちは世界、これはトークナイザーのテストです。日本語のテキスト。",
    "中文测试：这是一个分词器测试。数字 12345678901234 和符号 !@#$%^&*()",
    "あ" * 4000,
    "Ελληνικά, Русский, العربية, עברית, हिन्दी, ไทย, 한국어",
    "emoji: \U0001f600\U0001f389 combining: é à̂",
    "　 ​   exotic whitespace",
    # Rust's \s is \p{White_Space}; Python's regex module also counts the C0
    # separators. The pre-tokenizer pattern leans on \s, so pin the
    # codepoints where the two implementations could disagree.
    "a\x1cb\x1dc\x1ed\x1fe\x0bf\x0cg h",
    "".join(chr(i) for i in range(1, 0x500)),
    # The rendered-prompt shape, with special tokens inline.
    "[gMASK]<sop><|system|>You are helpful.<|user|>\nhi/nothink"
    "<|assistant|>\n<think></think>\nHello!",
    "<think>reasoning</think>answer",
]


def _random_corpus(n=300):
    rnd = random.Random(20260809)
    alphabet = (
        string.printable
        + "äöüßéèñ"
        + "中文日本語한국어"
        + "\U0001f600  ​　"
    )
    out = []
    for _ in range(n):
        size = rnd.randint(1, 400)
        out.append("".join(rnd.choice(alphabet) for _ in range(size)))
    for _ in range(n // 2):
        size = rnd.randint(1, 200)
        # Whole BMP minus the surrogate block, which is not encodable text.
        chars = []
        while len(chars) < size:
            cp = rnd.randint(1, 0xFFFF)
            if 0xD800 <= cp <= 0xDFFF:
                continue
            chars.append(chr(cp))
        out.append("".join(chars))
    return out


# ---------------------------------------------------------------------------
# Golden values — no reference implementation needed
# ---------------------------------------------------------------------------

# Pinned against tokenizers 0.22.2 / GLM-4.6 revision be721948.
_GOLDEN = {
    "Hello, world!": [9703, 11, 1879, 0],
    " hello": [23745],
    "hello": [14978],
    "\n\n": [271],
    "[gMASK]<sop>": [151331, 151333],
    "<|system|>": [151335],
    "<|assistant|>\n<think></think>\n": [151337, 198, 151350, 151351, 198],
    "あ": [29380],
    "日本語": [99799, 131513],
}


def test_golden_encodings(tok):
    for text, expected in _GOLDEN.items():
        assert tok.encode(text) == expected, text


def test_count_matches_encode(tok):
    for text in _FIXED:
        assert tok.count(text) == len(tok.encode(text)), repr(text[:60])


def test_cache_is_stable_across_calls(tok):
    """A second pass must not be perturbed by the pre-token cache."""
    for text in _FIXED:
        assert tok.encode(text) == tok.encode(text)


def test_added_tokens_are_atomic(tok):
    """Special markers encode as exactly one id wherever they appear."""
    for marker in ("<|system|>", "<|user|>", "<|assistant|>", "[gMASK]", "<sop>"):
        assert len(tok.encode(marker)) == 1
        wrapped = tok.encode(f"x{marker}y")
        assert len(wrapped) == 3
        assert wrapped[1] == tok.encode(marker)[0]


def test_long_cjk_run_is_not_quadratic(tok):
    """A 20k-character unbroken run pre-tokenizes into one huge piece.

    Guards the heap merge: the naive loop takes ~2 minutes here.
    """
    import time

    text = "あいうえお" * 4000
    start = time.perf_counter()
    ids = tok.encode(text)
    assert time.perf_counter() - start < 5.0
    assert ids


# ---------------------------------------------------------------------------
# Spec validation — wrong assumptions must fail loudly, not silently
# ---------------------------------------------------------------------------

def _minimal_spec(**overrides) -> dict:
    spec = {
        "normalizer": None,
        "pre_tokenizer": {
            "type": "Sequence",
            "pretokenizers": [
                {"type": "Split", "pattern": {"Regex": r"\s+|\S+"},
                 "behavior": "Isolated", "invert": False},
                {"type": "ByteLevel", "add_prefix_space": False, "use_regex": False},
            ],
        },
        "added_tokens": [],
        "model": {"type": "BPE", "vocab": {"a": 0}, "merges": [], "ignore_merges": True},
    }
    spec.update(overrides)
    return spec


@pytest.mark.parametrize("overrides", [
    {"normalizer": {"type": "NFKC"}},
    {"model": {"type": "Unigram", "vocab": {}, "merges": []}},
    {"model": {"type": "BPE", "vocab": {}, "merges": [], "byte_fallback": True}},
    {"model": {"type": "BPE", "vocab": {}, "merges": [], "unk_token": "<unk>"}},
    {"added_tokens": [{"content": "<x>", "id": 1, "lstrip": True}]},
])
def test_unsupported_features_raise(overrides):
    with pytest.raises(UnsupportedTokenizer):
        ByteLevelBPE(_minimal_spec(**overrides))


def test_bytelevel_use_regex_rejected():
    spec = _minimal_spec()
    spec["pre_tokenizer"]["pretokenizers"][1]["use_regex"] = True
    with pytest.raises(UnsupportedTokenizer):
        ByteLevelBPE(spec)


# ---------------------------------------------------------------------------
# Parity against the Rust tokenizer
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def reference():
    pytest.importorskip(
        "transformers",
        reason="install the `tokenizer-parity` dependency group to run parity tests",
    )
    from transformers import AutoTokenizer

    from server.aer.tokenizer import TOKENIZER_NAME

    return AutoTokenizer.from_pretrained(TOKENIZER_NAME)


def test_parity_fixed_corpus(tok, reference):
    for text in _FIXED:
        assert tok.encode(text) == reference.encode(text, add_special_tokens=False), (
            repr(text[:80])
        )


def test_parity_random_corpus(tok, reference):
    for text in _random_corpus():
        assert tok.encode(text) == reference.encode(text, add_special_tokens=False), (
            repr(text[:80])
        )


def test_parity_every_added_token(tok, reference):
    for content in tok.bpe.added:
        for text in (content, f"x{content}y", f" {content} "):
            assert tok.encode(text) == reference.encode(text, add_special_tokens=False)


def test_parity_chat_template(tok, reference):
    """Our Jinja environment must render byte-identically to
    ``transformers.apply_chat_template`` — the AER patch in
    ``template.py`` post-processes exact substrings of its output."""
    rnd = random.Random(3)
    bodies = [
        "Hello there.", "", "   ", "\n\n", "multi\nline\nbody",
        "trailing whitespace   \n", "  leading", "has <think>inner</think>",
        "has </think> only", "ends with /nothink", "Emotion: happy",
        "日本語", "**bold** and *italic*",
    ]
    roles = ["system", "user", "assistant"]
    cases = [
        [{"role": rnd.choice(roles), "content": rnd.choice(bodies)}
         for _ in range(rnd.randint(1, 8))]
        for _ in range(200)
    ]
    for messages in cases:
        for add_generation_prompt in (True, False):
            assert tok.render_chat(
                messages, add_generation_prompt=add_generation_prompt
            ) == reference.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=add_generation_prompt,
                enable_thinking=False,
            ), messages
