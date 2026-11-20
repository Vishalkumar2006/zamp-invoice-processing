"""Loading procurement/ledger state and the atomic finalization step."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from . import config
from .db import add_event, now_iso, transaction
from .models import Decision, ExtractionResult
from .rules import Snapshot, evaluate, po_balance


def load_snapshot(conn: sqlite3.Connection, workspace_id: str) -> Snapshot:
    vendors = [dict(r) for r in conn.execute("SELECT * FROM vendors WHERE workspace_id=?", (workspace_id,))]
    pos = {r["po_id"]: dict(r) for r in conn.execute("SELECT * FROM purchase_orders WHERE workspace_id=?",
                                                       (workspace_id,))}
    lines: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM po_lines WHERE workspace_id=? ORDER BY po_id, line_id", (workspace_id,)):
        lines.setdefault(r["po_id"], []).append(dict(r))
    allocs = []
    for a in conn.execute("SELECT a.*, r.created_at AS run_created_at, r.invoice_date AS invoice_date,"
                          " json_extract(r.extraction_json, '$.invoice.service_period.value') AS service_period,"
                          " json_extract(r.extraction_json, '$.invoice.service_period.raw') AS service_period_raw,"
                          " json_extract(r.extraction_json, '$.invoice.service_period.evidence.conf') AS service_period_conf"
                          " FROM allocations a JOIN runs r ON r.id=a.run_id"
                          " WHERE a.workspace_id=? ORDER BY a.id", (workspace_id,)):
        d = dict(a)
        d["lines"] = [dict(l) for l in conn.execute("SELECT * FROM allocation_lines WHERE allocation_id=?", (a["id"],))]
        allocs.append(d)
    return Snapshot(vendors=vendors, pos=pos, po_lines=lines, allocations=allocs)


def ledger_report(conn: sqlite3.Connection, workspace_id: str) -> dict[str, Any]:
    snap = load_snapshot(conn, workspace_id)
    out = []
    for po_id in sorted(snap.pos):
        bal = po_balance(snap, po_id)
        bal["vendor_id"] = snap.pos[po_id]["vendor_id"]
        bal["status"] = snap.pos[po_id]["status"]
        bal["allocations"] = [
            {"allocation_id": a["id"], "run_id": a["run_id"], "invoice_number": a["invoice_number_norm"],
             "net_cents": a["net_cents"], "tax_cents": a["tax_cents"], "gross_cents": a["gross_cents"],
             "created_at": a["created_at"], "lines": [{k: l[k] for k in ("line_id", "sku", "quantity", "unit_price",
                                                                          "net_cents")} for l in a["lines"]]}
            for a in snap.allocations if a["po_id"] == po_id]
        out.append(bal)
    return {"workspace_id": workspace_id, "allocation_count": len(snap.allocations),
            "total_allocated_net_cents": sum(a["net_cents"] for a in snap.allocations), "purchase_orders": out}


def finalize(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Evaluate the stored extraction against current ledger state and commit the decision.

    Idempotent: if the run already has a decision, nothing is written and the stored decision is
    returned with already_finalized=True. The decision, allocation (READY only), run row and the
    final event are written in one BEGIN IMMEDIATE transaction; UNIQUE(run_id) and
    UNIQUE(workspace, vendor, invoice number) on allocations back this up at the database level.
    """
    last_exc: Exception | None = None
    for attempt in range(config.FINALIZE_LOCK_RETRIES):
        try:
            with transaction(conn):
                run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if run is None:
                    raise KeyError(run_id)
                if run["decision"] is not None:
                    return {"already_finalized": True, "decision": run["decision"], "reason_code": run["reason_code"]}
                if not run["extraction_json"]:
                    raise ValueError("run has no stored extraction to finalize")
                extraction = ExtractionResult.model_validate_json(run["extraction_json"])
                snap = load_snapshot(conn, run["workspace_id"])  # fresh read inside the write lock
                decision: Decision = evaluate(extraction.invoice, snap, min_conf=config.MIN_FIELD_CONFIDENCE,
                                              policy_version=config.POLICY_VERSION)
                ts = now_iso()
                if decision.decision == "READY":
                    a = decision.allocation
                    cur = conn.execute(
                        "INSERT INTO allocations (workspace_id, run_id, vendor_id, invoice_number_norm, po_id, currency,"
                        " net_cents, tax_cents, gross_cents, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (run["workspace_id"], run_id, decision.vendor_id, extraction.invoice.invoice_number.value,
                         a["po_id"], a["currency"], a["net_cents"], a["tax_cents"], a["gross_cents"], ts))
                    a["allocation_id"] = cur.lastrowid
                    conn.executemany(
                        "INSERT INTO allocation_lines (allocation_id, po_id, line_id, sku, quantity, unit_price, net_cents)"
                        " VALUES (?,?,?,?,?,?,?)",
                        [(cur.lastrowid, a["po_id"], l["line_id"], l["sku"], l["quantity"], l["unit_price"],
                          l["net_cents"]) for l in a["lines"]])
                inv = extraction.invoice
                conn.execute(
                    "UPDATE runs SET decision=?, reason_code=?, reason=?, decision_json=?, vendor_id=?, po_id=?,"
                    " invoice_number=?, invoice_date=?, gross_cents=?, currency=?, policy_version=?, decided_at=?"
                    " WHERE id=? AND decision IS NULL",
                    (decision.decision, decision.reason_code, decision.reason, decision.model_dump_json(),
                     decision.vendor_id, decision.po_id, inv.invoice_number.value, inv.invoice_date.value,
                     inv.gross.value, inv.currency.value, decision.policy_version, ts, run_id))
                add_event(conn, run_id, "decide", "completed", f"{decision.decision}: {decision.reason_code}",
                          {"decision": decision.decision, "reason_code": decision.reason_code,
                           "allocation_id": decision.allocation.get("allocation_id") if decision.allocation else None})
                return {"already_finalized": False, "decision": decision.decision,
                        "reason_code": decision.reason_code}
        except sqlite3.OperationalError as exc:  # transient lock contention: retry with fresh data
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            last_exc = exc
            time.sleep(0.05 * (2 ** attempt))
        except sqlite3.IntegrityError as exc:
            # The same business invoice (or run) was allocated by a concurrent transaction. The
            # write was rolled back; re-evaluate against fresh state, which will now see it.
            if "UNIQUE" not in str(exc):
                raise
            last_exc = exc
    raise RuntimeError(f"finalize could not acquire the database lock: {last_exc}")
