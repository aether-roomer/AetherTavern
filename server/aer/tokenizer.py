"""Lazy-once tokenizer singleton for GLM-4.6.

The FastAPI lifespan calls :func:`load_tokenizer` once at startup so the first
generation request never sees a cold start.

The tokenizer is :class:`~server.aer.bpe.ByteLevelBPE` over the model's own
``tokenizer.json`` plus its Jinja chat template, both fetched from Hugging
Face with plain httpx on first run and cached under
``<data_dir>/tokenizer/``. Fetching them ourselves keeps ``transformers``
(and its numpy / Rust-extension dependency tree) out of the install, which
is what makes the server installable on platforms without a compiler
toolchain. It also means the download honours :mod:`server.proxy_rules`
like every other outbound request.

A user with no network can drop the files into that directory by hand; only
freshly downloaded bytes are checksum-verified.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import httpx
import jinja2
import jinja2.ext
from jinja2.sandbox import ImmutableSandboxedEnvironment

from server.aer.bpe import ByteLevelBPE
from server.proxy_rules import current_proxy_rules


TOKENIZER_NAME: str = os.environ.get("AETHER_TOKENIZER", "zai-org/GLM-4.6")

# Pinning the revision keeps the bytes reproducible and lets us verify what
# we downloaded. Only applied to the default repo — an AETHER_TOKENIZER
# override tracks ``main`` and skips the checksum.
_DEFAULT_NAME = "zai-org/GLM-4.6"
_DEFAULT_REVISION = "be72194883d968d7923a07e2f61681ea9a2826d1"
_DEFAULT_SHA256 = {
    "tokenizer.json":
        "9340665016419c825c4bdabbcc9acc43b7ca2c68ce142724afa829abb1be5efd",
}

_VOCAB_FILE = "tokenizer.json"
_TEMPLATE_FILE = "chat_template.jinja"
# Repos predating transformers' standalone template file carry it here.
_CONFIG_FILE = "tokenizer_config.json"

_HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")

_tokenizer: "GlmTokenizer | None" = None
_load_error: Exception | None = None
_logger = logging.getLogger("aether.tokenizer")


class TokenizerUnavailable(Exception):
    """The tokenizer files are neither on disk nor downloadable.

    Raised from :func:`get_tokenizer`, so it surfaces on the generation
    path (as an SSE ``error``) rather than taking the whole server down at
    boot — an offline user can still reach the UI and drop the files in.
    """


def tokenizer_dir() -> Path:
    """Directory holding this tokenizer's cached files."""
    override = os.environ.get("AETHER_TOKENIZER_DIR")
    if override:
        base = Path(override)
    else:
        from server import storage  # local: storage imports aer.rollover

        base = storage.DATA_DIR / "tokenizer"
    return base / TOKENIZER_NAME.replace("/", "--")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _revision() -> str:
    return _DEFAULT_REVISION if TOKENIZER_NAME == _DEFAULT_NAME else "main"


def _url_for(filename: str) -> str:
    return f"{_HF_ENDPOINT}/{TOKENIZER_NAME}/resolve/{_revision()}/{filename}"


def _download(filename: str) -> bytes | None:
    """Fetch one repo file. ``None`` when the repo does not have it."""
    url = _url_for(filename)
    kwargs: dict = {}
    rules = current_proxy_rules()
    if rules is not None:
        kwargs["proxy"] = rules.lookup(url=url, category="tokenizer")
        kwargs["trust_env"] = False
    _logger.info("Downloading %s …", url)
    chunks: list[bytes] = []
    with httpx.Client(
        follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0), **kwargs
    ) as client:
        with client.stream("GET", url) as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            next_mark = 0.25
            for chunk in response.iter_bytes(1 << 16):
                chunks.append(chunk)
                if total:
                    done = response.num_bytes_downloaded / total
                    if done >= next_mark:
                        _logger.info("  %s: %d%%", filename, int(done * 100))
                        while next_mark <= done:
                            next_mark += 0.25
    data = b"".join(chunks)
    expected = _DEFAULT_SHA256.get(filename) if TOKENIZER_NAME == _DEFAULT_NAME else None
    if expected is not None:
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected:
            raise TokenizerUnavailable(
                f"{url} does not match its expected checksum "
                f"(got {digest}, want {expected}); refusing to cache it."
            )
    _logger.info("  %s: %d bytes", filename, len(data))
    return data


def _ensure_file(filename: str, *, required: bool) -> bytes | None:
    """Return the file's bytes, downloading and caching it if absent."""
    from server import storage  # local: storage imports aer.rollover

    path = tokenizer_dir() / filename
    if path.is_file():
        return path.read_bytes()
    try:
        data = _download(filename)
    except TokenizerUnavailable:
        raise
    except Exception as e:
        raise TokenizerUnavailable(
            f"Could not download {filename} for tokenizer {TOKENIZER_NAME!r}: "
            f"{e}. Place the file at {path} to run without network access."
        ) from e
    if data is None:
        if required:
            raise TokenizerUnavailable(
                f"Tokenizer {TOKENIZER_NAME!r} has no {filename} at {_url_for(filename)}."
            )
        return None
    storage.atomic_write_bytes(path, data)
    return data


def _load_chat_template() -> str:
    data = _ensure_file(_TEMPLATE_FILE, required=False)
    if data is not None:
        return data.decode("utf-8")
    config = _ensure_file(_CONFIG_FILE, required=False)
    template = json.loads(config).get("chat_template") if config else None
    if not isinstance(template, str):
        raise TokenizerUnavailable(
            f"Tokenizer {TOKENIZER_NAME!r} ships no chat template "
            f"({_TEMPLATE_FILE} or a `chat_template` in {_CONFIG_FILE})."
        )
    return template


# ---------------------------------------------------------------------------
# Chat template
# ---------------------------------------------------------------------------


def _tojson(value, ensure_ascii=False, indent=None, separators=None, sort_keys=False):
    return json.dumps(
        value, ensure_ascii=ensure_ascii, indent=indent,
        separators=separators, sort_keys=sort_keys,
    )


def _raise_exception(message: str):
    raise jinja2.exceptions.TemplateError(message)


def _compile_template(source: str) -> jinja2.Template:
    """Compile a chat template the way ``transformers`` does.

    The environment settings are load-bearing: ``trim_blocks`` /
    ``lstrip_blocks`` change where the template's own newlines land, and a
    template written against them renders differently under the defaults.
    """
    env = ImmutableSandboxedEnvironment(
        trim_blocks=True, lstrip_blocks=True, extensions=[jinja2.ext.loopcontrols]
    )
    env.filters["tojson"] = _tojson
    env.globals["raise_exception"] = _raise_exception
    return env.from_string(source)


class GlmTokenizer:
    """Chat-template renderer + token counter for one model repo."""

    def __init__(self, spec: dict, chat_template: str) -> None:
        self.bpe = ByteLevelBPE(spec)
        self.template = _compile_template(chat_template)

    def encode(self, text: str) -> list[int]:
        return self.bpe.encode(text)

    def count(self, text: str) -> int:
        return self.bpe.count(text)

    def render_chat(
        self,
        messages: list[dict],
        *,
        add_generation_prompt: bool = True,
        enable_thinking: bool = False,
    ) -> str:
        return self.template.render(
            messages=messages,
            add_generation_prompt=add_generation_prompt,
            enable_thinking=enable_thinking,
        )


def load_tokenizer() -> GlmTokenizer:
    """Load (or return cached) tokenizer, fetching its files if needed. Idempotent."""
    global _tokenizer, _load_error
    if _tokenizer is not None:
        return _tokenizer
    _logger.info("Loading tokenizer %s …", TOKENIZER_NAME)
    try:
        spec = json.loads(_ensure_file(_VOCAB_FILE, required=True))
        _tokenizer = GlmTokenizer(spec, _load_chat_template())
    except Exception as e:
        _load_error = e
        raise
    _load_error = None
    _logger.info("Tokenizer ready.")
    return _tokenizer


def get_tokenizer() -> GlmTokenizer:
    """Return the loaded tokenizer; loads on demand if startup didn't run yet.

    A previous failure is replayed without going back to the network:
    :func:`load_tokenizer` is synchronous, so retrying it per generation
    would park the event loop on a connect timeout for every attempt. The
    one thing that does earn a fresh attempt is the files showing up on
    disk — that is the documented offline remedy, and it should take
    effect without a restart.
    """
    if _tokenizer is not None:
        return _tokenizer
    if _load_error is not None and not (tokenizer_dir() / _VOCAB_FILE).is_file():
        raise _load_error
    return load_tokenizer()
