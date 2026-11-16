"""OCR path on a synthetic scan. Runs where Poppler + Tesseract exist (e.g. inside the Docker
image); skipped otherwise.

The scan is drawn with Pillow's bundled font and rotated, then wrapped as an image-only PDF, so
the test input is the same on every host. (Rendering a non-embedded Helvetica PDF with pdftoppm
made the input depend on host fonts: blank pages with no fonts installed, weak glyphs with others.)
"""
from __future__ import annotations

import shutil

import pytest

from app.extraction import extract
from tests.pdfgen import invoice_items, scan_pdf

pytestmark = pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")),
                                reason="Poppler/Tesseract not installed")


def _scan(tmp_path, angle=0.6):
    out = tmp_path / "scan.pdf"
    out.write_bytes(scan_pdf(invoice_items(), angle=angle))
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

