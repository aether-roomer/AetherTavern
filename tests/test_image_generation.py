"""User-stepped NovelAI scene prompting and durable generated pictures."""
from __future__ import annotations

import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from server import storage
from server.main import app
from server.models import (
    Contact,
    ImageGenerationSettings,
    ImagePromptState,
    User,
)
from server.routers import image_generation


_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\xdac\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01"
    b"^\xf3*:"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _chat(client: TestClient):
    contact = storage.save_contact(Contact(
        name="Alice",
        appearance="Long black hair and green eyes",
    ))
    user = storage.save_user(User(name="Me", appearance="Short brown hair"))
    chat_id = client.post(
        "/api/chats",
        json={"contact_id": contact.id, "user_id": user.id},
    ).json()["id"]
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender": "user", "body": [{"text": "We stand in the rain."}]},
    )
    return chat_id


def _set_token() -> None:
    settings = storage.load_settings()
    settings.api_token = "nai-token"
    storage.save_settings(settings)


def test_image_settings_round_trip(tmp_storage):
    client = TestClient(app)
    current = client.get("/api/settings").json()["image_generation"]
    assert current["model"] == "nai-diffusion-5-full"
    assert "<image_prompt>" in current["system_prompt"]
    assert "through their own eyes" in current["system_prompt"]
    assert "Never write a tag list" in current["system_prompt"]
    assert "anime-style digital illustration" in current["system_prompt"]
    assert "never use commas to chain" in current["system_prompt"]
    assert "Visible contact emotion" in current["system_prompt"]
    assert "clothing as layered, stateful continuity" in current["system_prompt"]
    assert "Never collapse a displaced garment" in current["system_prompt"]
    assert "when an outer garment" in current["system_prompt"]
    assert "directly say that the garment is not worn" in current["system_prompt"]
    assert "each fully hidden clothing fact is omitted" in current["system_prompt"]
    assert "fully nude or partly nude" not in current["system_prompt"]
    assert "Describe each clothing state or absence only when it affects what is actually visible" in current["user_message"]
    assert "latest physically realized moment" in current["system_prompt"]
    assert "establish a scene anchor inside the reasoning" in current["system_prompt"]
    assert "plans, hypotheticals" in current["system_prompt"]
    assert "Freeze the existing scene" in current["user_message"]
    assert "emit </think> as the final characters" in current["system_prompt"]
    assert "must not exceed roughly 900 words" in current["system_prompt"]
    assert "Qwen" not in current["system_prompt"]
    assert "human" not in current["system_prompt"].lower()
    assert current["prompt_max_tokens"] == 4096

    current["user_message"] = "Make a cinematic close-up."
    current["negative_prompt"] = "custom UC prose"
    current["width"] = 1024
    response = client.put("/api/settings", json={"image_generation": current})
    assert response.status_code == 200, response.text
    assert response.json()["image_generation"]["user_message"] == "Make a cinematic close-up."
    assert response.json()["image_generation"]["negative_prompt"] == "custom UC prose"
    assert storage.load_settings().image_generation.width == 1024


def test_prompt_reasoning_requires_separate_continue_action(tmp_storage, monkeypatch):
    client = TestClient(app)
    chat_id = _chat(client)
    _set_token()
    calls: list[str] = []

    async def fake_stream_completion(**kwargs):
        calls.append(kwargs["prompt"])
        if len(calls) == 1:
            yield (
                " Cast audit includes a hallucinated "
                "<image_prompt>reasoning content, not a prompt</image_prompt> "
                "and reaches its conclusion.</thi"
            )
            yield "nk><image_prompt>THIS MUST BE DISCARDED"
        else:
            yield (
                "<think>Repeated reasoning that must not enter the prompt.</think>"
                "Alice with long black hair and green eyes stands in the rain. "
                "A brown-haired hand enters the foreground.</image_prompt>"
                "THIS MUST ALSO BE DISCARDED"
            )

    monkeypatch.setattr(image_generation, "stream_completion", fake_stream_completion)

    # A browser navigation/reload cannot start text generation. The endpoint
    # only accepts the explicit POST sent by the modal button.
    assert client.get(f"/api/chats/{chat_id}/image-prompt").status_code == 404
    assert calls == []

    first = client.post(f"/api/chats/{chat_id}/image-prompt")
    assert first.status_code == 200
    assert '"response_prefix": "<think>1."' in first.text
    assert len(calls) == 1
    state = storage.get_chat(chat_id).image_prompt_state
    assert "reasoning" not in state.model_dump()
    assert state.response.endswith("</think>\n<image_prompt>")
    assert "THIS MUST BE DISCARDED" not in state.response
    assert "<image_prompt>reasoning content" not in state.response
    assert state.prompt is None
    assert state.complete is False

    preview = client.get(
        f"/api/chats/{chat_id}/image-prompt-preview?continue=true"
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["transport"] == "raw_completion"
    assert preview_body["prompt"].startswith("[gMASK]<sop><|system|>")
    assert "<|user|>\n" in preview_body["prompt"]
    assert "reasoning content, not a prompt" in preview_body["prompt"]
    assert "and reaches its conclusion" in preview_body["prompt"]
    assert "Previously streamed reasoning" not in preview_body["prompt"]
    assert preview_body["prompt"].endswith(state.response)
    assert preview_body["prompt"].endswith("</think>\n<image_prompt>")
    assert preview_body["stage"] == "image_prompt"
    assert preview_body["context_tip_id"] == state.context_tip_id
    # Preview is local-only: it neither contacts NovelAI nor advances state.
    assert len(calls) == 1
    assert state.complete is False
    assert state.response.startswith("<think>1.")
    # No automatic second request: a distinct user-triggered HTTP call is
    # required to finish the prompt.
    assert len(calls) == 1

    second = client.post(f"/api/chats/{chat_id}/image-prompt?continue=true")
    assert second.status_code == 200
    assert len(calls) == 2
    assert calls[1].endswith("</think>\n<image_prompt>")
    state = storage.get_chat(chat_id).image_prompt_state
    assert state.complete is True
    assert state.prompt.startswith("Alice with long black hair")
    assert "Repeated reasoning" not in state.prompt
    assert "THIS MUST ALSO BE DISCARDED" not in state.response
    assert state.response.endswith("</image_prompt>")
    assert "Alice" in calls[0]
    assert "Long black hair and green eyes" in calls[0]


def test_fresh_reasoning_prompt_has_exact_native_thought_tail(tmp_storage):
    client = TestClient(app)
    chat_id = _chat(client)
    preview = client.get(f"/api/chats/{chat_id}/image-prompt-preview")
    assert preview.status_code == 200, preview.text
    raw = preview.json()["prompt"]
    settings = storage.load_settings().image_generation
    assert raw.endswith(
        f"<|user|>\n{settings.user_message}<|assistant|>\n<think>1."
    )
    assert raw.count("<|user|>") == 1
    assert raw.count("<|assistant|>") == 1
    body = preview.json()
    assert body["model"] == "glm-4-6"
    assert body["context_window_tokens"] == 28_672
    assert body["reserved_output_tokens"] == settings.prompt_max_tokens
    assert body["input_tokens"] <= body["input_token_budget"]
    assert (
        body["input_token_budget"]
        + body["reserved_output_tokens"]
        + body["context_safety_tokens"]
        == body["context_window_tokens"]
    )


def test_image_prompt_model_substitutes_only_inherited_xialong(tmp_storage):
    client = TestClient(app)
    chat_id = _chat(client)
    settings = storage.load_settings()
    settings.default_model = "xialong-v1"
    settings.generic.novelai.model_id = ""
    settings.image_generation.prompt_model = ""
    storage.save_settings(settings)

    inherited_default = client.get(
        f"/api/chats/{chat_id}/image-prompt-preview"
    )
    assert inherited_default.status_code == 200, inherited_default.text
    assert inherited_default.json()["model"] == "glm-4-6"

    settings = storage.load_settings()
    settings.default_model = "another-default"
    settings.generic.novelai.model_id = "xialong-v1"
    storage.save_settings(settings)
    inherited_generic = client.get(
        f"/api/chats/{chat_id}/image-prompt-preview"
    )
    assert inherited_generic.status_code == 200, inherited_generic.text
    assert inherited_generic.json()["model"] == "glm-4-6"

    settings = storage.load_settings()
    settings.image_generation.prompt_model = "xialong-v1"
    storage.save_settings(settings)
    explicit_image_model = client.get(
        f"/api/chats/{chat_id}/image-prompt-preview"
    )
    assert explicit_image_model.status_code == 200, explicit_image_model.text
    assert explicit_image_model.json()["model"] == "xialong-v1"


def test_contact_bubble_emotion_is_in_reasoning_context(tmp_storage):
    client = TestClient(app)
    chat_id = _chat(client)
    message = client.post(
        f"/api/chats/{chat_id}/messages",
        json={
            "sender": "contact",
            "body": [{"text": "Alice gives a small uncertain smile.", "emotion": "nervous"}],
        },
    )
    assert message.status_code == 200, message.text

    preview = client.get(f"/api/chats/{chat_id}/image-prompt-preview")
    assert preview.status_code == 200, preview.text
    raw = preview.json()["prompt"]
    assert "Alice gives a small uncertain smile." in raw
    assert "Visible contact emotion: nervous" in raw


def test_historical_prompt_context_stops_at_selected_message(tmp_storage):
    client = TestClient(app)
    chat_id = _chat(client)
    first = client.get(f"/api/chats/{chat_id}/active-path").json()[0]
    second = client.post(
        f"/api/chats/{chat_id}/messages",
        json={
            "parent_id": first["id"],
            "sender": "contact",
            "body": [{"text": "SECOND MOMENT ONLY", "emotion": "happy"}],
        },
    ).json()
    third = client.post(
        f"/api/chats/{chat_id}/messages",
        json={
            "parent_id": second["id"],
            "sender": "user",
            "body": [{"text": "THIRD MOMENT MUST BE EXCLUDED"}],
        },
    ).json()

    preview = client.get(
        f"/api/chats/{chat_id}/image-prompt-preview",
        params={"anchor_message_id": second["id"]},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["context_tip_id"] == second["id"]
    assert body["history_messages"] == 2
    assert "We stand in the rain." in body["prompt"]
    assert "SECOND MOMENT ONLY" in body["prompt"]
    assert "THIRD MOMENT MUST BE EXCLUDED" not in body["prompt"]

    chat = storage.get_chat(chat_id)
    chat.image_prompt_state = ImagePromptState(
        context_tip_id=second["id"],
        response="<think>1. continuing the selected moment",
    )
    storage.save_chat(chat, bump_version=False)
    continuation = client.get(
        f"/api/chats/{chat_id}/image-prompt-preview",
        params={"continue": "true", "anchor_message_id": second["id"]},
    )
    assert continuation.status_code == 200, continuation.text
    assert "SECOND MOMENT ONLY" in continuation.json()["prompt"]
    assert "THIRD MOMENT MUST BE EXCLUDED" not in continuation.json()["prompt"]

    wrong_anchor = client.get(
        f"/api/chats/{chat_id}/image-prompt-preview",
        params={"continue": "true", "anchor_message_id": third["id"]},
    )
    assert wrong_anchor.status_code == 409


def test_prompt_budget_trims_oldest_history_first(monkeypatch):
    cfg = ImageGenerationSettings(
        system_prompt="System",
        user_message="Instructions",
        prompt_max_tokens=128,
    )
    history = [
        "oldest " + "a" * 120,
        "middle " + "b" * 120,
        "latest " + "c" * 120,
    ]
    monkeypatch.setattr(image_generation, "_IMAGE_CONTEXT_WINDOW_TOKENS", 500)
    monkeypatch.setattr(image_generation, "_IMAGE_CONTEXT_SAFETY_TOKENS", 20)
    monkeypatch.setattr(image_generation, "_count_prompt_tokens", len)

    prompt, tokens, budget, omitted = image_generation._build_bounded_prompt(
        image_cfg=cfg,
        facts_text="REFERENCE DATA",
        history=history,
        assistant_text=None,
    )
    assert tokens <= budget == 352
    assert omitted > 0
    assert "latest " in prompt
    assert "oldest " not in prompt
    assert f"[{omitted} earlier active-path message(s) omitted" in prompt


@pytest.mark.parametrize(("aspect", "width", "height"), [
    ("portrait", 832, 1216),
    ("landscape", 1216, 832),
    ("square", 1024, 1024),
])
def test_aspect_sizes_are_exact(tmp_storage, aspect, width, height):
    cfg = storage.load_settings().image_generation
    payload = image_generation._image_payload(
        "scene", cfg, 123,
        width=image_generation._ASPECT_SIZES[aspect][0],
        height=image_generation._ASPECT_SIZES[aspect][1],
    )
    assert payload["parameters"]["width"] == width
    assert payload["parameters"]["height"] == height


def test_custom_uc_is_sent_to_both_novelai_negative_prompt_fields(tmp_storage):
    cfg = storage.load_settings().image_generation
    cfg.negative_prompt = "custom undesired content"
    payload = image_generation._image_payload("scene", cfg, 123)
    params = payload["parameters"]
    assert params["negative_prompt"] == "custom undesired content"
    assert (
        params["v4_negative_prompt"]["caption"]["base_caption"]
        == "custom undesired content"
    )


def test_generate_image_persists_contact_attachment(tmp_storage, monkeypatch):
    client = TestClient(app)
    chat_id = _chat(client)
    _set_token()
    captured: dict = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs["json"]
            captured["headers"] = kwargs["headers"]
            return httpx.Response(201, json={
                "images": [{
                    "image": base64.b64encode(_PNG_1X1).decode("ascii"),
                    "index": 0,
                    "seed": 12345,
                }],
            })

    monkeypatch.setattr(
        image_generation, "make_async_client", lambda *args, **kwargs: FakeClient(),
    )
    tip = client.get(f"/api/chats/{chat_id}/active-path").json()[-1]["id"]
    response = client.post(
        f"/api/chats/{chat_id}/generate-image",
        json={
            "prompt": "Alice in the rain",
            "anchor_message_id": tip,
            "aspect": "landscape",
        },
    )
    assert response.status_code == 200, response.text
    message = response.json()
    assert message["sender"] == "contact"
    assert message["body"] == []
    assert message["attachments"][0]["source"] == "generated"
    assert message["attachments"][0]["prompt"] == "Alice in the rain"
    assert captured["url"].endswith("/ai/generate-image")
    assert captured["json"]["parameters"]["v4_prompt"]["caption"]["base_caption"] == "Alice in the rain"
    assert captured["json"]["parameters"]["width"] == 1216
    assert captured["json"]["parameters"]["height"] == 832
    assert captured["headers"]["Accept"] == "application/json"

    att = message["attachments"][0]
    attachment_dir = storage.chat_attachments_dir(chat_id)
    cache_dir = storage.chat_attachment_cache_dir(chat_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{att['id']}.q85.jpg"
    cached.write_bytes(b"cached derivative")
    served = client.get(f"/api/files/chats/{chat_id}/attachments/{att['id']}")
    assert served.content == _PNG_1X1
    active = client.get(f"/api/chats/{chat_id}/active-path").json()
    assert active[-1]["id"] == message["id"]

    deleted = client.delete(
        f"/api/chats/{chat_id}/messages/{message['id']}/attachments/{att['id']}"
    )
    assert deleted.status_code == 200
    assert client.get(f"/api/files/chats/{chat_id}/attachments/{att['id']}").status_code == 404
    assert not list(attachment_dir.glob(f"{att['id']}.*"))
    assert not cached.exists()
    stored = storage.load_chat_messages(chat_id)
    assert next(m for m in stored.messages if m.id == message["id"]).attachments == []


def test_historical_image_inserts_below_anchor_and_branch_delete_reaps_file(
    tmp_storage, monkeypatch,
):
    client = TestClient(app)
    chat_id = _chat(client)
    _set_token()
    first = client.get(f"/api/chats/{chat_id}/active-path").json()[0]
    second = client.post(
        f"/api/chats/{chat_id}/messages",
        json={
            "parent_id": first["id"],
            "sender": "contact",
            "body": [{"text": "The selected second moment."}],
        },
    ).json()
    third = client.post(
        f"/api/chats/{chat_id}/messages",
        json={
            "parent_id": second["id"],
            "sender": "user",
            "body": [{"text": "A later third moment."}],
        },
    ).json()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return httpx.Response(201, json={
                "images": [{
                    "image": base64.b64encode(_PNG_1X1).decode("ascii"),
                    "index": 0,
                    "seed": 54321,
                }],
            })

    monkeypatch.setattr(
        image_generation, "make_async_client", lambda *args, **kwargs: FakeClient(),
    )
    generated = client.post(
        f"/api/chats/{chat_id}/generate-image",
        json={
            "prompt": "The selected second moment",
            "anchor_message_id": second["id"],
            "aspect": "portrait",
        },
    )
    assert generated.status_code == 200, generated.text
    image_message = generated.json()
    attachment = image_message["attachments"][0]

    active = client.get(f"/api/chats/{chat_id}/active-path").json()
    assert [message["id"] for message in active] == [
        first["id"], second["id"], image_message["id"], third["id"],
    ]
    stored = storage.load_chat_messages(chat_id)
    stored_third = next(message for message in stored.messages if message.id == third["id"])
    assert image_message["parent_id"] == second["id"]
    assert stored_third.parent_id == image_message["id"]

    attachment_dir = storage.chat_attachments_dir(chat_id)
    original = next(attachment_dir.glob(f"{attachment['id']}.*"))
    cache_dir = storage.chat_attachment_cache_dir(chat_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{attachment['id']}.q85.jpg"
    cached.write_bytes(b"cached derivative")
    assert original.exists()

    deleted = client.delete(
        f"/api/chats/{chat_id}/messages/{second['id']}"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_generated_attachments"] == 1
    assert not original.exists()
    assert not cached.exists()
    assert client.get(
        f"/api/files/chats/{chat_id}/attachments/{attachment['id']}"
    ).status_code == 404
    active_after_delete = client.get(f"/api/chats/{chat_id}/active-path").json()
    assert [message["id"] for message in active_after_delete] == [first["id"]]
    stored_after_delete = storage.load_chat_messages(chat_id)
    stored_image = next(
        message
        for message in stored_after_delete.messages
        if message.id == image_message["id"]
    )
    assert stored_image.attachments == []
