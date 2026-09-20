"""Units of the upload machinery: names, magic bytes, paths, clean-up, image preparation."""

import io
import os
import time

import pytest
from PIL import Image
from upload_helpers import animated_webp, photo

from app import render, uploads


# --- names -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,stem", [
    ("report.pdf", "report"),
    ("../../x.pdf", "x"),
    ("/etc/passwd", "passwd"),
    ("C:\\Users\\me\\Desktop\\lab.pdf", "lab"),
    ("a/b\\c.d.pdf", "c.d"),
    ("we\x00ird\x07.pdf", "weird"),
    ("con", "_con"),
    ("NUL.txt", "_NUL"),
    ("Lpt3.pdf", "_Lpt3"),
    ("com10.pdf", "com10"),  # only COM1-COM9 are devices
    (".", "upload"),
    ("...", "upload"),
    ("", "upload"),
    (None, "upload"),
    ("  spaced name  .pdf", "spaced name"),
    ("a<b>c:d\"e|f?g*h.pdf", "abcdefgh"),
    ("x" * 500 + ".pdf", "x" * 100),
    ("re\u202eport.pdf", "report"),  # a right-to-left override is not shown
    ("Ηλεκτρολύτες.pdf", "Ηλεκτρολύτες"),
    ("e\u0301.pdf", "\u00e9"),  # normalised
])
def test_name_stem(raw, stem):
    assert uploads.name_stem(raw) == stem


def test_display_name_uses_the_extension_of_the_bytes():
    assert uploads.display_name("evil.exe", "pdf") == "evil.pdf"
    assert uploads.display_name("x.pdf", "jpeg") == "x.jpg"
    assert uploads.display_name(None, "webp") == "upload.webp"


def test_titles():
    assert uploads.clean_title("  Blood \n test\t 2025 ") == "Blood test 2025"
    for bad in ("", "  \x00 ", "x" * 201):
        with pytest.raises(ValueError):
            uploads.clean_title(bad)


# --- magic bytes ---------------------------------------------------------------------------------------


def test_sniff():
    assert uploads.sniff(b"%PDF-1.7") == "pdf"
    assert uploads.sniff(b"\xff\xd8\xff\xe1xx") == "jpeg"
    assert uploads.sniff(b"\x89PNG\r\n\x1a\nxxxx") == "png"
    assert uploads.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    for other in (b"", b"%PDF", b" %PDF-1.4", b"<html>", b"GIF89a", b"BM", b"RIFF\x00\x00\x00\x00WAVE", b"PK\x03\x04",
                  b"\xff\xd8", b"MZ\x90\x00"):
        assert uploads.sniff(other) is None, other


# --- paths ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    None, "", "../vibehealth.db", "../../etc/passwd", "ab/../../x", "/etc/passwd", "\\windows\\x", "C:\\x",
    "ab/cd\x00.pdf", ".", "..", "ab/../..",
])
def test_stored_file_refuses_anything_outside_uploads(bad):
    assert uploads.stored_file(bad) is None


def test_stored_file_accepts_what_store_makes(tmp_path):
    tmp = os.path.join(uploads.tmp_root(), "x")
    with open(tmp, "wb") as f:
        f.write(b"%PDF-1.4")
    relative = uploads.store(tmp, "pdf")
    path = uploads.stored_file(relative)
    assert path and os.path.isfile(path) and not os.path.exists(tmp)
    assert os.path.commonpath([os.path.realpath(uploads.root()), path]) == os.path.realpath(uploads.root())


# --- the start-up sweep ------------------------------------------------------------------------------------


def test_sweep_removes_old_temp_files_only():
    old, fresh = (os.path.join(uploads.tmp_root(), n) for n in ("old", "fresh"))
    for path in (old, fresh):
        with open(path, "wb") as f:
            f.write(b"partial")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    almost = os.path.join(uploads.tmp_root(), "almost")
    with open(almost, "wb") as f:
        f.write(b"x")
    os.utime(almost, (time.time() - 3500, time.time() - 3500))

    result = uploads.sweep_at_start()
    assert result["tmp"] == 1
    assert not os.path.exists(old) and os.path.exists(fresh) and os.path.exists(almost)


def test_sweep_leaves_original_files_that_no_document_points_at():
    """After restoring an older database an original may have no row: it is still the only copy."""
    tmp = os.path.join(uploads.tmp_root(), "y")
    with open(tmp, "wb") as f:
        f.write(b"%PDF-1.4")
    relative = uploads.store(tmp, "pdf")
    uploads.sweep_at_start()
    assert os.path.isfile(uploads.stored_file(relative))


def test_sweep_drops_thumbnails_nobody_owns():
    stray = os.path.join(uploads.thumbs_root(), "a" * 64 + ".jpg")
    with open(stray, "wb") as f:
        f.write(b"x")
    assert uploads.sweep_at_start()["thumbs"] == 1 and not os.path.exists(stray)


def test_sweep_does_not_delete_a_pending_file_that_a_document_owns_again():
    tmp = os.path.join(uploads.tmp_root(), "z")
    with open(tmp, "wb") as f:
        f.write(b"%PDF-1.4")
    relative = uploads.store(tmp, "pdf")
    with open(os.path.join(uploads.root(), ".pending-delete"), "w") as f:
        f.write(relative + "\n../../outside\n\n")
    result = uploads.startup_sweep(set(), {relative})
    assert result["pending"] == 0 and os.path.isfile(uploads.stored_file(relative))
    assert not os.path.exists(os.path.join(uploads.root(), ".pending-delete"))


# --- images for the readers -------------------------------------------------------------------------------


def dims(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


def test_exif_orientation_is_applied():
    for orientation, expected in ((1, (200, 100)), (3, (200, 100)), (6, (100, 200)), (8, (100, 200))):
        assert dims(render.prepare_image(photo("JPEG", orientation=orientation))) == expected, orientation


def test_exif_rotation_moves_the_pixels_not_just_the_size():
    # left half red / right half blue, flagged "rotate 90 degrees clockwise to view" (6): the red half ends on top
    out = Image.open(io.BytesIO(render.prepare_image(photo("JPEG", size=(200, 100), orientation=6))))
    top, bottom = out.getpixel((50, 10)), out.getpixel((50, 190))
    assert top[0] > 200 and top[2] < 80 and bottom[2] > 200 and bottom[0] < 80


def test_images_are_shrunk_to_the_upload_side_and_never_enlarged():
    assert render.UPLOAD_IMAGE_SIDE == 2200
    assert max(dims(render.prepare_image(photo("JPEG", size=(4000, 3000))))) == 2200
    assert dims(render.prepare_image(photo("PNG", size=(4000, 3000)))) == (2200, 1650)
    assert dims(render.prepare_image(photo("PNG", size=(300, 200)))) == (300, 200)


def test_animated_webp_uses_its_first_frame():
    out = Image.open(io.BytesIO(render.prepare_image(animated_webp(size=(120, 80)))))
    assert out.size == (120, 80) and out.mode == "RGB" and getattr(out, "n_frames", 1) == 1
    assert out.getpixel((60, 40))[0] > 200  # the first frame is the red one


def test_transparency_is_flattened_onto_white_and_other_modes_become_rgb():
    rgba = Image.new("RGBA", (10, 10), (255, 0, 0, 0))  # fully transparent
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    out = Image.open(io.BytesIO(render.prepare_image(buf.getvalue())))
    assert out.mode == "RGB" and out.getpixel((5, 5)) == (255, 255, 255)
    for mode in ("L", "P", "1", "CMYK", "LA"):
        buf = io.BytesIO()
        Image.new(mode, (20, 20)).save(buf, "PNG" if mode != "CMYK" else "JPEG")
        assert Image.open(io.BytesIO(render.prepare_image(buf.getvalue()))).mode == "RGB", mode


def test_pages_still_treats_a_paperless_image_as_before():
    """Images from Paperless do not go through prepare_image: same 3000 px rule, no EXIF turn."""
    pages = render.Pages(photo("JPEG", size=(4000, 1000), orientation=6), "image/jpeg")
    assert len(pages) == 1 and dims(pages.png(0, 150)) == (3000, 750)
    pages.close()
