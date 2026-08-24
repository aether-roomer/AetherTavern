"""Playwright-driven UI smoke tests.

If ``AETHER_BASE_URL`` is set, hit that running server; otherwise spawn a
fresh ``uvicorn`` on a free port with a temp ``AETHER_DATA_DIR`` so the
suite never touches the user's real data. Screenshots land under
``AETHER_SCREENSHOTS`` for visual review.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import pytest


pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright, Page


SCREENSHOTS = Path(os.environ.get("AETHER_SCREENSHOTS", "/tmp/claude-1000/aether-screens"))
SYNTH_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Resolved by the ``_server`` fixture below — tests read this lazily via
# :func:`base_url` rather than at import time.
_BASE_URL: str | None = None


def base_url() -> str:
    assert _BASE_URL, "_server fixture must run before tests"
    return _BASE_URL


def _bail_if_production(base: str) -> None:
    """Refuse to run UI tests against a real instance — the suite creates,
    overwrites and deletes entities, and conflict tests in particular depend
    on a known starting state. Two signals flag a populated server:

    - The API token is set (settings expose ``api_token_indicator`` as a
      non-empty sentinel when the user has configured one).
    - Any contacts / users / scenarios / chats already exist (the seeded
      ``Anon`` / ``User`` personas are allowed since storage.initialize
      creates them).
    """
    try:
        with urlopen(f"{base}/healthz", timeout=3) as r:
            json.load(r)
    except Exception:
        pytest.skip(f"No server reachable at {base}")
    try:
        with urlopen(f"{base}/api/settings", timeout=3) as r:
            settings = json.load(r)
    except Exception:
        settings = {}
    if settings.get("api_token_indicator"):
        pytest.skip(
            f"Refusing to run UI tests against {base}: an API token is set, "
            f"so this looks like a real instance. Unset AETHER_BASE_URL to "
            f"spin up an isolated test server."
        )
    counts = {}
    for kind in ("contacts", "users", "scenarios", "chats"):
        try:
            with urlopen(f"{base}/api/{kind}", timeout=3) as r:
                counts[kind] = len(json.load(r))
        except Exception:
            counts[kind] = 0
    # Storage seeds "Anon" + "User" personas on first run, so up to 2 users
    # is fine.
    populated = (
        counts["contacts"] > 0
        or counts["scenarios"] > 0
        or counts["chats"] > 0
        or counts["users"] > 2
    )
    if populated:
        pytest.skip(
            f"Refusing to run UI tests against {base}: data dir already has "
            f"entities ({counts}). Unset AETHER_BASE_URL or point it at an "
            f"empty test instance."
        )


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture(scope="module", autouse=True)
def _server():
    """Use ``AETHER_BASE_URL`` if set; otherwise spawn an isolated uvicorn
    on a free port pointed at a fresh temp ``AETHER_DATA_DIR`` and tear it
    down after the module."""
    global _BASE_URL
    if os.environ.get("AETHER_BASE_URL"):
        _BASE_URL = os.environ["AETHER_BASE_URL"]
        _bail_if_production(_BASE_URL)
        yield
        return

    port = _free_port()
    tmpdir = tempfile.mkdtemp(prefix="aether-uitest-")
    env = {
        **os.environ,
        "AETHER_DATA_DIR": tmpdir,
        # Skip the GLM-4.6 tokenizer download; UI tests don't render prompts.
        "AETHER_SKIP_TOKENIZER": "1",
    }
    cmd = [
        sys.executable, "-m", "uvicorn",
        "server.main:app", "--host", "127.0.0.1", "--port", str(port),
        "--log-level", "warning",
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urlopen(f"{base}/healthz", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        shutil.rmtree(tmpdir, ignore_errors=True)
        pytest.skip(f"Failed to start test server: {err[:400]}")

    _BASE_URL = base
    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    errors: list = []
    pg.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
    pg.on("console", lambda m: m.type in ("error", "warning") and errors.append(f"[console.{m.type}] {m.text}"))
    yield pg
    if errors:
        print("\n".join(errors))
    ctx.close()


def shot(page: Page, name: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / f"{name}.png"), full_page=False)


def _api_delete_all(kind: str) -> None:
    """Wipe every entity of ``kind`` via the JSON API. Used by ``clean_state``."""
    from urllib.request import Request

    base = base_url()
    try:
        with urlopen(f"{base}/api/{kind}", timeout=3) as r:
            items = json.load(r)
    except Exception:
        return
    for item in items:
        eid = item.get("id")
        if not eid:
            continue
        try:
            urlopen(Request(f"{base}/api/{kind}/{eid}", method="DELETE"), timeout=3)
        except Exception:
            pass


@pytest.fixture
def clean_state():
    """Wipe all contacts/users/scenarios/libraries/chats so the test starts
    from a known empty state. The module-scoped ``_server`` fixture only sets
    up the storage dir once; tests share it otherwise."""
    for kind in ("chats", "contacts", "users", "scenarios", "libraries"):
        _api_delete_all(kind)
    yield


def test_app_loads(page: Page):
    page.goto(base_url())
    page.wait_for_selector("#rail", timeout=5000)
    shot(page, "01-app-loads")
    assert "AetherTavern" in page.title()


def test_navigate_tabs(page: Page):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    for tab in ("contacts", "users", "scenarios", "libraries", "settings", "chats"):
        page.click(f'.rail-btn[data-tab="{tab}"]')
        page.wait_for_timeout(200)
        shot(page, f"02-tab-{tab}")
    active = page.eval_on_selector(".rail-btn.active", "el => el.dataset.tab")
    assert active == "chats"


def test_settings_page_renders(page: Page):
    page.goto(base_url())
    page.click('.rail-btn[data-tab="settings"]')
    # The AER/Generic tab switcher doubles as the section header.
    page.wait_for_selector(".inference-tab", timeout=5000)
    shot(page, "03-settings")
    presets_count = page.locator("h3:has-text('Generation presets')").count()
    assert presets_count > 0
    assert page.locator("h3:has-text('Image generation')").count() == 1
    assert page.locator("label:has-text('Image system prompt')").count() == 1
    assert page.locator("label:has-text('UC (undesired content)')").count() == 1
    assert page.locator(".image-setting-reset").count() == 3


def test_image_text_settings_reset_independently(page: Page):
    shipped = json.load(urlopen(f"{base_url()}/api/settings"))[
        "image_generation_defaults"
    ]
    page.goto(base_url())
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector(".image-setting-reset", timeout=5000)

    system = page.locator('textarea[data-setting-key="system_prompt"]')
    user = page.locator('textarea[data-setting-key="user_message"]')
    uc = page.locator('textarea[data-setting-key="negative_prompt"]')
    system.fill("CUSTOM IMAGE SYSTEM")
    user.fill("CUSTOM IMAGE USER")
    uc.fill("CUSTOM IMAGE UC")
    page.wait_for_timeout(250)

    page.locator(
        '.image-setting-reset[data-setting-key="system_prompt"]'
    ).click()
    assert system.input_value() == shipped["system_prompt"]
    assert user.input_value() == "CUSTOM IMAGE USER"
    assert uc.input_value() == "CUSTOM IMAGE UC"
    page.wait_for_timeout(250)
    saved = json.load(urlopen(f"{base_url()}/api/settings"))["image_generation"]
    assert saved["system_prompt"] == shipped["system_prompt"]
    assert saved["user_message"] == "CUSTOM IMAGE USER"
    assert saved["negative_prompt"] == "CUSTOM IMAGE UC"

    page.locator(
        '.image-setting-reset[data-setting-key="user_message"]'
    ).click()
    page.locator(
        '.image-setting-reset[data-setting-key="negative_prompt"]'
    ).click()
    assert user.input_value() == shipped["user_message"]
    assert uc.input_value() == shipped["negative_prompt"]
    page.wait_for_timeout(250)
    saved = json.load(urlopen(f"{base_url()}/api/settings"))["image_generation"]
    assert saved["system_prompt"] == shipped["system_prompt"]
    assert saved["user_message"] == shipped["user_message"]
    assert saved["negative_prompt"] == shipped["negative_prompt"]


def test_edit_message_modal_textarea_grows_no_inner_scroll(page: Page):
    """The edit-message modal auto-grows each bubble textarea to fit its
    content, so the modal's own overflow is the single scroll container.

    Regression: the textarea's row count tracked newline count, not wrapped
    lines, so a long single-line message stayed ~2 rows tall and scrolled
    internally while the modal scrolled around it — double scrolling. The
    modal makes no API calls on open, so we drive it directly with a
    synthetic long message and inspect the textarea geometry.
    """
    page.goto(base_url())
    page.wait_for_selector("#rail")
    # 250 words, zero newlines: the old newline-based row count was 2, far
    # too short for the wrapped content.
    long_text = "word " * 250
    page.evaluate(
        """async (text) => {
          const mod = await import('/static/views/edit_message_modal.js');
          mod.openEditMessageModal('test-chat', {
            id: 'test-msg', sender: 'user', origin: 'manual',
            body: [{ text, emotion: 'neutral' }],
          });
        }""",
        long_text,
    )
    ta = page.locator(".bubble-edit-stack textarea").first
    ta.wait_for(state="visible", timeout=3000)
    metrics = page.evaluate(
        """() => {
          const ta = document.querySelector('.bubble-edit-stack textarea');
          return {
            scrollHeight: ta.scrollHeight,
            clientHeight: ta.clientHeight,
            overflowY: getComputedStyle(ta).overflowY,
          };
        }"""
    )
    # The textarea is tall enough for all its content — no internal scroll.
    assert metrics["scrollHeight"] - metrics["clientHeight"] <= 2, metrics
    # And it can never grow its own scrollbar to fight the modal's.
    assert metrics["overflowY"] == "hidden", metrics
    # Sanity: the content really did wrap well past the old 2-row height,
    # so this exercises the overflow case rather than a trivially short box.
    assert metrics["clientHeight"] > 120, metrics


def test_bubble_width_slider_drives_css_var(page: Page, clean_state):
    """The Max-bubble-width slider sets the --bubble-max-width CSS var in em,
    and the top 'Unlimited' tick maps it to 100% (the default / no cap — a
    length rather than ``none`` so it stays usable in the .msg.contact
    controls-alignment calc)."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="settings"]')
    slider = page.locator(".range-row input[type='range']").first
    slider.wait_for(state="visible", timeout=5000)

    def set_slider(value):
        # Dispatch input+change explicitly so both the live readout (input)
        # and the persist handler (change) fire, regardless of how Playwright
        # synthesizes range edits.
        slider.evaluate(
            "(el, v) => { el.value = String(v);"
            " el.dispatchEvent(new Event('input', { bubbles: true }));"
            " el.dispatchEvent(new Event('change', { bubbles: true })); }",
            value,
        )

    def css_var():
        return page.evaluate(
            "() => getComputedStyle(document.documentElement)"
            ".getPropertyValue('--bubble-max-width').trim()"
        )

    # Default is unlimited → no cap.
    assert css_var() == "100%"

    # A concrete em cap reaches the CSS var + the readout.
    set_slider(60)
    page.wait_for_function(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--bubble-max-width').trim() === '60em'",
        timeout=3000,
    )
    assert "60 em" in (page.locator(".range-value").first.text_content() or "")

    # Sliding fully right (top tick) maps back to unlimited → 100%.
    set_slider(141)
    page.wait_for_function(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--bubble-max-width').trim() === '100%'",
        timeout=3000,
    )
    assert "Unlimited" in (page.locator(".range-value").first.text_content() or "")


def test_capped_bubble_pulls_controls_to_bubble_edge(page: Page, clean_state):
    """With a bubble-width cap set, a contact message's action controls ride
    alongside the bubble's right edge instead of stranding at the far side of
    the pane. Regression guard for the `.msg.contact` width cap: the meta-row
    controls (group start) and the hover overlay (continuation) both anchor to
    the message's right edge, which must track the capped bubble's right edge.

    The UI font and chat font are set to deliberately different sizes: the em
    cap is relative to the chat font, so if the `.msg.contact` calc resolved
    `em` against the inherited UI font instead, the cap would diverge from the
    bubble's and the controls would drift off its edge."""
    from urllib.request import Request

    _api_create_contact("CapAria")
    user_id = _api_create_user("CapUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "CapAria")
    chat_id = _api_post_chat(contact_id, user_id)

    def post_contact_msg(text, parent_id=None):
        payload: dict = {"sender": "contact", "body": [{"text": text, "emotion": "neutral"}]}
        if parent_id is not None:
            payload["parent_id"] = parent_id
        req = Request(
            f"{base_url()}/api/chats/{chat_id}/messages", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return json.loads(urlopen(req, timeout=3).read())["id"]

    m1 = post_contact_msg("A short contact reply that does not fill the pane.")
    post_contact_msg(
        "A continuation message from the same contact, long enough to wrap a "
        "couple of lines inside the capped bubble so the overlay has a corner "
        "to sit against.",
        parent_id=m1,
    )

    # A cap small enough to leave slack on the 1400px-wide desktop pane, with
    # a UI font deliberately larger than the chat font so a wrong em basis in
    # the cap calc would visibly misalign the controls.
    urlopen(Request(
        f"{base_url()}/api/settings",
        data=json.dumps({
            "max_bubble_width_em": 24, "font_size": 20, "content_font_size": 12,
        }).encode(),
        headers={"Content-Type": "application/json"}, method="PUT",
    ), timeout=3)

    page.goto(f"{base_url()}/chats")
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".msg.contact.group-start .meta-row .controls", timeout=5000)
    page.wait_for_selector(".msg.contact.group-cont .controls-overlay", timeout=5000)

    rects = page.evaluate(
        """() => {
          const right = sel => document.querySelector(sel).getBoundingClientRect().right;
          return {
            paneRight: right('.chat-messages'),
            startBubbleRight: right('.msg.contact.group-start .bubble'),
            startCtrlsRight: right('.msg.contact.group-start .meta-row .controls'),
            contBubbleRight: right('.msg.contact.group-cont .bubble'),
            contOverlayRight: right('.msg.contact.group-cont .controls-overlay'),
          };
        }"""
    )

    # Group-start controls sit at the bubble's right edge (not the pane's).
    assert abs(rects["startCtrlsRight"] - rects["startBubbleRight"]) <= 12, rects
    # The continuation hover-overlay tracks its bubble's right edge too.
    assert abs(rects["contOverlayRight"] - rects["contBubbleRight"]) <= 12, rects
    # And both are pulled well clear of the far edge — the capped bubble leaves
    # substantial slack between its right edge and the pane edge (the wart was
    # the controls stranded out in that slack).
    assert rects["paneRight"] - rects["startCtrlsRight"] > 150, rects


def test_short_contact_bubble_hugs_content_and_reserves_controls(page: Page, clean_state):
    """A short contact reply hugs its text instead of filling the cap (the box
    shrink-wraps to content), while the message box is floored at a min-width
    that keeps the controls clear of the avatar sprite — so a continuation's
    absolutely-positioned overlay doesn't spill back over the sprite."""
    from urllib.request import Request

    _api_create_contact("HugAria")
    user_id = _api_create_user("HugUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "HugAria")
    chat_id = _api_post_chat(contact_id, user_id)

    def post_contact_msg(text, parent_id=None):
        payload: dict = {"sender": "contact", "body": [{"text": text, "emotion": "neutral"}]}
        if parent_id is not None:
            payload["parent_id"] = parent_id
        req = Request(
            f"{base_url()}/api/chats/{chat_id}/messages", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return json.loads(urlopen(req, timeout=3).read())["id"]

    m1 = post_contact_msg("Hi")
    post_contact_msg("Yo", parent_id=m1)  # continuation: controls live in the overlay

    # A generous cap so a short bubble that *hugged* and one that *filled* would
    # differ dramatically — the assertion below only holds if it hugs.
    urlopen(Request(
        f"{base_url()}/api/settings", data=json.dumps({"max_bubble_width_em": 60}).encode(),
        headers={"Content-Type": "application/json"}, method="PUT",
    ), timeout=3)

    page.goto(f"{base_url()}/chats")
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".msg.contact.group-cont .controls-overlay", timeout=5000)

    rects = page.evaluate(
        """() => {
          const r = sel => document.querySelector(sel).getBoundingClientRect();
          return {
            startBubbleW: r('.msg.contact.group-start .bubble').width,
            startBoxW: r('.msg.contact.group-start').width,
            contdOverlayLeft: r('.msg.contact.group-cont .controls-overlay').left,
            contdSpriteRight: r('.msg.contact.group-cont .emotion-sprite').right,
          };
        }"""
    )

    # The bubble hugs "Hi" — nowhere near the 60em cap (~840px at the default
    # chat font), so a small absolute threshold proves it shrank to content.
    assert rects["startBubbleW"] < 220, rects
    # The box is floored wide enough to seat the controls (avatar col + gap +
    # controls width), so a one-word reply still reserves their room.
    assert rects["startBoxW"] >= 360, rects
    # That floor is what keeps the continuation overlay off the sprite: its
    # left edge lands at/after the sprite's right edge rather than over it.
    assert rects["contdOverlayLeft"] >= rects["contdSpriteRight"] - 2, rects


def test_theme_picker_hides_toolbar_toggle_for_custom_themes(page: Page):
    """The toolbar sun/moon button only makes sense for the light/dark pair.
    Picking any other theme from the theme grid hides it; reverting to light/dark
    brings it back."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    # Reset to a known-toggleable state first (test order isn't guaranteed and
    # earlier tests may have left a custom theme selected).
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector(".theme-picker", timeout=5000)
    page.click('.theme-tile[data-theme="dark"]')
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    assert page.locator("#theme-toggle").is_visible()

    # Pick a non-toggleable theme — toolbar button must hide.
    page.click('.theme-tile[data-theme="noir"]')
    page.wait_for_function("document.documentElement.dataset.theme === 'noir'")
    page.wait_for_function(
        "document.getElementById('theme-toggle').hidden === true"
    )
    assert page.locator('.theme-tile[data-theme="noir"].selected').count() == 1
    shot(page, "04-theme-noir")

    # OLED is a first-class non-toggleable theme with its own true-black tile.
    assert page.locator('.theme-tile[data-theme="oled"]').count() == 1
    oled_bg = page.locator('.theme-tile[data-theme="oled"] .theme-tile-preview').evaluate(
        "el => getComputedStyle(el).backgroundColor"
    )
    assert oled_bg == "rgb(0, 0, 0)"

    # Server persisted the choice.
    with urlopen(f"{base_url()}/api/settings") as r:
        assert json.load(r)["theme"] == "noir"

    # Switch back to light — toolbar button returns.
    page.click('.theme-tile[data-theme="light"]')
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    page.wait_for_function(
        "document.getElementById('theme-toggle').hidden === false"
    )
    assert page.locator("#theme-toggle").is_visible()


def test_import_emits_download_and_derive_phases(page: Page, clean_state):
    """Contact import streams a ``download`` phase (image fetches) followed by
    a ``derive`` phase (display-derivative builds). The progress modal flips
    its phase chip caption between the two so the user can see which phase
    is in flight rather than thinking the import stalled while the
    ``build_display_file`` calls grind through 24 emotion sprites."""
    fixture_text = (SYNTH_FIXTURES / "contact_with_emotion.json").read_text()
    page.goto(base_url())
    page.wait_for_selector("#rail")

    # Patch ``api.importFile`` to snapshot the phase chip on every progress
    # event, then run the real ``importWithProgress`` flow so we exercise the
    # whole onProgress → DOM update path.
    states = page.evaluate(
        """async (text) => {
            const m = await import('/static/views/import_progress.js');
            const apiMod = await import('/static/api.js');
            const file = new File([text], 'contact.json', {
                type: 'application/json',
            });
            const out = [];
            const orig = apiMod.api.importFile;
            apiMod.api.importFile = (f, opts) => orig(f, {
                ...opts,
                onProgress: (p) => {
                    opts.onProgress && opts.onProgress(p);
                    const chip = document.querySelector('.progress-phase');
                    out.push({
                        phase: p.phase || null,
                        chipText: chip ? chip.textContent : null,
                        chipHidden: chip
                            ? chip.classList.contains('hidden') : null,
                    });
                },
            });
            try { await m.importWithProgress(file); }
            finally { apiMod.api.importFile = orig; }
            return out;
        }""",
        fixture_text,
    )
    phases = {s["phase"] for s in states if s["phase"]}
    assert "download" in phases, states
    assert "derive" in phases, states
    # Every phased event leaves the chip visible with the right caption.
    for s in states:
        if s["phase"] == "download":
            assert s["chipText"] == "Downloading", s
            assert s["chipHidden"] is False, s
        elif s["phase"] == "derive":
            assert s["chipText"] == "Saving display images", s
            assert s["chipHidden"] is False, s

    # Server-side: the import landed; contact is on disk.
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    assert any(c["name"] == "FixtureEmoOnly" for c in contacts), contacts


def test_import_synthesizes_avatar_from_neutral_emotion(page: Page, clean_state):
    """Contact has a neutral emotion sprite but no avatar — importer copies
    the sprite into the avatar slot. Exercises the synthesis fallback and
    confirms the avatar editor surfaces a real image."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_with_emotion.json")
    page.wait_for_selector("text=FixtureEmoOnly", timeout=8000)
    shot(page, "09-after-import")
    page.locator("#contact-list-body .list-row").filter(has_text="FixtureEmoOnly").first.click()
    page.wait_for_selector("h3:has-text('Emotion sprites')")
    shot(page, "10-emotion-only-detail")
    # Avatar element in the detail block should render a real <img>, not the
    # first-letter monogram fallback.
    assert page.locator('#contact-detail .avatar img').count() > 0
    # And on the server, contact.avatar is now set to the synthesised file.
    with urlopen(f"{base_url()}/api/contacts/fixture-emonly-contact-0001") as r:
        c = json.load(r)
    assert c["avatar"] is not None and c["avatar"].startswith("avatar.")


def test_create_contact_user_chat(page: Page):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    # Contacts → New (button is icon-only; identified by title attribute).
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    page.click('button[title="New contact"]')
    page.wait_for_selector("#contact-detail .page-header h2")
    name_input = page.locator('#contact-detail input[type="text"]').first
    name_input.fill("TestAlice")
    page.wait_for_timeout(800)  # debounced save

    persona_textarea = page.locator('#contact-detail textarea').first
    persona_textarea.fill("Cheerful and curious.")
    page.wait_for_timeout(800)
    shot(page, "04-contact-edited")

    # Users → New persona
    page.click('.rail-btn[data-tab="users"]')
    page.wait_for_selector("#user-list-body")
    page.click('button[title="New persona"]')
    page.wait_for_selector("#user-detail .page-header h2")
    user_name_input = page.locator('#user-detail input[type="text"]').first
    user_name_input.fill("TestBob")
    page.wait_for_timeout(800)
    shot(page, "05-user-edited")

    # Chats → New chat (modal). The list-pane button is icon-only too.
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector('button[title="New chat"]', timeout=3000)
    page.click('button[title="New chat"]')
    page.wait_for_selector('.modal h3:has-text("New chat")', timeout=3000)
    shot(page, "06-new-chat-wizard")
    page.click('.modal button:has-text("Create")')
    page.wait_for_selector(".chat-view", timeout=5000)
    shot(page, "07-empty-chat")

    # Send a user message — server has no endpoint configured so generation
    # will surface an error toast, which is fine for the UI smoke test.
    textarea = page.locator(".chat-input-area textarea")
    textarea.fill("Hi there!")
    page.click('button:has-text("Send")')
    page.wait_for_timeout(2000)
    shot(page, "08-user-msg-sent")


# ---------------------------------------------------------------------------
# Per-chat input drafts: every keystroke in the chat input bar persists to
# localStorage under ``chatDraft:{uuid}`` so switching chats / closing the
# tab / reloading restores the user's in-flight message. Cleared on send and
# on chat deletion.
# ---------------------------------------------------------------------------


def test_chat_list_shows_message_count(page: Page, clean_state):
    """Chat list rows render a per-chat badge with the active-path message
    count. The badge stays visible when the chat is selected (regression
    against ``loadActiveChat`` clobbering ``message_count`` on the in-state
    chat) and bumps after a new user message lands."""
    _api_create_contact("CountAlice")
    user_id = _api_create_user("CountUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "CountAlice")
    chat_id = _api_post_chat(contact_id, user_id)
    # Seed a 3-message linear chain.
    parent: str | None = None
    for i in range(3):
        parent = _api_post_user_message(chat_id, f"msg {i}", parent_id=parent)

    page.goto(f"{base_url()}/chats")
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)
    badge = page.locator("#chat-list-body .list-row .row-msg-count").first
    assert badge.text_content() == "3"

    # Click the chat — badge must stay (stays in state.chats with its count).
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-input-area textarea", timeout=5000)
    active_badge = page.locator("#chat-list-body .list-row.active .row-msg-count").first
    assert active_badge.text_content() == "3", \
        "count badge should stay visible when chat is active"

    # Send a message — generation will fail (no api token configured) but the
    # createMessage step succeeds and loadActiveChat refreshes state.chats with
    # the new active-path length.
    page.locator(".chat-input-area textarea").fill("hello")
    page.click('button:has-text("Send")')
    page.wait_for_function(
        "() => document.querySelector('#chat-list-body .list-row.active "
        ".row-msg-count')?.textContent === '4'",
        timeout=5000,
    )


def test_chat_input_draft_persists_per_chat(page: Page, clean_state):
    """Two chats, distinct drafts. Switch between them, reload the page —
    each chat shows its own draft restored. Auto-resize fires on restore so
    multi-line drafts don't render as a single squashed row."""
    _api_create_contact("DraftAlice")
    user_id = _api_create_user("DraftUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "DraftAlice")
    chat_a = _api_post_chat(contact_id, user_id)
    chat_b = _api_post_chat(contact_id, user_id)

    page.goto(base_url())
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)

    # Open chat A → fill draft → confirm it's in localStorage.
    rows = page.locator("#chat-list-body .list-row")
    rows.nth(0).click()
    page.wait_for_selector(".chat-input-area textarea", timeout=5000)
    ta = page.locator(".chat-input-area textarea")
    chat_a_id = page.evaluate("window.location.pathname.split('-').pop()")
    # Resolve actual chat ids by mapping the rendered list to API order.
    api_chats = json.load(urlopen(f"{base_url()}/api/chats", timeout=3))
    # Sorted desc by updated_at on the server; the list pane mirrors that.
    a_id_full = api_chats[0]["id"]
    b_id_full = api_chats[1]["id"]

    draft_a = "draft for A — line one\nline two\nline three"
    ta.fill(draft_a)
    saved_a = page.evaluate(
        f"localStorage.getItem('chatDraft:{a_id_full}')"
    )
    assert saved_a == draft_a, f"chat A draft not saved: {saved_a!r}"

    # Auto-resize on multi-line input — height should exceed a single row.
    multi_height = page.evaluate(
        "document.querySelector('.chat-input-area textarea').clientHeight"
    )
    assert multi_height > 30, f"expected auto-resize, got {multi_height}px"

    # Switch to chat B → blank draft, fill a different one.
    rows.nth(1).click()
    page.wait_for_function(
        "() => document.querySelector('.chat-input-area textarea')?.value === ''"
    )
    ta = page.locator(".chat-input-area textarea")
    draft_b = "draft for B"
    ta.fill(draft_b)

    # Switch back to A → draft restored, caret at end so typing resumes.
    rows.nth(0).click()
    page.wait_for_function(
        f"() => document.querySelector('.chat-input-area textarea')?.value "
        f"=== {json.dumps(draft_a)}"
    )
    sel = page.evaluate(
        "(() => { const t = document.querySelector('.chat-input-area textarea');"
        " return [t.selectionStart, t.selectionEnd, t.value.length]; })()"
    )
    assert sel[0] == sel[1] == sel[2], f"caret should be at end, got {sel}"

    # Reload — both drafts still present.
    page.reload()
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)
    rows = page.locator("#chat-list-body .list-row")
    rows.nth(0).click()
    page.wait_for_function(
        f"() => document.querySelector('.chat-input-area textarea')?.value "
        f"=== {json.dumps(draft_a)}"
    )
    rows.nth(1).click()
    page.wait_for_function(
        f"() => document.querySelector('.chat-input-area textarea')?.value "
        f"=== {json.dumps(draft_b)}"
    )

    # Clearing the textarea drops the localStorage key.
    page.locator(".chat-input-area textarea").fill("")
    cleared = page.evaluate(
        f"localStorage.getItem('chatDraft:{b_id_full}')"
    )
    assert cleared is None, f"empty draft should remove the LS key, got {cleared!r}"


def test_chat_input_draft_clears_on_send(page: Page, clean_state):
    """Sending a non-empty draft (createMessage succeeds even without an
    inference endpoint configured — generation fails afterwards but the
    user message is already on disk) clears the persisted draft."""
    _api_create_contact("DraftSendAlice")
    user_id = _api_create_user("DraftSendUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "DraftSendAlice")
    chat_id = _api_post_chat(contact_id, user_id)

    page.goto(base_url())
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-input-area textarea", timeout=5000)

    ta = page.locator(".chat-input-area textarea")
    ta.fill("about to send")
    assert page.evaluate(
        f"localStorage.getItem('chatDraft:{chat_id}')"
    ) == "about to send"

    page.click('button:has-text("Send")')
    # createMessage resolves quickly; the LS clear runs synchronously after.
    page.wait_for_function(
        f"() => localStorage.getItem('chatDraft:{chat_id}') === null",
        timeout=5000,
    )
    # Textarea also empties.
    assert page.locator(".chat-input-area textarea").input_value() == ""


def test_chat_input_draft_clears_on_chat_delete(page: Page, clean_state):
    """Deleting a chat removes its draft from localStorage so a future chat
    re-using the same UUID (improbable but possible via import) doesn't
    inherit a stale draft."""
    _api_create_contact("DraftDeleteAlice")
    user_id = _api_create_user("DraftDeleteUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "DraftDeleteAlice")
    chat_id = _api_post_chat(contact_id, user_id)

    page.goto(base_url())
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-input-area textarea", timeout=5000)
    page.locator(".chat-input-area textarea").fill("about to be orphaned")
    assert page.evaluate(
        f"localStorage.getItem('chatDraft:{chat_id}')"
    ) == "about to be orphaned"

    # Auto-confirm the destructive modal that the delete button raises.
    page.once("dialog", lambda d: d.accept())
    page.locator("button[title='Delete chat']").click()
    page.wait_for_selector(".modal button:has-text('Delete')", timeout=3000)
    page.locator(".modal button:has-text('Delete')").last.click()
    page.wait_for_function(
        f"() => localStorage.getItem('chatDraft:{chat_id}') === null",
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# Import conflict resolution: same UUID re-imported triggers the conflict
# modal; the user can pick Overwrite (with a destructive confirm), New copy,
# or Cancel.
# ---------------------------------------------------------------------------


def _import_contact(page: Page, fixture_name: str) -> None:
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    page.locator("#contact-import-input").set_input_files(
        str(SYNTH_FIXTURES / fixture_name)
    )


def test_import_conflict_new_copy(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_alice.json")
    page.wait_for_selector("text=FixtureAlice", timeout=8000)

    # Re-import a different contact JSON with the same UUID.
    _import_contact(page, "contact_alice_collision.json")
    page.wait_for_selector("h3:has-text('Already exists')", timeout=8000)
    shot(page, "11-conflict-modal")
    page.click('.modal button:has-text("New copy")')

    # Both names should now be present in the list.
    page.wait_for_selector("text=FixtureAliceVariant", timeout=8000)
    titles = page.locator("#contact-list-body .row-title").all_text_contents()
    assert any("FixtureAlice" in t for t in titles)
    assert any("FixtureAliceVariant" in t for t in titles)


def test_import_conflict_overwrite(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_alice.json")
    page.wait_for_selector("text=FixtureAlice", timeout=8000)

    _import_contact(page, "contact_alice_collision.json")
    page.wait_for_selector("h3:has-text('Already exists')", timeout=8000)
    page.click('.modal button:has-text("Overwrite")')

    # Destructive confirm modal pops on top.
    page.wait_for_selector("h3:has-text('Overwrite FixtureAlice')", timeout=5000)
    shot(page, "12-overwrite-confirm")
    # The confirmModal renders Cancel + Overwrite; click Overwrite.
    page.locator('.modal button:has-text("Overwrite")').last.click()

    # The original entry should now be replaced — only the variant remains.
    page.wait_for_selector("text=FixtureAliceVariant", timeout=8000)
    titles = page.locator("#contact-list-body .row-title").all_text_contents()
    assert any("FixtureAliceVariant" in t for t in titles)
    assert not any(t == "FixtureAlice" for t in titles)
    # And the contact's body really got replaced (not just the name).
    with urlopen(f"{base_url()}/api/contacts/fixture-alice-contact-0001") as r:
        c = json.load(r)
    assert c["name"] == "FixtureAliceVariant"
    assert c["persona"] == "Same id, different content."
    assert "variant" in c["tags"]
    assert c["greeting"] == "Hello again!"


def test_import_conflict_overwrite_canceled(page: Page, clean_state):
    """Backing out of the destructive confirm pops the user back to the
    conflict prompt — original data must stay intact and the import flow
    must not deadlock."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_alice.json")
    page.wait_for_selector("text=FixtureAlice", timeout=8000)

    _import_contact(page, "contact_alice_collision.json")
    page.wait_for_selector("h3:has-text('Already exists')", timeout=8000)
    page.click('.modal button:has-text("Overwrite")')

    page.wait_for_selector("h3:has-text('Overwrite FixtureAlice')", timeout=5000)
    # Cancel the destructive confirm.
    page.locator('.modal button:has-text("Cancel")').last.click()

    # Conflict prompt should be back so the user can pick another option.
    page.wait_for_selector("h3:has-text('Already exists')", timeout=5000)
    # Now cancel the whole import.
    page.locator('.modal button:has-text("Cancel")').first.click()

    # Original contact untouched.
    page.wait_for_timeout(400)
    with urlopen(f"{base_url()}/api/contacts/fixture-alice-contact-0001") as r:
        c = json.load(r)
    assert c["name"] == "FixtureAlice"
    assert c["greeting"] == "Hi there!"
    titles = page.locator("#contact-list-body .row-title").all_text_contents()
    assert sum(1 for t in titles if "FixtureAlice" in t) == 1
    assert not any("Variant" in t for t in titles)


def test_import_legacy_contact_no_uuid(page: Page, clean_state):
    """A v9-shape export with no ``id`` field should import silently, get a
    fresh UUID, and never trigger the conflict modal."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_legacy_no_id.json")
    page.wait_for_selector("text=FixtureLegacy", timeout=8000)

    # No conflict modal should be visible (importing a legacy file twice
    # creates two distinct contacts since each gets a fresh UUID).
    _import_contact(page, "contact_legacy_no_id.json")
    page.wait_for_timeout(800)
    assert page.locator("h3:has-text('Already exists')").count() == 0
    titles = page.locator("#contact-list-body .row-title").all_text_contents()
    assert sum(1 for t in titles if t == "FixtureLegacy") == 2


def test_import_conflict_cancel_leaves_state_unchanged(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_alice.json")
    page.wait_for_selector("text=FixtureAlice", timeout=8000)

    _import_contact(page, "contact_alice_collision.json")
    page.wait_for_selector("h3:has-text('Already exists')", timeout=8000)
    page.locator('.modal button:has-text("Cancel")').first.click()

    # No second contact got imported.
    page.wait_for_timeout(400)
    titles = page.locator("#contact-list-body .row-title").all_text_contents()
    assert sum(1 for t in titles if "FixtureAlice" in t) == 1
    assert not any("Variant" in t for t in titles)


# ---------------------------------------------------------------------------
# Composite name-match: chat composite carries character/user with a name
# that already exists on disk under a different UUID — the user is asked to
# pick the existing one or import as new.
# ---------------------------------------------------------------------------


def _import_chat_file(page: Page, fixture_name: str) -> None:
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector("#chat-list-body")
    page.locator("#chat-import-input").set_input_files(
        str(SYNTH_FIXTURES / fixture_name)
    )


def test_composite_name_match_pick_existing(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_alice.json")
    page.wait_for_selector("text=FixtureAlice", timeout=8000)
    # Seed the user too so it's a name-match candidate as well.
    page.click('.rail-btn[data-tab="users"]')
    page.wait_for_selector("#user-list-body")
    page.locator("#user-import-input").set_input_files(str(SYNTH_FIXTURES / "user_bob.json"))
    page.wait_for_selector("text=FixtureBob", timeout=8000)

    _import_chat_file(page, "chat_namematch.json")
    page.wait_for_selector("h3:has-text('Names match existing entities')", timeout=8000)
    shot(page, "13-namematch-modal")

    # Two role sections: Character + User. Pick the existing FixtureAlice
    # for character (first candidate radio), and existing FixtureBob for user.
    # The candidate radios are the ones whose label contains the existing UUID.
    char_section = page.locator(".name-match-role").filter(has_text="Character:")
    char_section.locator("label").filter(
        has_text="fixture-alice-contact-0001"
    ).first.click()
    user_section = page.locator(".name-match-role").filter(has_text="User:")
    user_section.locator("label").filter(
        has_text="fixture-bob-user-0001"
    ).first.click()
    page.click('.modal button:has-text("Confirm")')

    # Chat lands. Sidecars unchanged — still only one of each name.
    page.wait_for_selector("#chat-list-body .list-row", timeout=8000)
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    titles = page.locator("#contact-list-body .row-title").all_text_contents()
    assert sum(1 for t in titles if "FixtureAlice" in t) == 1


def test_composite_name_match_pick_new(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_alice.json")
    page.wait_for_selector("text=FixtureAlice", timeout=8000)
    page.click('.rail-btn[data-tab="users"]')
    page.wait_for_selector("#user-list-body")
    page.locator("#user-import-input").set_input_files(str(SYNTH_FIXTURES / "user_bob.json"))
    page.wait_for_selector("text=FixtureBob", timeout=8000)

    _import_chat_file(page, "chat_namematch.json")
    page.wait_for_selector("h3:has-text('Names match existing entities')", timeout=8000)
    # Default selection is "Import as new" — just confirm without picking
    # anything. Two distinct contacts/users should result.
    page.click('.modal button:has-text("Confirm")')
    page.wait_for_selector("#chat-list-body .list-row", timeout=8000)

    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    titles = page.locator("#contact-list-body .row-title").all_text_contents()
    # Two contacts share the name — the original and the freshly-imported
    # composite sidecar with its own UUID.
    assert sum(1 for t in titles if "FixtureAlice" in t) == 2


def test_composite_name_match_cancel(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_alice.json")
    page.wait_for_selector("text=FixtureAlice", timeout=8000)
    page.click('.rail-btn[data-tab="users"]')
    page.wait_for_selector("#user-list-body")
    page.locator("#user-import-input").set_input_files(str(SYNTH_FIXTURES / "user_bob.json"))
    page.wait_for_selector("text=FixtureBob", timeout=8000)

    _import_chat_file(page, "chat_namematch.json")
    page.wait_for_selector("h3:has-text('Names match existing entities')", timeout=8000)
    page.locator('.modal button:has-text("Cancel")').first.click()

    # No chat created.
    page.wait_for_timeout(400)
    page.wait_for_selector("#chat-list-body")
    rows = page.locator("#chat-list-body .list-row").count()
    assert rows == 0


# ---------------------------------------------------------------------------
# Sort dropdown + direction toggle on the contacts list. Smoke-checks that
# the controls render, that picking "Name" reorders the rows alphabetically,
# and that clicking the arrow flips the direction.
# ---------------------------------------------------------------------------


def _contact_row_names(page: Page) -> list[str]:
    return page.eval_on_selector_all(
        "#contact-list-body .list-row .row-title",
        "els => els.map(e => e.textContent)",
    )


def _api_create_contact(name: str) -> None:
    """Seed a contact directly via the API. Avoids the click-to-create-then-
    rename race in the UI: each POST is a complete operation."""
    from urllib.request import Request

    body = json.dumps({"name": name}).encode()
    req = Request(
        f"{base_url()}/api/contacts",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urlopen(req, timeout=3).read()


def test_save_queue_drains_on_boot(page: Page, clean_state):
    """A payload stuck in ``localStorage.pendingSaves`` from a prior
    session is flushed automatically on next boot. No conflict, so no
    modal — the description just lands on the server."""
    from urllib.request import Request

    _api_create_contact("QueueDrain")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    c_summary = next(x for x in contacts if x["name"] == "QueueDrain")
    # The list endpoint returns ContactSummary which lacks version_id and
    # many of the editor fields. The save-queue's real flow stores the
    # editor's full draft (which came from GET /api/contacts/{id}); mirror
    # that here so the replay's PUT body has a matching version_id.
    with urlopen(f"{base_url()}/api/contacts/{c_summary['id']}") as r:
        c = json.load(r)

    # Land on the app first so localStorage is for the right origin.
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.evaluate(
        """({key, payload}) => {
            localStorage.setItem('pendingSaves', JSON.stringify({
                [key]: { kind: 'contact', id: payload.id, payload, ts: Date.now() }
            }));
        }""",
        {"key": f"contact:{c['id']}", "payload": {**c, "description": "queued"}},
    )
    page.reload()
    page.wait_for_selector("#rail")
    page.wait_for_function(
        "() => Object.keys(JSON.parse(localStorage.getItem('pendingSaves') || '{}'))"
        ".length === 0",
        timeout=5000,
    )
    with urlopen(f"{base_url()}/api/contacts/{c['id']}") as r:
        live = json.load(r)
    assert live["description"] == "queued", live


def test_save_queue_drain_conflict_modal_resolves(page: Page, clean_state):
    """A queued payload with a stale ``version_id`` triggers the conflict
    modal on boot. Picking Overwrite re-issues the save with the live id
    and the queued description wins."""
    from urllib.request import Request

    _api_create_contact("QueueConflict")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    c_summary = next(x for x in contacts if x["name"] == "QueueConflict")
    # List endpoint returns ContactSummary; the editor's save-queue
    # payload is the full draft, so fetch it.
    with urlopen(f"{base_url()}/api/contacts/{c_summary['id']}") as r:
        c = json.load(r)

    # Bump the contact externally so ``c['version_id']`` is now stale.
    urlopen(Request(
        f"{base_url()}/api/contacts/{c['id']}",
        data=json.dumps({**c, "description": "remote"}).encode(),
        headers={"Content-Type": "application/json"}, method="PUT",
    )).read()

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.evaluate(
        """({key, payload}) => {
            localStorage.setItem('pendingSaves', JSON.stringify({
                [key]: { kind: 'contact', id: payload.id, payload, ts: Date.now() }
            }));
        }""",
        {"key": f"contact:{c['id']}", "payload": {**c, "description": "queued local"}},
    )
    page.reload()
    page.wait_for_selector("h3:has-text('changed elsewhere')", timeout=5000)
    page.locator(".modal button:has-text('Overwrite')").click()
    page.wait_for_selector(".modal", state="hidden", timeout=3000)
    page.wait_for_function(
        "() => Object.keys(JSON.parse(localStorage.getItem('pendingSaves') || '{}'))"
        ".length === 0",
        timeout=5000,
    )
    with urlopen(f"{base_url()}/api/contacts/{c['id']}") as r:
        live = json.load(r)
    assert live["description"] == "queued local", live


def test_contact_save_conflict_modal_resolves(page: Page, clean_state):
    """Two-tabs scenario: a contact is edited externally between the
    user's keystroke and the autosaver fire — the in-flight save 409s,
    the conflict modal opens, and the user picks Reload to discard their
    local edit. The remote description wins on disk."""
    from urllib.request import Request

    _api_create_contact("ConflictAlice")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    c_summary = next(x for x in contacts if x["name"] == "ConflictAlice")
    # List endpoint returns ContactSummary; for the PUT-by-original-copy
    # to be a real "stale version_id" simulation, we need the full
    # Contact payload (else missing-field defaults would also trigger
    # 409s for the wrong reason).
    with urlopen(f"{base_url()}/api/contacts/{c_summary['id']}") as r:
        c = json.load(r)

    page.goto(f"{base_url()}/contacts/conflictalice-{c['id'][:8]}")
    page.wait_for_selector("#contact-detail .page-header h2", timeout=5000)

    # Externally bump the version_id by saving with the original copy
    # (which still has the as-created version_id). Server matches and
    # rerolls, so the page's cached chat now holds a stale version.
    external = {**c, "description": "set elsewhere"}
    urlopen(Request(
        f"{base_url()}/api/contacts/{c['id']}", data=json.dumps(external).encode(),
        headers={"Content-Type": "application/json"}, method="PUT",
    )).read()

    # Type a local edit — autosaver fires after its 500 ms debounce.
    persona = page.locator('#contact-detail textarea').first
    persona.click()
    persona.type("local edit", delay=15)
    page.wait_for_selector("h3:has-text('changed elsewhere')", timeout=5000)

    # Pick Reload — the remote description wins on disk.
    page.locator(".modal button:has-text('Reload remote')").click()
    page.wait_for_selector(".modal", state="hidden", timeout=3000)
    page.wait_for_timeout(300)

    with urlopen(f"{base_url()}/api/contacts/{c['id']}") as r:
        live = json.load(r)
    assert live["description"] == "set elsewhere", live


def test_list_favorite_toggle_no_conflict_with_open_editor(page: Page, clean_state):
    """Clicking the favourite star on the contact whose edit page is open
    must not orphan the editor's draft from the new server-side version_id.
    Without the fix, the autosave fires later with the stale version_id and
    the conflict modal pops even though only this single user is editing."""
    _api_create_contact("StarConflictAlice")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    c = next(x for x in contacts if x["name"] == "StarConflictAlice")

    page.goto(f"{base_url()}/contacts/starconflictalice-{c['id'][:8]}")
    page.wait_for_selector("#contact-detail .page-header h2", timeout=5000)

    # Click the star on the same contact's row.
    row = (
        page.locator("#contact-list-body .list-row")
        .filter(has_text="StarConflictAlice").first
    )
    row.locator(".favorite-star").evaluate("el => el.click()")
    # Give the autosave debounce + PUT time to land.
    page.wait_for_timeout(700)

    # Make a real edit on the first textarea (description). With the fix
    # in place the next autosave uses the freshly-bumped version_id and
    # the modal never appears.
    desc = page.locator('#contact-detail textarea').first
    desc.click()
    desc.type("post-star edit", delay=15)
    page.wait_for_timeout(900)

    # The conflict modal must not have appeared.
    assert page.locator("h3:has-text('changed elsewhere')").count() == 0

    with urlopen(f"{base_url()}/api/contacts/{c['id']}") as r:
        live = json.load(r)
    assert live["favorite"] is True, live
    assert "post-star edit" in (live.get("description") or ""), live


def test_contact_sort_by_name_and_direction_toggle(page: Page, clean_state):
    # Seed in a known non-alphabetical order so each sort mode produces a
    # distinct ordering and we can tell them apart.
    for nm in ("Charlie", "Alice", "Bob"):
        _api_create_contact(nm)
        time.sleep(0.02)  # ensure distinct created_at timestamps

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")

    # Default sort is "Added" desc → newest first.
    rows = _contact_row_names(page)
    assert rows == ["Bob", "Alice", "Charlie"], rows

    # Switch the dropdown to "Name" — should produce A, B, C.
    page.locator(".list-pane .list-sort-mode").first.click()
    page.locator(".list-sort-menu-item", has_text="Name").click()
    page.wait_for_timeout(150)
    assert _contact_row_names(page) == ["Alice", "Bob", "Charlie"]

    # Flip the direction toggle — should reverse to Z → A.
    page.locator(".list-pane .list-sort-dir").first.click()
    page.wait_for_timeout(150)
    assert _contact_row_names(page) == ["Charlie", "Bob", "Alice"]


# ---------------------------------------------------------------------------
# Dirty-dot saving indicator on the contact edit view. Lights up the moment
# the user types and clears once the auto-save resolves.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Contact-scoped scenarios: nested under a Contact, surfaced at chat creation
# and on the chat info modal alongside global scenarios. Imported from the
# foreign ``presetSpaces`` array on a contact JSON.
# ---------------------------------------------------------------------------


def _api_get_contact(cid: str) -> dict:
    with urlopen(f"{base_url()}/api/contacts/{cid}") as r:
        return json.load(r)


def _api_create_user(name: str = "FixtureUser") -> str:
    from urllib.request import Request

    body = json.dumps({"name": name}).encode()
    req = Request(
        f"{base_url()}/api/users",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urlopen(req, timeout=3).read())["id"]


def test_import_contact_with_preset_spaces_renders_scenarios_section(
    page: Page, clean_state,
):
    """Importing a contact with ``presetSpaces`` lands two character-scoped
    scenarios under the Contact, with the ``is_default: true`` entry marked
    as default in the edit view."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_with_scenarios.json")
    page.wait_for_selector("text=FixtureCharlie", timeout=8000)

    # Server-side: scenarios are nested on the contact, default points at
    # the "Scenario A" entry.
    c = _api_get_contact("fixture-charlie-contact-0001")
    assert len(c["scenarios"]) == 2
    first = next(s for s in c["scenarios"] if s["name"] == "Scenario A")
    assert c["default_scenario_id"] == first["id"]
    assert first["tags"] == "alpha, bravo, charlie"
    assert first["style"] == "roleplay"
    assert first["intimacy"] == "acquaintance"
    assert first["response_length"] == "short"

    # UI side: edit view shows the Scenarios section with both entries.
    page.locator("#contact-list-body .list-row").filter(
        has_text="FixtureCharlie"
    ).first.click()
    page.wait_for_selector("h3:has-text('Scenarios')", timeout=5000)
    shot(page, "20-contact-scenarios-section")
    # Radios: [0] = (none), [1] = Scenario A (default at import), [2] = Scenario B.
    radios = page.locator("input[type=radio][name^=scenario-default]")
    assert radios.count() == 3
    assert not radios.first.is_checked()
    assert radios.nth(1).is_checked()


def test_contact_scenarios_add_and_delete_in_edit_view(page: Page, clean_state):
    """Adding and removing a scenario in the contact edit view round-trips
    through the API. First-added auto-becomes default; deleting the default
    promotes the next entry to default."""
    page.goto(base_url())
    page.wait_for_selector("#rail")

    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    page.click('button[title="New contact"]')
    page.wait_for_selector("h3:has-text('Scenarios')", timeout=5000)

    # Add two scenarios.
    add_btn = page.locator("button:has-text('+ Add scenario')")
    add_btn.click()
    page.wait_for_timeout(300)
    add_btn.click()
    page.wait_for_timeout(900)  # debounce + save

    # Radios: [0] = (none), [1] = first added (auto-default), [2] = second added.
    radios = page.locator("input[type=radio][name^=scenario-default]")
    assert radios.count() == 3
    assert not radios.first.is_checked()
    assert radios.nth(1).is_checked()
    assert not radios.nth(2).is_checked()
    shot(page, "21-scenarios-two-added")

    # Promote the second to default.
    radios.nth(2).click()
    page.wait_for_timeout(900)
    radios = page.locator("input[type=radio][name^=scenario-default]")
    assert not radios.nth(1).is_checked()
    assert radios.nth(2).is_checked()

    # Delete the (now-default) second scenario; default flips back to the
    # first remaining entry. Two "Remove scenario" buttons — the second one
    # corresponds to the second card.
    page.locator("button:has-text('Remove scenario')").nth(1).click()
    page.wait_for_selector(".modal h3:has-text('Remove scenario?')", timeout=3000)
    page.locator(".modal button:has-text('Remove')").last.click()
    page.wait_for_timeout(900)
    radios = page.locator("input[type=radio][name^=scenario-default]")
    # Now: [0] = (none), [1] = the remaining scenario (auto-promoted to default).
    assert radios.count() == 2
    assert not radios.first.is_checked()
    assert radios.nth(1).is_checked()


def test_contact_scenario_default_can_be_none(page: Page, clean_state):
    """The contact may have scenarios but no default — new chats with that
    contact then start without a scenario selected. The "(none)" radio at
    the top of the section flips ``default_scenario_id`` to null."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_with_scenarios.json")
    page.wait_for_selector("text=FixtureCharlie", timeout=8000)
    page.locator("#contact-list-body .list-row").filter(
        has_text="FixtureCharlie"
    ).first.click()
    page.wait_for_selector("h3:has-text('Scenarios')", timeout=5000)

    # Three radios share the same group: (none) + two scenarios. Initially
    # the first scenario is the default (came from ``is_default: true``).
    radios = page.locator("input[type=radio][name^=scenario-default]")
    assert radios.count() == 3
    assert not radios.first.is_checked()  # (none) — not default at import
    assert radios.nth(1).is_checked()     # Scenario A — default

    # Click (none); persists.
    radios.first.click()
    page.wait_for_timeout(900)  # debounce + save
    radios = page.locator("input[type=radio][name^=scenario-default]")
    assert radios.first.is_checked()
    assert not radios.nth(1).is_checked()
    c = _api_get_contact("fixture-charlie-contact-0001")
    assert c["default_scenario_id"] is None
    assert len(c["scenarios"]) == 2  # scenarios still present


def test_new_chat_wizard_lists_contact_scenarios_above_separator(
    page: Page, clean_state,
):
    """Wizard's scenario dropdown lists the contact's scenarios first
    (sorted), then a disabled separator, then global scenarios."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_with_scenarios.json")
    page.wait_for_selector("text=FixtureCharlie", timeout=8000)
    # Add a global scenario so we can verify the separator is rendered.
    page.click('.rail-btn[data-tab="scenarios"]')
    page.wait_for_selector("#scenario-list-body")
    page.locator("#scenario-import-input").set_input_files(
        str(SYNTH_FIXTURES / "scenario_park.json")
    )
    page.wait_for_selector("text=FixturePark", timeout=8000)

    # Reload to reset in-memory state — importing FixturePark above set
    # ``activeScenarioId``, which would otherwise win the wizard's default
    # scenario lookup over the contact's own ``default_scenario_id``.
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    page.locator("#contact-list-body .list-row").filter(
        has_text="FixtureCharlie"
    ).first.click()
    page.wait_for_selector("button:has-text('New chat')", timeout=5000)
    page.click("button:has-text('New chat')")
    page.wait_for_selector(".modal h3:has-text('New chat')", timeout=3000)
    shot(page, "22-new-chat-wizard-with-scenarios")

    # Scenario picker is the third .avatar-picker in the modal (after
    # contact + user).
    scenario_picker = page.locator(".modal .avatar-picker").nth(2)
    opts = _picker_options(page, scenario_picker)
    # Expected order: "" then "contact:" entries (sorted A,B) then a divider
    # below the last contact entry, then global entries.
    values = [o["value"] for o in opts]
    assert values[0] == ""
    contact_idxs = [i for i, v in enumerate(values) if v.startswith("contact:")]
    global_idxs = [i for i, v in enumerate(values) if v.startswith("global:")]
    assert contact_idxs and global_idxs
    assert max(contact_idxs) < min(global_idxs)
    # Divider sits between the last contact-scoped entry and the first global.
    assert opts[max(contact_idxs)]["has_divider_after"]
    # Default scenario is preselected (the contact's default — a contact: entry).
    selected = [o for o in opts if o["selected"]]
    assert len(selected) == 1
    assert selected[0]["value"].startswith("contact:")


def test_chat_info_modal_save_does_not_clobber_inline_edits(page: Page, clean_state):
    """Regression: the chat-view captures ``chat`` once at render time and
    doesn't re-render on state changes — only the ctx stat re-paints. So
    after inline-toggling CJK off, the header / config closures still hold
    the original ``chat`` (cjk=true). Clicking the info button passes that
    stale object into the modal; without a live-state fallback at save
    time, scenario reassignment would spread the stale cjk=true back."""
    from urllib.request import Request

    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_with_scenarios.json")
    page.wait_for_selector("text=FixtureCharlie", timeout=8000)
    user_id = _api_create_user("FixtureUser")

    # Seed a chat directly with cjk=true so the chat-view renders with that
    # value frozen into its closures from the start.
    body = json.dumps({
        "contact_id": "fixture-charlie-contact-0001",
        "user_id": user_id,
    }).encode()
    chat = json.loads(urlopen(Request(
        f"{base_url()}/api/chats", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )).read())
    chat_id = chat["id"]
    # Force cjk=true on the chat via the API.
    chat["cjk"] = True
    urlopen(Request(
        f"{base_url()}/api/chats/{chat_id}",
        data=json.dumps(chat).encode(),
        headers={"Content-Type": "application/json"}, method="PUT",
    )).read()

    # Open the chat through the rail — closures freeze chat.cjk = true.
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector(f"#chat-list-body .list-row", timeout=5000)
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-view", timeout=5000)
    cjk_toggle = page.locator(".chat-config .cjk-toggle input[type=checkbox]")
    assert cjk_toggle.is_checked()

    # Untick CJK inline. State.chats now has cjk=false but the captured
    # ``chat`` in renderHeader/renderConfig still has cjk=true.
    cjk_toggle.click()
    page.wait_for_timeout(700)
    assert json.loads(urlopen(f"{base_url()}/api/chats/{chat_id}").read())["cjk"] is False

    # Click info — modal opens with the stale chat. Change scenario, save.
    page.locator("button[title='Chat info']").click()
    page.wait_for_selector(".modal h3:has-text('Chat info')", timeout=3000)
    scenario_picker = page.locator(".chat-info .info-row").filter(
        has_text="Scenario"
    ).locator(".avatar-picker")
    # Pick any scenario option other than the current one — value irrelevant,
    # we just need the save to fire so it could clobber cjk if buggy.
    opts = _picker_options(page, scenario_picker)
    target = next(o["value"] for o in opts if not o["selected"])
    _picker_select(page, scenario_picker, target)
    page.locator(".modal button:has-text('Save')").click()
    page.wait_for_timeout(700)

    # Without the live-state fallback, the spread would put cjk=true back.
    final = json.loads(urlopen(f"{base_url()}/api/chats/{chat_id}").read())
    assert final["cjk"] is False
    assert final["contact_scenario_id"]


def test_new_chat_wizard_picks_active_entity_across_tabs(page: Page, clean_state):
    """Active entity in any list (contacts / users / scenarios) carries
    over to the wizard regardless of which tab is currently visible."""
    page.goto(base_url())
    page.wait_for_selector("#rail")

    for nm in ("Alpha", "Bravo", "Charlie"):
        _api_create_contact(nm)
    _api_create_user("UserAlpha")
    _api_create_user("UserBravo")

    page.goto(base_url())
    page.wait_for_selector("#rail")

    # Select Bravo on Contacts.
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")
    page.locator("#contact-list-body .list-row").filter(has_text="Bravo").first.click()
    page.wait_for_selector("#contact-detail .page-header")

    # Select UserBravo on Users.
    page.click('.rail-btn[data-tab="users"]')
    page.wait_for_selector("#user-list-body .list-row")
    page.locator("#user-list-body .list-row").filter(has_text="UserBravo").first.click()
    page.wait_for_selector("#user-detail .page-header")

    # Now hop to Chats and open the wizard — both selections must be
    # honoured even though we're not on either of the two tabs.
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector('button[title="New chat"]', timeout=3000)
    page.click('button[title="New chat"]')
    page.wait_for_selector(".modal h3:has-text('New chat')", timeout=3000)
    contact_picker = page.locator(".modal .avatar-picker").nth(0)
    user_picker = page.locator(".modal .avatar-picker").nth(1)
    assert _picker_label(contact_picker) == "Bravo"
    assert _picker_label(user_picker) == "UserBravo"


def test_new_chat_wizard_defaults_prefer_active_then_last_used(
    page: Page, clean_state,
):
    """Wizard defaults: active entity (when its tab is open) > last used in
    a chat > first/none. Verified by parking on a non-default contact in
    the Contacts tab and confirming it appears as the wizard's contact."""
    from urllib.request import Request

    page.goto(base_url())
    page.wait_for_selector("#rail")

    # Three contacts and two users — non-trivial selection space.
    for nm in ("Alpha", "Bravo", "Charlie"):
        _api_create_contact(nm)
    user_a = _api_create_user("UserAlpha")
    user_b = _api_create_user("UserBravo")

    # Refresh the page so state hydrates with the seeded entities.
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")

    # 1) Active-tab heuristic: park on Bravo in the Contacts tab — wizard
    # should open with Bravo selected even though it's not the first.
    page.locator("#contact-list-body .list-row").filter(has_text="Bravo").first.click()
    page.wait_for_selector("#contact-detail .page-header")
    page.click("button:has-text('New chat')")
    page.wait_for_selector(".modal h3:has-text('New chat')", timeout=3000)
    contact_picker = page.locator(".modal .avatar-picker").first
    assert _picker_label(contact_picker) == "Bravo"
    page.locator(".modal button:has-text('Cancel')").click()

    # 2) Last-used heuristic: create a chat with Charlie + UserBravo. Move
    # away from the contacts tab so the active heuristic stays out of it,
    # then open the wizard.
    body = json.dumps({
        "contact_id": next(c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts"))
                           if c["name"] == "Charlie"),
        "user_id": user_b,
    }).encode()
    urlopen(Request(
        f"{base_url()}/api/chats", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )).read()

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector("#chat-list-body", timeout=5000)
    page.click('button[title="New chat"]')
    page.wait_for_selector(".modal h3:has-text('New chat')", timeout=3000)

    contact_picker = page.locator(".modal .avatar-picker").nth(0)
    user_picker = page.locator(".modal .avatar-picker").nth(1)
    assert _picker_label(contact_picker) == "Charlie"
    assert _picker_label(user_picker) == "UserBravo"


def test_new_chat_wizard_scenario_default_uses_contact_default_only(
    page: Page, clean_state,
):
    """Scenario default in the wizard is (none) unless the contact has a
    ``default_scenario_id`` resolving to one of its character-scenarios.
    Last-used and active-tab heuristics are deliberately NOT consulted —
    they surprise more often than they help."""
    from urllib.request import Request

    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_with_scenarios.json")
    page.wait_for_selector("text=FixtureCharlie", timeout=8000)
    user_id = _api_create_user("FixtureUser")

    # Look up the two contact-scenario ids on the imported contact.
    c = _api_get_contact("fixture-charlie-contact-0001")
    sa = next(s for s in c["scenarios"] if s["name"] == "Scenario A")
    sb = next(s for s in c["scenarios"] if s["name"] == "Scenario B")
    assert c["default_scenario_id"] == sa["id"]

    # Create a chat that picked Scenario B — it must NOT seed the wizard's
    # default; only the contact's ``default_scenario_id`` does.
    body = json.dumps({
        "contact_id": c["id"], "user_id": user_id,
        "contact_scenario_id": sb["id"],
    }).encode()
    urlopen(Request(
        f"{base_url()}/api/chats", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )).read()

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector("#chat-list-body", timeout=5000)
    page.click('button[title="New chat"]')
    page.wait_for_selector(".modal h3:has-text('New chat')", timeout=3000)

    # The contact's ``default_scenario_id`` is Scenario A — that wins, not B.
    scenario_picker = page.locator(".modal .avatar-picker").nth(2)
    assert _picker_value(scenario_picker) == f"contact:{sa['id']}"


def test_chat_config_bar_exposes_tags_input(page: Page, clean_state):
    """The inline config bar carries a free-form tags input bound to
    ``chat.tags`` — surfaced here (not the info modal) because tags get
    iterated on often during experimentation."""
    _api_create_user("FixtureUser")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_with_scenarios.json")
    page.wait_for_selector("text=FixtureCharlie", timeout=8000)

    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    page.locator("#contact-list-body .list-row").filter(
        has_text="FixtureCharlie"
    ).first.click()
    page.wait_for_selector("button:has-text('New chat')", timeout=5000)
    page.click("button:has-text('New chat')")
    page.wait_for_selector(".modal h3:has-text('New chat')", timeout=3000)
    page.locator(".modal button:has-text('Create')").click()
    page.wait_for_selector(".chat-view", timeout=5000)

    # Tags input lives on the config bar — fill, blur, persist.
    tag_input = page.locator(".chat-config .chat-tags input")
    assert tag_input.count() == 1
    tag_input.fill("custom, edited tag")
    tag_input.blur()
    page.wait_for_timeout(700)

    chats = json.load(urlopen(f"{base_url()}/api/chats", timeout=3))
    assert any(c.get("tags") == "custom, edited tag" for c in chats)
    # The contact-scenario greeting was the seeded message body.
    chat_id = next(c["id"] for c in chats if c.get("tags") == "custom, edited tag")
    msgs = json.load(urlopen(f"{base_url()}/api/chats/{chat_id}/messages", timeout=3))
    assert msgs, "expected scenario greeting to be seeded"
    assert msgs[0]["body"][0]["text"] == "Scenario A greeting."
    assert msgs[0]["body"][0]["emotion"] == "happy"

    # Sanity: the info modal does not have a Tags row (tags live on the
    # config bar).
    page.locator("button[title='Chat info']").click()
    page.wait_for_selector(".modal h3:has-text('Chat info')", timeout=3000)
    assert page.locator(".chat-info .info-label", has_text="Tags").count() == 0
    page.locator(".modal button:has-text('Cancel')").click()


# ---------------------------------------------------------------------------
# Favourites: per-entity boolean flag with a star icon next to the name in
# list rows + a "favourites first" sort toggle on the controls bar.
# ---------------------------------------------------------------------------


def test_favorite_toggle_persists_via_star_icon(page: Page, clean_state):
    """Clicking the row star flips ``contact.favorite`` through the API and
    flips the icon's ``.active`` class for visual feedback."""
    _api_create_contact("Alpha")
    _api_create_contact("Bravo")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")

    bravo_row = page.locator("#contact-list-body .list-row").filter(has_text="Bravo").first
    star = bravo_row.locator(".favorite-star")
    # Force the hover-only star into view + click it.
    star.evaluate("el => el.click()")
    page.wait_for_timeout(700)

    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    bravo = next(c for c in contacts if c["name"] == "Bravo")
    alpha = next(c for c in contacts if c["name"] == "Alpha")
    assert bravo["favorite"] is True
    assert alpha["favorite"] is False
    # Visual: star is now active.
    bravo_row = page.locator("#contact-list-body .list-row").filter(has_text="Bravo").first
    assert "active" in (bravo_row.locator(".favorite-star").get_attribute("class") or "")


def test_favorites_first_sort_floats_favourites_to_top(page: Page, clean_state):
    """The favourites toggle on the sort group partitions the list:
    favourites at the top (sorted by current mode/direction), then the
    rest (sorted the same way)."""
    for nm in ("Alpha", "Bravo", "Charlie", "Delta"):
        _api_create_contact(nm)
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")

    # Switch to Name sort so the order is deterministic.
    page.locator(".list-pane .list-sort-mode").first.click()
    page.locator(".list-sort-menu-item", has_text="Name").click()
    page.wait_for_timeout(150)
    # ``favoritesFirst`` defaults to true; turn it off so the baseline is
    # plain name order regardless of any future favouriting.
    page.locator(".list-pane .list-sort-fav").first.click()
    page.wait_for_timeout(150)
    assert _contact_row_names(page) == ["Alpha", "Bravo", "Charlie", "Delta"]

    # Favourite Charlie + Bravo. With favourites-first off, they stay in
    # their name slots.
    for nm in ("Charlie", "Bravo"):
        row = page.locator("#contact-list-body .list-row").filter(has_text=nm).first
        row.locator(".favorite-star").evaluate("el => el.click()")
        page.wait_for_timeout(400)
    assert _contact_row_names(page) == ["Alpha", "Bravo", "Charlie", "Delta"]

    # Re-enable favourites-first — Bravo + Charlie at the top in name
    # order; Alpha + Delta in name order below.
    page.locator(".list-pane .list-sort-fav").first.click()
    page.wait_for_timeout(200)
    assert _contact_row_names(page) == ["Bravo", "Charlie", "Alpha", "Delta"]


def test_wizard_dropdowns_sort_favourites_to_top(page: Page, clean_state):
    """Favourites in the contact / user dropdowns float to the top with a
    disabled separator. The scenario dropdown gets three sections:
    contact-scoped → favourite globals → rest."""
    from urllib.request import Request

    page.goto(base_url())
    page.wait_for_selector("#rail")
    _import_contact(page, "contact_with_scenarios.json")
    page.wait_for_selector("text=FixtureCharlie", timeout=8000)
    # Two more contacts, favourite the second.
    _api_create_contact("Alpha")
    _api_create_contact("Bravo")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    bravo = next(c for c in contacts if c["name"] == "Bravo")
    urlopen(Request(
        f"{base_url()}/api/contacts/{bravo['id']}/favorite",
        data=json.dumps({"favorite": True}).encode(),
        headers={"Content-Type": "application/json"}, method="PATCH",
    )).read()
    # Two global scenarios, favourite the second.
    page.click('.rail-btn[data-tab="scenarios"]')
    page.wait_for_selector("#scenario-list-body")
    page.locator("#scenario-import-input").set_input_files(
        str(SYNTH_FIXTURES / "scenario_park.json")
    )
    page.wait_for_selector("text=FixturePark", timeout=8000)
    scens = json.load(urlopen(f"{base_url()}/api/scenarios", timeout=3))
    fav_scen = scens[0]
    urlopen(Request(
        f"{base_url()}/api/scenarios/{fav_scen['id']}/favorite",
        data=json.dumps({"favorite": True}).encode(),
        headers={"Content-Type": "application/json"}, method="PATCH",
    )).read()

    # Reload so state hydrates with the favourite flags we set via API.
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    page.locator("#contact-list-body .list-row").filter(
        has_text="FixtureCharlie"
    ).first.click()
    page.wait_for_selector("button:has-text('New chat')", timeout=5000)
    page.click("button:has-text('New chat')")
    page.wait_for_selector(".modal h3:has-text('New chat')", timeout=3000)

    # Contact picker: favourite ("Bravo") at top, divider, then rest.
    contact_picker = page.locator(".modal .avatar-picker").nth(0)
    contact_opts = _picker_options(page, contact_picker)
    assert contact_opts[0]["label"] == "Bravo", contact_opts
    assert contact_opts[0]["favorite"], contact_opts
    # The first (and only) favourite has a divider after it.
    assert contact_opts[0]["has_divider_after"], contact_opts


def test_dirty_dot_lights_then_clears_on_save(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    page.click('button[title="New contact"]')
    page.wait_for_selector("#contact-detail .page-header h2 .dirty-dot")

    dot = page.locator("#contact-detail .page-header .dirty-dot").first

    def state_classes() -> set[str]:
        return set(dot.evaluate(
            "el => Array.from(el.classList).filter(c => "
            "  ['dirty','saving','error'].includes(c))"
        ))

    # Idle: no state class.
    assert state_classes() == set()

    # Typing into the persona textarea should mark dirty synchronously.
    persona = page.locator('#contact-detail textarea').first
    persona.fill("Cheerful and curious.")
    page.wait_for_timeout(50)
    assert state_classes() & {"dirty", "saving"}, dot.get_attribute("class")

    # After the debounce + save resolves, the dot returns to idle.
    page.wait_for_function(
        "() => { const d = document.querySelector('#contact-detail .dirty-dot');"
        "  return d && !d.classList.contains('dirty') && !d.classList.contains('saving'); }",
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# Bubble rendering: ** bold, * italic+subdued, _ italic; markers consumed
# when paired, kept literal when unbalanced. Triple-backtick fences become
# plain code blocks. Non-code whitespace passes through verbatim.
# ---------------------------------------------------------------------------


def _api_post_chat(contact_id: str, user_id: str) -> str:
    from urllib.request import Request

    body = json.dumps({"contact_id": contact_id, "user_id": user_id}).encode()
    req = Request(
        f"{base_url()}/api/chats", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    return json.loads(urlopen(req, timeout=3).read())["id"]


def _api_post_user_message(chat_id: str, text: str, parent_id: str | None = None) -> str:
    from urllib.request import Request

    payload: dict = {
        "sender": "user",
        "body": [{"text": text, "emotion": "neutral"}],
    }
    if parent_id is not None:
        payload["parent_id"] = parent_id
    body = json.dumps(payload).encode()
    req = Request(
        f"{base_url()}/api/chats/{chat_id}/messages", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    return json.loads(urlopen(req, timeout=3).read())["id"]


# ---- avatar-picker helpers --------------------------------------------------
# The new chat wizard, chat info modal, and chat-config bar use the custom
# ``avatar-picker`` widget defined in ``static/avatar_picker.js``. The widget
# exposes ``getValue()`` / ``setValue()`` on its wrapper element; the trigger
# button shows the current label, and the popover (appended to ``document.body``
# while open) lists selectable rows as ``.avatar-picker-option`` buttons.


def _picker_label(picker_locator) -> str:
    """Return the visible label on a closed picker."""
    return picker_locator.locator(".avatar-picker-label").first.inner_text()


def _picker_value(picker_locator) -> str:
    """Read the current value via the wrapper's ``getValue()`` method."""
    return picker_locator.evaluate("el => el.getValue()")


def _picker_options(page: Page, picker_locator) -> list[dict]:
    """Open the picker, snapshot its option list, and close it.

    Returns ``[{value, label, selected, favorite, has_divider_after}, ...]``
    in source order so callers can assert on grouping (favourites above a
    divider, then the rest) and favourite-star presence."""
    picker_locator.locator(".avatar-picker-trigger").click()
    page.wait_for_selector(".avatar-picker-popover:not(.hidden)")
    items = page.evaluate(
        """() => {
            const list = document.querySelector(
                '.avatar-picker-popover:not(.hidden) .avatar-picker-list'
            );
            if (!list) return [];
            const out = [];
            for (const node of list.children) {
                if (node.classList.contains('avatar-picker-option')) {
                    out.push({
                        value: node.dataset.value,
                        label: node.querySelector('.avatar-picker-option-label')
                            ?.textContent || '',
                        selected: node.classList.contains('selected'),
                        favorite: !!node.querySelector('.avatar-picker-option-star'),
                        has_divider_after: node.nextElementSibling
                            ?.classList.contains('avatar-picker-divider') ?? false,
                    });
                }
            }
            return out;
        }"""
    )
    # Close: ESC at the document level.
    page.keyboard.press("Escape")
    page.wait_for_selector(".avatar-picker-popover", state="hidden")
    return items


def _picker_select(page: Page, picker_locator, value: str) -> None:
    """Click the picker trigger, then click the option with the given
    ``data-value``. Does nothing if it's already selected (would still toggle
    via the click, but skipping is safer for tests asserting save side-
    effects)."""
    if _picker_value(picker_locator) == value:
        return
    picker_locator.locator(".avatar-picker-trigger").click()
    page.wait_for_selector(".avatar-picker-popover:not(.hidden)")
    page.locator(
        f".avatar-picker-popover:not(.hidden) "
        f".avatar-picker-option[data-value=\"{value}\"]"
    ).click()
    page.wait_for_selector(".avatar-picker-popover", state="hidden")


def test_chat_per_chat_model_override(page: Page, clean_state):
    """The config bar's compact ``Model`` button opens a popover with the
    per-chat Provider / Model / Context-preset overrides. Picking a Generic
    provider reveals the model + context-preset pickers and persists
    ``provider_override``; picking AetherRoom hides them again."""
    _api_create_contact("Alpha")
    _api_create_user("Me")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector('button[title="New chat"]', timeout=3000)
    page.click('button[title="New chat"]')
    page.wait_for_selector('.modal h3:has-text("New chat")', timeout=3000)
    # The wizard carries the same override group; AER default -> just Provider.
    assert page.locator('.modal .model-overrides .avatar-picker').count() == 1
    page.click('.modal button:has-text("Create")')
    page.wait_for_selector(".chat-view", timeout=5000)

    # Open the Model popover from the bar button.
    page.click(".chat-config-model-btn")
    page.wait_for_selector('.modal h3:has-text("Model settings")', timeout=3000)
    body = page.locator(".model-overrides")
    # Default global mode is AER -> only the Provider picker shows.
    assert body.locator(".avatar-picker").count() == 1

    # Provider is always the first picker (its label is now a sibling headline).
    provider_picker = body.locator(".avatar-picker").first
    _picker_select(page, provider_picker, "nanogpt")
    # Generic provider -> model + context-preset pickers appear (3 total).
    page.wait_for_function(
        "() => document.querySelectorAll('.model-overrides .avatar-picker').length === 3"
    )
    page.wait_for_timeout(700)  # debounced save

    chats = json.load(urlopen(f"{base_url()}/api/chats", timeout=3))
    assert chats[0]["provider_override"] == "nanogpt"

    # Switch to AetherRoom -> model + context-preset pickers disappear again.
    _picker_select(page, provider_picker, "aetherroom")
    page.wait_for_function(
        "() => document.querySelectorAll('.model-overrides .avatar-picker').length === 1"
    )
    page.wait_for_timeout(700)
    chats = json.load(urlopen(f"{base_url()}/api/chats", timeout=3))
    assert chats[0]["provider_override"] == "aetherroom"

    # Close the popover; the bar button carries the override indicator dot.
    page.locator('.modal button:has-text("Done")').click()
    page.wait_for_selector(".modal", state="hidden")
    assert "has-override" in (
        page.locator(".chat-config-model-btn").get_attribute("class") or ""
    )


def test_new_chat_wizard_model_picker_popover_clickable(page: Page, clean_state):
    """The wizard's per-chat override pickers live inside the modal, but their
    dropdown popover is portalled to <body>. This guards against a z-index /
    overflow-clipping regression: Playwright's click actionability check fails
    if the option is obscured by the modal or clipped out of view."""
    _api_create_contact("Alpha")
    _api_create_user("Me")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector('button[title="New chat"]', timeout=3000)
    page.click('button[title="New chat"]')
    page.wait_for_selector('.modal h3:has-text("New chat")', timeout=3000)

    # Provider is the first (and, at AER default, only) override picker.
    provider_picker = page.locator('.modal .model-overrides .avatar-picker').first
    # Clicks the option THROUGH the popover — fails on z-index/clipping bugs.
    _picker_select(page, provider_picker, "nanogpt")
    assert _picker_value(provider_picker) == "nanogpt"
    # Generic provider reveals the model + context-preset pickers (3 total).
    page.wait_for_function(
        "() => document.querySelectorAll("
        "'.modal .model-overrides .avatar-picker').length === 3"
    )

    # Create the chat and confirm the wizard carried the override end-to-end.
    page.click('.modal button:has-text("Create")')
    page.wait_for_selector(".chat-view", timeout=5000)
    chats = json.load(urlopen(f"{base_url()}/api/chats", timeout=3))
    assert chats[0]["provider_override"] == "nanogpt"


def test_bubble_renders_aer_format(page: Page, clean_state):
    """Bubble text goes through the AER inline highlighter: ** bold, *
    italic+subdued, _ italic. Marker pairs are consumed; unbalanced markers
    stay literal. Triple-backtick fences become plain code blocks. Non-code
    whitespace is preserved verbatim."""
    from urllib.request import Request

    # Seed a contact + user, create one chat, post a message per case.
    _api_create_contact("BubbleAlice")
    user_id = _api_create_user("BubbleBob")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "BubbleAlice")
    chat_id = _api_post_chat(contact_id, user_id)

    cases = [
        ("italic-line-start",        "*foo*"),
        ("italic-mid-word",          "foo*bar*baz"),
        ("emoticon",                 "(*^_^)"),
        ("triple-bold-italic",       "***bold-italic***"),
        ("cjk-boundary",             "これは*強調*です"),
        ("crossed-pair",             "*Mixed, **bold and* italic** text."),
        ("unclosed",                 "*hello"),
        ("code-block",               "before\n```\nx = 1\n```\nafter"),
        ("multiline-whitespace",     "a\n\n  b"),
    ]
    parent: str | None = None
    for _, text in cases:
        parent = _api_post_user_message(chat_id, text, parent)

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".bubble", timeout=5000)
    # One bubble per posted SubMessage; user-side bubbles only.
    page.wait_for_function(
        f"() => document.querySelectorAll('.bubble-row.user .bubble').length === {len(cases)}",
        timeout=5000,
    )
    shot(page, "30-bubble-aer-format")

    bubbles = page.eval_on_selector_all(
        ".bubble-row.user .bubble",
        "els => els.map(b => ({"
        "  html: b.innerHTML, text: b.textContent,"
        "  spans: Array.from(b.querySelectorAll('span')).map(s => ({"
        "    text: s.textContent,"
        "    classes: Array.from(s.classList).sort().join(' '),"
        "  })),"
        "  hasPre: b.querySelectorAll('pre').length,"
        "  hasBr: b.querySelectorAll('br').length,"
        "  hasP: b.querySelectorAll('p').length,"
        "}))",
    )
    by_case = dict(zip([c[0] for c in cases], bubbles))

    # *foo* — markers consumed, italic+subdued span over "foo".
    b = by_case["italic-line-start"]
    assert b["spans"] == [{"text": "foo", "classes": "aer-italic aer-subdued"}], b
    assert b["text"] == "foo"

    # foo*bar*baz — context gate rejects, asterisks stay literal, no spans.
    b = by_case["italic-mid-word"]
    assert b["spans"] == []
    assert b["text"] == "foo*bar*baz"

    # (*^_^) — emoticon guard: both * and _ stay literal.
    b = by_case["emoticon"]
    assert b["spans"] == []
    assert b["text"] == "(*^_^)"

    # ***bold-italic*** — one span carrying bold + italic + subdued.
    b = by_case["triple-bold-italic"]
    assert b["spans"] == [
        {"text": "bold-italic", "classes": "aer-bold aer-italic aer-subdued"}
    ], b
    assert b["text"] == "bold-italic"

    # CJK boundary — italic+subdued opens after は, closes before で.
    b = by_case["cjk-boundary"]
    assert b["spans"] == [
        {"text": "強調", "classes": "aer-italic aer-subdued"}
    ], b
    assert b["text"] == "これは強調です"

    # *Mixed, **bold and* italic** text. — three styled regions with the
    # crossed-pair dance: italic+subdued, italic+bold+subdued, bold, plain.
    b = by_case["crossed-pair"]
    assert b["spans"] == [
        {"text": "Mixed, ",  "classes": "aer-italic aer-subdued"},
        {"text": "bold and", "classes": "aer-bold aer-italic aer-subdued"},
        {"text": " italic",  "classes": "aer-bold"},
    ], b
    assert b["text"] == "Mixed, bold and italic text."

    # *hello — unclosed marker degrades to literal.
    b = by_case["unclosed"]
    assert b["spans"] == []
    assert b["text"] == "*hello"

    # Triple-backtick fence — <pre><code> with no language tag, no
    # syntax-highlighting spans inside, surrounding text intact.
    b = by_case["code-block"]
    assert b["hasPre"] == 1
    assert b["hasBr"] == 0 and b["hasP"] == 0
    code_idx = [c[0] for c in cases].index("code-block")
    pre = page.locator(".bubble-row.user").nth(code_idx).locator(".bubble pre")
    pre_info = pre.evaluate(
        "el => ({text: el.textContent, "
        "  codeClass: el.querySelector('code').className})",
    )
    assert pre_info["text"] == "x = 1"
    assert pre_info["codeClass"] == ""
    # before / after lines surround the fence as plain text.
    assert "before" in b["text"] and "after" in b["text"]

    # Multi-line whitespace — blank line and leading two spaces survive.
    b = by_case["multiline-whitespace"]
    assert b["text"] == "a\n\n  b", repr(b["text"])
    assert b["hasBr"] == 0 and b["hasP"] == 0


def test_aer_bubble_collapse_blank_lines_flag(page: Page, clean_state):
    """``renderBubble``'s ``collapseBlankLines`` flag folds a lone blank line
    (exactly ``\\n\\n``) to a single break — the model's habitual paragraph
    spacing renders as an empty line under ``.bubble``'s ``white-space:
    pre-wrap``. Runs of three-plus newlines are deliberate and kept; a code
    fence's own blank lines are never touched (the collapse only sees
    non-fence segments); and with the flag off (user-typed text) the blank
    line renders verbatim."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    r = page.evaluate(
        r"""async () => {
          const { renderBubble } = await import('/static/aer_render.js');
          const on  = (t) => renderBubble(t, { collapseBlankLines: true });
          const off = (t) => renderBubble(t);
          return {
            two:   on('a\n\nb'),
            one:   on('a\nb'),
            three: on('a\n\n\nb'),
            four:  on('a\n\n\n\nb'),
            fence: on('x\n\ny\n```\ncode\n\nmore\n```\nz\n\nw'),
            userVerbatim: off('a\n\nb'),
          };
        }"""
    )
    # Exactly-two newlines collapse to one; single newline unchanged.
    assert r["two"] == "a\nb", repr(r["two"])
    assert r["one"] == "a\nb", repr(r["one"])
    # Three-plus newlines are preserved (deliberate vertical space).
    assert r["three"] == "a\n\n\nb", repr(r["three"])
    assert r["four"] == "a\n\n\n\nb", repr(r["four"])
    # Prose around a fence collapses; the fenced block's own blank line is
    # left intact.
    assert "code\n\nmore" in r["fence"], repr(r["fence"])
    assert "x\ny" in r["fence"] and "z\nw" in r["fence"], repr(r["fence"])
    # Flag off (user-typed text) keeps the blank line verbatim.
    assert r["userVerbatim"] == "a\n\nb", repr(r["userVerbatim"])


def test_aer_contact_collapses_blank_line_user_verbatim(page: Page, clean_state):
    """End-to-end wiring: a contact-side AER bubble (origin ``aer``) collapses
    the model's ``\\n\\n`` paragraph spacing to a single line break, while a
    user-typed message (origin ``manual``) keeps its blank line verbatim —
    ``renderBubbleRow`` passes ``collapseBlankLines`` only for contact
    bubbles."""
    from urllib.request import Request

    _api_create_contact("CollapseAlice")
    user_id = _api_create_user("CollapseBob")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "CollapseAlice")
    chat_id = _api_post_chat(contact_id, user_id)

    # User message (origin manual), then a contact reply (origin aer). Both
    # carry a lone blank line between two paragraphs.
    u_id = _api_post_user_message(chat_id, "u-one\n\nu-two")
    body = json.dumps({
        "parent_id": u_id, "sender": "contact",
        "body": [{"text": "c-one\n\nc-two", "emotion": "neutral"}],
    }).encode()
    req = Request(
        f"{base_url()}/api/chats/{chat_id}/messages", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    urlopen(req, timeout=3).read()

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector("#chat-list-body .list-row", timeout=5000)
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".bubble-row.contact .bubble", timeout=5000)

    contact_text = page.locator(".bubble-row.contact .bubble").first.text_content()
    user_text = page.locator(".bubble-row.user .bubble").first.text_content()
    # Contact bubble: blank line collapsed to a single break.
    assert contact_text == "c-one\nc-two", repr(contact_text)
    # User bubble: blank line preserved verbatim.
    assert user_text == "u-one\n\nu-two", repr(user_text)


def test_strip_for_tts_drops_html_comments(page: Page, clean_state):
    """``stripForTTS`` removes HTML comments so hidden metadata / a model's
    "chain-of-thought at home" (which renders invisibly in the bubble) is
    never spoken. Surrounding prose survives; a comment's own markdown-ish
    characters can't perturb the fence / emphasis passes because comments are
    stripped first. Shared by every TTS path (AER autoplay, Generic autoplay,
    manual speaker button)."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    r = page.evaluate(
        r"""async () => {
          const { stripForTTS: f } = await import('/static/tts_helpers.js');
          return {
            inline: f('Before <!-- secret --> after.'),
            multiline: f('a\n<!-- line1\nline2 -->\nb'),
            onlyComment: f('<!-- nothing to say -->'),
            withMarkers: f('<!-- has **stars** and `ticks` --> real **bold**'),
          };
        }"""
    )
    assert r["inline"] == "Before after.", repr(r["inline"])
    assert "secret" not in r["inline"]
    assert r["multiline"] == "a b", repr(r["multiline"])
    assert r["onlyComment"] == "", repr(r["onlyComment"])
    # The comment (with its ``**``/`` ` `` chars) is gone; the real ``**bold**``
    # outside it is still un-marked to plain "bold".
    assert r["withMarkers"] == "real bold", repr(r["withMarkers"])


# ---------------------------------------------------------------------------
# Scenario background image: uploading populates the list-row avatar slot,
# the editor grid switches the active theme on click, and a chat with the
# scenario has the .chat-bg layer wired with the expected CSS vars.
# ---------------------------------------------------------------------------


def _api_create_scenario(name: str) -> str:
    from urllib.request import Request

    body = json.dumps({"name": name}).encode()
    req = Request(
        f"{base_url()}/api/scenarios",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=3) as r:
        return json.load(r)["id"]


def _tiny_png(fill: tuple[int, int, int] = (60, 120, 200)) -> bytes:
    import io as _io
    from PIL import Image as _Image

    im = _Image.new("RGB", (200, 120), fill)
    buf = _io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_scenario_background_upload_populates_list_avatar(page: Page, clean_state):
    sid = _api_create_scenario("WithBg")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="scenarios"]')
    page.wait_for_selector("#scenario-list-body .list-row")

    page.click('#scenario-list-body .list-row:has-text("WithBg")')
    page.wait_for_selector("h3:has-text('Background image')", timeout=5000)

    # The empty-state file input is hidden but addressable by id.
    page.set_input_files(
        f"#bg-input-empty-{sid}",
        files=[{"name": "bg.png", "mimeType": "image/png", "buffer": _tiny_png()}],
    )

    # Editor flips into "with image" state — focal picker shows up; sliders
    # appear; list-row avatar gets an <img>.
    page.wait_for_selector(".focal-picker", timeout=5000)
    page.wait_for_selector(
        f'#scenario-list-body .list-row:has-text("WithBg") .avatar img',
        timeout=5000,
    )
    shot(page, "scenario-bg-upload")


def test_scenario_background_theme_grid_switches_active_theme(page: Page, clean_state):
    sid = _api_create_scenario("ThemeSwitch")

    # Make sure we start on dark; the theme picker test may have left us
    # on something else.
    from urllib.request import Request

    urlopen(
        Request(
            f"{base_url()}/api/settings",
            data=json.dumps({"theme": "dark"}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        ),
        timeout=3,
    ).read()

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="scenarios"]')
    page.wait_for_selector("#scenario-list-body .list-row")
    page.click('#scenario-list-body .list-row:has-text("ThemeSwitch")')
    page.wait_for_selector("h3:has-text('Background image')", timeout=5000)

    page.set_input_files(
        f"#bg-input-empty-{sid}",
        files=[{"name": "bg.png", "mimeType": "image/png", "buffer": _tiny_png()}],
    )
    page.wait_for_selector(".bg-theme-grid", timeout=5000)

    # Click the "noir" cell — the active theme on <html> must flip.
    page.click('.bg-theme-cell[data-theme="noir"]')
    page.wait_for_function("document.documentElement.dataset.theme === 'noir'")

    # And the server persisted the choice.
    with urlopen(f"{base_url()}/api/settings") as r:
        assert json.load(r)["theme"] == "noir"


def test_chat_pane_renders_scenario_background_vars(page: Page, clean_state):
    """Open a chat that uses a scenario with a background — the messages
    wrap must carry data-has-bg + the CSS custom properties so the bg layer
    paints correctly."""
    from urllib.request import Request

    _api_create_contact("BgContact")
    user_id = _api_create_user("BgUser")
    sid = _api_create_scenario("BgScenario")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="scenarios"]')
    page.wait_for_selector(f'#scenario-list-body .list-row:has-text("BgScenario")')
    page.click(f'#scenario-list-body .list-row:has-text("BgScenario")')
    page.wait_for_selector("h3:has-text('Background image')")
    page.set_input_files(
        f"#bg-input-empty-{sid}",
        files=[{"name": "bg.png", "mimeType": "image/png", "buffer": _tiny_png((250, 80, 80))}],
    )
    page.wait_for_selector(".focal-picker", timeout=5000)

    # Create a chat using this contact + user + scenario via the API.
    with urlopen(f"{base_url()}/api/contacts") as r:
        contacts = json.load(r)
    contact_id = next(c["id"] for c in contacts if c["name"] == "BgContact")
    chat_body = json.dumps({
        "contact_id": contact_id,
        "user_id": user_id,
        "scenario_id": sid,
    }).encode()
    req = Request(
        f"{base_url()}/api/chats",
        data=chat_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=3) as r:
        chat = json.load(r)

    # Reload the SPA so the freshly-created chat lands in ``state.chats``,
    # then open it via the list.
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector('#chat-list-body .list-row', timeout=5000)
    page.click('#chat-list-body .list-row')
    page.wait_for_selector("#chat-messages-wrap", timeout=5000)
    # ``data-has-bg`` is set only after ``state.activeChatScenario`` is
    # populated by the async fetch — without this wait the snapshot below
    # races the hydration and intermittently reads ``null``.
    page.wait_for_function(
        "() => document.querySelector('#chat-messages-wrap')?.dataset.hasBg === 'true'",
        timeout=5000,
    )

    # Assert wrap has data-has-bg + correct mode + an --bg-image CSS var.
    info = page.eval_on_selector(
        "#chat-messages-wrap",
        """el => ({
            hasBg: el.dataset.hasBg,
            mode: el.dataset.mode,
            bgImage: el.style.getPropertyValue('--bg-image'),
            blur: el.style.getPropertyValue('--bg-blur'),
        })""",
    )
    assert info["hasBg"] == "true"
    assert info["mode"] == "cover"
    assert "/api/files/scenarios/" in info["bgImage"]
    assert info["bgImage"].endswith(')') or "url(" in info["bgImage"]
    shot(page, "scenario-bg-on-chat")


# ---------------------------------------------------------------------------
# Message-list virtualization: only ~90 messages live in the DOM at once;
# scrolling slides the window. Prefix / suffix are reserved by spacer divs
# so the scrollbar geometry stays correct.
# ---------------------------------------------------------------------------


def _api_seed_message_chain(chat_id: str, count: int, contact_name: str, user_name: str) -> list[str]:
    """Post ``count`` alternating user / contact messages on a chain. Returns
    the list of message ids in path order."""
    from urllib.request import Request

    ids: list[str] = []
    parent: str | None = None
    for i in range(count):
        sender = "user" if i % 2 == 0 else "contact"
        body_item = {"text": f"Msg {i:04d}"}
        if sender == "contact":
            body_item["emotion"] = "neutral"
        payload = {
            "sender": sender,
            "sender_name": user_name if sender == "user" else contact_name,
            "body": [body_item],
        }
        if parent is not None:
            payload["parent_id"] = parent
        body = json.dumps(payload).encode()
        msg = json.loads(urlopen(Request(
            f"{base_url()}/api/chats/{chat_id}/messages", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=3).read())
        ids.append(msg["id"])
        parent = msg["id"]
    return ids


def test_mobile_chat_header_collapses_and_expands(browser, clean_state):
    """The mobile chat-header starts with the title visible and the
    action row collapsed behind a ``…`` toggle. Tapping the toggle
    swaps the title for the controls; tapping again restores it."""
    _api_create_contact("HdrChar")
    user_id = _api_create_user("HdrUser")
    contact_id = next(
        c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "HdrChar"
    )
    _api_post_chat(contact_id, user_id)

    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True, has_touch=True, device_scale_factor=2.0,
    )
    pg = ctx.new_page()
    try:
        pg.goto(base_url())
        pg.wait_for_selector("#rail")
        pg.click('.rail-btn[data-tab="chats"]')
        pg.locator("#chat-list-body .list-row").first.click()
        pg.wait_for_selector(".chat-header", timeout=5000)

        def visibility():
            return pg.evaluate("""() => {
              const hdr = document.querySelector('.chat-header');
              const vis = (sel) => {
                const e = hdr.querySelector(sel);
                return !!e && getComputedStyle(e).display !== 'none';
              };
              return {
                title: vis('.title'),
                toggle: vis('.header-more-toggle'),
                controls: vis('.controls'),
                expanded: hdr.classList.contains('controls-expanded'),
              };
            }""")

        # Initial: collapsed.
        assert visibility() == {
            "title": True, "toggle": True, "controls": False, "expanded": False,
        }, visibility()

        # Tap the toggle → expanded.
        pg.click(".chat-header .header-more-toggle")
        pg.wait_for_timeout(80)
        assert visibility() == {
            "title": False, "toggle": True, "controls": True, "expanded": True,
        }, visibility()

        # Tap again → collapsed.
        pg.click(".chat-header .header-more-toggle")
        pg.wait_for_timeout(80)
        assert visibility() == {
            "title": True, "toggle": True, "controls": False, "expanded": False,
        }, visibility()
    finally:
        ctx.close()


def test_search_button_in_chat_header_opens_search_pill(page: Page, clean_state):
    """The chat-header search button opens the same search surface
    Ctrl+F does — necessary on phones (no physical keyboard) but also
    a quick mouse-driven entry point on desktop."""
    _api_create_contact("SearchBtnChar")
    user_id = _api_create_user("SearchBtnUser")
    contact_id = next(
        c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "SearchBtnChar"
    )
    _api_post_chat(contact_id, user_id)

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-header", timeout=5000)

    assert page.locator(".chat-search-pill").count() == 0
    page.click(".chat-header button[title='Search messages (Ctrl+F)']")
    page.wait_for_selector(".chat-search-pill", timeout=2000)
    # Input should be auto-focused.
    focused = page.evaluate(
        "() => document.activeElement === document.querySelector('.chat-search-pill .search-input')"
    )
    assert focused


def test_chat_stats_counter_renders_msg_count(page: Page, clean_state):
    """The chat-stats span shows ``ctx: N · msgs: M`` when no rollover
    happened, and ``ctx: N · msgs: in/total`` once the path has a
    trimmed prefix. Driven by the four ``state.context*`` fields the
    chat view subscribes to."""
    _api_create_contact("CtxChar")
    user_id = _api_create_user("CtxUser")
    contact_id = next(
        c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "CtxChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-stats", timeout=5000)
    # Test fixture skips the GLM tokenizer, so the live API can't fill in
    # counts — drive the subscriber from the browser side instead.
    state_mod = page.evaluate("""async () => {
      const m = await import('/static/state.js');
      return Object.keys(m);
    }""")
    assert "setState" in state_mod, state_mod

    # No rollover → ``msgs: total``.
    page.evaluate("""async () => {
      const { setState } = await import('/static/state.js');
      setState({
        contextTokens: 1234,
        contextMsgsIn: 8,
        contextMsgsTotal: 8,
        contextOldestId: 'first-msg-id',
      });
    }""")
    page.wait_for_timeout(80)
    text = page.locator(".chat-stats").text_content()
    assert text == "ctx: 1,234 · msgs: 8", text

    # Rollover → ``msgs: in/total``.
    page.evaluate("""async () => {
      const { setState } = await import('/static/state.js');
      setState({
        contextTokens: 9876,
        contextMsgsIn: 12,
        contextMsgsTotal: 30,
        contextOldestId: 'cursor-msg-id',
      });
    }""")
    page.wait_for_timeout(80)
    text = page.locator(".chat-stats").text_content()
    assert text == "ctx: 9,876 · msgs: 12/30", text

    # Cleared → ``ctx: ?``.
    page.evaluate("""async () => {
      const { setState } = await import('/static/state.js');
      setState({
        contextTokens: null,
        contextMsgsIn: null,
        contextMsgsTotal: null,
        contextOldestId: null,
      });
    }""")
    page.wait_for_timeout(80)
    assert page.locator(".chat-stats").text_content() == "ctx: ?"


def test_truncation_divider_anchors_above_oldest_in_context(page: Page, clean_state):
    """The dashed divider appears immediately before the message whose id
    matches ``contextOldestId``, and only when the path actually has
    trimmed messages (``messages_in_context < messages_total``)."""
    from urllib.request import Request

    _api_create_contact("DivChar")
    user_id = _api_create_user("DivUser")
    contact_id = next(
        c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "DivChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    ids = _api_seed_message_chain(chat_id, 8, "DivChar", "DivUser")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=5000)
    # No divider before any state is set.
    assert page.locator(".context-divider").count() == 0

    # Anchor on msg index 3 (a contact message) — divider should sit
    # immediately before that .msg.
    target_id = ids[3]
    page.evaluate(f"""async () => {{
      const {{ setState }} = await import('/static/state.js');
      setState({{
        contextTokens: 1000,
        contextMsgsIn: 5,
        contextMsgsTotal: 8,
        contextOldestId: {target_id!r},
      }});
    }}""")
    page.wait_for_timeout(80)
    sibling_id = page.evaluate(f"""() => {{
      const div = document.querySelector('.context-divider');
      if (!div) return null;
      const next = div.nextElementSibling;
      return next ? next.getAttribute('data-msg-id') : null;
    }}""")
    assert sibling_id == target_id, sibling_id

    # Move the boundary to msg 5 — divider follows.
    new_target = ids[5]
    page.evaluate(f"""async () => {{
      const {{ setState }} = await import('/static/state.js');
      setState({{ contextMsgsIn: 3, contextOldestId: {new_target!r} }});
    }}""")
    page.wait_for_timeout(80)
    sibling_id = page.evaluate("""() => {
      const div = document.querySelector('.context-divider');
      return div?.nextElementSibling?.getAttribute('data-msg-id') ?? null;
    }""")
    assert sibling_id == new_target

    # No rollover (in == total) → divider should disappear.
    page.evaluate("""async () => {
      const { setState } = await import('/static/state.js');
      setState({ contextMsgsIn: 8, contextMsgsTotal: 8 });
    }""")
    page.wait_for_timeout(80)
    assert page.locator(".context-divider").count() == 0


def test_chat_stats_updates_on_branch_swap(page: Page, clean_state):
    """Switching the active branch via the ``select`` API and reloading
    the active chat re-fetches context tokens. The chat-stats counter
    + divider update to reflect the new branch's path. We mock the
    second fetch's response by stubbing ``state`` directly so this test
    works without the tokenizer."""
    from urllib.request import Request

    _api_create_contact("SwapChar")
    user_id = _api_create_user("SwapUser")
    contact_id = next(
        c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "SwapChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    main = _api_seed_message_chain(chat_id, 6, "SwapChar", "SwapUser")
    # Add a sibling at the root and switch the active branch.
    sib = json.loads(urlopen(Request(
        f"{base_url()}/api/chats/{chat_id}/messages",
        data=json.dumps({
            "sender": "contact",
            "sender_name": "SwapChar",
            "body": [{"text": "Alt branch", "emotion": "neutral"}],
        }).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    ), timeout=3).read())
    sib_id = sib["id"]
    # Restore main as the active path before opening (POST sets sib as the
    # selected child; we want to start on main).
    urlopen(Request(
        f"{base_url()}/api/chats/{chat_id}/select",
        data=json.dumps({"parent_id": None, "child_id": main[0]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    ), timeout=3).read()

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=5000)
    # Pretend the API returned 6 in-context messages (full main path).
    page.evaluate("""async () => {
      const { setState } = await import('/static/state.js');
      setState({
        contextTokens: 500, contextMsgsIn: 6, contextMsgsTotal: 6,
        contextOldestId: null,
      });
    }""")
    page.wait_for_timeout(80)
    assert page.locator(".chat-stats").text_content() == "ctx: 500 · msgs: 6"

    # Swap the active branch to the 1-message alt root.
    urlopen(Request(
        f"{base_url()}/api/chats/{chat_id}/select",
        data=json.dumps({"parent_id": None, "child_id": sib_id}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    ), timeout=3).read()
    page.evaluate("""async () => {
      const { loadActiveChat } = await import('/static/views/chat.js');
      await loadActiveChat(window._aetherChatId);
    }""", )
    # ``loadActiveChat`` requires the chat id; expose it for the eval.
    page.evaluate(f"window._aetherChatId = {chat_id!r};")
    page.evaluate("""async () => {
      const { loadActiveChat } = await import('/static/views/chat.js');
      await loadActiveChat(window._aetherChatId);
    }""")
    page.wait_for_timeout(150)
    # After the path swap, only the sibling is on the active path.
    msg_count = page.evaluate(
        "() => document.querySelectorAll('#chat-messages > .msg').length"
    )
    assert msg_count == 1, msg_count
    # Stub the "fresh" counts for the alt branch and verify the counter
    # shows the new total.
    page.evaluate("""async () => {
      const { setState } = await import('/static/state.js');
      setState({
        contextTokens: 90, contextMsgsIn: 1, contextMsgsTotal: 1,
        contextOldestId: null,
      });
    }""")
    page.wait_for_timeout(80)
    assert page.locator(".chat-stats").text_content() == "ctx: 90 · msgs: 1"


def test_picker_type_to_find_on_closed_trigger(page: Page, clean_state):
    """Native ``<select>`` lets you type letters while focused-and-closed
    to jump to a matching option. The custom dropdown must mirror that:
    focus a contact's Default Intimacy picker, type ``ac``, and the
    value should advance to ``Acquaintance`` without the picker even
    opening."""
    _api_create_contact("PickerChar")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").first.click()
    page.wait_for_selector("#contact-detail .avatar-picker", timeout=5000)

    intimacy = page.locator("#contact-detail").locator(
        ".form-group", has_text="Default intimacy"
    ).locator(".avatar-picker-trigger")
    intimacy.focus()
    page.wait_for_timeout(50)
    intimacy.press("a")
    intimacy.press("c")
    page.wait_for_timeout(80)

    label = intimacy.locator(".avatar-picker-label").text_content()
    assert label == "Acquaintance", label
    # Picker should NOT have opened (no popover in DOM).
    assert page.locator(".avatar-picker-popover").count() == 0


def test_modal_focus_trap_cycles_inside_modal(page: Page, clean_state):
    """Tabbing through a modal cycles focus among the modal's focusable
    elements — it must not leak into the chat list or rail behind."""
    _api_create_contact("ModalChar")
    user_id = _api_create_user("ModalUser")
    contact_id = next(
        c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "ModalChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-view", timeout=5000)
    # Open chat info modal.
    page.locator("button[title='Chat info']").click()
    page.wait_for_selector(".modal h3:has-text('Chat info')", timeout=3000)

    # Repeatedly Tab and verify focus stays inside the modal.
    for _ in range(15):
        page.keyboard.press("Tab")
        page.wait_for_timeout(20)
        in_modal = page.evaluate(
            "() => !!document.activeElement && "
            "!!document.activeElement.closest('#modal-root .modal')"
        )
        assert in_modal, "Tab leaked focus outside the modal"


def test_reactive_brain_badge_updates_on_add(page: Page, clean_state):
    """Adding a brain in the contact edit view bumps the ``Brains (N)``
    badge in the section header without re-rendering the section. The
    ``liveBadge`` helper hooks into the editor's onChange so the count
    stays current as the user mutates the underlying array."""
    _api_create_contact("BrainCounter")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").first.click()
    page.wait_for_selector("#contact-detail h3:has-text('Brains')", timeout=5000)

    brains_section = page.locator("#contact-detail .section").filter(
        has=page.locator("h3:has-text('Brains')")
    )
    badge = brains_section.locator("h3 .badge")
    assert badge.text_content() == "0"

    brains_section.locator("button:has-text('+ Add brain')").click()
    page.wait_for_timeout(150)
    assert badge.text_content() == "1"

    brains_section.locator("button:has-text('+ Add brain')").click()
    page.wait_for_timeout(150)
    assert badge.text_content() == "2"


def test_brain_activation_round_trips_through_reload(page: Page, clean_state):
    """Configure a brain's activation keys + flags + advanced condition,
    let the autosaver flush, reload the page, and verify everything sticks."""
    _api_create_contact("BrainActivation")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").first.click()
    page.wait_for_selector("#contact-detail h3:has-text('Brains')", timeout=5000)

    brains_section = page.locator("#contact-detail .section").filter(
        has=page.locator("h3:has-text('Brains')")
    )
    brains_section.locator("button:has-text('+ Add brain')").click()
    page.wait_for_timeout(100)

    row = brains_section.locator(".brain-row").first
    row.locator("input[type='text']").first.fill("Lore")
    row.locator("textarea").first.fill("Body of the lore entry.")

    row.locator("details.brain-activation > summary").click()
    page.wait_for_timeout(50)
    row.locator("button:has-text('+ Add key')").first.click()
    page.wait_for_timeout(50)
    row.locator(".brain-key-row input[type='text']").first.fill("dragon")
    # Toggle regex on.
    row.locator(".brain-key-row button.toggle").first.click()
    # Cascading on.
    row.locator(".brain-flag input[type='checkbox']").first.check()

    # Advanced: + Add condition → switch to numeric_compare via the styled
    # picker (custom avatar-picker — popover portalled to ``document.body``).
    row.locator("details.brain-advanced > summary").click()
    page.wait_for_timeout(50)
    row.locator(".brain-advanced-body button:has-text('+ Add condition')").first.click()
    page.wait_for_timeout(50)
    row.locator(".cond-node .avatar-picker-trigger").first.click()
    page.wait_for_selector(".avatar-picker-option:has-text('Numeric comparison')")
    page.locator(".avatar-picker-option:has-text('Numeric comparison')").click()
    page.wait_for_timeout(150)

    # Give the autosaver (400 ms debounce) time to flush.
    page.wait_for_timeout(700)

    # Reload + verify persistence.
    page.reload()
    page.wait_for_selector("#contact-detail h3:has-text('Brains')", timeout=5000)
    row = page.locator("#contact-detail .brain-row").first
    # Open both collapsibles (they default-open only when activation is
    # already configured — which it is post-reload, but be explicit).
    if row.locator("details.brain-activation").get_attribute("open") is None:
        row.locator("details.brain-activation > summary").click()
    if row.locator("details.brain-advanced").get_attribute("open") is None:
        row.locator("details.brain-advanced > summary").click()
    page.wait_for_timeout(100)

    assert row.locator(".brain-key-row input[type='text']").first.input_value() == "dragon"
    regex_btn_classes = row.locator(".brain-key-row button.toggle").first.get_attribute("class")
    assert "on" in regex_btn_classes
    assert row.locator(".brain-flag input[type='checkbox']").first.is_checked()
    # The styled picker trigger shows the selected option's label.
    trigger_label = row.locator(".cond-node .avatar-picker-label").first.text_content()
    assert trigger_label == "Numeric comparison"


def test_brain_remove_confirm_stacks_over_per_message_modal(page: Page, clean_state):
    """Removing a brain inside the per-message brain editor opens a confirm
    on top of it (``opts.stack = true``). The outer modal stays in the DOM,
    the confirm sits above it, and the underlying gets ``.modal-behind`` so
    it's visibly dimmed."""
    _api_create_contact("StackContact")
    user_id = _api_create_user("StackUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    contact_id = next(c["id"] for c in contacts if c["name"] == "StackContact")
    chat_id = _api_post_chat(contact_id, user_id)
    _api_post_user_message(chat_id, "hello")

    page.goto(f"{base_url()}/chats/{chat_id[:8]}")
    page.wait_for_selector(".msg", timeout=5000)
    page.locator(".msg").first.hover()
    page.wait_for_timeout(100)
    page.locator(".msg button.icon-btn[title='Brains']").first.click()
    page.wait_for_selector(".modal:has-text('Local brains')", timeout=3000)
    page.locator(".modal button:has-text('+ Add brain')").first.click()
    page.wait_for_timeout(100)
    page.locator(".modal .brain-row input[type='text']").first.fill("DoomedLocal")
    page.locator(".modal .brain-row button.brain-row-del").first.click()
    page.wait_for_selector(".modal:has-text('Remove DoomedLocal?')", timeout=2000)

    modals = page.locator("#modal-root > .modal")
    assert modals.count() == 2, "expected the outer + confirm to coexist"
    # The first DOM child is the outer (now ``.modal-behind``); the confirm
    # appended on top has plain ``.modal``.
    outer_classes = modals.nth(0).get_attribute("class")
    assert "modal-behind" in outer_classes, f"outer should be dimmed: {outer_classes!r}"
    top_classes = modals.nth(1).get_attribute("class")
    assert "modal-behind" not in top_classes
    # Cancelling the confirm leaves the outer modal intact (and active).
    page.locator(".modal:has-text('Remove DoomedLocal?') button:has-text('Cancel')").click()
    page.wait_for_timeout(150)
    assert page.locator("#modal-root > .modal").count() == 1
    assert page.locator(".modal:has-text('Local brains')").count() == 1


def test_per_message_save_warns_when_brain_is_incomplete(page: Page, clean_state):
    """Saving the per-message brain editor with a name-only (or content-only)
    brain pops a confirm asking if the user really wants to discard it,
    instead of silently dropping a half-typed paragraph."""
    _api_create_contact("SaveGuardContact")
    user_id = _api_create_user("SaveGuardUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    contact_id = next(c["id"] for c in contacts if c["name"] == "SaveGuardContact")
    chat_id = _api_post_chat(contact_id, user_id)
    _api_post_user_message(chat_id, "hi")

    page.goto(f"{base_url()}/chats/{chat_id[:8]}")
    page.wait_for_selector(".msg", timeout=5000)
    page.locator(".msg").first.hover()
    page.wait_for_timeout(100)
    page.locator(".msg button.icon-btn[title='Brains']").first.click()
    page.wait_for_selector(".modal:has-text('Local brains')", timeout=3000)
    page.locator(".modal button:has-text('+ Add brain')").first.click()
    page.wait_for_timeout(100)
    # Type only the content — name stays empty.
    page.locator(".modal .brain-row textarea").first.fill("A paragraph the user would hate to lose.")
    page.locator(".modal button:has-text('Save')").first.click()
    # The save handler should pop a stacked confirm.
    page.wait_for_selector(".modal:has-text('Discard 1 incomplete brain?')", timeout=2000)
    # Bailing keeps the editor open.
    page.locator(".modal:has-text('Discard 1 incomplete brain?') button:has-text('Cancel')").click()
    page.wait_for_timeout(150)
    assert page.locator(".modal:has-text('Local brains')").count() == 1
    # The paragraph the user typed is still in the textarea.
    assert "hate to lose" in page.locator(".modal .brain-row textarea").first.input_value()


def test_brain_active_picker_refreshes_options_on_focus(page: Page, clean_state):
    """The brain_active dropdown re-fetches its catalog when the trigger is
    clicked, so a sibling brain renamed mid-session shows up live — no
    full re-render or page reload required."""
    _api_create_contact("CatalogContact")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").first.click()
    page.wait_for_selector("#contact-detail h3:has-text('Brains')", timeout=5000)
    brains_section = page.locator("#contact-detail .section").filter(
        has=page.locator("h3:has-text('Brains')")
    )
    add_btn = brains_section.locator("button:has-text('+ Add brain')").first
    add_btn.click()
    page.wait_for_timeout(100)
    add_btn.click()
    page.wait_for_timeout(100)
    rows = brains_section.locator(".brain-row")
    rows.nth(0).locator("input[type='text']").first.fill("FocusA")
    rows.nth(1).locator("input[type='text']").first.fill("FocusB")
    page.wait_for_timeout(500)  # autosave

    # On FocusA, open Activation + Advanced, add a brain_active condition.
    rows.nth(0).locator("details.brain-activation > summary").click()
    page.wait_for_timeout(50)
    rows.nth(0).locator("details.brain-advanced > summary").click()
    page.wait_for_timeout(50)
    rows.nth(0).locator(".brain-advanced-body button:has-text('+ Add condition')").first.click()
    page.wait_for_timeout(80)
    # Type-picker: open trigger, pick "Brain entry active".
    rows.nth(0).locator(".cond-node .avatar-picker-trigger").first.click()
    page.wait_for_selector(".avatar-picker-option:has-text('Brain entry active')")
    page.locator(".avatar-picker-option:has-text('Brain entry active')").click()
    page.wait_for_timeout(150)

    # Sanity: catalog has FocusB (filtered to exclude self). The brain_active
    # picker lives inside ``.cond-body`` (the type picker is in ``.cond-head``).
    #
    # We open via ``element.click()`` rather than Playwright's mouse-driven
    # ``click()`` to dodge a real-mouse-only race: the focus event triggers
    # an auto-scroll, the avatar-picker registers a scroll listener inside
    # ``open()``, and that scroll listener fires the very next tick and
    # closes the popover. JS-driven .click() skips the focus/scroll path
    # entirely.
    def open_picker(trigger_locator):
        trigger_locator.evaluate("el => el.click()")
        page.wait_for_selector(".avatar-picker-popover:not(.hidden)", timeout=2000)

    ba_trigger = rows.nth(0).locator(".cond-node .cond-body .avatar-picker-trigger").first
    open_picker(ba_trigger)
    labels = page.locator(".avatar-picker-popover:not(.hidden) .avatar-picker-option-label").all_text_contents()
    assert any("FocusB" in l for l in labels), f"expected FocusB in catalog, got {labels}"
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)

    # Rename FocusB → FocusZ (the editor mutates draft + autosaves; no
    # rerender of FocusA's row, so the dropdown options live in stale memory).
    rows.nth(1).locator("input[type='text']").first.fill("FocusZ")
    page.wait_for_timeout(500)

    # Re-open the brain_active picker — the capture-phase click handler
    # refreshes ``setOptions`` from the live catalog, so FocusZ appears.
    open_picker(ba_trigger)
    labels2 = page.locator(".avatar-picker-popover:not(.hidden) .avatar-picker-option-label").all_text_contents()
    assert any("FocusZ" in l for l in labels2), f"expected FocusZ after rename, got {labels2}"
    assert not any("FocusB" in l for l in labels2)


def test_chat_info_uuid_truncates_with_ellipsis(page: Page, clean_state):
    """UUIDs in the chat info modal render as the first 8 chars + ``…``;
    the full id stays in the ``title`` tooltip so the user can still see
    or copy it."""
    _api_create_contact("UuidChar")
    user_id = _api_create_user("UuidUser")
    contact_id = next(
        c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "UuidChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-view", timeout=5000)
    page.locator("button[title='Chat info']").click()
    page.wait_for_selector(".modal h3:has-text('Chat info')", timeout=3000)

    # The Chat info-row is read-only and shows a uuidEl with truncation.
    chat_uuid_el = page.locator(".chat-info .info-row").filter(
        has_text="Chat"
    ).locator(".info-uuid").first
    rendered = chat_uuid_el.text_content().strip()
    full = chat_uuid_el.get_attribute("title")
    assert full == chat_id, (full, chat_id)
    assert rendered == f"{chat_id[:8]}…", (rendered, chat_id[:8] + "…")


def test_virtualization_long_chat_lands_at_bottom_mobile(browser, clean_state):
    """Opening a long virtualized chat on a phone-sized viewport must
    land the user at the BOTTOM. The ``_virtFirstRefreshAfterSwitch``
    flag snaps instead of smooth-scrolling on first load so the top
    spacer's IO threshold can't fire a slide mid-animation."""
    _api_create_contact("MobileChar")
    user_id = _api_create_user("MobileUser")
    contact_id = next(
        c["id"]
        for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "MobileChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    _api_seed_message_chain(chat_id, 200, "MobileChar", "MobileUser")

    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2.0,
        is_mobile=True,
        has_touch=True,
    )
    pg = ctx.new_page()
    try:
        pg.goto(base_url())
        pg.wait_for_selector("#rail", timeout=5000)
        pg.click('.rail-btn[data-tab="chats"]')
        pg.locator("#chat-list-body .list-row").first.click()
        pg.wait_for_selector(".chat-messages .msg", timeout=8000)
        # Allow time for the rAF re-affirm + any IO callbacks. If the bug
        # were back, an IO-driven slide would happen during this window.
        pg.wait_for_timeout(500)

        snapshot = pg.evaluate("""() => {
          const root = document.getElementById('chat-messages');
          return {
            scrollTop: root.scrollTop,
            scrollHeight: root.scrollHeight,
            clientHeight: root.clientHeight,
            atBottom: root.scrollTop + root.clientHeight >= root.scrollHeight - 20,
            windowStart: (root.querySelector('.virt-spacer-top')?.offsetHeight) || 0,
            bottomSpacer: (root.querySelector('.virt-spacer-bottom')?.offsetHeight) || 0,
          };
        }""")
        # Bottom spacer at zero confirms the window is anchored at the tail.
        # ``atBottom`` confirms the scroll position is at the visual bottom.
        assert snapshot["bottomSpacer"] == 0, snapshot
        assert snapshot["atBottom"], snapshot
    finally:
        ctx.close()


def test_virtualization_long_chat_loads_only_window(page: Page, clean_state):
    """Opening a 200-message chat keeps roughly ``WINDOW_TARGET`` (90)
    messages in the DOM, with a top spacer accounting for the unloaded
    prefix and the user landed at the bottom (latest message visible)."""
    _api_create_contact("VirtChar")
    user_id = _api_create_user("VirtUser")
    contact_id = next(
        c["id"]
        for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "VirtChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    _api_seed_message_chain(chat_id, 200, "VirtChar", "VirtUser")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=8000)
    # Wait an extra beat for spacer height measurement (rAF inside refresh).
    page.wait_for_timeout(150)

    snapshot = page.evaluate("""() => {
      const root = document.getElementById('chat-messages');
      const msgs = root.querySelectorAll(':scope > .msg');
      const top = root.querySelector('.virt-spacer-top');
      const bot = root.querySelector('.virt-spacer-bottom');
      return {
        msgCount: msgs.length,
        topSpacerHeight: top ? top.offsetHeight : 0,
        bottomSpacerHeight: bot ? bot.offsetHeight : 0,
        atBottom: root.scrollTop + root.clientHeight >= root.scrollHeight - 20,
      };
    }""")
    # Should be exactly the WINDOW_TARGET (90) on a 200-msg chat.
    assert snapshot["msgCount"] == 90, snapshot
    assert snapshot["topSpacerHeight"] > 0, snapshot
    assert snapshot["bottomSpacerHeight"] == 0, snapshot
    assert snapshot["atBottom"], snapshot


def test_virtualization_scroll_up_slides_window(page: Page, clean_state):
    """Scrolling to the top of the loaded window triggers an
    IntersectionObserver slide that brings earlier messages into the DOM."""
    _api_create_contact("VirtCharScroll")
    user_id = _api_create_user("VirtUserScroll")
    contact_id = next(
        c["id"]
        for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "VirtCharScroll"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    _api_seed_message_chain(chat_id, 200, "VirtCharScroll", "VirtUserScroll")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=8000)
    page.wait_for_timeout(150)

    first_before = page.evaluate(
        "() => document.querySelector('#chat-messages > .msg')?.dataset.msgId"
    )
    # Scroll the chat-messages pane to the top — the IntersectionObserver
    # picks up the spacer crossing the trigger margin and slides up.
    page.evaluate("document.getElementById('chat-messages').scrollTop = 0")
    # Throttle is 80 ms; allow for slide + repaint.
    page.wait_for_timeout(250)

    first_after = page.evaluate(
        "() => document.querySelector('#chat-messages > .msg')?.dataset.msgId"
    )
    assert first_after and first_after != first_before, (first_before, first_after)


def test_virtualization_fast_scroll_does_not_strand(page: Page, clean_state):
    """A single deep scrollTop jump (scrollbar drag, fast mousewheel)
    into the top spacer must land the user inside the rendered window
    — not stuck mid-spacer. Centring the window on the actual scroll
    position handles the jump in one shot and lets the IO state cleanly
    transition back to non-intersecting."""
    _api_create_contact("VirtFastChar")
    user_id = _api_create_user("VirtFastUser")
    contact_id = next(
        c["id"]
        for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "VirtFastChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    _api_seed_message_chain(chat_id, 200, "VirtFastChar", "VirtFastUser")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=8000)
    page.wait_for_timeout(150)

    # Confirm the window starts tail-anchored (top spacer present).
    initial = page.evaluate("""() => {
        const top = document.querySelector('.virt-spacer-top');
        return top ? top.offsetHeight : 0;
    }""")
    assert initial > 0, "setup: top spacer must exist for tail-anchored window"

    # Jump scrollTop deep into the top spacer in a single shot.
    page.evaluate("document.getElementById('chat-messages').scrollTop = 200")
    page.wait_for_timeout(300)

    snapshot = page.evaluate("""() => {
        const root = document.getElementById('chat-messages');
        const top = document.querySelector('.virt-spacer-top');
        return {
            scrollTop: root.scrollTop,
            topSpacerHeight: top ? top.offsetHeight : 0,
        };
    }""")
    # Viewport top must be at-or-below the rendered area's start: the user
    # is looking at real messages, not a blank spacer prefix.
    assert snapshot["scrollTop"] >= snapshot["topSpacerHeight"], snapshot


def test_virtualization_resting_in_spacer_keeps_loading(page: Page, clean_state):
    """After scrolling up to land in the top spacer, the window must
    converge to cover the user's position without further interaction.
    The centre-on-scrollTop slide fully covers the user's position in
    one pass — IntersectionObserver only re-fires on transitions, so
    leaving the new spacer still intersecting would strand the user."""
    _api_create_contact("VirtRestChar")
    user_id = _api_create_user("VirtRestUser")
    contact_id = next(
        c["id"]
        for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "VirtRestChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    msg_ids = _api_seed_message_chain(chat_id, 200, "VirtRestChar", "VirtRestUser")
    early_msg_id = msg_ids[5]  # well outside the initial tail-anchored window

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=8000)
    page.wait_for_timeout(150)

    # The early message should not be in the DOM yet.
    in_dom_initially = page.evaluate(f"""() => {{
        return !!document.querySelector(
            '#chat-messages .msg[data-msg-id="{early_msg_id}"]'
        );
    }}""")
    assert not in_dom_initially, "setup: early msg must start virtualized out"

    # Drop scrollTop to near the top in one shot, then sit still — no
    # additional scrolling, clicks, or key wiggling.
    page.evaluate("document.getElementById('chat-messages').scrollTop = 100")
    page.wait_for_timeout(400)

    # The early message should now be in the DOM, having been pulled in
    # by the slide that re-centred on the user's scroll position.
    in_dom_after = page.evaluate(f"""() => {{
        return !!document.querySelector(
            '#chat-messages .msg[data-msg-id="{early_msg_id}"]'
        );
    }}""")
    assert in_dom_after, "early msg must be loaded after deep scroll, with no further interaction"


def _virt_resize_setup(page, contact_name, user_name):
    _api_create_contact(contact_name)
    user_id = _api_create_user(user_name)
    contact_id = next(
        c["id"]
        for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == contact_name
    )
    chat_id = _api_post_chat(contact_id, user_id)
    _api_seed_message_chain(chat_id, 200, contact_name, user_name)

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=8000)
    page.wait_for_timeout(200)


_RESIZE_SNAPSHOT_JS = """(anchorId) => {
    const root = document.getElementById('chat-messages');
    const rootRect = root.getBoundingClientRect();
    const msg = document.querySelector(
        `#chat-messages .msg[data-msg-id="${anchorId}"]`
    );
    if (!msg) {
        const msgs = [...root.querySelectorAll(':scope > .msg')];
        return {
            present: false,
            firstText: msgs[0]?.textContent.slice(0, 40),
            lastText: msgs.at(-1)?.textContent.slice(0, 40),
            count: msgs.length,
            scrollTop: root.scrollTop,
        };
    }
    const r = msg.getBoundingClientRect();
    return {
        present: true,
        offsetFromTop: r.top - rootRect.top,
        msgHeight: r.height,
        rootHeight: rootRect.height,
    };
}"""


def _virt_resize_anchor_id(page):
    return page.evaluate("""() => {
        const root = document.getElementById('chat-messages');
        const rootTop = root.getBoundingClientRect().top;
        for (const msg of root.querySelectorAll(':scope > .msg')) {
            const r = msg.getBoundingClientRect();
            if (r.bottom > rootTop) return msg.dataset.msgId;
        }
        return null;
    }""")


def _virt_resize_assert_anchor_visible(snapshot):
    """Anchor must be at least partially visible — without resize handling
    the user's scrollTop drifts as bubbles reflow and the spacer math
    falls out of sync."""
    assert snapshot["present"], f"anchor msg must remain in DOM after resize: {snapshot}"
    msg_top = snapshot["offsetFromTop"]
    msg_bottom = msg_top + snapshot["msgHeight"]
    assert msg_bottom > 0 and msg_top < snapshot["rootHeight"], snapshot


def test_virtualization_preserves_view_across_modest_resize(page: Page, clean_state):
    """Width changes reflow bubbles. The resize handler must wipe the
    cached heights and re-anchor on whichever msg the user was viewing
    pre-resize. This case keeps scrollTop inside the rendered window
    after the initial slide."""
    _virt_resize_setup(page, "VirtResizeChar", "VirtResizeUser")
    page.set_viewport_size({"width": 1200, "height": 800})
    page.evaluate("document.getElementById('chat-messages').scrollTop = 800")
    page.wait_for_timeout(300)
    anchor = _virt_resize_anchor_id(page)
    assert anchor is not None

    page.set_viewport_size({"width": 600, "height": 800})
    page.wait_for_timeout(400)
    _virt_resize_assert_anchor_visible(page.evaluate(_RESIZE_SNAPSHOT_JS, anchor))


def test_virtualization_preserves_view_across_aggressive_resize(page: Page, clean_state):
    """Aggressive resize (large dimension change in both axes) on a
    long virtualized chat must keep the previously-anchored msg in
    view. The resize handler must capture the pre-resize anchor BEFORE
    reflow so it doesn't pin to whichever msg happens to land at the
    new viewport top."""
    _virt_resize_setup(page, "VirtAggrChar", "VirtAggrUser")
    page.set_viewport_size({"width": 1500, "height": 1000})
    # scrollTop=5000 lands the user in the top spacer pre-slide; the
    # slide then materialises the surrounding window. Exercises the
    # spacer-walk fallback in the anchor identification.
    page.evaluate("document.getElementById('chat-messages').scrollTop = 5000")
    page.wait_for_timeout(400)
    anchor = _virt_resize_anchor_id(page)
    assert anchor is not None

    page.set_viewport_size({"width": 380, "height": 400})
    page.wait_for_timeout(800)
    _virt_resize_assert_anchor_visible(page.evaluate(_RESIZE_SNAPSHOT_JS, anchor))


def test_virtualization_resize_round_trip_is_stable(page: Page, clean_state):
    """Resizing wide → narrow → wide must round-trip the user back to
    (approximately) the same anchor msg — intermediate resize fires
    must not accumulate drift on the anchor identification."""
    _virt_resize_setup(page, "VirtRoundChar", "VirtRoundUser")
    page.set_viewport_size({"width": 1200, "height": 800})
    page.evaluate("document.getElementById('chat-messages').scrollTop = 6000")
    page.wait_for_timeout(400)
    anchor = _virt_resize_anchor_id(page)
    assert anchor is not None

    page.set_viewport_size({"width": 500, "height": 600})
    page.wait_for_timeout(400)
    page.set_viewport_size({"width": 1200, "height": 800})
    page.wait_for_timeout(400)
    _virt_resize_assert_anchor_visible(page.evaluate(_RESIZE_SNAPSHOT_JS, anchor))


def test_virtualization_search_navigates_to_virtualised_match(page: Page, clean_state):
    """Search → next on a match in the virtualized prefix should pull the
    target message into the loaded window AND scroll the highlight into
    view. Verifies the smart-scroll-by-id materialisation path that
    branch nav also relies on."""
    from urllib.request import Request

    _api_create_contact("VirtSearchChar")
    user_id = _api_create_user("VirtSearchUser")
    contact_id = next(
        c["id"]
        for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "VirtSearchChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    # 200-msg chain with a unique sentinel placed at index 5 — well
    # outside the initial tail-anchored window of 110..199.
    parent: str | None = None
    target_id = ""
    for i in range(200):
        sender = "user" if i % 2 == 0 else "contact"
        text = f"VIRTNEEDLE in Msg {i:04d}" if i == 5 else f"Msg {i:04d}"
        body_item = {"text": text}
        if sender == "contact":
            body_item["emotion"] = "neutral"
        payload = {
            "sender": sender,
            "sender_name": "VirtSearchUser" if sender == "user" else "VirtSearchChar",
            "body": [body_item],
        }
        if parent is not None:
            payload["parent_id"] = parent
        msg = json.loads(urlopen(Request(
            f"{base_url()}/api/chats/{chat_id}/messages",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=3).read())
        parent = msg["id"]
        if i == 5:
            target_id = msg["id"]

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=8000)
    page.wait_for_timeout(150)

    # Confirm the sentinel is virtualized out of the initial window.
    in_dom_before = page.evaluate(f"""() => {{
      return !!document.querySelector(
        '#chat-messages .msg[data-msg-id="{target_id}"]'
      );
    }}""")
    assert not in_dom_before, "setup: target msg must start virtualized"

    # Open search, type the sentinel — recompute auto-navigates to first
    # match, which materialises the target message into the window.
    page.keyboard.press("Control+f")
    page.wait_for_selector(".chat-search-pill", timeout=2000)
    page.locator(".chat-search-pill .search-input").fill("VIRTNEEDLE")
    page.wait_for_timeout(300)

    snapshot = page.evaluate(f"""() => {{
      const root = document.getElementById('chat-messages');
      const target = document.querySelector(
        '#chat-messages .msg[data-msg-id="{target_id}"]'
      );
      const current = root.querySelector('mark.search-mark.current');
      const inView = (() => {{
        if (!current) return null;
        const cr = current.getBoundingClientRect();
        const rr = root.getBoundingClientRect();
        return cr.top >= rr.top - 1 && cr.bottom <= rr.bottom + 1;
      }})();
      return {{
        targetInDom: !!target,
        hasCurrent: !!current,
        currentInView: inView,
      }};
    }}""")
    assert snapshot["targetInDom"], snapshot
    assert snapshot["hasCurrent"], snapshot
    assert snapshot["currentInView"], snapshot


# ---------------------------------------------------------------------------
# Ctrl+F search bar.
# ---------------------------------------------------------------------------


def test_search_finds_query_across_chat(page: Page, clean_state):
    """Ctrl+F opens the search pill and counts matches across ALL messages
    on the active path — including ones the virtualization has dropped
    from the DOM."""
    _api_create_contact("SearchChar")
    user_id = _api_create_user("SearchUser")
    contact_id = next(
        c["id"]
        for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "SearchChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    # 30 messages, 5 of which contain the unique sentinel "needle".
    from urllib.request import Request

    parent: str | None = None
    sentinel_msgs: list[str] = []
    for i in range(30):
        sender = "user" if i % 2 == 0 else "contact"
        text = f"Msg {i:04d}"
        if i in (3, 8, 14, 22, 27):
            text = f"sometimes a needle in Msg {i:04d}"
        body_item = {"text": text}
        if sender == "contact":
            body_item["emotion"] = "neutral"
        payload = {
            "sender": sender,
            "sender_name": "SearchUser" if sender == "user" else "SearchChar",
            "body": [body_item],
        }
        if parent is not None:
            payload["parent_id"] = parent
        msg = json.loads(urlopen(Request(
            f"{base_url()}/api/chats/{chat_id}/messages",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=3).read())
        parent = msg["id"]
        if "needle" in text:
            sentinel_msgs.append(msg["id"])

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=8000)

    # Ctrl+F → search pill.
    page.keyboard.press("Control+f")
    page.wait_for_selector(".chat-search-pill", timeout=2000)
    pill = page.locator(".chat-search-pill")
    pill.locator(".search-input").fill("needle")
    # Debounce is 80 ms.
    page.wait_for_timeout(200)

    counter = pill.locator(".search-counter").text_content()
    assert counter == "1/5", counter

    # Navigate next a few times and watch the counter advance.
    pill.locator(".search-nav", has_text="▾").click()
    page.wait_for_timeout(150)
    assert pill.locator(".search-counter").text_content() == "2/5"
    pill.locator(".search-nav", has_text="▾").click()
    page.wait_for_timeout(150)
    assert pill.locator(".search-counter").text_content() == "3/5"

    # Highlights are present on at least one rendered match.
    highlight_count = page.evaluate(
        "() => document.querySelectorAll('mark.search-mark').length"
    )
    assert highlight_count >= 1

    # Esc closes the pill and clears highlights.
    page.locator(".chat-search-pill .search-input").focus()
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    assert page.locator(".chat-search-pill").count() == 0
    assert page.evaluate(
        "() => document.querySelectorAll('mark.search-mark').length"
    ) == 0


# ---------------------------------------------------------------------------
# Bulk zip import — drive both flavours through the Settings file input.
# ---------------------------------------------------------------------------


def _build_flat_zip(tmpdir: Path) -> Path:
    """Synthetic flat-JSON zip with one of each kind. Placeholder content
    only — no archive-derived data."""
    import zipfile as _zf

    contact = {
        "id": "ui-flat-contact-001",
        "name": "FlatUiAlice",
        "description": "UI flat-zip contact",
        "greeting": "Hi.",
        "greetingEmotion": "neutral",
        "relationship": "stranger",
        "style": "chat",
        "tags": "",
        "emotions": {},
        "exampleMessages": [],
    }
    user = {
        "id": "ui-flat-user-001",
        "name": "FlatUiBob",
        "persona": "UI persona",
        "tags": "",
    }
    scenario = {
        "id": "ui-flat-scenario-001",
        "name": "FlatUiLobby",
        "environment": "UI test env",
        "scene": "",
    }
    chat = {
        "chat": {
            "id": "ui-flat-chat-001",
            "title": "FlatUiChat",
            "tags": "",
            "intimacy": "stranger",
            "style": "chat",
            "messages": [],
            "selectedChildId": {},
        },
        "character": {
            "id": "ui-flat-chat-char-001",
            "name": "FlatUiChar",
            "greeting": "Yo.",
            "greetingEmotion": "neutral",
            "tags": "",
            "emotions": {},
        },
        "user": {
            "id": "ui-flat-chat-user-001",
            "name": "FlatUiChatUser",
            "persona": "UI",
            "tags": "",
        },
    }
    p = tmpdir / "ui-flat.zip"
    with _zf.ZipFile(p, "w", _zf.ZIP_DEFLATED) as zf:
        zf.writestr("alice.json", json.dumps(contact))
        zf.writestr("bob.json", json.dumps(user))
        zf.writestr("lobby.json", json.dumps(scenario))
        zf.writestr("chat.json", json.dumps(chat))
    return p


def _build_aer_zip(tmpdir: Path) -> Path:
    """Synthetic AER-bulk zip with one contact + one space (with overrides
    so it promotes to a ContactScenario) + a tiny linear message stream."""
    import zipfile as _zf

    src_contact = "ui-aer-contact-src-id-1"
    src_space = "ui-aer-space-src-id-1"
    contact_meta = {
        "contact_id": src_contact,
        "name": "AerUiAlice",
        "description": "UI AER contact",
        "tagline": "",
        "avatar_uri": "",
        "search_tags": ["ui-test"],
        "gender": "any",
        "pronouns": "they/them",
        "species": "test",
        "relationship": "stranger",
        "emotions": {},
        "revision_data": {
            "revision_id": "ui-aer-revision-1",
            "revision_timestamp": "2026-05-09T12:00:00Z",
            "is_rollback": False,
        },
        "ai_data": {
            "persona": "UI persona",
            "appearance": "",
            "greeting": "",
            "greeting_emotion": "neutral",
            "example_messages": [],
        },
    }
    space_meta = {
        "stream_id": src_space,
        "stream_name": "AerUiSpace",
        # Non-empty scene so has_overrides → True (promotes to ContactScenario).
        "scene": "A test scene",
        "environment": "",
        "chat_tags": [],
        "greeting": "",
        "greeting_emotion": "",
        "style": "",
        "background_uri": "",
        "relationship": "",
        "response_length": 0,
        "last_update_timestamp": "2026-05-09T12:00:00Z",
    }
    base_ts = 1_700_000_000_000
    messages = [
        {"id": "m0", "timestamp": base_ts, "sequence_number": 1,
         "event_type": "create", "speaker": "AerUiAlice", "message": "Hi.",
         "emotion": "neutral"},
        {"id": "m1", "timestamp": base_ts + 1000, "sequence_number": 2,
         "event_type": "create", "speaker": None, "message": "Hello back."},
    ]
    p = tmpdir / "ui-aer.zip"
    with _zf.ZipFile(p, "w", _zf.ZIP_DEFLATED) as zf:
        cdir = f"contacts/AerUiAlice-{src_contact}"
        zf.writestr(f"{cdir}/meta-contact.json", json.dumps(contact_meta))
        sdir = f"{cdir}/spaces/AerUiSpace-{src_space}"
        zf.writestr(f"{sdir}/meta-space.json", json.dumps(space_meta))
        zf.writestr(f"{sdir}/messages.json", json.dumps(messages))
    return p


def test_bulk_import_flat_zip_via_settings(page: Page, clean_state, tmp_path):
    """Upload a flat-JSON zip through the Settings bulk-import input. The
    picker should render one row per file with kind badges; submitting
    imports all four entities."""
    zip_path = _build_flat_zip(tmp_path)
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector("h3:has-text('Bulk import')", timeout=5000)

    page.locator('#bulk-import-input').set_input_files(str(zip_path))

    # Picker modal renders. The intro line tallies kinds; rows show the
    # synthetic names with kind badges next to them.
    page.wait_for_selector(".zip-picker", timeout=8000)
    intro = page.locator(".zip-picker-intro").text_content() or ""
    assert "4 items" in intro, intro
    for label in ("FlatUiAlice", "FlatUiBob", "FlatUiLobby", "FlatUiChat"):
        assert page.locator(".zip-picker-row").filter(has_text=label).count() == 1, label
    # Each row has a kind badge as its first inline pill.
    badges = page.locator(".zip-picker-row .zip-picker-badge").all_text_contents()
    assert {"Contact", "User", "Scenario", "Chat"}.issubset(set(badges)), badges
    shot(page, "30-bulk-flat-picker")

    # Submit → progress modal → toast.
    page.locator(".zip-picker .modal-actions .btn.primary").click()
    page.wait_for_selector(".zip-picker", state="detached", timeout=15000)
    page.wait_for_selector(".toast.success", timeout=15000)
    shot(page, "31-bulk-flat-toast")

    # Server-side: each kind has its imported entity (by deterministic id).
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    users = json.load(urlopen(f"{base_url()}/api/users", timeout=3))
    scenarios = json.load(urlopen(f"{base_url()}/api/scenarios", timeout=3))
    chats = json.load(urlopen(f"{base_url()}/api/chats", timeout=3))
    assert any(c["id"] == "ui-flat-contact-001" for c in contacts), contacts
    assert any(u["id"] == "ui-flat-user-001" for u in users), users
    assert any(s["id"] == "ui-flat-scenario-001" for s in scenarios), scenarios
    assert any(c["id"] == "ui-flat-chat-001" for c in chats), chats


def test_bulk_import_aer_zip_via_settings(page: Page, clean_state, tmp_path):
    """Upload an AER-bulk zip with one contact + one space (with overrides).
    Picker renders the contact row + child chat row; submitting imports the
    contact, attaches the scenario, and creates the chat with coalesced
    messages."""
    from server.importers import _aer_to_uuid

    zip_path = _build_aer_zip(tmp_path)
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector("h3:has-text('Bulk import')", timeout=5000)

    page.locator('#bulk-import-input').set_input_files(str(zip_path))

    # Picker renders nested rows: one contact, one expandable child space.
    page.wait_for_selector(".zip-picker", timeout=8000)
    assert page.locator(".zip-picker-row-contact").count() == 1
    assert page.locator(".zip-picker-row-space").count() == 1
    # Contact row carries the source name; child space row shows the chat name.
    assert page.locator(".zip-picker-row-contact").filter(has_text="AerUiAlice").count() == 1
    assert page.locator(".zip-picker-row-space").filter(has_text="AerUiSpace").count() == 1
    # The space promotes to a scenario (scene is non-empty), so the
    # "Custom scenario" pill appears on the child row.
    assert page.locator(".zip-picker-row-space").filter(
        has_text="Custom scenario",
    ).count() == 1
    shot(page, "32-bulk-aer-picker")

    page.locator(".zip-picker .modal-actions .btn.primary").click()
    page.wait_for_selector(".zip-picker", state="detached", timeout=20000)
    page.wait_for_selector(".toast.success", timeout=20000)
    shot(page, "33-bulk-aer-toast")

    # Server-side: deterministic UUIDs landed; chat's contact_scenario_id
    # points at the appended ContactScenario.
    contact_uuid = _aer_to_uuid("contact", "ui-aer-contact-src-id-1")
    chat_uuid = _aer_to_uuid("chat", "ui-aer-space-src-id-1")
    scen_uuid = _aer_to_uuid("scenario", "ui-aer-space-src-id-1")
    with urlopen(f"{base_url()}/api/contacts/{contact_uuid}", timeout=3) as r:
        contact = json.load(r)
    assert contact["name"] == "AerUiAlice"
    assert any(cs["id"] == scen_uuid for cs in contact["scenarios"]), contact
    with urlopen(f"{base_url()}/api/chats/{chat_uuid}", timeout=3) as r:
        chat = json.load(r)
    assert chat["contact_scenario_id"] == scen_uuid
    assert chat["contact_id"] == contact_uuid


def test_bulk_import_reimport_flat_zip_defaults_to_skip(page: Page, clean_state, tmp_path):
    """After importing a flat zip, re-uploading it should show every row
    in ``up_to_date`` state with the action defaulted to skip — submitting
    imports nothing."""
    zip_path = _build_flat_zip(tmp_path)
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector("h3:has-text('Bulk import')", timeout=5000)

    # First import.
    page.locator('#bulk-import-input').set_input_files(str(zip_path))
    page.wait_for_selector(".zip-picker", timeout=8000)
    page.locator(".zip-picker .modal-actions .btn.primary").click()
    page.wait_for_selector(".zip-picker", state="detached", timeout=15000)
    page.wait_for_selector(".toast.success", timeout=15000)

    pre_counts = {
        "contacts": len(json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))),
        "users": len(json.load(urlopen(f"{base_url()}/api/users", timeout=3))),
        "scenarios": len(json.load(urlopen(f"{base_url()}/api/scenarios", timeout=3))),
        "chats": len(json.load(urlopen(f"{base_url()}/api/chats", timeout=3))),
    }

    # Toast auto-dismisses; wait it out so the second one we look for later
    # is unambiguously the new one.
    page.wait_for_selector(".toast.success", state="detached", timeout=10000)

    # Re-upload the same file.
    page.locator('#bulk-import-input').set_input_files(str(zip_path))
    page.wait_for_selector(".zip-picker", timeout=8000)
    # Every row carries an "Imported" badge (state=up_to_date).
    imported_badges = page.locator(".zip-picker-badge", has_text="Imported").count()
    assert imported_badges == 4, imported_badges
    shot(page, "34-bulk-reimport-up-to-date")

    # Submit with defaults (all skip) — toast still fires, nothing
    # duplicated.
    page.locator(".zip-picker .modal-actions .btn.primary").click()
    page.wait_for_selector(".zip-picker", state="detached", timeout=15000)
    page.wait_for_selector(".toast.success", timeout=15000)

    post_counts = {
        "contacts": len(json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))),
        "users": len(json.load(urlopen(f"{base_url()}/api/users", timeout=3))),
        "scenarios": len(json.load(urlopen(f"{base_url()}/api/scenarios", timeout=3))),
        "chats": len(json.load(urlopen(f"{base_url()}/api/chats", timeout=3))),
    }
    assert post_counts == pre_counts, (pre_counts, post_counts)


def _build_many_flat_contacts(tmpdir: Path, n: int) -> Path:
    """Synthetic flat zip with ``n`` contact JSONs, each with a distinct
    name + tag. Used by virtualization / search tests below."""
    import zipfile as _zf

    p = tmpdir / f"flat-{n}.zip"
    with _zf.ZipFile(p, "w", _zf.ZIP_DEFLATED) as zf:
        for i in range(n):
            contact = {
                "id": f"flat-many-{i:04d}",
                "name": f"Contact{i:04d}",
                "description": f"Description for contact {i}",
                "greeting": "Hi.",
                "greetingEmotion": "neutral",
                "relationship": "stranger",
                "style": "chat",
                "tags": f"tag-{i % 5}",
                "emotions": {},
                "exampleMessages": [],
            }
            zf.writestr(f"contact-{i:04d}.json", json.dumps(contact))
    return p


def test_bulk_import_picker_search_filters_rows(page: Page, clean_state, tmp_path):
    """The picker's search box filters rows in place. The closure-state
    map preserves actions for hidden rows so the user can search-and-tweak
    across batches."""
    zip_path = _build_many_flat_contacts(tmp_path, 12)
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector("h3:has-text('Bulk import')", timeout=5000)
    page.locator('#bulk-import-input').set_input_files(str(zip_path))
    page.wait_for_selector(".zip-picker", timeout=8000)

    # Type a substring that matches exactly one of the synthetic names.
    search = page.locator('.zip-picker-search')
    search.fill('Contact0007')
    page.wait_for_timeout(150)  # debounce window
    rows = page.locator('.zip-picker-row').count()
    assert rows == 1, rows

    # Clearing the search restores everything.
    search.fill('')
    page.wait_for_timeout(150)
    # Wait for the virtualizer to re-mount; assert at least the first
    # several rows are visible (full count requires scrolling).
    page.wait_for_function("document.querySelectorAll('.zip-picker-row').length > 1")
    assert page.locator('.zip-picker-row').count() > 1


def test_bulk_import_picker_search_matches_tags(page: Page, clean_state, tmp_path):
    """Search is haystack-based: matches name *and* description / tags."""
    zip_path = _build_many_flat_contacts(tmp_path, 12)
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector("h3:has-text('Bulk import')", timeout=5000)
    page.locator('#bulk-import-input').set_input_files(str(zip_path))
    page.wait_for_selector(".zip-picker", timeout=8000)

    # Tag "tag-0" appears on every fifth contact (i % 5 == 0) → 3 of 12.
    page.locator('.zip-picker-search').fill('tag-0')
    page.wait_for_timeout(150)
    rows = page.locator('.zip-picker-row').count()
    assert rows == 3, rows


def test_bulk_import_picker_bulk_skip_flips_visible_rows(page: Page, clean_state, tmp_path):
    """The bulk-action menu's "Skip all" command flips every visible
    row's action picker to "Skip"."""
    zip_path = _build_many_flat_contacts(tmp_path, 5)
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector("h3:has-text('Bulk import')", timeout=5000)
    page.locator('#bulk-import-input').set_input_files(str(zip_path))
    page.wait_for_selector(".zip-picker", timeout=8000)

    # Default: every row has "Import" on its action select. The trigger
    # text also includes a unicode chevron, so substring-match the label.
    triggers = page.locator('.zip-picker-row .avatar-picker-trigger')
    assert triggers.count() == 5
    for i in range(5):
        assert 'Import' in (triggers.nth(i).text_content() or '')

    # Open the bulk-action picker and select "Skip all".
    bulk_trigger = page.locator('.zip-picker-bulk .avatar-picker-trigger')
    bulk_trigger.click()
    page.locator('.avatar-picker-popover .avatar-picker-option',
                 has_text='Skip all').click()
    page.wait_for_timeout(150)

    # Every row's action select now reads "Skip".
    triggers = page.locator('.zip-picker-row .avatar-picker-trigger')
    for i in range(5):
        text = (triggers.nth(i).text_content() or '').strip()
        # Trigger text is the label + chevron — match the label only.
        assert text.startswith('Skip'), text


def test_bulk_import_picker_virtualization_caps_dom_rows(page: Page, clean_state, tmp_path):
    """For a synthetic large manifest, the picker only mounts a window of
    rows around the visible viewport — total DOM rows stay well under
    the manifest size."""
    zip_path = _build_many_flat_contacts(tmp_path, 200)
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector("h3:has-text('Bulk import')", timeout=5000)
    page.locator('#bulk-import-input').set_input_files(str(zip_path))
    page.wait_for_selector(".zip-picker", timeout=15000)
    # Give the ResizeObserver a frame to settle.
    page.wait_for_timeout(150)

    mounted = page.locator('.zip-picker-row').count()
    # 200 manifest rows; with 80 px estimated height + ~600 px viewport +
    # 200 px buffer either side, expect ~25-35 mounted.
    assert mounted < 60, mounted
    assert mounted > 0, mounted



# ===========================================================================
# Brain libraries — tab, list, detail, cascade, missing-library marker.
# ===========================================================================


def _api_create_library(name: str, *, tags: str = "", description: str = "") -> dict:
    """Seed a library via the JSON API (same shape as ``_api_create_contact``)."""
    from urllib.request import Request

    body = json.dumps({
        "name": name, "tags": tags, "description": description,
    }).encode()
    req = Request(
        f"{base_url()}/api/libraries",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urlopen(req, timeout=3) as r:
        return json.load(r)


def test_brain_libraries_tab_loads(page: Page, clean_state):
    """The 6th rail icon appears between Scenarios and Settings; clicking
    it routes to ``/libraries`` and renders an empty-state."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    btn = page.locator('.rail-btn[data-tab="libraries"]')
    assert btn.count() == 1
    btn.click()
    page.wait_for_selector("h2:has-text('Brain libraries')", timeout=5000)
    # URL becomes /libraries.
    assert page.url.rstrip("/").endswith("/libraries")
    # Empty state for a fresh data dir.
    assert page.locator(".list-empty:has-text('No brain libraries')").count() == 1


def test_brain_library_create_via_ui_then_edit_autosaves(page: Page, clean_state):
    """Click the ``+`` button to mint a library, type into the description
    textarea, and verify the change persists to the server through the
    auto-save debounce path."""
    page.goto(f"{base_url()}/libraries")
    page.wait_for_selector("h2:has-text('Brain libraries')", timeout=5000)
    page.locator('.list-header button[title="New brain library"]').click()
    page.wait_for_selector('.list-row', timeout=3000)
    # The new library is auto-selected; the detail pane is open.
    page.wait_for_selector('#library-detail .page-header h2', timeout=3000)

    # Type a description and wait for auto-save to flush.
    desc_textarea = page.locator('#library-detail textarea').first
    desc_textarea.click()
    desc_textarea.type("Setting bible for the long-running campaign", delay=10)

    # The dirty dot lights up while the save is pending; it clears once the
    # 400 ms autosaver debounce + the request finishes.
    page.wait_for_function(
        "() => document.querySelector('#library-detail .dirty-dot')"
        " && !document.querySelector('#library-detail .dirty-dot').classList.contains('dirty')",
        timeout=4000,
    )

    libs = json.load(urlopen(f"{base_url()}/api/libraries"))
    assert any(
        l["description"] == "Setting bible for the long-running campaign"
        for l in libs
    ), libs


def test_brain_library_row_shows_count_badge_inline_with_name(page: Page, clean_state):
    """The brain count is rendered as a chip badge sitting at the end of
    the row's name (not in a far-right column). Description shows below."""
    _api_create_library("Lore", description="World rules and recurring NPCs")
    page.goto(f"{base_url()}/libraries")
    page.wait_for_selector('.list-row', timeout=5000)
    row = page.locator('.list-row').first
    # Badge lives inside the title group, adjacent to the name.
    badge = row.locator('.row-title .row-title-name-group .row-inline-badge')
    assert badge.count() == 1
    assert badge.text_content().strip() == "0"
    # Description renders below as the row-sub.
    assert "World rules" in row.locator('.row-sub').text_content()


def test_wizard_cascade_appends_library_pickers(page: Page, clean_state):
    """Open the new-chat wizard. Below the scenario picker, a (none)
    library picker waits. Picking a library reveals another (none)
    picker that excludes the already-picked entry."""
    # Wizard needs a contact + a user persona before it can render.
    _api_create_contact("Aria")
    from urllib.request import Request
    urlopen(Request(
        f"{base_url()}/api/users",
        data=json.dumps({"name": "Me"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )).read()

    lib_a = _api_create_library("Library A")
    lib_b = _api_create_library("Library B")

    page.goto(f"{base_url()}/chats")
    page.wait_for_selector("#rail")
    # Open the new-chat wizard via the chat-list "+" button.
    page.locator('.list-header button[title="New chat"]').click()
    page.wait_for_selector('.modal h3:has-text("New chat")', timeout=5000)

    cascade = page.locator('.modal .library-cascade')
    assert cascade.count() == 1
    # Initially only the trailing (none) picker is mounted.
    rows = cascade.locator('.library-cascade-row')
    assert rows.count() == 1
    trigger = rows.nth(0).locator('.avatar-picker-trigger')
    assert "(none)" in trigger.text_content()

    # Pick Library A.
    trigger.click()
    page.locator('.avatar-picker-option:has-text("Library A")').click()
    # Two rows now: A and a fresh trailing (none).
    page.wait_for_function(
        "() => document.querySelectorAll('.modal .library-cascade-row').length === 2",
        timeout=2000,
    )

    # The second picker must NOT offer Library A — only the remaining one.
    second_trigger = cascade.locator('.library-cascade-row').nth(1).locator('.avatar-picker-trigger')
    second_trigger.click()
    options = page.locator('.avatar-picker-popover:not(.hidden) .avatar-picker-option')
    labels = [options.nth(i).text_content().strip() for i in range(options.count())]
    assert "Library A" not in labels, labels
    assert any("Library B" in l for l in labels), labels
    # Pick Library B.
    page.locator('.avatar-picker-option:has-text("Library B")').click()
    # All libraries now picked → no trailing (none) row.
    page.wait_for_function(
        "() => document.querySelectorAll('.modal .library-cascade-row').length === 2",
        timeout=2000,
    )

    # Setting the first picker back to (none) drops it and shifts the rest up.
    cascade.locator('.library-cascade-row').nth(0).locator('.avatar-picker-trigger').click()
    page.locator('.avatar-picker-popover:not(.hidden) .avatar-picker-option:has-text("(none)")').click()
    page.wait_for_function(
        "() => document.querySelectorAll('.modal .library-cascade-row').length === 2",
        timeout=2000,
    )
    # Surviving entry is Library B with a trailing (none) for re-adding A.
    first_label = cascade.locator('.library-cascade-row').nth(0).locator('.avatar-picker-trigger').text_content()
    assert "Library B" in first_label
    second_label = cascade.locator('.library-cascade-row').nth(1).locator('.avatar-picker-trigger').text_content()
    assert "(none)" in second_label

    # Hit Create — the new chat must carry brain_library_ids = [B].
    page.locator('.modal button:has-text("Create")').click()
    page.wait_for_selector('.modal', state='hidden', timeout=3000)
    page.wait_for_timeout(200)
    chats = json.load(urlopen(f"{base_url()}/api/chats"))
    assert len(chats) == 1
    assert chats[0]["brain_library_ids"] == [lib_b["id"]]


def test_chat_info_modal_shows_missing_library_marker(page: Page, clean_state):
    """A chat attached to a since-deleted library renders a greyed-out
    placeholder in the cascade (so the user can reassign or detach)."""
    from urllib.request import Request

    _api_create_contact("Aria")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    cid = contacts[0]["id"]
    urlopen(Request(
        f"{base_url()}/api/users",
        data=json.dumps({"name": "Me"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )).read()
    uid = json.load(urlopen(f"{base_url()}/api/users"))[0]["id"]
    lib = _api_create_library("Will be deleted")

    # Create the chat with the library attached, then delete the library.
    chat = json.loads(urlopen(Request(
        f"{base_url()}/api/chats",
        data=json.dumps({
            "contact_id": cid, "user_id": uid,
            "brain_library_ids": [lib["id"]],
        }).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )).read())
    urlopen(Request(
        f"{base_url()}/api/libraries/{lib['id']}",
        method="DELETE",
    )).read()

    # Open the chat info modal.
    page.goto(f"{base_url()}/chats")
    page.wait_for_selector('.list-row', timeout=5000)
    page.locator('.list-row').first.click()
    page.wait_for_selector('#chat-content-pane .chat-header', timeout=5000)
    page.locator('#chat-content-pane button[title*="info" i]').first.click()
    page.wait_for_selector('.modal h3:has-text("Chat info")', timeout=5000)

    # The library cascade row for this missing id has the ``.missing`` class
    # and its picker trigger says "Unknown library".
    missing_row = page.locator('.modal .library-cascade-row.missing')
    assert missing_row.count() == 1
    assert "Unknown library" in missing_row.locator('.avatar-picker-trigger').text_content()

    # Set it to (none) to detach.
    missing_row.locator('.avatar-picker-trigger').click()
    page.locator('.avatar-picker-popover:not(.hidden) .avatar-picker-option:has-text("(none)")').click()
    # The cascade now only has the trailing (none) entry.
    page.wait_for_function(
        "() => document.querySelectorAll('.modal .library-cascade-row.missing').length === 0",
        timeout=2000,
    )

    # Save and verify the chat has detached the library.
    page.locator('.modal button:has-text("Save")').click()
    page.wait_for_selector('.modal', state='hidden', timeout=5000)
    refreshed = json.load(urlopen(f"{base_url()}/api/chats/{chat['id']}"))
    assert refreshed["brain_library_ids"] == [], refreshed


def test_brain_library_export_round_trip_via_api(page: Page, clean_state):
    """End-to-end UI sanity: create a library, edit it via the detail
    view, then fetch the per-resource export and reimport it on top of a
    cleared instance — the library returns with the same name and tags."""
    page.goto(f"{base_url()}/libraries")
    page.wait_for_selector("h2:has-text('Brain libraries')", timeout=5000)
    page.locator('.list-header button[title="New brain library"]').click()
    page.wait_for_selector('#library-detail', timeout=3000)
    # Set name + tags via the detail view.
    name_input = page.locator('#library-detail .form-grid input[type="text"]').first
    name_input.click()
    page.keyboard.press('Control+A')
    name_input.type("Roundtrip Library", delay=10)
    tags_input = page.locator('#library-detail .form-grid input[type="text"]').nth(1)
    tags_input.click()
    tags_input.type("fantasy, magic", delay=10)
    page.wait_for_function(
        "() => document.querySelector('#library-detail .dirty-dot')"
        " && !document.querySelector('#library-detail .dirty-dot').classList.contains('dirty')",
        timeout=4000,
    )

    libs = json.load(urlopen(f"{base_url()}/api/libraries"))
    lib = next(l for l in libs if l["name"] == "Roundtrip Library")
    exported = json.load(urlopen(f"{base_url()}/api/export/library/{lib['id']}"))
    assert exported["kind"] == "brain_library"
    assert exported["name"] == "Roundtrip Library"
    assert exported["tags"] == "fantasy, magic"


# ---------------------------------------------------------------------------
# New feature surfaces (foreign-format imports, card-image slot, reminder
# brain panel, unified Import button cross-tab dispatch, Export card button,
# brain editor Import/Export buttons, BrainKey whole-word + search-messages).
# ---------------------------------------------------------------------------


def _png_bytes(size=(8, 8), color=(120, 30, 90)) -> bytes:
    import io as _io
    from PIL import Image as _Img

    buf = _io.BytesIO()
    _Img.new("RGB", size, color=color).save(buf, "PNG")
    return buf.getvalue()


def _make_st_card_v2_json(**data_overrides) -> bytes:
    """Synthetic ST-card v2 JSON for import. Never carries real fixtures
    or names from outside repos — all fields are explicit here."""
    base = {
        "name": "TestST",
        "description": "ST import test character.",
        "personality": "Curious.",
        "scenario": "",
        "first_mes": "Hi there!",
        "mes_example": "",
        "creator_notes": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "tags": ["test"],
        "extensions": {},
    }
    base.update(data_overrides)
    card = {"spec": "chara_card_v2", "spec_version": "2.0", "data": base}
    return json.dumps(card).encode("utf-8")


def _make_st_card_v2_png() -> bytes:
    """ST card v2 as PNG with ``chara`` tEXt chunk."""
    import base64 as _b64
    import io as _io
    from PIL import Image as _Img
    from PIL.PngImagePlugin import PngInfo as _PngInfo

    info = _PngInfo()
    info.add_text("chara", _b64.b64encode(_make_st_card_v2_json(
        name="PngST", post_history_instructions="Stay terse."
    )).decode("ascii"))
    buf = _io.BytesIO()
    _Img.new("RGB", (16, 16), color=(80, 30, 200)).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


# Tiny WI fixture written inline so no real archive content leaks in.
def _make_wi_json() -> bytes:
    wi = {
        "entries": {
            "0": {"comment": "WiEntryA", "content": "About coffee.",
                  "key": ["coffee"]},
            "1": {"comment": "WiEntryB", "content": "About tea.",
                  "key": ["tea"]},
        },
    }
    return json.dumps(wi).encode("utf-8")


def _make_lorebook_json() -> bytes:
    lb = {
        "lorebookVersion": 5,
        "entries": [
            {"id": "x1", "displayName": "LbA", "text": "Foo body", "keys": ["foo"]},
            {"id": "x2", "displayName": "LbB", "text": "Bar body", "keys": ["bar"]},
        ],
        "categories": [],
        "settings": {},
    }
    return json.dumps(lb).encode("utf-8")


def _drop_file(page: Page, input_selector: str, name: str, data: bytes,
                mime: str) -> None:
    """Programmatically set a file on an ``<input type=file>``. Playwright's
    ``set_input_files`` needs a path, so write to a temp file first."""
    p = SCREENSHOTS / f"_tmpfile_{name}"
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    page.locator(input_selector).set_input_files(str(p))


def test_card_image_slot_renders_on_contact(page: Page, clean_state):
    _api_create_contact("CardSlot")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body")
    page.locator("#contact-list-body .list-row").filter(has_text="CardSlot").first.click()
    page.wait_for_selector("h3:has-text('Avatar & card image')", timeout=5000)
    # Both blocks present, both labelled.
    assert page.locator(".media-row .media-block").count() == 2
    labels = page.locator(".media-block-label").all_text_contents()
    assert any("AVATAR" in l.upper() for l in labels)
    assert any("CARD" in l.upper() for l in labels)


def test_card_image_upload_round_trip(page: Page, clean_state):
    _api_create_contact("CardRound")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    cid = next(c["id"] for c in contacts if c["name"] == "CardRound")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").filter(has_text="CardRound").first.click()
    page.wait_for_selector(".card-image-edit")

    _drop_file(
        page,
        f'#card-input-contact-{cid}',
        "card.png", _png_bytes(),
        "image/png",
    )
    # Wait for the preview img to appear inside the card block.
    page.wait_for_selector(".card-image-preview img", timeout=5000)
    # The Export card button shows up in the header.
    page.wait_for_selector('button:has-text("Export card")', timeout=5000)


def test_st_card_json_imports_as_contact_from_libraries_tab(page: Page, clean_state):
    """Drop an ST card JSON on the Brain Libraries tab's Import button —
    server detects the kind as contact, frontend auto-switches to the
    Contacts tab and opens the new entity."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="libraries"]')
    page.wait_for_selector("#library-list-body, .list-empty", timeout=5000)
    _drop_file(page, "#library-import-input", "card.json",
               _make_st_card_v2_json(name="JumpST"), "application/json")
    # After import, the UI auto-switches to Contacts and opens JumpST.
    page.wait_for_selector('.rail-btn.active[data-tab="contacts"]', timeout=8000)
    page.wait_for_selector("#contact-detail h2:has-text('JumpST')", timeout=5000)


def test_st_card_png_imports_with_card_image_set(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body, .list-empty", timeout=5000)
    _drop_file(page, "#contact-import-input", "card.png",
               _make_st_card_v2_png(), "image/png")
    page.wait_for_selector("#contact-detail h2:has-text('PngST')", timeout=8000)
    # The Export card button appears (card_image was set during import).
    page.wait_for_selector('button:has-text("Export card")', timeout=3000)
    # And the reminder brain panel shows the post_history_instructions.
    page.locator("h3:has-text('Reminder brain')").scroll_into_view_if_needed()
    rb_content = page.locator(".reminder-brain-content").input_value()
    assert rb_content == "Stay terse."
    rb_name = page.locator(".reminder-brain-name").input_value()
    assert rb_name == "Notes"


def test_wi_json_imports_as_brain_library_from_contacts_tab(page: Page, clean_state):
    """ST WI standalone JSON dropped on the Contacts Import button —
    detected as brain_library, UI jumps to Libraries tab."""
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body, .list-empty", timeout=5000)
    _drop_file(page, "#contact-import-input", "wi.json",
               _make_wi_json(), "application/json")
    page.wait_for_selector('.rail-btn.active[data-tab="libraries"]', timeout=8000)
    page.wait_for_selector("#library-detail", timeout=5000)
    # Imported entries surface as brains on the library.
    libs = json.load(urlopen(f"{base_url()}/api/libraries"))
    assert len(libs) == 1
    lib_full = json.load(urlopen(f"{base_url()}/api/libraries/{libs[0]['id']}"))
    names = [b["name"] for b in lib_full["brains"]]
    assert "WiEntryA" in names
    assert "WiEntryB" in names


def test_lorebook_json_imports_as_brain_library(page: Page, clean_state):
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body, .list-empty", timeout=5000)
    _drop_file(page, "#contact-import-input", "book.json",
               _make_lorebook_json(), "application/json")
    page.wait_for_selector('.rail-btn.active[data-tab="libraries"]', timeout=8000)
    libs = json.load(urlopen(f"{base_url()}/api/libraries"))
    assert len(libs) == 1
    lib_full = json.load(urlopen(f"{base_url()}/api/libraries/{libs[0]['id']}"))
    names = [b["name"] for b in lib_full["brains"]]
    assert "LbA" in names and "LbB" in names


def test_reminder_brain_panel_add_edit_delete(page: Page, clean_state):
    _api_create_contact("RemUI")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    cid = next(c["id"] for c in contacts if c["name"] == "RemUI")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").filter(has_text="RemUI").first.click()
    page.locator("h3:has-text('Reminder brain')").scroll_into_view_if_needed()

    # Initially: "Add reminder" affordance, no editing controls.
    page.wait_for_selector('button:has-text("Add reminder")', timeout=3000)
    assert page.locator(".reminder-brain-name").count() == 0

    # Click + Add reminder.
    page.click('button:has-text("Add reminder")')
    page.wait_for_selector(".reminder-brain-name", timeout=3000)

    # Type a name + content; depth defaults to 0.
    page.locator(".reminder-brain-name").type("UI Notes", delay=10)
    page.locator(".reminder-brain-content").type("Be brief.", delay=10)

    # Autosaves — wait for dirty dot to clear, then verify server-side.
    page.wait_for_function(
        "() => { const d = document.querySelector('#contact-detail .dirty-dot');"
        " return d && !d.classList.contains('dirty'); }",
        timeout=4000,
    )
    c = json.load(urlopen(f"{base_url()}/api/contacts/{cid}"))
    assert c["reminder_brain"]["name"] == "UI Notes"
    assert c["reminder_brain"]["content"] == "Be brief."
    assert c["reminder_brain"]["depth"] == 0

    # Remove the reminder — panel reverts to + Add affordance.
    page.locator(".reminder-brain-header .icon-btn").last.click()
    page.wait_for_function(
        "() => document.querySelector('button')"
        " && Array.from(document.querySelectorAll('button')).some("
        "  b => b.textContent.includes('Add reminder')"
        ")",
        timeout=3000,
    )
    page.wait_for_function(
        "() => { const d = document.querySelector('#contact-detail .dirty-dot');"
        " return d && !d.classList.contains('dirty'); }",
        timeout=4000,
    )
    c = json.load(urlopen(f"{base_url()}/api/contacts/{cid}"))
    assert c["reminder_brain"] is None


def test_brain_disabled_toggle(page: Page, clean_state):
    _api_create_contact("BrainDisable")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").filter(has_text="BrainDisable").first.click()
    page.locator("h3:has-text('Brains')").scroll_into_view_if_needed()
    # Add a brain
    page.click('button:has-text("+ Add brain")')
    page.wait_for_selector(".brain-row", timeout=3000)
    # Name + content
    name_in = page.locator('.brain-row input[type="text"]').first
    name_in.click()
    name_in.type("Toggleable", delay=10)
    content_ta = page.locator('.brain-row textarea').first
    content_ta.click()
    content_ta.type("some content", delay=10)
    # Toggle disabled
    page.locator(".brain-disabled-toggle").first.click()
    # Row visually disabled
    assert page.locator(".brain-row.disabled").count() >= 1
    # Autosave + check server side
    page.wait_for_function(
        "() => { const d = document.querySelector('#contact-detail .dirty-dot');"
        " return d && !d.classList.contains('dirty'); }",
        timeout=4000,
    )
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    cid = next(c["id"] for c in contacts if c["name"] == "BrainDisable")
    c = json.load(urlopen(f"{base_url()}/api/contacts/{cid}"))
    assert c["brains"][0]["disabled"] is True


def test_brain_export_filename_uses_owner_name_and_id(page: Page, clean_state):
    """Export-brains download filename includes both the entity's name
    and the first 8 chars of its UUID, so two ``Alice`` contacts don't
    produce filename collisions."""
    _api_create_contact("ExportFile")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    cid = next(c["id"] for c in contacts if c["name"] == "ExportFile")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").filter(has_text="ExportFile").first.click()
    page.locator("h3:has-text('Brains')").scroll_into_view_if_needed()
    # Add a brain so export isn't blocked by emptiness.
    page.click('button:has-text("+ Add brain")')
    name_in = page.locator('.brain-row input[type="text"]').first
    name_in.click()
    name_in.type("B", delay=10)
    content_ta = page.locator('.brain-row textarea').first
    content_ta.click()
    content_ta.type("c", delay=10)
    page.wait_for_function(
        "() => { const d = document.querySelector('#contact-detail .dirty-dot');"
        " return d && !d.classList.contains('dirty'); }",
        timeout=4000,
    )

    # Trigger Export brains and capture the download.
    with page.expect_download() as download_info:
        page.locator(".brain-editor-actions").first.locator(
            'button:has-text("Export brains")'
        ).click()
    fname = download_info.value.suggested_filename
    assert "exportfile" in fname.lower()
    assert cid[:8] in fname
    assert fname.endswith("-brains.json")


def test_brain_import_via_editor_appends_to_brains(page: Page, clean_state):
    """Click Import brains on a contact, drop a WI JSON, brain count
    rises by the file's entry count."""
    _api_create_contact("BrainImport")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    cid = next(c["id"] for c in contacts if c["name"] == "BrainImport")

    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").filter(has_text="BrainImport").first.click()
    page.locator("h3:has-text('Brains')").scroll_into_view_if_needed()

    # The brain editor hides its file <input> inside the Import button.
    # Click "Import brains…" and set the file on the now-visible hidden input.
    p = SCREENSHOTS / "_tmp_wi.json"
    p.write_bytes(_make_wi_json())
    # The hidden input is a sibling of the button in a span — set it directly.
    file_inputs = page.locator('.brain-editor-actions input[type="file"]')
    assert file_inputs.count() >= 1
    file_inputs.first.set_input_files(str(p))

    page.wait_for_function(
        "() => document.querySelectorAll('.brain-row').length >= 2",
        timeout=5000,
    )
    page.wait_for_function(
        "() => { const d = document.querySelector('#contact-detail .dirty-dot');"
        " return d && !d.classList.contains('dirty'); }",
        timeout=4000,
    )
    c = json.load(urlopen(f"{base_url()}/api/contacts/{cid}"))
    names = [b["name"] for b in c["brains"]]
    assert "WiEntryA" in names and "WiEntryB" in names


def test_brain_key_match_whole_words_toggle(page: Page, clean_state):
    """The new ·w· toggle on a literal key persists round-trip."""
    _api_create_contact("KeyWord")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.locator("#contact-list-body .list-row").filter(has_text="KeyWord").first.click()
    page.locator("h3:has-text('Brains')").scroll_into_view_if_needed()
    page.click('button:has-text("+ Add brain")')
    page.wait_for_selector(".brain-row")
    name_in = page.locator('.brain-row input[type="text"]').first
    name_in.type("WW", delay=10)
    content_ta = page.locator('.brain-row textarea').first
    content_ta.type("body", delay=10)
    # Open the Activation section.
    page.locator(".brain-activation > summary").first.click()
    page.click('.brain-row button:has-text("+ Add key")')
    # Type the key.
    pattern_in = page.locator('.brain-key-row input[type="text"]').first
    pattern_in.type("the", delay=10)
    # Click the ·w· toggle (whole-words).
    page.locator('.brain-key-toggles button:has-text("·w·")').first.click()
    page.wait_for_function(
        "() => { const d = document.querySelector('#contact-detail .dirty-dot');"
        " return d && !d.classList.contains('dirty'); }",
        timeout=4000,
    )
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    cid = next(c["id"] for c in contacts if c["name"] == "KeyWord")
    c = json.load(urlopen(f"{base_url()}/api/contacts/{cid}"))
    assert c["brains"][0]["keys"][0]["match_whole_words"] is True


# ---------------------------------------------------------------------------
# Duplicate button: contacts / users / scenarios / brain libraries each get
# a "Duplicate" header button between Export and Delete that clones the
# entity (and its files) under a "(N)" suffix.
# ---------------------------------------------------------------------------


def _list_row_titles(page: Page, list_body_id: str) -> list[str]:
    # ``.row-title-text`` is the inner name span (consistent across contact /
    # user / scenario / library views); ``.row-title`` itself may include
    # sibling text like the library row's brain-count badge.
    return page.eval_on_selector_all(
        f"#{list_body_id} .list-row .row-title-text",
        "els => els.map(e => e.textContent)",
    )


def test_duplicate_contact_via_button(page: Page, clean_state):
    """Clicking Duplicate on a contact creates "{name} (1)" and lands the
    user on the copy."""
    _api_create_contact("DupAlice")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")
    page.click("#contact-list-body .list-row")
    page.wait_for_selector("#contact-detail .page-header h2")

    page.click('#contact-detail button:has-text("Duplicate")')
    # The detail header should re-render onto the copy.
    page.wait_for_function(
        "() => document.querySelector('#contact-detail .page-header h2')"
        ".textContent.includes('DupAlice (1)')",
        timeout=3000,
    )
    titles = _list_row_titles(page, "contact-list-body")
    assert "DupAlice" in titles
    assert "DupAlice (1)" in titles


def test_duplicate_contact_twice_increments_suffix(page: Page, clean_state):
    """Two clicks on Duplicate produce "(1)" then "(2)" — the route walks
    past an existing collision."""
    _api_create_contact("DupBob")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")

    # Click the original (DupBob) by row.
    page.locator("#contact-list-body .list-row .row-title", has_text="DupBob").first.click()
    page.wait_for_selector("#contact-detail .page-header h2")
    page.click('#contact-detail button:has-text("Duplicate")')
    page.wait_for_function(
        "() => document.querySelector('#contact-detail .page-header h2')"
        ".textContent.includes('DupBob (1)')",
        timeout=3000,
    )

    # Re-select the original — the detail pane is on the copy now.
    page.locator(
        "#contact-list-body .list-row .row-title",
        has_text="DupBob",
    ).filter(has_not_text="(1)").first.click()
    page.wait_for_function(
        "() => document.querySelector('#contact-detail .page-header h2')"
        ".textContent.trim() === 'DupBob'",
        timeout=3000,
    )
    page.click('#contact-detail button:has-text("Duplicate")')
    page.wait_for_function(
        "() => document.querySelector('#contact-detail .page-header h2')"
        ".textContent.includes('DupBob (2)')",
        timeout=3000,
    )

    titles = _list_row_titles(page, "contact-list-body")
    assert "DupBob" in titles
    assert "DupBob (1)" in titles
    assert "DupBob (2)" in titles


def test_duplicate_user_via_button(page: Page, clean_state):
    _api_create_user("DupPersona")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="users"]')
    page.wait_for_selector("#user-list-body .list-row")
    # The seeded Anon user is also present; click the DupPersona row.
    page.locator(
        "#user-list-body .list-row .row-title",
        has_text="DupPersona",
    ).first.click()
    page.wait_for_selector("#user-detail .page-header h2")

    page.click('#user-detail button:has-text("Duplicate")')
    page.wait_for_function(
        "() => document.querySelector('#user-detail .page-header h2')"
        ".textContent.includes('DupPersona (1)')",
        timeout=3000,
    )
    titles = _list_row_titles(page, "user-list-body")
    assert "DupPersona" in titles
    assert "DupPersona (1)" in titles


def test_duplicate_scenario_via_button(page: Page, clean_state):
    _api_create_scenario("DupForest")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="scenarios"]')
    page.wait_for_selector("#scenario-list-body .list-row")
    page.click("#scenario-list-body .list-row")
    page.wait_for_selector("#scenario-detail .page-header h2")

    page.click('#scenario-detail button:has-text("Duplicate")')
    page.wait_for_function(
        "() => document.querySelector('#scenario-detail .page-header h2')"
        ".textContent.includes('DupForest (1)')",
        timeout=3000,
    )
    titles = _list_row_titles(page, "scenario-list-body")
    assert "DupForest" in titles
    assert "DupForest (1)" in titles


def test_duplicate_brain_library_via_button(page: Page, clean_state):
    _api_create_library("DupBible")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="libraries"]')
    page.wait_for_selector("#library-list-body .list-row")
    page.click("#library-list-body .list-row")
    page.wait_for_selector("#library-detail .page-header h2")

    page.click('#library-detail button:has-text("Duplicate")')
    page.wait_for_function(
        "() => document.querySelector('#library-detail .page-header h2')"
        ".textContent.includes('DupBible (1)')",
        timeout=3000,
    )
    titles = _list_row_titles(page, "library-list-body")
    assert "DupBible" in titles
    assert "DupBible (1)" in titles


# ===========================================================================
# List-pane virtualization (chats / contacts / users / scenarios / libraries).
#
# The chat-message virtualizer is covered above (``test_virtualization_*``
# and friends) — those are about the per-chat message tree. The tests
# below cover the list-pane virtualizers: each list tab mounts a bounded
# number of DOM rows regardless of how many entities are seeded, scrolling
# slides the window, and clicking a row in the middle of a long list
# activates the right entity (not whichever row the virtualizer parked at
# index 0).
# ===========================================================================


_VVIEW_H = 800
_VROWS_PER_VP = _VVIEW_H // 60


def _v_mounted(page: Page, body_id: str) -> int:
    """Count rows the virtualizer has mounted under ``virt-list-inner``.
    Skips the empty-state / footer divs the consumer appends below."""
    return page.evaluate(
        f"document.querySelectorAll('#{body_id} .virt-list-inner [data-virt-row]').length"
    )


def _v_scroll(page: Page, body_id: str, to) -> None:
    if to == 'bottom':
        page.evaluate(
            f"((b) => {{ b.scrollTop = b.scrollHeight - b.clientHeight; }})"
            f"(document.getElementById('{body_id}'))"
        )
    else:
        page.evaluate(
            f"document.getElementById('{body_id}').scrollTop = {int(to)};"
        )


def _v_seed_chats(n: int) -> list[str]:
    from urllib.request import Request
    _api_create_contact("Alpha")
    user_id = _api_create_user("Me")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "Alpha")
    ids = []
    for i in range(n):
        body = json.dumps({
            "contact_id": contact_id, "user_id": user_id,
            "title": f"Chat {i:03d}",
        }).encode()
        r = urlopen(Request(
            f"{base_url()}/api/chats", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=3)
        ids.append(json.loads(r.read())["id"])
    return ids


def _v_seed_contacts(n: int) -> None:
    for i in range(n):
        _api_create_contact(f"Contact {i:03d}")


def _v_seed_users(n: int) -> None:
    for i in range(n):
        _api_create_user(f"User {i:03d}")


def _v_seed_scenarios(n: int) -> None:
    for i in range(n):
        _api_create_scenario(f"Scenario {i:03d}")


def _v_seed_libraries(n: int) -> None:
    for i in range(n):
        _api_create_library(f"Library {i:03d}")


def test_chat_list_virtualizes_long_list(page: Page, clean_state):
    """120 chats seeded → chat list mounts well under 80 DOM rows.
    Verifies the server's first page (50 rows) lands and only a
    viewport-worth gets mounted."""
    _v_seed_chats(120)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(200)
    mounted = _v_mounted(page, "chat-list-body")
    assert mounted < 80, f"chat-list mounted: {mounted}"
    assert mounted >= _VROWS_PER_VP // 2


def test_chat_list_paginates_on_scroll(page: Page, clean_state):
    """Server pageSize = 50. With 120 seeded chats, scrolling to the
    bottom triggers the next-page fetches; oldest 'Chat 000' appears."""
    _v_seed_chats(120)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(300)

    for _ in range(8):
        _v_scroll(page, "chat-list-body", to='bottom')
        page.wait_for_timeout(300)

    inner_height = page.evaluate(
        "document.querySelector('#chat-list-body .virt-list-inner').offsetHeight"
    )
    assert inner_height > 4000, f"inner height too short: {inner_height}px"

    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Chat 000')",
        timeout=5000,
    )


def test_chat_list_click_middle_row_activates(page: Page, clean_state):
    """Clicking a non-first row in a long list activates THAT chat.
    Scrolls the list body so the target row is in the virtualizer's
    mount window — ``scroll_into_view_if_needed`` doesn't help when the
    row doesn't exist in the DOM yet."""
    _v_seed_chats(80)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(300)
    # Chat 060 is the 19th-from-top under updated_at desc (079, 078, …).
    # ~72 px per row × 18 prior rows ≈ 1300 px scroll.
    _v_scroll(page, "chat-list-body", 1200)
    page.wait_for_timeout(200)
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Chat 060')",
        timeout=3000,
    )
    target = page.locator(
        "#chat-list-body .list-row:has-text('Chat 060')"
    ).first
    target.click()
    page.wait_for_selector(
        "#chat-list-body .list-row.active:has-text('Chat 060')",
        timeout=3000,
    )
    # Router slug is contact-user-id8 (not the chat title). Confirm the
    # URL routed to a chat detail page and the chat-view mounted.
    page.wait_for_function(
        "() => location.pathname.startsWith('/chats/') && location.pathname !== '/chats'",
        timeout=3000,
    )
    page.wait_for_selector(".chat-view", timeout=5000)


def test_chat_list_search_filters_via_server(page: Page, clean_state):
    """The debounced search input pushes ``q=`` to the server; mounted
    DOM row count drops to exactly 1 when the query matches one chat."""
    _v_seed_chats(60)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(300)
    search = page.locator(".list-pane .list-search").first
    search.fill("Chat 042")
    page.wait_for_timeout(500)
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Chat 042')",
        timeout=3000,
    )
    mounted = _v_mounted(page, "chat-list-body")
    assert mounted == 1, f"expected 1 matching row, got {mounted}"


@pytest.mark.parametrize("kind, body_id, tab_btn, seeder, sample_label, detail_id", [
    ("contacts", "contact-list-body", '.rail-btn[data-tab="contacts"]', _v_seed_contacts, "Contact 060", "contact-detail"),
    ("users", "user-list-body", '.rail-btn[data-tab="users"]', _v_seed_users, "User 060", "user-detail"),
    ("scenarios", "scenario-list-body", '.rail-btn[data-tab="scenarios"]', _v_seed_scenarios, "Scenario 060", "scenario-detail"),
    ("libraries", "library-list-body", '.rail-btn[data-tab="libraries"]', _v_seed_libraries, "Library 060", "library-detail"),
])
def test_non_chat_list_virtualizes(
    page: Page, clean_state, kind, body_id, tab_btn, seeder, sample_label, detail_id,
):
    """All four non-chat list panes mount a bounded DOM regardless of
    how many entities are seeded."""
    seeder(80)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click(tab_btn)
    page.wait_for_selector(f"#{body_id} [data-virt-row]", timeout=8000)
    page.wait_for_timeout(200)
    mounted = _v_mounted(page, body_id)
    assert mounted < 80, f"{kind}: too many DOM rows mounted: {mounted}"
    assert mounted >= _VROWS_PER_VP // 2, f"{kind}: too few mounted: {mounted}"
    _v_scroll(page, body_id, to='bottom')
    page.wait_for_timeout(200)
    mounted_bottom = _v_mounted(page, body_id)
    assert mounted_bottom < 80, f"{kind}: bottom mounted: {mounted_bottom}"


@pytest.mark.parametrize("kind, body_id, tab_btn, seeder, sample_label, detail_id", [
    ("contacts", "contact-list-body", '.rail-btn[data-tab="contacts"]', _v_seed_contacts, "Contact 040", "contact-detail"),
    ("users", "user-list-body", '.rail-btn[data-tab="users"]', _v_seed_users, "User 040", "user-detail"),
    ("scenarios", "scenario-list-body", '.rail-btn[data-tab="scenarios"]', _v_seed_scenarios, "Scenario 040", "scenario-detail"),
    ("libraries", "library-list-body", '.rail-btn[data-tab="libraries"]', _v_seed_libraries, "Library 040", "library-detail"),
])
def test_non_chat_list_click_middle_row(
    page: Page, clean_state, kind, body_id, tab_btn, seeder, sample_label, detail_id,
):
    """Clicking a middle row activates the right entity AND the detail
    pane mounts (i.e. ``await api.getX(id)`` resolved + the editor
    rendered for THAT entity, not whichever was at row 0).

    The default sort is ``added desc`` (newest first). The seeder
    creates names 000..069 in order, so ``X 069`` sits at the top and
    ``X 040`` lives ~29 rows down — under the viewport with virt mount.
    Scroll the body manually so the virtualizer mounts the row, then
    locate + click.
    """
    seeder(70)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.click(tab_btn)
    page.wait_for_selector(f"#{body_id} [data-virt-row]", timeout=8000)
    page.wait_for_timeout(200)
    _v_scroll(page, body_id, 1800)
    page.wait_for_timeout(200)
    page.wait_for_selector(
        f"#{body_id} .list-row:has-text('{sample_label}')",
        timeout=5000,
    )
    target = page.locator(f"#{body_id} .list-row:has-text('{sample_label}')").first
    target.click()
    page.wait_for_selector(
        f"#{body_id} .list-row.active:has-text('{sample_label}')",
        timeout=3000,
    )
    page.wait_for_selector(
        f"#{detail_id} .page-header h2:has-text('{sample_label}')",
        timeout=5000,
    )


def test_chat_list_mobile_virtualizes_and_taps(browser, clean_state):
    """Mobile-viewport chat list — bounded DOM under the smaller
    viewport and a tap on a middle row opens the chat-view full-screen."""
    _v_seed_chats(80)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True, has_touch=True, device_scale_factor=2.0,
    )
    try:
        page = ctx.new_page()
        page.goto(base_url())
        page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
        page.wait_for_timeout(300)
        mounted = _v_mounted(page, "chat-list-body")
        assert mounted < 50, f"mobile chat-list mounted: {mounted}"
        target = page.locator("#chat-list-body .list-row").nth(2)
        target.tap()
        page.wait_for_selector(".chat-view", timeout=5000)
    finally:
        ctx.close()


def test_contacts_list_mobile_virtualizes(browser, clean_state):
    """Mobile contacts pane — bounded DOM at touch-viewport size."""
    _v_seed_contacts(80)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True, has_touch=True, device_scale_factor=2.0,
    )
    try:
        page = ctx.new_page()
        page.goto(base_url())
        page.wait_for_selector("#rail")
        page.click('.rail-btn[data-tab="contacts"]')
        page.wait_for_selector("#contact-list-body [data-virt-row]", timeout=8000)
        page.wait_for_timeout(300)
        mounted = _v_mounted(page, "contact-list-body")
        assert mounted < 50, f"mobile contacts mounted: {mounted}"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# Filter / search / sort interaction with the paged virtualizer at scale.
# These tests exercise the server-side pagination shape AND the
# virtualizer's behavior once the user has filtered / sorted / searched
# into a result set bigger than a single page.
# ---------------------------------------------------------------------------


def _v_seed_chats_across_contacts(n_per_contact: int, n_contacts: int = 3) -> dict:
    """Seed ``n_per_contact * n_contacts`` chats split evenly across
    ``n_contacts`` contacts. Returns ``{contact_name: contact_id}``."""
    from urllib.request import Request
    user_id = _api_create_user("Me")
    contact_ids: dict[str, str] = {}
    for i in range(n_contacts):
        name = f"Contact-{chr(ord('A') + i)}"  # Contact-A, Contact-B, ...
        _api_create_contact(name)
        contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        cid = next(c["id"] for c in contacts if c["name"] == name)
        contact_ids[name] = cid
    counter = 0
    for name, cid in contact_ids.items():
        for j in range(n_per_contact):
            body = json.dumps({
                "contact_id": cid, "user_id": user_id,
                "title": f"{name} chat {j:03d}",
            }).encode()
            urlopen(Request(
                f"{base_url()}/api/chats", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            ), timeout=3)
            counter += 1
    return contact_ids


def test_chat_list_sort_by_title_asc_then_scroll(page: Page, clean_state):
    """120 chats, sort by name ascending → "Chat 000" lands at the top,
    "Chat 119" at the bottom. Scrolling through the list passes the
    sorted middle ("Chat 060", "Chat 080") in order.
    """
    _v_seed_chats(120)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(300)

    # Open the sort menu and pick "Name". Direction defaults to ascending
    # for the name mode (see ``defaultSortDirection`` in util.js).
    page.locator(".list-pane .list-sort-mode").first.click()
    page.locator(".list-pane .list-sort-menu-item:has-text('Name')").first.click()
    page.wait_for_timeout(500)  # debounce + refetch

    # Top of the list: alphabetically first = "Chat 000".
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Chat 000')",
        timeout=3000,
    )

    # Scroll past page 0 — server returned the first 50 sorted asc, the
    # virtualizer should fetch page 1 (50-99) and we can see "Chat 060".
    _v_scroll(page, "chat-list-body", 4000)
    page.wait_for_timeout(400)
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Chat 060')",
        timeout=3000,
    )

    # Scroll to the bottom — "Chat 119" sits at the end of the asc sort.
    _v_scroll(page, "chat-list-body", to='bottom')
    page.wait_for_timeout(400)
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Chat 119')",
        timeout=3000,
    )


def test_chat_list_sort_direction_flips_ordering(page: Page, clean_state):
    """120 chats, switch sort to "Added" (created_at) and then toggle
    direction. Default desc → top row is the most recently created
    (Chat 119). Click the direction toggle → top is now Chat 000."""
    _v_seed_chats(120)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(300)

    # Pick "Added" mode (defaults to desc).
    page.locator(".list-pane .list-sort-mode").first.click()
    page.locator(".list-pane .list-sort-menu-item:has-text('Added')").first.click()
    page.wait_for_timeout(500)
    # Top should be the most-recently-created chat.
    first_row_text = page.locator("#chat-list-body .list-row").first.inner_text()
    assert "Chat 119" in first_row_text, f"top row before flip: {first_row_text!r}"

    # Toggle direction → ascending. Top is now Chat 000.
    page.locator(".list-pane .list-sort-dir").first.click()
    page.wait_for_timeout(500)
    first_row_text = page.locator("#chat-list-body .list-row").first.inner_text()
    assert "Chat 000" in first_row_text, f"top row after flip: {first_row_text!r}"


def test_chat_list_filter_by_contact_scopes_results(page: Page, clean_state):
    """120 chats spread across 3 contacts (40 each). Setting the
    ``filterContactId`` filter via state — that's the mechanism the UI
    uses when the user clicks a contact's chip in another view —
    should drop the visible list to just that contact's 40 chats,
    scrollable end to end."""
    contact_ids = _v_seed_chats_across_contacts(n_per_contact=40, n_contacts=3)
    target_id = contact_ids["Contact-B"]
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(300)

    # Apply the filter through setState — same path the chip-set flow
    # uses (filterContactId is wired into the chat-list params).
    page.evaluate(
        f"import('/static/state.js').then(m => m.setState({{ filterContactId: '{target_id}' }}))"
    )
    page.wait_for_timeout(600)
    # Chip is rendered above the list.
    page.wait_for_selector(".list-pane .chip:has-text('Contact: Contact-B')", timeout=3000)
    # Only Contact-B chats remain — confirm by spot-checking visible titles
    # AND scrolling to the bottom shows the last Contact-B chat.
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Contact-B chat')",
        timeout=3000,
    )
    # Contact-A / Contact-C must NOT be on screen.
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll("
        "  '#chat-list-body .list-row'"
        ")).every(r => !r.textContent.includes('Contact-A chat')"
        "   && !r.textContent.includes('Contact-C chat'))",
        timeout=3000,
    )

    # Scroll through the filtered set — the virtualizer must still paginate
    # the server-side filtered result (40 rows = page 0 only; the inner
    # height should be roughly 40 * (64+8) ≈ 2880 px).
    inner_height = page.evaluate(
        "document.querySelector('#chat-list-body .virt-list-inner').offsetHeight"
    )
    # Allow a generous margin for row-height measurement settling.
    assert 1800 < inner_height < 4500, (
        f"filtered inner height: {inner_height}px (expected ~40 rows)"
    )

    _v_scroll(page, "chat-list-body", to='bottom')
    page.wait_for_timeout(400)
    # The earliest Contact-B chat (created first → lowest updated_at) sits
    # at the bottom under default desc sort.
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Contact-B chat 000')",
        timeout=3000,
    )


def test_chat_list_search_result_scrolls_through_pages(page: Page, clean_state):
    """Search for a prefix that matches >1 server page worth of chats
    (Chat 000-099 = 100 matches out of 120 seeded → 2 server pages).
    Scrolling through the filtered set triggers the page-1 fetch and
    rows from that page mount correctly."""
    _v_seed_chats(120)
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(300)

    search = page.locator(".list-pane .list-search").first
    search.fill("Chat 0")
    page.wait_for_timeout(500)  # debounce + server fetch

    # Default sort (updated_at desc + creation order) puts "Chat 099" at
    # the top of the search results — the most-recently-created chat
    # whose title contains "Chat 0".
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Chat 099')",
        timeout=3000,
    )

    # 100 matches at ~72 px per row ≈ 7200 px. Scrolling far down should
    # trigger the second-page fetch and reveal early-numbered chats.
    _v_scroll(page, "chat-list-body", to='bottom')
    page.wait_for_timeout(500)
    page.wait_for_selector(
        "#chat-list-body .list-row:has-text('Chat 000')",
        timeout=3000,
    )

    # Chats outside the match set (Chat 100-119) must NOT appear at all
    # — the filter is server-side, not client-side.
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll("
        "  '#chat-list-body .list-row'"
        ")).every(r => !r.textContent.includes('Chat 100')"
        "   && !r.textContent.includes('Chat 119'))",
        timeout=3000,
    )


def test_chat_list_filter_plus_sort_combine(page: Page, clean_state):
    """Filter by contact AND switch sort mode in the same session.
    The two paramsets must combine on the wire — server returns only
    Contact-B's chats in name-asc order."""
    contact_ids = _v_seed_chats_across_contacts(n_per_contact=40, n_contacts=3)
    target_id = contact_ids["Contact-B"]
    page.set_viewport_size({"width": 1200, "height": _VVIEW_H})
    page.goto(base_url())
    page.wait_for_selector("#chat-list-body [data-virt-row]", timeout=8000)
    page.wait_for_timeout(300)

    page.evaluate(
        f"import('/static/state.js').then(m => m.setState({{ filterContactId: '{target_id}' }}))"
    )
    page.wait_for_timeout(500)
    page.locator(".list-pane .list-sort-mode").first.click()
    page.locator(".list-pane .list-sort-menu-item:has-text('Name')").first.click()
    page.wait_for_timeout(500)

    # Top of the filtered+sorted-asc list: alphabetically lowest title
    # among Contact-B's chats — "Contact-B chat 000".
    first_row_text = page.locator("#chat-list-body .list-row").first.inner_text()
    assert "Contact-B chat 000" in first_row_text, (
        f"top row of filter+sort: {first_row_text!r}"
    )


# ---------------------------------------------------------------------------
# Recent-chats sidebars on the four detail views — regression for the
# ``[object HTMLDivElement]`` bug where ``recentBox.replaceChildren(h3,
# items.map(...))`` passed an array as a single varargs arg and the
# browser called ``toString`` on it. The fix spreads the array.
# ---------------------------------------------------------------------------


def _api_create_chat_with(*, contact_id, user_id, title,
                          scenario_id=None, brain_library_ids=None) -> str:
    from urllib.request import Request
    body = {
        "contact_id": contact_id, "user_id": user_id, "title": title,
    }
    if scenario_id:
        body["scenario_id"] = scenario_id
    if brain_library_ids:
        body["brain_library_ids"] = brain_library_ids
    r = urlopen(Request(
        f"{base_url()}/api/chats", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    ), timeout=3)
    return json.loads(r.read())["id"]


@pytest.mark.parametrize("kind, tab_btn, detail_id, header_text", [
    ("contact", '.rail-btn[data-tab="contacts"]',
     "contact-detail", "Recent chats with"),
    ("user", '.rail-btn[data-tab="users"]',
     "user-detail", "Recent chats with this persona"),
    ("scenario", '.rail-btn[data-tab="scenarios"]',
     "scenario-detail", "Recent chats in this scenario"),
    ("library", '.rail-btn[data-tab="libraries"]',
     "library-detail", "Chats that use this library"),
])
def test_recent_chats_section_renders_dom_rows_not_object_strings(
    page: Page, clean_state, kind, tab_btn, detail_id, header_text,
):
    """Each detail view's recent-chats section async-fetches the relevant
    chats and renders them as DOM rows. Regression check: the section's
    inner text must NEVER contain ``[object HTMLDivElement]`` — that
    string signals the rendered array was stringified rather than
    spread into ``replaceChildren``.

    Also asserts at least one seeded chat's title shows up in the
    section, proving the rows are real DOM and not just a fallback
    'No chats yet.' string."""
    # Common: contact + user so any chat is creatable.
    _api_create_contact("FixtureContact")
    user_id = _api_create_user("FixtureUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    contact_id = next(c["id"] for c in contacts if c["name"] == "FixtureContact")

    # Kind-specific entity + chats that reference it through the right field.
    if kind == "contact":
        target_id = contact_id
        for i in range(2):
            _api_create_chat_with(contact_id=contact_id, user_id=user_id,
                                  title=f"Recent {i}")
    elif kind == "user":
        target_id = user_id
        for i in range(2):
            _api_create_chat_with(contact_id=contact_id, user_id=user_id,
                                  title=f"Recent {i}")
    elif kind == "scenario":
        target_id = _api_create_scenario("FixtureScenario")
        for i in range(2):
            _api_create_chat_with(
                contact_id=contact_id, user_id=user_id,
                scenario_id=target_id, title=f"Recent {i}",
            )
    elif kind == "library":
        lib = _api_create_library("FixtureLibrary")
        target_id = lib["id"]
        for i in range(2):
            _api_create_chat_with(
                contact_id=contact_id, user_id=user_id,
                brain_library_ids=[target_id], title=f"Recent {i}",
            )

    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click(tab_btn)
    # Pick the seeded entity (it's the only one).
    page.wait_for_selector(f"#{detail_id.split('-')[0]}-list-body .list-row",
                           timeout=5000)
    page.locator(
        f"#{detail_id.split('-')[0]}-list-body .list-row"
    ).first.click()
    page.wait_for_selector(f"#{detail_id} .page-header h2", timeout=5000)

    # Wait for the async ``api.listChats({...})`` call inside the detail
    # view to land and populate the recent-chats section.
    recent = page.locator(
        f"#{detail_id} .section:has(h3:has-text(\"{header_text}\"))"
    ).first
    recent.scroll_into_view_if_needed()
    # Loading → settled. Wait for at least one row to mount.
    page.wait_for_selector(
        f"#{detail_id} .section:has(h3:has-text(\"{header_text}\")) .list-row",
        timeout=5000,
    )

    text = recent.inner_text()
    # Regression guard: spreads must not collapse into stringified arrays.
    assert "object HTMLDivElement" not in text, (
        f"recent-chats section ({kind}) shows the stringified array "
        f"bug: {text!r}"
    )
    # And the seeded chat titles actually render.
    assert "Recent 0" in text, f"missing seeded chat in section: {text!r}"
    assert "Recent 1" in text, f"missing seeded chat in section: {text!r}"


# ---------------------------------------------------------------------------
# Per-tab scroll preservation. ``app.js`` snapshots ``.list-body`` and
# ``.page-scroll`` scrollTop on every tab switch and restores them on
# return. The page-scroll case is fragile because each list+detail tab's
# ``refreshDetail`` is async (awaits ``api.getX(id)`` before appending the
# editor DOM) — a naive synchronous restore would run before ``.page-scroll``
# exists, so the restore has to wait for the element to appear.
# ---------------------------------------------------------------------------


def _api_put_contact(c: dict) -> None:
    from urllib.request import Request

    urlopen(Request(
        f"{base_url()}/api/contacts/{c['id']}",
        data=json.dumps(c).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )).read()


def test_page_scroll_preserved_across_tab_switch(page: Page, clean_state):
    """Scroll the contact editor's ``.page-scroll``, switch to chats, switch
    back. The scrollTop must be restored — the editor's async fetch means
    ``.page-scroll`` isn't in the DOM at the synchronous restore moment, so
    the restore has to wait for it to appear."""
    _api_create_contact("Scrolly")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
    cid = next(c["id"] for c in contacts if c["name"] == "Scrolly")
    full = _api_get_contact(cid)
    big = "Lorem ipsum dolor sit amet. " * 40
    full["persona"] = big
    full["appearance"] = big
    full["description"] = big
    _api_put_contact(full)

    # 700px viewport ensures the page-scroll is actually overflowing.
    page.set_viewport_size({"width": 1280, "height": 700})
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")
    page.locator("#contact-list-body .list-row").first.click()
    page.wait_for_selector("#contact-detail .page-scroll")
    # Detail view has further async work (recent-chats fetch); wait a beat.
    page.wait_for_timeout(400)

    target = 400
    page.evaluate(
        "(t) => document.querySelector('#contact-detail .page-scroll').scrollTop = t",
        target,
    )
    before = page.evaluate(
        "document.querySelector('#contact-detail .page-scroll').scrollTop"
    )
    assert before == target, f"could not scroll page (got {before})"

    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_timeout(200)
    page.click('.rail-btn[data-tab="contacts"]')
    # ``.page-scroll`` is appended only after the async ``refreshDetail``
    # resolves; the restore has to wait for it.
    page.wait_for_selector("#contact-detail .page-scroll")
    page.wait_for_function(
        "(t) => {"
        "  const el = document.querySelector('#contact-detail .page-scroll');"
        "  return el && Math.abs(el.scrollTop - t) < 5;"
        "}",
        arg=target,
        timeout=3000,
    )
    after = page.evaluate(
        "document.querySelector('#contact-detail .page-scroll').scrollTop"
    )
    assert abs(after - target) < 5, (
        f"page-scroll not restored after tab switch: expected ~{target}, got {after}"
    )


def test_list_body_scroll_preserved_across_tab_switch(page: Page, clean_state):
    """Companion to the page-scroll test: the ``.list-body`` half of the
    same machinery uses a synchronously-built element, so it should work
    too — this pins it down."""
    # Need enough rows to make the list-body actually scrollable. Virtualized
    # row height ~60px; 40 rows comfortably exceeds 700px viewport.
    for i in range(40):
        _api_create_contact(f"Bulk{i:02d}")

    page.set_viewport_size({"width": 1280, "height": 700})
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")
    page.wait_for_timeout(300)

    target = 500
    page.evaluate(
        "(t) => document.querySelector('#contact-list-body').scrollTop = t",
        target,
    )
    before = page.evaluate(
        "document.querySelector('#contact-list-body').scrollTop"
    )
    assert before > 0, f"could not scroll list-body (got {before})"

    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_timeout(200)
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_selector("#contact-list-body .list-row")
    page.wait_for_function(
        "(t) => {"
        "  const el = document.querySelector('#contact-list-body');"
        "  return el && Math.abs(el.scrollTop - t) < 5;"
        "}",
        arg=before,
        timeout=3000,
    )
    after = page.evaluate(
        "document.querySelector('#contact-list-body').scrollTop"
    )
    assert abs(after - before) < 5, (
        f"list-body not restored after tab switch: expected ~{before}, got {after}"
    )


# ===========================================================================
# Context presets
# ===========================================================================


def _api_set_provider_mode(mode: str) -> None:
    from urllib.request import Request

    body = json.dumps({"provider_mode": mode}).encode()
    req = Request(
        f"{base_url()}/api/settings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    urlopen(req, timeout=3).read()


def _api_list_context_presets() -> list[dict]:
    with urlopen(f"{base_url()}/api/context-presets", timeout=3) as r:
        return json.load(r)


def test_context_presets_rail_hidden_in_aer_mode(page: Page):
    _api_set_provider_mode("aetherroom")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    btn = page.locator('.rail-btn[data-tab="contextPresets"]')
    assert btn.is_hidden(), "Context presets rail entry should hide in AER mode"


def test_context_presets_rail_visible_in_generic_mode(page: Page):
    _api_set_provider_mode("generic")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.wait_for_function(
        "() => document.documentElement.dataset.providerMode === 'generic'",
        timeout=3000,
    )
    btn = page.locator('.rail-btn[data-tab="contextPresets"]')
    assert btn.is_visible(), "Context presets rail entry should show in Generic mode"


def test_context_presets_open_default_seeded(page: Page):
    _api_set_provider_mode("generic")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="contextPresets"]')
    page.wait_for_selector("#context-preset-list-body", timeout=5000)
    # Default seed should be in the list.
    page.wait_for_selector("#context-preset-list-body .list-row", timeout=5000)
    rows = page.locator("#context-preset-list-body .list-row")
    assert rows.count() >= 1
    # Click into the Default row.
    page.click(
        "#context-preset-list-body .list-row:has-text('Default')",
        timeout=3000,
    )
    page.wait_for_selector(".context-preset-block", timeout=5000)
    block_cards = page.locator(".context-preset-block")
    # Seeded Default preset: Intro, Character, User, Scenario, Lore, HTML output.
    assert block_cards.count() == 6
    shot(page, "context-presets-default")


def test_context_presets_block_enabled_toggle_updates_preview(page: Page):
    _api_set_provider_mode("generic")
    page.goto(f"{base_url()}/context-presets")
    page.wait_for_selector("#context-preset-list-body .list-row", timeout=5000)
    page.click("#context-preset-list-body .list-row:has-text('Default')")
    page.wait_for_selector(".context-preset-block", timeout=5000)
    page.wait_for_selector(".preview-text", timeout=5000)
    # Preview should mention CHARACTER before we toggle anything.
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.preview-text'))"
        ".some(t => t.value.includes('<CHARACTER>'))",
        timeout=5000,
    )
    # Disable the second block (Character). The Enabled control is now
    # an icon-btn (eye toggle), not a checkbox.
    page.evaluate(
        """() => {
            const cards = document.querySelectorAll('.context-preset-block');
            const btn = cards[1].querySelector('.enabled-toggle');
            btn.click();
        }""",
    )
    # Wait for debounced preview to refresh (300 ms debounce + render).
    page.wait_for_timeout(700)
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.preview-text'))"
        ".every(t => !t.value.includes('<CHARACTER>'))",
        timeout=5000,
    )


def test_context_presets_duplicate_creates_copy(page: Page):
    _api_set_provider_mode("generic")
    page.goto(f"{base_url()}/context-presets")
    page.wait_for_selector("#context-preset-list-body .list-row", timeout=5000)
    before = len(_api_list_context_presets())
    page.click("#context-preset-list-body .list-row:has-text('Default')")
    page.wait_for_selector(".page-header", timeout=5000)
    page.click(".page-header button:has-text('Duplicate')")
    # List grows by one and the new row is highlighted.
    page.wait_for_function(
        f"() => document.querySelectorAll('#context-preset-list-body .list-row').length >= {before + 1}",
        timeout=5000,
    )


def test_context_presets_floating_message_with_divider(page: Page):
    _api_set_provider_mode("generic")
    page.goto(f"{base_url()}/context-presets")
    page.wait_for_selector("#context-preset-list-body .list-row", timeout=5000)
    page.click("#context-preset-list-body .list-row:has-text('Default')")
    page.wait_for_selector(".context-preset-message", timeout=5000)
    # The seeded preset has one floating message (Prefill) and no static
    # messages — so the dashed divider should NOT appear (one section is
    # empty). Add a static message and re-check the divider.
    page.evaluate(
        """() => {
            const btns = Array.from(document.querySelectorAll('button'))
              .filter(b => b.textContent.trim().endsWith('Add message'));
            if (btns.length) btns[btns.length - 1].click();
        }""",
    )
    # Wait for the new static message to render.
    page.wait_for_function(
        "() => document.querySelectorAll('.context-preset-message').length >= 2",
        timeout=3000,
    )
    # The divider should now be present between the new static message
    # and the seeded floating Prefill.
    assert page.locator(".float-divider").count() >= 1


def test_context_presets_macros_help_modal(page: Page):
    _api_set_provider_mode("generic")
    page.goto(f"{base_url()}/context-presets")
    page.wait_for_selector("#context-preset-list-body .list-row", timeout=5000)
    page.click("#context-preset-list-body .list-row:has-text('Default')")
    page.wait_for_selector(".page-header", timeout=5000)
    page.click(".page-header button:has-text('Macros')")
    page.wait_for_selector(".macros-help", timeout=5000)
    body = page.locator(".macros-help").inner_text()
    # The new entries should all appear.
    for token in ("if", "/if", "else", "eq", "neq", "and", "or",
                  "global_brains", "chat.title", "chat.tags"):
        assert token in body, f"macros help missing {token!r}"
    shot(page, "context-presets-macros-help")


def _open_default_context_preset(page: Page) -> None:
    """Navigate to the Context Presets tab and open the seeded Default."""
    _api_set_provider_mode("generic")
    page.goto(f"{base_url()}/context-presets")
    page.wait_for_selector("#context-preset-list-body .list-row", timeout=5000)
    page.click("#context-preset-list-body .list-row:has-text('Default')")
    page.wait_for_selector(".context-preset-block", timeout=5000)


def test_context_preset_card_uses_icon_toggles_not_checkboxes(page: Page):
    """The Enabled toggle on block + message cards is an icon-btn (eye),
    not a checkbox. The Float toggle is a separate icon-btn with the
    ``.float-toggle`` class. Drag-handle ``.move-grip`` is present too.
    Don't refactor these toggles to checkboxes — the icon-btn layout
    is deliberate (compact header strip, no label clutter).
    """
    _open_default_context_preset(page)
    # System prompt section has at least one block — assert no checkboxes
    # inside the block header, and the eye-icon toggle is present.
    block_header = page.locator(".context-preset-block-header").first
    assert block_header.locator("input[type='checkbox']").count() == 0, (
        "Enabled toggle on block header should be an icon-btn, not a checkbox"
    )
    assert block_header.locator(".enabled-toggle").count() >= 1
    assert block_header.locator(".move-grip").count() == 1


def test_context_preset_additional_messages_help_button_and_toggles(page: Page):
    """The Additional Messages section has the new (?) help button next
    to its heading, and adding a message gives us a card whose header
    has both ``.enabled-toggle`` and ``.float-toggle`` icon-btn shapes
    plus a ``.move-grip`` drag handle. Footer hosts Role + Mode."""
    _open_default_context_preset(page)
    # Help (?) button next to the "Additional messages" heading.
    add_msgs_help_row = page.locator(".help-row:has(h3:text('Additional messages'))")
    assert add_msgs_help_row.locator(".help-btn").count() == 1, (
        "Additional messages heading should expose a (?) helpDetails button"
    )
    # And next to "System prompt" too.
    sys_help_row = page.locator(".help-row:has(h3:text('System prompt'))")
    assert sys_help_row.locator(".help-btn").count() == 1

    # Add a new message — fresh card with the new header + footer.
    # The button lives outside any ``.section`` since the layout
    # promoted each message card to be its own top-level section.
    page.click(".context-preset-msg-add button:has-text('Add message')")
    page.wait_for_selector(".context-preset-message", timeout=3000)
    msg = page.locator(".context-preset-message").first
    header = msg.locator(".context-preset-message-header")
    assert header.locator(".move-grip").count() == 1
    assert header.locator(".enabled-toggle").count() == 1
    assert header.locator(".float-toggle").count() == 1
    # Role and Mode are in the footer, not the header.
    footer = msg.locator(".context-preset-message-footer")
    assert footer.locator(".context-preset-message-role").count() == 1
    assert footer.locator(".context-preset-message-mode").count() == 1
    # Depth is hidden by default (float off).
    assert footer.locator(".context-preset-message-depth").count() == 0
    # Toggle float on — depth appears.
    msg.locator(".float-toggle").click()
    page.wait_for_timeout(150)
    msg2 = page.locator(".context-preset-message").first
    assert msg2.locator(".context-preset-message-footer .context-preset-message-depth").count() == 1


def test_context_preset_name_input_keeps_focus_across_debounce(page: Page):
    """Generation/preset card name input must not defocus across the
    debounced save's roundtrip. Don't call the parent ``onChange``
    (which rerenders the editor and tears down the live ``<input>``)
    from inside the save closure — the input must stay mounted so a
    follow-up keystroke appends rather than landing in nowhere."""
    _api_set_provider_mode("aetherroom")
    page.goto(base_url())
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector(".preset-card-name", timeout=5000)
    name_input = page.locator(".preset-card-name").first
    name_input.click()
    name_input.fill("")
    name_input.type("Hi", delay=40)
    # Past the 400ms debounce — save will fire, but the input must keep
    # focus and the next keystroke should append.
    page.wait_for_timeout(700)
    name_input.type("There", delay=40)
    assert name_input.input_value() == "HiThere", (
        f"focus stolen mid-typing: got {name_input.input_value()!r}"
    )


def test_custom_provider_name_input_keeps_focus_per_keystroke(page: Page):
    """Custom-provider name input must not defocus per keystroke — the
    rerender must defer to ``onChange`` (blur) so the input stays mounted
    across typing."""
    # Pre-stage settings: provider_mode=generic + generic.provider=
    # openai_compatible so the "OpenAI-compatible endpoints" sub-section
    # renders immediately on Settings open. Avoids walking the
    # active-provider dropdown (popover-portalled, fiddly to drive).
    from urllib.request import Request
    named = {"base_url": "", "api_token": "", "model_id": "",
             "cache_minutes": None, "streaming": True,
             "brain_message_role": "system", "context_preset_id": None}
    body = json.dumps({
        "provider_mode": "generic",
        "generic": {
            "provider": "openai_compatible",
            "novelai": named, "openrouter": named, "nanogpt": named,
            "openai_compatible": {"custom_providers": [], "active_id": None},
        },
    }).encode()
    req = Request(f"{base_url()}/api/settings", data=body,
                  headers={"Content-Type": "application/json"}, method="PUT")
    urlopen(req, timeout=3).read()

    page.goto(base_url())
    page.click('.rail-btn[data-tab="settings"]')
    page.wait_for_selector("button:has-text('Add custom provider')", timeout=5000)
    page.click("button:has-text('Add custom provider')")
    page.wait_for_selector(".preset-card-name", timeout=3000)
    name = page.locator(".preset-card-name").first
    name.click()
    name.fill("")
    name.type("ABC", delay=40)
    assert name.input_value() == "ABC", (
        f"defocus mid-typing: got {name.input_value()!r}"
    )


def test_tab_switch_then_sibling_nav_loads_deeper_messages(
    page: Page, clean_state,
):
    """Switching off the chat tab and back must not strand the
    virtualisation observer on the destroyed root element. After tab
    return, switching to a sibling whose subtree extends the active
    path must reveal those deeper messages without requiring a reload.

    Tree:
        ""  → m_root1                              (short branch)
        ""  → m_root2 → m_root2a → m_root2aa       (deep branch)
        ""  → m_root3                              (short branch)

    Start with m_root1 selected (path = 1 message). After tab-switch +
    return, swap root selection to m_root2 — the new path is 3
    messages, all of which must render.
    """
    from urllib.request import Request

    _api_create_contact("NavChar")
    user_id = _api_create_user("NavUser")
    contact_id = next(
        c["id"] for c in json.load(urlopen(f"{base_url()}/api/contacts", timeout=3))
        if c["name"] == "NavChar"
    )
    chat_id = _api_post_chat(contact_id, user_id)

    def _post_msg(parent_id, text, sender="user"):
        payload = {
            "sender": sender,
            "sender_name": "NavUser" if sender == "user" else "NavChar",
            "body": [{"text": text, "emotion": "neutral"}],
        }
        if parent_id is not None:
            payload["parent_id"] = parent_id
        body = json.dumps(payload).encode()
        return json.loads(urlopen(Request(
            f"{base_url()}/api/chats/{chat_id}/messages", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=3).read())["id"]

    def _select(parent_id, child_id):
        urlopen(Request(
            f"{base_url()}/api/chats/{chat_id}/select",
            data=json.dumps({"parent_id": parent_id, "child_id": child_id}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=3).read()

    # Three root siblings; second one gets a 3-deep chain.
    m_root1 = _post_msg(None, "root one")
    m_root2 = _post_msg(None, "root two")
    m_root2a = _post_msg(m_root2, "two child", sender="contact")
    m_root2aa = _post_msg(m_root2a, "two grandchild")
    m_root3 = _post_msg(None, "root three")

    # Pre-set selected_child along the deep branch so when we flip root
    # selection to m_root2 the full chain materialises.
    _select(m_root2, m_root2a)
    _select(m_root2a, m_root2aa)
    # And start on the short branch.
    _select(None, m_root1)

    # Open the chat — initial path is just [m_root1].
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.locator("#chat-list-body .list-row").first.click()
    page.wait_for_selector(".chat-messages .msg", timeout=5000)
    rendered = page.locator(".chat-messages > .msg").count()
    assert rendered == 1, f"expected 1 rendered msg initially, got {rendered}"

    # Switch off the chat tab and back. The chat-messages root element
    # gets destroyed + recreated; the IntersectionObserver instance from
    # before is now pointed at a detached node.
    page.click('.rail-btn[data-tab="contacts"]')
    page.wait_for_timeout(150)
    page.click('.rail-btn[data-tab="chats"]')
    page.wait_for_selector(".chat-messages .msg", timeout=5000)

    # Flip root selection to the deep branch and force a reload through
    # the client (same path the sibling-arrow click takes). The new
    # path is 3 messages — without the observer rebind, the bottom
    # spacer-driven slide never fires, so m_root2a + m_root2aa stay
    # stranded in the spacer.
    _select(None, m_root2)
    page.evaluate(f"window._aetherChatId = {chat_id!r};")
    page.evaluate(
        "async () => {"
        "  const { loadActiveChat } = await import('/static/views/chat.js');"
        "  await loadActiveChat(window._aetherChatId, "
        "    { preserveScroll: true, flush: true });"
        "}",
    )

    # After loadActiveChat resolves, the IntersectionObserver callback
    # may need a frame or two to fire and run _virtSlide. Wait for the
    # deepest descendant to land in the DOM.
    page.wait_for_selector(
        f".chat-messages .msg[data-msg-id='{m_root2aa}']",
        timeout=3000,
    )
    # And the middle of the chain is there too.
    assert page.locator(f".chat-messages .msg[data-msg-id='{m_root2a}']").count() == 1
    assert page.locator(f".chat-messages .msg[data-msg-id='{m_root2}']").count() == 1


# ---------------------------------------------------------------------------
# Chat-input menu (Continue / Impersonate / Attach)
# ---------------------------------------------------------------------------


def _open_chat_for_menu(page: Page, chat_id: str) -> None:
    """Land directly on a chat in the chats tab. Wait for boot's
    settings fetch to land before calling setState — otherwise no
    subscribers are registered yet and the chat view never mounts."""
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
    page.wait_for_selector(".chat-input-menu-btn", timeout=10000)


def test_menu_button_opens_popover_with_impersonate(page: Page, clean_state):
    """Clicking the menu button reveals a popover above the input with
    at least an Impersonate row. Continue appears when the active tip is
    a contact message — seed the contact with a greeting so the chat
    has a contact-side message at the root."""
    from urllib.request import Request
    body = json.dumps({"name": "MenuAlice", "greeting": "hi"}).encode()
    req = Request(f"{base_url()}/api/contacts", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    contact_id = json.loads(urlopen(req).read())["id"]
    user_id = _api_create_user("MenuUser")
    chat_id = _api_post_chat(contact_id, user_id)

    _open_chat_for_menu(page, chat_id)
    page.click(".chat-input-menu-btn")
    page.wait_for_selector(".chat-input-menu-panel", timeout=2000)
    panel_text = page.locator(".chat-input-menu-panel").text_content()
    assert "Impersonate" in panel_text
    assert "Continue" in panel_text
    assert "View model context" in panel_text
    assert "Request image" not in panel_text

    page.get_by_role("button", name="View model context", exact=True).click()
    page.wait_for_selector(".chat-context-modal", timeout=2000)
    page.wait_for_selector(".chat-context-preview-text", timeout=2000)
    context_modal = page.locator(".chat-context-modal")
    assert "does not contact the model" in context_modal.text_content()
    assert page.get_by_role("button", name="Next reply", exact=True).count() == 1
    assert page.get_by_role("button", name="Continue", exact=True).count() == 1
    assert page.get_by_role("button", name="Impersonate", exact=True).count() == 1
    context_text = page.locator(".chat-context-preview-text").text_content()
    assert context_text.startswith("[gMASK]<sop><|system|>")
    assert context_text.endswith("MenuAlice:")
    page.get_by_role("button", name="Close", exact=True).click()


def test_message_control_requests_image_from_historical_point(page: Page, clean_state):
    _api_create_contact("HistoricalImageAlice")
    user_id = _api_create_user("HistoricalImageUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    contact_id = next(
        contact["id"]
        for contact in contacts
        if contact["name"] == "HistoricalImageAlice"
    )
    chat_id = _api_post_chat(contact_id, user_id)
    first_id = _api_post_user_message(chat_id, "HISTORICAL FIRST MOMENT")
    _api_post_user_message(
        chat_id,
        "NEWER SECOND MOMENT MUST BE EXCLUDED",
        parent_id=first_id,
    )

    _open_chat_for_menu(page, chat_id)
    first_message = page.locator(f'.msg[data-msg-id="{first_id}"]')
    first_message.hover()
    first_message.get_by_role(
        "button", name="Request image from this message", exact=True,
    ).click()
    page.wait_for_selector(".image-request-modal", timeout=2000)
    page.wait_for_selector(".image-context-preview-text", timeout=2000)
    assert "Request an image after message 1" in page.locator(
        ".image-request-modal"
    ).text_content()
    modal_text = page.locator(".image-request-modal").text_content()
    assert "Raw model output" in modal_text
    assert "Raw model output (including the seeded <think>)" not in modal_text
    assert "Separate provider reasoning channel" not in modal_text
    assert "Fresh request" not in modal_text
    assert "Continuation request" not in modal_text
    assert page.locator(
        ".image-request-modal .image-request-label"
    ).all_text_contents() == [
        "Raw model output", "Image resolution", "Image prompt",
    ]
    prompt = page.locator(".image-context-preview-text").text_content()
    assert "HISTORICAL FIRST MOMENT" in prompt
    assert "NEWER SECOND MOMENT MUST BE EXCLUDED" not in prompt
    assert "Resolution: 832 × 1216 pixels" in prompt

    page.locator(".image-request-aspect", has_text="Landscape").click()
    page.wait_for_function(
        """() => document.querySelector('.image-context-preview-text')
          ?.textContent.includes('Resolution: 1216 × 832 pixels')""",
        timeout=2000,
    )


def test_menu_button_continue_hidden_when_tip_is_user(page: Page, clean_state):
    """The Continue row is hidden when the active tip is a user
    message (continue only makes sense after a contact message)."""
    _api_create_contact("MenuUserTipAlice")
    user_id = _api_create_user("MenuUserTipUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    contact_id = next(c["id"] for c in contacts if c["name"] == "MenuUserTipAlice")
    # Create a chat with NO greeting → empty active path.
    from urllib.request import Request
    body = json.dumps({
        "contact_id": contact_id, "user_id": user_id, "title": "T",
    }).encode()
    req = Request(f"{base_url()}/api/chats", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    chat_id = json.loads(urlopen(req).read())["id"]
    # Append a user message → tip is now a user-side message.
    _api_post_user_message(chat_id, "hi")

    _open_chat_for_menu(page, chat_id)
    page.click(".chat-input-menu-btn")
    page.wait_for_selector(".chat-input-menu-panel", timeout=2000)
    panel_text = page.locator(".chat-input-menu-panel").text_content()
    assert "Impersonate" in panel_text
    assert "Continue" not in panel_text


def test_menu_button_in_aer_mode_does_not_morph(page: Page, clean_state):
    """AER mode: the menu button stays as the ``more`` icon when the
    popover is open, and re-clicking it closes the popover."""
    # Settings persist across tests on the shared server — make sure
    # we're actually in AER mode (a prior generic-mode test may have
    # flipped the switch).
    from urllib.request import Request
    body = json.dumps({"provider_mode": "aetherroom"}).encode()
    req = Request(f"{base_url()}/api/settings", data=body,
                  headers={"Content-Type": "application/json"}, method="PUT")
    urlopen(req, timeout=3).read()

    _api_create_contact("AerMenuAlice")
    user_id = _api_create_user("AerMenuUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    contact_id = next(c["id"] for c in contacts if c["name"] == "AerMenuAlice")
    chat_id = _api_post_chat(contact_id, user_id)

    _open_chat_for_menu(page, chat_id)
    # Stamp the icon shape before opening so we can compare.
    icon_before = page.evaluate(
        "document.querySelector('.chat-input-menu-btn svg').innerHTML"
    )
    page.click(".chat-input-menu-btn")
    page.wait_for_selector(".chat-input-menu-panel", timeout=2000)
    icon_open = page.evaluate(
        "document.querySelector('.chat-input-menu-btn svg').innerHTML"
    )
    # AER mode: same icon both states.
    assert icon_before == icon_open, "AER mode shouldn't morph the menu button"
    # Re-clicking closes the popover.
    page.click(".chat-input-menu-btn")
    page.wait_for_function(
        "() => !document.querySelector('.chat-input-menu-panel')",
        timeout=2000,
    )


def test_menu_popover_closes_on_escape_and_outside_click(page: Page, clean_state):
    """Esc closes the popover; clicking outside the popover/button does too."""
    _api_create_contact("MenuCloseAlice")
    user_id = _api_create_user("MenuCloseUser")
    contacts = json.load(urlopen(f"{base_url()}/api/contacts"))
    contact_id = next(c["id"] for c in contacts if c["name"] == "MenuCloseAlice")
    chat_id = _api_post_chat(contact_id, user_id)

    _open_chat_for_menu(page, chat_id)
    page.click(".chat-input-menu-btn")
    page.wait_for_selector(".chat-input-menu-panel", timeout=2000)
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => !document.querySelector('.chat-input-menu-panel')",
        timeout=2000,
    )

    page.click(".chat-input-menu-btn")
    page.wait_for_selector(".chat-input-menu-panel", timeout=2000)
    # Outside click — anywhere on the body that isn't the panel or the btn.
    page.click("#main")
    page.wait_for_function(
        "() => !document.querySelector('.chat-input-menu-panel')",
        timeout=2000,
    )


def test_picker_popover_survives_soft_keyboard_on_mobile(browser, clean_state):
    """A dropdown inside a modal must not vanish when the on-screen keyboard
    appears. Chrome on Android shrinks the layout viewport for the keyboard,
    which fires ``window.resize``; the picker has to reposition itself into
    the space that's left instead of closing. Autofocusing the search field
    (which raises that keyboard the instant the popover opens) is also off on
    touch, so the option list is what the user actually gets to see."""
    for name in ("KbAlpha", "KbBravo", "KbCharlie", "KbDelta", "KbEcho"):
        _api_create_contact(name)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True, has_touch=True, device_scale_factor=2.0,
    )
    try:
        page = ctx.new_page()
        page.goto(base_url())
        page.wait_for_selector("#rail")
        page.click('.rail-btn[data-tab="chats"]')
        page.click('[title="New chat"]')
        page.wait_for_selector(".modal .avatar-picker-trigger", timeout=5000)

        trigger = page.locator(".modal .avatar-picker-trigger").first
        trigger.tap()
        page.wait_for_selector("body > .avatar-picker-popover", timeout=2000)
        page.wait_for_timeout(250)   # let the post-open rAF settle
        assert page.evaluate(
            "document.activeElement.classList.contains('avatar-picker-search')"
        ) is False, "search field must not autofocus on touch (raises the keyboard)"

        # The keyboard comes up: Android shrinks the layout viewport.
        page.set_viewport_size({"width": 390, "height": 420})
        page.wait_for_timeout(250)
        pop = page.locator("body > .avatar-picker-popover")
        assert pop.count() == 1, "popover closed itself when the keyboard opened"
        box = pop.first.bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= 420, (
            f"popover not repositioned into the visible area: {box}"
        )

        # Still usable: tapping an option selects it.
        page.locator(
            "body > .avatar-picker-popover .avatar-picker-option", has_text="KbDelta"
        ).first.tap()
        page.wait_for_timeout(200)
        assert "KbDelta" in trigger.inner_text()
        assert page.locator("body > .avatar-picker-popover").count() == 0
    finally:
        ctx.close()


def test_picker_popover_still_closes_on_ancestor_scroll(page: Page, clean_state):
    """The repositioning added for the soft keyboard must not cost the
    desktop behaviour: scrolling the modal behind an open picker closes it."""
    _api_create_contact("ScrollCloseAlice")
    page.goto(base_url())
    page.wait_for_selector("#rail")
    page.click('.rail-btn[data-tab="chats"]')
    page.click('[title="New chat"]')
    page.wait_for_selector(".modal .avatar-picker-trigger", timeout=5000)
    page.locator(".modal .avatar-picker-trigger").first.click()
    page.wait_for_selector("body > .avatar-picker-popover", timeout=2000)
    page.evaluate("document.querySelector('.modal').dispatchEvent(new Event('scroll'))")
    page.wait_for_function(
        "() => !document.querySelector('body > .avatar-picker-popover')",
        timeout=2000,
    )


def test_help_popover_stays_in_visible_area_on_mobile(browser):
    """The (?) panel is body-portalled and fixed-positioned, so it has to be
    clamped to the *visible* box, not the layout viewport: when the on-screen
    keyboard shrinks the former, its anchor can end up off-screen and a panel
    placed relative to that anchor would hang past the bottom edge."""
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True, has_touch=True, device_scale_factor=2.0,
    )
    try:
        page = ctx.new_page()
        page.goto(base_url())
        page.wait_for_selector("#rail")
        page.click('.rail-btn[data-tab="settings"]')
        page.wait_for_selector(".help-btn", timeout=5000)
        btn = page.locator(".help-btn").first
        btn.scroll_into_view_if_needed()
        btn.tap()
        page.wait_for_selector("body > .help-popover", timeout=2000)

        page.set_viewport_size({"width": 390, "height": 420})
        page.wait_for_timeout(300)
        panel = page.locator("body > .help-popover")
        assert panel.count() == 1, "help panel closed itself on the viewport shrink"
        box = panel.first.bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= 421, (
            f"help panel outside the visible area: {box}"
        )
    finally:
        ctx.close()
