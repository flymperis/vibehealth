"""Page images for the readers, rendered in memory (nothing is written to disk)."""

from __future__ import annotations

import io
import threading

import pypdfium2 as pdfium
from PIL import Image, ImageOps

MAX_IMAGE_SIDE = 3000  # photos straight from a phone are far bigger than the readers need

# No page is rendered above this on its long side, whatever the dpi setting says. A4 at the 150 dpi
# default is 1754 px, at 200 dpi 2339 px, at the 300 dpi upper bound of the setting 3508 px (cut to
# this); a page drawn 2000 pt wide would otherwise be 4200+ px at 150 dpi. Nothing below the limit
# changes: the scale is `dpi / 72` exactly as before.
MAX_RENDER_SIDE = 3500

# An uploaded photo is brought down to this before it reaches the readers. A PDF page at the default
# 150 dpi is 1754 px on its long side (A4) and 2339 px at the 200 dpi fallback, which is what the
# models are used to and read reliably; a photo of a page needs about the same to keep small print
# legible, and every extra pixel is more vision tokens (slower, and a 4B model gets no better at it).
# 2200 px is A4 at ~190 dpi: no loss against the PDF path, ~3x fewer pixels than a 12 MP phone photo.
UPLOAD_IMAGE_SIDE = 2200

# PDFium is not thread-safe, not even across different documents. Every PDFium call in this process
# goes through this lock, and only ever from a worker thread (never the event loop). Since uploads are
# parsed in a sandbox child (sandbox.py), the only in-process user is the reading of a Paperless
# document, which the reading loop does one document at a time. The child is single-threaded and
# takes the same lock, which is never contended there.
PDFIUM_LOCK = threading.RLock()


def page_scale(width_pt: float, height_pt: float, dpi: int) -> float:
    """The pdfium render scale for `dpi`, lowered when the page would come out above MAX_RENDER_SIDE."""
    scale = dpi / 72
    longest = max(width_pt, height_pt)
    if longest > 0 and longest * scale > MAX_RENDER_SIDE:
        scale = MAX_RENDER_SIDE / longest
    return scale


def render_pdf_page(pdf, index: int, dpi: int) -> Image.Image:
    """One page of an open PdfDocument as a PIL image. The caller holds PDFIUM_LOCK."""
    page = pdf[index]
    try:
        width, height = page.get_size()
        return page.render(scale=page_scale(width, height, dpi)).to_pil()
    finally:
        page.close()


def fit_scan(image: Image.Image, dpi: int) -> Image.Image:
    """In place: a scan carries no page size, so the stated dpi is taken relative to 150 (the default)
    and the picture is shrunk to that, never above MAX_IMAGE_SIDE and never enlarged."""
    scale = dpi / 150
    longest = max(image.size)
    target = min(MAX_IMAGE_SIDE, int(longest * scale)) if scale < 1 else min(MAX_IMAGE_SIDE, longest)
    if longest > target:
        image.thumbnail((target, target))
    return image


class Pages:
    """A PDF or a single image, rendered page by page at any dpi."""

    def __init__(self, data: bytes, media_type: str = "") -> None:
        self._pdf = None
        self._image = None
        if data[:5] == b"%PDF-" or "pdf" in media_type:
            with PDFIUM_LOCK:
                self._pdf = pdfium.PdfDocument(data)
        else:
            self._image = Image.open(io.BytesIO(data))
            self._image.load()

    def __len__(self) -> int:
        return len(self._pdf) if self._pdf is not None else 1

    def png(self, index: int, dpi: int) -> bytes:
        if self._pdf is not None:
            with PDFIUM_LOCK:
                image = render_pdf_page(self._pdf, index, dpi)
        else:
            image = fit_scan(self._image.copy(), dpi)
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "PNG")
        return buf.getvalue()

    def close(self) -> None:
        if self._pdf is not None:
            with PDFIUM_LOCK:
                self._pdf.close()
            self._pdf = None
        self._image = None


# --- uploaded images ----------------------------------------------------------------------------


def upright_rgb(image: Image.Image) -> Image.Image:
    """The picture as a person sees it: EXIF orientation applied (a phone photo is often stored
    sideways with a flag), transparency flattened onto white, RGB. A new image; the input is not
    changed. Only the first frame of an animated file is used (Pillow opens frame 0)."""
    image = ImageOps.exif_transpose(image)  # a copy, with the orientation tag removed
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        image = image.point(lambda v: v * (1 / 256)).convert("L")
    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return image.convert("RGB")


def shrink(image: Image.Image, longest: int) -> Image.Image:
    """In place: no side longer than `longest`, aspect ratio kept, never enlarged."""
    if max(image.size) > longest:
        image.thumbnail((longest, longest), Image.Resampling.LANCZOS)
    return image


def prepare_image(data: bytes, longest: int = UPLOAD_IMAGE_SIDE, formats: list[str] | None = None) -> bytes:
    """An uploaded JPEG / PNG / WebP as an upright RGB PNG of at most `longest` px, ready for
    `Pages`. Used for uploads only: images from Paperless go through `Pages` exactly as before."""
    with Image.open(io.BytesIO(data), formats=formats) as source:
        image = shrink(upright_rgb(source), longest)
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()
