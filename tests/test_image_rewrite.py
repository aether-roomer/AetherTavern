"""Stream-time markdown image rewriter.

Two parallel buffers — ``wire`` (URLs rewritten to the proxy) and
``persist`` (original URLs preserved). HTTP(S) URLs land in
``image_refs`` for later proxy resolution; ``data:`` URLs are decoded to
disk inline so the persisted markdown points at the proxy URL of a real
file (and ``image_refs`` stays empty for that one).
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from server.generic.image_rewrite import ImageRewriter


@pytest.fixture
def images_dir(tmp_path) -> Path:
    return tmp_path / "images"


def test_http_url_split_across_chunks(images_dir):
    rw = ImageRewriter(chat_id="chatzzz", images_dir=images_dir)
    o1 = rw.feed("Hello ![cat](https://")
    o2 = rw.feed("example.com/foo.jpg) world")
    wire = o1.wire + o2.wire
    persist = o1.persist + o2.persist
    # Wire references the proxy URL with the original filename in the path.
    assert "/api/chats/chatzzz/images/" in wire
    assert "/foo.jpg" in wire
    # Persist preserves the original URL.
    assert "![cat](https://example.com/foo.jpg)" in persist
    # image_refs registered for HTTP URL.
    assert "https://example.com/foo.jpg" in rw.image_refs


def test_data_url_decoded_to_disk(images_dir, tmp_path):
    rw = ImageRewriter(chat_id="chatddd", images_dir=images_dir)
    # Smallest valid PNG.
    png_bytes = bytes.fromhex(
        "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000"
        "000A4944415478DA63000000000200015E7B1B7C0000000049454E44AE426082"
    )
    b64 = base64.b64encode(png_bytes).decode("ascii")
    out = rw.feed(f"![p](data:image/png;base64,{b64})")
    # Wire AND persist both contain the proxy URL — we don't persist the
    # inline payload.
    assert "/api/chats/chatddd/images/" in out.wire
    assert "/api/chats/chatddd/images/" in out.persist
    # No HTTP image_refs entry — data: URLs are encoded inline at stream time.
    assert rw.image_refs == {}
    # The decoded file lives on disk.
    files = list(images_dir.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == png_bytes


def test_not_image_syntax_flushes_verbatim(images_dir):
    rw = ImageRewriter(chat_id="c", images_dir=images_dir)
    out = rw.feed("look ![just text] not image")
    assert out.wire == "look ![just text] not image"
    assert out.persist == "look ![just text] not image"
    assert rw.image_refs == {}


def test_partial_image_markdown_flushes_at_end_of_stream(images_dir):
    """If the model stops mid-``![…``, the buffered prefix must flush
    verbatim — otherwise the last segment of an interrupted turn vanishes."""
    rw = ImageRewriter(chat_id="c", images_dir=images_dir)
    feed_out = rw.feed("Pre ![alt](https://example.com/incompl")
    # While buffering, only the pre-bracket text is forwarded.
    assert feed_out.wire == "Pre "
    assert feed_out.persist == "Pre "
    flush_out = rw.flush()
    assert flush_out.wire == "![alt](https://example.com/incompl"
    assert flush_out.persist == "![alt](https://example.com/incompl"
    # Partial image isn't registered.
    assert rw.image_refs == {}


def test_consecutive_images_in_one_chunk(images_dir):
    rw = ImageRewriter(chat_id="c", images_dir=images_dir)
    out = rw.feed(
        "First ![a](https://example.com/a.jpg) "
        "Second ![b](https://example.com/b.jpg) done"
    )
    # Both HTTP URLs get their own uuid registered.
    assert len(rw.image_refs) == 2
    assert "https://example.com/a.jpg" in rw.image_refs
    assert "https://example.com/b.jpg" in rw.image_refs
    # Wire contains both proxy URLs.
    assert out.wire.count("/api/chats/c/images/") == 2
    # Persist contains both original URLs intact.
    assert "https://example.com/a.jpg" in out.persist
    assert "https://example.com/b.jpg" in out.persist


def test_same_remote_url_reuses_uuid(images_dir):
    rw = ImageRewriter(chat_id="c", images_dir=images_dir)
    rw.feed("![one](https://example.com/foo.jpg)")
    first_uuid = rw.image_refs["https://example.com/foo.jpg"]
    rw.feed("![two](https://example.com/foo.jpg)")
    # Same URL, same uuid — image proxy serves once, reused.
    assert rw.image_refs["https://example.com/foo.jpg"] == first_uuid
    assert len(rw.image_refs) == 1


def test_data_url_too_large_passes_through(images_dir):
    """Defensive: a model emitting a massive ``data:`` URL shouldn't fill
    disk. Falls back to verbatim pass-through on both wire + persist."""
    rw = ImageRewriter(chat_id="c", images_dir=images_dir)
    # 21 MB of "A" base64-encoded — over the 20 MB cap.
    payload = base64.b64encode(b"A" * (21 * 1024 * 1024)).decode("ascii")
    md = f"![big](data:image/png;base64,{payload})"
    out = rw.feed(md)
    # Wire AND persist are verbatim — no proxy rewrite.
    assert out.wire == md
    assert out.persist == md
    # Nothing on disk.
    assert not images_dir.exists() or len(list(images_dir.iterdir())) == 0


def test_relative_path_left_intact(images_dir):
    """Local URLs aren't proxied — they pass through unchanged. Sanitize-on
    at the renderer level neuters the syntax when the URL was escaped."""
    rw = ImageRewriter(chat_id="c", images_dir=images_dir)
    out = rw.feed("see ![rel](/static/img.png) here")
    assert out.wire == "see ![rel](/static/img.png) here"
    assert out.persist == "see ![rel](/static/img.png) here"
    assert rw.image_refs == {}
