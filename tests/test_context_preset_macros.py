"""{{global_brains}} + {{chat.*}} macro coverage."""
from __future__ import annotations

from server.aer.format import collect_unconditional_global_brains, render_brain_blocks
from server.aer.macros import apply_macros
from server.models import (
    Brain,
    BrainKey,
    BrainLibrary,
    Chat,
    Contact,
    Intimacy,
    ResponseLength,
    Scenario,
    Style,
    User,
)


def _c(**kwargs):
    return Contact(name="Alice", **kwargs)


def _u(**kwargs):
    return User(name="Bob", **kwargs)


# ---------------------------------------------------------------------------
# {{global_brains}}
# ---------------------------------------------------------------------------


def test_global_brains_empty_when_no_brains_anywhere():
    out = apply_macros("L:{{global_brains}}:R", _c(), _u())
    assert out == "L::R"


def test_global_brains_renders_contact_brain():
    c = _c(brains=[Brain(name="Lore A", content="alpha content")])
    out = apply_macros("L\n{{global_brains}}\nR", c, _u())
    assert out == "L\n----\nLore A\nalpha content\nR"


def test_global_brains_omits_conditional_brains():
    c = _c(brains=[
        Brain(name="Always", content="kept"),
        Brain(name="Conditional", content="dropped",
              keys=[BrainKey(pattern="foo")]),
    ])
    out = apply_macros("{{global_brains}}", c, _u())
    assert "kept" in out
    assert "dropped" not in out
    assert "Conditional" not in out


def test_global_brains_concatenates_multiple_unconditional_brains():
    c = _c(brains=[
        Brain(name="A", content="alpha"),
        Brain(name="B", content="beta"),
    ])
    out = apply_macros("{{global_brains}}", c, _u())
    assert out == "----\nA\nalpha\n----\nB\nbeta"


def test_global_brains_byte_identical_to_format_path():
    """The macro and the AER system-prompt path should produce byte-
    identical brain blocks."""
    c = _c(brains=[Brain(name="X", content="content")])
    u = _u()

    via_macro = apply_macros("{{global_brains}}", c, u)
    via_format = render_brain_blocks(
        collect_unconditional_global_brains(c, u, None, None),
    ).rstrip("\n")
    assert via_macro == via_format


def test_global_brains_includes_libraries():
    c = _c(brains=[Brain(name="From contact", content="contact-content")])
    lib = BrainLibrary(
        name="Lib", brains=[Brain(name="From lib", content="lib-content")],
    )
    out = apply_macros("{{global_brains}}", c, _u(), libraries=[lib])
    # Contact brain first (order: contact → user → scenario → libraries).
    assert "From contact" in out
    assert "From lib" in out
    assert out.index("From contact") < out.index("From lib")


def test_global_brains_in_if_block():
    """Bare-name auto-resolve lets ``{{if global_brains}}...`` work."""
    c_with_brain = _c(brains=[Brain(name="X", content="X")])
    c_no_brain = _c()
    text = "{{#if global_brains}}\n<LORE>\n{{global_brains}}\n</LORE>{{/if}}"
    assert apply_macros(text, c_with_brain, _u()).startswith("\n<LORE>")
    assert apply_macros(text, c_no_brain, _u()) == ""


# ---------------------------------------------------------------------------
# {{chat.*}}
# ---------------------------------------------------------------------------


def _chat(**kwargs):
    base = dict(
        contact_id="c", user_id="u",
        title="", tags="",
        intimacy=Intimacy.STRANGER, style=Style.CHAT,
        response_length=None, cjk=False,
    )
    base.update(kwargs)
    return Chat(**base)


def test_chat_title():
    assert apply_macros("[{{chat.title}}]", _c(), _u(), chat=_chat(title="Lab notes")) == "[Lab notes]"


def test_chat_tags():
    out = apply_macros("[{{chat.tags}}]", _c(), _u(), chat=_chat(tags="a, b"))
    assert out == "[a, b]"


def test_chat_intimacy_enum_value():
    out = apply_macros("[{{chat.intimacy}}]", _c(), _u(),
                       chat=_chat(intimacy=Intimacy.CLOSE))
    assert out == "[close]"


def test_chat_style_enum_value():
    out = apply_macros("[{{chat.style}}]", _c(), _u(),
                       chat=_chat(style=Style.ROLEPLAY))
    assert out == "[roleplay]"


def test_chat_response_length_set_and_none():
    out_set = apply_macros("[{{chat.response_length}}]", _c(), _u(),
                           chat=_chat(response_length=ResponseLength.MEDIUM))
    assert out_set == "[medium]"
    out_none = apply_macros("[{{chat.response_length}}]", _c(), _u(),
                            chat=_chat(response_length=None))
    assert out_none == "[]"


def test_chat_cjk_true_false_literal():
    out_t = apply_macros("[{{chat.cjk}}]", _c(), _u(), chat=_chat(cjk=True))
    assert out_t == "[true]"
    out_f = apply_macros("[{{chat.cjk}}]", _c(), _u(), chat=_chat(cjk=False))
    assert out_f == "[false]"


def test_chat_namespace_all_empty_without_chat_in_scope():
    text = "[{{chat.title}}][{{chat.tags}}][{{chat.intimacy}}][{{chat.style}}][{{chat.response_length}}][{{chat.cjk}}]"
    out = apply_macros(text, _c(), _u())
    assert out == "[][][][][][]"


def test_chat_cjk_in_if_branch():
    # ``{{if chat.cjk}}`` fires on true, ``{{if !chat.cjk}}`` on false.
    text = "{{if chat.cjk}}CJK{{else}}LATIN{{/if}}"
    assert apply_macros(text, _c(), _u(), chat=_chat(cjk=True)) == "CJK"
    assert apply_macros(text, _c(), _u(), chat=_chat(cjk=False)) == "LATIN"
