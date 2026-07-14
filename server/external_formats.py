"""Read foreign character-card / world-info / lorebook formats into the AER
on-disk export shape, so they can re-enter the regular import pipeline.

All decoders are clean reimplementations from format semantics — no code is
ported from any external project. Each field's mapping rationale is
documented inline where it isn't obvious.

Public surface:

- :func:`parse_external` — Try to interpret raw bytes as a foreign format
  (JSON or PNG-with-embedded-data). Returns the AER-shape payload plus
  the original card image bytes when applicable. Returns ``None`` if the
  input isn't a recognised foreign format (so callers can fall back to
  native parsing).
- :func:`extract_brains` — Brain-only flavour: returns the list of brain
  dicts a foreign payload would contribute, for callers (e.g. the
  "Import brains" button) that want to merge brains into an existing
  entity rather than create a new one.
- :func:`write_png_with_data` — Re-emit an image as PNG with our own
  ``aertavern_data`` tEXt chunk (base64-encoded JSON) for the
  card-export flow.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
import struct
import uuid
from typing import Any, Literal, TypedDict

import msgspec
import msgspec.json

log = logging.getLogger("aether.external_formats")

# Reusable encoder/decoder instances. msgspec's instance-based API caches
# type-resolution state internally; reusing the same instance is faster
# than calling the module-level ``msgspec.json.encode`` / ``decode``
# helpers (which spin up a new codec each call).
_json_enc = msgspec.json.Encoder()
_json_dec = msgspec.json.Decoder()


# Chunk name we use to carry our own JSON export inside a PNG.
AER_CHUNK = "aertavern_data"

# Foreign chunk names we recognise on import.
ST_CHARA_CHUNK = "chara"
NAI_LORE_CHUNK = "naidata"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ParsedImport(TypedDict):
    kind: Literal["contact", "user", "scenario", "brain_library", "chat"]
    payload: dict
    card_image_bytes: bytes | None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def parse_external(data: bytes, filename: str = "") -> ParsedImport | None:
    """Try to interpret ``data`` as a foreign format.

    Returns ``None`` if it isn't one (caller falls through to native JSON
    import). Raises :class:`ValueError` on a malformed/recognised-but-broken
    file (e.g. a PNG with a ``chara`` chunk whose base64 doesn't decode).
    """
    if not data:
        return None

    # PNG path: read text chunks and dispatch by chunk name preference.
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _parse_png(data, filename)

    # JSON path.
    try:
        obj = _json_dec.decode(data)
    except msgspec.DecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return _dispatch_foreign_json(obj, filename, card_image_bytes=None)


def is_foreign_json(obj: Any) -> bool:
    """Heuristic: does this dict look like a foreign-format JSON we
    decode (ST card v1/v2/v3, ST WI standalone, or a lorebook)?

    Used by ``importers.detect_format`` to route foreign payloads
    through :func:`parse_external_json`.
    """
    if not isinstance(obj, dict):
        return False
    spec = obj.get("spec")
    if isinstance(spec, str) and spec in ("chara_card_v2", "chara_card_v3"):
        return True
    if isinstance(obj.get("lorebookVersion"), (int, float)) and isinstance(
        obj.get("entries"), list
    ):
        return True
    entries = obj.get("entries")
    if isinstance(entries, dict) and entries:
        if any(
            isinstance(v, dict) and ("content" in v or "key" in v)
            for v in entries.values()
        ):
            return True
    if _looks_like_st_card_v1(obj):
        return True
    return False


def parse_external_json(
    obj: dict, filename: str = "", card_image_bytes: bytes | None = None
) -> ParsedImport | None:
    """Foreign-JSON entry point used when the bytes were already parsed
    elsewhere (e.g. by the flat-zip TOC walker)."""
    return _dispatch_foreign_json(obj, filename, card_image_bytes=card_image_bytes)


def extract_brains(data: bytes, filename: str = "") -> dict:
    """Brain-only flavour: return ``{"brains": [...], "source_name": str}``.

    Accepts native AER brain library JSON / contact export / any foreign
    format that ``parse_external`` understands. The result is purely the
    ``brains`` array of whichever shape the input resolves to plus a
    human-readable source name for the frontend's toast.
    """
    parsed = parse_external(data, filename)
    if parsed is not None:
        return _brains_from_payload(parsed["payload"], parsed["kind"], filename)

    # Native JSON fallback.
    try:
        obj = _json_dec.decode(data)
    except msgspec.DecodeError as exc:
        raise ValueError(f"unrecognised input: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("unrecognised input")

    # Native shapes: brain library, contact, user, scenario, chat composite.
    brains: list[dict] = []
    if obj.get("kind") == "brain_library":
        brains = list(obj.get("brains") or [])
        name = str(obj.get("name") or _name_from_filename(filename, "brains"))
    elif "chat" in obj and isinstance(obj["chat"], dict):
        char = obj.get("character") or {}
        brains = list(char.get("brains") or [])
        name = str(char.get("name") or _name_from_filename(filename, "brains"))
    else:
        brains = list(obj.get("brains") or [])
        name = str(obj.get("name") or _name_from_filename(filename, "brains"))

    return {"brains": brains, "source_name": name}


# ---------------------------------------------------------------------------
# PNG metadata
# ---------------------------------------------------------------------------


def read_png_text_chunks(data: bytes) -> dict[str, str]:
    """Return the union of tEXt + iTXt chunks. Best-effort: tries Pillow
    first (handles common files cleanly), falls back to a manual chunk
    walker for files Pillow won't decode.
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.load()
        return dict(getattr(img, "text", {}) or {})
    except Exception as exc:  # noqa: BLE001 — any decoder failure → fallback
        log.debug("Pillow PNG read failed (%s); using manual walker", exc)
        return _manual_png_text_chunks(data)


def write_png_with_data(image_bytes: bytes, data: dict) -> bytes:
    """Re-encode an image as PNG with our ``aertavern_data`` chunk.

    The chunk value is **base64(utf8(json))** — matching the convention
    used by the formats we read on the way in. Base64 keeps the chunk
    ASCII-safe so tEXt (Latin-1 only per the PNG spec) reliably carries
    non-ASCII payloads through any image-editing tool that resaves the
    PNG.
    """
    from PIL import Image, ImageOps
    from PIL.PngImagePlugin import PngInfo

    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        # Convert palette / 16-bit / etc. to a safe PNG-encodable mode.
        if "A" in img.mode or img.mode == "P":
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")

    encoded = base64.b64encode(_json_enc.encode(data)).decode("ascii")

    info = PngInfo()
    info.add_text(AER_CHUNK, encoded)

    out = io.BytesIO()
    img.save(out, "PNG", pnginfo=info)
    return out.getvalue()


def _manual_png_text_chunks(data: bytes) -> dict[str, str]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    chunks: dict[str, str] = {}
    pos = 8
    n = len(data)
    while pos + 12 <= n:
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8].decode("ascii", errors="replace")
        body_end = pos + 8 + size
        if body_end + 4 > n:
            break
        body = data[pos + 8 : body_end]
        if ctype == "tEXt":
            try:
                null_idx = body.index(b"\x00")
                key = body[:null_idx].decode("latin-1")
                value = body[null_idx + 1 :].decode("latin-1")
                chunks[key] = value
            except (ValueError, UnicodeDecodeError):
                pass
        elif ctype == "iTXt":
            try:
                null_idx = body.index(b"\x00")
                key = body[:null_idx].decode("utf-8")
                rest = body[null_idx + 1 :]
                # iTXt: keyword\0 comp_flag(1) comp_method(1) lang\0 trans\0 text
                if len(rest) >= 2:
                    rest = rest[2:]
                    lang_end = rest.index(b"\x00")
                    rest = rest[lang_end + 1 :]
                    trans_end = rest.index(b"\x00")
                    rest = rest[trans_end + 1 :]
                    chunks[key] = rest.decode("utf-8", errors="replace")
            except (ValueError, UnicodeDecodeError):
                pass
        pos = body_end + 4  # skip CRC
        if ctype == "IEND":
            break
    return chunks


def _decode_base64_json(value: str) -> dict | None:
    try:
        raw = base64.b64decode(value, validate=False)
        decoded = _json_dec.decode(raw)
    except (binascii.Error, msgspec.DecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _parse_png(png_bytes: bytes, filename: str) -> ParsedImport | None:
    chunks = read_png_text_chunks(png_bytes)

    if AER_CHUNK in chunks:
        decoded = _decode_base64_json(chunks[AER_CHUNK])
        if decoded is None:
            raise ValueError(f"{AER_CHUNK} chunk is malformed")
        kind = _detect_native_kind(decoded)
        if kind is None:
            raise ValueError(f"{AER_CHUNK} chunk doesn't match a known AER kind")
        return ParsedImport(kind=kind, payload=decoded, card_image_bytes=png_bytes)

    if NAI_LORE_CHUNK in chunks:
        decoded = _decode_base64_json(chunks[NAI_LORE_CHUNK])
        if decoded is None:
            raise ValueError(f"{NAI_LORE_CHUNK} chunk is malformed")
        name = _name_from_filename(filename, "Imported Lorebook")
        return ParsedImport(
            kind="brain_library",
            payload=_decode_lorebook(decoded, name=name),
            card_image_bytes=png_bytes,
        )

    if ST_CHARA_CHUNK in chunks:
        decoded = _decode_base64_json(chunks[ST_CHARA_CHUNK])
        if decoded is None:
            raise ValueError(f"{ST_CHARA_CHUNK} chunk is malformed")
        return _dispatch_foreign_json(decoded, filename, card_image_bytes=png_bytes)

    raise ValueError("no embedded card data found")


def _dispatch_foreign_json(
    obj: dict, filename: str, card_image_bytes: bytes | None
) -> ParsedImport | None:
    spec = obj.get("spec")
    if isinstance(spec, str) and spec in ("chara_card_v2", "chara_card_v3"):
        return ParsedImport(
            kind="contact",
            payload=_decode_st_card_v2(obj),
            card_image_bytes=card_image_bytes,
        )

    if isinstance(obj.get("lorebookVersion"), (int, float)) and isinstance(
        obj.get("entries"), list
    ):
        name = _name_from_filename(filename, "Imported Lorebook")
        return ParsedImport(
            kind="brain_library",
            payload=_decode_lorebook(obj, name=name),
            card_image_bytes=card_image_bytes,
        )

    entries = obj.get("entries")
    if isinstance(entries, dict) and entries:
        if any(
            isinstance(v, dict) and ("content" in v or "key" in v)
            for v in entries.values()
        ):
            name = _name_from_filename(filename, "Imported World Info")
            return ParsedImport(
                kind="brain_library",
                payload=_decode_st_worldinfo(obj, name=name),
                card_image_bytes=card_image_bytes,
            )

    if _looks_like_st_card_v1(obj):
        return ParsedImport(
            kind="contact",
            payload=_decode_st_card_v1(obj),
            card_image_bytes=card_image_bytes,
        )

    return None


def _looks_like_st_card_v1(obj: dict) -> bool:
    """Flat 6-field shape with no v2 ``data`` wrapper."""
    if "data" in obj:
        return False
    required = ("name", "description", "personality", "scenario", "first_mes", "mes_example")
    return all(k in obj and isinstance(obj[k], str) for k in required)


def _detect_native_kind(obj: dict) -> str | None:
    # Explicit kind marker wins. Per-resource exporters stamp this on every
    # export; the heuristic below is the fallback for legacy files written
    # before the marker existed.
    explicit = obj.get("kind")
    if explicit in ("contact", "user", "scenario", "brain_library", "chat"):
        return explicit
    if "chat" in obj and isinstance(obj["chat"], dict):
        return "chat"
    if "emotions" in obj or "exampleMessages" in obj or "scenarios" in obj or "greeting" in obj:
        return "contact"
    if obj.get("environment") or obj.get("scene"):
        return "scenario"
    if "persona" in obj or "appearance" in obj:
        return "user"
    if "name" in obj and "brains" in obj:
        # Last-resort fallback; brain library shape with kind missing.
        return "brain_library"
    return None


def _brains_from_payload(payload: dict, kind: str, filename: str) -> dict:
    brains: list[dict] = []
    if kind == "chat":
        char = payload.get("character") or {}
        brains = list(char.get("brains") or [])
        name = str(char.get("name") or _name_from_filename(filename, "brains"))
    else:
        brains = list(payload.get("brains") or [])
        name = str(payload.get("name") or _name_from_filename(filename, "brains"))
    return {"brains": brains, "source_name": name}


def _name_from_filename(filename: str, default: str) -> str:
    if not filename:
        return default
    base = os.path.basename(filename)
    stem, _ = os.path.splitext(base)
    return stem.strip() or default


# ---------------------------------------------------------------------------
# ST character card decoders
# ---------------------------------------------------------------------------


def _decode_st_card_v2(obj: dict) -> dict:
    """Translate an ST v2/v3 character card to the AER contact export shape."""
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}

    name = str(data.get("name") or "").strip() or "Imported character"
    description = str(data.get("description") or "")
    personality = str(data.get("personality") or "")
    scenario = str(data.get("scenario") or "")
    first_mes = str(data.get("first_mes") or "")
    mes_example = str(data.get("mes_example") or "")
    system_prompt = str(data.get("system_prompt") or "")
    post_history_instructions = str(data.get("post_history_instructions") or "")

    alt_greetings = [
        str(g).strip()
        for g in (data.get("alternate_greetings") or [])
        if isinstance(g, str) and g.strip()
    ]

    tags_raw = data.get("tags") or []
    if isinstance(tags_raw, list):
        tags_str = ", ".join(
            str(t).strip() for t in tags_raw if isinstance(t, str) and str(t).strip()
        )
    else:
        tags_str = ""

    # Persona absorbs ST's description AND personality fields — both are
    # prompt content in the source format (rendered into the model's
    # context), unlike AER's ``description`` which is cosmetic / UI-only.
    # Order: description first (usually the longer prose with backstory
    # and behaviour notes), then personality (often short trait lines)
    # if non-empty and distinct. system_prompt — if present — appends
    # as a labelled footer so the user can see and edit it as a single
    # block.
    persona_parts: list[str] = []
    if description.strip():
        persona_parts.append(description.strip())
    if personality.strip() and personality.strip() != description.strip():
        persona_parts.append(personality.strip())
    if system_prompt.strip():
        persona_parts.append(f"General instructions:\n{system_prompt.strip()}")
    persona = "\n\n".join(persona_parts)

    # Greeting + scenario + alts: two-branch rule.
    has_scenario = bool(scenario.strip())
    scenarios_payload: list[dict] = []
    contact_greeting = first_mes

    if has_scenario:
        scenarios_payload.append({
            "name": f"Meeting {name}",
            "greeting": first_mes,
            "scene": scenario,
            "is_default": True,
        })
        for idx, alt in enumerate(alt_greetings, start=1):
            scenarios_payload.append({
                "name": f"Alternate Greeting #{idx}",
                "greeting": alt,
                "scene": scenario,
            })
    else:
        for idx, alt in enumerate(alt_greetings, start=1):
            scenarios_payload.append({
                "name": f"Alternate Greeting #{idx}",
                "greeting": alt,
                "scene": "",
            })

    # Brains from character_book.
    char_book = data.get("character_book") if isinstance(data.get("character_book"), dict) else {}
    recursive_scanning = bool(char_book.get("recursive_scanning", False))
    book_entries = char_book.get("entries") or []
    brains: list[dict] = []
    if isinstance(book_entries, list):
        for entry in book_entries:
            if isinstance(entry, dict):
                b = _translate_st_wi_entry(entry, recursive_scanning=recursive_scanning)
                if b:
                    brains.append(b)

    # mes_example → AER example chats. If parsing produced nothing
    # despite a non-empty source (e.g. the card used a literal character
    # name as the speaker rather than the ``{{char}}`` macro, or used
    # an unconventional layout), fall back to dumping the raw text into
    # the persona footer so it's preserved and reviewable rather than
    # silently lost.
    example_chats = _parse_mes_example(mes_example)
    if mes_example.strip() and not example_chats:
        sep = "\n\n" if persona else ""
        persona = f"{persona}{sep}Example messages:\n{mes_example.strip()}"

    # AER's ``description`` is cosmetic / UI-only — ST's prompt content
    # (description + personality) lives in ``persona`` instead. ST's
    # ``creator_notes`` is meta-commentary intended for human readers of
    # the card; route it into the UI-only description. ST's ``creator``
    # names the card's author and lands in the dedicated ``author`` field.
    creator_notes = str(data.get("creator_notes") or "").strip()
    creator = str(data.get("creator") or "").strip()
    payload: dict[str, Any] = {
        "name": name,
        "description": creator_notes,
        "author": creator,
        "persona": persona,
        "tags": tags_str,
        "greeting": contact_greeting,
        "brains": brains,
        "exampleMessages": example_chats,
        # ST cards are predominantly authored for roleplay use.
        # The "chat" style trains shorter, more conversational replies
        # that doesn't match the source intent; default to "roleplay"
        # so the imported character feels at home from turn one.
        "style": "roleplay",
    }
    if scenarios_payload:
        payload["scenarios"] = scenarios_payload

    if post_history_instructions.strip():
        payload["reminderBrain"] = {
            "name": "Notes",
            "content": post_history_instructions,
            "depth": 0,
        }

    return payload


def _decode_st_card_v1(obj: dict) -> dict:
    """Translate a legacy flat ST card (no v2 wrapper) by lifting its 6
    fields into a synthetic v2 ``data`` block and reusing the v2 path."""
    synthetic = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": obj.get("name", ""),
            "description": obj.get("description", ""),
            "personality": obj.get("personality", ""),
            "scenario": obj.get("scenario", ""),
            "first_mes": obj.get("first_mes", ""),
            "mes_example": obj.get("mes_example", ""),
            "creator_notes": "",
            "system_prompt": "",
            "post_history_instructions": "",
            "alternate_greetings": [],
            "tags": [],
            "extensions": {},
        },
    }
    return _decode_st_card_v2(synthetic)


# ---------------------------------------------------------------------------
# mes_example parser
# ---------------------------------------------------------------------------


_START_TAG_RE = re.compile(r"<\s*START\s*>", re.IGNORECASE)
_SPEAKER_LINE_RE = re.compile(
    r"^\s*\{\{(?P<who>user|char)\}\}\s*:\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _parse_mes_example(text: str) -> list[dict]:
    """Translate ST's ``mes_example`` field to AER ExampleChat dicts.

    ST's format is a sequence of ``<START>``-delimited blocks; within
    each block lines like ``{{user}}: hi`` are user bubbles and
    ``{{char}}: hi`` are contact bubbles. Multi-line bodies belong to
    the most recent speaker line.

    Contact bubbles get ``emotion: "neutral"`` by default — ST's
    format doesn't carry emotion, and the AER renderer needs an
    ``Emotion:`` line on every contact bubble to keep the streaming
    parser's bubble boundaries intact.

    Returns an empty list when nothing matched the speaker pattern —
    the caller is expected to detect that case and fall back to
    something like a persona footer rather than silently losing the
    raw text.
    """
    if not text or not text.strip():
        return []
    blocks = _START_TAG_RE.split(text)
    out: list[dict] = []
    example_idx = 0
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        messages: list[dict] = []
        for raw_line in block.split("\n"):
            line = raw_line.rstrip("\r")
            stripped = line.strip()
            if not stripped:
                if messages:
                    messages[-1]["text"] += "\n"
                continue
            m = _SPEAKER_LINE_RE.match(stripped)
            if m:
                speaker = m.group("who").lower()
                body = (m.group("rest") or "").strip()
                is_contact = speaker == "char"
                msg: dict[str, Any] = {"isContact": is_contact, "text": body}
                if is_contact:
                    msg["emotion"] = "neutral"
                messages.append(msg)
            else:
                if messages:
                    messages[-1]["text"] = (messages[-1]["text"] + "\n" + line).rstrip()
                # else: leading prose with no speaker tag — drop
        for msg in messages:
            msg["text"] = msg["text"].strip()
        messages = [m for m in messages if m["text"]]
        if messages:
            example_idx += 1
            out.append({
                "name": f"Example {example_idx}",
                "messages": messages,
                "userName": "",
                "style": "chat",
            })
    return out


# ---------------------------------------------------------------------------
# ST WorldInfo translation
# ---------------------------------------------------------------------------

# ST's selectiveLogic enum values (per spec):
#   0 = AND_ANY  — primary AND any secondary key match
#   1 = NOT_ALL  — primary AND NOT (all secondary)
#   2 = NOT_ANY  — primary AND NOT (any secondary)
#   3 = AND_ALL  — primary AND every secondary individually
_SL_AND_ANY = 0
_SL_NOT_ALL = 1
_SL_NOT_ANY = 2
_SL_AND_ALL = 3


def _decode_st_worldinfo(obj: dict, *, name: str) -> dict:
    """Translate a standalone WI export to the AER brain library shape."""
    entries_raw = obj.get("entries")
    entries: list[dict] = []
    if isinstance(entries_raw, dict):
        try:
            # Numeric-key ordering when keys parse as ints.
            sorted_keys = sorted(
                entries_raw.keys(),
                key=lambda x: (int(x) if str(x).lstrip("-").isdigit() else 0),
            )
        except (TypeError, ValueError):
            sorted_keys = list(entries_raw.keys())
        for k in sorted_keys:
            v = entries_raw[k]
            if isinstance(v, dict):
                entries.append(v)
    elif isinstance(entries_raw, list):
        entries = [e for e in entries_raw if isinstance(e, dict)]

    brains: list[dict] = []
    for entry in entries:
        b = _translate_st_wi_entry(entry, recursive_scanning=False)
        if b:
            brains.append(b)

    return {
        "kind": "brain_library",
        "name": name,
        "description": "",
        "tags": "",
        "brains": brains,
    }


def _translate_st_wi_entry(
    entry: dict, *, recursive_scanning: bool
) -> dict | None:
    content = str(entry.get("content") or "").strip()
    if not content:
        return None

    name = str(entry.get("comment") or entry.get("name") or "").strip() or "Entry"

    case_sensitive = entry.get("caseSensitive")
    case_sensitive_default = bool(case_sensitive) if isinstance(case_sensitive, bool) else False
    match_whole_words = bool(entry.get("matchWholeWords"))

    scan_depth = entry.get("scanDepth")
    search_messages = (
        int(scan_depth) if isinstance(scan_depth, int) and scan_depth > 0 else None
    )

    def _key_list(raw: Any) -> list[dict]:
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for k in raw:
            if isinstance(k, str) and k.strip():
                out.append(
                    _parse_key_pattern(
                        k,
                        case_sensitive=case_sensitive_default,
                        match_whole_words=match_whole_words,
                        search_messages=search_messages,
                        search_range=None,
                    )
                )
        return out

    primary_keys = _key_list(entry.get("key"))
    secondary_keys = _key_list(entry.get("keysecondary"))

    constant = bool(entry.get("constant", False))
    disabled = bool(entry.get("disable", False))

    advanced: dict | None
    keys_field: list[dict]

    if constant:
        # constant short-circuits selective/probability — brain always fires.
        keys_field = primary_keys
        advanced = {"type": "true"}
    else:
        keys_field, advanced = _st_selective_logic_to_advanced(
            primary_keys,
            secondary_keys,
            selective=bool(entry.get("selective", True)),
            selective_logic=_coerce_int(entry.get("selectiveLogic", _SL_AND_ANY)),
        )

        probability = _coerce_int(entry.get("probability", 100))
        use_probability = bool(entry.get("useProbability", True))
        if use_probability and probability < 100:
            # ST treats probability as a multiplicative gate on top of the
            # keyword/selective check, so AND it in. probability=100 is a
            # no-op (the default activation is "fire when keys match").
            advanced = _and_with(
                advanced, {"type": "random_chance", "percent": float(probability)}
            )

    exclude_recursion = bool(entry.get("excludeRecursion", False))
    prevent_recursion = bool(entry.get("preventRecursion", False))
    cascades = recursive_scanning and not prevent_recursion

    brain: dict[str, Any] = {
        "name": name,
        "content": content,
        "keys": keys_field,
    }
    if advanced is not None:
        brain["advanced"] = advanced
    if cascades:
        brain["cascades"] = True
    if exclude_recursion:
        brain["blocks_recursion"] = True
    if disabled:
        brain["disabled"] = True
    return brain


def _st_selective_logic_to_advanced(
    primary_keys: list[dict],
    secondary_keys: list[dict],
    *,
    selective: bool,
    selective_logic: int,
) -> tuple[list[dict], dict | None]:
    """Combine primary + secondary keys per ST's selectiveLogic.

    Returns ``(keys_field, advanced)``. When advanced is set, the primary
    keys also live inside the tree (because AER's engine ORs the
    ``keys`` field with ``advanced`` — we want strict AND), so the
    ``keys_field`` is empty in that case.
    """
    if not primary_keys and not secondary_keys:
        return [], None
    if not selective or not secondary_keys:
        return primary_keys, None
    if not primary_keys:
        # Unusual but valid: only secondary keys with selective=true. The
        # selective semantic requires both primary and secondary to match,
        # so with no primary the brain effectively never fires. Encode
        # as a never-true placeholder.
        return [], {"type": "not", "child": {"type": "true"}}

    primary_cond: dict = {"type": "keyword", "keys": list(primary_keys)}
    if selective_logic == _SL_AND_ANY:
        secondary_cond: dict = {"type": "keyword", "keys": list(secondary_keys)}
        return [], {"type": "and", "children": [primary_cond, secondary_cond]}
    if selective_logic == _SL_AND_ALL:
        children = [primary_cond]
        for k in secondary_keys:
            children.append({"type": "keyword", "keys": [k]})
        return [], {"type": "and", "children": children}
    if selective_logic == _SL_NOT_ANY:
        secondary_cond = {"type": "keyword", "keys": list(secondary_keys)}
        return [], {
            "type": "and",
            "children": [primary_cond, {"type": "not", "child": secondary_cond}],
        }
    if selective_logic == _SL_NOT_ALL:
        all_children = [{"type": "keyword", "keys": [k]} for k in secondary_keys]
        all_secondary: dict = {"type": "and", "children": all_children}
        return [], {
            "type": "and",
            "children": [primary_cond, {"type": "not", "child": all_secondary}],
        }
    # Unknown selectiveLogic value — fall back to AND_ANY.
    secondary_cond = {"type": "keyword", "keys": list(secondary_keys)}
    return [], {"type": "and", "children": [primary_cond, secondary_cond]}


# ---------------------------------------------------------------------------
# Lorebook translation
# ---------------------------------------------------------------------------


def _decode_lorebook(obj: dict, *, name: str) -> dict:
    """Translate a lorebook JSON to the AER brain library shape.

    Two-pass: pass 1 mints AER brain IDs for every entry so that
    ``lore``-type advanced conditions in pass 2 can resolve their
    ``entryId`` references to the mapped AER id.
    """
    entries_raw = obj.get("entries") or []
    if not isinstance(entries_raw, list):
        entries_raw = []

    categories_raw = obj.get("categories") or []
    category_disabled: dict[str, bool] = {}
    if isinstance(categories_raw, list):
        for c in categories_raw:
            if isinstance(c, dict):
                cid = str(c.get("id") or "")
                if cid:
                    category_disabled[cid] = not bool(c.get("enabled", True))

    # Pass 1: collect entries with non-empty text and mint ids.
    valid: list[dict] = []
    id_map: dict[str, str] = {}
    for entry in entries_raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        src_id = str(entry.get("id") or "")
        if src_id:
            id_map[src_id] = uuid.uuid4().hex
        valid.append(entry)

    # Pass 2: translate.
    brains: list[dict] = []
    for entry in valid:
        cat_id = str(entry.get("category") or "")
        cat_disabled = category_disabled.get(cat_id, False) if cat_id else False
        b = _translate_lorebook_entry(
            entry, id_map=id_map, category_disabled=cat_disabled
        )
        if b:
            brains.append(b)

    return {
        "kind": "brain_library",
        "name": name,
        "description": "",
        "tags": "",
        "brains": brains,
    }


def _translate_lorebook_entry(
    entry: dict, *, id_map: dict[str, str], category_disabled: bool
) -> dict | None:
    text = str(entry.get("text") or "").strip()
    if not text:
        return None

    display_name = str(entry.get("displayName") or "").strip()
    if not display_name:
        display_name = text[:40].strip() or "Entry"

    search_range = entry.get("searchRange")
    search_range_chars = (
        int(search_range) if isinstance(search_range, int) and search_range > 0 else None
    )

    keys_raw = entry.get("keys") or []
    keys: list[dict] = []
    if isinstance(keys_raw, list):
        for k in keys_raw:
            if isinstance(k, str) and k.strip():
                stripped = k.lstrip("$") if k.startswith("$") else k
                key = _parse_key_pattern(
                    stripped,
                    case_sensitive=False,
                    match_whole_words=False,
                    search_messages=None,
                    search_range=search_range_chars,
                )
                keys.append(key)

    enabled = entry.get("enabled", True)
    enabled = bool(enabled) if isinstance(enabled, bool) else True
    disabled = (not enabled) or category_disabled

    force_activation = bool(entry.get("forceActivation", False))
    non_story_activatable = bool(entry.get("nonStoryActivatable", False))

    if force_activation:
        advanced: dict | None = {"type": "true"}
    else:
        conds_raw = entry.get("advancedConditions") or []
        translated: list[dict] = []
        if isinstance(conds_raw, list):
            for cond in conds_raw:
                if isinstance(cond, dict):
                    t = _translate_lorebook_condition(cond, id_map)
                    if t is not None:
                        translated.append(t)
        if not translated:
            advanced = None
        elif len(translated) == 1:
            advanced = translated[0]
        else:
            advanced = {"type": "or", "children": translated}

    brain_id = id_map.get(str(entry.get("id") or "")) or uuid.uuid4().hex
    brain: dict[str, Any] = {
        "id": brain_id,
        "name": display_name,
        "content": text,
        "keys": keys,
    }
    if advanced is not None:
        brain["advanced"] = advanced
    if non_story_activatable:
        brain["cascades"] = True
    if disabled:
        brain["disabled"] = True
    return brain


_LORE_STRING_OPS = {
    "equals": "equals",
    "includes": "includes",
    "startswith": "starts_with",
    "endswith": "ends_with",
}

_LORE_NUMERIC_OPS = {
    "=": "==",
    "!=": "!=",
    "<": "<",
    ">": ">",
    "<=": "<=",
    ">=": ">=",
}


def _translate_lorebook_condition(node: dict, id_map: dict[str, str]) -> dict | None:
    """Recursive translator for lorebook ``advancedConditions`` nodes.

    Nodes we can't represent (model / storymode / unknown) degrade to
    ``CondTrue`` so the surrounding tree still evaluates.
    """
    t = node.get("type")
    if t == "true":
        return {"type": "true"}

    if t == "key":
        key_str = str(node.get("key") or "").strip()
        if not key_str:
            return {"type": "true"}
        stripped = key_str.lstrip("$") if key_str.startswith("$") else key_str
        key = _parse_key_pattern(
            stripped,
            case_sensitive=False,
            match_whole_words=False,
            search_messages=None,
            search_range=None,
        )
        return {"type": "keyword", "keys": [key]}

    if t == "lore":
        src_id = str(node.get("entryId") or "")
        mapped = id_map.get(src_id)
        if mapped:
            return {"type": "brain_active", "brain_id": mapped}
        # Reference to an entry that didn't make it into the library —
        # the brain will never be active, so the condition is always false.
        return {"type": "not", "child": {"type": "true"}}

    if t == "not":
        inner = node.get("condition")
        if isinstance(inner, dict):
            translated = _translate_lorebook_condition(inner, id_map)
            if translated is not None:
                return {"type": "not", "child": translated}
        # not(absent) → not(true) = false
        return {"type": "not", "child": {"type": "true"}}

    if t in ("and", "or"):
        children_raw = node.get("children") or []
        if not isinstance(children_raw, list):
            children_raw = []
        children: list[dict] = []
        for c in children_raw:
            if isinstance(c, dict):
                tc = _translate_lorebook_condition(c, id_map)
                if tc is not None:
                    children.append(tc)
        if not children:
            # Empty and → true; empty or → false (per the spec semantics).
            return {"type": "true"} if t == "and" else {"type": "not", "child": {"type": "true"}}
        if len(children) == 1:
            return children[0]
        return {"type": t, "children": children}

    if t == "equation":
        terms = node.get("terms")
        if not isinstance(terms, list) or not terms:
            return {"type": "true"}
        first = terms[0] if isinstance(terms[0], dict) else {}
        lhs = _lorebook_term_to_numeric_value(first.get("value"))
        if lhs is None:
            return {"type": "true"}
        comparison = node.get("comparison", "=")
        op = _LORE_NUMERIC_OPS.get(comparison, "==")
        target = node.get("target", 0)
        rhs = _lorebook_term_to_numeric_value(target)
        if rhs is None:
            return {"type": "true"}
        return {"type": "numeric_compare", "lhs": lhs, "op": op, "rhs": rhs}

    if t == "string":
        comparison = node.get("comparison", "includes")
        op = _LORE_STRING_OPS.get(comparison, "includes")
        return {
            "type": "string_compare",
            "lhs": _lorebook_term_to_string_value(node.get("left")),
            "op": op,
            "rhs": _lorebook_term_to_string_value(node.get("right")),
        }

    if t == "random":
        chance = _coerce_int(node.get("chance", 50))
        return {"type": "random_chance", "percent": float(chance)}

    if t in ("model", "storymode"):
        # AER has no equivalent signal; always-true so the surrounding
        # tree still evaluates as intended.
        return {"type": "true"}

    return {"type": "true"}


def _lorebook_term_to_numeric_value(value: Any) -> dict | None:
    """Map a lorebook term value (literal number or named variable) to an
    AER ``NumericValue`` dict. Returns None for unrepresentable cases
    (e.g. ``paragraphCount`` / ``characterCount`` — see plan)."""
    if isinstance(value, bool):
        # Distinguish from numeric (bools are ints in Python).
        return None
    if isinstance(value, (int, float)):
        return {"kind": "literal", "literal": float(value)}
    if value == "currentStep":
        return {"kind": "variable", "variable": "message_count"}
    # paragraphCount, characterCount — no AER equivalent. Returning None
    # causes the surrounding equation cond to degrade to CondTrue.
    return None


def _lorebook_term_to_string_value(value: Any) -> dict:
    if isinstance(value, str):
        return {"kind": "literal", "literal": value}
    return {"kind": "literal", "literal": ""}


# ---------------------------------------------------------------------------
# Shared key parser
# ---------------------------------------------------------------------------


_REGEX_KEY_RE = re.compile(r"^/(.+)/([gimsuxA-Z]*)$")


def _parse_key_pattern(
    raw: str,
    *,
    case_sensitive: bool,
    match_whole_words: bool,
    search_messages: int | None,
    search_range: int | None,
) -> dict:
    """Parse a key string into an AER ``BrainKey`` dict.

    ``/pattern/flags`` becomes a regex key; ``flags`` characters in
    ``imsux`` are encoded into the pattern via ``(?...)`` prefixes so
    they survive into the AER engine without needing a dedicated flags
    field. (The ``g`` flag is non-semantic for matching and is ignored.)
    """
    s = raw.strip()
    m = _REGEX_KEY_RE.match(s)
    if m and m.group(1):
        body = m.group(1)
        flags = (m.group(2) or "").lower()
        prefix = ""
        for f in ("i", "s", "m", "x"):
            if f in flags:
                prefix += f"(?{f})"
        return {
            "pattern": prefix + body,
            "is_regex": True,
            # case_sensitive / match_whole_words are ignored for regex keys.
            "case_sensitive": False,
            "match_whole_words": False,
            "search_range": search_range,
            "search_messages": search_messages,
        }
    return {
        "pattern": s,
        "is_regex": False,
        "case_sensitive": case_sensitive,
        "match_whole_words": match_whole_words,
        "search_range": search_range,
        "search_messages": search_messages,
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _and_with(existing: dict | None, addition: dict) -> dict:
    """AND ``addition`` into an existing advanced tree (creating one when
    absent)."""
    if existing is None:
        return addition
    if existing.get("type") == "and" and isinstance(existing.get("children"), list):
        return {"type": "and", "children": existing["children"] + [addition]}
    return {"type": "and", "children": [existing, addition]}
