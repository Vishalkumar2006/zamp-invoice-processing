"""Document extraction: PDF -> positioned tokens -> structured invoice with evidence.

This package never touches procurement data or the ledger; decisions live in app.rules.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..models import ExtractionResult
from .pages import ExtractionError, read_pages, tool_versions  # noqa: F401
from .parser import parse_invoice


def extract(pdf_path: Path) -> ExtractionResult:
    t0 = time.perf_counter()
    tokens, pages = read_pages(pdf_path)
    if not tokens:
        raise ExtractionError("No text could be read from the document (native or OCR)")
    invoice, slopes = parse_invoice(tokens)
    for p in pages:
        p.skew_slope = round(slopes.get(p.page, 0.0), 5)
    return ExtractionResult(invoice=invoice, pages=pages, duration_ms=int((time.perf_counter() - t0) * 1000))
