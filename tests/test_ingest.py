"""pipeline/ingest.py image loading and the zoom crop of image files. No model."""
import pymupdf
from PIL import Image

from pipeline import extract, ingest


def _rotated_photo(path):
    """A photo stored sideways: 400x200 pixels, EXIF orientation 6 (viewers show 200x400).
    The left half is red, so after turning upright the top half is red."""
    im = Image.new("RGB", (400, 200), "white")
    im.paste((255, 0, 0), (0, 0, 200, 200))
    exif = Image.Exif()
    exif[0x0112] = 6
    im.save(path, exif=exif)


def test_photo_is_turned_upright(tmp_path):
    path = tmp_path / "receipt.jpg"
    _rotated_photo(path)
    page = ingest.load(path).pages[0]
    pix = pymupdf.Pixmap(page.image_png)
    assert (pix.width, pix.height) == (200, 400)
    assert pix.pixel(100, 50)[0] > 200 and pix.pixel(100, 50)[1] < 60   # red on top
    assert min(pix.pixel(100, 350)) > 200                                # white below


def test_zoom_crops_the_upright_photo(tmp_path):
    path = tmp_path / "receipt.jpg"
    _rotated_photo(path)
    text, png = extract._zoom(ingest.load(path), {"page": 1, "x0": 0, "y0": 0, "x1": 1, "y1": 0.5})
    assert png is not None, text
    crop = pymupdf.Pixmap(png)
    assert (crop.width, crop.height) == (200, 200)
    assert crop.pixel(100, 100)[0] > 200 and crop.pixel(100, 100)[1] < 60  # the red half


def test_cmyk_and_grayscale_images_load(tmp_path):
    for mode in ("CMYK", "L", "RGBA"):
        path = tmp_path / f"{mode}.{'png' if mode == 'RGBA' else 'jpg'}"
        Image.new(mode, (50, 40)).save(path)
        pix = pymupdf.Pixmap(ingest.load(path).pages[0].image_png)
        assert (pix.width, pix.height, pix.n) == (50, 40, 3)
