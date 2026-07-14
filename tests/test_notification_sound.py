"""Notification-sound upload / serve / delete round-trip.

The global audio file is stored at ``data/notification_sound.{ext}`` and
referenced by ``Settings.notification_sound``. Routes follow the same
write-then-cleanup discipline as avatar uploads — settings YAML never
points at a deleted file, and old-extension siblings are reaped after a
successful save.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from server import storage
from server.main import app


# Valid WAV header for the sniffer: RIFF + 'WAVE' at offset 8.
_TINY_WAV = b"RIFF" + (44).to_bytes(4, "little") + b"WAVE" + b"\0" * 36


def test_notification_sound_round_trip(tmp_storage):
    with TestClient(app) as client:
        # Defaults: feature off, no custom sound.
        r = client.get("/api/settings").json()
        assert r["notify_on_complete"] is False
        assert r["notification_sound"] is None

        # Toggle the boolean via the generic PUT.
        r = client.put("/api/settings", json={"notify_on_complete": True}).json()
        assert r["notify_on_complete"] is True

        # Upload a custom audio file — the settings field becomes the
        # on-disk filename and the bytes are reachable via GET.
        up = client.post(
            "/api/settings/notification-sound",
            files={"file": ("ping.wav", _TINY_WAV, "audio/wav")},
        )
        assert up.status_code == 200, up.text
        assert up.json()["filename"].startswith("notification_sound.")

        r = client.get("/api/settings").json()
        assert r["notification_sound"] == "notification_sound.wav"

        got = client.get("/api/files/notification-sound")
        assert got.status_code == 200
        assert got.headers["content-type"].startswith("audio/")
        assert got.content == _TINY_WAV

        # DELETE clears the setting and unlinks the file.
        d = client.delete("/api/settings/notification-sound")
        assert d.status_code == 200
        r = client.get("/api/settings").json()
        assert r["notification_sound"] is None
        # File is gone.
        got = client.get("/api/files/notification-sound")
        assert got.status_code == 404


def test_notification_sound_rejects_non_audio(tmp_storage):
    with TestClient(app) as client:
        # Random bytes with no audio magic — should 400.
        r = client.post(
            "/api/settings/notification-sound",
            files={"file": ("bogus.wav", b"NOT AUDIO", "audio/wav")},
        )
        assert r.status_code == 400
