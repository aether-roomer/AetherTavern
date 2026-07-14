"""SSE streaming endpoint for chat completions, plus cancel-generation route.

Per-bubble streaming: deltas from the upstream completions endpoint feed
:class:`StreamingBubbleParser`; complete bubbles are emitted as SSE events
with ``{text, emotion}`` so the UI can render them with an emotion sprite as
soon as they land.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


class _LeadingNamePrefixStripper:
    """Strip a leading ``[whitespace]ContactName:[whitespace/newline]``
    prefix from a streaming text source — buffering only as many
    characters as the longest possible match could occupy, then emitting
    the rest verbatim once the question is settled.

    Generic-mode upstreams sometimes echo the prefix the prompt seeded
    them with (``prefix_names`` on ⇒ ``Name:\\n`` ahead of each turn).
    Stripping at the wire instead of after-the-fact spares the user
    seeing the echo flash into the bubble and then vanish at ``done``.
    """

    def __init__(self, name: str | None):
        self._name = name or ""
        self._enabled = bool(name)
        self._pattern = (
            re.compile(rf"\A\s*{re.escape(name)}\s*:\s*\n?") if name else None
        )
        self._buf = ""
        # Generous cap so we tolerate odd whitespace patterns without
        # blocking output indefinitely on a non-match.
        self._cap = (len(name) + 32) if name else 0

    def feed(self, text: str) -> str:
        if not self._enabled or not text:
            return text
        self._buf += text
        m = self._pattern.match(self._buf)
        if m:
            # The pattern's trailing ``\s*\n?`` is greedy, but greedy
            # only over what's currently in the buffer — if the match
            # ends exactly at buf-end, more whitespace might still
            # arrive that the pattern would have consumed (and stripped).
            # Hold off committing until we see at least one byte past
            # the match, or hit the cap as a deadlock guard.
            if m.end() == len(self._buf) and len(self._buf) < self._cap:
                return ""
            self._enabled = False
            tail = self._buf[m.end():]
            self._buf = ""
            return tail
        if len(self._buf) < self._cap and self._could_still_match():
            return ""
        self._enabled = False
        out = self._buf
        self._buf = ""
        return out

    def flush(self) -> str:
        if not self._enabled:
            return ""
        self._enabled = False
        out = self._buf
        self._buf = ""
        return out

    def _could_still_match(self) -> bool:
        # The leading-whitespace portion is always optional, so a
        # wholly-whitespace buffer keeps hope alive. Otherwise the
        # non-whitespace head must be an initial substring of
        # ``ContactName:`` — once that's no longer true, the prefix
        # cannot show up and we should flush.
        stripped = self._buf.lstrip()
        if not stripped:
            return True
        candidate = f"{self._name}:"
        return candidate.startswith(stripped)

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from server import storage
from server.aer.parser import StreamingBubbleParser
from server.aer.rollover import (
    BrainBudgetExceeded,
    build_messages_for_generation,
    get_active_path,
)
from server.aer.template import render
from server.generic.context import (
    GenericGenerationContext,
    build_messages_for_generic,
    guesstimate_tokens,
)
from server.generic.image_rewrite import ImageRewriter
from server.inference import CompletionParams, InferenceError, stream_completion
from server.inference_generic import (
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    GenerationParams,
    ReasoningDeltaEvent,
    StartEvent,
    UsageEvent,
    stream_chat,
)
from server.proxy_rules import NoMatchingProxyRule, proxy_rules_scope
from server.secrets import resolve_llm_token
from server.generation_target import (
    EffectiveTarget,
    GenerationTargetError,
    resolve_generation_target,
)
from server.models import (
    EMPTY_SENTINEL,
    ROOT_PARENT_KEY,
    BrainLibrary,
    Chat,
    ChatMessage,
    Contact,
    Emotion,
    GenerationMode,
    Scenario,
    SubMessage,
    User,
    new_id,
    now_seconds,
)


log = logging.getLogger("aether.generate")

router = APIRouter(prefix="/api/chats", tags=["generate"])
contacts_router = APIRouter(prefix="/api/contacts", tags=["generate"])

# Per-chat cancel events (independent of the global slot — cancel is keyed by
# the chat the user is looking at) plus a single-slot global generation lock.
# AetherTavern is single-user / localhost; two parallel generations would just
# split bandwidth on the upstream endpoint and the UI can only follow one
# stream at a time, so we decline rather than serialise.
_cancel_events: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
_inflight_label: Optional[str] = None

_BUSY_MESSAGE = (
    "Another generation is already in progress. Wait for it to finish or "
    "cancel it before starting a new one."
)


def _claim_slot(label: str) -> bool:
    """Atomically claim the global generation slot.

    Returns False when another generation is active. The check-and-set is
    race-free because asyncio is single-threaded — there is no await between
    the read and the assignment.
    """
    global _inflight_label
    if _inflight_label is not None:
        return False
    _inflight_label = label
    return True


def _release_slot(label: str) -> None:
    global _inflight_label
    if _inflight_label == label:
        _inflight_label = None


def _sse(event: str, data: dict | str) -> bytes:
    body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def _error_stream(message: str, kind: str) -> StreamingResponse:
    """One-shot SSE response that emits a typed error event and closes.

    Used for preflight failures (busy slot, stale tip) that we want to surface
    through the same channel the client already uses for inference errors —
    a plain HTTPException would arrive as an opaque connection failure since
    EventSource can't read response bodies on non-2xx replies.
    """
    async def gen():
        yield _sse("error", {"message": message, "kind": kind})
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _resolve_chat_libraries(chat: Chat) -> list[BrainLibrary]:
    """Look up the chat's attached brain libraries in attach order.

    Unknown ids are silently skipped — DELETE library leaves dangling
    references intentionally so a later re-import can rehydrate them.
    """
    out: list[BrainLibrary] = []
    for lid in (chat.brain_library_ids or []):
        lib = storage.get_brain_library(lid)
        if lib is not None:
            out.append(lib)
    return out


def _resolve_chat_scenario(chat: Chat, contact: Contact) -> Scenario | None:
    """Pick the scenario object the AER builders should consume for this chat.

    A contact-scenario takes precedence over the global scenario reference;
    we wrap it in a ``Scenario`` so :func:`build_system_prompt` and friends
    keep operating on a single shape (they only read scene/environment/tags/
    cjk/brains, all of which ``ContactScenario`` carries).
    """
    if chat.contact_scenario_id:
        cs = next((c for c in contact.scenarios if c.id == chat.contact_scenario_id), None)
        if cs is None:
            return None
        return Scenario(
            id=cs.id,
            name=cs.name,
            description=cs.description,
            environment=cs.environment,
            scene=cs.scene,
            tags=cs.tags,
            cjk=cs.cjk,
            brains=list(cs.brains),
        )
    if chat.scenario_id:
        return storage.get_scenario(chat.scenario_id)
    return None


def _attribute_active_brains(
    active_brains,
    owners: dict[str, dict],
) -> list[dict]:
    """Serialize ``ctx.active_brains`` (rollover ``ActiveBrain`` dataclasses)
    into a JSON-friendly list, attaching owner info from a pre-built
    ``_build_brain_owner_index`` map. Used by both the SSE ``context``
    event (during generation) and the ``/context-tokens`` endpoint."""
    out: list[dict] = []
    for ab in (active_brains or []):
        item = {
            "brain_id": ab.brain_id,
            "name": ab.name,
            "tokens": ab.tokens,
            "conditional": ab.conditional,
        }
        owner = owners.get(ab.brain_id)
        if owner is not None:
            item.update(owner)
        out.append(item)
    return out


def _attribute_brain_offenders(
    offenders: list[tuple[str, int, str | None]],
    *,
    chat: Chat,
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: list[BrainLibrary],
    messages: list,
) -> list[dict]:
    """Attach owner info to each offender so the toast can deep-link the
    user to where the brain lives. Lookup is by ``brain.id`` via a single
    pre-built dict; unattributed offenders ship with just name + tokens and
    render as plain text in the toast."""
    owners = _build_brain_owner_index(chat, contact, user, scenario, libraries, messages)
    out: list[dict] = []
    for name, tokens, brain_id in offenders:
        item: dict = {"name": name, "tokens": tokens}
        if brain_id:
            item["brain_id"] = brain_id
            owner = owners.get(brain_id)
            if owner is not None:
                item.update(owner)
        out.append(item)
    return out


def _build_brain_name_index(
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: list[BrainLibrary],
    messages: list,
) -> dict[str, str]:
    """``{brain_id: current_name}`` map for refreshing stored provenance
    against the live state. Mirror walk order of ``_build_brain_owner_index``."""
    out: dict[str, str] = {}
    for b in contact.brains:
        out[b.id] = b.name or "(unnamed)"
    for cs in (contact.scenarios or []):
        for b in (cs.brains or []):
            out[b.id] = b.name or "(unnamed)"
    for b in user.brains:
        out[b.id] = b.name or "(unnamed)"
    if scenario is not None:
        for b in scenario.brains:
            out[b.id] = b.name or "(unnamed)"
    for lib in libraries:
        for b in lib.brains:
            out[b.id] = b.name or "(unnamed)"
    for m in (messages or []):
        for b in (m.brains or []):
            out[b.id] = b.name or "(unnamed)"
    return out


def _build_brain_owner_index(
    chat: Chat,
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: list[BrainLibrary],
    messages: list,
) -> dict[str, dict]:
    """Build a ``{brain.id: {kind, owner_id, owner_name}}`` index covering
    every brain that could appear in this chat's prompt. The user edits
    ContactScenario brains via the parent Contact, so they share the
    contact-page route; ``owner_name`` reflects the visible-label hierarchy
    used by the active-brains modal (``{contact} · {scenario}`` for
    ContactScenario brains, plain entity names otherwise)."""
    out: dict[str, dict] = {}
    contact_name = contact.name or "(unnamed contact)"
    for b in contact.brains:
        out[b.id] = {
            "kind": "contact", "owner_id": contact.id, "owner_name": contact_name,
        }
    for cs in (contact.scenarios or []):
        cs_name = cs.name or "(unnamed scenario)"
        label = f"{contact_name} · {cs_name}"
        for b in (cs.brains or []):
            out[b.id] = {
                "kind": "contact_scenario", "owner_id": contact.id, "owner_name": label,
            }
    user_name = user.name or "(unnamed persona)"
    for b in user.brains:
        out[b.id] = {"kind": "user", "owner_id": user.id, "owner_name": user_name}
    if scenario is not None:
        # The scenario may be a synthesized stand-in for a ContactScenario —
        # in that case we want the contact's route, not a non-existent
        # standalone-scenario route.
        is_contact_scen = bool(chat.contact_scenario_id) and scenario.id == chat.contact_scenario_id
        scen_name = scenario.name or "(unnamed scenario)"
        for b in scenario.brains:
            if is_contact_scen:
                out[b.id] = {
                    "kind": "contact_scenario", "owner_id": contact.id,
                    "owner_name": f"{contact_name} · {scen_name}",
                }
            else:
                out[b.id] = {
                    "kind": "scenario", "owner_id": scenario.id, "owner_name": scen_name,
                }
    for lib in libraries:
        lib_name = lib.name or "(unnamed library)"
        for b in lib.brains:
            out[b.id] = {
                "kind": "brain_library", "owner_id": lib.id, "owner_name": lib_name,
            }
    chat_title = chat.title or "this chat"
    for m in (messages or []):
        for b in (m.brains or []):
            out[b.id] = {
                "kind": "chat_message", "owner_id": chat.id,
                "owner_name": f"Per-message brains · {chat_title}",
            }
    return out


def _resolve_preset(chat, settings, presets):
    preset_id = chat.preset_id or settings.default_preset_id
    if preset_id is not None:
        for p in presets:
            if p.id == preset_id:
                return p
    return presets[0] if presets else None


def _persist_assistant_message(
    *,
    chat_id: str,
    chat,
    msgs_container,
    new_msg_id: str,
    chosen_parent_id: Optional[str],
    bubbles: list[SubMessage],
    contact_name: str,
    new_cursor: int,
    new_path_ids: list[str],
    new_rolled_over: bool,
    context_tokens: int,
    active_brains_payload: list[dict],
    origin: str = "aer",
    reasoning: Optional[str] = None,
    image_refs: Optional[dict[str, str]] = None,
    generation_started_at: Optional[float] = None,
    generation_duration_seconds: Optional[float] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    generation_preset_id: Optional[str] = None,
    context_preset_id: Optional[str] = None,
    sender: str = "contact",
    sender_name: Optional[str] = None,
    seed_bubbles: Optional[list[SubMessage]] = None,
) -> None:
    """Append a freshly-generated message and update chat metadata.

    ``seed_bubbles`` is the original message's body for continue mode — the
    new sibling carries ``seed_bubbles + bubbles``. ``sender`` / ``sender_name``
    cover impersonate mode (user-side messages with a non-manual origin).
    """
    body = list(seed_bubbles) + list(bubbles) if seed_bubbles else list(bubbles)
    new_msg = ChatMessage(
        id=new_msg_id,
        parent_id=chosen_parent_id,
        sender=sender,
        sender_name=sender_name if sender_name is not None else contact_name,
        body=body,
        # Stamp the brains that actually shipped onto the message itself
        # as provenance. Each gen carries its own snapshot — survives
        # reloads, doesn't conflict across chats/tabs, and naturally
        # ties to message branches: switching to a sibling assistant
        # message shows that branch's brains, not the most recent gen's.
        active_brains=list(active_brains_payload),
        origin=origin,
        reasoning=reasoning,
        image_refs=dict(image_refs) if image_refs else {},
        generation_started_at=generation_started_at,
        generation_duration_seconds=generation_duration_seconds,
        provider=provider,
        model=model,
        generation_preset_id=generation_preset_id,
        context_preset_id=context_preset_id,
    )
    msgs_container.messages.append(new_msg)
    key = chosen_parent_id if chosen_parent_id is not None else ROOT_PARENT_KEY
    chat.selected_child_id[key] = new_msg_id
    chat.rollover_start_index = new_cursor
    chat.rollover_path_ids = list(new_path_ids) + [new_msg_id]
    chat.rolled_over = new_rolled_over
    chat.last_context_tokens = context_tokens
    chat.updated_at = now_seconds()
    # messages.yaml first so chat.selected_child_id's reference to the new
    # message resolves if the process dies between the two writes.
    # bump_version=False: a generation isn't an entity-level edit and
    # shouldn't 409 the chat info modal's open draft.
    storage.save_chat_messages(chat_id, msgs_container)
    storage.save_chat(chat, bump_version=False)


# ---------------------------------------------------------------------------
# Generic-mode generation branch
# ---------------------------------------------------------------------------


def _build_generic_response(
    *,
    chat_id: str,
    chat: Chat,
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: list[BrainLibrary],
    settings,
    target: EffectiveTarget,
    request: Request,
    parent_id: Optional[str],
    expected_tip_id: Optional[str],
    is_mobile: bool,
    started_at_wall: float,
    started_at_mono: float,
    label: str,
    mode: str = "normal",
) -> StreamingResponse:
    """Generic-mode counterpart to AER's stream-definition block.

    All preflight (provider / context preset / token resolution, active-path
    alignment, regen path truncation) happens inside the ``stream()``
    closure so the ``finally`` clause always releases the global slot
    regardless of which preflight step fails.

    Yields typed SSE errors (``no_provider`` / ``no_preset`` / ``no_token`` /
    ``brain_budget`` / ``proxy`` / provider error kinds / ``persist``)
    inline; the client renders them through the same channel as
    successful generations.
    """
    rules_for_request = getattr(request.app.state, "proxy_rules", None)
    cancel = _cancel_events[chat_id]
    cancel.clear()
    new_msg_id = new_id()

    async def stream():
        try:
            # 1. Preflight. Provider / model / token were resolved by
            # ``resolve_generation_target`` (the route turned any
            # ``GenerationTargetError`` into a one-shot SSE before reaching
            # here); we still load the Context Preset object.
            provider_cfg = target.provider_cfg
            provider_kind = target.provider_kind
            model_id = target.model
            llm_token = target.token
            preset = storage.get_context_preset(target.context_preset_id)
            if preset is None:
                yield _sse("error", {
                    "message": "The Context Preset for this chat no longer exists. Pick another in Settings.",
                    "kind": "no_preset",
                })
                return

            presets = storage.load_presets().presets
            generation_preset = _resolve_preset(chat, settings, presets)
            if generation_preset is None:
                yield _sse("error", {
                    "message": "No generation presets configured.",
                    "kind": "no_preset",
                })
                return

            # 2. Active-path resolution — mirrors AER's logic so multi-tab
            # branches land where the user is looking.
            msgs_container = storage.load_chat_messages(chat_id)
            active = get_active_path(chat, msgs_container.messages)

            if expected_tip_id is not None:
                actual_tip = active[-1].id if active else ""
                if expected_tip_id != actual_tip:
                    if expected_tip_id == "":
                        chat.selected_child_id[ROOT_PARENT_KEY] = EMPTY_SENTINEL
                    else:
                        by_id = {m.id: m for m in msgs_container.messages}
                        cur = by_id.get(expected_tip_id)
                        while cur is not None:
                            key = cur.parent_id if cur.parent_id is not None else ROOT_PARENT_KEY
                            chat.selected_child_id[key] = cur.id
                            cur = by_id.get(cur.parent_id) if cur.parent_id else None
                    async with storage.lock(f"chat:{chat_id}"):
                        storage.recount_active_path(chat, msgs_container)
                        storage.save_chat(chat, bump_version=False)
                    active = get_active_path(chat, msgs_container.messages)

            is_regen = parent_id is not None
            if not is_regen:
                chosen_parent_id = active[-1].id if active else None
            elif parent_id == "":
                chosen_parent_id = None
            else:
                chosen_parent_id = parent_id

            # Continue mode: keep the full path so the provider sees the
            # assistant message at the tail; persist as sibling of the tip
            # with the streamed text appended to the LAST bubble's text
            # (the model continues mid-bubble — it doesn't know about our
            # bubble boundaries) and the rest of the original body left
            # intact. Multi-bubble originals therefore keep their
            # structure across continues.
            continue_persist_parent_id: Optional[str] = None
            continue_prior_bubbles: list[SubMessage] = []
            continue_last_text: str = ""
            continue_last_emotion: Optional[Emotion] = None
            if mode == "continue":
                if not active or active[-1].sender != "contact":
                    yield _sse("error", {
                        "message": "Continue requires the last message to be from the contact.",
                        "kind": "continue_invalid",
                    })
                    return
                original_tip = active[-1]
                if not original_tip.body:
                    yield _sse("error", {
                        "message": "Cannot continue an empty message.",
                        "kind": "continue_invalid",
                    })
                    return
                continue_prior_bubbles = list(original_tip.body[:-1])
                continue_last_text = original_tip.body[-1].text or ""
                continue_last_emotion = original_tip.body[-1].emotion
                continue_persist_parent_id = original_tip.parent_id
                # Keep the tip in the path; the builder's history loop
                # produces it as the trailing ``role: assistant`` message.
                chosen_parent_id = original_tip.id
                is_regen = False

            # Truncate the active path for regen (same idiom AER's builder uses).
            if is_regen:
                if chosen_parent_id is None:
                    path: list[ChatMessage] = []
                else:
                    by_id = {m.id: m for m in msgs_container.messages}
                    if chosen_parent_id in {m.id for m in active}:
                        path = []
                        for m in active:
                            path.append(m)
                            if m.id == chosen_parent_id:
                                break
                    else:
                        cur = by_id.get(chosen_parent_id)
                        walked: list[ChatMessage] = []
                        while cur is not None:
                            walked.append(cur)
                            cur = by_id.get(cur.parent_id) if cur.parent_id else None
                        path = list(reversed(walked))
            else:
                path = list(active)

            # Images dir for this chat — created lazily by the rewriter when
            # a data: URL is decoded to disk.
            chat_path = storage.chat_dir(chat_id)
            if chat_path is None:
                yield _sse("error", {
                    "message": "Chat directory not found.",
                    "kind": "no_provider",
                })
                return
            images_dir = chat_path / "images"
            yield _sse("start", {
                "message_id": new_msg_id,
                "parent_id": (
                    continue_persist_parent_id
                    if mode == "continue"
                    else chosen_parent_id
                ),
            })

            # Build messages.
            try:
                ctx = build_messages_for_generic(
                    chat=chat,
                    contact=contact,
                    user=user,
                    scenario=scenario,
                    preset=preset,
                    generation_preset=generation_preset,
                    history=path,
                    settings=settings,
                    libraries=libraries,
                    rollover_cursor=chat.rollover_start_index if chat.rolled_over else 0,
                    is_mobile=is_mobile,
                    mode=mode,
                    brain_message_role=target.brain_message_role,
                )
            except BrainBudgetExceeded as e:
                yield _sse("error", {
                    "message": str(e),
                    "kind": "brain_budget",
                    "total": e.total,
                    "cap": e.cap,
                    "offenders": _attribute_brain_offenders(
                        e.offenders,
                        chat=chat,
                        contact=contact,
                        user=user,
                        scenario=scenario,
                        libraries=libraries,
                        messages=msgs_container.messages,
                    ),
                })
                return

            yield _sse("context", {
                "total_tokens": ctx.total_tokens,
                "messages_in_context": max(0, len(ctx.new_path_ids) - ctx.new_cursor),
                "messages_total": len(ctx.new_path_ids),
                "oldest_in_context_id":
                    ctx.new_path_ids[ctx.new_cursor]
                    if 0 <= ctx.new_cursor < len(ctx.new_path_ids) else None,
                "active_brains": [],
                "active_brains_from_last_gen": False,
            })
            # Debug surface mirroring AER's ``prompt`` event: the full
            # messages list as it'll be sent to /v1/chat/completions, so
            # the operator can inspect exactly what the model saw. Shape
            # matches a standard OpenAI ``messages=[{role, content}]``
            # payload.
            yield _sse("prompt", {
                "messages": ctx.api_messages,
                "tokens": ctx.total_tokens,
            })

            # Stream from the provider.
            params = GenerationParams(
                model=model_id,
                temperature=generation_preset.temperature,
                top_p=generation_preset.top_p,
                top_k=generation_preset.top_k if generation_preset.top_k else None,
                min_p=generation_preset.min_p if generation_preset.min_p else None,
                max_tokens=generation_preset.max_new_tokens,
                presence_penalty=generation_preset.presence_penalty or None,
                frequency_penalty=generation_preset.frequency_penalty or None,
            )

            rewriter = ImageRewriter(chat_id=chat_id, images_dir=images_dir)
            # Strip the prompt-seeded ``Name:\n`` echo at the wire so the
            # client never sees it. The post-stream re-strip below still
            # runs as a belt-and-braces measure for stored text.
            speaker_name = user.name if mode == "impersonate" else contact.name
            name_stripper = _LeadingNamePrefixStripper(
                speaker_name if (preset.prefix_names and speaker_name) else None,
            )
            persist_text_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage_prompt_tokens: int | None = None
            finish_reason: str | None = None
            error_emitted = False
            error_kind: str | None = None

            async def disconnect_watch():
                try:
                    while not cancel.is_set():
                        if await request.is_disconnected():
                            cancel.set()
                            return
                        await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    return

            watch_task = asyncio.create_task(disconnect_watch())
            try:
                try:
                    with proxy_rules_scope(rules_for_request):
                        async for evt in stream_chat(
                            api_messages=ctx.api_messages,
                            model=model_id,
                            base_url=provider_cfg.base_url,
                            token=llm_token,
                            params=params,
                            provider=provider_kind,
                            cache_minutes=provider_cfg.cache_minutes,
                            cancel_event=cancel,
                        ):
                            if cancel.is_set():
                                break
                            if isinstance(evt, StartEvent):
                                # Internal start; client already got our own start above.
                                continue
                            if isinstance(evt, DeltaEvent):
                                stripped = name_stripper.feed(evt.text)
                                if not stripped:
                                    continue
                                out = rewriter.feed(stripped)
                                if out.persist:
                                    persist_text_parts.append(out.persist)
                                if out.wire:
                                    yield _sse("delta", {"text": out.wire})
                                continue
                            if isinstance(evt, ReasoningDeltaEvent):
                                reasoning_parts.append(evt.text)
                                yield _sse("reasoning_delta", {"text": evt.text})
                                continue
                            if isinstance(evt, UsageEvent):
                                if evt.prompt_tokens:
                                    usage_prompt_tokens = evt.prompt_tokens
                                continue
                            if isinstance(evt, DoneEvent):
                                finish_reason = evt.finish_reason
                                continue
                            if isinstance(evt, ErrorEvent):
                                yield _sse("error", {
                                    "message": evt.message,
                                    "kind": evt.error_kind,
                                })
                                error_emitted = True
                                error_kind = evt.error_kind
                                break
                except NoMatchingProxyRule as e:
                    yield _sse("error", {"message": str(e), "kind": "proxy"})
                    return
            finally:
                watch_task.cancel()

            # End-of-stream flush. The name stripper is drained first so
            # any buffered head bytes (no prefix match found) feed into
            # the rewriter ahead of its own image-buffer flush.
            stripped_tail = name_stripper.flush()
            if stripped_tail:
                out = rewriter.feed(stripped_tail)
                if out.persist:
                    persist_text_parts.append(out.persist)
                if out.wire:
                    yield _sse("delta", {"text": out.wire})
            tail = rewriter.flush()
            if tail.persist:
                persist_text_parts.append(tail.persist)
            if tail.wire:
                yield _sse("delta", {"text": tail.wire})

            cancelled = cancel.is_set()

            if error_emitted:
                # Already surfaced; finalize done with no message id.
                yield _sse("done", {"message_id": None, "cancelled": cancelled})
                return

            assistant_text = "".join(persist_text_parts)
            # Belt-and-braces re-strip: the wire-side stripper already
            # drops the prefix from the stream, but if anything slipped
            # through (e.g. an empty-name edge case) the persisted text
            # gets one more pass with leading whitespace tolerated.
            if preset.prefix_names and speaker_name:
                pattern = r"^\s*" + re.escape(speaker_name) + r"\s*:\s*\n?"
                assistant_text = re.sub(pattern, "", assistant_text, count=1)

            # Continue mode: rebuild the body as
            # ``original.body[:-1] + [last_bubble_with_text_extended]`` so
            # multi-bubble originals keep their structure and the
            # streamed extension lands inside the bubble the model was
            # actually continuing.
            if mode == "continue":
                continued_bubbles = list(continue_prior_bubbles) + [
                    SubMessage(
                        text=continue_last_text + assistant_text,
                        emotion=continue_last_emotion,
                    ),
                ]
                persist_parent = continue_persist_parent_id
            else:
                continued_bubbles = [SubMessage(text=assistant_text, emotion=None)]
                persist_parent = chosen_parent_id

            persist_error: str | None = None
            has_persistable_text = (
                any(b.text for b in continued_bubbles) if mode == "continue"
                else bool(assistant_text)
            )
            if has_persistable_text or reasoning_parts:
                try:
                    async with storage.lock(f"chat:{chat_id}"):
                        # Final context tokens: prefer usage.prompt_tokens
                        # when the upstream reported it; else stick with
                        # the build-time guesstimate. Either way, persist
                        # the value into Chat.last_context_tokens so the
                        # next turn's pre-flight estimate has a baseline.
                        final_context_tokens = (
                            usage_prompt_tokens
                            if usage_prompt_tokens is not None
                            else ctx.total_tokens
                        )
                        _persist_assistant_message(
                            chat_id=chat_id, chat=chat,
                            msgs_container=msgs_container,
                            new_msg_id=new_msg_id,
                            chosen_parent_id=persist_parent,
                            bubbles=continued_bubbles,
                            contact_name=contact.name,
                            new_cursor=ctx.new_cursor,
                            new_path_ids=ctx.new_path_ids,
                            new_rolled_over=ctx.new_rolled_over,
                            context_tokens=final_context_tokens,
                            active_brains_payload=[],
                            origin="generic",
                            reasoning="".join(reasoning_parts) or None,
                            image_refs=dict(rewriter.image_refs),
                            generation_started_at=started_at_wall,
                            generation_duration_seconds=time.monotonic() - started_at_mono,
                            provider=provider_kind,
                            model=model_id,
                            generation_preset_id=generation_preset.id,
                            context_preset_id=preset.id,
                            sender="user" if mode == "impersonate" else "contact",
                            sender_name=(
                                user.name if mode == "impersonate" else contact.name
                            ),
                        )
                except Exception as e:
                    log.exception("Generic persist failed for chat %s", chat_id)
                    persist_error = str(e)

            if persist_error:
                yield _sse("error", {
                    "message": f"Failed to persist message: {persist_error}",
                    "kind": "persist",
                })
                yield _sse("done", {"message_id": None, "cancelled": cancelled})
            elif has_persistable_text or reasoning_parts:
                yield _sse("done", {"message_id": new_msg_id, "cancelled": cancelled})
            else:
                yield _sse("done", {"message_id": None, "cancelled": cancelled})
        except Exception as e:
            log.exception("Generic generate failed for chat %s", chat_id)
            yield _sse("error", {"message": str(e)})
        finally:
            _release_slot(label)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{chat_id}/generate")
async def generate(
    chat_id: str,
    request: Request,
    parent_id: Optional[str] = Query(default=None),
    greeting: bool = Query(default=False),
    expected_tip_id: Optional[str] = Query(default=None),
    is_mobile: bool = Query(default=False),
    mode: GenerationMode = Query(default="normal"),
) -> StreamingResponse:
    """SSE: stream a contact reply (or a fresh greeting if the chat is empty)."""
    label = f"chat:{chat_id}"
    if not _claim_slot(label):
        return _error_stream(_BUSY_MESSAGE, "busy")

    # Sample the generation start time once the slot is claimed. Wall-clock
    # for the popover's "X minutes ago"; monotonic for an honest duration
    # measurement that won't jump if the system clock moves.
    started_at_wall = time.time()
    started_at_mono = time.monotonic()

    # Once claimed, the slot must be released on every exit path. The async
    # stream() takes ownership on the success path; the finally below catches
    # everything else (early returns for staleness, HTTPExceptions from the
    # storage lookups, anything unexpected).
    handed_off_to_stream = False
    try:
        chat = storage.get_chat(chat_id)
        if chat is None:
            raise HTTPException(404, f"chat {chat_id!r} not found")
        contact = storage.get_contact(chat.contact_id)
        user = storage.get_user(chat.user_id)
        if contact is None:
            raise HTTPException(400, "chat references a missing contact")
        if user is None:
            raise HTTPException(400, "chat references a missing user persona")
        scenario = _resolve_chat_scenario(chat, contact)
        libraries = _resolve_chat_libraries(chat)

        settings = storage.load_settings()
        # Resolve the chat's effective provider/model/context-preset, honoring
        # any per-chat override. Generation branches on the RESOLVED mode, not
        # settings.provider_mode, so a chat can pin AER or a specific Generic
        # provider regardless of the global switch.
        try:
            target = resolve_generation_target(chat, settings)
        except GenerationTargetError as e:
            return _error_stream(e.message, e.kind)
        if target.mode == "generic":
            # Generic-mode branch — Context Preset load, active-path resolution,
            # stream definition, slot hand-off. Returns its own StreamingResponse;
            # AER code below is skipped.
            generic_response = _build_generic_response(
                chat_id=chat_id,
                chat=chat,
                contact=contact,
                user=user,
                scenario=scenario,
                libraries=libraries,
                settings=settings,
                target=target,
                request=request,
                parent_id=parent_id,
                expected_tip_id=expected_tip_id,
                is_mobile=is_mobile,
                started_at_wall=started_at_wall,
                started_at_mono=started_at_mono,
                label=label,
                mode=mode,
            )
            if isinstance(generic_response, StreamingResponse):
                # The Generic stream() owns the slot from here on out;
                # its finally clause releases.
                handed_off_to_stream = True
            return generic_response
        presets = storage.load_presets().presets
        preset = _resolve_preset(chat, settings, presets)
        if preset is None:
            raise HTTPException(500, "No generation presets configured.")

        msgs_container = storage.load_chat_messages(chat_id)

        active = get_active_path(chat, msgs_container.messages)

        # Path jump: when the client passes ``expected_tip_id`` it is asserting
        # "the active path's tail in my view is this id" (or "" for an empty
        # chat). The tab the user actually clicked in is the source of truth
        # for where the generation lands, so align ``selected_child_id`` with
        # that view before generating. Handles two-tabs-of-the-same-chat: each
        # generation extends from where its tab is looking, regardless of any
        # cursor changes the other tab made meanwhile. Last-gen-wins on shared
        # state, but each tab's own actions stay coherent within itself.
        if expected_tip_id is not None:
            actual_tip = active[-1].id if active else ""
            if expected_tip_id != actual_tip:
                if expected_tip_id == "":
                    chat.selected_child_id[ROOT_PARENT_KEY] = EMPTY_SENTINEL
                else:
                    by_id = {m.id: m for m in msgs_container.messages}
                    cur = by_id.get(expected_tip_id)
                    # Walk back to root, pinning each ancestor as the chosen
                    # child of its parent. Silently no-ops on an unknown tip
                    # (e.g. a message deleted in another tab) — fall back to
                    # whatever the server's current selection produces.
                    while cur is not None:
                        key = cur.parent_id if cur.parent_id is not None else ROOT_PARENT_KEY
                        chat.selected_child_id[key] = cur.id
                        cur = by_id.get(cur.parent_id) if cur.parent_id else None
                # Brief lock around the chat-tree write — serializes against
                # message-create / select-child / persist running in another
                # request. bump_version=False: tree-state alignment, not an
                # entity-level edit, so the chat info modal's open draft
                # shouldn't 409 because another tab kicked off a gen.
                async with storage.lock(f"chat:{chat_id}"):
                    storage.recount_active_path(chat, msgs_container)
                    storage.save_chat(chat, bump_version=False)
                active = get_active_path(chat, msgs_container.messages)

        # ``parent_id`` carries three cases (see api.js): omitted (None) ⇒ extend
        # the chat from the active tip; "" ⇒ regenerate at the root (e.g.
        # rerolling a seeded greeting); a real id ⇒ regenerate as a sibling of
        # that parent.
        is_regen = parent_id is not None
        if not is_regen:
            chosen_parent_id = active[-1].id if active else None
        elif parent_id == "":
            chosen_parent_id = None
        else:
            chosen_parent_id = parent_id

        # Continue mode: build the prompt with the full path INCLUDING the
        # active tip's content (so the model sees ``  Emotion: …\n`` and
        # emits the next bubble). The new message persists as a SIBLING of
        # the tip, with body = tip.body + new bubbles.
        continue_seed_bubbles: list[SubMessage] | None = None
        continue_persist_parent_id: Optional[str] = None
        if mode == "continue":
            if not active or active[-1].sender != "contact":
                return _error_stream(
                    "Continue requires the last message to be from the contact.",
                    "continue_invalid",
                )
            original_tip = active[-1]
            continue_seed_bubbles = list(original_tip.body)
            continue_persist_parent_id = original_tip.parent_id
            # Builder sees the full path; persistence uses the tip's parent.
            chosen_parent_id = original_tip.id
            is_regen = False

        # Impersonate mode: the model plays the user. New message is a
        # child of the current tip (or root if empty) with ``sender="user"``.
        if mode == "impersonate":
            chosen_parent_id = active[-1].id if active else None
            is_regen = False

        # A new turn with no parent IS the chat's opening line, so render with
        # greeting-style instructions. Covers both empty-chat extends and explicit
        # root regens (where we don't want the prior greeting bleeding in).
        is_greeting_for_api = chosen_parent_id is None or greeting

        cancel = _cancel_events[chat_id]
        cancel.clear()

        new_msg_id = new_id()
        handed_off_to_stream = True
    finally:
        if not handed_off_to_stream:
            _release_slot(label)

    rules_for_request = getattr(request.app.state, "proxy_rules", None)

    # Effective parent for the NEW message (sibling-of-tip in continue mode,
    # otherwise the builder's chosen_parent_id which is also the persist key).
    effective_persist_parent_id = (
        continue_persist_parent_id if mode == "continue" else chosen_parent_id
    )

    async def stream():
        bubbles: list[SubMessage] = []
        try:
            yield _sse("start", {
                "message_id": new_msg_id,
                "parent_id": effective_persist_parent_id,
            })

            # API-backed generation.
            try:
                from server.aer.rollover import MAX_OUTPUT_TOKENS as _MAX
                ctx = build_messages_for_generation(
                    chat=chat,
                    messages_tree=msgs_container.messages,
                    contact=contact,
                    user=user,
                    scenario=scenario,
                    settings=settings,
                    libraries=libraries,
                    is_greeting=is_greeting_for_api,
                    is_deletion=False,
                    max_output_tokens=_MAX,
                    regen_parent_id=chosen_parent_id,
                    is_regen=is_regen,
                    is_mobile=is_mobile,
                    mode=mode,
                )
            except BrainBudgetExceeded as e:
                yield _sse("error", {
                    "message": str(e),
                    "kind": "brain_budget",
                    "total": e.total,
                    "cap": e.cap,
                    "offenders": _attribute_brain_offenders(
                        e.offenders,
                        chat=chat,
                        contact=contact,
                        user=user,
                        scenario=scenario,
                        libraries=libraries,
                        messages=msgs_container.messages,
                    ),
                })
                return

            try:
                prompt = render(
                    ctx.api_messages,
                    add_generation_prompt=True,
                    continue_mode=(mode == "continue"),
                )
            except Exception as e:
                yield _sse("error", {"message": f"Failed to render prompt: {e}"})
                return
            # Pre-seed the assistant turn with the speaker's "Name:" header
            # so the model can't drift into speaking as the wrong side. The
            # seed is mirrored into the parser's buffer below so the regex
            # still matches the first bubble. Continue mode also uses the
            # seed — the prompt ends on ``  Emotion: …\n`` and we force
            # the next bubble's prefix so the parser doesn't have to hope
            # the model picks the right format. Impersonate flips the
            # speaker to the user persona.
            seed_name = user.name if mode == "impersonate" else contact.name
            seed = f"{seed_name}:"
            prompt += seed

            _gen_owners = _build_brain_owner_index(
                chat, contact, user, scenario, libraries, msgs_container.messages,
            )
            yield _sse("context", {
                "total_tokens": ctx.total_tokens,
                "messages_in_context": max(0, len(ctx.new_path_ids) - ctx.new_cursor),
                "messages_total": len(ctx.new_path_ids),
                "oldest_in_context_id":
                    ctx.new_path_ids[ctx.new_cursor]
                    if 0 <= ctx.new_cursor < len(ctx.new_path_ids) else None,
                "active_brains": _attribute_active_brains(ctx.active_brains, _gen_owners),
                # In-flight gen — the set IS what's being used right now, not
                # a preview or a stale snapshot. The frontend stamps it as
                # "this generation"; once the assistant message persists, the
                # next /context-tokens fetch reads the same set back from
                # message provenance and flips this to true.
                "active_brains_from_last_gen": False,
            })
            yield _sse("prompt", {"text": prompt, "tokens": ctx.total_tokens})

            from server.aer.rollover import MAX_OUTPUT_TOKENS
            params = CompletionParams(
                model=target.model,
                temperature=preset.temperature,
                top_p=preset.top_p,
                top_k=preset.top_k,
                min_p=preset.min_p,
                max_tokens=MAX_OUTPUT_TOKENS,
            )

            parser_name = user.name if mode == "impersonate" else contact.name
            parser = StreamingBubbleParser(parser_name)
            # Mirror the prompt seed so the parser/log start in the same state
            # the model sees — i.e. with "Name:" already present in the buffer.
            parser.feed(seed)
            raw_text = seed

            async def disconnect_watch():
                try:
                    while not cancel.is_set():
                        if await request.is_disconnected():
                            cancel.set()
                            return
                        await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    return

            watch_task = asyncio.create_task(disconnect_watch())
            try:
                try:
                    with proxy_rules_scope(rules_for_request):
                        async for delta in stream_completion(
                            endpoint_url=settings.endpoint_url,
                            api_token=target.token,
                            prompt=prompt,
                            params=params,
                            stop_event=cancel,
                        ):
                            if cancel.is_set():
                                break
                            raw_text += delta
                            new_bubbles = parser.feed(delta)
                            for b in new_bubbles:
                                bubbles.append(b)
                                yield _sse("bubble", {
                                    "text": b.text,
                                    "emotion": b.emotion.value,
                                })
                            # Heartbeat: emit a ``token`` event when a delta
                            # arrives after at least one bubble has been
                            # yielded but no new bubble completed this delta.
                            # The client uses this to flush its buffered
                            # bubble — knowing more text is on its way.
                            if not new_bubbles and bubbles:
                                yield _sse("token", {})
                except InferenceError as e:
                    yield _sse("error", {"message": str(e), "kind": "inference"})
                    return
                except NoMatchingProxyRule as e:
                    yield _sse("error", {"message": str(e), "kind": "proxy"})
                    return
            finally:
                watch_task.cancel()

            cancelled = cancel.is_set()
            if not cancelled:
                for b in parser.end():
                    bubbles.append(b)
                    yield _sse("bubble", {
                        "text": b.text,
                        "emotion": b.emotion.value,
                    })

            # Persist BEFORE the diagnostic yields. A client disconnect
            # raises on the next ``yield``; persisting first guarantees the
            # message hits disk regardless of whether SSE delivery is still
            # alive. Wrapped in its own try so a persist failure doesn't
            # bypass the rest of the stream — the error path below picks
            # it up and surfaces a typed event.
            persist_error: str | None = None
            if bubbles:
                try:
                    async with storage.lock(f"chat:{chat_id}"):
                        # Pre-attribute the provenance so the stored shape
                        # carries owner names as they were at gen time
                        # (honest snapshot — survives entity renames /
                        # deletions).
                        _persist_owners = _build_brain_owner_index(
                            chat, contact, user, scenario, libraries,
                            msgs_container.messages,
                        )
                        _persist_assistant_message(
                            chat_id=chat_id, chat=chat,
                            msgs_container=msgs_container,
                            new_msg_id=new_msg_id,
                            chosen_parent_id=(
                                continue_persist_parent_id
                                if mode == "continue"
                                else chosen_parent_id
                            ),
                            bubbles=bubbles, contact_name=contact.name,
                            new_cursor=ctx.new_cursor,
                            new_path_ids=ctx.new_path_ids,
                            new_rolled_over=ctx.new_rolled_over,
                            context_tokens=ctx.total_tokens,
                            active_brains_payload=_attribute_active_brains(
                                ctx.active_brains, _persist_owners,
                            ),
                            origin="aer",
                            generation_started_at=started_at_wall,
                            generation_duration_seconds=time.monotonic() - started_at_mono,
                            provider="aetherroom",
                            model=target.model,
                            generation_preset_id=preset.id,
                            context_preset_id=None,
                            seed_bubbles=continue_seed_bubbles,
                            sender="user" if mode == "impersonate" else "contact",
                            sender_name=(
                                user.name if mode == "impersonate" else contact.name
                            ),
                        )
                except Exception as e:  # pragma: no cover — defence in depth
                    log.exception("persist failed for chat %s", chat_id)
                    persist_error = str(e)

            yield _sse("completion", {"text": raw_text})

            yield _sse("parse_summary", {
                "proper_bubbles": parser.proper_count,
                "format_error": parser.format_error,
                "cancelled": cancelled,
            })

            if not bubbles and parser.format_error:
                yield _sse("error", {
                    "message": (
                        "The model's response did not parse as AER format. "
                        "Try regenerating, or pick a different preset."
                    ),
                    "kind": "format",
                })
                return

            if persist_error:
                yield _sse("error", {
                    "message": f"Failed to persist message: {persist_error}",
                    "kind": "persist",
                })
                yield _sse("done", {"message_id": None, "cancelled": cancelled})
            elif bubbles:
                yield _sse("done", {"message_id": new_msg_id, "cancelled": cancelled})
            else:
                yield _sse("done", {"message_id": None, "cancelled": cancelled})
        except Exception as e:
            log.exception("generate failed for chat %s", chat_id)
            yield _sse("error", {"message": str(e)})
        finally:
            _release_slot(label)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{chat_id}/cancel-generation")
async def cancel_generation(chat_id: str) -> dict:
    """Flip the per-chat cancel event; the in-flight stream will tear down."""
    _cancel_events[chat_id].set()
    return {"cancelled": True}


@dataclass
class _ContextTokensView:
    """Adapter so the /context-tokens response shape works for both
    AER's ``GenerationContext`` and the Generic builder's output. Only
    the fields the frontend reads are surfaced; ``brain_tokens`` and
    ``active_brains`` are zero / empty for Generic since global brains
    are baked into the preset's system block via macros (not separately
    accounted for)."""
    total_tokens: int
    history_tokens: int
    brain_tokens: int
    system_tokens: int
    new_cursor: int
    new_path_ids: list
    active_brains: list


def _build_context_tokens_generic(
    *,
    chat: Chat,
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: list[BrainLibrary],
    settings,
    target: EffectiveTarget,
    msgs_container,
    active: list[ChatMessage],
    chosen_parent_id: Optional[str],
    is_regen: bool,
    mode: str,
    is_mobile: bool,
) -> _ContextTokensView:
    """Generic-mode equivalent of ``build_messages_for_generation`` for
    the /context-tokens stat — uses Generic's own guesstimator so the
    displayed count matches what a real Generic-mode generation would
    cost. The caller passes the resolved ``target`` (and only calls this when
    it resolved to a Generic provider); falls back to a zero stub when the
    Context Preset object is gone (the chat-view shows ``ctx: 0`` rather than
    500ing).
    """
    preset = storage.get_context_preset(target.context_preset_id)
    if preset is None:
        return _ContextTokensView(0, 0, 0, 0, 0, [], [])

    presets = storage.load_presets().presets
    generation_preset = _resolve_preset(chat, settings, presets)
    if generation_preset is None:
        return _ContextTokensView(0, 0, 0, 0, 0, [], [])

    # Mirror the route's path-truncation logic for regen so the
    # estimate excludes the message being replaced.
    if is_regen:
        if chosen_parent_id is None:
            path: list[ChatMessage] = []
        else:
            by_id = {m.id: m for m in msgs_container.messages}
            if chosen_parent_id in {m.id for m in active}:
                path = []
                for m in active:
                    path.append(m)
                    if m.id == chosen_parent_id:
                        break
            else:
                cur = by_id.get(chosen_parent_id)
                walked: list[ChatMessage] = []
                while cur is not None:
                    walked.append(cur)
                    cur = by_id.get(cur.parent_id) if cur.parent_id else None
                path = list(reversed(walked))
    else:
        path = list(active)

    ctx = build_messages_for_generic(
        chat=chat,
        contact=contact, user=user, scenario=scenario,
        preset=preset, generation_preset=generation_preset,
        history=path, settings=settings, libraries=libraries,
        rollover_cursor=chat.rollover_start_index if chat.rolled_over else 0,
        is_mobile=is_mobile, mode=mode,
        brain_message_role=target.brain_message_role,
    )
    return _ContextTokensView(
        total_tokens=ctx.total_tokens,
        history_tokens=ctx.history_tokens,
        brain_tokens=0,  # Generic bakes brains into the system block.
        system_tokens=ctx.system_tokens,
        new_cursor=ctx.new_cursor,
        new_path_ids=list(ctx.new_path_ids),
        active_brains=[],  # No separate provenance in Generic — brains
                           # live inline in the preset's system block.
    )


@router.get("/{chat_id}/context-tokens")
async def context_tokens(
    chat_id: str,
    parent_id: Optional[str] = Query(default=None),
    greeting: bool = Query(default=False),
    is_mobile: bool = Query(default=False),
    mode: GenerationMode = Query(default="normal"),
) -> dict:
    """Tokenize the prompt for the next-turn context without running inference.

    Mirrors the resolution logic of :func:`generate` so the count reflects
    what a generation from this point would actually cost. Used by the chat
    view to keep the ``ctx: N tokens`` stat fresh when the user edits the
    chat / branches / config without firing a generation. ``mode`` covers
    the same canonical strings as ``/generate`` so the ``ctx: N`` stat is
    honest for ``continue`` / ``impersonate`` preflights too.
    """
    chat = storage.get_chat(chat_id)
    if chat is None:
        raise HTTPException(404, f"chat {chat_id!r} not found")
    contact = storage.get_contact(chat.contact_id)
    user = storage.get_user(chat.user_id)
    if contact is None:
        raise HTTPException(400, "chat references a missing contact")
    if user is None:
        raise HTTPException(400, "chat references a missing user persona")
    scenario = _resolve_chat_scenario(chat, contact)
    libraries = _resolve_chat_libraries(chat)

    settings = storage.load_settings()
    msgs_container = storage.load_chat_messages(chat_id)
    active = get_active_path(chat, msgs_container.messages)

    is_regen = parent_id is not None
    if not is_regen:
        chosen_parent_id = active[-1].id if active else None
    elif parent_id == "":
        chosen_parent_id = None
    else:
        chosen_parent_id = parent_id
    # Mirror the route's continue/impersonate handling so the displayed
    # ``ctx: N tokens`` matches what a real generation would cost.
    if mode == "continue" and active and active[-1].sender == "contact":
        chosen_parent_id = active[-1].id
        is_regen = False
    elif mode == "impersonate":
        chosen_parent_id = active[-1].id if active else None
        is_regen = False
    is_greeting_for_api = chosen_parent_id is None or greeting

    # Branch on the chat's RESOLVED provider (honoring any per-chat override)
    # so the displayed stat matches what THIS mode's builder would actually
    # produce. AER's tokenizer-aware ``build_messages_for_generation`` is
    # meaningless for Generic mode's UTF-8 / 3.35 char-per-token guesstimate
    # (different per-msg framing, different per-block cost), and vice versa.
    # An unresolvable override (deleted provider/preset/token) yields a zero
    # stub — the chat view shows ``ctx: 0`` rather than 500ing.
    try:
        target = resolve_generation_target(chat, settings)
    except GenerationTargetError:
        target = None
    try:
        if target is not None and target.mode == "generic":
            ctx = _build_context_tokens_generic(
                chat=chat, contact=contact, user=user, scenario=scenario,
                libraries=libraries, settings=settings, target=target,
                msgs_container=msgs_container, active=active,
                chosen_parent_id=chosen_parent_id, is_regen=is_regen,
                mode=mode, is_mobile=is_mobile,
            )
        elif target is not None and target.mode == "aetherroom":
            from server.aer.rollover import MAX_OUTPUT_TOKENS as _MAX
            ctx = build_messages_for_generation(
                chat=chat,
                messages_tree=msgs_container.messages,
                contact=contact,
                user=user,
                scenario=scenario,
                settings=settings,
                libraries=libraries,
                is_greeting=is_greeting_for_api,
                is_deletion=False,
                max_output_tokens=_MAX,
                regen_parent_id=chosen_parent_id,
                is_regen=is_regen,
                is_mobile=is_mobile,
                mode=mode,
            )
        else:
            ctx = _ContextTokensView(0, 0, 0, 0, 0, [], [])
    except BrainBudgetExceeded as e:
        raise HTTPException(422, str(e))

    # Counts let the chat view show ``msgs: in_ctx / total`` next to the
    # token stat, and the id of the oldest message still in context lets
    # the view draw a divider above it when the head of the path has been
    # rolled over. ``new_path_ids`` is the full active path; ``new_cursor``
    # is the index from which messages are still in context.
    path_ids = list(ctx.new_path_ids)
    cursor = ctx.new_cursor
    oldest_id = path_ids[cursor] if 0 <= cursor < len(path_ids) else None

    # Per-brain breakdown (powers the ``brains: N (i)`` stat + modal).
    # Honest provenance: when the active path ends in an assistant
    # message, surface the brain set stamped onto THAT message at gen
    # time. Random conditionals would otherwise re-roll on every fetch.
    # When the tail is a user message (or empty), fall back to a fresh
    # activation roll as a preview for the *next* generation.
    active_path = get_active_path(chat, msgs_container.messages)
    last_assistant = next(
        (m for m in reversed(active_path) if m.sender == "contact"),
        None,
    )
    from_last_gen = False
    owners = _build_brain_owner_index(
        chat, contact, user, scenario, libraries, msgs_container.messages,
    )
    if last_assistant is not None and last_assistant.active_brains:
        # Refresh names + ownership from the *current* state so renames
        # propagate into the modal. The stored payload is the fallback
        # for brains whose owning entity has since been deleted — those
        # entries stay at their at-gen labels so the user still sees the
        # provenance for any brain that fired in the previous turn.
        current_names = _build_brain_name_index(
            contact, user, scenario, libraries, msgs_container.messages,
        )
        active_brains = []
        for entry in last_assistant.active_brains:
            refreshed = dict(entry)
            bid = refreshed.get("brain_id")
            owner = owners.get(bid) if bid else None
            in_live_state = bid is not None and bid in current_names
            if owner is not None:
                # Owner still exists — pick up renames.
                refreshed.update(owner)
            if in_live_state:
                refreshed["name"] = current_names[bid]
            else:
                # Brain id no longer resolves anywhere — entity (or just
                # the brain) was deleted since the last gen. Surface a
                # flag so the modal can render a trash marker next to
                # stale entries instead of pretending they're live.
                refreshed["deleted"] = True
            active_brains.append(refreshed)
        from_last_gen = True
    else:
        active_brains = _attribute_active_brains(ctx.active_brains, owners)

    return {
        "total_tokens": ctx.total_tokens,
        "history_tokens": ctx.history_tokens,
        "brain_tokens": ctx.brain_tokens,
        "system_tokens": ctx.system_tokens,
        "messages_in_context": max(0, len(path_ids) - cursor),
        "messages_total": len(path_ids),
        "oldest_in_context_id": oldest_id,
        "active_brains": active_brains,
        # Tells the frontend whether the brain set is provenance from the
        # most recent assistant message in the active path (true) or a
        # fresh activation roll for the next gen (false). Drives the
        # modal copy so users know whether they're looking at a snapshot
        # of the prior turn or a preview of the next.
        "active_brains_from_last_gen": from_last_gen,
    }


def _pick_user_for_contact(contact_id: str):
    """Pick the user persona to pair with ``contact_id`` for a standalone
    deletion response. Resolution order:

    1. The user on the contact's most recently touched chat (the persona this
       contact was last talking to).
    2. Failing that, the user on the most recently touched chat overall
       (the persona the human last picked anywhere).
    3. Failing that, the first user persona in storage.
    """
    chats = storage.list_chats()
    chats.sort(key=lambda c: c.updated_at, reverse=True)
    for chat in chats:
        if chat.contact_id == contact_id:
            user = storage.get_user(chat.user_id)
            if user is not None:
                return user
    for chat in chats:
        user = storage.get_user(chat.user_id)
        if user is not None:
            return user
    users = storage.list_users()
    return users[0] if users else None


@contacts_router.get("/{contact_id}/deletion-response")
async def contact_deletion_response(
    contact_id: str,
    request: Request,
    is_mobile: bool = Query(default=False),
) -> StreamingResponse:
    """Stream a one-shot in-character farewell from the given contact, without
    requiring a backing chat. Used by the contact delete-confirmation modal so
    the response works for freshly-imported contacts too. Not persisted."""
    label = f"deletion:{contact_id}"
    if not _claim_slot(label):
        return _error_stream(_BUSY_MESSAGE, "busy")

    handed_off_to_stream = False
    try:
        contact = storage.get_contact(contact_id)
        if contact is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        user = _pick_user_for_contact(contact_id)
        if user is None:
            raise HTTPException(400, "no user persona available for deletion response")
        settings = storage.load_settings()
        # Deletion responses always go through the AER pipeline regardless of
        # ``provider_mode`` — Generic doesn't have a deletion analogue (no
        # contact persona conventions, no synthetic-chat construct). If AER
        # isn't configured (no token), there's no deletion response.
        if not (settings.api_token and settings.endpoint_url):
            return _error_stream(
                "Deletion responses require AetherRoom to be configured "
                "(endpoint URL + API token). Set them in Settings, or skip "
                "the deletion message.",
                "no_aer_configured",
            )
        presets = storage.load_presets().presets
        preset = presets[0] if presets else None
        if settings.default_preset_id:
            for p in presets:
                if p.id == settings.default_preset_id:
                    preset = p
                    break
        if preset is None:
            raise HTTPException(500, "No generation presets configured.")

        # Synthetic chat — never persisted, just carries intimacy/style/cjk
        # defaults into ``build_messages_for_generation`` (which only consults
        # those fields when building a deletion context — no history is included).
        synthetic_chat = Chat(
            contact_id=contact.id,
            user_id=user.id,
            intimacy=contact.default_intimacy,
            style=contact.default_style,
            response_length=contact.default_response_length,
            cjk=contact.cjk or user.cjk,
        )

        try:
            from server.aer.rollover import MAX_OUTPUT_TOKENS as _MAX
            ctx = build_messages_for_generation(
                chat=synthetic_chat,
                messages_tree=[],
                contact=contact,
                user=user,
                scenario=None,
                settings=settings,
                is_deletion=True,
                max_output_tokens=_MAX,
                is_mobile=is_mobile,
            )
        except BrainBudgetExceeded as e:
            raise HTTPException(400, str(e))

        prompt = render(ctx.api_messages, add_generation_prompt=True)
        # Same "Name:" seed as the main /generate endpoint — keeps the model from
        # speaking as the user.
        seed = f"{contact.name}:"
        prompt += seed
        from server.aer.rollover import MAX_OUTPUT_TOKENS

        params = CompletionParams(
            model=settings.default_model,
            temperature=preset.temperature,
            top_p=preset.top_p,
            top_k=preset.top_k,
            min_p=preset.min_p,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        parser = StreamingBubbleParser(contact.name)
        parser.feed(seed)
        cancel = asyncio.Event()
        handed_off_to_stream = True
    finally:
        if not handed_off_to_stream:
            _release_slot(label)

    rules_for_request = getattr(request.app.state, "proxy_rules", None)

    async def stream():
        try:
            with proxy_rules_scope(rules_for_request):
                async for delta in stream_completion(
                    endpoint_url=settings.endpoint_url,
                    api_token=resolve_llm_token("aer", settings) or "",
                    prompt=prompt,
                    params=params,
                    stop_event=cancel,
                ):
                    if await request.is_disconnected():
                        cancel.set()
                        break
                    for b in parser.feed(delta):
                        yield _sse("bubble", {"text": b.text, "emotion": b.emotion.value})
                for b in parser.end():
                    yield _sse("bubble", {"text": b.text, "emotion": b.emotion.value})
                yield _sse("done", {"cancelled": cancel.is_set()})
        except InferenceError as e:
            yield _sse("error", {"message": str(e), "kind": "inference"})
        except NoMatchingProxyRule as e:
            yield _sse("error", {"message": str(e), "kind": "proxy"})
        finally:
            _release_slot(label)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
