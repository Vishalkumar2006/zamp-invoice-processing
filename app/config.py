"""Runtime configuration read from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "app.db"
UPLOAD_DIR = DATA_DIR / "uploads"

POLICY_VERSION = "ps1-policy-2026.11-m1"

# Upload / extraction bounds.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))
OCR_DPI = int(os.environ.get("OCR_DPI", "300"))
OCR_PAGE_TIMEOUT_S = int(os.environ.get("OCR_PAGE_TIMEOUT_S", "60"))
# A page with fewer printable native characters than this is treated as a scan.
NATIVE_TEXT_MIN_CHARS = int(os.environ.get("NATIVE_TEXT_MIN_CHARS", "25"))
# Minimum OCR word confidence (0-100) for a value that drives an allocation.
MIN_FIELD_CONFIDENCE = float(os.environ.get("MIN_FIELD_CONFIDENCE", "80"))

# Worker.
MAX_RUN_ATTEMPTS = int(os.environ.get("MAX_RUN_ATTEMPTS", "2"))
WORKER_POLL_S = float(os.environ.get("WORKER_POLL_S", "0.25"))
FINALIZE_LOCK_RETRIES = 5

# Optional HTTP basic auth for every route except /health.
APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
