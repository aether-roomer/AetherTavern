"""Stream-time markdown image URL rewriter.

The model can emit ``![alt](url)`` mid-stream. Two things have to happen
before the client sees that token:

1. The URL gets minted a uuid and is rewritten to
   ``/api/chats/{chat_id}/images/{uuid}/{filename}`` so the browser
   loads it from our proxy (which lazy-downloads on first hit).
2. For ``data:`` URLs the bytes are decoded and written to disk
   immediately as a real image asset, and the markdown is rewritten
   to the proxy URL of that asset. We never persist the inline payload.

This means the stream forwarder needs **two parallel buffers**:

- **wire** — what gets forwarded to the client. Stalls between ``![``
  and ``)`` so the client never sees a partially-rewritten URL.
- **persist** — what eventually lands on disk. Preserves the original
  URL exactly as the model emitted it (so re-rendering the chat on a
  fresh page load can resolve a missing proxy file via reverse-lookup).

End-of-stream flush: if the stream ends (cancel, done, max_tokens) while
the wire buffer still has a partial ``![…`` sequence, that buffer flushes
verbatim. Otherwise the last segment of an interrupted turn vanishes.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from server.models import new_id


log = logging.getLogger("aether.generic.image_rewrite")


# Maximum size we'll accept for a decoded ``data:`` URL. Mirrors the
# regular image-import cap so a malicious model can't fill disk.
_MAX_DATA_BYTES = 20 * 1024 * 1024


def _ext_from_data_mime(mime: str) -> str:
    """Map a MIME type from a ``data:`` URL to a file extension."""
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if mime == "image/png":
        return "png"
    if mime in ("image/jpeg", "image/jpg"):
        return "jpg"
    if mime == "image/webp":
        return "webp"
    if mime == "image/gif":
        return "gif"
    return "bin"


def _filename_from_url(url: str) -> str:
    """Derive a filename for the ``Content-Disposition`` header.

    HTTP(S) URLs: tail of the path. ``data:`` URLs: synthesized from the
    MIME type. Falls back to ``image.bin``.
    """
    if url.startswith("data:"):
        mime_part = url[5:].split(",", 1)[0]
        return f"image.{_ext_from_data_mime(mime_part)}"
    try:
        parsed = urlparse(url)
        tail = os.path.basename(parsed.path) or "image.bin"
    except Exception:
        return "image.bin"
    return tail or "image.bin"


def _proxy_url(chat_id: str, uuid: str, filename: str) -> str:
    """Build the proxy URL the client will fetch."""
    return f"/api/chats/{chat_id}/images/{uuid}/{filename}"


@dataclass
class _Output:
    """Bundled wire + persist text produced by one parser step."""

    wire: str = ""
    persist: str = ""


@dataclass
class ImageRewriter:
    """Stateful markdown ``![alt](url)`` parser for streaming text.

    Call :meth:`feed` with each delta chunk. It returns ``_Output`` with
    the chunk's worth of wire-bound text (URL-rewritten, stall-buffered)
    and persist-bound text (original URLs). Call :meth:`flush` at end of
    stream to drain any remaining buffer.

    Image refs collected at flush land in :attr:`image_refs` as
    ``{remote_url: uuid}`` for HTTP(S) URLs only. ``data:`` URLs are
    decoded to disk inline and never enter this dict.
    """

    chat_id: str
    images_dir: Path
    image_refs: dict[str, str] = field(default_factory=dict)
    # Internal stall buffer — accumulates the bytes between ``![`` and the
    # closing ``)`` so we can resolve the URL before forwarding to wire.
    _buffer: str = ""
    _in_image: bool = False

    def feed(self, delta: str) -> _Output:
        """Process one streaming delta. Returns the wire + persist slices."""
        if not delta:
            return _Output()
        out = _Output()
        if not self._in_image:
            # Look for an opening ``![`` inside the new chunk.
            idx = delta.find("![")
            if idx < 0:
                out.wire = delta
                out.persist = delta
                return out
            # Forward everything before ``![`` immediately.
            out.wire = delta[:idx]
            out.persist = delta[:idx]
            self._buffer = delta[idx:]
            self._in_image = True
            # Try to resolve in case the whole markdown landed in one chunk.
            self._try_resolve(out)
            return out
        # Already buffering — append + retry resolution.
        self._buffer += delta
        self._try_resolve(out)
        return out

    def flush(self) -> _Output:
        """End-of-stream drain. If a partial ``![…`` is still buffered,
        emit it verbatim to both wire + persist."""
        if not self._buffer:
            return _Output()
        out = _Output(wire=self._buffer, persist=self._buffer)
        self._buffer = ""
        self._in_image = False
        return out

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _try_resolve(self, out: _Output) -> None:
        """If the buffer contains a complete ``![alt](url)``, rewrite the
        URL, append to ``out``, and clear the in-image flag. Confirmed
        non-image syntax also clears (buffer flushes verbatim).
        """
        while self._in_image and self._buffer:
            # Quick sanity: we always start with ``![``.
            if not self._buffer.startswith("!["):
                # Defensive — shouldn't happen, but if it does, flush.
                out.wire += self._buffer
                out.persist += self._buffer
                self._buffer = ""
                self._in_image = False
                return
            # Find the closing ``]`` (end of alt text).
            close_bracket = self._buffer.find("]", 2)
            if close_bracket < 0:
                # Still streaming the alt text.
                return
            # The character after ``]`` decides the fate. ``(`` -> image
            # markdown; anything else -> not an image, flush the prefix.
            after = close_bracket + 1
            if after >= len(self._buffer):
                return  # need the next char
            if self._buffer[after] != "(":
                # Confirmed not image markdown. Flush ``![alt]`` verbatim
                # plus the next char that ruled it out — but careful: we
                # only flush the ``![alt]`` part, leaving the rest of the
                # buffer to be re-fed as a normal stream segment (it might
                # contain a fresh ``![``).
                prefix = self._buffer[: after]
                rest = self._buffer[after:]
                out.wire += prefix
                out.persist += prefix
                self._buffer = ""
                self._in_image = False
                if rest:
                    # Re-enter feed-like logic for the rest.
                    sub = self.feed(rest)
                    out.wire += sub.wire
                    out.persist += sub.persist
                return
            # We have ``![alt](`` — look for the closing ``)``. URL may
            # legitimately contain neither `(` nor `)` (the standard rules
            # out unescaped parens in the URL portion of image markdown),
            # so a plain ``rfind('(')...find(')')`` is enough.
            close_paren = self._buffer.find(")", after + 1)
            if close_paren < 0:
                # Still streaming the URL. NO lookahead bound — wait.
                return
            alt = self._buffer[2:close_bracket]
            url = self._buffer[after + 1 : close_paren]
            trailing = self._buffer[close_paren + 1 :]
            wire_md, persist_md = self._rewrite(alt, url)
            out.wire += wire_md
            out.persist += persist_md
            self._buffer = ""
            self._in_image = False
            if trailing:
                sub = self.feed(trailing)
                out.wire += sub.wire
                out.persist += sub.persist
            return

    def _rewrite(self, alt: str, url: str) -> tuple[str, str]:
        """Rewrite one ``![alt](url)`` for wire + persist.

        - HTTP(S) URLs → mint a uuid, register in ``image_refs``, return
          the proxy URL on wire and the original on persist.
        - ``data:`` URLs → decode to disk, return the proxy URL on BOTH
          wire and persist (we never persist the inline payload).
        - Other URLs (relative paths, mailto:, etc.) → pass through
          unchanged on both sides. Local-URL safety is enforced at the
          renderer level when sanitize is on.
        """
        url_stripped = url.strip()
        if url_stripped.startswith("data:"):
            disk_url = self._save_data_url_to_disk(url_stripped)
            if disk_url is None:
                # Decode failed — fall through to emit the data URL
                # verbatim on both sides. Better the client sees a
                # broken image than a missing message segment.
                md = f"![{alt}]({url})"
                return md, md
            md = f"![{alt}]({disk_url})"
            # Persist the proxy URL too, not the inline payload.
            return md, md
        if url_stripped.startswith(("http://", "https://")):
            uuid = self.image_refs.get(url_stripped)
            if uuid is None:
                uuid = new_id()
                self.image_refs[url_stripped] = uuid
            filename = _filename_from_url(url_stripped)
            wire = f"![{alt}]({_proxy_url(self.chat_id, uuid, filename)})"
            persist = f"![{alt}]({url})"
            return wire, persist
        # Relative or unknown scheme — leave intact.
        md = f"![{alt}]({url})"
        return md, md

    def _save_data_url_to_disk(self, url: str) -> Optional[str]:
        """Decode a ``data:image/...;base64,...`` URL to disk.

        Returns the proxy URL of the saved file, or ``None`` if decode
        failed (caller falls back to passing through verbatim).
        """
        if not url.startswith("data:"):
            return None
        header, _sep, payload = url[5:].partition(",")
        if not _sep:
            return None
        parts = header.split(";")
        mime = parts[0].strip().lower()
        is_base64 = any(p.strip().lower() == "base64" for p in parts[1:])
        if not mime.startswith("image/"):
            return None
        try:
            if is_base64:
                data = base64.b64decode(payload, validate=False)
            else:
                # URL-decoded text payload — rare for images, but handle it.
                from urllib.parse import unquote_to_bytes
                data = unquote_to_bytes(payload)
        except (binascii.Error, ValueError) as e:
            log.warning("data: URL decode failed: %s", e)
            return None
        if len(data) > _MAX_DATA_BYTES:
            log.warning("data: URL too large (%d bytes); refusing to write", len(data))
            return None
        ext = _ext_from_data_mime(mime)
        uuid = new_id()
        self.images_dir.mkdir(parents=True, exist_ok=True)
        target = self.images_dir / f"{uuid}.{ext}"
        # Atomic write so a partial file never serves.
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except OSError as e:
            log.warning("data: URL disk write failed: %s", e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return None
        return _proxy_url(self.chat_id, uuid, f"image.{ext}")
