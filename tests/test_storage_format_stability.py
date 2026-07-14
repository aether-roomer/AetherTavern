"""Format-stability checks for the msgspec storage layer.

These tests pin down the round-trip semantics that aren't covered by the
domain-level tests: write→read equality, write-twice byte determinism,
the ``tuple[float, float]`` ↔ list quirk on ``Scenario.background_focal``,
and the recursive ``BrainCondition`` discriminated union surviving the
``model_dump(mode="json")`` → ``msgspec.yaml.encode`` → ``msgspec.yaml.decode``
→ ``model_validate`` round-trip.
"""
from __future__ import annotations

from server import storage
from server.models import (
    Brain,
    BrainKey,
    CondAnd,
    CondKeyword,
    CondNot,
    CondNumericCompare,
    CondOr,
    CondRelationship,
    CondTrue,
    Contact,
    Intimacy,
    NumericValue,
    Scenario,
)


def test_atomic_write_yaml_is_byte_deterministic(tmp_storage):
    """msgspec.yaml encoding must be deterministic: encoding the same
    model twice produces byte-identical YAML. Without this, every
    load+save cycle would generate spurious diffs in ``data/``. We
    call ``atomic_write_yaml`` directly to bypass the
    ``updated_at = now_seconds()`` bump that ``save_contact`` applies."""
    contact = Contact(name="Determinism", persona="hello world 👋",
                      tags="alpha,beta", appearance="A line.\nAnother line.")
    storage.save_contact(contact, bump_version=False)
    path = storage.contact_dir(contact.id) / "info.yaml"
    # Pin the timestamp so subsequent calls produce identical bytes.
    contact.updated_at = 1700000000.0
    storage.atomic_write_yaml(path, contact)
    bytes1 = path.read_bytes()
    storage.atomic_write_yaml(path, contact)
    bytes2 = path.read_bytes()
    assert bytes1 == bytes2


def test_scenario_background_focal_tuple_round_trip(tmp_storage):
    """Pydantic emits ``tuple[float, float]`` as a JSON list; msgspec
    round-trips the list; Pydantic re-validates to a tuple. Verifying
    so a Scenario with a manual crop focal point survives a save+load."""
    s = Scenario(name="focal-test", background_focal=(0.25, 0.75))
    storage.save_scenario(s, bump_version=False)
    loaded = storage.load_yaml(
        storage.scenario_dir(s.id) / "info.yaml", Scenario,
    )
    assert loaded is not None
    assert loaded.background_focal == (0.25, 0.75)
    # And it's a tuple, not a list — Pydantic re-validates back to tuple.
    assert isinstance(loaded.background_focal, tuple)


def test_brain_condition_discriminated_union_round_trips(tmp_storage):
    """The 13-variant BrainCondition union goes through
    ``model_dump(mode="json")`` (which serialises the ``type`` discriminator)
    and ``model_validate`` (which dispatches back to the right variant).
    Build a nested tree and check it survives an end-to-end save+reload."""
    tree = CondAnd(children=[
        CondKeyword(keys=[BrainKey(pattern="alpha"), BrainKey(pattern="beta")]),
        CondOr(children=[
            CondNot(child=CondTrue()),
            CondRelationship(relationship=Intimacy.CLOSE),
            CondNumericCompare(
                lhs=NumericValue(kind="variable", variable="message_count"),
                op=">=",
                rhs=NumericValue(kind="literal", literal=5.0),
            ),
        ]),
    ])
    brain = Brain(name="condition-test", content="if matched, fires",
                  advanced=tree)
    contact = Contact(name="HasBrains", brains=[brain])
    storage.save_contact(contact, bump_version=False)

    loaded = storage.get_contact(contact.id)
    assert loaded is not None
    assert len(loaded.brains) == 1
    out = loaded.brains[0].advanced
    assert isinstance(out, CondAnd)
    assert isinstance(out.children[0], CondKeyword)
    assert [k.pattern for k in out.children[0].keys] == ["alpha", "beta"]
    inner = out.children[1]
    assert isinstance(inner, CondOr)
    assert isinstance(inner.children[0], CondNot)
    assert isinstance(inner.children[0].child, CondTrue)
    assert isinstance(inner.children[1], CondRelationship)
    assert inner.children[1].relationship == Intimacy.CLOSE
    assert isinstance(inner.children[2], CondNumericCompare)
    assert inner.children[2].op == ">="
    assert inner.children[2].rhs.literal == 5.0


def test_unicode_and_emoji_round_trip(tmp_storage):
    """msgspec.yaml encodes BMP unicode as raw UTF-8 and supplementary-plane
    characters (emoji) as ``\\Uxxxxxxxx`` escapes. Both decode back to
    the same Python string — the round-trip is correct even though the
    on-disk representation differs from PyYAML's emoji-as-raw-UTF-8."""
    contact = Contact(
        name="Unicode 茶",
        persona="こんにちは 🦀 héllo",
        appearance="Line 1 ❤️\nLine 2 🎉",
    )
    storage.save_contact(contact, bump_version=False)
    loaded = storage.get_contact(contact.id)
    assert loaded is not None
    assert loaded.name == "Unicode 茶"
    assert loaded.persona == "こんにちは 🦀 héllo"
    assert loaded.appearance == "Line 1 ❤️\nLine 2 🎉"
