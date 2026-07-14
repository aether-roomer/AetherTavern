"""Build small, pre-cropped WebP derivatives of avatars and emotion sprites.

The originals (PNG/JPEG/WebP/GIF/...) live alongside their derivatives on
disk; the derivatives are what every list row, chat bubble, and edit-view
preview actually loads. This keeps the UI snappy when contacts have
multi-megabyte source images. The crop modal still operates on the
original so the user can re-crop without compounding loss; whenever the
crop changes the derivative is regenerated.
"""
from __future__ import annotations

import io
import logging
import math
from pathlib import Path

from PIL import Image, ImageOps

from server.models import CropRect


log = logging.getLogger(__name__)


# Tuned for the largest UI surface (the 120 px avatar preview on the edit
# page, ~110 px chat emotion sprite). 384 px gives us ~1.5x DPR headroom
# without going overboard. WebP at quality 85 is visually transparent for
# photo-style content while keeping derivative sizes around 30-80 KB.
DISPLAY_MAX_EDGE = 384
DISPLAY_QUALITY = 85
DISPLAY_SUFFIX = ".display.webp"

# Picker-row thumbnails for bulk-import flows. Tiny + low-quality on
# purpose: rows are 40 px tall and the user sees ~20 of them at a time,
# so we want each thumbnail under 5 KB to keep the picker manifest small
# and the lazy-image network round trip cheap. ``method=0`` is libwebp's
# fastest encoder — the visual difference at 64 px is invisible.
PREVIEW_MAX_EDGE = 64
PREVIEW_QUALITY = 60

# Default quality for the multimodal-upload JPEG compressor. The Settings
# field ``image_compression_quality`` overrides this per-install; it's also
# the value the encoder clamps toward.
COMPRESS_QUALITY_DEFAULT = 85

# Pixel-area cap for that compressor. Images ABOVE this are downscaled
# (aspect-ratio preserving) so total area lands at/below the cap; smaller
# images are left untouched — never upscaled. Vision models bill by
# pixels/tiles, so an area cap bounds per-image cost across any aspect ratio
# (1280x1920 worth of area; the longest edge may still exceed 1920 on very
# wide/tall images as long as the area fits).
COMPRESS_MAX_AREA = 1280 * 1920


def _crop_box(rect: CropRect | None, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Convert a normalised ``CropRect`` into pixel bounds for ``Image.crop``.

    Returns ``None`` if no crop is set or the rect covers the full image.
    """
    if rect is None:
        return None
    w, h = size
    x = max(0, int(round(rect.x * w)))
    y = max(0, int(round(rect.y * h)))
    cw = max(1, int(round(rect.w * w)))
    ch = max(1, int(round(rect.h * h)))
    cw = min(cw, w - x)
    ch = min(ch, h - y)
    if x == 0 and y == 0 and cw == w and ch == h:
        return None
    return (x, y, x + cw, y + ch)


def _encode_webp(im: Image.Image, *, max_edge: int, quality: int, method: int) -> bytes:
    """Downscale ``im`` to fit ``max_edge`` then encode as WebP bytes."""
    if max(im.size) > max_edge:
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="WEBP", quality=quality, method=method)
    return out.getvalue()


def build_display_bytes(src: bytes, crop: CropRect | None) -> bytes:
    """Build a downsized, pre-cropped WebP for display surfaces.

    ``crop`` is baked in (the result has no further crop metadata); the
    longer edge is capped at :data:`DISPLAY_MAX_EDGE`.
    """
    with Image.open(io.BytesIO(src)) as im:
        # Animations (GIF/WebP) — flatten to the first frame; the display
        # surfaces are static thumbnails.
        if getattr(im, "is_animated", False):
            im.seek(0)
        # Honour EXIF orientation for JPEGs out of phones / scans.
        im = ImageOps.exif_transpose(im)
        # Drop palette / alpha-only modes to RGBA so WebP encodes cleanly.
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")

        box = _crop_box(crop, im.size)
        if box is not None:
            im = im.crop(box)

        # method=4 is libwebp's default — a few percent larger output than
        # method=6 (our derivatives are 30-80 KB, so this lands at ~35-90)
        # but 3-5x faster to encode. With 24 emotion sprites per import the
        # difference between the two is the difference between an import
        # finishing in seconds vs. dozens of seconds.
        return _encode_webp(im, max_edge=DISPLAY_MAX_EDGE, quality=DISPLAY_QUALITY, method=4)


def build_preview_bytes(src: bytes) -> bytes:
    """Tiny picker thumbnail. Smaller and lower-quality than display
    derivatives, with libwebp's fastest encode setting — the picker
    needs many of these and the visual difference is imperceptible at
    :data:`PREVIEW_MAX_EDGE` pixels.

    No crop input: the bulk-import picker shows the source image whole,
    not a focal-point square.
    """
    with Image.open(io.BytesIO(src)) as im:
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        return _encode_webp(im, max_edge=PREVIEW_MAX_EDGE, quality=PREVIEW_QUALITY, method=0)


def build_compressed_jpeg_bytes(src: bytes, quality: int = COMPRESS_QUALITY_DEFAULT) -> bytes:
    """Re-encode an image as a JPEG for multimodal upload.

    Two size levers: an area cap (:data:`COMPRESS_MAX_AREA`) downscales
    oversized images aspect-ratio-preserving (smaller images pass through
    untouched — never upscaled), and JPEG quantization shrinks what remains.
    Callers compare the result against the original and forward whichever is
    smaller (a simple palette PNG can beat its own JPEG re-encode, so the
    upload never grows).

    Alpha is flattened onto white (JPEG has no alpha channel); animations
    collapse to their first frame; EXIF orientation is baked in. Raises if
    Pillow can't decode ``src`` — callers fall back to the original bytes.
    """
    quality = max(1, min(100, int(quality)))
    with Image.open(io.BytesIO(src)) as im:
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = ImageOps.exif_transpose(im)
        if im.mode == "RGB":
            flat = im
        elif "A" in im.getbands() or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
        else:
            flat = im.convert("RGB")
        w, h = flat.size
        if w * h > COMPRESS_MAX_AREA:
            scale = math.sqrt(COMPRESS_MAX_AREA / (w * h))
            flat = flat.resize(
                (max(1, round(w * scale)), max(1, round(h * scale))),
                Image.LANCZOS,
            )
        out = io.BytesIO()
        flat.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()


def display_path_for(original: Path) -> Path:
    """Return the display-derivative path that sits next to ``original``."""
    return original.with_name(original.stem + DISPLAY_SUFFIX)


def build_display_file(original: Path, crop: CropRect | None) -> Path | None:
    """Generate the display sibling for ``original`` and return its path.

    Returns ``None`` and logs a warning if the original is missing or
    cannot be decoded — the display URL falls back to the original in
    that case.
    """
    if not original.exists():
        return None
    try:
        data = original.read_bytes()
        out = build_display_bytes(data, crop)
    except Exception as e:  # pragma: no cover — Pillow has many error types
        log.warning("Could not build display image for %s: %s", original, e)
        return None
    target = display_path_for(original)
    # Atomic write — same pattern as storage.atomic_write_bytes.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(out)
    tmp.replace(target)
    return target


def remove_display_siblings(*originals: Path) -> None:
    """Best-effort delete of the display sibling for each path."""
    for original in originals:
        try:
            display_path_for(original).unlink(missing_ok=True)
        except OSError:
            pass
