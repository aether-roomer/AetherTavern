"""Two-tier rollover trimming with brain immunity.

The trim cursor (an index into the chat's active path) is the position from
which user/assistant messages are visible. Brain entries from messages *before*
the cursor still appear, accumulating before the first surviving message.

Reads inputs only — does not mutate the chat. The caller persists the new
cursor / path-ids / rolled_over fields after a successful generation.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

log = logging.getLogger("aether.rollover")

from server.aer.activation import ChatFacts, activate_brains, is_conditional
from server.aer.format import (
    build_deletion_style_instruction,
    build_local_brain_block,
    build_style_instruction,
    build_system_prompt,
    collect_conditional_global_brains,
    collect_global_brains,
    collect_unconditional_global_brains,
    format_message,
)
from server.aer.macros import apply_macros
from server.aer.template import count_tokens, render_and_count
from server.models import (
    EMPTY_SENTINEL,
    ROOT_PARENT_KEY,
    Brain,
    BrainLibrary,
    Chat,
    ChatMessage,
    Contact,
    ContactScenario,
    Intimacy,
    ResponseLength,
    Scenario,
    Settings,
    Style,
    User,
)


BRAIN_PREFIX = "----"
START_OF_CHAT = "Start of chat."

# Conditional brains relocate to a single system message whose tail sits this
# many tokens before the end of the rendered prompt. The intent is cache
# stability: flipping a brain's activation only invalidates the last
# ~CONDITIONAL_BRAIN_TARGET_TOKENS of the KV cache, not the whole history.
# (The relocated block itself sits *above* the tail, so the trailing 2k tokens
# stay the same modulo the brain content's own length.)
CONDITIONAL_BRAIN_TARGET_TOKENS = 2048

# Named context-window presets exposed to users in Settings. Tuned for
# Opus/Scroll/Tablet model variants the user runs.
CONTEXT_PRESETS: dict[str, dict[str, int]] = {
    "opus":   {"base_context_size": 28672, "rollover_window": 8192},
    "scroll": {"base_context_size": 12288, "rollover_window": 4096},
    "tablet": {"base_context_size":  8192, "rollover_window": 4096},
}

# Hardcoded — the AER format reserves a 1536-token response budget on every
# call so the rollover trim leaves room for a full reply. Not user-tunable.
MAX_OUTPUT_TOKENS = 1536

# GLM-4.6 BOS prefix ([gMASK]<sop>) + the generation-prompt suffix
# (<|assistant|>\n<think></think>\n) bracket the prompt outside any single
# message. The trailing \n on the suffix is added by template.render so the
# model's first generated token is real content rather than a separator.
# Counted once and cached.
_FRAMING_BOS = "[gMASK]<sop>"
_FRAMING_GEN_SUFFIX = "<|assistant|>\n<think></think>\n"

# GLM-4.6 chat-template framing reproduced for per-message token estimation.
_FRAMING_SYSTEM_FIRST = "<|system|>"          # AER patch: no \n
_FRAMING_SYSTEM = "<|system|>\n"
_FRAMING_USER = "<|user|>\n"
_FRAMING_ASSISTANT = "<|assistant|>\n<think></think>\n"


def context_limits(settings: "Settings") -> tuple[int, int]:
    """Return ``(base_context_size, rollover_window)`` for the active preset."""
    cfg = CONTEXT_PRESETS.get(settings.context_preset, CONTEXT_PRESETS["opus"])
    return cfg["base_context_size"], cfg["rollover_window"]


# ---------------------------------------------------------------------------
# Active path traversal
# ---------------------------------------------------------------------------


def get_active_path(chat: Chat, messages: list[ChatMessage]) -> list[ChatMessage]:
    """Walk the message tree following ``selected_child_id``.

    Falls back to the most recent sibling when no selection is set; bails on
    ``EMPTY_SENTINEL`` (soft-deleted branch).
    """
    by_id: dict[str, ChatMessage] = {m.id: m for m in messages}
    by_parent: dict[str | None, list[ChatMessage]] = {}
    for m in messages:
        by_parent.setdefault(m.parent_id, []).append(m)
    for siblings in by_parent.values():
        siblings.sort(key=lambda m: m.timestamp)

    path: list[ChatMessage] = []
    parent_id: str | None = None
    while True:
        key = parent_id if parent_id is not None else ROOT_PARENT_KEY
        siblings = by_parent.get(parent_id, [])
        if not siblings:
            break
        chosen_id = chat.selected_child_id.get(key)
        if chosen_id == EMPTY_SENTINEL:
            break
        if chosen_id is None:
            chosen = siblings[-1]
        else:
            chosen = by_id.get(chosen_id) or siblings[-1]
        path.append(chosen)
        parent_id = chosen.id
    return path


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BrainBudgetExceeded(Exception):
    """Total brain tokens exceed ``base_context_size / 2.5``.

    ``offenders`` is the top-N (largest first) breakdown as a list of
    ``(name, tokens, brain_id)``. ``brain_id`` is None for synthetic
    entries that don't correspond to a single brain (e.g. an activated-
    conditional block in the after-splice check).
    """

    def __init__(
        self,
        total: int,
        cap: int,
        offenders: list[tuple[str, int, str | None]],
    ):
        super().__init__(
            f"Brain budget exceeded: {total} > {cap}. "
            f"Largest offenders: {offenders}"
        )
        self.total = total
        self.cap = cap
        self.offenders = offenders


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ActiveBrain:
    """One brain that contributed to the rendered prompt this turn.

    Surface for the frontend "brains: N (i)" stat — clicking the (i)
    opens a breakdown modal grouped by source. ``brain_id`` is the
    Brain.id (uniquely identifies it across the whole chat so the
    router can attribute it to a contact / user / scenario / library /
    chat message). ``tokens`` is the brain's solo-block cost
    (``----\\n{name}\\n{content}\\n``).
    """
    brain_id: str
    name: str
    tokens: int
    conditional: bool  # True if this brain activated via keys/advanced; False if always-on


@dataclass
class GenerationContext:
    api_messages: list[dict]
    total_tokens: int
    history_tokens: int
    brain_tokens: int
    system_tokens: int
    new_cursor: int
    new_path_ids: list[str]
    new_rolled_over: bool
    # Per-brain breakdown of what actually shipped this turn.
    active_brains: list[ActiveBrain] = field(default_factory=list)
    # Reminder-brain tokens. Tracked separately from ``brain_tokens``:
    # reminders don't count toward the brain-budget cap (they're a tail
    # instruction, not a lore block), but they DO count toward the total
    # context budget so they can still trigger history trimming.
    reminder_tokens: int = 0


# ---------------------------------------------------------------------------
# Per-message token estimation
# ---------------------------------------------------------------------------


def _solo_tokens(api_msg: dict, *, is_first: bool = False) -> int:
    """Token count of a single message's contribution to the rendered prompt."""
    role = api_msg["role"]
    content = api_msg["content"]
    if role == "system":
        framing = _FRAMING_SYSTEM_FIRST if is_first else _FRAMING_SYSTEM
        # AER patch removes \n only after the FIRST <|system|>; for is_first the
        # framing already excludes \n.
    elif role == "user":
        framing = _FRAMING_USER
    elif role == "assistant":
        # GLM-4.6 strips assistant content in the chat template.
        content = content.strip()
        framing = _FRAMING_ASSISTANT
    else:
        framing = ""
    return count_tokens(framing + content)


def _is_brain(api_msg: dict) -> bool:
    return api_msg["role"] == "system" and api_msg["content"].startswith(BRAIN_PREFIX)


def _is_history(api_msg: dict) -> bool:
    return api_msg["role"] in ("user", "assistant")


def _brain_block_tokens(brain) -> int:
    """Tokens for a single brain rendered as ``----\\n{name}\\n{content}\\n``."""
    content = (brain.content or "").strip()
    return count_tokens(f"----\n{brain.name}\n{content}\n")


def compute_global_brain_tokens(
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: Iterable[BrainLibrary] | None = None,
) -> int:
    return sum(_brain_block_tokens(b) for b in collect_global_brains(contact, user, scenario, libraries))


def _splice_reminder_brain(
    api_messages: list[dict],
    contact: Contact,
    *,
    user: User,
    scenario: Scenario | None,
    chat: Chat | None,
    contact_scenario: ContactScenario | None,
    active_path: list[ChatMessage],
    messages_tree: list[ChatMessage],
    rollover_cursor: int,
    settings: Settings | None,
    is_mobile: bool,
    generation_type: str | None,
    libraries: list[BrainLibrary] | None = None,
    splice_role: str = "system",
    count_tokens_fn: Callable[[dict, bool], int] | None = None,
) -> int:
    """If the contact has a non-disabled reminder brain, insert its content
    as a system message ``depth`` user/assistant messages before the
    style marker. Returns the token cost of the spliced message, or 0
    if nothing was added.

    Depth counts **only user / assistant messages** — interleaved system
    messages (brain blocks, "Start of chat.") are stepped over but
    don't consume depth. ``depth=0`` puts the reminder immediately
    before the style marker; ``depth=N`` places it so that exactly N
    user/assistant turns sit between the reminder and the style
    marker. If history has fewer than N user/assistant turns the
    reminder lands at the post-header floor (index 1).

    Merge rule: when the reminder lands directly adjacent to an existing
    brain block (a system message whose body starts with
    :data:`BRAIN_PREFIX`), its content is folded into that brain block
    rather than inserted as a separate system message — keeps the
    chat-template output from emitting two ``<|system|>`` blocks
    back-to-back for what is semantically one chunk of system content.
    The merge only happens against brain blocks; other system messages
    (style marker, "Start of chat.") are never merged into.

    Macros (``{{user}}``, ``{{char}}``, etc.) are expanded against the
    same context the AER system prompt uses; if expansion leaves the
    content empty, the splice is skipped.
    """
    reminder = contact.reminder_brain
    if reminder is None or reminder.disabled:
        return 0

    def _cost(api_msg: dict) -> int:
        if count_tokens_fn is not None:
            return count_tokens_fn(api_msg, False)
        return _solo_tokens(api_msg)

    content = apply_macros(
        reminder.content or "",
        contact,
        user,
        scenario=scenario,
        contact_scenario=contact_scenario,
        chat=chat,
        active_path=active_path,
        messages_tree=messages_tree,
        rollover_cursor=rollover_cursor,
        settings=settings,
        is_mobile=is_mobile,
        generation_type=generation_type,
        libraries=libraries,
    ).strip()
    if not content:
        return 0
    n = len(api_messages)
    if n == 0:
        return 0
    # Style marker = last system message in the list.
    style_marker_index = n - 1
    if api_messages[style_marker_index].get("role") != "system":
        style_marker_index = n

    depth = max(0, min(10, int(reminder.depth)))
    if depth == 0:
        target = style_marker_index
    else:
        # Walk backward, counting only user/assistant messages. Land
        # just before the (depth)-th such message; floor at 1 so the
        # AER system header at index 0 stays put.
        target = 1
        seen = 0
        for i in range(style_marker_index - 1, 0, -1):
            if api_messages[i].get("role") in ("user", "assistant"):
                seen += 1
                if seen >= depth:
                    target = i
                    break
    target = max(1, min(target, style_marker_index))

    # Merge into an immediately preceding brain block if there is one. Brain
    # blocks keep whatever role they were inserted with, so ``splice_role``
    # doesn't override merges — only the standalone-insert path below uses it.
    prev_index = target - 1
    if prev_index >= 0 and _is_brain(api_messages[prev_index]):
        prev = api_messages[prev_index]
        merged_content = (prev["content"] or "") + "\n\n" + content
        new_tokens = _cost({"role": prev["role"], "content": merged_content})
        prev_tokens = _cost(prev)
        prev["content"] = merged_content
        return new_tokens - prev_tokens

    # Or merge into an immediately following brain block.
    if target < len(api_messages) and _is_brain(api_messages[target]):
        nxt = api_messages[target]
        merged_content = content + "\n\n" + (nxt["content"] or "")
        new_tokens = _cost({"role": nxt["role"], "content": merged_content})
        nxt_tokens = _cost(nxt)
        nxt["content"] = merged_content
        return new_tokens - nxt_tokens

    # No adjacent brain block — insert as its own message using the
    # caller-supplied role (default "system" for AER; Generic mode passes
    # whatever ``settings.generic.<provider>.brain_message_role`` is set to).
    msg = {"role": splice_role, "content": content}
    api_messages.insert(target, msg)
    return _cost(msg)


def _find_splice_index(msg_tokens: list[int], target_tokens: int) -> int:
    """Index at which to insert the relocated conditional-brain block.

    Walks the api_messages list from the end, accumulating per-message tokens
    until the running tail reaches ``target_tokens``. Returns the index of the
    first message in that tail — inserting there positions the new system
    message just *before* the trailing ``target_tokens`` of context.

    Floors at 1 so the AER system prompt at index 0 stays put.
    """
    accumulated = 0
    n = len(msg_tokens)
    if n <= 1:
        return n
    for i in range(n - 1, 0, -1):
        accumulated += msg_tokens[i]
        if accumulated >= target_tokens:
            return i
    return 1


# ---------------------------------------------------------------------------
# api_messages assembly
# ---------------------------------------------------------------------------


def _build_api_messages(
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    *,
    intimacy: Intimacy,
    style: Style,
    response_length: ResponseLength | None,
    cjk: bool,
    chat_tags: str,
    path: list[ChatMessage],
    cursor: int,
    is_greeting: bool,
    is_deletion: bool,
    libraries: list[BrainLibrary] | None = None,
    chat: Chat | None = None,
    contact_scenario: "ContactScenario | None" = None,
    messages_tree: list[ChatMessage] | None = None,
    settings: Settings | None = None,
    is_mobile: bool = False,
    generation_type: str | None = None,
    flip_personas: bool = False,
    flip_roles: bool = False,
) -> tuple[list[dict], list[Brain]]:
    """Build the api_messages list as it would look WITHOUT any conditional
    per-message brain. Conditional per-message brains are held back and
    returned as the second tuple element in message-encounter order so the
    caller can fold them into the activation pool."""
    api_messages: list[dict] = []
    conditional_msg_brains: list[Brain] = []

    # [0] AER system prompt.
    api_messages.append({
        "role": "system",
        "content": build_system_prompt(
            contact, user, scenario, intimacy,
            chat_tags=chat_tags,
            include_scenario=not is_deletion,
            libraries=libraries,
            chat=chat,
            contact_scenario=contact_scenario,
            settings=settings,
            active_path=path,
            messages_tree=messages_tree or [],
            rollover_cursor=cursor,
            is_mobile=is_mobile,
            generation_type=generation_type,
            flip_personas=flip_personas,
        ),
    })

    # [1] "Start of chat." — only emitted before any rollover has trimmed
    # the head of the conversation; once the cursor moves the marker is
    # omitted so the model doesn't think we've returned to the beginning.
    if cursor == 0:
        api_messages.append({"role": "system", "content": START_OF_CHAT})

    if is_deletion:
        api_messages.append({
            "role": "system",
            "content": build_deletion_style_instruction(cjk),
        })
        return api_messages, conditional_msg_brains

    if is_greeting:
        api_messages.append({
            "role": "system",
            "content": build_style_instruction(
                style, response_length, cjk, is_greeting=True,
            ),
        })
        return api_messages, conditional_msg_brains

    # History from path. Brains are *always* included regardless of cursor;
    # user/assistant bodies only appear from cursor onwards. Each message's
    # brains are split: unconditional ones go inline, and conditional ones
    # are diverted to the activation pool.
    for i, msg in enumerate(path):
        live = [b for b in msg.brains if not b.disabled]
        uncond = [b for b in live if not is_conditional(b)]
        cond = [b for b in live if is_conditional(b)]
        if uncond:
            api_messages.append({
                "role": "system",
                "content": build_local_brain_block(uncond),
            })
        if cond:
            conditional_msg_brains.extend(cond)
        if i >= cursor:
            if flip_roles:
                # Impersonate: each side speaks under the OTHER's name and
                # role. Contact-side bubbles become user messages (named
                # after the contact, no emotion line); user-side bubbles
                # become assistant messages (named after the user, emotion
                # defaulted to neutral if missing).
                if msg.sender == "user":
                    role = "assistant"
                    display_name = user.name
                    force_emotion = True
                else:
                    role = "user"
                    display_name = contact.name
                    force_emotion = False
                api_messages.append({
                    "role": role,
                    "content": format_message(
                        msg,
                        display_sender_name=display_name,
                        emit_emotion=force_emotion,
                        default_neutral_emotion=force_emotion,
                    ),
                })
            else:
                api_messages.append({
                    "role": "assistant" if msg.sender == "contact" else "user",
                    "content": format_message(msg),
                })

    # Style instruction is always last.
    api_messages.append({
        "role": "system",
        "content": build_style_instruction(style, response_length, cjk, is_greeting=False),
    })

    return api_messages, conditional_msg_brains


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def build_messages_for_generation(
    chat: Chat,
    messages_tree: Iterable[ChatMessage],
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    settings: Settings,
    *,
    libraries: list[BrainLibrary] | None = None,
    is_greeting: bool = False,
    is_deletion: bool = False,
    max_output_tokens: int = 1536,
    regen_parent_id: str | None = None,
    is_regen: bool = False,
    is_mobile: bool = False,
    mode: str = "normal",
) -> GenerationContext:
    """Build the API message list with rollover trimming + brain budget enforcement.

    ``regen_parent_id`` is the id of the message the *new* assistant turn will
    be parented to. The active path is truncated at this id so the prompt
    doesn't include the message currently being replaced (or its descendants).
    Pass ``None`` to use the full active path (extend / deletion contexts).

    ``is_regen=True`` forces the truncation logic even when ``regen_parent_id``
    is ``None`` — used by root-level regens (e.g. greeting reroll) where the
    new turn has no parent and the prior root message must NOT bleed into
    context.
    """
    cjk = chat.cjk
    intimacy = chat.intimacy
    style = chat.style
    response_length = chat.response_length
    chat_tags = chat.tags

    contact_scenario: ContactScenario | None = None
    if chat.contact_scenario_id:
        contact_scenario = next(
            (c for c in contact.scenarios if c.id == chat.contact_scenario_id),
            None,
        )
    # Canonical {{lastgenerationtype}} string. Continue / Impersonate
    # are explicit modes; otherwise fall back to the regen heuristic.
    if mode in ("continue", "impersonate"):
        generation_type = mode
    else:
        generation_type = "swipe" if is_regen else "normal"
    flip_personas = mode == "impersonate"
    flip_roles = mode == "impersonate"

    messages_tree = list(messages_tree)
    full_path = get_active_path(chat, messages_tree)
    if regen_parent_id is None and not is_regen:
        path = full_path
    else:
        # Truncate the active path at and including the regen-parent. If the
        # parent isn't on the active path (rare — user picked a parent from a
        # non-active branch), walk up from it via parent_id to root.
        truncated: list[ChatMessage] = []
        found = False
        for m in full_path:
            truncated.append(m)
            if m.id == regen_parent_id:
                found = True
                break
        if not found:
            by_id = {m.id: m for m in messages_tree}
            chain: list[ChatMessage] = []
            cur_id: str | None = regen_parent_id
            while cur_id is not None and cur_id in by_id:
                m = by_id[cur_id]
                chain.append(m)
                cur_id = m.parent_id
            truncated = list(reversed(chain))
        path = truncated
    current_path_ids = [m.id for m in path]

    # Validate cached rollover state: if the path prefix above the cursor
    # changed (branch switch), invalidate.
    cursor = chat.rollover_start_index if chat.rolled_over else 0
    if chat.rolled_over and cursor > 0:
        cached_prefix = chat.rollover_path_ids[:cursor]
        live_prefix = current_path_ids[:cursor]
        if cached_prefix != live_prefix or len(live_prefix) < cursor:
            cursor = 0

    # Greeting / deletion contexts don't trim — return immediately. We still
    # run the conditional-brain activation engine so brains can fire on facts
    # alone (relationship, style, …), but those contexts have very short
    # ``base_text`` so keyword matches against history rarely apply.
    if is_greeting or is_deletion:
        api_messages, conditional_msg_brains = _build_api_messages(
            contact, user, scenario,
            intimacy=intimacy, style=style, response_length=response_length, cjk=cjk,
            chat_tags=chat_tags,
            path=path, cursor=0,
            is_greeting=is_greeting, is_deletion=is_deletion,
            libraries=libraries,
            chat=chat,
            contact_scenario=contact_scenario,
            messages_tree=messages_tree,
            settings=settings,
            is_mobile=is_mobile,
            generation_type=generation_type,
            flip_personas=flip_personas,
            flip_roles=flip_roles,
        )
        activated_out: list[Brain] = []
        block_tokens = _maybe_splice_conditional_block(
            api_messages, path,
            contact=contact, user=user, scenario=scenario,
            intimacy=intimacy, style=style, response_length=response_length, cjk=cjk,
            chat_tags=chat_tags, is_deletion=is_deletion,
            conditional_msg_brains=conditional_msg_brains,
            settings=settings,
            libraries=libraries,
            activated_out=activated_out,
        )
        reminder_tokens = _splice_reminder_brain(
            api_messages, contact,
            user=user,
            scenario=None if is_deletion else scenario,
            chat=chat,
            contact_scenario=contact_scenario,
            active_path=path,
            messages_tree=messages_tree,
            rollover_cursor=0,
            settings=settings,
            is_mobile=is_mobile,
            generation_type=generation_type,
            libraries=libraries,
        )
        _, total = render_and_count(api_messages)
        # Build the active-brains breakdown: unconditional globals + per-message
        # unconditional (always shipped) + activated conditionals (just fired).
        active_brains = _collect_active_brains(
            contact, user, scenario, libraries, path, activated_out, is_deletion,
        )
        return GenerationContext(
            api_messages=api_messages,
            total_tokens=total,
            history_tokens=0,
            brain_tokens=block_tokens,
            system_tokens=_solo_tokens(api_messages[0], is_first=True),
            new_cursor=0,
            new_path_ids=current_path_ids,
            new_rolled_over=False,
            active_brains=active_brains,
            reminder_tokens=reminder_tokens,
        )

    api_messages, conditional_msg_brains = _build_api_messages(
        contact, user, scenario,
        intimacy=intimacy, style=style, response_length=response_length, cjk=cjk,
        chat_tags=chat_tags,
        path=path, cursor=cursor,
        is_greeting=False, is_deletion=False,
        libraries=libraries,
        chat=chat,
        contact_scenario=contact_scenario,
        messages_tree=messages_tree,
        settings=settings,
        is_mobile=is_mobile,
        generation_type=generation_type,
        flip_personas=flip_personas,
        flip_roles=flip_roles,
    )

    # Per-message token costs. The first system message uses the AER patch
    # (no \n after <|system|>); subsequent system messages do not.
    msg_tokens: list[int] = [
        _solo_tokens(m, is_first=(i == 0)) for i, m in enumerate(api_messages)
    ]

    system_tokens = msg_tokens[0]
    local_brain_tokens = sum(t for m, t in zip(api_messages, msg_tokens) if _is_brain(m))
    history_tokens = sum(t for m, t in zip(api_messages, msg_tokens) if _is_history(m))

    # Brain budget cap (unconditional only — conditional brains are checked
    # later, after we know which of them activate). Prevents the user from
    # saving so many always-on brains that the chat history is starved.
    unconditional_global_brains = collect_unconditional_global_brains(contact, user, scenario, libraries)
    breakdown: list[tuple[str, int, str | None]] = [
        (b.name or "(unnamed)", _brain_block_tokens(b), b.id)
        for b in unconditional_global_brains
    ]
    # Per-message unconditional brains — walked directly off the path rather
    # than parsed back out of api_messages content, so we get per-brain ids
    # (the inline block joins multiple brains; api_messages tokens are
    # block-level, not per-brain). Sum across breakdown may diverge from the
    # block-level ``local_brain_tokens`` by a few framing tokens — that's
    # fine for *display*; ``local_brain_tokens`` remains the source of truth
    # for the cap check.
    for msg in path:
        for b in msg.brains:
            if b.disabled or is_conditional(b):
                continue
            breakdown.append((b.name or "(unnamed)", _brain_block_tokens(b), b.id))
    unconditional_brain_tokens = sum(_brain_block_tokens(b) for b in unconditional_global_brains) + local_brain_tokens
    base_context_size, _ = context_limits(settings)
    brain_cap = int(base_context_size / 2.5)
    if unconditional_brain_tokens > brain_cap:
        offenders = sorted(breakdown, key=lambda kv: -kv[1])[:5]
        raise BrainBudgetExceeded(unconditional_brain_tokens, brain_cap, offenders)

    # ``brain_tokens`` accumulates as we splice the conditional block below;
    # for now it counts only the unconditional per-message brains, since
    # unconditional globals are already inside ``system_tokens``.
    brain_tokens = local_brain_tokens

    base_context_size, rollover_window = context_limits(settings)
    hard_limit = base_context_size + rollover_window
    soft_limit = base_context_size
    lower_limit = base_context_size - rollover_window

    # Compute the reserved budget exactly: max output tokens + the always-on
    # framing pieces (BOS prefix, generation-prompt suffix, the trailing
    # "[ Style: … ]" message, and the "Start of chat." message when present).
    style_tokens = sum(
        msg_tokens[i]
        for i, m in enumerate(api_messages)
        if i == len(api_messages) - 1 and m["role"] == "system"
    )
    soc_tokens = sum(
        msg_tokens[i]
        for i, m in enumerate(api_messages)
        if m["role"] == "system" and m["content"] == START_OF_CHAT
    )
    framing_tokens = count_tokens(_FRAMING_BOS) + count_tokens(_FRAMING_GEN_SUFFIX)
    reserved = max_output_tokens + style_tokens + soc_tokens + framing_tokens

    hard_budget = hard_limit - system_tokens - reserved - brain_tokens
    soft_budget = soft_limit - system_tokens - reserved - brain_tokens

    rolled_over = cursor > 0

    # Stale-state detection: if total < lower limit AND cursor > 0, the
    # cached cursor is too aggressive (probably from a different branch).
    # Reset and rebuild from the start.
    initial_total = sum(msg_tokens)
    if rolled_over and initial_total < lower_limit:
        return build_messages_for_generation(
            chat=chat.model_copy(update={"rollover_start_index": 0, "rolled_over": False}),
            messages_tree=messages_tree,
            contact=contact, user=user, scenario=scenario,
            settings=settings,
            libraries=libraries,
            is_greeting=False, is_deletion=False,
            max_output_tokens=max_output_tokens,
            is_mobile=is_mobile,
        )

    # Trim if the history exceeds the hard budget.
    if history_tokens > hard_budget:
        while history_tokens > soft_budget:
            for i, m in enumerate(api_messages):
                if _is_history(m):
                    history_tokens -= msg_tokens[i]
                    del api_messages[i]
                    del msg_tokens[i]
                    cursor += 1
                    break
            else:
                break  # no more history to drop
        rolled_over = True
        # Once trimmed, drop the "Start of chat." system message.
        for i, m in enumerate(api_messages):
            if m["role"] == "system" and m["content"] == START_OF_CHAT:
                del api_messages[i]
                del msg_tokens[i]
                break

    # Conditional-brain activation runs against the (now-trimmed) base context.
    # If any conditional brain activates, splice the block ~2048 tokens before
    # the end. Raises ``BrainBudgetExceeded`` when the combined uncond+cond
    # brain tokens exceed the cap.
    activated_out: list[Brain] = []
    conditional_block_tokens = _maybe_splice_conditional_block(
        api_messages, path,
        contact=contact, user=user, scenario=scenario,
        intimacy=intimacy, style=style, response_length=response_length, cjk=cjk,
        chat_tags=chat_tags, is_deletion=False,
        conditional_msg_brains=conditional_msg_brains,
        settings=settings,
        libraries=libraries,
        activated_out=activated_out,
    )
    brain_tokens = local_brain_tokens + conditional_block_tokens

    # Reminder brain splice runs AFTER the conditional block has been
    # placed, so the adjacency check sees the post-splice neighbours.
    # Reminder tokens are counted separately and intentionally NOT
    # added to ``brain_tokens`` (the brain-budget cap excludes them).
    reminder_tokens = _splice_reminder_brain(
        api_messages, contact,
        user=user,
        scenario=scenario,
        chat=chat,
        contact_scenario=contact_scenario,
        active_path=path,
        messages_tree=messages_tree,
        rollover_cursor=cursor,
        settings=settings,
        is_mobile=is_mobile,
        generation_type=generation_type,
        libraries=libraries,
    )

    # Recompute the actual total via full chat-template render (per-msg sum is
    # an approximation — boundaries can shift token IDs by 1-2).
    _, total_tokens = render_and_count(api_messages)

    active_brains = _collect_active_brains(
        contact, user, scenario, libraries, path, activated_out, is_deletion=False,
    )

    return GenerationContext(
        api_messages=api_messages,
        total_tokens=total_tokens,
        history_tokens=history_tokens,
        brain_tokens=brain_tokens,
        system_tokens=system_tokens,
        new_cursor=cursor,
        new_path_ids=current_path_ids,
        new_rolled_over=rolled_over,
        active_brains=active_brains,
        reminder_tokens=reminder_tokens,
    )


def _collect_active_brains(
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: list[BrainLibrary] | None,
    path: list[ChatMessage],
    activated_conditionals: list[Brain],
    is_deletion: bool,
) -> list[ActiveBrain]:
    """Build the per-brain breakdown of what shipped this turn.

    Includes:
    - Every unconditional global brain (contact/user/scenario/library).
    - Every unconditional per-message brain on the active path (those
      ride along even when the message body has been rolled out of
      context; see ``_build_api_messages``).
    - Every conditional brain that actually activated (subset of the
      pool returned by ``_maybe_splice_conditional_block``'s
      ``activated_out`` parameter).

    Tokens are the brain's solo-block cost (``----\\n{name}\\n{content}\\n``);
    that's slightly different from the framed-message cost rolled into
    ``ctx.brain_tokens`` but is the right unit for a per-brain display.

    Order: global uncond (contact → user → scenario → libraries) →
    per-message uncond (path order) → activated conditionals (the order
    the activation engine surfaced them, which is "deterministic-priority
    then cascade order"). The order is informational only — the actual
    prompt embeds them at their natural locations (system prompt for
    globals, inline for per-message, spliced block for conditionals).
    """
    out: list[ActiveBrain] = []
    effective_scenario = scenario if not is_deletion else None
    for b in collect_unconditional_global_brains(contact, user, effective_scenario, libraries):
        out.append(ActiveBrain(
            brain_id=b.id,
            name=b.name or "(unnamed)",
            tokens=_brain_block_tokens(b),
            conditional=False,
        ))
    if not is_deletion:
        for msg in path:
            for b in msg.brains:
                if b.disabled or is_conditional(b):
                    continue
                out.append(ActiveBrain(
                    brain_id=b.id,
                    name=b.name or "(unnamed)",
                    tokens=_brain_block_tokens(b),
                    conditional=False,
                ))
    for b in activated_conditionals:
        out.append(ActiveBrain(
            brain_id=b.id,
            name=b.name or "(unnamed)",
            tokens=_brain_block_tokens(b),
            conditional=True,
        ))
    return out


def _maybe_splice_conditional_block(
    api_messages: list[dict],
    path: list[ChatMessage],
    *,
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    intimacy: Intimacy,
    style: Style,
    response_length: ResponseLength | None,
    cjk: bool,
    chat_tags: str,
    is_deletion: bool,
    conditional_msg_brains: list[Brain],
    settings: Settings,
    libraries: list[BrainLibrary] | None = None,
    activated_out: list[Brain] | None = None,
    splice_role: str = "system",
    count_tokens_fn: Callable[[dict, bool], int] | None = None,
    base_context_size_override: int | None = None,
) -> int:
    """Activate conditional brains against the assembled ``base_text`` and, if
    any activate, splice them into ``api_messages`` as a single relocated
    system message ~``CONDITIONAL_BRAIN_TARGET_TOKENS`` tokens before the end.

    Returns the spliced block's solo-token count (0 if nothing activated, or
    nothing fits the remaining budget).

    Unlike the unconditional-brain check upstream, this path **never raises**:
    a turn whose chat content happens to activate an oversized combination of
    conditional brains drops the lowest-priority activations (tail-first) until
    the spliced block fits the remaining budget. The user can't predict every
    activation combination ahead of time, so failing the generation is too
    aggressive — losing one lore entry is better than losing the whole turn.
    Dropped brains are logged at WARNING for diagnosability.

    Mutates ``api_messages`` in place.
    """
    effective_scenario = scenario if not is_deletion else None
    conditional_globals = collect_conditional_global_brains(contact, user, effective_scenario, libraries)
    pool = list(conditional_globals) + list(conditional_msg_brains)
    if not pool:
        return 0

    base_text = "\n".join(m["content"] for m in api_messages)

    raw_tags = [t.strip() for t in (chat_tags or "").split(",") if t.strip()]
    facts = ChatFacts(
        message_count=len(path),
        user_message_count=sum(1 for m in path if m.sender == "user"),
        contact_message_count=sum(1 for m in path if m.sender == "contact"),
        relationship=intimacy,
        style=style,
        length=response_length,
        japanese=cjk,
        tags=raw_tags,
        memory_text=(effective_scenario.description if effective_scenario else "") or "",
    )

    activated = activate_brains(pool, base_text=base_text, facts=facts)
    if not activated:
        return 0

    def _cost(api_msg: dict, is_first: bool = False) -> int:
        if count_tokens_fn is not None:
            return count_tokens_fn(api_msg, is_first)
        return _solo_tokens(api_msg, is_first=is_first)

    # Remaining budget after unconditional brains are accounted for.
    unconditional_global_brains = collect_unconditional_global_brains(contact, user, effective_scenario, libraries)
    uncond_global_tokens = sum(_brain_block_tokens(b) for b in unconditional_global_brains)
    msg_tokens_now = [_cost(m, is_first=(i == 0)) for i, m in enumerate(api_messages)]
    local_brain_tokens = sum(t for m, t in zip(api_messages, msg_tokens_now) if _is_brain(m))
    if base_context_size_override is not None:
        base_context_size = base_context_size_override
    else:
        base_context_size, _ = context_limits(settings)
    brain_cap = int(base_context_size / 2.5)
    headroom = brain_cap - uncond_global_tokens - local_brain_tokens

    # Drop tail-first until the block fits. Earlier-in-list activations win;
    # cascade-discovered brains (which append) get pushed out first.
    trimmed = list(activated)
    dropped: list[Brain] = []
    block_tokens = 0
    while trimmed:
        block_content = build_local_brain_block(trimmed)
        block_tokens = _cost({"role": splice_role, "content": block_content})
        if block_tokens <= headroom:
            break
        dropped.append(trimmed.pop())

    if dropped:
        log.warning(
            "Conditional brain budget overflow — dropped activations: %s "
            "(cap=%d, uncond=%d, local=%d, headroom=%d)",
            [b.name or "(unnamed)" for b in dropped],
            brain_cap, uncond_global_tokens, local_brain_tokens, headroom,
        )

    if not trimmed:
        return 0

    insert_at = _find_splice_index(msg_tokens_now, CONDITIONAL_BRAIN_TARGET_TOKENS)
    api_messages.insert(insert_at, {"role": splice_role, "content": build_local_brain_block(trimmed)})
    # Surface the brains that actually shipped (after tail-trim) so the
    # caller can include them in the breakdown.
    if activated_out is not None:
        activated_out.extend(trimmed)
    return block_tokens
