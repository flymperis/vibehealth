"""Synthetic files for the upload tests. Nothing here is real medical data."""

from __future__ import annotations

import functools
import io
import struct
import zlib

from PIL import Image

BOUNDARY = "vhtestboundary7d3f"


@functools.lru_cache(maxsize=None)
def pdf(pages: int = 1, size: tuple[int, int] = (595, 842)) -> bytes:
    """Cached: Pillow stamps the creation time (to the second) into a PDF, so two calls a moment apart
    would give two different files, and a test that means "the same file twice" would be flaky."""
    images = [Image.new("RGB", size, "white") for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=72)
    return buf.getvalue()


def encrypted_pdf(user_password: str = "secret", owner_password: str = "owner") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf(1))))
    writer.encrypt(user_password, owner_password, algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def photo(fmt: str = "JPEG", size: tuple[int, int] = (200, 100), orientation: int | None = None, **save) -> bytes:
    """Left half red, right half blue: which way up it is can be told from the pixels."""
    image = Image.new("RGB", size, "blue")
    image.paste(Image.new("RGB", (size[0] // 2, size[1]), "red"), (0, 0))
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        save["exif"] = exif
    buf = io.BytesIO()
    image.save(buf, fmt, **save)
    return buf.getvalue()


def animated_webp(size: tuple[int, int] = (120, 80), frames: int = 3) -> bytes:
    images = [Image.new("RGB", size, colour) for colour in ("red", "green", "blue")[:frames]]
    buf = io.BytesIO()
    images[0].save(buf, "WEBP", save_all=True, append_images=images[1:], duration=100, loop=0)
    return buf.getvalue()


def header_only_png(width: int, height: int) -> bytes:
    """A PNG that claims these dimensions and carries no pixels: what a decompression bomb starts with."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 8)) + chunk(b"IEND", b"")


def multipart(
    filename: bytes | str | None,
    data: bytes,
    content_type: str = "application/octet-stream",
    fields: dict[str, str] | None = None,
    field_name: str = "file",
) -> tuple[bytes, dict[str, str]]:
    """A multipart body written by hand, so that the file name can hold any bytes (NUL, `..`)."""
    name = filename.encode() if isinstance(filename, str) else filename
    body = b""
    for key, value in (fields or {}).items():
        body += (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n").encode()
        body += value.encode() + b"\r\n"
    if name is not None:
        body += f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{field_name}\"; filename=\"".encode()
        body += name + b"\"\r\n" + f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n"
    body += f"--{BOUNDARY}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}
