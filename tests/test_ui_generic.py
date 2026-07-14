"""Playwright UI tests for Generic-mode + popover + reasoning.

Spins up a tiny mock provider HTTP server in a background thread per
module — the Generic provider client points its
``openai_compatible.custom_providers[0].base_url`` at it, so every UI
flow that runs a generation exercises the full route + builder + image
rewriter + persist + wire-rewrite pipeline without hitting any real
upstream.

Test classes share the ``_server`` + ``browser`` module fixtures from
``test_ui.py``; we just import the helpers + ``base_url`` and inject
our mock provider's response per test via a module-global.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pytest


pytest.importorskip("playwright")
from playwright.sync_api import Page, expect

# Reuse the module-scoped server / browser / page fixtures + base_url helper.
from tests.test_ui import (  # noqa: E402  (after pytest.importorskip)
    base_url,
    browser,  # noqa: F401  re-export so pytest discovers it here too
    page,     # noqa: F401
    _server,  # noqa: F401
    clean_state,  # noqa: F401
)


# ---------------------------------------------------------------------------
# Mock provider HTTP server
# ---------------------------------------------------------------------------


class _MockProviderState:
    """Per-test mutable response config.

    Default emits a single content delta then ``[DONE]``. Tests assign
    new values before triggering a generation.
    """
    deltas: list[str] = ["Hello from mock."]
    reasoning_deltas: list[str] = []
    finish_reason: str = "stop"
    prompt_tokens: int = 42
    completion_tokens: int = 5
    # Pause (seconds) inserted between each delta. Lets tests verify
    # typing-indicator hiding on first delta vs. last delta.
    delta_delay: float = 0.0


_mock_state = _MockProviderState()


def _reset_mock_state() -> None:
    _mock_state.deltas = ["Hello from mock."]
    _mock_state.reasoning_deltas = []
    _mock_state.finish_reason = "stop"
    _mock_state.prompt_tokens = 42
    _mock_state.completion_tokens = 5
    _mock_state.delta_delay = 0.0


def _sse_chunk(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


class _MockProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):  # silence access log
        return

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/models":
            body = json.dumps({
                "data": [
                    {"id": "mock-model", "object": "model"},
                ],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        _body = self.rfile.read(length)  # consumed but ignored
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for r in _mock_state.reasoning_deltas:
                chunk = {
                    "id": "mock", "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {"reasoning": r},
                    }],
                }
                self.wfile.write(_sse_chunk(chunk))
                self.wfile.flush()
                if _mock_state.delta_delay:
                    time.sleep(_mock_state.delta_delay)
            for d in _mock_state.deltas:
                chunk = {
                    "id": "mock", "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": d},
                    }],
                }
                self.wfile.write(_sse_chunk(chunk))
                self.wfile.flush()
                if _mock_state.delta_delay:
                    time.sleep(_mock_state.delta_delay)
            # Final chunk with finish_reason + usage.
            final = {
                "id": "mock", "object": "chat.completion.chunk",
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": _mock_state.finish_reason,
                }],
                "usage": {
                    "prompt_tokens": _mock_state.prompt_tokens,
                    "completion_tokens": _mock_state.completion_tokens,
                    "total_tokens": _mock_state.prompt_tokens + _mock_state.completion_tokens,
                },
            }
            self.wfile.write(_sse_chunk(final))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected mid-stream — normal during cancellation.
            return


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture(scope="module")
def mock_provider():
    """Spawn the mock provider HTTP server for the duration of the module.

    Yields ``"http://127.0.0.1:{port}"`` for the test to point provider
    config at.
    """
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _MockProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _reset_mock_each_test():
    _reset_mock_state()
    yield
    _reset_mock_state()


# ---------------------------------------------------------------------------
# JSON-API helpers
# ---------------------------------------------------------------------------


def _post_json(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = Request(
        f"{base_url()}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    return json.loads(urlopen(req, timeout=5).read())


def _put_json(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = Request(
        f"{base_url()}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="PUT",
    )
    return json.loads(urlopen(req, timeout=5).read())


def _get_json(path: str) -> dict:
    return json.loads(urlopen(f"{base_url()}{path}", timeout=5).read())


def _configure_generic_provider(mock_url: str) -> str:
    """Wire the openai_compatible custom provider to the mock server.

    Returns the active custom-provider entry id.

    Omits ``id`` so the server's ``default_factory=new_id`` mints a fresh
    uuid. The second PUT (to pin ``active_id``) echoes the GET response
    back, with ``api_token`` swapped to the sentinel ``"__present__"``
    — the GET only carries an indicator, and the model's write side
    treats an empty/absent token as "clear" rather than "preserve".
    """
    presets = _get_json("/api/context-presets")
    preset_id = presets[0]["id"] if presets else None
    entry = {
        # NO id field — let the server fill it.
        "label": "MockProvider",
        "base_url": mock_url,
        "api_token": "mock-token",
        "model_id": "mock-model",
        "cache_minutes": None,
        "streaming": True,
        "brain_message_role": "system",
        "context_preset_id": preset_id,
    }
    settings = _get_json("/api/settings")
    g = settings.get("generic") or {}

    _put_json("/api/settings", {
        "provider_mode": "generic",
        "generic": {
            "provider": "openai_compatible",
            "novelai": g.get("novelai") or {},
            "openrouter": g.get("openrouter") or {},
            "nanogpt": g.get("nanogpt") or {},
            "openai_compatible": {
                "custom_providers": [entry],
                "active_id": "",
            },
        },
    })

    settings = _get_json("/api/settings")
    custom = settings["generic"]["openai_compatible"]["custom_providers"]
    eid = custom[0]["id"]
    for ent in custom:
        ent["api_token"] = "__present__"
    for k in ("novelai", "openrouter", "nanogpt"):
        settings["generic"][k]["api_token"] = "__present__"
    _put_json("/api/settings", {
        "generic": {
            "provider": "openai_compatible",
            "novelai": settings["generic"]["novelai"],
            "openrouter": settings["generic"]["openrouter"],
            "nanogpt": settings["generic"]["nanogpt"],
            "openai_compatible": {
                "custom_providers": custom,
                "active_id": eid,
            },
        },
    })
    return eid


def _create_contact(name: str = "MockTester") -> str:
    return _post_json("/api/contacts", {"id": "", "name": name})["id"]


def _create_chat(contact_id: str, user_id: str, title: str = "T") -> str:
    return _post_json("/api/chats", {
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": title,
    })["id"]


def _post_user_message(chat_id: str, text: str) -> dict:
    return _post_json(f"/api/chats/{chat_id}/messages", {
        "sender": "user",
        "body": [{"text": text, "emotion": "neutral"}],
    })


def _user_id() -> str:
    """Return the first user persona's id, creating one if storage was
    wiped by ``clean_state``."""
    users = _get_json("/api/users")
    if users:
        return users[0]["id"]
    return _post_json("/api/users", {"id": "", "name": "Anon"})["id"]


def _open_chat_in_browser(page: Page, chat_id: str) -> None:
    """Navigate to the chats tab and select ``chat_id``.

    Chat rows in the list don't carry ``data-id`` attributes (the click
    handler reads from a closure), so we drive selection through the
    state-module's ``setState`` directly. The router watches
    ``activeChatId`` and lays out the detail pane in response.

    Boot races: ``page.goto`` resolves on DOMContentLoaded, but
    ``boot()`` in app.js still has in-flight fetches before its first
    ``setState`` registers subscribers. If the test calls setState too
    soon, no subscriber fires and ``#chat-messages`` never mounts.
    Wait for ``state.settings`` to land — that's part of boot's
    required-fetches batch and the last thing ``setState(patch)`` lands
    before the rail / router are wired.
    """
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
        async (cid) => {
          const state = await import('/static/state.js');
          state.setState({ activeTab: 'chats', activeChatId: cid });
        }
        """,
        chat_id,
    )
    page.wait_for_selector("#chat-messages", timeout=10000)


def _trigger_generic_generation(page: Page, chat_id: str) -> None:
    """Type a user message + click send to fire a generation against the
    mock provider. The mock responds with whatever ``_mock_state``
    currently holds."""
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("trigger")
    textarea.press("Enter")
    # Wait for the assistant message to land in the DOM. Use a generous
    # timeout because the SSE round-trip + persist + DOM refresh can
    # take a moment.
    page.wait_for_selector(".msg.contact:not(#typing-indicator) .bubble.generic", timeout=8000)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_generic_markdown_renders_code_list_link(
    page: Page, clean_state, mock_provider,
):
    """A Generic-mode reply containing a code fence, a bulleted list, and
    a link should render as proper markdown — not as the AER per-line
    highlighter's literal text."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("Markdowner")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "Here is a **list**:\n\n",
        "- alpha\n- beta\n\n",
        "And a [link](https://example.com).\n\n",
        "```python\nprint('hi')\n```\n",
    ]

    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(".bubble.generic").first
    # Code fence → <pre><code class="language-python">
    assert bubble.locator("pre code").count() >= 1
    # List → <ul><li>alpha</li>...
    assert bubble.locator("ul li").count() >= 2
    # Link
    assert bubble.locator('a[href="https://example.com"]').count() == 1


def test_generic_reasoning_tray_collapsed_then_expands(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("Reasoner")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.reasoning_deltas = ["First step. ", "Second step."]
    _mock_state.deltas = ["The answer."]

    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    tray = page.locator(".reasoning-tray").first
    expect(tray).to_be_visible()
    # Collapsed by default: arrow is ▶ and body is hidden.
    arrow = tray.locator(".reasoning-arrow")
    expect(arrow).to_have_text("▶")
    body = tray.locator(".reasoning-body")
    expect(body).to_be_hidden()
    # Click → expanded.
    tray.locator(".reasoning-header").click()
    expect(arrow).to_have_text("▼")
    expect(body).to_be_visible()
    # Reasoning content rendered (markdown sanitize forced on).
    assert "First step" in body.inner_text()
    assert "Second step" in body.inner_text()


def test_generic_typing_dots_disappear_after_first_delta(
    page: Page, clean_state, mock_provider,
):
    """The typing indicator's bouncing dots stop after the first ``delta``
    arrives — the moving tokens take over as the "still working" signal."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("Streamer")
    chat_id = _create_chat(contact_id, _user_id())

    # Long-ish stream with delay so we can observe the in-flight state.
    _mock_state.deltas = ["Hel", "lo ", "wo", "rld!"]
    _mock_state.delta_delay = 0.15  # 4 deltas × 150ms = ~600ms total

    _open_chat_in_browser(page, chat_id)
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("trigger")
    textarea.press("Enter")

    # During stream, the typing indicator's ``.typing`` dot wrapper should
    # disappear after the first delta. Wait for at least one bubble
    # element to land, then assert the typing-dots class is gone.
    page.wait_for_selector(".bubble.generic", timeout=5000)
    # Confirm the placeholder text "typing..." is no longer visible.
    # The bubble class ``.typing`` is on the placeholder we replaced.
    typing = page.locator("#typing-indicator .bubble.typing").count()
    assert typing == 0, "typing-dots placeholder should be replaced once delta arrives"
    # Wait for stream completion.
    page.wait_for_selector(".msg.contact:not(#typing-indicator) .bubble.generic", timeout=8000)


def test_generic_leading_html_comment_keeps_typing_indicator(
    page: Page, clean_state, mock_provider,
):
    """A reply that opens with an HTML comment renders to nothing, so the
    bubble must not mount yet — the typing indicator holds until real text
    arrives. Regression: the first delta used to consume the placeholder
    unconditionally, flashing an empty bubble for the length of the comment.

    The comment streams in fragments starting with a bare ``<`` to also cover
    the partial-opener case: ``<`` on its own must not count as visible
    content just because the rest of ``<!--`` hasn't arrived yet.

    Deterministic (not timing-based): whenever the streaming generic bubble
    first appears, it should already carry visible text. Under the bug it
    appears on a comment-only (or bare ``<``) buffer; under the fix it only
    appears once "Now visible." has streamed in.
    """
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("CommentLead")
    chat_id = _create_chat(contact_id, _user_id())

    # The comment arrives in pieces ("<" then the rest); visible text only
    # lands on the final delta.
    _mock_state.deltas = ["<", "!-- hidden meta -->", "Now visible."]
    _mock_state.delta_delay = 0.25

    _open_chat_in_browser(page, chat_id)
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("trigger")
    textarea.press("Enter")

    # The streaming generic bubble must never mount empty (or with a stray
    # ``<``): the moment it first exists, it should already contain the
    # visible text.
    page.wait_for_selector("#typing-indicator .bubble.generic", timeout=5000)
    text = page.locator("#typing-indicator .bubble.generic").first.inner_text()
    assert text.strip(), (
        "generic bubble mounted with no visible content — the placeholder "
        "was consumed on a leading / partial HTML comment"
    )
    assert "Now visible" in text, text

    # Drain to completion so the stream doesn't leak into the next test.
    page.wait_for_selector(
        ".msg.contact:not(#typing-indicator) .bubble.generic", timeout=8000,
    )


def test_has_visible_generic_content_predicate(page: Page, clean_state):
    """Unit-level contract for the streaming visibility predicate in
    render.js: HTML comments and bare/partial comment openers count as
    "nothing to show yet"; anything else is visible. Pins the regex that
    gates when the Generic typing indicator gives way to the bubble — its
    branches (``<`` / ``<!`` / ``<!-`` vs ``<!-x`` / mid-string ``<``) are
    easy to break and the e2e test only walks one path."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    results = page.evaluate(
        """async () => {
          const { hasVisibleGenericContent: f } = await import('/static/render.js');
          const cases = [
            ['', false],
            ['   ', false],
            ['<', false],
            ['<!', false],
            ['<!-', false],
            ['<!--', false],
            ['<!-- partial', false],
            ['<!-- done -->', false],
            ['<!-- a --> <!-- b -->', false],
            ['<!-- meta -->Now visible.', true],
            ['Now visible.', true],
            ['Hello <', true],
            ['<!-x', true],
            ['a < b', true],
            ['![cat](u)', true],
          ];
          return cases.map(([input, want]) => ({ input, want, got: f(input) }));
        }"""
    )
    bad = [r for r in results if r["got"] != r["want"]]
    assert not bad, f"predicate mismatches: {bad}"


def test_generic_image_markdown_proxies_url_in_rendered_img(
    page: Page, clean_state, mock_provider,
):
    """Image markdown the model emits gets its URL rewritten to the
    server's proxy URL before reaching the client, both during streaming
    and after persist."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("ImgEmitter")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = [
        "Look at this:\n\n![cat](https://example.com/cat.jpg)\n",
    ]

    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    img = page.locator(".bubble.generic img").first
    src = img.get_attribute("src") or ""
    assert src.startswith(f"/api/chats/{chat_id}/images/"), src
    assert src.endswith("/cat.jpg"), src
    # The remote URL must NOT appear in the rendered DOM — the wire
    # rewrite happens at delta forward AND at persist read.
    bubble_html = page.locator(".bubble.generic").first.inner_html()
    assert "https://example.com/cat.jpg" not in bubble_html


def test_existing_messages_keep_renderer_after_mode_switch(
    page: Page, clean_state, mock_provider,
):
    """Mode-switch mid-chat doesn't repaint: a Generic message stays
    markdown-rendered after flipping ``provider_mode`` back to AER."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("ModeSwitcher")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = ["**bold-only-via-markdown**"]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)
    # Verify the Generic message renders <strong> (markdown bold).
    bubble = page.locator(".bubble.generic").first
    assert bubble.locator("strong").count() == 1

    # Flip back to AER mode.
    _put_json("/api/settings", {"provider_mode": "aetherroom"})

    # Reload the page and re-open the chat; the existing Generic message
    # should STILL render as markdown (origin-driven dispatch).
    _open_chat_in_browser(page, chat_id)
    bubble2 = page.locator(".bubble.generic").first
    expect(bubble2).to_be_visible()
    assert bubble2.locator("strong").count() == 1


def test_popover_opens_on_avatar_click_for_generic_message(
    page: Page, clean_state, mock_provider,
):
    """Avatar click on a Generic-origin contact-side message opens the
    popover and shows Origin = Generic + Provider/Model populated."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("PopoverContact")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = ["A short reply."]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    # The Generic-origin bubble with no emotion + no contact avatar lands
    # in the ``bare`` layout, so the trigger is the ``(i)`` badge next to
    # the sender name. Find it via the meta-row.
    badge = page.locator(".msg.contact .meta-row .msg-info-badge").first
    expect(badge).to_be_visible()
    badge.click()

    popover = page.locator(".msg-info-popover")
    expect(popover).to_be_visible()
    # Match against the row LABELS (``<dt>`` elements) not substring
    # over the whole popover text — "Model" the label contains the
    # substring "Mode" which would false-positive a plain ``in`` check.
    labels = popover.locator("dt").all_inner_texts()
    assert "Origin" in labels
    assert "Mode" not in labels, (
        f"row label should be 'Origin', not 'Mode' (saw: {labels!r})"
    )
    text = popover.inner_text()
    assert "Generic" in text
    # Provider row label for openai_compatible is "OpenAI-compatible".
    assert "OpenAI-compatible" in text
    assert "mock-model" in text

    # AI messages get "Generation start" + "Completed" in chronological order.
    assert "Generation start" in labels
    assert "Completed" in labels
    assert labels.index("Generation start") < labels.index("Completed"), (
        f"Generation start must precede Completed (saw labels: {labels!r})"
    )
    assert "Sent" not in labels, "AI messages label as 'Completed', not 'Sent'"

    # Escape dismisses.
    page.keyboard.press("Escape")
    expect(popover).not_to_be_visible()


def test_popover_on_manual_user_message_shows_origin_manual(
    page: Page, clean_state, mock_provider,
):
    """User-typed manual messages without a user avatar show the ``(i)``
    badge in the meta-row; opening the popover shows Origin = Manual.
    Manual / user messages keep the "Sent" timestamp label (only AI
    messages relabel to "Completed")."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("PopoverManual")
    chat_id = _create_chat(contact_id, _user_id())

    _open_chat_in_browser(page, chat_id)
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("hi there")
    textarea.press("Enter")
    # Wait for the user message to land (don't trigger generation).
    page.wait_for_selector(".msg.user", timeout=5000)
    # The default Anon user persona has no avatar set, so the badge
    # lands in the meta-row.
    badge = page.locator(".msg.user .meta-row .msg-info-badge").first
    expect(badge).to_be_visible()
    badge.click()
    popover = page.locator(".msg-info-popover")
    expect(popover).to_be_visible()
    labels = popover.locator("dt").all_inner_texts()
    text = popover.inner_text()
    assert "Origin" in labels
    assert "Mode" not in labels
    assert "Manual" in text
    # Manual / user messages keep "Sent"; they don't carry
    # Generation start / Completed / Duration rows at all.
    assert "Sent" in labels
    assert "Generation start" not in labels
    assert "Completed" not in labels
    # Provider / Model / Generation preset / Context preset rows are "—".
    assert text.count("—") >= 3


def test_popover_on_legacy_aer_greeting_shows_origin_aer(
    page: Page, clean_state, mock_provider,
):
    """Greetings persist as AER-origin with no generation metadata; the
    popover labels the message Origin = AER. AER-origin messages take
    the AI-message rows (Generation start / Completed / Duration), all
    of which are "—" for a greeting."""
    contact_id = _post_json("/api/contacts", {
        "id": "", "name": "Greeter", "greeting": "Hi I'm here.",
    })["id"]
    chat_id = _create_chat(contact_id, _user_id())

    _open_chat_in_browser(page, chat_id)
    # The greeting renders as a contact-side AER bubble with the
    # contact's emotion-sprite avatar (or text fallback). Click the
    # avatar to open the popover.
    page.wait_for_selector(".msg.contact .emotion-sprite", timeout=5000)
    page.locator(".msg.contact .emotion-sprite").first.click()
    popover = page.locator(".msg-info-popover")
    expect(popover).to_be_visible()
    labels = popover.locator("dt").all_inner_texts()
    text = popover.inner_text()
    assert "Origin" in labels
    assert "AER" in text
    # Greetings have no generation telemetry — all chronology rows blank.
    assert "Generation start" in labels
    # Several "—" placeholders, plus the persisted "Completed" timestamp
    # row carries a real value (greeting creation time).
    assert text.count("—") >= 3


# ---------------------------------------------------------------------------
# Reasoning tray DOM structure: tray must live INSIDE the first bubble at
# the top, not as a sibling above it. Regresses on a structural refactor
# that's easy to undo when re-jiggering message rendering.
# ---------------------------------------------------------------------------


def test_generic_reasoning_tray_is_inside_first_bubble(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("Trayer")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.reasoning_deltas = ["Thinking aloud."]
    _mock_state.deltas = ["Answer."]

    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    # Wait until generation has fully landed (the persisted message
    # supersedes the streaming placeholder).
    page.wait_for_function(
        "document.querySelectorAll('#chat-messages > .msg.contact .bubble.generic').length > 0",
        timeout=8000,
    )
    # The reasoning tray must be a DIRECT child of the bubble — not a
    # sibling of the bubble-row. ``:scope > .reasoning-tray`` against
    # the bubble locator is the structural assertion.
    bubble = page.locator(".msg.contact .bubble.generic").first
    inside = bubble.locator(":scope > .reasoning-tray").count()
    assert inside == 1, "reasoning tray must be a direct child of the bubble"
    # The tray must not appear as a sibling of the bubble-row.
    sibling = page.locator(".msg.contact > .reasoning-tray").count()
    assert sibling == 0, (
        "reasoning tray must not appear as a sibling of the bubble-row"
    )


# ---------------------------------------------------------------------------
# Streaming-off gate: when the active provider's ``streaming`` flag is
# false, the client should buffer deltas without rendering and only
# materialise the bubble + content at ``done``.
# ---------------------------------------------------------------------------


def _set_streaming_flag(enabled: bool) -> None:
    """Toggle the active openai_compatible provider's ``streaming`` field.
    Echoes tokens via the ``__present__`` sentinel so the PUT doesn't
    wipe them."""
    settings = _get_json("/api/settings")
    g = settings["generic"]
    custom = g["openai_compatible"]["custom_providers"]
    for ent in custom:
        ent["api_token"] = "__present__"
        if ent["id"] == g["openai_compatible"]["active_id"]:
            ent["streaming"] = enabled
    for k in ("novelai", "openrouter", "nanogpt"):
        g[k]["api_token"] = "__present__"
    _put_json("/api/settings", {
        "generic": {
            "provider": g["provider"],
            "novelai": g["novelai"],
            "openrouter": g["openrouter"],
            "nanogpt": g["nanogpt"],
            "openai_compatible": {
                "custom_providers": custom,
                "active_id": g["openai_compatible"]["active_id"],
            },
        },
    })


def test_generic_streaming_off_defers_bubble_to_done(
    page: Page, clean_state, mock_provider,
):
    """With ``streaming: false`` set on the active provider, the per-delta
    rendering is gated — the bubble.generic should appear only after the
    final ``done`` event, not progressively. The typing-dots placeholder
    persists in the meantime."""
    _configure_generic_provider(mock_provider)
    _set_streaming_flag(False)
    contact_id = _create_contact("NonStreamer")
    chat_id = _create_chat(contact_id, _user_id())

    # Multi-chunk reply with delay so the in-flight state is observable.
    _mock_state.deltas = ["First ", "second ", "third."]
    _mock_state.delta_delay = 0.2  # ~600ms total

    _open_chat_in_browser(page, chat_id)
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("trigger")
    textarea.press("Enter")

    # The placeholder bubble (with ``…`` typing dots) should appear,
    # but NO ``.bubble.generic`` should exist while the stream is
    # in-flight. Sample mid-stream — wait long enough for at least one
    # delta to have been emitted server-side.
    page.wait_for_selector("#typing-indicator", timeout=3000)
    page.wait_for_timeout(300)  # past the first delta but before done
    mid_count = page.locator(".bubble.generic").count()
    assert mid_count == 0, (
        "streaming-off must NOT render the generic bubble mid-stream"
    )

    # After done the bubble + content materialise.
    page.wait_for_selector(".bubble.generic", timeout=5000)
    page.wait_for_function(
        "Array.from(document.querySelectorAll('.bubble.generic'))"
        ".some(el => el.textContent.includes('third.'))",
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# Markdown italic muting: single-asterisk emphasis in generic-mode
# bubbles inherits the AER subdued color. Easy to regress by removing
# the ``.bubble.generic em`` rule when reorganizing styles.
# ---------------------------------------------------------------------------


def test_generic_markdown_em_uses_text_mute_color(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("EmStyler")
    chat_id = _create_chat(contact_id, _user_id())
    _mock_state.deltas = ["Some *aside* text."]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    page.wait_for_selector(".bubble.generic em", timeout=5000)
    em = page.locator(".bubble.generic em").first
    # ``getComputedStyle`` resolves the var to its concrete rgb(...).
    # The same expression evaluated against ``--text-mute`` is the
    # value we expect — pin to the var to keep the test theme-agnostic.
    em_color = em.evaluate("el => getComputedStyle(el).color")
    mute_color = page.evaluate(
        "() => { "
        "  const probe = document.createElement('span');"
        "  probe.style.color = 'var(--text-mute)';"
        "  document.body.appendChild(probe);"
        "  const c = getComputedStyle(probe).color;"
        "  probe.remove();"
        "  return c;"
        "}"
    )
    assert em_color == mute_color, (
        f"<em> in generic bubble should use --text-mute "
        f"(got {em_color!r}, expected {mute_color!r})"
    )


# ---------------------------------------------------------------------------
# Keyboard reroll on a mid-chat contact message must visually anchor the
# streaming bubble at the rerolled message's slot — descendants on the
# original branch hide for the duration of the stream. Regresses when
# the virtual-visibility logic only handles the "rerolled message is the
# path tail" case and leaves a mid-chat reroll's typing indicator stranded
# at the bottom of the chat below the still-visible downstream messages.
# ---------------------------------------------------------------------------


def test_keyboard_reroll_on_mid_chat_msg_anchors_indicator_in_place(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("MidReroller")
    chat_id = _create_chat(contact_id, _user_id())

    # Build U/A/U/A by hand so the active path resolves to all four in order.
    def _msg(parent_id, sender, text):
        return _post_json(f"/api/chats/{chat_id}/messages", {
            "parent_id": parent_id, "sender": sender,
            "body": [{"text": text, "emotion": "neutral"}],
        })
    m1 = _msg(None, "user", "first user")
    m2 = _msg(m1["id"], "contact", "first assistant")
    m3 = _msg(m2["id"], "user", "second user")
    m4 = _msg(m3["id"], "contact", "second assistant")

    # Multi-delta stream with delay so the in-flight DOM state is observable.
    _mock_state.deltas = ["A ", "B ", "C "]
    _mock_state.delta_delay = 0.25

    _open_chat_in_browser(page, chat_id)
    for mid in (m1, m2, m3, m4):
        page.wait_for_selector(
            f'.msg[data-msg-id="{mid["id"]}"]', timeout=5000,
        )

    # ArrowUp × 3 from the auto-focused empty textarea steps the lock from
    # the path tail (m4) → m3 → m2.
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")
    page.wait_for_selector(
        f'.msg[data-msg-id="{m2["id"]}"].nav-locked', timeout=2000,
    )

    # curright past the last (only) sibling on a contact message triggers
    # a reroll — a virtual sibling appears at the same parent slot.
    page.keyboard.press("ArrowRight")
    page.wait_for_selector("#typing-indicator", timeout=3000)

    # The rerolled message + its downstream descendants must be hidden so
    # the indicator visually occupies the rerolled message's position.
    for mid, label in (
        (m2, "rerolled message"),
        (m3, "first descendant"),
        (m4, "second descendant"),
    ):
        assert not page.locator(
            f'.msg[data-msg-id="{mid["id"]}"]'
        ).is_visible(), (
            f"{label} {mid['id']} must hide while the reroll streams"
        )

    # And the indicator should be the trailing visible .msg in the chat.
    last_visible_id = page.evaluate(
        """
        () => {
          const msgs = Array.from(document.querySelectorAll(
            '#chat-messages > .msg'
          )).filter(m => m.offsetParent !== null);
          return msgs.length ? msgs[msgs.length - 1].id : null;
        }
        """,
    )
    assert last_visible_id == "typing-indicator", (
        f"typing indicator should be the trailing visible msg "
        f"(got {last_visible_id!r})"
    )

    # Wait for the persist to land + path refresh.
    page.wait_for_function(
        "!document.getElementById('typing-indicator')", timeout=8000,
    )

    # The active path naturally truncates to [m1, new_msg] — m2/m3/m4
    # sit on the original branch and are no longer on the active path.
    path_ids = page.evaluate(
        """
        async () => {
          const s = await import('/static/state.js');
          return s.state.activePathIds;
        }
        """,
    )
    assert len(path_ids) == 2, (
        f"path must truncate to root + new sibling (got {path_ids!r})"
    )
    assert path_ids[0] == m1["id"]
    assert path_ids[1] not in (m2["id"], m3["id"], m4["id"]), (
        f"path tail must be the new sibling, not one of the originals "
        f"(got {path_ids[1]!r})"
    )


def test_keyboard_reroll_curleft_restores_original_branch(
    page: Page, clean_state, mock_provider,
):
    """During a mid-chat reroll, curleft from the virtual sibling back to
    the original swaps visibility: indicator hides, the rerolled message +
    its descendants come back. curright steps back onto the virtual and
    re-hides them. Mirrors the tail-case behaviour so the user can preview
    either branch while the new one streams."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("MidRerollSwap")
    chat_id = _create_chat(contact_id, _user_id())

    def _msg(parent_id, sender, text):
        return _post_json(f"/api/chats/{chat_id}/messages", {
            "parent_id": parent_id, "sender": sender,
            "body": [{"text": text, "emotion": "neutral"}],
        })
    m1 = _msg(None, "user", "first user")
    m2 = _msg(m1["id"], "contact", "first assistant")
    m3 = _msg(m2["id"], "user", "second user")
    m4 = _msg(m3["id"], "contact", "second assistant")

    # Long-enough stream that we can observe two visibility swaps inside it.
    _mock_state.deltas = ["A ", "B ", "C ", "D "]
    _mock_state.delta_delay = 0.25  # ~1s total

    _open_chat_in_browser(page, chat_id)
    for mid in (m1, m2, m3, m4):
        page.wait_for_selector(
            f'.msg[data-msg-id="{mid["id"]}"]', timeout=5000,
        )

    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")
    page.wait_for_selector(
        f'.msg[data-msg-id="{m2["id"]}"].nav-locked', timeout=2000,
    )
    page.keyboard.press("ArrowRight")
    page.wait_for_selector("#typing-indicator", timeout=3000)

    # After the reroll fires, the originals are hidden (the primary
    # assertion lives in the sibling test; we only need to know we
    # started from the "indicator visible, originals hidden" state).
    assert not page.locator(f'.msg[data-msg-id="{m2["id"]}"]').is_visible()

    # curleft from the virtual swaps the slot: originals come back, the
    # indicator hides — even though the stream is still in flight.
    page.keyboard.press("ArrowLeft")
    page.wait_for_function(
        f'document.querySelector(\'.msg[data-msg-id="{m2["id"]}"]\').'
        f'offsetParent !== null',
        timeout=2000,
    )
    assert page.locator(f'.msg[data-msg-id="{m3["id"]}"]').is_visible()
    assert page.locator(f'.msg[data-msg-id="{m4["id"]}"]').is_visible()
    indicator_visible = page.evaluate(
        "() => { const e = document.getElementById('typing-indicator');"
        " return !!e && e.offsetParent !== null; }"
    )
    assert not indicator_visible, "indicator must hide when slot shows the original"

    # curright back onto the virtual re-hides the originals.
    page.keyboard.press("ArrowRight")
    page.wait_for_function(
        f'!document.querySelector(\'.msg[data-msg-id="{m2["id"]}"]\').'
        f'offsetParent',
        timeout=2000,
    )
    assert not page.locator(f'.msg[data-msg-id="{m4["id"]}"]').is_visible()
    indicator_visible = page.evaluate(
        "() => { const e = document.getElementById('typing-indicator');"
        " return !!e && e.offsetParent !== null; }"
    )
    assert indicator_visible

    # Let generation finish so the test doesn't leave the SSE thread hanging.
    page.wait_for_function(
        "!document.getElementById('typing-indicator')", timeout=8000,
    )


# ---------------------------------------------------------------------------
# Reasoning tray white-space: while reasoning streams BEFORE the first
# content delta, the tray is hosted inside the placeholder ``.bubble.streaming``
# (no ``.generic``). ``.bubble``'s ``pre-wrap`` would otherwise inherit into
# ``.reasoning-body`` and double up the literal ``\n`` between markdown
# blocks on top of <p> margins — the same regression the ``.bubble.generic``
# override fixed for persisted content. Pin the override on .reasoning-body
# itself so it holds regardless of containing bubble class.
# ---------------------------------------------------------------------------


def test_reasoning_tray_white_space_normal_during_streaming(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("ReasoningPreWrap")
    chat_id = _create_chat(contact_id, _user_id())

    # Reasoning streams first, then content. Delay between deltas so we
    # can sample mid-stream: after the first reasoning delta lands but
    # before content arrives + replaces the placeholder bubble.
    _mock_state.reasoning_deltas = ["First thought.\n\n", "Second thought."]
    _mock_state.deltas = ["Final answer."]
    _mock_state.delta_delay = 0.3

    _open_chat_in_browser(page, chat_id)
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("trigger")
    textarea.press("Enter")

    # Wait for the reasoning tray to mount. With the delay set above, no
    # content delta has arrived yet — the tray lives inside the placeholder
    # ``.bubble.streaming`` rather than a ``.bubble.generic`` bubble.
    page.wait_for_selector(".reasoning-tray", timeout=5000)
    parent_is_generic = page.evaluate(
        "() => { const tray = document.querySelector('.reasoning-tray');"
        " return !!(tray && tray.parentElement"
        "   && tray.parentElement.classList.contains('generic')); }"
    )
    assert not parent_is_generic, (
        "tray should be inside the placeholder bubble during reasoning-only"
        " streaming — otherwise the white-space inheritance under test is"
        " masked by the .bubble.generic override"
    )

    # The fix: .reasoning-body must override the inherited .bubble pre-wrap
    # so block-level newlines from the markdown renderer don't double up on
    # top of <p> margins. Once content arrives and the tray re-homes into
    # .bubble.generic, the issue self-corrects — but mid-reasoning-stream
    # the user sees the doubled whitespace until then.
    white_space = page.evaluate(
        "() => getComputedStyle(document.querySelector('.reasoning-body'))"
        ".whiteSpace"
    )
    assert white_space == "normal", (
        f".reasoning-body must compute white-space: normal during streaming "
        f"(got {white_space!r})"
    )

    # Drain the rest of the stream so the SSE thread shuts down cleanly.
    page.wait_for_function(
        "!document.getElementById('typing-indicator')", timeout=8000,
    )


# ---------------------------------------------------------------------------
# Typing indicator: the placeholder bubble pre-first-bubble/delta should
# carry the classic italic + muted "Typing…" look (.bubble.typing class +
# the text), restored after the mobile-compatibility pass swapped it for
# a bare … ellipsis. Once the first delta lands, the placeholder is
# replaced entirely — the .typing class shouldn't be on the persisted
# bubble.
# ---------------------------------------------------------------------------


def test_streaming_placeholder_shows_typing_indicator(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("TypingPlaceholder")
    chat_id = _create_chat(contact_id, _user_id())

    # Reasoning streams first — those events mount the tray inside the
    # placeholder bubble but DON'T swap the placeholder row. That gives
    # the test a wide window to inspect the placeholder before the first
    # content delta replaces it.
    _mock_state.reasoning_deltas = ["a.", "b.", "c."]
    _mock_state.deltas = ["A"]
    _mock_state.delta_delay = 0.3

    _open_chat_in_browser(page, chat_id)
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("trigger")
    textarea.press("Enter")

    # Placeholder bubble: present pre-first-content-delta with the
    # .bubble.typing class and the literal "Typing…" text.
    page.wait_for_selector("#typing-indicator .bubble.typing", timeout=2000)
    result = page.evaluate(
        "() => { const el = document.querySelector("
        " '#typing-indicator .bubble.typing'); "
        " return el ? el.innerText : null; }"
    )
    assert result is not None, "placeholder vanished before we could read it"
    assert "Typing…" in result, (
        f"placeholder text should include 'Typing…' (got {result!r})"
    )

    # Drain.
    page.wait_for_function(
        "!document.getElementById('typing-indicator')", timeout=8000,
    )


# ---------------------------------------------------------------------------
# Reasoning tray expand state survives stream-end: if the user opens the
# Thoughts tray while a generation is streaming, refreshMessages re-renders
# the persisted msg from state and the new tray must come back expanded.
# ---------------------------------------------------------------------------


def test_reasoning_tray_stays_expanded_after_stream_end(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("PersistExpand")
    chat_id = _create_chat(contact_id, _user_id())

    # Reasoning first then content so we can click the tray mid-stream
    # before it gets replaced when content streams in.
    _mock_state.reasoning_deltas = ["Step one. ", "Step two."]
    _mock_state.deltas = ["The answer."]
    _mock_state.delta_delay = 0.3

    _open_chat_in_browser(page, chat_id)
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("trigger")
    textarea.press("Enter")

    # Wait for the tray, then expand it via header click.
    tray = page.locator(".reasoning-tray").first
    tray.wait_for(timeout=5000)
    header = tray.locator(".reasoning-header")
    # The default is collapsed — confirm baseline, then click to expand.
    expect(header).to_have_attribute("aria-expanded", "false")
    header.click()
    expect(header).to_have_attribute("aria-expanded", "true")

    # Drain the rest of the stream. refreshMessages will re-render the
    # persisted msg, building a fresh tray; the open state must carry.
    page.wait_for_function(
        "!document.getElementById('typing-indicator')", timeout=8000,
    )
    # After refresh, the new tray sits inside a persisted .msg (not the
    # indicator). Header's aria-expanded must still be true and the body
    # must be visible — i.e. the user's mid-stream expand carried over.
    persisted_tray = page.locator(
        ".msg.contact:not(#typing-indicator) .reasoning-tray"
    ).first
    expect(persisted_tray.locator(".reasoning-header")).to_have_attribute(
        "aria-expanded", "true",
    )
    expect(persisted_tray.locator(".reasoning-body")).to_be_visible()


# ---------------------------------------------------------------------------
# Chat-input menu — generic mode
# ---------------------------------------------------------------------------


def test_menu_button_morphs_to_attach_when_open_in_generic_mode(
    page: Page, clean_state, mock_provider,
):
    """Open the menu in Generic mode → the button itself becomes the
    paperclip (attach) action; clicking it again fires the file picker.
    Closing the menu restores the ``menu`` (hamburger) icon."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("MenuMorph")
    chat_id = _create_chat(contact_id, _user_id())

    _open_chat_in_browser(page, chat_id)
    page.wait_for_selector(".chat-input-menu-btn", timeout=5000)

    icon_closed = page.evaluate(
        "document.querySelector('.chat-input-menu-btn svg').innerHTML"
    )
    page.click(".chat-input-menu-btn")
    page.wait_for_selector(".chat-input-menu-panel", timeout=2000)
    icon_open = page.evaluate(
        "document.querySelector('.chat-input-menu-btn svg').innerHTML"
    )
    assert icon_closed != icon_open, "generic mode should morph the button to the attach paperclip"
    # The open-state icon is the ``paperclip`` path — distinctive M-prefix.
    assert "M21.44 11.05" in icon_open, icon_open

    # Second click while open ⇒ fires the file picker (a hidden input
    # appended to the body). The picker is a native dialog — we can't
    # interact with it via Playwright, but we can verify the helper
    # element shows up and the popover closes.
    page.click(".chat-input-menu-btn")
    page.wait_for_function(
        "() => !document.querySelector('.chat-input-menu-panel')",
        timeout=2000,
    )
    # Icon restores to the closed ``menu`` (hamburger) icon.
    icon_after = page.evaluate(
        "document.querySelector('.chat-input-menu-btn svg').innerHTML"
    )
    assert icon_after == icon_closed


def test_chat_input_menu_attach_row_generic_only(
    page: Page, clean_state, mock_provider,
):
    """The chat-input menu carries an explicit ``Attach`` row in Generic mode
    (same action as the morphed ``+``), discoverable without the two-click
    dance. AER mode omits it. Clicking the row closes the popover and opens
    the (native, non-interactive) file picker."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("AttachRow")
    chat_id = _create_chat(contact_id, _user_id())
    _open_chat_in_browser(page, chat_id)

    page.wait_for_selector(".chat-input-menu-btn", timeout=5000)
    page.click(".chat-input-menu-btn")
    page.wait_for_selector(".chat-input-menu-panel", timeout=2000)
    rows = page.locator(".chat-input-menu-row").all_inner_texts()
    assert any("Attach" in r for r in rows), rows
    page.click(".chat-input-menu-row:has-text('Attach')")
    page.wait_for_function(
        "() => !document.querySelector('.chat-input-menu-panel')", timeout=2000,
    )

    # Flip the global provider mode to AER and re-open: the row is gone.
    _put_json("/api/settings", {"provider_mode": "aetherroom"})
    _open_chat_in_browser(page, chat_id)
    page.wait_for_selector(".chat-input-menu-btn", timeout=5000)
    page.click(".chat-input-menu-btn")
    page.wait_for_selector(".chat-input-menu-panel", timeout=2000)
    aer_rows = page.locator(".chat-input-menu-row").all_inner_texts()
    assert not any("Attach" in r for r in aer_rows), aer_rows


def test_attached_chip_appears_above_input(page: Page, clean_state, mock_provider):
    """Uploading an attachment via the API (the picker is non-interactive
    from Playwright) renders a chip above the input, and a per-chip
    remove button purges it from disk."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("AttachAlice")
    chat_id = _create_chat(contact_id, _user_id())

    _open_chat_in_browser(page, chat_id)
    page.wait_for_selector(".chat-input-menu-btn", timeout=5000)

    # Smallest valid PNG bytes — same fixture the backend tests use.
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\xdac\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01"
        b"^\xf3*:"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    # Push the attachment onto the textarea's pendingAttachments list
    # via the client API — simulates the picker flow without needing to
    # drive the native file-picker dialog.
    import base64
    page.evaluate(
        """async ({chatId, b64}) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const file = new File([bytes], 'cat.png', { type: 'image/png' });
          const fd = new FormData();
          fd.append('file', file);
          const r = await fetch(`/api/chats/${chatId}/attachments`, {
            method: 'POST', body: fd,
          });
          const att = await r.json();
          const ta = document.querySelector('.chat-input-area textarea');
          ta._pendingAttachments.push(att);
          ta._renderAttachmentChips();
        }""",
        {"chatId": chat_id, "b64": base64.b64encode(png).decode("ascii")},
    )

    chip = page.locator(".chat-input-chip").first
    chip.wait_for(timeout=2000)
    assert chip.locator(".chat-input-chip-name").text_content() == "cat.png"
    # Remove button purges chip + file.
    chip.locator(".icon-btn").click()
    page.wait_for_function(
        "() => !document.querySelector('.chat-input-chip')",
        timeout=2000,
    )


def test_paste_and_drop_image_attaches_in_generic_mode(page: Page, clean_state):
    """Pasting (Ctrl/Cmd+V or the context-menu Paste — both fire `paste`) and
    drag-dropping an image onto the chat input add it as a pending attachment
    chip in Generic mode, persisted on the chat."""
    import base64
    _put_json("/api/settings", {"provider_mode": "generic"})
    contact_id = _create_contact("Paster")
    chat_id = _create_chat(contact_id, _user_id())
    _open_chat_in_browser(page, chat_id)

    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\xdac\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01"
        b"^\xf3*:"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    b64 = base64.b64encode(png).decode("ascii")

    # Paste: a synthetic ClipboardEvent carrying the image File.
    page.evaluate(
        """({b64}) => {
          const ta = document.querySelector('.chat-input-area textarea');
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const f = new File([bytes], 'pasted.png', { type: 'image/png' });
          const dt = new DataTransfer(); dt.items.add(f);
          ta.dispatchEvent(new ClipboardEvent('paste',
            { clipboardData: dt, bubbles: true, cancelable: true }));
        }""",
        {"b64": b64},
    )
    page.wait_for_selector(".chat-input-chip", timeout=4000)
    assert "pasted.png" in page.locator(".chat-input-chip-name").first.text_content()

    # Drop: a synthetic DragEvent carrying the image File, onto the input bar.
    page.evaluate(
        """({b64}) => {
          const area = document.querySelector('.chat-input-area');
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const f = new File([bytes], 'dropped.png', { type: 'image/png' });
          const dt = new DataTransfer(); dt.items.add(f);
          area.dispatchEvent(new DragEvent('drop',
            { dataTransfer: dt, bubbles: true, cancelable: true }));
        }""",
        {"b64": b64},
    )
    page.wait_for_function(
        "() => document.querySelectorAll('.chat-input-chip').length === 2",
        timeout=4000,
    )
    names = page.locator(".chat-input-chip-name").all_inner_texts()
    assert "dropped.png" in names, names
    chat = _get_json(f"/api/chats/{chat_id}")
    assert len(chat.get("pending_attachments") or []) == 2


def _is_impersonate_generate(request) -> bool:
    return "/generate" in request.url and "mode=impersonate" in request.url


def test_alt_enter_in_input_triggers_impersonate(
    page: Page, clean_state, mock_provider,
):
    """Alt+Enter from the focused chat input fires an Impersonate generation
    (the model writes the user's next turn) instead of sending — verified by
    the generate request carrying ``mode=impersonate``. A plain send would hit
    ``/generate`` without that param (or POST a message first), so requiring
    the param also proves Alt+Enter wasn't treated as Enter."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("ImpersonateInput")
    chat_id = _create_chat(contact_id, _user_id())
    _open_chat_in_browser(page, chat_id)

    textarea = page.locator(".chat-input-area textarea")
    with page.expect_request(_is_impersonate_generate, timeout=5000):
        textarea.press("Alt+Enter")


def test_alt_enter_outside_input_triggers_impersonate(
    page: Page, clean_state, mock_provider,
):
    """Alt+Enter also works when the chat input isn't focused (the document-
    level fallback) — e.g. after clicking a message or a control button."""
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("ImpersonateDoc")
    chat_id = _create_chat(contact_id, _user_id())
    _open_chat_in_browser(page, chat_id)

    # Move focus off the input so its own keydown can't claim the combo.
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    with page.expect_request(_is_impersonate_generate, timeout=5000):
        page.keyboard.press("Alt+Enter")


def test_impersonate_shortcut_listed_in_help(page: Page, clean_state):
    """The Alt+Enter impersonate shortcut is advertised in the empty-state
    'Keyboard shortcuts' help — intentionally NOT in the input menu."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    # No chat selected → the empty state with the shortcut legend renders.
    page.wait_for_selector(".shortcut-list", timeout=5000)
    row = page.locator(".shortcut-row", has_text="Impersonate")
    row.wait_for(timeout=2000)
    keys = row.locator("kbd").all_text_contents()
    assert "Enter" in keys, keys
    # Modifier label is platform-dependent (Alt elsewhere, ⌥ on macOS).
    assert ("Alt" in keys) or ("⌥" in keys), keys


def test_help_shortcuts_use_mac_symbols_on_macos(page: Page, clean_state):
    """On macOS the shortcut legend swaps Ctrl / Alt for ⌘ / ⌥. The handlers
    accept the native key either way (Cmd is metaKey, Option is altKey), so
    only the labels change. Force a Mac platform before the app boots."""
    page.add_init_script(
        "Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });"
    )
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector(".shortcut-list", timeout=5000)
    keys = page.locator(".shortcut-list kbd").all_text_contents()
    assert "⌘" in keys and "⌥" in keys, keys
    # Translated, not duplicated — the plain-text labels are gone.
    assert "Ctrl" not in keys and "Alt" not in keys, keys


def test_bubble_max_width_caps_rendered_bubble(page: Page, clean_state):
    """End-to-end: with a max bubble width set, a long message's bubble is
    capped to that em width (× the chat font size) instead of filling its
    column. Guards the CSS rule that binds --bubble-max-width to .bubble."""
    # 20 em × the default 14px chat font ≈ 280px border-box.
    _put_json("/api/settings", {"max_bubble_width_em": 20})
    contact_id = _create_contact("BubbleCap")
    chat_id = _create_chat(contact_id, _user_id())
    # Long enough that, uncapped, the bubble would fill far past 280px.
    _post_user_message(chat_id, "lorem ipsum dolor sit amet " * 30)
    _open_chat_in_browser(page, chat_id)

    bubble = page.locator(".msg.user .bubble").first
    bubble.wait_for(timeout=5000)
    width = bubble.evaluate("(el) => el.offsetWidth")
    assert width <= 290, width      # capped near 20em (=280px), not column-wide
    assert width >= 100, width      # sanity: the long content really rendered


# ---------------------------------------------------------------------------
# Generic-mode newline handling: ``marked`` runs with ``breaks: true`` so a
# lone newline inside a paragraph renders as a ``<br>``. Without it, GFM
# collapses a single newline to a space and the visible line structure of a
# chat reply is lost.
# ---------------------------------------------------------------------------


def test_generic_single_newline_becomes_br(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    contact_id = _create_contact("BreakLines")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = ["line one\nline two"]
    _open_chat_in_browser(page, chat_id)
    _trigger_generic_generation(page, chat_id)

    bubble = page.locator(
        ".msg.contact:not(#typing-indicator) .bubble.generic"
    ).first
    # The single newline produced a <br> inside one paragraph — not a
    # collapsed space, and not a split into two <p> blocks.
    assert bubble.locator("br").count() >= 1
    assert bubble.locator("p").count() == 1
    text = bubble.inner_text()
    assert "line one" in text and "line two" in text


# ---------------------------------------------------------------------------
# Generic-mode auto-TTS: AER emits discrete ``bubble`` events and speaks each
# in ``onBubble``; Generic streams one buffer via ``delta`` and has no
# per-bubble boundary, so the finished reply is spoken in ``onDone``.
# Regression: auto-TTS was wired only into ``onBubble``, so Generic replies
# never spoke even with the gate armed.
# ---------------------------------------------------------------------------


def test_generic_auto_tts_fires_on_completion(
    page: Page, clean_state, mock_provider,
):
    _configure_generic_provider(mock_provider)
    # Global TTS active (default_on); a default contact follows the global
    # toggle, so this alone arms auto-TTS. clean_state doesn't reset settings,
    # so restore off in ``finally`` for sibling tests.
    _put_json("/api/settings", {"tts": {"mode": "default_on"}})
    contact_id = _create_contact("TalkyContact")
    chat_id = _create_chat(contact_id, _user_id())

    _mock_state.deltas = ["Hello there, a spoken reply."]
    _open_chat_in_browser(page, chat_id)
    try:
        # Record every media ``src`` assignment. The queued TTS <audio> gets a
        # ``/api/tts/speak`` URL — proof the autoplay gate ran and the reply was
        # enqueued, with no dependency on headless media actually fetching.
        page.evaluate(
            """() => {
              window.__ttsUrls = [];
              const d = Object.getOwnPropertyDescriptor(
                HTMLMediaElement.prototype, 'src');
              Object.defineProperty(HTMLMediaElement.prototype, 'src', {
                configurable: true,
                get() { return d.get.call(this); },
                set(v) { window.__ttsUrls.push(v); d.set.call(this, v); },
              });
            }"""
        )
        _trigger_generic_generation(page, chat_id)
        page.wait_for_function(
            "() => (window.__ttsUrls || [])"
            ".some(u => String(u).includes('/api/tts/speak'))",
            timeout=8000,
        )
        urls = page.evaluate("window.__ttsUrls")
        assert any("/api/tts/speak" in u for u in urls), urls
    finally:
        _put_json("/api/settings", {"tts": {"mode": "off"}})
