"""Foreign-format parsers + PNG metadata round-trip."""
from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from server import external_formats as ef


# ---------------------------------------------------------------------------
# PNG metadata
# ---------------------------------------------------------------------------


def _png_with_text(chunks: dict[str, str], size=(8, 8)) -> bytes:
    """Build a tiny PNG carrying ``chunks`` as tEXt entries."""
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    for k, v in chunks.items():
        info.add_text(k, v)
    buf = io.BytesIO()
    Image.new("RGB", size, color=(0, 0, 0)).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def _b64_json(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


class TestPngMetadata:
    def test_read_no_chunks(self):
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "PNG")
        assert ef.read_png_text_chunks(buf.getvalue()) == {}

    def test_read_single_text_chunk(self):
        png = _png_with_text({"aertavern_data": "hello"})
        chunks = ef.read_png_text_chunks(png)
        assert chunks.get("aertavern_data") == "hello"

    def test_read_multiple_chunks(self):
        png = _png_with_text({"chara": "a", "naidata": "b", "aertavern_data": "c"})
        chunks = ef.read_png_text_chunks(png)
        assert chunks["chara"] == "a"
        assert chunks["naidata"] == "b"
        assert chunks["aertavern_data"] == "c"

    def test_round_trip_via_write_png_with_data(self):
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color=(255, 0, 0)).save(buf, "PNG")
        sample = {"kind": "brain_library", "name": "Foo", "brains": []}
        out = ef.write_png_with_data(buf.getvalue(), sample)
        chunks = ef.read_png_text_chunks(out)
        decoded = json.loads(base64.b64decode(chunks["aertavern_data"]).decode("utf-8"))
        assert decoded == sample

    def test_round_trip_non_ascii_payload(self):
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "PNG")
        sample = {"name": "アリス", "description": "日本語 description"}
        out = ef.write_png_with_data(buf.getvalue(), sample)
        chunks = ef.read_png_text_chunks(out)
        decoded = json.loads(base64.b64decode(chunks["aertavern_data"]).decode("utf-8"))
        assert decoded == sample

    def test_write_accepts_non_png_source(self):
        # JPEG decoded and re-emitted as PNG.
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), color=(0, 128, 0)).save(buf, "JPEG")
        out = ef.write_png_with_data(buf.getvalue(), {"name": "X"})
        assert out[:8] == b"\x89PNG\r\n\x1a\n"
        chunks = ef.read_png_text_chunks(out)
        decoded = json.loads(base64.b64decode(chunks["aertavern_data"]).decode("utf-8"))
        assert decoded == {"name": "X"}


# ---------------------------------------------------------------------------
# ST card v2
# ---------------------------------------------------------------------------


def _make_st_v2(**overrides) -> dict:
    data = {
        "name": "Alice",
        "description": "A friendly AI.",
        "personality": "Curious and kind.",
        "scenario": "",
        "first_mes": "Hello!",
        "mes_example": "",
        "creator_notes": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "tags": [],
        "extensions": {},
    }
    data.update(overrides)
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


class TestStCardV2:
    def test_branch_a_no_scenario_no_alts(self):
        card = _make_st_v2()
        p = ef._decode_st_card_v2(card)
        assert p["greeting"] == "Hello!"
        assert "scenarios" not in p or not p["scenarios"]

    def test_branch_a_alts_only(self):
        card = _make_st_v2(alternate_greetings=["Hey!", "Yo!"])
        p = ef._decode_st_card_v2(card)
        assert p["greeting"] == "Hello!"
        scenarios = p["scenarios"]
        assert [s["name"] for s in scenarios] == [
            "Alternate Greeting #1", "Alternate Greeting #2",
        ]
        assert all(s.get("scene", "") == "" for s in scenarios)
        assert not any(s.get("is_default") for s in scenarios)

    def test_branch_b_scenario_no_alts(self):
        card = _make_st_v2(scenario="A coffee shop.")
        p = ef._decode_st_card_v2(card)
        scenarios = p["scenarios"]
        assert len(scenarios) == 1
        meeting = scenarios[0]
        assert meeting["name"] == "Meeting Alice"
        assert meeting["greeting"] == "Hello!"
        assert meeting["scene"] == "A coffee shop."
        assert meeting["is_default"] is True
        assert p["greeting"] == "Hello!"  # fallback kept

    def test_branch_b_scenario_and_alts(self):
        card = _make_st_v2(
            scenario="A coffee shop.",
            alternate_greetings=["Hey!", "Yo!"],
        )
        p = ef._decode_st_card_v2(card)
        names = [s["name"] for s in p["scenarios"]]
        assert names == ["Meeting Alice", "Alternate Greeting #1", "Alternate Greeting #2"]
        scenes = [s["scene"] for s in p["scenarios"]]
        assert scenes == ["A coffee shop."] * 3
        assert p["scenarios"][0]["is_default"] is True

    def test_system_prompt_in_persona_footer(self):
        card = _make_st_v2(personality="Calm.", system_prompt="Stay in character.")
        p = ef._decode_st_card_v2(card)
        assert "Calm." in p["persona"]
        assert "General instructions:" in p["persona"]
        assert "Stay in character." in p["persona"]

    def test_description_is_prompt_content_so_goes_to_persona(self):
        # ST's ``description`` is part of the model's context, unlike
        # AER's ``description`` which is UI-only. The decoder routes it
        # into ``persona`` so the imported character actually reads the
        # backstory at generation time. AER's description stays empty.
        card = _make_st_v2(
            description="Long backstory paragraph about the character.",
            personality="Cheery, curious.",
        )
        p = ef._decode_st_card_v2(card)
        assert "Long backstory paragraph" in p["persona"]
        assert "Cheery, curious." in p["persona"]
        # Description first (longer prose), personality second.
        bs_idx = p["persona"].find("Long backstory")
        pn_idx = p["persona"].find("Cheery")
        assert 0 <= bs_idx < pn_idx
        # AER description stays empty when ST has no creator_notes
        # (creator_notes is the only ST field routed to the UI-only
        # description; description/personality are prompt content and
        # live on persona).
        assert p["description"] == ""

    def test_persona_dedupes_when_personality_equals_description(self):
        card = _make_st_v2(
            description="Same text",
            personality="Same text",
        )
        p = ef._decode_st_card_v2(card)
        assert p["persona"].count("Same text") == 1

    def test_post_history_instructions_become_reminder_brain(self):
        card = _make_st_v2(post_history_instructions="Be terse.")
        p = ef._decode_st_card_v2(card)
        rb = p["reminderBrain"]
        assert rb["name"] == "Notes"
        assert rb["content"] == "Be terse."
        assert rb["depth"] == 0

    def test_empty_post_history_no_reminder(self):
        p = ef._decode_st_card_v2(_make_st_v2())
        assert "reminderBrain" not in p

    def test_tags_array_joined(self):
        card = _make_st_v2(tags=["friendly", "ai", "test"])
        p = ef._decode_st_card_v2(card)
        assert p["tags"] == "friendly, ai, test"

    def test_character_book_entries_become_brains(self):
        card = _make_st_v2(character_book={
            "recursive_scanning": False,
            "entries": [
                {"comment": "Coffee", "content": "About coffee.", "key": ["coffee"]},
                {"comment": "Tea", "content": "About tea.", "key": ["tea"]},
            ],
        })
        p = ef._decode_st_card_v2(card)
        names = [b["name"] for b in p["brains"]]
        assert names == ["Coffee", "Tea"]

    def test_recursive_scanning_cascades_through(self):
        card = _make_st_v2(character_book={
            "recursive_scanning": True,
            "entries": [{"comment": "X", "content": "X content", "key": ["x"]}],
        })
        p = ef._decode_st_card_v2(card)
        assert p["brains"][0].get("cascades") is True

    def test_mes_example_parses_into_chats(self):
        card = _make_st_v2(mes_example=(
            "<START>\n{{user}}: Hi\n{{char}}: Hello!\n"
            "<START>\n{{user}}: Bye\n{{char}}: Later!"
        ))
        p = ef._decode_st_card_v2(card)
        chats = p["exampleMessages"]
        assert len(chats) == 2
        assert chats[0]["name"] == "Example 1"
        assert chats[1]["name"] == "Example 2"

    def test_mes_example_parse_failure_dumps_into_persona(self):
        # Lines using a literal name (no {{char}}/{{user}} macros) — our
        # parser doesn't recognise them; the fallback dumps the raw
        # text into the persona footer rather than silently dropping.
        unrecognised = "Alice: Hello!\nUser: Hi"
        card = _make_st_v2(mes_example=unrecognised)
        p = ef._decode_st_card_v2(card)
        assert p["exampleMessages"] == []
        assert "Example messages:" in p["persona"]
        assert "Alice: Hello!" in p["persona"]

    def test_creator_notes_go_into_description(self):
        card = _make_st_v2(creator_notes="A note from the author.")
        p = ef._decode_st_card_v2(card)
        assert p["description"] == "A note from the author."
        assert p["author"] == ""

    def test_creator_lands_in_author_field(self):
        card = _make_st_v2(
            creator_notes="A note from the author.",
            creator="Some Author",
        )
        p = ef._decode_st_card_v2(card)
        # Creator is the card's author; routed to the dedicated field
        # so it stays out of the UI-only description.
        assert p["description"] == "A note from the author."
        assert p["author"] == "Some Author"

    def test_creator_only_no_notes(self):
        card = _make_st_v2(creator="Solo Author")
        p = ef._decode_st_card_v2(card)
        assert p["description"] == ""
        assert p["author"] == "Solo Author"

    def test_default_style_is_roleplay(self):
        # ST cards are predominantly roleplay-flavoured; imports default
        # to that style rather than the chat default.
        card = _make_st_v2()
        p = ef._decode_st_card_v2(card)
        assert p["style"] == "roleplay"

    def test_drop_list(self):
        card = _make_st_v2(character_version="1.0")
        p = ef._decode_st_card_v2(card)
        # Confirm dropped fields don't leak into the payload.
        for k in ("character_version", "extensions"):
            assert k not in p


# ---------------------------------------------------------------------------
# ST card v1
# ---------------------------------------------------------------------------


class TestStCardV1:
    def test_flat_six_field_card(self):
        v1 = {
            "name": "Bob", "description": "Bob desc",
            "personality": "Bob persona", "scenario": "",
            "first_mes": "Hi", "mes_example": "",
        }
        # detect_format on the flat shape
        from server.importers import detect_format
        assert detect_format(v1) == "contact"
        parsed = ef.parse_external(json.dumps(v1).encode("utf-8"), "bob.json")
        assert parsed["kind"] == "contact"
        assert parsed["payload"]["name"] == "Bob"
        assert parsed["payload"]["greeting"] == "Hi"


# ---------------------------------------------------------------------------
# ST WorldInfo standalone
# ---------------------------------------------------------------------------


class TestStWi:
    def _entry(self, **kw):
        return {"comment": "E", "content": "content", "key": ["k"], **kw}

    def test_constant_short_circuits_to_cond_true(self):
        b = ef._translate_st_wi_entry(self._entry(constant=True), recursive_scanning=False)
        assert b["advanced"] == {"type": "true"}
        assert b["keys"], "primary keys are preserved alongside constant=true"

    def test_disable_imports_as_disabled(self):
        b = ef._translate_st_wi_entry(self._entry(disable=True), recursive_scanning=False)
        assert b.get("disabled") is True

    def test_case_sensitive_true_on_keys(self):
        b = ef._translate_st_wi_entry(self._entry(caseSensitive=True), recursive_scanning=False)
        assert b["keys"][0]["case_sensitive"] is True

    def test_match_whole_words(self):
        b = ef._translate_st_wi_entry(self._entry(matchWholeWords=True), recursive_scanning=False)
        assert b["keys"][0]["match_whole_words"] is True

    def test_scan_depth_to_search_messages(self):
        b = ef._translate_st_wi_entry(self._entry(scanDepth=4), recursive_scanning=False)
        assert b["keys"][0]["search_messages"] == 4

    def test_exclude_recursion_to_blocks(self):
        b = ef._translate_st_wi_entry(self._entry(excludeRecursion=True), recursive_scanning=False)
        assert b.get("blocks_recursion") is True

    def test_prevent_recursion_overrides_global(self):
        b = ef._translate_st_wi_entry(
            self._entry(preventRecursion=True), recursive_scanning=True,
        )
        # cascades is suppressed by preventRecursion even when the card
        # has global recursive_scanning=true.
        assert "cascades" not in b

    def test_default_probability_no_extra_advanced(self):
        b = ef._translate_st_wi_entry(self._entry(), recursive_scanning=False)
        # Defaults (probability=100, useProbability=true) shouldn't add
        # CondTrue or random_chance — keys do the work.
        assert "advanced" not in b

    def test_probability_below_100_adds_random_chance(self):
        b = ef._translate_st_wi_entry(
            self._entry(probability=40, useProbability=True),
            recursive_scanning=False,
        )
        assert b["advanced"] == {"type": "random_chance", "percent": 40.0}

    def test_selective_and_any(self):
        b = ef._translate_st_wi_entry(
            self._entry(keysecondary=["k2"], selective=True, selectiveLogic=0),
            recursive_scanning=False,
        )
        # Both primary and secondary land inside the advanced tree;
        # brain.keys is empty so AER doesn't OR them in.
        assert b["keys"] == []
        adv = b["advanced"]
        assert adv["type"] == "and"
        assert len(adv["children"]) == 2

    def test_regex_key_form(self):
        b = ef._translate_st_wi_entry(self._entry(key=["/foo/i"]), recursive_scanning=False)
        k = b["keys"][0]
        assert k["is_regex"] is True
        assert k["pattern"].startswith("(?i)")

    def test_empty_content_drops_entry(self):
        b = ef._translate_st_wi_entry(self._entry(content=""), recursive_scanning=False)
        assert b is None


# ---------------------------------------------------------------------------
# Lorebook
# ---------------------------------------------------------------------------


class TestLorebook:
    def test_basic_entry(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [{"id": "a", "displayName": "Foo", "text": "Foo body", "keys": ["foo"]}],
            "categories": [],
            "settings": {},
        }
        p = ef._decode_lorebook(lb, name="Test")
        assert p["kind"] == "brain_library"
        assert p["name"] == "Test"
        assert p["brains"][0]["name"] == "Foo"
        assert p["brains"][0]["content"] == "Foo body"

    def test_force_activation_to_cond_true(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [{"id": "a", "displayName": "F", "text": "x", "forceActivation": True}],
            "categories": [], "settings": {},
        }
        p = ef._decode_lorebook(lb, name="T")
        assert p["brains"][0]["advanced"] == {"type": "true"}

    def test_disabled_entry_marked(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [{"id": "a", "displayName": "F", "text": "x", "enabled": False}],
            "categories": [], "settings": {},
        }
        p = ef._decode_lorebook(lb, name="T")
        assert p["brains"][0].get("disabled") is True

    def test_advanced_conditions_or_folded(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [{"id": "a", "displayName": "F", "text": "x",
                          "advancedConditions": [
                              {"type": "random", "chance": 30},
                              {"type": "random", "chance": 60},
                          ]}],
            "categories": [], "settings": {},
        }
        p = ef._decode_lorebook(lb, name="T")
        adv = p["brains"][0]["advanced"]
        assert adv["type"] == "or"
        assert len(adv["children"]) == 2

    def test_lore_reference_resolves_to_brain_active(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [
                {"id": "a", "displayName": "A", "text": "A body"},
                {"id": "b", "displayName": "B", "text": "B body",
                 "advancedConditions": [{"type": "lore", "entryId": "a"}]},
            ],
            "categories": [], "settings": {},
        }
        p = ef._decode_lorebook(lb, name="T")
        a_id = p["brains"][0]["id"]
        b_adv = p["brains"][1]["advanced"]
        assert b_adv == {"type": "brain_active", "brain_id": a_id}

    def test_lore_reference_unknown_is_never_true(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [{"id": "a", "displayName": "A", "text": "A body",
                         "advancedConditions": [{"type": "lore", "entryId": "missing"}]}],
            "categories": [], "settings": {},
        }
        p = ef._decode_lorebook(lb, name="T")
        adv = p["brains"][0]["advanced"]
        assert adv == {"type": "not", "child": {"type": "true"}}

    def test_equation_currentstep_maps_to_message_count(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [{"id": "a", "displayName": "A", "text": "x",
                         "advancedConditions": [{
                             "type": "equation",
                             "terms": [{"value": "currentStep"}],
                             "comparison": ">=",
                             "target": 3,
                         }]}],
            "categories": [], "settings": {},
        }
        p = ef._decode_lorebook(lb, name="T")
        adv = p["brains"][0]["advanced"]
        assert adv["type"] == "numeric_compare"
        assert adv["lhs"] == {"kind": "variable", "variable": "message_count"}
        assert adv["op"] == ">="
        assert adv["rhs"] == {"kind": "literal", "literal": 3.0}

    def test_unrepresentable_variable_collapses_to_cond_true(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [{"id": "a", "displayName": "A", "text": "x",
                         "advancedConditions": [{
                             "type": "equation",
                             "terms": [{"value": "paragraphCount"}],
                             "comparison": ">", "target": 1,
                         }]}],
            "categories": [], "settings": {},
        }
        p = ef._decode_lorebook(lb, name="T")
        assert p["brains"][0]["advanced"] == {"type": "true"}

    def test_category_disabled_propagates(self):
        lb = {
            "lorebookVersion": 5,
            "entries": [{"id": "a", "displayName": "A", "text": "x", "category": "cat1"}],
            "categories": [{"id": "cat1", "enabled": False}],
            "settings": {},
        }
        p = ef._decode_lorebook(lb, name="T")
        assert p["brains"][0].get("disabled") is True


# ---------------------------------------------------------------------------
# PNG dispatch (chunk preference order)
# ---------------------------------------------------------------------------


class TestPngDispatch:
    def test_aertavern_data_preferred(self):
        native_payload = {"kind": "brain_library", "name": "Native", "brains": []}
        png = _png_with_text({
            "aertavern_data": _b64_json(native_payload),
            "chara": _b64_json({"spec": "chara_card_v2", "spec_version": "2.0",
                                  "data": {"name": "Foreign", "description": "",
                                            "personality": "", "scenario": "",
                                            "first_mes": "", "mes_example": "",
                                            "creator_notes": "", "system_prompt": "",
                                            "post_history_instructions": "",
                                            "alternate_greetings": [], "tags": [],
                                            "extensions": {}}}),
        })
        p = ef.parse_external(png, "x.png")
        assert p["kind"] == "brain_library"
        assert p["payload"]["name"] == "Native"

    def test_naidata_preferred_over_chara(self):
        png = _png_with_text({
            "chara": _b64_json({"spec": "chara_card_v2", "spec_version": "2.0",
                                  "data": {"name": "Foreign", "description": "",
                                            "personality": "", "scenario": "",
                                            "first_mes": "", "mes_example": "",
                                            "creator_notes": "", "system_prompt": "",
                                            "post_history_instructions": "",
                                            "alternate_greetings": [], "tags": [],
                                            "extensions": {}}}),
            "naidata": _b64_json({"lorebookVersion": 5, "entries": [], "categories": [], "settings": {}}),
        })
        p = ef.parse_external(png, "x.png")
        assert p["kind"] == "brain_library"

    def test_no_chunk_raises(self):
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "PNG")
        with pytest.raises(ValueError, match="no embedded card data"):
            ef.parse_external(buf.getvalue(), "x.png")

    def test_malformed_chunk_raises(self):
        png = _png_with_text({"aertavern_data": "not-valid-base64-or-json"})
        with pytest.raises(ValueError):
            ef.parse_external(png, "x.png")


# ---------------------------------------------------------------------------
# extract_brains
# ---------------------------------------------------------------------------


class TestExtractBrains:
    def test_st_card_returns_character_book_entries(self):
        card = _make_st_v2(
            character_book={"recursive_scanning": False, "entries": [
                {"comment": "E", "content": "C", "key": ["x"]},
            ]},
        )
        raw = json.dumps(card).encode("utf-8")
        result = ef.extract_brains(raw, "alice.json")
        assert len(result["brains"]) == 1
        assert result["brains"][0]["name"] == "E"
        assert result["source_name"] == "Alice"

    def test_native_contact_json_returns_brains_array(self):
        native = {
            "name": "X",
            "brains": [{"id": "1", "name": "B", "content": "c"}],
        }
        raw = json.dumps(native).encode("utf-8")
        result = ef.extract_brains(raw, "x.json")
        assert len(result["brains"]) == 1
        assert result["brains"][0]["name"] == "B"


# ---------------------------------------------------------------------------
# Cross-contamination guard: post_history_instructions must NOT end up in
# the brains list. The decoder routes the two fields independently
# (``character_book.entries`` → ``brains``; ``post_history_instructions``
# → ``reminderBrain``); this test verifies that no brain is constructed
# from reminder source data even when both are present.
# ---------------------------------------------------------------------------


class TestImportBrainContentSanitization:
    """Content imported from a pasted prompt may include leading framing
    ("----" separator + brain-name header) that the AER renderer would
    add again on its own. ``_import_brains`` strips both."""

    def _import(self, name: str, content: str):
        from server.importers import _import_brains

        return _import_brains([{"name": name, "content": content}])

    def test_strips_separator_only(self):
        b = self._import("Foo", "----\nthe real content")
        assert b[0].content == "the real content"

    def test_strips_separator_then_name_header(self):
        b = self._import("Foo", "----\nFoo\nthe real content")
        assert b[0].content == "the real content"

    def test_name_header_without_separator_is_also_stripped(self):
        b = self._import("Bar", "Bar\nthe real content")
        assert b[0].content == "the real content"

    def test_separator_with_trailing_whitespace(self):
        b = self._import("Foo", "----   \nFoo\nthe real content")
        assert b[0].content == "the real content"

    def test_unrelated_content_untouched(self):
        b = self._import("Foo", "Just the real content here.")
        assert b[0].content == "Just the real content here."

    def test_empty_after_strip_drops_brain(self):
        b = self._import("Foo", "----\nFoo\n")
        assert b == []


class TestReminderStaysOutOfBrains:
    def test_st_card_with_both_keeps_them_separate(self):
        card = _make_st_v2(
            post_history_instructions="Be terse.",
            character_book={"recursive_scanning": False, "entries": [
                {"comment": "Coffee", "content": "About coffee.", "key": ["coffee"]},
            ]},
        )
        p = ef._decode_st_card_v2(card)
        # Brains: one, from the character_book.
        assert len(p["brains"]) == 1
        assert p["brains"][0]["name"] == "Coffee"
        # No brain carries the reminder content.
        assert all("Be terse" not in b.get("content", "") for b in p["brains"])
        assert all(b.get("name") != "Notes" for b in p["brains"])
        # Reminder is on its own key.
        assert p["reminderBrain"]["content"] == "Be terse."

    def test_st_card_with_only_post_history_has_no_brains(self):
        card = _make_st_v2(post_history_instructions="Stay terse.")
        p = ef._decode_st_card_v2(card)
        assert p["brains"] == []
        assert p["reminderBrain"]["content"] == "Stay terse."
