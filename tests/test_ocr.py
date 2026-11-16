"""OCR path on a synthetic scan (rendered and rasterised). Runs where Poppler + Tesseract exist
(e.g. inside the Docker image); skipped otherwise."""
from __future__ import annotations

import shutil
import subprocess

import pytest
from PIL import Image

from app.extraction import extract
from tests.pdfgen import invoice_pdf

pytestmark = pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")),
                                reason="Poppler/Tesseract not installed")


def _scan(tmp_path, angle=0.6):
    src = tmp_path / "native.pdf"
    src.write_bytes(invoice_pdf())
    subprocess.run(["pdftoppm", "-r", "200", "-png", "-singlefile", str(src), str(tmp_path / "img")], check=True)
    img = Image.open(tmp_path / "img.png").convert("L").rotate(angle, expand=False, fillcolor=255)
    out = tmp_path / "scan.pdf"
    img.save(out, "PDF", resolution=200)
    return out


def test_image_only_pdf_goes_through_ocr(tmp_path):
    r = extract(_scan(tmp_path))
    inv = r.invoice
    assert r.pages[0].method == "ocr" and r.pages[0].native_chars == 0
    assert inv.invoice_number.value == "HCI-0042"
    assert inv.po_reference.value == "PO-7781"
    assert (inv.net.value, inv.gross.value) == (348000, 348000)
    assert [(l.sku.value, l.quantity.value, l.amount.value) for l in inv.lines] == [
        ("CAL-STD", "12", 300000), ("CAL-TRV", "1", 48000)]
    assert inv.gross.evidence.method == "ocr" and inv.gross.evidence.conf > 0
