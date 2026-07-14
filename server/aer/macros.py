"""AER macros: registry-based substitution for prompt content.

Macros are registered via ``@register("name", *aliases)``. Each handler takes
a :class:`MacroCtx` plus the macro's ``::``-separated arguments and returns
the substituted string. Unknown macros pass through literally so a card
referencing an unsupported token is visible rather than silently vanishing.

Name matching is case-insensitive. The ``$contact`` / ``$user`` shorthand
runs in a separate pass with word-boundary rules. Expansion is multi-pass
(capped at three iterations) so a macro nested inside another macro's args
still resolves.

The public entry point is :func:`apply_macros`. Callers holding a fully
populated :class:`MacroCtx` can use :func:`expand` directly. The
``allowlist`` kwarg restricts expansion to a subset of macros — used by the
chat-message persist path to permit ``{{roll}}`` in user-typed text while
leaving every other token literal.
"""
from __future__ import annotations

import hashlib
import operator
import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING

from server.models import Contact, User

if TYPE_CHECKING:
    from server.models import (
        BrainLibrary,
        Chat,
        ChatMessage,
        ContactScenario,
        Scenario,
        Settings,
    )


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class MacroCtx:
    """Per-evaluation context for macro handlers.

    ``raw`` and ``seq_num`` are set by :func:`expand` before each handler
    call; the rest are populated by the caller and stable for the duration
    of the expansion.
    """

    contact: Contact
    user: User
    scenario: "Scenario | None" = None
    contact_scenario: "ContactScenario | None" = None
    chat: "Chat | None" = None
    active_path: list["ChatMessage"] = field(default_factory=list)
    messages_tree: list["ChatMessage"] = field(default_factory=list)
    rollover_cursor: int = 0
    settings: "Settings | None" = None
    is_mobile: bool = False
    generation_type: str | None = None
    now: datetime = field(default_factory=datetime.now)
    # ``BrainLibrary``s attached to the chat (resolved by the caller from
    # ``Chat.brain_library_ids``). Drives the ``{{global_brains}}`` macro —
    # ``[]`` is fine when the macro isn't reachable.
    libraries: list["BrainLibrary"] = field(default_factory=list)
    raw: str = ""
    seq_num: int = 0
    # Set by ``expand`` so handlers that re-enter ``expand`` (the inline
    # ``if`` handler and the scoped-if pre-pass's condition resolver) can
    # forward the same gating. Private — not part of ``apply_macros``.
    _allowlist: "set[str] | None" = None


# ---------------------------------------------------------------------------
# Registry & expansion
# ---------------------------------------------------------------------------


Handler = Callable[..., str]
_REGISTRY: dict[str, Handler] = {}


def register(*names: str) -> Callable[[Handler], Handler]:
    """Register a macro handler under one or more case-insensitive names."""

    def deco(fn: Handler) -> Handler:
        for n in names:
            _REGISTRY[n.lower()] = fn
        return fn

    return deco


_MACRO_RE = re.compile(r"\{\{([^{}]+?)\}\}")
_DOLLAR_RE = re.compile(
    r"\$(contact|user|c|u)(?=\Z|[\s.,!?;:\-'\")])",
    re.IGNORECASE,
)
_MAX_PASSES = 3

# Macros whose ``::``-separated args are passed VERBATIM to the handler
# (skipping the default ``[p.strip() for p in parts[1:]]`` step). Needed for
# inline ``{{if}}``: the seed preset's Tags wart relies on
# ``{{if X::\n\n::\n}}`` returning literal ``"\n\n"`` or ``"\n"`` to control
# the blank line above ``Tags:``. A blanket strip would collapse both
# branches to ``""``.
_RAW_ARG_HANDLERS: set[str] = {"if"}


def _split_invocation(body: str) -> tuple[str, list[str]]:
    """Split a ``{{...}}`` macro body into ``(name, args)``.

    Rewrites ``if <condition>`` openers into name ``"if"`` with the
    condition prepended as ``args[0]`` — so the regular dispatcher's
    handler lookup finds the inline-if handler. For names in
    :data:`_RAW_ARG_HANDLERS` the remaining args are passed verbatim;
    otherwise they're stripped.
    """
    parts = body.split("::")
    head = parts[0]
    head_stripped = head.strip()
    name = head_stripped.lower()
    rest_raw = parts[1:]

    head_words = head_stripped.split(None, 1)
    if head_words and head_words[0].lower() == "if":
        cond = head_words[1] if len(head_words) > 1 else ""
        name = "if"
        rest_raw = [cond, *rest_raw]

    if name in _RAW_ARG_HANDLERS:
        args = list(rest_raw)
    else:
        args = [p.strip() for p in rest_raw]
    return name, args


def expand(
    text: str,
    ctx: MacroCtx,
    *,
    allowlist: set[str] | None = None,
) -> str:
    """Expand registered macros in ``text``.

    The ``$contact`` / ``$user`` shorthand pass is skipped when an
    ``allowlist`` is supplied — callers scoping to a subset wouldn't expect
    the shorthand either.

    A scoped-``{{if}}`` pre-pass runs before macro substitution so the
    chosen branch's body is the only one whose macros resolve (lazy
    evaluation; an unselected branch with ``{{roll}}`` doesn't burn
    entropy). Inline ``{{if X::Y::Z}}`` form is dispatched through the
    regular pass via the registered ``if`` handler.
    """
    if not text:
        return text

    ctx = replace(ctx)
    ctx._allowlist = allowlist

    text = _apply_scoped_if(text, ctx, allowlist)

    seen: dict[str, int] = {}

    def sub(m: re.Match[str]) -> str:
        body = m.group(1)
        stripped = body.lstrip()
        if stripped.startswith("//"):
            if allowlist is None or "//" in allowlist:
                return ""
            return m.group(0)
        name, args = _split_invocation(body)
        if allowlist is not None and name not in allowlist:
            return m.group(0)
        handler = _REGISTRY.get(name)
        if handler is None:
            return m.group(0)
        raw = m.group(0)
        ctx.raw = raw
        ctx.seq_num = seen.get(raw, 0)
        seen[raw] = ctx.seq_num + 1
        try:
            return handler(ctx, *args)
        except Exception:
            return raw

    for _ in range(_MAX_PASSES):
        new = _MACRO_RE.sub(sub, text)
        if new == text:
            break
        text = new
        seen.clear()

    if allowlist is None:
        text = _DOLLAR_RE.sub(lambda m: _dollar(ctx, m), text)

    return text


def _dollar(ctx: MacroCtx, m: re.Match[str]) -> str:
    kind = m.group(1).lower()
    if kind in ("contact", "c"):
        return ctx.contact.name
    if kind in ("user", "u"):
        return ctx.user.name
    return m.group(0)


# ---------------------------------------------------------------------------
# Scoped {{if}} pre-pass — runs on raw text before _MACRO_RE.sub
# ---------------------------------------------------------------------------


_FALSY: frozenset[str] = frozenset({"", "false", "0", "off"})


def _is_truthy(s: str) -> bool:
    """ST-compatible truthiness: a value is falsy iff (after strip +
    lowercase) it equals the empty string, ``"false"``, ``"0"``, or
    ``"off"``. Anything else is truthy."""
    return s.strip().lower() not in _FALSY


def _read_macro_body(text: str, open_pos: int) -> tuple[str | None, int]:
    """Brace-balanced scan from ``text[open_pos]`` (which MUST be ``{{``)
    to the matching outer ``}}``. Returns ``(body, close_end)`` where body
    is the content between the outer braces and close_end is the index just
    past the ``}}``. ``(None, len(text))`` if there's no matching close.

    Used by the scoped-if pre-pass to find token boundaries when the body
    contains nested macros (``{{if {{contact.persona}}}}…``)."""
    i = open_pos + 2
    depth = 1
    n = len(text)
    while i < n:
        c2 = text[i:i + 2]
        if c2 == "{{":
            depth += 1
            i += 2
        elif c2 == "}}":
            depth -= 1
            if depth == 0:
                return text[open_pos + 2:i], i + 2
            i += 2
        else:
            i += 1
    return None, n


def _depth0_split(text: str, sep: str = "::") -> list[str]:
    """Split ``text`` on ``sep`` only at brace depth 0 — ignores separators
    nested inside ``{{...}}`` pairs."""
    parts: list[list[str]] = [[]]
    depth = 0
    i = 0
    n = len(text)
    sep_len = len(sep)
    while i < n:
        c2 = text[i:i + 2]
        if c2 == "{{":
            parts[-1].append(c2)
            depth += 1
            i += 2
        elif c2 == "}}":
            parts[-1].append(c2)
            if depth > 0:
                depth -= 1
            i += 2
        elif depth == 0 and text[i:i + sep_len] == sep:
            parts.append([])
            i += sep_len
        else:
            parts[-1].append(text[i])
            i += 1
    return ["".join(p) for p in parts]


def _trim_scoped_body(text: str) -> str:
    """Trim + dedent the body of a non-``#`` scoped ``{{if}}``.

    Mirrors ST's ``trimScopedContent``: find the indentation (spaces /
    tabs) of the first non-empty line; strip that exact prefix from every
    line starting with it; ``lstrip`` lines with less; then ``.strip()``
    the joined result. Empty / all-whitespace input → ``""``.
    """
    if not text or not text.strip():
        return ""
    lines = text.split("\n")
    indent: str | None = None
    for line in lines:
        if line.strip():
            stripped = line.lstrip(" \t")
            indent = line[: len(line) - len(stripped)]
            break
    if indent is None:
        return ""
    if not indent:
        return text.strip()
    out: list[str] = []
    for line in lines:
        if line.startswith(indent):
            out.append(line[len(indent):])
        else:
            out.append(line.lstrip(" \t"))
    return "\n".join(out).strip()


def _eval_condition(cond_text: str, ctx: MacroCtx) -> bool:
    """Evaluate a scoped/inline-if condition for truthiness.

    Recursively expands nested macros in ``cond_text`` (so
    ``{{if {{contact.persona}}}}`` resolves the inner macro first), peels
    a leading ``!`` for negation, then auto-resolves a bare registered
    macro name (so ``{{if contact.persona}}`` works without brace-wrapping).
    Empty / ``"false"`` / ``"0"`` / ``"off"`` (case-insensitive) are falsy.
    """
    allowlist = ctx._allowlist
    resolved = expand(cond_text, ctx, allowlist=allowlist)
    s = resolved.strip()
    negate = False
    if s.startswith("!"):
        negate = True
        s = s[1:].lstrip()
    # Bare-name auto-resolve: if the post-expand condition is a plain
    # identifier (no remaining brace tokens) and matches a registered
    # macro, expand it once more as ``{{NAME}}``.
    if s and "{{" not in s and "}}" not in s:
        candidate = s.lower()
        if candidate in _REGISTRY:
            s = expand("{{" + s + "}}", ctx, allowlist=allowlist).strip()
    truthy = _is_truthy(s)
    return (not truthy) if negate else truthy


def _classify_if_token(body: str) -> tuple[str | None, str, str]:
    """Classify a ``{{...}}`` token body as a scoped opener, close, else,
    or other. Returns ``(kind, flag, condition)`` where:

    - ``kind`` is ``"open"`` (scoped if-opener), ``"close"`` (``/if``),
      ``"else"``, or ``None`` (anything else — including inline if).
    - ``flag`` is ``"#"`` for the preserve-whitespace flag, ``""`` otherwise.
    - ``condition`` is the post-``if`` text for openers; ``""`` otherwise.

    Inline form (``{{if X::Y}}`` / ``{{if X::Y::Z}}``) classifies as
    ``None`` so the regular pass dispatches it via the registered ``if``
    handler.
    """
    b = body.lstrip()
    if b == "/if":
        return "close", "", ""
    if b == "else":
        return "else", "", ""
    flag = ""
    if b.startswith("#"):
        flag = "#"
        b = b[1:]
    words = b.split(None, 1)
    if not words or words[0].lower() != "if":
        return None, "", ""
    cond_arg = words[1] if len(words) > 1 else ""
    # Inline vs scoped: 1 piece ⇒ scoped, 2+ pieces ⇒ inline.
    parts = _depth0_split(cond_arg, "::")
    if len(parts) == 1:
        return "open", flag, parts[0]
    return None, "", ""  # inline — pre-pass skips


def _find_if_tokens(text: str) -> list[dict]:
    """Walk ``text`` collecting scoped-if structural tokens.

    Inline-if openers, regular macros, and arbitrary ``{{...}}`` content
    are skipped (left for the regular pass). Returns tokens in source
    order; each is a dict with ``kind`` / ``start`` / ``end`` / (for
    openers) ``flag`` / ``cond``.
    """
    out: list[dict] = []
    pos = 0
    n = len(text)
    while True:
        open_pos = text.find("{{", pos)
        if open_pos < 0:
            break
        body, close_end = _read_macro_body(text, open_pos)
        if body is None:
            break
        kind, flag, cond = _classify_if_token(body)
        if kind == "open":
            out.append({
                "kind": "open", "start": open_pos, "end": close_end,
                "flag": flag, "cond": cond,
            })
        elif kind == "close":
            out.append({"kind": "close", "start": open_pos, "end": close_end})
        elif kind == "else":
            out.append({"kind": "else", "start": open_pos, "end": close_end})
        pos = close_end
        if pos >= n:
            break
    return out


def _scoped_if_one_pass(text: str, ctx: MacroCtx) -> str:
    """Single pass of scoped-if resolution.

    Matches every well-formed ``{{if}}…{{/if}}`` (with optional
    ``{{else}}`` split at the same depth) via a stack. Resolves
    innermost-first (frames pop in LIFO order). Splices the chosen branch
    in place; the branch text re-enters the buffer for the next pass to
    handle nested ifs that came in via macro expansion of the condition.

    Unmatched / stray tokens are left literal — the regular pass will
    surface them as unknown macros so the author sees the syntax error.
    """
    tokens = _find_if_tokens(text)
    if not tokens:
        return text

    subs: list[tuple[int, int, str]] = []  # (start, end, replacement)
    stack: list[dict] = []  # frames with open + optional else

    for tok in tokens:
        if tok["kind"] == "open":
            stack.append({"open": tok, "else": None})
        elif tok["kind"] == "else":
            if stack and stack[-1]["else"] is None:
                stack[-1]["else"] = tok
        else:  # close
            if not stack:
                continue  # stray /if — leave literal
            frame = stack.pop()
            open_tok = frame["open"]
            else_tok = frame["else"]
            then_start = open_tok["end"]
            close_start = tok["start"]
            close_end = tok["end"]
            if else_tok is not None:
                then_end = else_tok["start"]
                else_start = else_tok["end"]
                else_end = close_start
            else:
                then_end = close_start
                else_start = else_end = None

            truthy = _eval_condition(open_tok["cond"], ctx)
            if truthy:
                branch = text[then_start:then_end]
            else:
                branch = text[else_start:else_end] if else_start is not None else ""

            if not open_tok["flag"]:  # non-# scoped: auto-trim + dedent
                branch = _trim_scoped_body(branch)

            subs.append((open_tok["start"], close_end, branch))

    if not subs:
        return text

    # Apply right-to-left so earlier indices stay valid.
    subs.sort(key=lambda s: s[0], reverse=True)
    for start, end, replacement in subs:
        text = text[:start] + replacement + text[end:]
    return text


def _apply_scoped_if(
    text: str, ctx: MacroCtx, allowlist: set[str] | None,
) -> str:
    """Top-level scoped-if pre-pass entry point.

    Gated on the ``"if"`` allowlist token: chat-message persist with
    ``allowlist={"roll"}`` skips the pre-pass entirely so user-typed
    ``{{if}}`` content survives literally.

    Re-runs the scan up to :data:`_MAX_PASSES` times so nested ifs that
    arrive via macro expansion of a chosen branch get resolved.
    """
    if allowlist is not None and "if" not in allowlist:
        return text
    if "{{" not in text:
        return text
    for _ in range(_MAX_PASSES):
        new = _scoped_if_one_pass(text, ctx)
        if new == text:
            break
        text = new
    return text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply_macros(
    text: str,
    contact: Contact | str,
    user: User | str,
    *,
    scenario: "Scenario | None" = None,
    contact_scenario: "ContactScenario | None" = None,
    chat: "Chat | None" = None,
    active_path: Iterable["ChatMessage"] | None = None,
    messages_tree: Iterable["ChatMessage"] | None = None,
    rollover_cursor: int = 0,
    settings: "Settings | None" = None,
    is_mobile: bool = False,
    generation_type: str | None = None,
    now: datetime | None = None,
    libraries: Iterable["BrainLibrary"] | None = None,
    allowlist: set[str] | None = None,
) -> str:
    """Apply AER macros to ``text``.

    Accepts ``Contact`` / ``User`` objects or plain names (in which case
    only macros that need the names resolve). Pass the richer context for
    scenario / chat / history / preset-aware macros. ``libraries`` is the
    list of ``BrainLibrary``s attached to the chat — used by
    ``{{global_brains}}`` to render unconditional library brains in
    Generic-mode prompts.
    """
    if isinstance(contact, str):
        contact = Contact(name=contact)
    if isinstance(user, str):
        user = User(name=user)
    ctx = MacroCtx(
        contact=contact,
        user=user,
        scenario=scenario,
        contact_scenario=contact_scenario,
        chat=chat,
        active_path=list(active_path or []),
        messages_tree=list(messages_tree or []),
        rollover_cursor=rollover_cursor,
        settings=settings,
        is_mobile=is_mobile,
        generation_type=generation_type,
        now=now or datetime.now(),
        libraries=list(libraries or []),
    )
    return expand(text, ctx, allowlist=allowlist)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scen_field(ctx: MacroCtx, name: str) -> str:
    """Read ``name`` from whichever scenario is active on the chat."""
    if ctx.contact_scenario is not None:
        v = getattr(ctx.contact_scenario, name, "")
        if v:
            return str(v)
    if ctx.scenario is not None:
        return str(getattr(ctx.scenario, name, "") or "")
    return ""


def _last_msg(ctx: MacroCtx, sender: str | None = None) -> "ChatMessage | None":
    for m in reversed(ctx.active_path):
        if sender is None or m.sender == sender:
            return m
    return None


def _flatten_body(msg: "ChatMessage") -> str:
    parts = []
    for bubble in msg.body:
        t = (bubble.text or "").strip()
        if t:
            parts.append(t)
    return "\n".join(parts)


def _context_limits(ctx: MacroCtx) -> tuple[int, int, int]:
    """Return ``(max_context, max_prompt, max_response)`` in tokens."""
    from server.aer.rollover import CONTEXT_PRESETS, MAX_OUTPUT_TOKENS

    preset_name = ctx.settings.context_preset if ctx.settings else "opus"
    cfg = CONTEXT_PRESETS.get(preset_name, CONTEXT_PRESETS["opus"])
    base = cfg["base_context_size"]
    return base, base - MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _moment_format(now: datetime, fmt: str) -> str:
    """Translate a moment.js-style format string against ``now``.

    Supports the common token subset (year/month/day/weekday/hour/minute/
    second/AM-PM, padded and unpadded variants, ordinals). Bracketed
    literals (``[at]``) pass through unchanged. Unknown characters are
    emitted literally.
    """
    tokens: list[tuple[str, Callable[[datetime], str]]] = [
        ("YYYY", lambda d: d.strftime("%Y")),
        ("YY", lambda d: d.strftime("%y")),
        ("MMMM", lambda d: d.strftime("%B")),
        ("MMM", lambda d: d.strftime("%b")),
        ("MM", lambda d: d.strftime("%m")),
        ("Mo", lambda d: _ordinal(d.month)),
        ("M", lambda d: str(d.month)),
        ("DDDD", lambda d: f"{d.timetuple().tm_yday:03d}"),
        ("DDDo", lambda d: _ordinal(d.timetuple().tm_yday)),
        ("DDD", lambda d: str(d.timetuple().tm_yday)),
        ("DD", lambda d: d.strftime("%d")),
        ("Do", lambda d: _ordinal(d.day)),
        ("D", lambda d: str(d.day)),
        ("dddd", lambda d: d.strftime("%A")),
        ("ddd", lambda d: d.strftime("%a")),
        ("HH", lambda d: d.strftime("%H")),
        ("H", lambda d: str(d.hour)),
        ("hh", lambda d: d.strftime("%I")),
        ("h", lambda d: str(((d.hour - 1) % 12) + 1)),
        ("mm", lambda d: d.strftime("%M")),
        ("m", lambda d: str(d.minute)),
        ("ss", lambda d: d.strftime("%S")),
        ("s", lambda d: str(d.second)),
        ("A", lambda d: "AM" if d.hour < 12 else "PM"),
        ("a", lambda d: "am" if d.hour < 12 else "pm"),
    ]
    out: list[str] = []
    i = 0
    n = len(fmt)
    while i < n:
        if fmt[i] == "[":
            end = fmt.find("]", i)
            if end > i:
                out.append(fmt[i + 1 : end])
                i = end + 1
                continue
        matched = False
        for tok, fn in tokens:
            if fmt.startswith(tok, i):
                out.append(fn(now))
                i += len(tok)
                matched = True
                break
        if not matched:
            out.append(fmt[i])
            i += 1
    return "".join(out)


_DICE_RE = re.compile(r"^\s*(\d*)d(\d+)\s*([+\-]\s*\d+)?\s*$", re.IGNORECASE)


def _roll_dice(formula: str) -> str | None:
    """Roll ``NdM+K`` / ``NdM-K`` / ``dM``. Returns ``None`` on malformed
    input or out-of-range counts; caller decides whether to leave the macro
    literal."""
    m = _DICE_RE.match(formula)
    if not m:
        return None
    count = int(m.group(1) or "1")
    sides = int(m.group(2))
    mod = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    if not (1 <= count <= 100 and 1 <= sides <= 1000):
        return None
    total = sum(random.randint(1, sides) for _ in range(count)) + mod
    return str(total)


def _pronoun_parts(pronouns: str) -> tuple[str, str]:
    """Split an AER-style ``she/her`` pronoun string into ``(subject, object)``.

    AER expects a two-part format; if only one part is supplied both slots
    fall back to it, and an empty string yields two empty parts. Anything
    after the second slash is ignored."""
    parts = [s.strip() for s in (pronouns or "").split("/") if s.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], parts[1]


def _human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


# ---------------------------------------------------------------------------
# Identity & character card
# ---------------------------------------------------------------------------


@register("char")
def _m_char(ctx: MacroCtx, *_: str) -> str:
    return ctx.contact.name or ""


@register("user")
def _m_user(ctx: MacroCtx, *_: str) -> str:
    return ctx.user.name or ""


@register("persona")
def _m_persona(ctx: MacroCtx, *_: str) -> str:
    return ctx.user.persona or ""


@register("description", "chardescription")
def _m_description(ctx: MacroCtx, *_: str) -> str:
    return ctx.contact.persona or ""


@register("personality", "charpersonality")
def _m_personality(ctx: MacroCtx, *_: str) -> str:
    return ctx.contact.appearance or ""


@register("scenario", "charscenario")
def _m_scenario(ctx: MacroCtx, *_: str) -> str:
    return _scen_field(ctx, "scene")


@register("charfirstmessage")
def _m_first_msg(ctx: MacroCtx, *_: str) -> str:
    if ctx.contact_scenario and (ctx.contact_scenario.greeting or "").strip():
        return ctx.contact_scenario.greeting
    # ``getattr`` with default — impersonate flips contact ↔ user in the
    # macro context, and User has no ``greeting`` field. Empty string is
    # the right semantic in that case.
    return getattr(ctx.contact, "greeting", "") or ""


@register("charcreatornotes", "creatornotes")
def _m_creator_notes(ctx: MacroCtx, *_: str) -> str:
    return ctx.contact.description or ""


@register("charversion", "version", "char_version")
def _m_version(ctx: MacroCtx, *_: str) -> str:
    return ""


@register("chardepthprompt")
def _m_depth_prompt(ctx: MacroCtx, *_: str) -> str:
    # User has no reminder_brain — impersonate flips contact ↔ user.
    rb = getattr(ctx.contact, "reminder_brain", None)
    return (rb.content if rb else "") or ""


@register("mesexamplesraw", "mesexamples")
def _m_mes_examples(ctx: MacroCtx, *_: str) -> str:
    chats = getattr(ctx.contact, "example_chats", None) or []
    if not chats:
        return ""
    blocks: list[str] = []
    for ex in chats:
        lines: list[str] = []
        user_name = (ex.user_name or "").strip() or "User"
        for msg in ex.messages:
            text = (msg.text or "").strip()
            if not text:
                continue
            sender = ctx.contact.name if msg.is_contact else user_name
            lines.append(f"{sender}: {text}")
        if lines:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@register("group", "groupnotmuted", "charifnotgroup")
def _m_group(ctx: MacroCtx, *_: str) -> str:
    return ctx.contact.name or ""


@register("notchar")
def _m_not_char(ctx: MacroCtx, *_: str) -> str:
    return ""


# ---------------------------------------------------------------------------
# AER namespace
# ---------------------------------------------------------------------------


def _field_macro(getter: Callable[[MacroCtx], str]) -> Handler:
    """Wrap a one-arg getter into a macro handler.

    The result is ``.strip()``ed so a Persona textarea that contains only
    whitespace doesn't falsely satisfy ``{{if contact.persona}}``. The
    falsy-coerce-to-empty-string semantics are preserved.
    """

    def fn(ctx: MacroCtx, *_: str) -> str:
        return (getter(ctx) or "").strip()

    return fn


for _alias, _getter in {
    "contact.name": lambda c: c.contact.name,
    "contact.species": lambda c: c.contact.species,
    "contact.gender": lambda c: c.contact.gender,
    "contact.pronouns": lambda c: c.contact.pronouns,
    "contact.pronouns.subject": lambda c: _pronoun_parts(c.contact.pronouns)[0],
    "contact.pronouns.object": lambda c: _pronoun_parts(c.contact.pronouns)[1],
    "contact.persona": lambda c: c.contact.persona,
    "contact.appearance": lambda c: c.contact.appearance,
    "contact.description": lambda c: c.contact.description,
    "contact.greeting": lambda c: getattr(c.contact, "greeting", "") or "",
    "contact.tags": lambda c: c.contact.tags,
    "user.name": lambda c: c.user.name,
    "user.species": lambda c: c.user.species,
    "user.gender": lambda c: c.user.gender,
    "user.pronouns": lambda c: c.user.pronouns,
    "user.pronouns.subject": lambda c: _pronoun_parts(c.user.pronouns)[0],
    "user.pronouns.object": lambda c: _pronoun_parts(c.user.pronouns)[1],
    "user.persona": lambda c: c.user.persona,
    "user.appearance": lambda c: c.user.appearance,
    "user.description": lambda c: c.user.description,
    "user.tags": lambda c: c.user.tags,
    "scenario.name": lambda c: _scen_field(c, "name"),
    "scenario.environment": lambda c: _scen_field(c, "environment"),
    "scenario.scene": lambda c: _scen_field(c, "scene"),
    "scenario.description": lambda c: _scen_field(c, "description"),
    "scenario.tags": lambda c: _scen_field(c, "tags"),
    "scenario.greeting": lambda c: _scen_field(c, "greeting"),
}.items():
    register(_alias)(_field_macro(_getter))


# ---------------------------------------------------------------------------
# Chat namespace (Generic mode mostly; safe in AER too)
# ---------------------------------------------------------------------------


def _chat_field(getter: Callable[[MacroCtx], str]) -> Handler:
    """Like ``_field_macro`` but doesn't `.strip()` — the chat enum-backed
    fields (``chat.intimacy`` / ``chat.style`` / ``chat.cjk`` / ...) are
    already exact values; trimming them would silently corrupt the
    ``"true"``/``"false"`` literals (no whitespace to trim, but skipping
    the call documents intent)."""

    def fn(ctx: MacroCtx, *_: str) -> str:
        return getter(ctx) or ""

    return fn


for _alias, _getter in {
    "chat.title": lambda c: (c.chat.title if c.chat else "") or "",
    "chat.tags": lambda c: (c.chat.tags if c.chat else "") or "",
    "chat.intimacy": lambda c: c.chat.intimacy.value if c.chat else "",
    "chat.style": lambda c: c.chat.style.value if c.chat else "",
    "chat.response_length": lambda c: (
        c.chat.response_length.value
        if c.chat and c.chat.response_length is not None
        else ""
    ),
    # ``"true"`` / ``"false"`` mirrors ``{{ismobile}}``; matters because
    # authors may want ``{{if chat.cjk}}{{else}}...{{/if}}`` to fire on
    # the non-CJK branch too. Empty when no chat is in scope.
    "chat.cjk": lambda c: (
        ("true" if c.chat.cjk else "false") if c.chat else ""
    ),
}.items():
    register(_alias)(_chat_field(_getter))


# ---------------------------------------------------------------------------
# Brain rendering — {{global_brains}} for Generic mode prompts
# ---------------------------------------------------------------------------


@register("global_brains")
def _m_global_brains(ctx: MacroCtx, *_: str) -> str:
    """Render every unconditional brain attached to contact / user /
    scenario / libraries as a single ``----\\nName\\nContent\\n``-joined
    block. Conditional brains never leak in here — those go through the
    activation engine and are spliced separately by the rollover code.

    Output is byte-identical to AER's system-prompt brain section (shared
    helper). The trailing ``\\n`` from the last block is trimmed so a
    wrapping ``</LORE>`` tag on the next line lands flush.
    """
    # Lazy import to avoid a circular dependency between ``aer.macros`` and
    # ``aer.format`` at module load.
    from server.aer.format import (
        collect_unconditional_global_brains,
        render_brain_blocks,
    )

    brains = collect_unconditional_global_brains(
        ctx.contact, ctx.user, ctx.scenario, ctx.libraries or None,
    )
    return render_brain_blocks(brains).rstrip("\n")


# ---------------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------------


@register("date", "isodate")
def _m_date(ctx: MacroCtx, *_: str) -> str:
    return ctx.now.strftime("%Y-%m-%d")


@register("time")
def _m_time(ctx: MacroCtx, *_: str) -> str:
    ampm = "AM" if ctx.now.hour < 12 else "PM"
    return ctx.now.strftime("%I:%M") + " " + ampm


@register("time24", "isotime")
def _m_time24(ctx: MacroCtx, *_: str) -> str:
    return ctx.now.strftime("%H:%M")


@register("weekday")
def _m_weekday(ctx: MacroCtx, *_: str) -> str:
    return ctx.now.strftime("%A")


@register("datetimeformat")
def _m_datetime_format(ctx: MacroCtx, *args: str) -> str:
    if not args or not args[0]:
        return ctx.raw
    fmt = "::".join(args)
    return _moment_format(ctx.now, fmt)


# ---------------------------------------------------------------------------
# Randomness
# ---------------------------------------------------------------------------


@register("random")
def _m_random(ctx: MacroCtx, *options: str) -> str:
    if not options:
        return ctx.raw
    return random.choice(list(options))


@register("pick")
def _m_pick(ctx: MacroCtx, *options: str) -> str:
    if not options:
        return ctx.raw
    chat_id = ctx.chat.id if ctx.chat else ""
    nonce = ctx.chat.pick_reroll_nonce if ctx.chat else ""
    seed_str = f"{chat_id}|{nonce}|{ctx.raw}|{ctx.seq_num}"
    digest = hashlib.sha256(seed_str.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:8], "big") % len(options)
    return options[idx]


@register("roll")
def _m_roll(ctx: MacroCtx, *args: str) -> str:
    if not args:
        return ctx.raw
    formula = "::".join(args)
    result = _roll_dice(formula)
    return result if result is not None else ctx.raw


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------


@register("lastmessage")
def _m_last_message(ctx: MacroCtx, *_: str) -> str:
    msg = _last_msg(ctx)
    return _flatten_body(msg) if msg else ""


@register("lastusermessage")
def _m_last_user_message(ctx: MacroCtx, *_: str) -> str:
    msg = _last_msg(ctx, sender="user")
    return _flatten_body(msg) if msg else ""


@register("lastcharmessage")
def _m_last_char_message(ctx: MacroCtx, *_: str) -> str:
    msg = _last_msg(ctx, sender="contact")
    return _flatten_body(msg) if msg else ""


@register("lastmessageid")
def _m_last_message_id(ctx: MacroCtx, *_: str) -> str:
    if not ctx.active_path:
        return ""
    return str(len(ctx.active_path) - 1)


@register("allchatrange")
def _m_all_chat_range(ctx: MacroCtx, *_: str) -> str:
    if not ctx.active_path:
        return ""
    return f"0-{len(ctx.active_path) - 1}"


@register("firstincludedmessageid")
def _m_first_included(ctx: MacroCtx, *_: str) -> str:
    if not ctx.active_path:
        return ""
    return str(min(ctx.rollover_cursor, len(ctx.active_path) - 1))


@register("idleduration", "idle_duration")
def _m_idle_duration(ctx: MacroCtx, *_: str) -> str:
    msg = _last_msg(ctx, sender="user")
    if msg is None:
        return ""
    return _human_duration(ctx.now.timestamp() - msg.timestamp)


@register("lastswipeid")
def _m_last_swipe_id(ctx: MacroCtx, *_: str) -> str:
    tip = _last_msg(ctx)
    if tip is None:
        return ""
    siblings = [m for m in ctx.messages_tree if m.parent_id == tip.parent_id]
    return str(len(siblings))


@register("currentswipeid")
def _m_current_swipe_id(ctx: MacroCtx, *_: str) -> str:
    tip = _last_msg(ctx)
    if tip is None:
        return ""
    siblings = [m for m in ctx.messages_tree if m.parent_id == tip.parent_id]
    siblings.sort(key=lambda m: m.timestamp)
    for idx, m in enumerate(siblings, start=1):
        if m.id == tip.id:
            return str(idx)
    return ""


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


@register("model")
def _m_model(ctx: MacroCtx, *_: str) -> str:
    if ctx.settings is None:
        return ""
    return ctx.settings.default_model or ""


@register("maxcontext", "maxcontexttokens")
def _m_max_context(ctx: MacroCtx, *_: str) -> str:
    return str(_context_limits(ctx)[0])


@register("maxprompt", "maxprompttokens")
def _m_max_prompt(ctx: MacroCtx, *_: str) -> str:
    return str(_context_limits(ctx)[1])


@register("maxresponse", "maxresponsetokens")
def _m_max_response(ctx: MacroCtx, *_: str) -> str:
    return str(_context_limits(ctx)[2])


@register("ismobile")
def _m_is_mobile(ctx: MacroCtx, *_: str) -> str:
    return "true" if ctx.is_mobile else "false"


@register("sanitize_html")
def _m_sanitize_html(ctx: MacroCtx, *_: str) -> str:
    """``"true"`` when Generic-mode HTML sanitization is on (the default),
    ``"false"`` when the user has turned it off so raw HTML / JS reaches the
    bubble. Mirrors ``settings.sanitize_generic_html``; ``"true"`` (the safe
    default) when no settings are in scope. Gate a section that only applies
    when raw HTML is live with ``{{#if !sanitize_html}}…{{/if}}``."""
    if ctx.settings is None:
        return "true"
    return "true" if ctx.settings.sanitize_generic_html else "false"


@register("lastgenerationtype")
def _m_last_gen_type(ctx: MacroCtx, *_: str) -> str:
    # Canonical values: "normal", "swipe", "continue", "impersonate".
    # Set per call from routers.generate; mirrors SillyTavern so prompts that
    # branch on the macro stay portable across imports.
    return ctx.generation_type or ""


# ---------------------------------------------------------------------------
# Format / utility
# ---------------------------------------------------------------------------


@register("space")
def _m_space(ctx: MacroCtx, *args: str) -> str:
    n = 1
    if args and args[0].isdigit():
        n = min(int(args[0]), 1000)
    return " " * n


@register("newline")
def _m_newline(ctx: MacroCtx, *args: str) -> str:
    n = 1
    if args and args[0].isdigit():
        n = min(int(args[0]), 1000)
    return "\n" * n


@register("noop")
def _m_noop(ctx: MacroCtx, *_: str) -> str:
    return ""


@register("reverse")
def _m_reverse(ctx: MacroCtx, *args: str) -> str:
    if not args:
        return ctx.raw
    return "::".join(args)[::-1]


# ---------------------------------------------------------------------------
# Control flow — inline {{if}} (scoped form handled by the pre-pass)
# ---------------------------------------------------------------------------


@register("if")
def _m_if(ctx: MacroCtx, *args: str) -> str:
    """Inline ``{{if X::then::else}}`` form.

    The scoped form (``{{if X}}…{{/if}}``) is resolved by the pre-pass
    before macro substitution. If the inline handler fires it's either
    because the author used the inline syntax or because an unmatched
    ``{{if X}}`` opener slipped past the pre-pass — in the latter case
    we leave the token literal so the author sees the syntax error.

    Args are passed verbatim (no strip) thanks to
    :data:`_RAW_ARG_HANDLERS`, so literal-whitespace branches like
    ``{{if X::\\n\\n::\\n}}`` round-trip exactly.
    """
    if len(args) < 2:
        return ctx.raw
    cond_text = args[0]
    then_branch = args[1]
    else_branch = args[2] if len(args) >= 3 else ""
    return then_branch if _eval_condition(cond_text, ctx) else else_branch


# ---------------------------------------------------------------------------
# Comparison + boolean helpers — AER additions (not in SillyTavern)
# ---------------------------------------------------------------------------


@register("eq")
def _m_eq(ctx: MacroCtx, *args: str) -> str:
    return "true" if len(args) >= 2 and args[0] == args[1] else ""


@register("neq")
def _m_neq(ctx: MacroCtx, *args: str) -> str:
    return "true" if len(args) >= 2 and args[0] != args[1] else ""


def _numeric_compare(args: tuple[str, ...], op: Callable[[float, float], bool]) -> str:
    if len(args) < 2:
        return ""
    try:
        a = float(args[0])
        b = float(args[1])
    except (TypeError, ValueError):
        return ""
    return "true" if op(a, b) else ""


@register("lt")
def _m_lt(ctx: MacroCtx, *args: str) -> str:
    return _numeric_compare(args, operator.lt)


@register("gt")
def _m_gt(ctx: MacroCtx, *args: str) -> str:
    return _numeric_compare(args, operator.gt)


@register("lte")
def _m_lte(ctx: MacroCtx, *args: str) -> str:
    return _numeric_compare(args, operator.le)


@register("gte")
def _m_gte(ctx: MacroCtx, *args: str) -> str:
    return _numeric_compare(args, operator.ge)


@register("and")
def _m_and(ctx: MacroCtx, *args: str) -> str:
    if not args:
        return ""
    return "true" if all(_is_truthy(a) for a in args) else ""


@register("or")
def _m_or(ctx: MacroCtx, *args: str) -> str:
    if not args:
        return ""
    return "true" if any(_is_truthy(a) for a in args) else ""
