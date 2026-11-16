"""Business-duplicate behaviour over the real HTTP flow, on synthetic data.

Business key = approved vendor ID + conservatively normalised printed invoice number, looked up
against previously accepted (allocated) invoices only.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading

import pytest

from app.db import db, now_iso
from app.ledger import finalize
from tests.conftest import wait_for
from tests.pdfgen import LINES_CSV, POS_CSV, VENDORS_CSV, invoice_items, invoice_pdf, scan_pdf

NUMBER = "HCR-04517"  # OCR-stable in the synthetic scan (no I/l/1 look-alikes); keeps a leading zero
ONE_LINE = (("CAL-STD", "Standard calibration", "4", "$250.00", "$1,000.00"),)
ONE = dict(lines=ONE_LINE, subtotal="$1,000.00", total="$1,000.00")


def _import(client):
    r = client.post("/procurement/import", files={
        "vendors": ("vendors.csv", VENDORS_CSV), "purchase_orders": ("purchase_orders.csv", POS_CSV),
        "po_lines": ("po_lines.csv", LINES_CSV)})
    assert r.status_code == 201


def _submit(client, pdf: bytes, name="inv.pdf"):
    r = client.post("/runs", files={"file": (name, pdf, "application/pdf")})
    assert r.status_code == 202
    return wait_for(client, r.json()["run_id"])


def _ledger(client):
    d = client.get("/ledger").json()
    po = next(p for p in d["purchase_orders"] if p["po_id"] == "PO-7781")
    return d["allocation_count"], po["remaining_net_cents"]


def _conflicts(run):
    return [r["field"] for r in run["decision_detail"]["duplicate"]["comparison"] if r["status"] == "conflict"]


@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="needs Poppler/Tesseract")
def test_native_original_then_rasterized_copy_is_blocked(client):
    _import(client)
    original = invoice_pdf(number=NUMBER, **ONE)
    copy = scan_pdf(invoice_items(number=NUMBER, footer="Copy of invoice. Page 1 of 1", **ONE))
    assert hashlib.sha256(original).hexdigest() != hashlib.sha256(copy).hexdigest()

    first = _submit(client, original, "original.pdf")
    assert first["decision"] == "READY" and first["extraction"]["pages"][0]["method"] == "native"
    second = _submit(client, copy, "portal-copy.pdf")
    assert second["extraction"]["pages"][0]["method"] == "ocr"
    assert second["decision"] == "BLOCKED" and second["reason_code"] == "DUPLICATE_INVOICE", second["reason"]
    assert second["allocation_id"] is None
    dup = second["decision_detail"]["duplicate"]
    assert dup["business_key"]["vendor_id"] == "V-900" and dup["business_key"]["invoice_number"] == NUMBER
    assert dup["accepted"]["run_id"] == first["run_id"] and dup["outcome"] == "corroborated"
    assert _conflicts(second) == []
    assert _ledger(client) == (1, 696000 - 100000)

    # Retrying finalization of the blocked run writes nothing.
    r = client.post(f"/runs/{second['run_id']}/finalize").json()
    assert r["already_finalized"] is True and r["decision"] == "BLOCKED" and r["allocations_for_run"] == 0
    assert _ledger(client) == (1, 596000)


def test_legitimate_same_amount_recurrence_is_accepted(client):
    _import(client)
    a = _submit(client, invoice_pdf(number="HCR-04517", date="March 4, 2026", **ONE))
    b = _submit(client, invoice_pdf(number="HCR-04518", date="April 4, 2026", **ONE))
    assert (a["decision"], b["decision"]) == ("READY", "READY")
    assert b["decision_detail"]["duplicate"]["outcome"] == "no_match"
    assert _ledger(client) == (2, 696000 - 200000)


def test_same_key_with_conflicting_date_and_amount_holds_with_fields(client):
    _import(client)
    first = _submit(client, invoice_pdf(number=NUMBER, **ONE))
    redated = _submit(client, invoice_pdf(number=NUMBER, date="March 9, 2026", **ONE))
    assert redated["decision"] == "HOLD" and redated["reason_code"] == "DUPLICATE_NUMBER_CONFLICT"
    assert _conflicts(redated) == ["invoice_date"] and redated["allocation_id"] is None
    rebilled = _submit(client, invoice_pdf(number=NUMBER, lines=(("CAL-STD", "Standard calibration", "5", "$250.00",
                                                                  "$1,250.00"),), subtotal="$1,250.00",
                                           total="$1,250.00"))
    assert rebilled["reason_code"] == "DUPLICATE_NUMBER_CONFLICT"
    assert _conflicts(rebilled) == ["net", "gross", "CAL-STD quantity", "CAL-STD amount"]
    assert rebilled["decision_detail"]["duplicate"]["accepted"]["run_id"] == first["run_id"]
    assert _ledger(client) == (1, 596000)


def test_corrected_resubmission_after_hold_is_evaluated(client):
    _import(client)
    over = _submit(client, invoice_pdf(number=NUMBER, lines=(("CAL-STD", "Standard calibration", "30", "$250.00",
                                                              "$7,500.00"),), subtotal="$7,500.00", total="$7,500.00"))
    assert over["decision"] == "HOLD" and over["reason_code"] == "AMOUNT_EXCEEDS_PO"
    corrected = _submit(client, invoice_pdf(number=NUMBER, **ONE))
    assert corrected["decision"] == "READY" and corrected["decision_detail"]["duplicate"]["outcome"] == "no_match"
    assert _ledger(client) == (1, 596000)


def test_race_between_two_runs_for_the_same_business_invoice(client):
    _import(client)
    a = _submit(client, invoice_pdf(number=NUMBER, **ONE), "a.pdf")
    b = _submit(client, invoice_pdf(number=NUMBER, footer="Duplicate copy sent by email", **ONE), "b.pdf")
    assert a["sha256"] != b["sha256"] and (a["decision"], b["decision"]) == ("READY", "BLOCKED")
    for _trial in range(5):
        with db() as conn:  # undo both decisions, then let two finalizers race
            conn.execute("DELETE FROM allocation_lines")
            conn.execute("DELETE FROM allocations")
            conn.execute("UPDATE runs SET decision=NULL, decision_json=NULL WHERE id IN (?,?)",
                         (a["run_id"], b["run_id"]))
        barrier = threading.Barrier(2)
        out = {}

        def go(run_id):
            barrier.wait()
            with db() as conn:
                out[run_id] = finalize(conn, run_id)

        threads = [threading.Thread(target=go, args=(r["run_id"],)) for r in (a, b)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(o["decision"] for o in out.values()) == ["BLOCKED", "READY"]
        with db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 1


def test_database_rejects_second_allocation_for_same_business_key(client):
    _import(client)
    a = _submit(client, invoice_pdf(number=NUMBER, **ONE))
    b = _submit(client, invoice_pdf(number=NUMBER, footer="resent", **ONE))
    import sqlite3
    with db() as conn:
        row = conn.execute("SELECT * FROM allocations WHERE run_id=?", (a["run_id"],)).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO allocations (workspace_id, run_id, vendor_id, invoice_number_norm, po_id, currency,"
                         " net_cents, tax_cents, gross_cents, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (row["workspace_id"], b["run_id"], row["vendor_id"], NUMBER, row["po_id"], "USD", 1, 0, 1,
                          now_iso()))


def test_older_stored_decisions_stay_readable(client):
    """A run decided by an earlier release (no reason_codes/authorization/duplicate keys) still renders."""
    _import(client)
    run = _submit(client, invoice_pdf(number=NUMBER, **ONE))
    legacy = {k: v for k, v in run["decision_detail"].items() if k not in ("reason_codes", "authorization", "duplicate")}
    with db() as conn:
        conn.execute("UPDATE runs SET decision_json=? WHERE id=?", (json.dumps(legacy), run["run_id"]))
    again = client.get(f"/runs/{run['run_id']}")
    assert again.status_code == 200 and again.json()["decision_detail"] == legacy
    assert client.get(f"/ui/runs/{run['run_id']}").status_code == 200
    assert client.get("/ui/history").status_code == 200


NEAR_NATIVE = "HCR-54517"  # an OCR scan of "HCR-S4517" collapses to the same look-alike form
NEAR_SCAN = "HCR-S4517"


@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="needs Poppler/Tesseract")
def test_scanned_near_match_that_corroborates_a_copy_is_held(client):
    _import(client)
    _submit(client, invoice_pdf(number=NEAR_NATIVE, **ONE))
    scan = _submit(client, scan_pdf(invoice_items(number=NEAR_SCAN, **ONE)))
    inv = scan["extraction"]["invoice"]
    assert inv["invoice_number"]["value"] == NEAR_SCAN and inv["invoice_number"]["evidence"]["conf"] >= 80
    assert scan["decision"] == "HOLD" and scan["reason_code"] == "DUPLICATE_IDENTITY_UNCERTAIN"
    assert scan["decision_detail"]["duplicate"]["accepted"]["invoice_number"] == NEAR_NATIVE
    assert scan["allocation_id"] is None and _ledger(client) == (1, 596000)


@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="needs Poppler/Tesseract")
def test_scanned_distinct_number_with_later_date_and_new_billing_proceeds(client):
    _import(client)
    _submit(client, invoice_pdf(number=NEAR_NATIVE, date="March 4, 2026", **ONE))
    later = scan_pdf(invoice_items(number=NEAR_SCAN, date="April 4, 2026",
                                   lines=(("CAL-STD", "Standard calibration", "5", "$250.00", "$1,250.00"),),
                                   subtotal="$1,250.00", total="$1,250.00"))
    run = _submit(client, later)
    assert run["extraction"]["invoice"]["invoice_number"]["value"] == NEAR_SCAN
    assert run["decision"] == "READY", (run["reason_code"], run["reason"])
    nm = run["decision_detail"]["duplicate"]["near_matches"][0]
    assert nm["outcome"] == "distinct" and nm["accepted"]["invoice_number"] == NEAR_NATIVE
    assert _ledger(client) == (2, 696000 - 100000 - 125000)
