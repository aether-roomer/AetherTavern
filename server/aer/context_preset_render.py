"""Generic-mode context-preset rendering.

Two render entry points consumed by the preview route and the Generic
context builder:

- :func:`render_system_prompt` joins enabled blocks with ``\\n``, dropping
  blocks whose macro expansion is empty after strip. The kept block's
  un-stripped expansion is appended so a leading ``\\n`` on a block
  introduces the blank line between sections.
- :func:`render_additional_messages` returns ``[{role, content, float_enabled,
  float_depth}]``. Disabled or empty-after-strip messages are dropped.

Both use :func:`server.aer.macros.expand` directly so the caller's
``MacroCtx`` (with libraries, chat, contact, etc.) is honoured byte-for-byte.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from server.aer.macros import MacroCtx, expand

if TYPE_CHECKING:
    from server.models import (
        ContextPreset,
        ContextPresetAdditionalMessage,
        ContextPresetBlock,
    )


def _render_blocks(blocks: "list[ContextPresetBlock]", ctx: MacroCtx) -> str:
    """Same join idiom as :func:`render_system_prompt` — factored so a
    blocks-mode additional message body shares one implementation."""
    parts: list[str] = []
    for block in blocks:
        if not block.enabled:
            continue
        expanded = expand(block.content, ctx)
        if expanded.strip():
            # Append un-stripped — leading ``\n`` on a block is load-bearing
            # for inter-block blank lines.
            parts.append(expanded)
    return "\n".join(parts)


def render_system_prompt(preset: "ContextPreset", ctx: MacroCtx) -> str:
    """Render ``preset.system_prompt_blocks`` to the system message body.

    Blocks where ``enabled is False`` are skipped. Blocks whose macro
    expansion is empty after strip are dropped (so wrapping an entire
    section in ``{{#if X}}…{{/if}}`` makes it vanish cleanly). Kept blocks'
    un-stripped expansion is joined with a single ``\\n``.
    """
    return _render_blocks(list(preset.system_prompt_blocks), ctx)


def render_additional_messages(
    preset: "ContextPreset", ctx: MacroCtx,
) -> list[dict]:
    """Render ``preset.additional_messages`` to per-message dicts.

    Each output dict has ``role`` / ``content`` / ``float_enabled`` /
    ``float_depth``. Disabled messages are skipped. Messages whose rendered
    content strips to empty are dropped (so ``{{if X}}…{{/if}}``-gated
    messages disappear cleanly when X is falsy).

    Floating-message ordering is the caller's responsibility — the Generic
    context builder consumes ``float_enabled`` / ``float_depth`` to splice
    floats at ``len(api_messages) - depth`` at insertion time.
    """
    out: list[dict] = []
    for msg in preset.additional_messages:
        if not msg.enabled:
            continue
        if msg.mode == "simple":
            content = expand(msg.simple_content, ctx)
        else:
            content = _render_blocks(list(msg.blocks), ctx)
        if not content.strip():
            continue
        out.append({
            # ``id`` lets the editor's per-message preview panel match
            # this rendered output back to its source card without
            # having to re-index by position (disabled / empty msgs
            # are filtered out, so positional zip breaks).
            "id": msg.id,
            "role": msg.role,
            "content": content,
            "float_enabled": msg.float_enabled,
            "float_depth": msg.float_depth,
        })
    return out
