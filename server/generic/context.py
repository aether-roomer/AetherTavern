"""Generic-mode context builder.

Mirrors :func:`server.aer.rollover.build_messages_for_generation` for the
``/v1/chat/completions`` shape: a flat list of ``{role, content}`` dicts
assembled from a user-authored :class:`ContextPreset` instead of AER's
hardcoded template.

Token costing inside the builder uses a UTF-8 byte / 3.35 guesstimate
(SillyTavern's ``BYTES_PER_TOKEN``) — cheap, called dozens of times per
build, and good enough for ordering decisions in the rollover trim loop.
The total prompt count returned in :class:`GenericGenerationContext`
prefers (in order) the upstream tokenize endpoint when the phase-1 probe
found one, the carryover ``Chat.last_context_tokens`` plus a guesstimate
of newly-added content, and finally a pure guesstimate.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from server.aer.activation import is_conditional
from server.aer.format import (
    build_local_brain_block,
    collect_unconditional_global_brains,
)
from server.aer.macros import MacroCtx, expand
from server.aer.context_preset_render import (
    render_additional_messages,
    render_system_prompt,
)
from server.aer.rollover import (
    BrainBudgetExceeded,
    _brain_block_tokens,
    _is_brain,
    _is_history,
    _maybe_splice_conditional_block,
    _splice_reminder_brain,
)
from server.models import (
    Brain,
    BrainLibrary,
    Chat,
    ChatMessage,
    Contact,
    ContactScenario,
    ContextPreset,
    Preset,
    Scenario,
    Settings,
    User,
)

if TYPE_CHECKING:
    pass


log = logging.getLogger("aether.generic.context")


_BYTES_PER_TOKEN = 3.35


def guesstimate_tokens(s: str) -> int:
    """SillyTavern-style UTF-8 / 3.35 token guesstimate.

    Handles English (~3.35 chars/token) and CJK (~1 char/token via UTF-8's
    3-byte expansion) uniformly. Returns 0 for empty input.
    """
    if not s:
        return 0
    return max(1, math.ceil(len(s.encode("utf-8")) / _BYTES_PER_TOKEN))


def _solo_tokens_generic(msg: dict, is_first: bool = False) -> int:
    """Per-message token guesstimate for Generic-mode prompts.

    Generic providers don't use AER's GLM-4.6 framing tokens; the
    contribution of each ``{role, content}`` to the upstream's token count
    is dominated by the content bytes themselves plus a small per-message
    overhead. Tally just the content here — close enough for ordering
    decisions inside the trim loop, and the post-generation
    ``usage.prompt_tokens`` from the upstream becomes the authoritative
    count once it arrives.

    Multimodal content arrives as ``list[dict]`` (``{type:"text",...}`` +
    ``{type:"image_url",...}``). Text parts feed the guesstimator; image
    parts get a flat 1536-token overestimate (above OpenAI's
    medium-detail formula) so the rollover trim has headroom.
    """
    content = msg.get("content")
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += guesstimate_tokens(part.get("text") or "")
            elif part.get("type") == "image_url":
                total += 1536
        return total
    return guesstimate_tokens(content or "")


def _brain_block_tokens_generic(brain: Brain) -> int:
    """Token guesstimate for a single brain rendered as its block."""
    content = (brain.content or "").strip()
    return guesstimate_tokens(f"----\n{brain.name}\n{content}\n")


@dataclass
class GenericGenerationContext:
    """Output of :func:`build_messages_for_generic`."""

    api_messages: list[dict] = field(default_factory=list)
    total_tokens: int = 0
    history_tokens: int = 0
    system_tokens: int = 0
    new_cursor: int = 0
    new_path_ids: list[str] = field(default_factory=list)
    new_rolled_over: bool = False


def _resolve_provider_config(settings: Settings):
    """Return the provider config object for the currently-active provider.

    Returns ``None`` when ``provider_mode != "generic"`` or when the
    ``openai_compatible`` slot is selected without an ``active_id``.
    """
    if settings.provider_mode != "generic":
        return None
    g = settings.generic
    if g.provider == "novelai":
        return g.novelai
    if g.provider == "openrouter":
        return g.openrouter
    if g.provider == "nanogpt":
        return g.nanogpt
    if g.provider == "openai_compatible":
        if not g.openai_compatible.active_id:
            return None
        for entry in g.openai_compatible.custom_providers:
            if entry.id == g.openai_compatible.active_id:
                return entry
        return None
    return None


def _history_to_api_messages(
    path: list[ChatMessage],
    *,
    cursor: int,
    contact_name: str,
    user_name: str,
    prefix_names: bool,
    brain_role: str,
    flip_roles: bool = False,
    chat_id: str | None = None,
    compress_images: bool = False,
    image_quality: int = 85,
) -> tuple[list[dict], list[Brain]]:
    """Walk active-path messages from ``cursor`` onwards, emitting API messages.

    Per-message unconditional brains land inline before the owning message
    (same position AER puts them); conditional brains are accumulated for
    later activation.

    One ChatMessage → one OpenAI api_message. AER-origin multi-bubble
    messages collapse their bubbles into a single content string joined
    by ``\\n\\n`` (Generic mode has no bubble concept). Empty bubbles
    are stripped; messages whose bubbles all strip to empty are skipped
    entirely (better than emitting a literal ``{role, content: ""}``
    that some providers reject on consecutive same-role pairs).

    ``flip_roles`` is used by impersonate: contact-origin messages become
    ``role: "user"`` (named after the contact, since that's whom the
    flipped prompt addresses), and user-origin messages become
    ``role: "assistant"`` (named after the user, the persona being played).

    Returns ``(api_messages, conditional_msg_brains)``.
    """
    out: list[dict] = []
    conditional_msg_brains: list[Brain] = []
    for i, msg in enumerate(path):
        live = [b for b in (msg.brains or []) if not b.disabled]
        uncond = [b for b in live if not is_conditional(b)]
        cond = [b for b in live if is_conditional(b)]
        if uncond:
            out.append({
                "role": brain_role,
                "content": build_local_brain_block(uncond),
            })
        if cond:
            conditional_msg_brains.extend(cond)
        if i < cursor:
            continue
        if flip_roles:
            role = "user" if msg.sender == "contact" else "assistant"
        else:
            role = "assistant" if msg.sender == "contact" else "user"
        speaker_name = contact_name if msg.sender == "contact" else user_name

        # Merge bubbles → one content string. Empty-after-strip bubbles
        # drop out; if nothing survives, fall through to image-only
        # handling below (or skip the message entirely).
        bubble_texts = [(b.text or "").strip() for b in (msg.body or [])]
        bubble_texts = [t for t in bubble_texts if t]
        content_text = "\n\n".join(bubble_texts) if bubble_texts else ""
        if content_text and prefix_names and speaker_name:
            content_text = f"{speaker_name}:\n{content_text}"

        # Multimodal: user-side messages with image attachments produce
        # an OpenAI parts array (images first, then text). Image-first
        # matches the vision-API convention (image as context, then the
        # question / text). Skipped under ``flip_roles`` (impersonate):
        # provider support for multimodal assistant content is spotty.
        attachments = getattr(msg, "attachments", None) or []
        usable_atts = [a for a in attachments if a.mime.startswith("image/")] \
            if (not flip_roles and msg.sender == "user" and chat_id) else []
        if usable_atts:
            content_parts: list[dict] = []
            for att in usable_atts:
                data_url = _attachment_data_url(
                    chat_id, att,
                    compress=compress_images,
                    quality=image_quality,
                )
                if data_url:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    })
            if content_text:
                content_parts.append({"type": "text", "text": content_text})
            if content_parts:
                out.append({"role": role, "content": content_parts})
            continue

        if content_text:
            out.append({"role": role, "content": content_text})
        # else: message was empty after strip → skip silently rather
        # than emit a ghost ``{role, content: ""}``.
    return out, conditional_msg_brains


def _attachment_data_url(
    chat_id: str,
    att,
    *,
    compress: bool = False,
    quality: int = 85,
) -> str | None:
    """Encode a chat attachment file as a ``data:`` URL for multimodal use.

    With ``compress`` set, image attachments are re-encoded as JPEG at
    ``quality`` (memoised under ``attachments/.cached/``) and the
    smaller of {compressed, original} is sent — a simple palette PNG can
    beat its own JPEG, so the payload never grows. The original file on
    disk is left untouched either way.
    """
    import base64
    from server import storage as _storage
    d = _storage.chat_attachments_dir(chat_id)
    if d is None or not d.exists():
        return None
    original = next(
        (p for p in d.glob(f"{att.id}.*") if not p.name.endswith(".tmp")),
        None,
    )
    if original is None:
        return None
    try:
        payload = original.read_bytes()
    except OSError:
        return None
    mime = att.mime

    if compress and att.mime.startswith("image/"):
        compressed = _compressed_attachment_bytes(chat_id, att, payload, quality)
        if compressed is not None and len(compressed) < len(payload):
            payload, mime = compressed, "image/jpeg"

    b64 = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _compressed_attachment_bytes(
    chat_id: str, att, original: bytes, quality: int,
) -> bytes | None:
    """Return JPEG-compressed bytes for ``att``, memoised on disk.

    Cache path: ``attachments/.cached/{att_id}.q{quality}.jpg``. A
    quality change orphans the prior file, so we drop any other
    ``{att_id}.q*.jpg`` before writing — at most one cached copy per
    attachment survives. Returns ``None`` when Pillow can't encode the
    source, so the caller falls back to the original bytes.
    """
    from server import storage as _storage
    from server import imaging

    quality = max(1, min(100, int(quality)))
    cache_dir = _storage.chat_attachment_cache_dir(chat_id)
    cache_file = (
        cache_dir / f"{att.id}.q{quality}.jpg" if cache_dir is not None else None
    )

    if cache_file is not None and cache_file.exists():
        try:
            return cache_file.read_bytes()
        except OSError:
            pass

    try:
        compressed = imaging.build_compressed_jpeg_bytes(original, quality)
    except Exception:
        log.debug(
            "Could not compress attachment %s; sending original", att.id,
            exc_info=True,
        )
        return None

    if cache_file is not None:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            for stale in cache_dir.glob(f"{att.id}.q*.jpg"):
                if stale != cache_file:
                    stale.unlink()
            _storage.atomic_write_bytes(cache_file, compressed)
        except OSError:
            pass
    return compressed


def _splice_floats(
    api_messages: list[dict],
    floats: list[dict],
) -> None:
    """Splice ``float_enabled`` additional messages at ``len(api_messages) - depth``.

    Higher ``float_depth`` inserted first so lower-depth messages end up
    closer to the tail. ``depth=0`` is guaranteed to be the absolute
    final entry. Mutates ``api_messages`` in place.
    """
    # Stable-sort descending so we insert the deepest first and end with the
    # depth=0 entries at the absolute tail.
    floats_sorted = sorted(
        ((f["float_depth"], i, f) for i, f in enumerate(floats)),
        key=lambda t: (-t[0], t[1]),
    )
    for depth, _i, f in floats_sorted:
        insert_at = max(0, len(api_messages) - max(0, depth))
        api_messages.insert(
            insert_at,
            {"role": f["role"], "content": f["content"]},
        )


def build_messages_for_generic(
    chat: Chat,
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    preset: ContextPreset,
    generation_preset: Preset,
    history: list[ChatMessage],
    settings: Settings,
    *,
    libraries: list[BrainLibrary] | None = None,
    contact_scenario: ContactScenario | None = None,
    rollover_cursor: int = 0,
    is_mobile: bool = False,
    mode: str = "normal",
    brain_message_role: str | None = None,
) -> GenericGenerationContext:
    """Assemble the chat-completions API message list for Generic mode.

    Mirrors the AER builder's two-tier rollover with brain immunity.
    ``history`` is the active path's messages (caller resolves it via
    ``get_active_path`` and truncates for regen). The returned context's
    ``api_messages`` is ready to feed ``stream_chat`` verbatim.
    """
    libraries = list(libraries or [])
    history = list(history)
    if contact_scenario is None and chat.contact_scenario_id:
        contact_scenario = next(
            (c for c in contact.scenarios if c.id == chat.contact_scenario_id),
            None,
        )

    # The caller (which resolves the chat's effective provider, honoring any
    # per-chat override) passes the brain role directly. Fall back to the global
    # active provider only when it isn't supplied.
    if brain_message_role is not None:
        brain_role = brain_message_role
    else:
        provider_cfg = _resolve_provider_config(settings)
        brain_role = provider_cfg.brain_message_role if provider_cfg is not None else "system"

    # MacroCtx for system-prompt + additional-messages rendering. The same
    # context drives any ``{{global_brains}}`` / ``{{contact.*}}`` etc.
    # references in user-authored blocks.
    #
    # Impersonate flips the personas so ``{{contact.name}}`` /
    # ``{{contact.persona}}`` etc. resolve to the user persona's fields
    # (and vice versa) — the model gets a system block describing whomever
    # it's about to play. Contact-only fields (greeting, reminder_brain,
    # example_chats) defensively fall back to empty when the User entity
    # sits in the contact slot — see ``aer.macros`` getattr usage.
    if mode == "impersonate":
        macro_contact, macro_user = user, contact
    else:
        macro_contact, macro_user = contact, user
    macro_ctx = MacroCtx(
        contact=macro_contact,
        user=macro_user,
        scenario=scenario,
        contact_scenario=contact_scenario,
        chat=chat,
        active_path=history,
        messages_tree=history,
        rollover_cursor=rollover_cursor,
        settings=settings,
        is_mobile=is_mobile,
        generation_type=mode,
        libraries=libraries,
    )

    # 1. System message from the preset's blocks.
    system_content = render_system_prompt(preset, macro_ctx)
    api_messages: list[dict] = []
    if system_content.strip():
        api_messages.append({"role": "system", "content": system_content})

    # 2. Non-floating additional messages, in authored order.
    additionals = render_additional_messages(preset, macro_ctx)
    non_floats = [m for m in additionals if not m["float_enabled"]]
    for m in non_floats:
        api_messages.append({"role": m["role"], "content": m["content"]})

    # 3. History → API messages. Impersonate flips role assignment so the
    # historic contact/user lines land under the opposite roles — model
    # treats the user side as the assistant for the upcoming generation.
    contact_name_expanded = expand(contact.name or "", macro_ctx) if contact.name else ""
    user_name_expanded = expand(user.name or "", macro_ctx) if user.name else ""
    history_messages, conditional_msg_brains = _history_to_api_messages(
        history,
        cursor=rollover_cursor,
        contact_name=contact_name_expanded or contact.name or "",
        user_name=user_name_expanded or user.name or "",
        prefix_names=bool(preset.prefix_names),
        brain_role=brain_role,
        flip_roles=(mode == "impersonate"),
        chat_id=chat.id,
        compress_images=settings.compress_images,
        image_quality=settings.image_compression_quality,
    )
    api_messages.extend(history_messages)

    # 4. Floating additional messages.
    floats = [m for m in additionals if m["float_enabled"]]
    if floats:
        _splice_floats(api_messages, floats)

    # 5. Unconditional brain budget check. Generic mode bills unconditional
    # globals via ``{{global_brains}}`` inside the preset, so they don't
    # appear here as separate ``api_messages`` entries — but they DO take
    # up tokens inside whichever block contains the macro. We measure the
    # system content's already-rendered tokens; the global brain bytes are
    # already in there.
    base_context_size = max(1, int(generation_preset.max_context_tokens))
    rollover_window = max(0, int(generation_preset.rollover_window_tokens))
    brain_cap = int(base_context_size / 2.5)

    unconditional_globals = list(collect_unconditional_global_brains(
        contact, user, scenario, libraries,
    ))
    uncond_global_tokens = sum(_brain_block_tokens_generic(b) for b in unconditional_globals)
    # Local (per-message) unconditional brains live in ``api_messages`` as
    # brain-prefixed system blocks; sum their token cost separately.
    local_brain_tokens = 0
    for m in api_messages:
        if _is_brain(m):
            local_brain_tokens += _solo_tokens_generic(m)
    if uncond_global_tokens + local_brain_tokens > brain_cap:
        # Mirror AER's loud failure. Top-N offenders attributed by name.
        offenders: list[tuple[str, int, str | None]] = []
        for b in unconditional_globals:
            offenders.append((b.name or "(unnamed)", _brain_block_tokens_generic(b), b.id))
        # Per-message brains aren't attributed individually here; the route
        # can enrich offenders via ``_build_brain_owner_index`` like AER does.
        offenders.sort(key=lambda t: t[1], reverse=True)
        raise BrainBudgetExceeded(
            total=uncond_global_tokens + local_brain_tokens,
            cap=brain_cap,
            offenders=offenders[:5],
        )

    # 6. Two-tier rollover trim. Brain messages stay; user/assistant history
    # trims from cursor forward. Each trim step advances the cursor so the
    # caller can persist it onto the chat afterwards.
    cursor = rollover_cursor
    hard_limit = base_context_size + rollover_window
    soft_limit = base_context_size

    msg_tokens = [_solo_tokens_generic(m, is_first=(i == 0)) for i, m in enumerate(api_messages)]
    total = sum(msg_tokens)
    rolled_over = False
    if total > hard_limit:
        while total > soft_limit:
            dropped = False
            for i, m in enumerate(api_messages):
                if _is_history(m):
                    total -= msg_tokens[i]
                    del api_messages[i]
                    del msg_tokens[i]
                    cursor += 1
                    dropped = True
                    break
            if not dropped:
                break
        rolled_over = True

    # 7. Conditional brain splice. Uses the parametrised AER helper so the
    # tail-trim discipline matches AER exactly. ``base_context_size_override``
    # routes the helper's budget math through the preset's
    # ``max_context_tokens`` instead of AER's ``context_limits(settings)``;
    # ``count_tokens_fn`` swaps AER tokenizer-aware costing for Generic's
    # UTF-8 guesstimate; ``splice_role`` picks the inserted message's role.
    _maybe_splice_conditional_block(
        api_messages,
        history,
        contact=contact,
        user=user,
        scenario=scenario,
        intimacy=chat.intimacy,
        style=chat.style,
        response_length=chat.response_length,
        cjk=chat.cjk,
        chat_tags=chat.tags or "",
        is_deletion=False,
        conditional_msg_brains=conditional_msg_brains,
        settings=settings,
        libraries=libraries,
        splice_role=brain_role,
        count_tokens_fn=_solo_tokens_generic,
        base_context_size_override=base_context_size,
    )

    # 8. Reminder brain splice. Same parametrised helper; ``splice_role`` is
    # the only Generic-vs-AER difference (and the token counter for honest
    # budget math when merging into adjacent brain blocks).
    _splice_reminder_brain(
        api_messages,
        contact,
        user=user,
        scenario=scenario,
        chat=chat,
        contact_scenario=contact_scenario,
        active_path=history,
        messages_tree=history,
        rollover_cursor=cursor,
        settings=settings,
        is_mobile=is_mobile,
        generation_type="normal",
        libraries=libraries,
        splice_role=brain_role,
        count_tokens_fn=_solo_tokens_generic,
    )

    # 9. Final token totals. ``history_tokens`` counts the user/assistant
    # entries that survived the trim (excluding brain blocks and the
    # system/preset messages). ``system_tokens`` is the first system entry's
    # contribution — the AER renderer's equivalent.
    final_msg_tokens = [_solo_tokens_generic(m, is_first=(i == 0)) for i, m in enumerate(api_messages)]
    total_tokens = sum(final_msg_tokens)
    history_tokens = sum(
        t for m, t in zip(api_messages, final_msg_tokens) if _is_history(m)
    )
    system_tokens = final_msg_tokens[0] if final_msg_tokens and api_messages[0].get("role") == "system" else 0

    new_path_ids = [m.id for m in history]

    return GenericGenerationContext(
        api_messages=api_messages,
        total_tokens=total_tokens,
        history_tokens=history_tokens,
        system_tokens=system_tokens,
        new_cursor=cursor,
        new_path_ids=new_path_ids,
        new_rolled_over=rolled_over,
    )
