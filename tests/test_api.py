"""End-to-end HTTP flow on synthetic data: import -> upload -> background run -> ledger."""
from __future__ import annotations

import sqlite3
import threading

from fastapi.testclient import TestClient

from app import config
from app.db import db
from app.ledger import finalize
from tests.conftest import wait_for
from tests.pdfgen import LINES_CSV, POS_CSV, VENDORS_CSV, invoice_pdf


def _import(client, lines=LINES_CSV):
    return client.post("/procurement/import", files={
        "vendors": ("vendors.csv", VENDORS_CSV), "purchase_orders": ("purchase_orders.csv", POS_CSV),
        "po_lines": ("po_lines.csv", lines)})


def _upload(client, pdf: bytes, name="inv.pdf"):
    return client.post("/runs", files={"file": (name, pdf, "application/pdf")})


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["db"] == "ok"


def test_import_rejects_header_line_mismatch_and_bad_refs(client):
    bad = LINES_CSV.replace(b"PO-7781,2,CAL-TRV,Technician travel,2,480.00,960.00",
                            b"PO-7781,2,CAL-TRV,Technician travel,2,480.00,900.00")
    r = _import(client, bad)
    assert r.status_code == 422
    errs = " ".join(r.json()["errors"])
    assert "authorized_quantity x unit_price" in errs and "sum of line authorized_net" in errs
    assert client.get("/procurement").status_code == 404  # nothing committed


def test_upload_requires_import_and_pdf(client):
    assert _upload(client, invoice_pdf()).status_code == 409
    _import(client)
    assert _upload(client, b"hello", "x.pdf").status_code == 415


def test_ready_flow_allocation_retry_duplicate_and_restart(client, data_dir):
    assert _import(client).status_code == 201
    pdf = invoice_pdf()
    up = _upload(client, pdf)
    assert up.status_code == 202
    run_id = up.json()["run_id"]
    run = wait_for(client, run_id)
    assert run["status"] == "COMPLETED" and run["decision"] == "READY", run.get("reason")
    assert run["po_id"] == "PO-7781" and run["vendor_id"] == "V-900" and run["gross_cents"] == 348000
    stages = [e["stage"] for e in run["events"]]
    assert stages[:3] == ["upload", "queue", "claim"] and "extract" in stages and stages[-1] == "complete"
    assert run["model_calls"] == 0
    assert run["po_balance_now"]["remaining_net_cents"] == 696000 - 348000

    ledger = client.get("/ledger").json()
    po = next(p for p in ledger["purchase_orders"] if p["po_id"] == "PO-7781")
    assert ledger["allocation_count"] == 1 and po["remaining_net_cents"] == 348000
    assert {l["sku"]: l["remaining_quantity"] for l in po["lines"]} == {"CAL-STD": "12", "CAL-TRV": "1"}

    # Retrying finalization never creates a second allocation.
    for _ in range(3):
        r = client.post(f"/runs/{run_id}/finalize").json()
        assert r["already_finalized"] is True and r["allocations_for_run"] == 1
    assert client.get("/ledger").json()["allocation_count"] == 1

    # Same PDF again: business duplicate -> BLOCKED, zero allocation.
    dup = wait_for(client, _upload(client, pdf).json()["run_id"])
    assert dup["decision"] == "BLOCKED" and dup["reason_code"] == "DUPLICATE_INVOICE"
    assert client.get("/ledger").json()["allocation_count"] == 1

    # UI pages render.
    assert client.get(f"/ui/runs/{run_id}").status_code == 200
    assert "HCI-0042" in client.get("/ui/history").text
    assert client.get(f"/runs/{run_id}/pdf").content == pdf

    # Restart: a new app lifespan over the same data directory keeps runs and ledger.
    client.__exit__(None, None, None)
    from app.main import app
    with TestClient(app) as c2:
        again = c2.get(f"/runs/{run_id}").json()
        assert again["decision"] == "READY" and again["allocation_id"] == run["allocation_id"]
        assert c2.get("/ledger").json()["allocation_count"] == 1
        assert len(c2.get("/runs").json()["runs"]) == 2


def test_concurrent_finalize_writes_one_allocation(client):
    _import(client)
    run_id = _upload(client, invoice_pdf()).json()["run_id"]
    wait_for(client, run_id)
    # Undo the decision so we can race two finalizers on the same stored extraction.
    with db() as conn:
        conn.execute("DELETE FROM allocation_lines")
        conn.execute("DELETE FROM allocations")
        conn.execute("UPDATE runs SET decision=NULL, decision_json=NULL WHERE id=?", (run_id,))
    results = []

    def go():
        with db() as conn:
            results.append(finalize(conn, run_id))

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r["already_finalized"] for r in results) == [False, True, True, True]
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 1


def test_allocation_uniqueness_enforced_by_database(client):
    _import(client)
    run_id = _upload(client, invoice_pdf()).json()["run_id"]
    wait_for(client, run_id)
    with db() as conn:
        a = conn.execute("SELECT * FROM allocations").fetchone()
        try:
            conn.execute("INSERT INTO allocations (workspace_id, run_id, vendor_id, invoice_number_norm, po_id, currency,"
                         " net_cents, tax_cents, gross_cents, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (a["workspace_id"], a["run_id"], a["vendor_id"], "OTHER", a["po_id"], "USD", 1, 0, 1, "x"))
            raise AssertionError("duplicate run_id allocation was accepted")
        except sqlite3.IntegrityError:
            pass


def test_hold_writes_no_allocation(client):
    _import(client)
    pdf = invoice_pdf(number="HCI-0043", lines=(("CAL-STD", "Standard calibration", "30", "$250.00", "$7,500.00"),),
                      subtotal="$7,500.00", total="$7,500.00")
    run = wait_for(client, _upload(client, pdf).json()["run_id"])
    assert run["decision"] == "HOLD" and run["reason_code"] == "QUANTITY_EXCEEDS_PO"
    assert run["allocation_id"] is None
    assert client.get("/ledger").json()["allocation_count"] == 0


def test_corrupt_pdf_fails_safely_with_retry_path(client):
    _import(client)
    run = wait_for(client, _upload(client, b"%PDF-1.4\nnot really a pdf").json()["run_id"])
    assert run["status"] == "FAILED" and run["decision"] is None and "extraction" in run["error"]
    assert client.get("/ledger").json()["allocation_count"] == 0
    assert client.post(f"/runs/{run['run_id']}/retry").status_code == 202
