"""Render chat-template-style prompts for GLM-4.6 with the AER patch.

The AER format deviates from the standard GLM-4.6 chat template: the
**initial** ``<|system|>`` marker is *not* followed by a newline. The full
GLM-4.6 template emits ``[gMASK]<sop><|system|>\\n{content}…``; we patch the
first ``<|system|>\\n`` to ``<|system|>`` so the content tokens follow the
marker directly.
"""
from __future__ import annotations

from server.aer.tokenizer import get_tokenizer


SYSTEM_NL = "<|system|>\n"
SYSTEM_NO_NL = "<|system|>"
GEN_SUFFIX = "<|assistant|>\n<think></think>"


def render(
    messages: list[dict],
    *,
    add_generation_prompt: bool = True,
    continue_mode: bool = False,
) -> str:
    """Render ``messages`` with the GLM-4.6 chat template + AER ``<|system|>`` patch.

    Thinking is suppressed: we render with ``enable_thinking=False`` so the
    generation prompt ends with ``<|assistant|>\\n<think></think>\\n`` (the
    model emits AER content directly after the trailing newline) and user
    messages carry a ``/nothink`` marker the model is trained to recognise.

    ``messages`` is a list of ``{"role": "system" | "user" | "assistant", "content": str}``.

    ``continue_mode``: when True, do NOT open a new assistant turn — the last
    assistant message is left open and the prompt ends with a trailing
    ``\\n`` so the next token starts a new bubble. ``add_generation_prompt`` is
    ignored in this mode.
    """
    tok = get_tokenizer()
    # Defensive: strip whitespace from every message body. For user messages
    # this keeps the GLM-4.6 template's "/nothink" suffix flush against the
    # content (otherwise content's trailing \n leaves a blank line). For
    # system messages it ensures the system prompt doesn't end on whitespace.
    # For assistant messages it's a no-op against the chat template (which
    # already strips assistant content) but matches the AER format contract.
    prepared = [{**m, "content": m["content"].strip()} for m in messages]
    text = tok.apply_chat_template(
        prepared,
        tokenize=False,
        add_generation_prompt=False if continue_mode else add_generation_prompt,
        enable_thinking=False,
    )
    # AER patch: strip the \n that the GLM-4.6 template puts right after the
    # initial <|system|> marker. Only the FIRST occurrence is patched —
    # subsequent system messages keep their newline.
    idx = text.find(SYSTEM_NL)
    if idx != -1:
        text = text[: idx + len(SYSTEM_NO_NL)] + text[idx + len(SYSTEM_NL):]
    # Generation-prompt patch: the GLM-4.6 template emits the suffix as
    # ``<|assistant|>\n<think></think>`` with no trailing \n, but existing
    # assistant turns are formatted ``<|assistant|>\n<think></think>\n{content}``.
    # Without the \n the model's first generated token has to be \n, so we
    # add it here so the first token is real content.
    if add_generation_prompt and not continue_mode and text.endswith(GEN_SUFFIX):
        text += "\n"
    # Continue mode: the chat template strips trailing whitespace from
    # assistant content, so the rendered string ends on ``  Emotion: <name>``
    # with no newline. Restore the newline so the model sees an
    # emotion-line-terminated turn and emits ``ContactName: …`` next.
    if continue_mode:
        text += "\n"
    return text


def count_tokens(text: str) -> int:
    """Return the number of tokens in ``text`` (no special tokens added — already in template)."""
    tok = get_tokenizer()
    return len(tok.encode(text, add_special_tokens=False))


def render_and_count(
    messages: list[dict], *, add_generation_prompt: bool = True
) -> tuple[str, int]:
    text = render(messages, add_generation_prompt=add_generation_prompt)
    return text, count_tokens(text)
