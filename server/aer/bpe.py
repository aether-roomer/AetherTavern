"""Pure-Python byte-level BPE encoder for HuggingFace ``tokenizer.json`` files.

Covers the subset of the ``tokenizers`` spec that GLM-4.6 uses — no
normalizer, a ``Split``+``ByteLevel`` pre-tokenizer sequence, and a plain
BPE model with ``ignore_merges``. Anything outside that subset raises
:class:`UnsupportedTokenizer` rather than silently producing wrong counts:
a mis-tokenizing context accountant corrupts rollover decisions in ways
that are very hard to notice.

Token ids are byte-exact against ``tokenizers``' Rust implementation —
:mod:`tests.test_bpe` pins that against ``transformers`` when it is
installed.

Two things keep this fast enough to run per-message on a 36k-token prompt:

* ``ignore_merges`` — a pre-token that is already a vocab entry becomes one
  id with a single dict lookup, no merge loop. That is the overwhelming
  majority of Latin-script text.
* the merge loop itself is heap-driven (``_merge``), so a long unbroken
  run of CJK — which has no vocab shortcut and pre-tokenizes into one
  huge ``\\p{L}+`` piece — costs O(n log n) instead of O(n²). The naive
  loop takes 2 minutes on a 20k-character run; this takes 40 ms.
"""
from __future__ import annotations

import heapq

import regex


class UnsupportedTokenizer(Exception):
    """``tokenizer.json`` uses a feature this encoder does not implement."""


def _bytes_to_unicode() -> list[str]:
    """GPT-2's reversible byte↔codepoint map, as a 256-entry lookup list."""
    bs = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("\xa1"), ord("\xac") + 1))
        + list(range(ord("\xae"), ord("\xff") + 1))
    )
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    table = [""] * 256
    for b, c in zip(bs, cs):
        table[b] = chr(c)
    return table


# Pre-token → ids results are memoised. Chats re-tokenize the same text on
# every turn (rollover counts each message, then the whole prompt), so the
# hit rate is high; the cap only exists so a long session can't grow the
# dict without bound.
_CACHE_LIMIT = 262_144


class ByteLevelBPE:
    """Encoder built from a parsed ``tokenizer.json`` document."""

    def __init__(self, spec: dict) -> None:
        if spec.get("normalizer") is not None:
            raise UnsupportedTokenizer(
                f"normalizer {spec['normalizer'].get('type')!r} is not implemented"
            )
        self._load_model(spec.get("model") or {})
        self._load_pre_tokenizer(spec.get("pre_tokenizer") or {})
        self._load_added_tokens(spec.get("added_tokens") or [])
        self._b2u = _bytes_to_unicode()
        self._cache: dict[str, list[int]] = {}

    # -- spec parsing -------------------------------------------------------

    def _load_model(self, model: dict) -> None:
        if model.get("type") != "BPE":
            raise UnsupportedTokenizer(
                f"model type {model.get('type')!r} is not BPE"
            )
        for field in ("dropout", "unk_token", "continuing_subword_prefix",
                      "end_of_word_suffix"):
            if model.get(field) is not None:
                raise UnsupportedTokenizer(f"model.{field} is not implemented")
        if model.get("byte_fallback"):
            raise UnsupportedTokenizer("model.byte_fallback is not implemented")

        self.vocab: dict[str, int] = model["vocab"]
        self.ignore_merges: bool = bool(model.get("ignore_merges"))

        # Merge halves are all vocab entries. Reusing the vocab's string
        # objects instead of the ones json just minted drops ~30 MB of
        # duplicate strings off a 151k-token vocab.
        pool = {k: k for k in self.vocab}
        ranks: dict[tuple[str, str], int] = {}
        for i, merge in enumerate(model["merges"]):
            # Older tokenizers.json spellings store a merge as "a b".
            a, b = merge.split(" ", 1) if isinstance(merge, str) else merge
            ranks[(pool.get(a, a), pool.get(b, b))] = i
        self.ranks = ranks

    def _load_pre_tokenizer(self, pre: dict) -> None:
        stages = (
            pre["pretokenizers"] if pre.get("type") == "Sequence" else [pre]
        )
        pattern: str | None = None
        for stage in stages:
            kind = stage.get("type")
            if kind == "Split":
                if stage.get("invert"):
                    raise UnsupportedTokenizer("inverted Split is not implemented")
                if stage.get("behavior") != "Isolated":
                    raise UnsupportedTokenizer(
                        f"Split behavior {stage.get('behavior')!r} is not implemented"
                    )
                regex_src = (stage.get("pattern") or {}).get("Regex")
                if regex_src is None:
                    raise UnsupportedTokenizer("Split needs a Regex pattern")
                if pattern is not None:
                    raise UnsupportedTokenizer("multiple Split stages")
                pattern = regex_src
            elif kind == "ByteLevel":
                if stage.get("add_prefix_space"):
                    raise UnsupportedTokenizer(
                        "ByteLevel add_prefix_space is not implemented"
                    )
                if stage.get("use_regex"):
                    raise UnsupportedTokenizer(
                        "ByteLevel use_regex is not implemented; expected an "
                        "explicit Split stage"
                    )
            else:
                raise UnsupportedTokenizer(
                    f"pre-tokenizer stage {kind!r} is not implemented"
                )
        if pattern is None:
            raise UnsupportedTokenizer("no Split stage in the pre-tokenizer")
        self._pre_re = regex.compile(pattern)

    def _load_added_tokens(self, added: list[dict]) -> None:
        self.added: dict[str, int] = {}
        for tok in added:
            if tok.get("lstrip") or tok.get("rstrip") or tok.get("single_word"):
                raise UnsupportedTokenizer(
                    f"added token {tok.get('content')!r} uses lstrip/rstrip/"
                    f"single_word matching, which is not implemented"
                )
            self.added[tok["content"]] = tok["id"]
        # Longest-first so a token that prefixes another can't shadow it.
        self._added_re = (
            regex.compile(
                "|".join(
                    regex.escape(c)
                    for c in sorted(self.added, key=len, reverse=True)
                )
            )
            if self.added
            else None
        )

    # -- merging ------------------------------------------------------------

    def _merge(self, parts: list[str]) -> list[str]:
        """Apply merges lowest-rank-first, leftmost on ties.

        Symbols live in ``parts`` and are threaded by ``nxt``/``prv``; a
        merge folds the right symbol into the left slot and tombstones the
        right one. The heap holds candidate pairs by ``(rank, left_slot)``,
        which reproduces the leftmost-first tie-break. A popped candidate
        is stale whenever either side has since been merged, so the rank is
        re-checked against the slots' *current* contents — the liveness
        flags alone don't catch a left slot that grew via an earlier merge.
        """
        ranks = self.ranks
        n = len(parts)
        nxt = list(range(1, n + 1))
        prv = list(range(-1, n - 1))
        alive = [True] * n
        heap = [
            (rank, i, i + 1)
            for i in range(n - 1)
            if (rank := ranks.get((parts[i], parts[i + 1]))) is not None
        ]
        heapq.heapify(heap)
        while heap:
            rank, i, j = heapq.heappop(heap)
            if not alive[i] or j >= n or not alive[j] or nxt[i] != j:
                continue
            if ranks.get((parts[i], parts[j])) != rank:
                continue
            parts[i] += parts[j]
            alive[j] = False
            right = nxt[j]
            nxt[i] = right
            if right < n:
                prv[right] = i
                new_rank = ranks.get((parts[i], parts[right]))
                if new_rank is not None:
                    heapq.heappush(heap, (new_rank, i, right))
            left = prv[i]
            if left >= 0:
                new_rank = ranks.get((parts[left], parts[i]))
                if new_rank is not None:
                    heapq.heappush(heap, (new_rank, left, i))
        return [parts[i] for i in range(n) if alive[i]]

    def _encode_piece(self, piece: str) -> list[int]:
        b2u = self._b2u
        mapped = "".join([b2u[b] for b in piece.encode()])
        vocab = self.vocab
        if self.ignore_merges:
            token_id = vocab.get(mapped)
            if token_id is not None:
                return [token_id]
        return [vocab[part] for part in self._merge(list(mapped))]

    def _ids_for(self, piece: str) -> list[int]:
        ids = self._cache.get(piece)
        if ids is None:
            if len(self._cache) >= _CACHE_LIMIT:
                self._cache.clear()
            ids = self._cache[piece] = self._encode_piece(piece)
        return ids

    # -- encoding -----------------------------------------------------------

    def _pieces(self, text: str):
        """Yield pre-tokens. Text between matches is isolated as its own
        piece, matching ``Split(behavior="Isolated")``."""
        pos = 0
        for match in self._pre_re.finditer(text):
            start, end = match.span()
            if start > pos:
                yield text[pos:start]
            yield match.group()
            pos = end
        if pos < len(text):
            yield text[pos:]

    def encode(self, text: str) -> list[int]:
        """Token ids for ``text``. Added tokens are matched before splitting."""
        out: list[int] = []
        for chunk, token_id in self._split_added(text):
            if token_id is not None:
                out.append(token_id)
                continue
            for piece in self._pieces(chunk):
                out.extend(self._ids_for(piece))
        return out

    def count(self, text: str) -> int:
        """Number of tokens in ``text``, without materialising the id list."""
        total = 0
        for chunk, token_id in self._split_added(text):
            if token_id is not None:
                total += 1
                continue
            for piece in self._pieces(chunk):
                total += len(self._ids_for(piece))
        return total

    def _split_added(self, text: str):
        """Yield ``(text, None)`` / ``("", added_token_id)`` in order."""
        if self._added_re is None:
            yield text, None
            return
        pos = 0
        for match in self._added_re.finditer(text):
            start, end = match.span()
            if start > pos:
                yield text[pos:start], None
            yield "", self.added[match.group()]
            pos = end
        if pos < len(text):
            yield text[pos:], None
