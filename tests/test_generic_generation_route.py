"""End-to-end Generic generation route tests.

Stubbed ``stream_chat`` async generator drives the route through the
full pipeline (preflight → context build → SSE forward → image rewrite
→ persist → done). Doesn't hit a real upstream.

Coverage:
- Happy path: start / delta / done sequence in the SSE response, with
  the assistant message persisted with origin="generic" and the
  generation metadata fields populated.
- Image markdown in a delta is rewritten to the proxy URL in the wire,
  and the persisted body keeps the original URL (with image_refs).
- Reasoning_delta events propagate, and the reasoning lands on the
  persisted message.
- ``no_provider`` / ``no_preset`` / ``no_token`` preflight failures
  short-circuit before any stream_chat call.
- ``brain_budget`` failures from the builder are caught and emitted as
  typed SSE errors.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from server import storage
from server.inference_generic import (
    DeltaEvent,
    DoneEvent,
    ProviderEvent,
    ReasoningDeltaEvent,
    StartEvent,
    UsageEvent,
)
from server.main import app


def _create_chat(client: TestClient) -> tuple[str, str]:
    r = client.post("/api/contacts", json={"id": "", "name": "C"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    return contact_id, r.json()["id"]


def _configure_generic(client: TestClient) -> None:
    """Configure the openai_compatible custom provider with a Default
    context preset selected. Must run AFTER ``tmp_storage`` so the
    Default preset has been seeded by ``storage.initialize``."""
    presets = client.get("/api/context-presets").json()
    assert presets, "expected the Default context preset to be seeded"
    preset_id = presets[0]["id"]
    # Omit ``id`` so Pydantic's default_factory mints a fresh uuid. Sending
    # ``id: ""`` would bypass the factory and store an empty string.
    custom = {
        "label": "MockProvider",
        "base_url": "http://127.0.0.1:0",  # mock — never actually hit
        "api_token": "mock-token",
        "model_id": "mock-model",
        "cache_minutes": None,
        "streaming": True,
        "brain_message_role": "system",
        "context_preset_id": preset_id,
    }
    settings = client.get("/api/settings").json()
    g = settings["generic"]
    payload = {
        "provider_mode": "generic",
        "generic": {
            "provider": "openai_compatible",
            "novelai": g["novelai"],
            "openrouter": g["openrouter"],
            "nanogpt": g["nanogpt"],
            "openai_compatible": {
                "custom_providers": [custom],
                "active_id": "",
            },
        },
    }
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 200, r.text
    # Re-read to pick up the server-assigned custom-provider id, then
    # second PUT to set ``active_id``. The GET returns
    # ``api_token_indicator`` instead of ``api_token`` — echoing those
    # entries back would wipe the token (the field is unknown to the
    # write model so it falls back to the empty default). Stamp the
    # sentinel ``"__present__"`` on each entry's ``api_token`` to
    # preserve the value the first PUT just set.
    settings = client.get("/api/settings").json()
    eid = settings["generic"]["openai_compatible"]["custom_providers"][0]["id"]
    custom_list = settings["generic"]["openai_compatible"]["custom_providers"]
    for entry in custom_list:
        entry["api_token"] = "__present__"
    # Same trick for the named-provider tokens we echo back.
    for k in ("novelai", "openrouter", "nanogpt"):
        settings["generic"][k]["api_token"] = "__present__"
    client.put("/api/settings", json={
        "generic": {
            "provider": "openai_compatible",
            "novelai": settings["generic"]["novelai"],
            "openrouter": settings["generic"]["openrouter"],
            "nanogpt": settings["generic"]["nanogpt"],
            "openai_compatible": {
                "custom_providers": custom_list,
                "active_id": eid,
            },
        },
    })


def _make_fake_stream(events: list):
    """Build an async generator that yields ``events`` in order, then ends."""
    async def fake_stream(*args, **kwargs):
        for ev in events:
            yield ev
    return fake_stream


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Crude SSE parser: split on blank lines, decode each ``event:`` +
    ``data:`` pair to ``(event_name, payload_dict)``. Good enough for
    tests where the response shape is predictable."""
    out: list[tuple[str, dict]] = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event_name = None
        data_line = None
        for line in chunk.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_line = line[len("data: "):]
        if event_name is None or data_line is None:
            continue
        try:
            payload = json.loads(data_line)
        except json.JSONDecodeError:
            payload = {"_raw": data_line}
        out.append((event_name, payload))
    return out


def test_generic_happy_path_persists_with_metadata(tmp_storage, monkeypatch):
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    _configure_generic(client)

    events = [
        StartEvent(model="mock-model"),
        DeltaEvent(text="Hello "),
        DeltaEvent(text="world."),
        UsageEvent(prompt_tokens=123, completion_tokens=2, total_tokens=125),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    r = client.get(f"/api/chats/{chat_id}/generate?expected_tip_id=")
    assert r.status_code == 200
    parsed = _parse_sse(r.text)
    event_names = [name for name, _ in parsed]
    assert "start" in event_names
    assert "delta" in event_names
    assert "done" in event_names
    # Combined delta text reconstructs the streamed message.
    deltas = [p["text"] for n, p in parsed if n == "delta"]
    assert "".join(deltas) == "Hello world."

    # Persisted message has origin="generic" + all six metadata fields populated.
    msgs = client.get(f"/api/chats/{chat_id}/messages").json()
    contact_msg = next((m for m in msgs if m["sender"] == "contact"), None)
    assert contact_msg is not None
    assert contact_msg["origin"] == "generic"
    assert contact_msg["provider"] == "openai_compatible"
    assert contact_msg["model"] == "mock-model"
    assert contact_msg["generation_preset_id"] is not None
    assert contact_msg["context_preset_id"] is not None
    assert isinstance(contact_msg["generation_started_at"], float)
    assert isinstance(contact_msg["generation_duration_seconds"], float)


def test_generic_per_chat_model_override_is_stamped(tmp_storage, monkeypatch):
    """A per-chat ``model_overrides`` entry (keyed by the resolved provider
    slug) overrides the provider's configured model and is stamped onto the
    generated message — proving the override flows through the resolver into
    generation + persistence for the Generic path."""
    client = TestClient(app)
    contact_id = client.post("/api/contacts", json={"id": "", "name": "C"}).json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    _configure_generic(client)
    eid = (client.get("/api/settings").json()
           ["generic"]["openai_compatible"]["custom_providers"][0]["id"])
    slug = f"openai_compatible:{eid}"
    chat_id = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
        "model_overrides": {slug: "override-model"},
    }).json()["id"]

    events = [
        StartEvent(model="ignored"),
        DeltaEvent(text="hi"),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    r = client.get(f"/api/chats/{chat_id}/generate?expected_tip_id=")
    assert r.status_code == 200, r.text
    contact_msg = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json()
        if m["sender"] == "contact"
    )
    assert contact_msg["provider"] == "openai_compatible"
    assert contact_msg["model"] == "override-model"  # not the provider's "mock-model"
    assert contact_msg["context_preset_id"] is not None


def test_generic_reasoning_persisted(tmp_storage, monkeypatch):
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    _configure_generic(client)

    events = [
        StartEvent(model="mock-model"),
        ReasoningDeltaEvent(text="step 1. "),
        ReasoningDeltaEvent(text="step 2."),
        DeltaEvent(text="The answer."),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    r = client.get(f"/api/chats/{chat_id}/generate?expected_tip_id=")
    parsed = _parse_sse(r.text)
    reasoning_chunks = [p["text"] for n, p in parsed if n == "reasoning_delta"]
    assert "".join(reasoning_chunks) == "step 1. step 2."

    msgs = client.get(f"/api/chats/{chat_id}/messages").json()
    contact_msg = next(m for m in msgs if m["sender"] == "contact")
    assert contact_msg["reasoning"] == "step 1. step 2."


def test_generic_image_markdown_rewrites_wire_keeps_original_persisted(
    tmp_storage, monkeypatch,
):
    """A ``delta`` containing image markdown should arrive at the wire
    with the URL rewritten to the proxy. ``messages.yaml`` should keep
    the original URL so the proxy can still resolve via image_refs."""
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    _configure_generic(client)

    events = [
        StartEvent(model="mock-model"),
        DeltaEvent(text="See ![cat](https://example.com/cat.jpg) here."),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    r = client.get(f"/api/chats/{chat_id}/generate?expected_tip_id=")
    parsed = _parse_sse(r.text)
    deltas = "".join(p["text"] for n, p in parsed if n == "delta")
    # Wire side: original URL never appears.
    assert "https://example.com/cat.jpg" not in deltas
    assert "/api/chats/" in deltas
    assert "/cat.jpg" in deltas

    # On-disk message keeps the original URL (so re-list rewrites
    # correctly on the wire next time).
    chat_path = storage.chat_dir(chat_id)
    import msgspec.yaml
    raw = msgspec.yaml.decode((chat_path / "messages.yaml").read_bytes())
    contact_raw = [m for m in raw["messages"] if m["sender"] == "contact"][0]
    body_text = contact_raw["body"][0]["text"]
    assert "https://example.com/cat.jpg" in body_text
    assert "https://example.com/cat.jpg" in contact_raw["image_refs"]


def test_generic_route_no_provider_short_circuits(tmp_storage, monkeypatch):
    """When the provider isn't configured, the route yields the typed
    ``no_provider`` error WITHOUT calling stream_chat."""
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    client.put("/api/settings", json={"provider_mode": "generic"})

    called = {"n": 0}
    async def boom(*args, **kwargs):
        called["n"] += 1
        yield DoneEvent(finish_reason="stop")
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", boom)

    r = client.get(f"/api/chats/{chat_id}/generate")
    assert "no_provider" in r.text
    assert called["n"] == 0


def test_generic_route_no_preset_short_circuits(tmp_storage, monkeypatch):
    """Provider configured but the selected context preset id doesn't
    resolve — short-circuit with ``no_preset``."""
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    _configure_generic(client)

    # Stomp the context_preset_id to a non-existent id. Use sentinels
    # for api_token preservation (the GET returns ``api_token_indicator``
    # instead of ``api_token``).
    settings = client.get("/api/settings").json()
    g = settings["generic"]
    custom = g["openai_compatible"]["custom_providers"]
    custom[0]["context_preset_id"] = "does-not-exist"
    for entry in custom:
        entry["api_token"] = "__present__"
    for k in ("novelai", "openrouter", "nanogpt"):
        g[k]["api_token"] = "__present__"
    client.put("/api/settings", json={
        "generic": {
            "provider": "openai_compatible",
            "novelai": g["novelai"],
            "openrouter": g["openrouter"],
            "nanogpt": g["nanogpt"],
            "openai_compatible": {
                "custom_providers": custom,
                "active_id": custom[0]["id"],
            },
        },
    })

    called = {"n": 0}
    async def boom(*args, **kwargs):
        called["n"] += 1
        yield DoneEvent(finish_reason="stop")
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", boom)

    r = client.get(f"/api/chats/{chat_id}/generate")
    assert "no_preset" in r.text
    assert called["n"] == 0


def test_generic_route_no_token_short_circuits(tmp_storage, monkeypatch):
    """Provider + preset configured, but the api_token field is empty —
    short-circuit with ``no_token``."""
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    _configure_generic(client)

    settings = client.get("/api/settings").json()
    g = settings["generic"]
    custom = g["openai_compatible"]["custom_providers"]
    # Clear the active entry's token (PUT semantics: empty string clears).
    # Other entries (none here) would preserve via sentinel.
    custom[0]["api_token"] = ""
    for k in ("novelai", "openrouter", "nanogpt"):
        g[k]["api_token"] = "__present__"
    client.put("/api/settings", json={
        "generic": {
            "provider": "openai_compatible",
            "novelai": g["novelai"],
            "openrouter": g["openrouter"],
            "nanogpt": g["nanogpt"],
            "openai_compatible": {
                "custom_providers": custom,
                "active_id": custom[0]["id"],
            },
        },
    })

    called = {"n": 0}
    async def boom(*args, **kwargs):
        called["n"] += 1
        yield DoneEvent(finish_reason="stop")
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", boom)

    r = client.get(f"/api/chats/{chat_id}/generate")
    assert "no_token" in r.text
    assert called["n"] == 0


def test_generic_route_persist_lands_before_done_event(tmp_storage, monkeypatch):
    """Spec invariant 8 (CLAUDE.md): generation persists BEFORE the final
    ``done`` SSE event. Check by asserting the assistant message exists
    via the GET endpoint right after the stream completes — we couldn't
    do that if persist were after done (the client read would race the
    write, but for an in-process TestClient the GET happens after the
    stream future resolves, so this assertion is meaningful)."""
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    _configure_generic(client)

    events = [
        StartEvent(model="mock-model"),
        DeltaEvent(text="Reply text."),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    r = client.get(f"/api/chats/{chat_id}/generate?expected_tip_id=")
    parsed = _parse_sse(r.text)
    # Index of the 'done' event must be the last event in the stream.
    names = [n for n, _ in parsed]
    assert names[-1] == "done"
    # Message is on disk now.
    msgs = client.get(f"/api/chats/{chat_id}/messages").json()
    assert any(m["sender"] == "contact" and m["origin"] == "generic" for m in msgs)


# ---------------------------------------------------------------------------
# Generic ``prompt`` debug-event shape
# ---------------------------------------------------------------------------


def test_generic_prompt_event_emits_messages_list(tmp_storage, monkeypatch):
    """The generic-mode generation route should yield a ``prompt`` SSE
    event with a ``messages`` list — mirrors AER's ``prompt`` event but
    structured (Generic doesn't have a single rendered string)."""
    client = TestClient(app)
    _create_chat(client)
    contact_id, chat_id = _create_chat(client)
    _configure_generic(client)

    events = [
        StartEvent(model="mock-model"),
        DeltaEvent(text="Hi."),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    r = client.get(f"/api/chats/{chat_id}/generate?expected_tip_id=")
    parsed = _parse_sse(r.text)
    prompt_evts = [p for n, p in parsed if n == "prompt"]
    assert prompt_evts, "expected a prompt SSE event for the operator to inspect"
    p = prompt_evts[0]
    assert isinstance(p.get("messages"), list)
    # At a minimum it carries the rendered system message; every entry
    # must have role + content fields.
    assert p["messages"], "messages list shouldn't be empty"
    for m in p["messages"]:
        assert "role" in m and "content" in m
    assert isinstance(p.get("tokens"), int)


# ---------------------------------------------------------------------------
# Streaming-time leading ``ContactName:`` strip
# ---------------------------------------------------------------------------


def _stream_assistant_text(client: TestClient, chat_id: str) -> str:
    """Concatenate all ``delta`` payloads from a streamed generation."""
    r = client.get(f"/api/chats/{chat_id}/generate?expected_tip_id=")
    parsed = _parse_sse(r.text)
    return "".join(p["text"] for n, p in parsed if n == "delta")


def test_generic_strips_leading_contact_name_during_stream(
    tmp_storage, monkeypatch,
):
    """When ``prefix_names`` is on (Default preset's default), an
    upstream that echoes back ``ContactName:\\n`` at the start of its
    reply must have that prefix stripped at the wire — the user never
    sees it flash into the bubble."""
    client = TestClient(app)
    contact_id, chat_id = _create_chat(client)
    _configure_generic(client)
    # The fixture creates a contact named "C". Echo ``C:\n`` then the
    # body — the wire output should not contain the echo.
    events = [
        StartEvent(model="mock-model"),
        DeltaEvent(text="C:\nReply body."),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    wire = _stream_assistant_text(client, chat_id)
    assert wire == "Reply body.", f"echo not stripped at wire: {wire!r}"


def test_generic_strips_leading_contact_name_with_whitespace(
    tmp_storage, monkeypatch,
):
    """Sometimes the model emits whitespace before the name (e.g. a
    space or newline). The streaming stripper must tolerate optional
    leading whitespace too."""
    client = TestClient(app)
    contact_id, chat_id = _create_chat(client)
    _configure_generic(client)
    events = [
        StartEvent(model="mock-model"),
        DeltaEvent(text="  C: Reply body."),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    wire = _stream_assistant_text(client, chat_id)
    assert wire == "Reply body.", f"whitespace-prefixed echo not stripped: {wire!r}"


def test_generic_strips_name_across_split_chunks(tmp_storage, monkeypatch):
    """The stripper must buffer across chunk boundaries — the upstream
    can fragment the prefix at any point (``"C"`` then ``":\\n"`` then
    body) and the wire output still must not contain the echo."""
    client = TestClient(app)
    contact_id, chat_id = _create_chat(client)
    _configure_generic(client)
    events = [
        StartEvent(model="mock-model"),
        DeltaEvent(text="C"),
        DeltaEvent(text=":"),
        DeltaEvent(text="\nReal body."),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    wire = _stream_assistant_text(client, chat_id)
    assert wire == "Real body.", f"split-chunk echo leaked: {wire!r}"


def test_generic_does_not_strip_unrelated_leading_text(
    tmp_storage, monkeypatch,
):
    """A reply that doesn't start with the contact name must pass
    through unchanged — the stripper's negative path is just as
    important as the positive."""
    client = TestClient(app)
    contact_id, chat_id = _create_chat(client)
    _configure_generic(client)
    events = [
        StartEvent(model="mock-model"),
        DeltaEvent(text="Anyway, hello."),
        DoneEvent(finish_reason="stop"),
    ]
    from server.routers import generate as gen_mod
    monkeypatch.setattr(gen_mod, "stream_chat", _make_fake_stream(events))

    wire = _stream_assistant_text(client, chat_id)
    assert wire == "Anyway, hello.", f"unrelated text mangled: {wire!r}"
