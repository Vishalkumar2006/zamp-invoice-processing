"""Per-page token reading: native PDF text when usable, otherwise Poppler render + Tesseract OCR.

Every token keeps its page, method, position (PDF points, top-left origin) and confidence.
"""
from __future__ import annotations

import csv
import io
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

import pdfplumber

from .. import config
from ..models import PageInfo, Token


class ExtractionError(Exception):
    """A technical failure reading the document (maps to run status FAILED)."""


def tool_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name, args in (("tesseract", ["tesseract", "--version"]), ("pdftoppm", ["pdftoppm", "-v"])):
        if shutil.which(name) is None:
            out[name] = None
            continue
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=10)
            text = (p.stdout or p.stderr).strip().splitlines()
            out[name] = text[0] if text else "unknown"
        except Exception:  # pragma: no cover - diagnostic only
            out[name] = "error"
    return out


def _native_tokens(page, page_no: int) -> list[Token]:
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False, extra_attrs=["size"])
    return [
        Token(text=w["text"], page=page_no, x0=w["x0"], y0=w["top"], x1=w["x1"], y1=w["bottom"],
              conf=100.0, method="native")
        for w in words if w["text"].strip()
    ]


def _ocr_page(pdf_path: Path, page_no: int, page_w: float, page_h: float) -> tuple[list[Token], float]:
    if shutil.which("pdftoppm") is None or shutil.which("tesseract") is None:
        raise ExtractionError("OCR tools (pdftoppm/tesseract) are not installed")
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        try:
            subprocess.run(
                ["pdftoppm", "-r", str(config.OCR_DPI), "-f", str(page_no), "-l", str(page_no),
                 "-gray", "-png", "-singlefile", str(pdf_path), str(prefix)],
                check=True, capture_output=True, timeout=config.OCR_PAGE_TIMEOUT_S,
            )
            png = prefix.with_suffix(".png")
            proc = subprocess.run(
                ["tesseract", str(png), "stdout", "--psm", "3", "-l", "eng", "tsv"],
                check=True, capture_output=True, timeout=config.OCR_PAGE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExtractionError(f"OCR timed out on page {page_no}") from exc
        except subprocess.CalledProcessError as exc:
            raise ExtractionError(f"OCR failed on page {page_no}: {exc.stderr[-300:]!r}") from exc

    scale = 72.0 / config.OCR_DPI
    tokens: list[Token] = []
    reader = csv.DictReader(io.StringIO(proc.stdout.decode("utf-8", "replace")), delimiter="\t",
                            quoting=csv.QUOTE_NONE)
    for row in reader:
        text = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            conf = -1
        if not text or conf < 0:
            continue
        left, top = int(row["left"]), int(row["top"])
        width, height = int(row["width"]), int(row["height"])
        tokens.append(Token(
            text=text, page=page_no,
            x0=left * scale, y0=top * scale, x1=(left + width) * scale, y1=(top + height) * scale,
            conf=conf, method="ocr",
            group=f"{row['block_num']}.{row['par_num']}.{row['line_num']}",
        ))
    mean_conf = statistics.fmean(t.conf for t in tokens) if tokens else 0.0
    return tokens, mean_conf


def read_pages(pdf_path: Path) -> tuple[list[Token], list[PageInfo]]:
    """Read every page; returns all tokens and per-page method info."""
    tokens: list[Token] = []
    infos: list[PageInfo] = []
    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as exc:
        raise ExtractionError(f"Not a readable PDF: {exc}") from exc
    with pdf:
        if len(pdf.pages) == 0:
            raise ExtractionError("PDF has no pages")
        if len(pdf.pages) > config.MAX_PAGES:
            raise ExtractionError(f"PDF has {len(pdf.pages)} pages; limit is {config.MAX_PAGES}")
        for idx, page in enumerate(pdf.pages, start=1):
            t0 = time.perf_counter()
            native = _native_tokens(page, idx)
            native_chars = sum(len(t.text) for t in native)
            if native_chars >= config.NATIVE_TEXT_MIN_CHARS:
                page_tokens, method, mean_conf = native, "native", None
            else:
                page_tokens, mean_conf = _ocr_page(pdf_path, idx, float(page.width), float(page.height))
                method = "ocr"
            tokens.extend(page_tokens)
            infos.append(PageInfo(
                page=idx, method=method, native_chars=native_chars, token_count=len(page_tokens),
                mean_conf=round(mean_conf, 1) if mean_conf is not None else None,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            ))
    return tokens, infos
