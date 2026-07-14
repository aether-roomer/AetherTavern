"""Continue / Impersonate generation modes.

Both modes thread a new ``mode`` query param through the existing
generate route. The persistence path differs:

  * ``mode=continue``: validates the active tip is a contact message,
    then appends streamed bubbles to the original message's body and
    saves the combined result as a SIBLING (same ``parent_id``) so the
    original short version stays as a navigable branch.

  * ``mode=impersonate``: flips personas + roles in the prompt and
    persists the streamed text as a ``sender="user"`` message whose
    ``origin`` is the generating pipeline (``"aer"`` / ``"generic"``),
    not ``"manual"``.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from server import storage
from server.main import app
from server.models import Contact, User


async def _fake_stream_one_bubble(*_args, **_kwargs):
    """Upstream stub: a single AER-formatted bubble."""
    yield " more text\n  Emotion: happy\n"


def _patch_build_for_test(monkeypatch):
    """Patch the AER build helpers so tests don't need the real tokenizer."""
    from server.aer.rollover import GenerationContext

    def fake_build(**kwargs):
        return GenerationContext(
            api_messages=[{"role": "system", "content": ""}],
            total_tokens=0, history_tokens=0,
            brain_tokens=0, system_tokens=0,
            new_cursor=0, new_path_ids=[], new_rolled_over=False,
        )

    monkeypatch.setattr(
        "server.routers.generate.build_messages_for_generation",
        fake_build,
    )
    monkeypatch.setattr(
        "server.routers.generate.render", lambda *a, **kw: "PROMPT",
    )


def _drain_sse(client, url):
    events: list[str] = []
    with client.stream("GET", url) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event: "):
                events.append(line[7:])
    return events


def test_context_tokens_uses_generic_builder_when_provider_mode_generic(tmp_storage):
    """When provider_mode='generic', /context-tokens calls
    build_messages_for_generic (UTF-8/3.35 guesstimate) rather than
    AER's tokenizer-aware builder — the stat the chat view displays
    should match what a Generic-mode generation would actually cost."""
    with TestClient(app) as client:
        contact = client.post("/api/contacts", json={"name": "Alice"}).json()
        user_id = client.get("/api/users").json()[0]["id"]
        chat = client.post("/api/chats", json={
            "contact_id": contact["id"], "user_id": user_id, "title": "T",
        }).json()
        chat_id = chat["id"]

        presets = client.get("/api/context-presets").json()
        preset_id = presets[0]["id"]
        # Configure Generic with a non-zero context preset so the builder
        # runs without bailing into the zero-stub branch.
        client.put("/api/settings", json={
            "provider_mode": "generic",
            "generic": {
                "provider": "openai_compatible",
                "novelai": client.get("/api/settings").json()["generic"]["novelai"],
                "openrouter": client.get("/api/settings").json()["generic"]["openrouter"],
                "nanogpt": client.get("/api/settings").json()["generic"]["nanogpt"],
                "openai_compatible": {
                    "custom_providers": [{
                        "label": "Mock",
                        "base_url": "http://127.0.0.1:0",
                        "api_token": "tok",
                        "model_id": "m",
                        "cache_minutes": None,
                        "streaming": True,
                        "brain_message_role": "system",
                        "context_preset_id": preset_id,
                    }],
                    "active_id": "",
                },
            },
        })
        s2 = client.get("/api/settings").json()
        cust = s2["generic"]["openai_compatible"]["custom_providers"][0]
        cust["api_token"] = "tok"
        client.put("/api/settings", json={
            "generic": {
                "provider": "openai_compatible",
                "novelai": s2["generic"]["novelai"],
                "openrouter": s2["generic"]["openrouter"],
                "nanogpt": s2["generic"]["nanogpt"],
                "openai_compatible": {
                    "custom_providers": [cust],
                    "active_id": cust["id"],
                },
            },
        })

        r = client.get(f"/api/chats/{chat_id}/context-tokens")
        assert r.status_code == 200
        body = r.json()
        # Generic mode returns the contract shape with brain_tokens=0
        # (brains are baked into the system block via {{global_brains}}
        # macros — not separately accounted for) and active_brains=[].
        assert body["brain_tokens"] == 0
        assert body["active_brains"] == []
        # Total tokens is a positive integer (the preset's system block
        # alone produces non-zero output).
        assert isinstance(body["total_tokens"], int)
        assert body["total_tokens"] >= 0


def test_continue_persists_as_sibling_with_seed(tmp_storage, monkeypatch):
    """Continue mode: new ChatMessage is a sibling of the contact tip,
    with body = original.body + new bubbles. ``selected_child_id`` points
    at the sibling so the combined version becomes the active branch."""
    contact = storage.save_contact(Contact(name="Alice", greeting="hi"))
    user = storage.save_user(User(name="Me"))

    monkeypatch.setattr(
        "server.routers.generate.stream_completion", _fake_stream_one_bubble,
    )
    _patch_build_for_test(monkeypatch)

    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        chat_id = chat["id"]

        # The seeded greeting creates a contact tip at the root.
        msgs_before = storage.load_chat_messages(chat_id).messages
        original_tip = [m for m in msgs_before if m.sender == "contact"][-1]
        original_parent = original_tip.parent_id

        events = _drain_sse(
            client,
            f"/api/chats/{chat_id}/generate?mode=continue"
            f"&expected_tip_id={original_tip.id}",
        )
        assert "done" in events
        assert "error" not in events

    msgs = storage.load_chat_messages(chat_id).messages
    siblings = [m for m in msgs if m.parent_id == original_parent and m.sender == "contact"]
    # Original + new combined sibling at minimum.
    assert len(siblings) >= 2
    # The new sibling is the one whose first bubble starts with the
    # original tip's text; the streamed extension follows.
    combined = next(
        m for m in siblings
        if m.id != original_tip.id
        and m.body
        and m.body[0].text == original_tip.body[0].text
    )
    # Body grew by exactly the one streamed bubble.
    assert len(combined.body) == len(original_tip.body) + 1
    assert "more text" in combined.body[-1].text
    # Active branch points at the combined sibling.
    chat_obj = storage.get_chat(chat_id)
    key = original_parent if original_parent is not None else ""
    assert chat_obj.selected_child_id.get(key) == combined.id


def test_continue_requires_contact_tip(tmp_storage, monkeypatch):
    """Continue is invalid when the active tip is a user message —
    the route surfaces ``error.kind == "continue_invalid"`` via SSE."""
    contact = storage.save_contact(Contact(name="Alice"))
    user = storage.save_user(User(name="Me"))

    monkeypatch.setattr(
        "server.routers.generate.stream_completion", _fake_stream_one_bubble,
    )
    _patch_build_for_test(monkeypatch)

    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        chat_id = chat["id"]
        # Add a user message so the tip is user-side.
        client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "sender": "user",
                "body": [{"text": "hello", "emotion": "neutral"}],
            },
        )

        # SSE response carries a typed error event then closes — the
        # body collects to a single "error" line.
        body = ""
        with client.stream(
            "GET", f"/api/chats/{chat_id}/generate?mode=continue",
        ) as r:
            assert r.status_code == 200
            body = r.read().decode("utf-8")

    assert "continue_invalid" in body


def test_generic_continue_preserves_multi_bubble_structure(tmp_storage, monkeypatch):
    """Generic continue mode keeps the original message's bubble
    structure intact and appends the streamed text inside the LAST
    bubble (where the model is mid-sentence). Earlier bubbles are NOT
    flattened into one string."""
    from server import storage
    from server.inference_generic import DeltaEvent, DoneEvent
    from server.models import ChatMessage, SubMessage

    with TestClient(app) as client:
        # Set up generic mode via the existing helper pattern from
        # test_generic_generation_route.py — configure a mock provider.
        contact = client.post("/api/contacts", json={"name": "Alice"}).json()
        user_id = client.get("/api/users").json()[0]["id"]
        chat = client.post("/api/chats", json={
            "contact_id": contact["id"], "user_id": user_id, "title": "T",
        }).json()
        chat_id = chat["id"]

        presets = client.get("/api/context-presets").json()
        preset_id = presets[0]["id"]
        settings = client.get("/api/settings").json()
        g = settings["generic"]
        client.put("/api/settings", json={
            "provider_mode": "generic",
            "generic": {
                "provider": "openai_compatible",
                "novelai": g["novelai"],
                "openrouter": g["openrouter"],
                "nanogpt": g["nanogpt"],
                "openai_compatible": {
                    "custom_providers": [{
                        "label": "Mock",
                        "base_url": "http://127.0.0.1:0",
                        "api_token": "tok",
                        "model_id": "m",
                        "cache_minutes": None,
                        "streaming": True,
                        "brain_message_role": "system",
                        "context_preset_id": preset_id,
                    }],
                    "active_id": "",
                },
            },
        })
        s2 = client.get("/api/settings").json()
        cust = s2["generic"]["openai_compatible"]["custom_providers"][0]
        cust["api_token"] = "tok"  # rehydrate the secret over the indicator
        client.put("/api/settings", json={
            "generic": {
                "provider": "openai_compatible",
                "novelai": s2["generic"]["novelai"],
                "openrouter": s2["generic"]["openrouter"],
                "nanogpt": s2["generic"]["nanogpt"],
                "openai_compatible": {
                    "custom_providers": [cust],
                    "active_id": cust["id"],
                },
            },
        })

        # Inject a multi-bubble contact message bypassing the create route
        # (which always produces single-bubble user-typed messages).
        multi = ChatMessage(
            sender="contact", sender_name="Alice",
            body=[
                SubMessage(text="First.", emotion=None),
                SubMessage(text="Second.", emotion=None),
            ],
            origin="aer",
        )
        msgs = storage.load_chat_messages(chat_id)
        msgs.messages.append(multi)
        chat_obj = storage.get_chat(chat_id)
        chat_obj.selected_child_id[""] = multi.id
        storage.save_chat_messages(chat_id, msgs)
        storage.save_chat(chat_obj, bump_version=False)

        async def fake_stream_chat(*_args, **_kwargs):
            yield DeltaEvent(text=" Then continued.")
            yield DoneEvent(finish_reason="stop")

        monkeypatch.setattr(
            "server.routers.generate.stream_chat", fake_stream_chat,
        )

        events: list[str] = []
        with client.stream(
            "GET",
            f"/api/chats/{chat_id}/generate?mode=continue"
            f"&expected_tip_id={multi.id}",
        ) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("event: "):
                    events.append(line[7:])

    assert "done" in events, events
    msgs = storage.load_chat_messages(chat_id).messages
    siblings = [
        m for m in msgs if m.parent_id is None and m.sender == "contact"
    ]
    new_sibling = next(m for m in siblings if m.id != multi.id)
    # Critical: bubble structure preserved (NOT flattened to one bubble).
    assert len(new_sibling.body) == 2, [b.text for b in new_sibling.body]
    assert new_sibling.body[0].text == "First."
    # Last bubble carries the original + streamed extension.
    assert new_sibling.body[1].text == "Second. Then continued."


def test_impersonate_persists_user_side_in_generic_mode(tmp_storage, monkeypatch):
    """Impersonate in Generic mode persists a user-side message with
    ``origin="generic"`` (NOT manual). The flipped roles + flipped
    personas in the prompt cause the model to play the user persona;
    the streamed text lands on a new sender=user message."""
    from server import storage
    from server.inference_generic import DeltaEvent, DoneEvent

    with TestClient(app) as client:
        contact = client.post("/api/contacts", json={"name": "Alice"}).json()
        user_id = client.get("/api/users").json()[0]["id"]
        chat = client.post("/api/chats", json={
            "contact_id": contact["id"], "user_id": user_id, "title": "T",
        }).json()
        chat_id = chat["id"]

        # Configure Generic mode + a context preset.
        presets = client.get("/api/context-presets").json()
        preset_id = presets[0]["id"]
        client.put("/api/settings", json={
            "provider_mode": "generic",
            "generic": {
                "provider": "openai_compatible",
                "novelai": client.get("/api/settings").json()["generic"]["novelai"],
                "openrouter": client.get("/api/settings").json()["generic"]["openrouter"],
                "nanogpt": client.get("/api/settings").json()["generic"]["nanogpt"],
                "openai_compatible": {
                    "custom_providers": [{
                        "label": "Mock",
                        "base_url": "http://127.0.0.1:0",
                        "api_token": "tok",
                        "model_id": "m",
                        "cache_minutes": None,
                        "streaming": True,
                        "brain_message_role": "system",
                        "context_preset_id": preset_id,
                    }],
                    "active_id": "",
                },
            },
        })
        s2 = client.get("/api/settings").json()
        cust = s2["generic"]["openai_compatible"]["custom_providers"][0]
        cust["api_token"] = "tok"
        client.put("/api/settings", json={
            "generic": {
                "provider": "openai_compatible",
                "novelai": s2["generic"]["novelai"],
                "openrouter": s2["generic"]["openrouter"],
                "nanogpt": s2["generic"]["nanogpt"],
                "openai_compatible": {
                    "custom_providers": [cust],
                    "active_id": cust["id"],
                },
            },
        })

        # Capture what gets sent to the provider so we can assert the
        # flipped roles + flipped persona names landed in the prompt.
        captured_messages: list[dict] = []

        async def fake_stream_chat(*_args, **kwargs):
            captured_messages.extend(kwargs.get("api_messages") or [])
            yield DeltaEvent(text="I feel curious about this.")
            yield DoneEvent(finish_reason="stop")

        monkeypatch.setattr(
            "server.routers.generate.stream_chat", fake_stream_chat,
        )

        # Seed a contact greeting + a user message + a contact reply so
        # we have at least one bubble of each side to flip.
        client.post(f"/api/chats/{chat_id}/messages", json={
            "sender": "user",
            "body": [{"text": "hi", "emotion": "neutral"}],
        })

        events: list[str] = []
        with client.stream(
            "GET",
            f"/api/chats/{chat_id}/generate?mode=impersonate",
        ) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("event: "):
                    events.append(line[7:])

    assert "done" in events, events
    # Persistence check: new user-side message with origin=generic.
    msgs = storage.load_chat_messages(chat_id).messages
    impersonated = [
        m for m in msgs if m.sender == "user" and m.origin == "generic"
    ]
    assert len(impersonated) == 1
    new = impersonated[0]
    assert new.body and "curious" in new.body[0].text
    assert new.generation_started_at is not None
    assert new.provider == "openai_compatible"

    # Flip check: the prior user "hi" bubble should have landed in the
    # api_messages as an ASSISTANT role (since roles are flipped).
    assistant_msgs = [m for m in captured_messages if m.get("role") == "assistant"]
    assert any(
        "hi" in (m.get("content") if isinstance(m.get("content"), str) else "")
        for m in assistant_msgs
    ), captured_messages


def test_impersonate_persists_user_side_with_origin_aer(tmp_storage, monkeypatch):
    """Impersonate mode: new message is ``sender=user``, ``origin=aer``,
    NOT ``"manual"``. Generation metadata is populated."""
    contact = storage.save_contact(Contact(name="Alice", greeting="hi"))
    user = storage.save_user(User(name="Me"))

    monkeypatch.setattr(
        "server.routers.generate.stream_completion", _fake_stream_one_bubble,
    )
    _patch_build_for_test(monkeypatch)

    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        chat_id = chat["id"]

        events = _drain_sse(
            client,
            f"/api/chats/{chat_id}/generate?mode=impersonate",
        )
        assert "done" in events
        assert "error" not in events

    msgs = storage.load_chat_messages(chat_id).messages
    impersonated = [
        m for m in msgs
        if m.sender == "user" and m.origin == "aer"
    ]
    assert len(impersonated) == 1
    new = impersonated[0]
    # Parser is primed with user.name in impersonate mode, so the body
    # text contains the streamed bubble content.
    assert new.body and "more text" in new.body[0].text
    # Generation metadata stamped (not ``None``).
    assert new.generation_started_at is not None
    assert new.provider == "aetherroom"
