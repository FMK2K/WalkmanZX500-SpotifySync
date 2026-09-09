"""
cover_art.py  —  Baseline-JPEG cover art normalisation (Rev 14)
================================================================
Prevents the ZX500 "no cover art shown in browser" defect — Hypothesis C.

Why this module exists
----------------------
The ZX500 and the Sony Music Player app both render embedded cover art
from the ID3 APIC / FLAC Picture frame, but they're picky about format.
Per the Head-Fi/Sony forums community testing (documented in the PDF
analyses), covers fail to render when:

  - The JPEG is encoded as progressive rather than baseline.
  - The image is PNG (ZX500 firmware decodes PNGs inconsistently).
  - The embedded bytes are >~256 KB (older firmware truncates; newer
    firmware decodes but takes ~1s per scroll, making the browser
    sluggish).
  - The image is larger than ~600x600 (same browser-sluggishness).

Rev 13's `_embed_cover_art()` in audit.py:

    - Detects PNG via magic bytes and sets the APIC mime accordingly,
      but does NOT convert to JPEG.  Result: PNGs go in as PNGs.
    - Embeds the raw bytes verbatim.  No re-encode, no size cap, no
      baseline check.  Progressive JPEGs from Spotify/Apple album art
      CDNs go straight through.
    - Has no Pillow import anywhere in the file.

This module fixes all four with a single normaliser: any input (PNG,
progressive JPEG, huge JPEG) becomes a baseline JPEG, ≤500px on the long
edge, ≤~90 KB on disk, suitable for APIC embedding.

If Pillow is not installed, the normaliser degrades to a passthrough
with a warning — the module is NOT a hard dependency.

Public API
----------
    normalize_bytes(img_bytes: bytes) -> tuple[bytes, str]
        Returns (normalised_bytes, mime).  mime is always "image/jpeg"
        on success; on passthrough it reflects the input.

    build_apic(img_bytes: bytes) -> APIC
        Convenience — returns a ready-to-add mutagen APIC frame.

    build_flac_picture(img_bytes: bytes) -> Picture
        Convenience — returns a ready-to-add mutagen FLAC Picture.
"""
from __future__ import annotations

import io
import logging

import config

log = logging.getLogger(__name__)

MAX_PIXELS = getattr(config, "COVER_ART_MAX_PIXELS", 500)
MAX_BYTES  = getattr(config, "COVER_ART_MAX_BYTES",  90 * 1024)
JPEG_QUALITY_START = 90
JPEG_QUALITY_FLOOR = 60


# ---------------------------------------------------------------------------
# Core normaliser
# ---------------------------------------------------------------------------

def normalize_bytes(img_bytes: bytes) -> tuple[bytes, str]:
    """
    Convert *img_bytes* to baseline JPEG ≤MAX_PIXELS ≤MAX_BYTES.

    If Pillow is unavailable OR the bytes are already compliant (baseline
    JPEG, small enough, small dimensions), returns the input unchanged.

    Never raises — on any failure, returns (input_bytes, detected_mime)
    so the caller can fall back to embedding the raw bytes if needed.
    """
    if not img_bytes:
        return img_bytes, "image/jpeg"

    # Detect mime from magic bytes — useful for the passthrough case too.
    in_mime = "image/jpeg"
    if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        in_mime = "image/png"

    try:
        from PIL import Image  # type: ignore
    except ImportError:
        log.debug(
            "  [cover_art] Pillow not installed — embedding raw bytes. "
            "Progressive JPEGs and PNGs may not render on ZX500. "
            "Install with: pip install pillow"
        )
        return img_bytes, in_mime

    try:
        im = Image.open(io.BytesIO(img_bytes))
        im.load()
    except Exception as e:
        log.debug(f"  [cover_art] Pillow could not decode input: {e}")
        return img_bytes, in_mime

    # Fast-path: already baseline JPEG, small enough, small dimensions
    try:
        is_jpeg   = (im.format or "").upper() == "JPEG"
        is_prog   = bool(im.info.get("progressive") or im.info.get("progression"))
        small_dim = max(im.size) <= MAX_PIXELS
        small_sz  = len(img_bytes) <= MAX_BYTES
        if is_jpeg and not is_prog and small_dim and small_sz:
            return img_bytes, "image/jpeg"
    except Exception:
        pass  # fall through to re-encode

    # Re-encode path
    try:
        # Flatten alpha against white — ZX500's decoder treats transparent
        # PNG regions as black, which looks wrong on most covers.
        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")

        # Resize if needed (preserve aspect ratio)
        if max(im.size) > MAX_PIXELS:
            # Pillow 10+ uses Image.Resampling.LANCZOS; older uses LANCZOS.
            try:
                resample = Image.Resampling.LANCZOS  # type: ignore
            except AttributeError:
                resample = Image.LANCZOS  # type: ignore
            im.thumbnail((MAX_PIXELS, MAX_PIXELS), resample)

        # Encode, dropping quality until under size cap.  Never below 60.
        q = JPEG_QUALITY_START
        while q >= JPEG_QUALITY_FLOOR:
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=q, optimize=True, progressive=False)
            out = buf.getvalue()
            if len(out) <= MAX_BYTES:
                return out, "image/jpeg"
            q -= 10

        # Gave up on quality — return whatever we have at floor
        return out, "image/jpeg"

    except Exception as e:
        log.debug(f"  [cover_art] Re-encode failed: {e} — embedding raw")
        return img_bytes, in_mime


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------

def build_apic(img_bytes: bytes):
    """Return a mutagen APIC frame with normalised bytes, front-cover type."""
    from mutagen.id3 import APIC
    data, mime = normalize_bytes(img_bytes)
    return APIC(
        encoding=3,     # UTF-8
        mime=mime,
        type=3,         # front cover
        desc="Cover",
        data=data,
    )


def build_flac_picture(img_bytes: bytes):
    """Return a mutagen FLAC Picture object with normalised bytes."""
    from mutagen.flac import Picture
    data, mime = normalize_bytes(img_bytes)
    pic = Picture()
    pic.type = 3        # front cover
    pic.mime = mime
    pic.desc = "Cover"
    pic.data = data
    return pic
