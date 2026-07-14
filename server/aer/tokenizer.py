"""Lazy-once tokenizer singleton for GLM-4.6.

The FastAPI lifespan calls :func:`load_tokenizer` once at startup so the first
generation request never sees a cold start.
"""
from __future__ import annotations

import logging
import os
from typing import Any


TOKENIZER_NAME: str = os.environ.get("AETHER_TOKENIZER", "zai-org/GLM-4.6")

_tokenizer: Any | None = None
_logger = logging.getLogger("aether.tokenizer")


def load_tokenizer() -> Any:
    """Load (or return cached) ``AutoTokenizer``. Idempotent."""
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    # Local import keeps transformers off the import path until it's actually needed.
    from transformers import AutoTokenizer

    _logger.info("Loading tokenizer %s …", TOKENIZER_NAME)
    _tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_NAME, trust_remote_code=False)
    _logger.info("Tokenizer ready.")
    return _tokenizer


def get_tokenizer() -> Any:
    """Return the loaded tokenizer; loads on demand if startup didn't run yet."""
    if _tokenizer is None:
        return load_tokenizer()
    return _tokenizer
