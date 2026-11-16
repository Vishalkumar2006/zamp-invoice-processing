"""Single background worker with a persisted queue (the runs table)."""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from pathlib import Path

from . import config
from .db import add_event, db, now_iso, transaction
from .extraction import ExtractionError, extract
from .ledger import finalize

log = logging.getLogger("worker")


def recover_interrupted() -> list[str]:
    """At startup, runs left RUNNING by a crash are re-queued (or failed past the attempt limit)."""
    changed = []
    with db() as conn, transaction(conn):
        for r in conn.execute("SELECT id, attempts, decision FROM runs WHERE status='RUNNING'").fetchall():
            if r["decision"] is not None:
                conn.execute("UPDATE runs SET status='COMPLETED', finished_at=COALESCE(finished_at, ?) WHERE id=?",
                             (now_iso(), r["id"]))
                add_event(conn, r["id"], "recover", "info", "Decision was already committed; marked completed")
            elif r["attempts"] >= config.MAX_RUN_ATTEMPTS:
                conn.execute("UPDATE runs SET status='FAILED', error=?, finished_at=? WHERE id=?",
                             ("Interrupted and attempt limit reached", now_iso(), r["id"]))
                add_event(conn, r["id"], "recover", "failed", "Interrupted; attempt limit reached")
            else:
                conn.execute("UPDATE runs SET status='QUEUED' WHERE id=?", (r["id"],))
                add_event(conn, r["id"], "recover", "info", "Interrupted run re-queued after restart")
            changed.append(r["id"])
    return changed


def claim_next() -> str | None:
    with db() as conn, transaction(conn):
        row = conn.execute("SELECT id FROM runs WHERE status='QUEUED' ORDER BY created_at, rowid LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE runs SET status='RUNNING', attempts=attempts+1, started_at=? WHERE id=? AND status='QUEUED'",
                     (now_iso(), row["id"]))
        add_event(conn, row["id"], "claim", "completed", "Worker picked up the run")
        return row["id"]


def process(run_id: str) -> None:
    with db() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        try:
            if not run["extraction_json"]:
                add_event(conn, run_id, "extract", "started", "Reading pages (native text or OCR)")
                result = extract(Path(run["pdf_path"]))
                inv = result.invoice
                with transaction(conn):
                    conn.execute("UPDATE runs SET extraction_json=? WHERE id=?", (result.model_dump_json(), run_id))
                    add_event(conn, run_id, "extract", "completed",
                              ", ".join(f"page {p.page}: {p.method}" + (f" (mean conf {p.mean_conf})" if p.mean_conf else "")
                                        for p in result.pages),
                              {"pages": [p.model_dump() for p in result.pages], "duration_ms": result.duration_ms,
                               "model_calls": result.model_calls})
                    add_event(conn, run_id, "parse", "completed",
                              f"Invoice {inv.invoice_number.value or '?'} from {inv.supplier_name.value or '?'}, "
                              f"PO {inv.po_reference.value or 'none'}, {len(inv.lines)} line(s)"
                              + (f"; issues: {'; '.join(inv.issues)}" if inv.issues else ""),
                              {"issues": inv.issues})
            add_event(conn, run_id, "match_and_validate", "started", "Matching vendor/PO and applying rules")
            out = finalize(conn, run_id)
            with transaction(conn):
                conn.execute("UPDATE runs SET status='COMPLETED', finished_at=? WHERE id=?", (now_iso(), run_id))
                add_event(conn, run_id, "complete", "completed", "Run completed", out)
        except Exception as exc:  # technical failure -> FAILED with zero allocation
            kind = "extraction" if isinstance(exc, ExtractionError) else "internal"
            log.error("run %s failed: %s", run_id, traceback.format_exc())
            with transaction(conn):
                conn.execute("UPDATE runs SET status='FAILED', error=?, finished_at=? WHERE id=? AND decision IS NULL",
                             (f"{kind}: {exc}", now_iso(), run_id))
                add_event(conn, run_id, "failed", "failed", f"{kind} error: {exc}")


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="run-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = claim_next()
            except Exception:
                log.exception("claim failed")
                run_id = None
            if run_id:
                process(run_id)
                continue
            self._wake.wait(config.WORKER_POLL_S)
            self._wake.clear()
