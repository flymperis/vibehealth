"""The code that runs INSIDE the sandbox child process (see sandbox.py for the server side).

A hostile PDF or image is parsed by native code (pdfium, Pillow's codecs). That parsing happens
here, in a short-lived process that the server can kill and that has resource limits (POSIX), never
in the server itself. The child imports nothing of the application except `filelimits` and
`render` (which need only pypdfium2 and Pillow): no database, no settings, no secrets.

Protocol. The server starts `python -c <boot>` and writes ONE line of JSON on stdin:
    {"cmd": "...", "args": {...}, "limits": {...}}
The child applies the limits, does the work and writes ONE frame on the real stdout:
    b"VHSB1" + u32 meta_len + u32 payload_len + meta (JSON) + payload (raw bytes)
`meta` always has `ok`; on failure it has `message` (a fixed sentence written here). Nothing is
pickled in either direction. Everything else a library prints goes to the null device.

Commands (all take a path inside the data folder, made by the server, and never a client's name):
    check_upload {path, kind}          validate, and make the thumbnail (payload: JPEG)
    thumbnail    {path, kind}          the thumbnail only (payload: JPEG)
    page_count   {path}                pages of a stored PDF
    render_page  {path, kind, index, dpi}   one page as PNG (payload), as the readers get it
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import warnings

import pypdfium2 as pdfium
from PIL import Image

from . import filelimits, render

MAGIC = b"VHSB1"
HEADER = struct.Struct(">II")

# what Pillow may open for each accepted kind. MPO is the multi-picture JPEG that many phones write
# (a JPEG whose extra pictures are appended): Pillow reports its first picture as format "MPO".
IMAGE_FORMATS = {"jpeg": ["JPEG", "MPO"], "png": ["PNG"], "webp": ["WEBP"]}


class Refusal(Exception):
    """The file is not acceptable / not readable. The message is one of ours and is shown to the user."""


# --- limits (POSIX only) ------------------------------------------------------------------------


def apply_limits(limits: dict) -> list[str]:
    """Lower this process's resource limits before any file is opened. Returns the names applied.
    Where the `resource` module does not exist (Windows) nothing is applied and the list is empty:
    the server then relies on the wall-clock timeout alone."""
    try:
        import resource
    except ImportError:
        return []
    applied: list[str] = []

    def setlimit(name: str, soft: int | None, hard: int | None = None) -> None:
        which = getattr(resource, name, None)
        if which is None or soft is None:
            return
        try:
            _cur_soft, cur_hard = resource.getrlimit(which)
            hard = soft if hard is None else hard
            if cur_hard != resource.RLIM_INFINITY:  # a limit can be lowered, never raised
                soft, hard = min(soft, cur_hard), min(hard, cur_hard)
            resource.setrlimit(which, (soft, hard))
            applied.append(name.removeprefix("RLIMIT_").lower())
        except (ValueError, OSError):
            pass

    memory = limits.get("memory_bytes")
    # address space (a blow-up ends in MemoryError / a kill), or - when the server found that too strict for
    # this pdfium build - the private writable memory only
    setlimit("RLIMIT_DATA" if limits.get("memory_kind") == "data" else "RLIMIT_AS", int(memory) if memory else None)
    cpu = limits.get("cpu_seconds")
    setlimit("RLIMIT_CPU", int(cpu) if cpu else None, int(cpu) + 5 if cpu else None)  # SIGXCPU, then SIGKILL
    setlimit("RLIMIT_CORE", 0)  # no core dump of a medical file
    files = limits.get("open_files")
    setlimit("RLIMIT_NOFILE", int(files) if files else None)
    return applied


# --- frames -------------------------------------------------------------------------------------


def _frame(meta: dict, payload: bytes = b"") -> bytes:
    body = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return MAGIC + HEADER.pack(len(body), len(payload)) + body + payload


def _refuse(message: str) -> bytes:
    return _frame({"ok": False, "message": message})


# --- images -------------------------------------------------------------------------------------


def _open_image(path: str, kind: str):
    return Image.open(path, formats=IMAGE_FORMATS[kind])


def _validate_image(path: str, kind: str) -> None:
    try:
        with _open_image(path, kind) as image:
            width, height = image.size  # from the header: nothing is decoded yet
            if (width < 1 or height < 1 or max(width, height) > filelimits.MAX_IMAGE_SIDE
                    or width * height > filelimits.MAX_IMAGE_PIXELS):
                raise Refusal(f"this image is too large ({width} x {height} pixels)")
            # verify() reads the structure (PNG: the chunk checksums); for a JPEG it checks next to
            # nothing, and it never decodes a pixel. It is a cheap first look, not the protection:
            # the full decode of the first frame below is what proves the file can be read.
            image.verify()
        with _open_image(path, kind) as image:  # verify() leaves the image unusable: open it again
            image.load()
    except Refusal:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise Refusal("this image is too large") from None
    except Exception:  # noqa: BLE001 - whatever a codec throws at a hostile file, MemoryError included
        raise Refusal("this image could not be read: it looks damaged") from None


def _image_thumbnail(path: str, kind: str) -> bytes:
    with _open_image(path, kind) as source:
        if source.format in ("JPEG", "MPO"):
            source.draft("RGB", (filelimits.THUMB_SIDE * 2, filelimits.THUMB_SIDE * 2))  # decode at a fraction
        image = render.upright_rgb(source)
    return _jpeg(image)


def _jpeg(image) -> bytes:
    render.shrink(image, filelimits.THUMB_SIDE)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _image_page(path: str, kind: str, dpi: int) -> bytes:
    """What the readers get for an uploaded photo: upright, RGB, at most UPLOAD_IMAGE_SIDE, then the
    same dpi rule as any scan (render.fit_scan). Equal to prepare_image + Pages.png, minus the
    lossless PNG round trip in between."""
    with _open_image(path, kind) as source:
        image = render.shrink(render.upright_rgb(source), render.UPLOAD_IMAGE_SIDE)
    render.fit_scan(image, dpi)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


# --- PDFs ---------------------------------------------------------------------------------------


def _open_pdf(path: str):
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        # A PDF that needs a password to open. (One with only an owner password opens with the empty
        # user password and is accepted: it is readable, and "encrypted" is not a reason to refuse it.)
        if "password" in str(exc).lower():
            raise Refusal("this PDF is password-protected: remove the protection and upload it again") from None
        raise Refusal("this PDF could not be read: it looks damaged") from None
    except Exception:  # noqa: BLE001
        raise Refusal("this PDF could not be read: it looks damaged") from None


def _validate_pdf(pdf) -> int:
    pages = len(pdf)
    if pages < 1:
        raise Refusal("this PDF has no pages")
    if pages > filelimits.MAX_PDF_PAGES:
        raise Refusal(f"this PDF has {pages} pages: at most {filelimits.MAX_PDF_PAGES} are accepted")
    for index in range(pages):
        try:
            page = pdf[index]
        except Exception:  # noqa: BLE001
            raise Refusal("this PDF could not be read: a page is damaged") from None
        try:
            width, height = page.get_size()
        finally:
            page.close()
        limit = filelimits.MAX_PDF_PAGE_POINTS
        if not (0 < width <= limit and 0 < height <= limit):
            raise Refusal("this PDF has a page of an unusable size")
    return pages


def _pdf_thumbnail(pdf) -> bytes:
    page = pdf[0]
    try:
        width, height = page.get_size()
        image = page.render(scale=filelimits.THUMB_SIDE / max(width, height, 1)).to_pil().convert("RGB")
    finally:
        page.close()
    return _jpeg(image)


# --- commands -----------------------------------------------------------------------------------


def _path(args: dict) -> str:
    path = args.get("path")
    if not isinstance(path, str) or not path or "\0" in path:
        raise Refusal("this file could not be read")
    return path


def _kind(args: dict) -> str:
    kind = args.get("kind")
    if kind not in ("pdf", *IMAGE_FORMATS):
        raise Refusal("this file could not be read")
    return kind


def cmd_check_upload(args: dict) -> tuple[dict, bytes]:
    path, kind = _path(args), _kind(args)
    if kind == "pdf":
        with render.PDFIUM_LOCK:
            pdf = _open_pdf(path)
            try:
                pages = _validate_pdf(pdf)
                try:
                    thumb = _pdf_thumbnail(pdf)
                except Exception:  # noqa: BLE001
                    raise Refusal("this PDF could not be read: its first page cannot be drawn") from None
            finally:
                pdf.close()
        return {"ok": True, "pages": pages}, thumb
    _validate_image(path, kind)
    try:
        thumb = _image_thumbnail(path, kind)
    except Exception:  # noqa: BLE001
        raise Refusal("this image could not be read: it looks damaged") from None
    return {"ok": True, "pages": 1}, thumb


def cmd_thumbnail(args: dict) -> tuple[dict, bytes]:
    path, kind = _path(args), _kind(args)
    if kind == "pdf":
        with render.PDFIUM_LOCK:
            pdf = _open_pdf(path)
            try:
                return {"ok": True}, _pdf_thumbnail(pdf)
            finally:
                pdf.close()
    return {"ok": True}, _image_thumbnail(path, kind)


def cmd_page_count(args: dict) -> tuple[dict, bytes]:
    with render.PDFIUM_LOCK:
        pdf = _open_pdf(_path(args))
        try:
            return {"ok": True, "pages": len(pdf)}, b""
        finally:
            pdf.close()


def cmd_render_page(args: dict) -> tuple[dict, bytes]:
    path, kind = _path(args), _kind(args)
    dpi, index = args.get("dpi"), args.get("index")
    if not isinstance(dpi, int) or not 20 <= dpi <= 1200 or not isinstance(index, int) or index < 0:
        raise Refusal("this page could not be drawn")
    if kind != "pdf":
        return {"ok": True}, _image_page(path, kind, dpi)
    with render.PDFIUM_LOCK:
        pdf = _open_pdf(path)
        try:
            if index >= len(pdf):
                raise Refusal("this page could not be drawn")
            image = render.render_pdf_page(pdf, index, dpi)
        finally:
            pdf.close()
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "PNG")
    return {"ok": True}, buf.getvalue()


COMMANDS = {
    "check_upload": cmd_check_upload,
    "thumbnail": cmd_thumbnail,
    "page_count": cmd_page_count,
    "render_page": cmd_render_page,
}


# --- entry point --------------------------------------------------------------------------------


def main() -> None:
    """Read the request, apply the limits, do the work, write the frame, exit."""
    out = os.fdopen(os.dup(1), "wb")  # the real stdout, kept for the frame ...
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)  # ... and stdout itself goes nowhere: a library that prints cannot corrupt it
    os.close(devnull)
    try:
        line = sys.stdin.buffer.readline(1 << 16)
        request = json.loads(line)
        command, args, limits = request["cmd"], request["args"], request.get("limits", {})
        if not isinstance(args, dict) or not isinstance(limits, dict):
            raise ValueError
    except Exception:  # noqa: BLE001
        out.write(_refuse("this file could not be read"))
        out.flush()
        return

    applied = apply_limits(limits)
    # A decompression-bomb warning is an error here: this process does one job, so a global filter is fine.
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    try:
        handler = COMMANDS[command]
        meta, payload = handler(args)
        meta["limits"] = applied
        frame = _frame(meta, payload)
    except Refusal as exc:
        frame = _frame({"ok": False, "message": str(exc), "limits": applied})
    except MemoryError:
        frame = _frame({"ok": False, "message": "this file is too large or too complex to read", "limits": applied})
    except Exception as exc:  # noqa: BLE001
        frame = _frame({"ok": False, "message": "this file could not be read", "error": type(exc).__name__,
                        "limits": applied})
    out.write(frame)
    out.flush()


if __name__ == "__main__":  # pragma: no cover - the server starts it with -c, see sandbox.py
    main()
