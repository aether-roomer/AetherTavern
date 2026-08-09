"""Tokenizer file resolution: cache location, offline behaviour, proxy category."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from server.aer import tokenizer as T
from server.proxy_rules import KNOWN_CATEGORIES


CACHED = Path(__file__).parent / ".tokenizer-cache" / "zai-org--GLM-4.6"


@pytest.fixture
def fresh_singleton():
    """Swap out the module singleton so a test can drive loading itself."""
    saved = (T._tokenizer, T._load_error)
    T._tokenizer, T._load_error = None, None
    yield
    T._tokenizer, T._load_error = saved


@pytest.fixture
def unreachable(monkeypatch):
    # Discard port — connections are refused immediately rather than hanging.
    monkeypatch.setattr(T, "_HF_ENDPOINT", "http://127.0.0.1:9")


def test_tokenizer_dir_defaults_under_data_dir(tmp_storage, monkeypatch):
    monkeypatch.delenv("AETHER_TOKENIZER_DIR", raising=False)
    assert T.tokenizer_dir() == tmp_storage / "tokenizer" / "zai-org--GLM-4.6"


def test_tokenizer_dir_env_override(monkeypatch):
    monkeypatch.setenv("AETHER_TOKENIZER_DIR", "/somewhere/else")
    assert T.tokenizer_dir() == Path("/somewhere/else") / "zai-org--GLM-4.6"


def test_download_has_a_proxy_category():
    """The boot fetch routes through the rules like any other outbound call."""
    assert "tokenizer" in KNOWN_CATEGORIES


def test_offline_raises_with_the_path_to_fix_it(
    tmp_path, monkeypatch, fresh_singleton, unreachable
):
    monkeypatch.setenv("AETHER_TOKENIZER_DIR", str(tmp_path))
    with pytest.raises(T.TokenizerUnavailable) as excinfo:
        T.get_tokenizer()
    assert str(tmp_path / "zai-org--GLM-4.6" / "tokenizer.json") in str(excinfo.value)


def test_failed_load_is_not_retried_over_the_network(
    tmp_path, monkeypatch, fresh_singleton, unreachable
):
    """get_tokenizer() is sync and runs on the event loop during generation;
    re-dialling a dead endpoint per attempt would park the whole server."""
    monkeypatch.setenv("AETHER_TOKENIZER_DIR", str(tmp_path))
    with pytest.raises(T.TokenizerUnavailable):
        T.get_tokenizer()

    def _boom(filename):  # pragma: no cover — must not be reached
        raise AssertionError(f"retried the download for {filename}")

    monkeypatch.setattr(T, "_download", _boom)
    with pytest.raises(T.TokenizerUnavailable):
        T.get_tokenizer()


@pytest.mark.skipif(not CACHED.is_dir(), reason="tokenizer not downloaded yet")
def test_hand_placed_files_recover_without_a_restart(
    tmp_path, monkeypatch, fresh_singleton, unreachable
):
    """The documented offline remedy — copy the files in — must take effect
    on the next attempt rather than needing the server bounced."""
    target = tmp_path / "zai-org--GLM-4.6"
    monkeypatch.setenv("AETHER_TOKENIZER_DIR", str(tmp_path))
    with pytest.raises(T.TokenizerUnavailable):
        T.get_tokenizer()

    target.mkdir(parents=True)
    for name in ("tokenizer.json", "chat_template.jinja"):
        shutil.copy(CACHED / name, target / name)

    assert T.get_tokenizer().encode("hello")
