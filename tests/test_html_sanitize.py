"""HTML sanitization in the Generic-mode markdown renderer.

When ``Settings.sanitize_generic_html`` is True (default), raw HTML the
model emits is escaped before ``marked`` parses, so no raw
``<script>`` / ``<a href>`` / ``<img src>`` outside markdown-generated
tags reaches the DOM. When False, the user has opted in to permissive
rendering and HTML passes through.

AER's renderer always escapes by construction — flipping the Generic
setting must not affect AER bubbles.

These tests run in the same module as the other UI tests and reuse the
mock provider, ``_configure_generic_provider``, etc.
"""
from __future__ import annotations

import pytest


pytest.importorskip("playwright")
from playwright.sync_api import Page, expect

# Reuse the module fixtures.
from tests.test_ui_generic import (  # noqa: E402
    _configure_generic_provider,
    _create_chat,
    _create_contact,
    _get_json,
    _mock_state,
    _open_chat_in_browser,
    _put_json,
    _trigger_generic_generation,
    _user_id,
    base_url,
    browser,  # noqa: F401
    clean_state,  # noqa: F401
    mock_provider,
    page,  # noqa: F401
    _server,  # noqa: F401
)


def test_generic_sanitize_on_escapes_raw_script(
    page: Page, clean_state, mock_provider,
):
    """With sanitize on (default), a model emitting raw ``<script>``
    text never lands as an executing element — it shows as escaped
    literal text in the bubble."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("ScriptSanitize")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "Before <script>alert(1)</script> after.",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # No actual <script> element reached the DOM.
    assert bubble.locator("script").count() == 0
    # The script text is visible as escaped literal — assert via the
    # rendered text contains the angle-bracket characters.
    text = bubble.inner_text()
    assert "<script>" in text
    assert "alert(1)" in text


def test_generic_sanitize_on_escapes_raw_anchor(
    page: Page, clean_state, mock_provider,
):
    """Raw ``<a href>`` from the model gets escaped; only markdown-
    generated anchors (``[label](url)``) render as actual links."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("AnchorSanitize")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        'See <a href="https://evil.example">click</a> not real.',
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # No real anchor element.
    assert bubble.locator('a[href="https://evil.example"]').count() == 0


def test_generic_sanitize_off_lets_raw_html_through(
    page: Page, clean_state, mock_provider,
):
    """When the user has opted in by disabling sanitize, raw HTML in
    the model's output becomes real DOM elements. Same risk profile
    as opening a random web page from the model."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("UnsafeMode")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        'Some <span class="injected-marker">raw</span> html.',
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # Real <span> reaches the DOM when sanitize is off.
    assert bubble.locator("span.injected-marker").count() == 1

    # Cleanup: flip sanitize back on so other tests in the module
    # observe the default state.
    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_html_block_bypasses_markdown_when_sanitize_off(
    page: Page, clean_state, mock_provider,
):
    """With sanitize off, an ``<|RAWHTML|>…<|/RAWHTML|>`` region is spliced into the
    bubble verbatim: markdown-significant characters inside it (``**``,
    backticks, ``_``) stay literal, while markdown OUTSIDE the block still
    renders. The ``<|RAWHTML|>`` / ``<|/RAWHTML|>`` delimiters themselves are dropped.
    """
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("RawHtmlBlock")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "**outside**\n"
        "<|RAWHTML|>\n"
        '<div class="raw-block">**inside** and `code` and _under_</div>\n'
        "<|/RAWHTML|>\n"
        "tail",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # The raw <div> reached the DOM as a real element.
    raw = bubble.locator("div.raw-block")
    assert raw.count() == 1
    # Markdown markers inside the block produced no emphasis/code elements.
    assert raw.locator("strong").count() == 0
    assert raw.locator("em").count() == 0
    assert raw.locator("code").count() == 0
    inner = raw.inner_text()
    assert "**inside**" in inner
    assert "`code`" in inner
    assert "_under_" in inner
    # Markdown OUTSIDE the block still renders: the only <strong> in the
    # bubble is the surrounding **outside**, proving the splice boundary.
    strongs = bubble.locator("strong")
    assert strongs.count() == 1
    assert strongs.first.inner_text() == "outside"
    # The <|RAWHTML|> delimiters are not emitted as visible text.
    visible = bubble.inner_text()
    assert "<|RAWHTML|>" not in visible
    assert "<|/RAWHTML|>" not in visible

    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_html_block_inert_when_sanitize_on(
    page: Page, clean_state, mock_provider,
):
    """The ``<|RAWHTML|>…<|/RAWHTML|>`` escape hatch is gated behind sanitize-off.
    With sanitize on (default) the tags are escaped and shown as literal
    text — no real element materialises and markdown isn't bypassed."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": True})
    contact_id = _create_contact("HtmlBlockSafe")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        '<|RAWHTML|><span class="raw-block">x</span><|/RAWHTML|>',
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # No real element — the markup stays inert literal text.
    assert bubble.locator("span.raw-block").count() == 0
    visible = bubble.inner_text()
    assert "<|RAWHTML|>" in visible
    assert '<span class="raw-block">' in visible


def test_generic_unterminated_html_block_passes_through_when_sanitize_off(
    page: Page, clean_state, mock_provider,
):
    """An ``<|RAWHTML|>`` opener with no closing ``<|/RAWHTML|>`` — the mid-stream
    state where the close tag hasn't arrived, or a truncated generation —
    still passes the remainder through raw rather than flashing it through
    the markdown parser."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("DanglingHtml")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "lead\n"
        "<|RAWHTML|>\n"
        '<div class="dangling">**still raw**</div>',
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    raw = bubble.locator("div.dangling")
    assert raw.count() == 1
    assert raw.locator("strong").count() == 0
    assert "**still raw**" in raw.inner_text()
    assert "<|RAWHTML|>" not in bubble.inner_text()

    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_html_block_not_carved_inside_code_fence_when_sanitize_off(
    page: Page, clean_state, mock_provider,
):
    """A model showing HTML *source* in a fenced code block keeps its
    ``<|RAWHTML|>`` visible as literal code — the escape-hatch carve must not
    fire inside markdown code, even with sanitize off."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("FencedHtml")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "Here is HTML:\n\n"
        "```html\n"
        '<|RAWHTML|><div class="should-not-exist">hi</div><|/RAWHTML|>\n'
        "```\n",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # Rendered as a code block showing the literal tag text.
    code = bubble.locator("pre code").first
    code_text = code.inner_text()
    assert "<|RAWHTML|>" in code_text
    assert '<div class="should-not-exist">' in code_text
    # The carve did NOT fire — no real element materialised from the fence.
    assert bubble.locator("div.should-not-exist").count() == 0

    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_html_not_carved_inside_inline_code_when_sanitize_off(
    page: Page, clean_state, mock_provider,
):
    """``<|RAWHTML|>`` inside an inline code span stays literal code — and the
    dangling-opener branch must not swallow the trailing prose."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("InlineHtml")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "Use `<|RAWHTML|>` to start a document.",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    code = bubble.locator("code").first
    assert "<|RAWHTML|>" in code.inner_text()
    # The trailing prose survived (no dangling-to-end raw swallow).
    assert "to start a document." in bubble.inner_text()

    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_rawhtml_region_survives_inner_html_close_tag(
    page: Page, clean_state, mock_provider,
):
    """A script inside the passthrough that builds a document containing a
    literal ``</html>`` in a string no longer closes the region early — the
    distinctive ``<|/RAWHTML|>`` sentinel can't collide with the markup the
    region holds, so the whole script reaches the DOM intact and runs. (With
    the old bare ``</html>`` delimiter the region closed at the inner tag and
    the tail fell back to markdown — escaping the closing quote to ``&#39;``
    and wrapping it in ``<p>`` — which corrupted the script into invalid JS.)
    """
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("InnerCloseTag")
    chat_id = _create_chat(contact_id, _user_id())

    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    _mock_state.deltas = [
        "<|RAWHTML|>\n"
        '<div class="probe-target">pending</div>\n'
        "<script>\n"
        # A full document built in a string — holds both <html> and </html>.
        "var docHtml = '<html><body><div>x</div></body></html>';\n"
        "document.querySelector('.probe-target').textContent = 'ran:' + docHtml.length;\n"
        "</script>\n"
        "<|/RAWHTML|>",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # Ran intact: the string wasn't truncated at the inner </html> (that would
    # leave an unterminated literal → a skipped malformed script → "pending").
    expect(bubble.locator(".probe-target")).to_contain_text("ran:")
    page.wait_for_timeout(100)
    assert not any("SyntaxError" in e for e in page_errors), page_errors

    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_markdown_escape_then_marked_preserves_code_content(
    page: Page, clean_state, mock_provider,
):
    """The escape-then-marked gotcha: marked applies its own HTML escape
    to code-span / fenced-code content. Our renderer override entity-
    decodes our five entities inside ``token.text`` before marked
    re-escapes, so a fenced block containing ``<div>`` renders as the
    visible string ``<div>`` (not ``&lt;div&gt;``)."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("CodeBlock")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "```\n<div>hello</div>\n```",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    code = bubble.locator("pre code").first
    text = code.inner_text()
    # The user should see the literal HTML tag text inside the code
    # block — not the doubly-escaped form.
    assert "<div>hello</div>" in text


def test_html_comment_renders_as_invisible_in_prose(
    page: Page, clean_state, mock_provider,
):
    """``<!-- ... -->`` in prose (not code) gets restored to a real HTML
    comment after escape-then-marked — so it's invisible in the rendered
    bubble. The model can use comments for hidden metadata."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("Commenter")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "Before <!-- hidden meta --> after.",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    text = bubble.inner_text()
    # Visible text has no comment markers.
    assert "<!--" not in text
    assert "-->" not in text
    assert "hidden meta" not in text
    # Surrounding prose still rendered.
    assert "Before" in text and "after" in text


def test_html_comment_inside_fenced_code_block_renders_as_literal(
    page: Page, clean_state, mock_provider,
):
    """``<!-- ... -->`` inside a fenced code block must render as literal
    text — even though the prose-level exception turns it into a real
    comment, the comment marker inside a code block is content that the
    user expects to see. The ``<pre>`` skip in the comment restorer
    keeps the code-block content as escaped entities, then the browser
    paints them as literal characters."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("CodeCommenter")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "```html\n<!-- comment in code -->\n```",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    code = bubble.locator("pre code").first
    text = code.inner_text()
    # Visible inside the code block.
    assert "<!--" in text
    assert "comment in code" in text
    assert "-->" in text


def test_aer_renderer_keeps_comments_inside_fenced_code_visible(
    page: Page, clean_state, mock_provider,
):
    """AER fenced code blocks render with strict escape — a ``<!--`` inside
    them stays visible. The comment-restore exception only fires inside
    ``renderInlineSegment`` (prose), not inside ``escapeHtml(fence)``.

    Drive it through a Manual user message (origin='manual' → AER renderer)
    rather than a Generic gen since AER's bubble parser would otherwise
    need a properly-formatted AER prompt.
    """
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("AerFence")
    chat_id = _create_chat(contact_id, _user_id())
    # Post a user message via the API with a fenced block containing a comment.
    import json
    from urllib.request import Request
    from urllib.request import urlopen
    body = json.dumps({
        "sender": "user",
        "body": [{
            "text": "```\n<!-- aer fence -->\n```",
            "emotion": "neutral",
        }],
    }).encode()
    req = Request(
        f"{base_url()}/api/chats/{chat_id}/messages", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    urlopen(req, timeout=3).read()

    _open_chat_in_browser(page, chat_id)
    page.wait_for_selector(".msg.user .bubble", timeout=5000)
    bubble = page.locator(".msg.user .bubble").first
    code = bubble.locator("pre code").first
    text = code.inner_text()
    assert "<!--" in text
    assert "aer fence" in text


def test_html_comment_inside_inline_code_renders_as_literal(
    page: Page, clean_state, mock_provider,
):
    """``<!-- ... -->`` inside an inline ``<code>`` span (backticks) must
    also render as literal text — the prose-level exception must NOT
    consume entities inside inline code, only inside top-level prose."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("InlineCodeCommenter")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "Look at `<!-- inline -->` here.",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    code = bubble.locator("code").first
    text = code.inner_text()
    # Inside the inline code span, the comment markers should be visible.
    assert "<!--" in text
    assert "inline" in text
    assert "-->" in text
    # And the inline code should NOT have been swallowed as an actual
    # HTML comment (which would have removed it from the DOM entirely).
    assert bubble.locator("code").count() >= 1


def test_sanitize_toggle_persists_from_settings_ui(
    page: Page, clean_state, mock_provider,
):
    """Regression: toggling "Sanitize HTML" in the settings UI must reach
    the server.

    The Generic-tab save path (``saveInferenceDraft``) built its PUT
    payload from an explicit field list that omitted
    ``sanitize_generic_html`` — so the checkbox flipped in the DOM but the
    debounced save never carried the value, and the backend preserved the
    old one. The toggle silently reverted on reload.
    """
    # The toggle only renders inside the Generic inference tab, so put the
    # app in Generic mode first.
    _configure_generic_provider(mock_provider)
    assert _get_json("/api/settings")["sanitize_generic_html"] is True

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.wait_for_function(
        "async () => {"
        "  const state = await import('/static/state.js');"
        "  return state.state.settings !== null;"
        "}",
        timeout=10000,
    )
    page.evaluate(
        """
        async () => {
          const state = await import('/static/state.js');
          state.setState({ activeTab: 'settings' });
        }
        """,
    )

    checkbox = page.locator("#sanitize-generic-html")
    checkbox.wait_for(state="visible", timeout=5000)
    expect(checkbox).to_be_checked()

    # Uncheck → fires the debounced save (600ms) → saveInferenceDraft PUT.
    checkbox.uncheck()

    # Poll the server until the change lands (debounce + round-trip).
    import time
    deadline = time.time() + 5
    persisted = True
    while time.time() < deadline:
        persisted = _get_json("/api/settings")["sanitize_generic_html"]
        if persisted is False:
            break
        time.sleep(0.2)
    assert persisted is False, "sanitize toggle did not persist to the server"

    # Restore the default so sibling module tests observe sanitize on.
    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_script_executes_when_sanitize_off(
    page: Page, clean_state, mock_provider,
):
    """With sanitize off, a ``<script>`` the model emits runs once the bubble
    is rendered. ``innerHTML`` leaves parser-created scripts inert, so the
    render path swaps each for an executable clone (render.js#activateScripts);
    it fires when the row is connected to the document."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("ScriptRuns")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "<|RAWHTML|>\n"
        '<div class="probe-target">pending</div>\n'
        "<script>window.__probeRan = true; "
        "document.querySelector('.probe-target').textContent = 'executed';"
        "</script>\n"
        "<|/RAWHTML|>",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # The script ran: it rewrote its sibling node and set a window flag.
    # ``expect`` auto-retries, covering the post-``done`` rebuild that
    # materialises the committed (script-activating) bubble.
    expect(bubble.locator(".probe-target")).to_have_text("executed")
    assert page.evaluate("window.__probeRan") is True

    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_script_inert_when_sanitize_on(
    page: Page, clean_state, mock_provider,
):
    """With sanitize on (default) the same ``<script>`` is escaped to literal
    text — no element materialises and nothing executes."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("ScriptInert")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        '<div class="probe-target">pending</div>'
        "<script>window.__inertProbeRan = true;</script>",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    assert bubble.locator("script").count() == 0
    assert bubble.locator(".probe-target").count() == 0
    assert page.evaluate("window.__inertProbeRan") is None
    assert "<script>" in bubble.inner_text()


def test_generic_script_reruns_on_rerender_when_sanitize_off(
    page: Page, clean_state, mock_provider,
):
    """Every-render semantics: a bubble is rebuilt from its saved text on each
    (re)mount, so its scripts re-run. A page reload re-renders the persisted
    message and fires the script again."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("ScriptRerun")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "<|RAWHTML|>\n"
        '<div class="probe-target">pending</div>\n'
        "<script>"
        "sessionStorage.setItem('probeRuns', "
        "String(parseInt(sessionStorage.getItem('probeRuns') || '0', 10) + 1));"
        "document.querySelector('.probe-target').textContent = 'executed';"
        "</script>\n"
        "<|/RAWHTML|>",
    ]
    _open_chat_in_browser(page, chat_id)
    page.evaluate("sessionStorage.removeItem('probeRuns')")
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    expect(bubble.locator(".probe-target")).to_have_text("executed")

    def _runs():
        return int(page.evaluate("sessionStorage.getItem('probeRuns') || '0'"))

    runs_after_gen = _runs()
    assert runs_after_gen >= 1

    # Reload re-renders the saved message from disk — the static render path
    # activates the script again.
    page.reload()
    bubble = page.locator(".bubble.generic").first
    expect(bubble.locator(".probe-target")).to_have_text("executed")
    assert _runs() > runs_after_gen

    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_malformed_script_skipped_without_uncaught_error(
    page: Page, clean_state, mock_provider,
):
    """A malformed inline script (here an unterminated string — also what a
    ``</script>`` split inside a string produces) is compile-checked and
    skipped with a contained console warning, rather than throwing an uncaught
    SyntaxError that the browser pins on the render loop's append. A valid
    sibling script still runs."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("MalformedScript")
    chat_id = _create_chat(contact_id, _user_id())

    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on(
        "console",
        lambda m: console_errors.append(m.text) if m.type == "error" else None,
    )

    _mock_state.deltas = [
        "<|RAWHTML|>\n"
        '<div class="probe-target">pending</div>\n'
        '<script>const s = "oops;</script>\n'
        "<script>document.querySelector('.probe-target').textContent = 'ran';"
        "</script>\n"
        "<|/RAWHTML|>",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # The valid sibling ran despite the malformed script preceding it.
    expect(bubble.locator(".probe-target")).to_have_text("ran")
    page.wait_for_timeout(150)  # let console / error events flush
    # No uncaught SyntaxError leaked, and the skip path logged the source.
    assert not any("SyntaxError" in e for e in page_errors), page_errors
    assert any("malformed" in e for e in console_errors), console_errors

    _put_json("/api/settings", {"sanitize_generic_html": True})


def test_generic_script_runtime_error_reported_not_uncaught(
    page: Page, clean_state, mock_provider,
):
    """A script that parses but throws at runtime is wrapped so the error is
    logged to the console with a clear prefix and caught — it does not surface
    as an uncaught error pinned on the render loop. The throw still aborts the
    rest of that script (standard JS): work before it lands, work after it
    doesn't."""
    _configure_generic_provider(mock_provider)
    _put_json("/api/settings", {"sanitize_generic_html": False})
    contact_id = _create_contact("RuntimeError")
    chat_id = _create_chat(contact_id, _user_id())

    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on(
        "console",
        lambda m: console_errors.append(m.text) if m.type == "error" else None,
    )

    _mock_state.deltas = [
        "<|RAWHTML|>\n"
        '<div class="probe-target">pending</div>\n'
        "<script>\n"
        "document.querySelector('.probe-target').textContent = 'before';\n"
        "definitelyNotDefined();\n"  # ReferenceError at runtime
        "document.querySelector('.probe-target').textContent = 'after';\n"
        "</script>\n"
        "<|/RAWHTML|>",
    ]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # Ran up to the throw ('before'), which aborted the rest ('after' never
    # set) — and the throw was caught and reported, not left uncaught.
    expect(bubble.locator(".probe-target")).to_have_text("before")
    page.wait_for_timeout(150)
    assert any("threw at runtime" in e for e in console_errors), console_errors
    assert not any("ReferenceError" in e for e in page_errors), page_errors

    _put_json("/api/settings", {"sanitize_generic_html": True})
