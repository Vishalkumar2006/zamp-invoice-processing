"""Per-line unit-price check: every mapped line is evaluated; offsetting differences still hold."""
from __future__ import annotations

import pytest

from app.models import Field_
from app.procurement import norm_name
from app.rules import Snapshot, evaluate
from tests.conftest import wait_for
from tests.test_rules import f, invoice, line, native

POLICY = "t"


def two_line_snapshot(price_a="150", price_b="50"):
    return Snapshot(
        vendors=[{"vendor_id": "V1", "legal_name": "ACME Testing Co", "legal_name_norm": norm_name("ACME Testing Co"),
                  "status": "APPROVED", "source_file": "vendors.csv", "source_row": 2}],
        pos={"PO-1": {"po_id": "PO-1", "vendor_id": "V1", "status": "OPEN", "currency": "USD", "issued_on": "2026-11-01",
                      "authorized_net_cents": 250000, "source_file": "purchase_orders.csv", "source_row": 2}},
        po_lines={"PO-1": [
            {"po_id": "PO-1", "line_id": "10", "sku": "AAA", "unit_price": price_a, "authorized_quantity": "10",
             "authorized_net_cents": 150000, "source_file": "po_lines.csv", "source_row": 2},
            {"po_id": "PO-1", "line_id": "20", "sku": "BBB", "unit_price": price_b, "authorized_quantity": "20",
             "authorized_net_cents": 100000, "source_file": "po_lines.csv", "source_row": 3}]},
    )


def offsetting_invoice():
    # +5% on AAA (+75.00 at 10 units), -7.5% on BBB (-75.00 at 20 units): subtotal unchanged at 2,500.00.
    return invoice(net=f(250000), tax=f(25000), gross=f(275000),
                   lines=[line("AAA", "10", "157.5", 157500, idx=0), line("BBB", "20", "46.25", 92500, idx=1)])


def test_offsetting_line_prices_hold_with_every_failed_line():
    d = evaluate(offsetting_invoice(), two_line_snapshot(), min_conf=80, policy_version=POLICY)
    assert d.decision == "HOLD" and d.reason_code == "PRICE_VARIANCE" and d.allocation is None
    pc = d.price_check
    assert pc["failed_lines"] == [1, 2]
    a, b = pc["lines"]
    assert (a["sku"], a["invoice_unit_price"], a["po_unit_price"], a["variance_display"], a["amount_difference_cents"],
            a["po_source"]) == ("AAA", "157.5", "150", "+5.00%", 7500, "po_lines.csv row 2")
    assert (b["sku"], b["invoice_unit_price"], b["po_unit_price"], b["variance_display"], b["amount_difference_cents"],
            b["po_source"]) == ("BBB", "46.25", "50", "-7.50%", -7500, "po_lines.csv row 3")
    assert a["invoice_evidence"]["page"] == 1 and a["invoice_evidence"]["text"] == "157.5"
    assert (pc["subtotal_cents"], pc["subtotal_at_po_prices_cents"], pc["subtotal_difference_cents"]) == (250000, 250000, 0)
    assert pc["offsetting"] is True and pc["tax_cents"] == 25000 and pc["gross_cents"] == 275000
    assert "line 1 AAA at USD 157.50 vs PO USD 150.00 (+5.00%, +USD 75.00 at 10 units)" in d.reason
    assert "line 2 BBB at USD 46.25 vs PO USD 50.00 (-7.50%, -USD 75.00 at 20 units)" in d.reason
    assert "The differences offset" in d.reason and "Tax USD 250.00 is separate" in d.reason
    assert "re-bill at the PO prices: AAA (line 1) at USD 150.00 and BBB (line 2) at USD 50.00, or ask" in d.next_action
    assert "line 10 (AAA) and line 20 (BBB)" in d.next_action
    assert d.review_field == "line 1 unit_price"
    assert [c["status"] for c in d.checks if c["rule"] == "price"] == ["fail", "fail"]
    assert d.authorization is None  # authorization is not evaluated once prices fail


@pytest.mark.parametrize("rate,amount,ok", [
    ("101", 101000, True),       # exactly +1%  (inclusive)
    ("99", 99000, True),         # exactly -1%  (inclusive)
    ("101.01", 101010, False),   # just above +1%
    ("98.99", 98990, False),     # just below -1%
])
def test_inclusive_one_percent_boundary_in_both_directions(rate, amount, ok):
    snap = two_line_snapshot(price_a="100", price_b="50")
    inv = invoice(net=f(amount + 50000), tax=f(0), gross=f(amount + 50000),
                  lines=[line("AAA", "10", rate, amount, idx=0), line("BBB", "10", "50", 50000, idx=1)])
    d = evaluate(inv, snap, min_conf=80, policy_version=POLICY)
    assert (d.decision == "READY") is ok, (rate, d.reason_code)
    row = d.price_check["lines"][0]
    assert row["within_tolerance"] is ok
    if not ok:
        assert d.reason_code == "PRICE_VARIANCE" and d.price_check["failed_lines"] == [1]
        assert row["variance_display"] == ("+1.01%" if rate == "101.01" else "-1.01%")


def test_single_failed_line_is_named_and_others_pass():
    inv = invoice(net=f(257500), tax=f(0), gross=f(257500),
                  lines=[line("AAA", "10", "157.5", 157500, idx=0), line("BBB", "20", "50", 100000, idx=1)])
    d = evaluate(inv, two_line_snapshot(), min_conf=80, policy_version=POLICY)
    assert d.price_check["failed_lines"] == [1] and d.price_check["offsetting"] is False
    assert "1 line is outside" in d.reason and "BBB" not in d.next_action


# --------------------------------------------------------------------------- repeated SKU, candidates vs explicit PO

def _flt_snapshot():
    return Snapshot(
        vendors=[{"vendor_id": "V1", "legal_name": "ACME Testing Co", "legal_name_norm": norm_name("ACME Testing Co"),
                  "status": "APPROVED", "source_file": "vendors.csv", "source_row": 2}],
        pos={"PO-A": {"po_id": "PO-A", "vendor_id": "V1", "status": "OPEN", "currency": "USD", "issued_on": "2026-10-01",
                      "authorized_net_cents": 300000, "source_file": "purchase_orders.csv", "source_row": 2}},
        po_lines={"PO-A": [{"po_id": "PO-A", "line_id": "10", "sku": "FLT", "unit_price": "125",
                            "authorized_quantity": "24", "authorized_net_cents": 300000, "source_file": "po_lines.csv",
                            "source_row": 2}]},
    )


def _repeated_flt(po_ref):
    return invoice(po_reference=po_ref, net=f(300000), gross=f(300000),
                   lines=[line("FLT", "12", "125", 150000, idx=0), line("FLT", "12", "125", 150000, idx=1)])


def test_repeated_sku_candidate_is_ineligible_like_an_explicit_po():
    explicit = evaluate(_repeated_flt(native("PO-A")), _flt_snapshot(), min_conf=80, policy_version=POLICY)
    assert explicit.decision == "HOLD" and explicit.reason_code == "LINE_NOT_ON_PO"

    missing = evaluate(_repeated_flt(Field_()), _flt_snapshot(), min_conf=80, policy_version=POLICY)
    assert missing.decision == "HOLD" and missing.reason_code == "PO_MISSING_NO_MATCH"
    assert missing.candidate_po_ids == [] and missing.allocation is None and missing.po_id is None
    cand = missing.po_candidates["candidates"][0]
    assert cand["viable"] is False
    assert cand["failures"] == ["invoice lines 1 and 2 both map to FLT PO line 10 (each invoice line needs its own PO line)"]
    assert cand["lines"][0]["sku_ok"] is True and cand["lines"][1]["repeated_mapping_of"] == 1
    assert cand["lines"][1]["sku_ok"] is False


# --------------------------------------------------------------------------- HTTP flow, real extraction

VENDORS = b"vendor_id,legal_name,status\nV-900,Harborline Calibration Inc,APPROVED\n"
POS = (b"po_id,vendor_id,issued_on,status,currency,description,authorized_net\n"
       b"PO-8801,V-900,2026-02-01,OPEN,USD,Instruments,3480.00\n")
LINES = (b"po_id,line_id,sku,description,authorized_quantity,unit_price,authorized_net\n"
         b"PO-8801,1,CAL-STD,Standard calibration,12,250.00,3000.00\n"
         b"PO-8801,2,CAL-TRV,Technician travel,1,480.00,480.00\n")


def test_offsetting_prices_over_http_with_separate_tax(client):
    from tests.pdfgen import invoice_pdf
    r = client.post("/procurement/import", files={"vendors": ("vendors.csv", VENDORS),
                                                  "purchase_orders": ("purchase_orders.csv", POS),
                                                  "po_lines": ("po_lines.csv", LINES)})
    assert r.status_code == 201
    before = client.get("/ledger").json()
    # CAL-STD +2.00% (+60.00 at 12), CAL-TRV -12.50% (-60.00 at 1): subtotal 3,480.00 = PO total; 10% tax.
    pdf = invoice_pdf(po="PO-8801", lines=(("CAL-STD", "Standard calibration", "12", "$255.00", "$3,060.00"),
                                           ("CAL-TRV", "Technician travel", "1", "$420.00", "$420.00")),
                      subtotal="$3,480.00", tax="$348.00", total="$3,828.00")
    run_id = client.post("/runs", files={"file": ("inv.pdf", pdf, "application/pdf")}).json()["run_id"]
    run = wait_for(client, run_id)
    assert run["decision"] == "HOLD" and run["reason_code"] == "PRICE_VARIANCE" and run["allocation_id"] is None
    pc = run["decision_detail"]["price_check"]
    assert [(l["sku"], l["variance_display"], l["amount_difference_cents"]) for l in pc["lines"]] == [
        ("CAL-STD", "+2.00%", 6000), ("CAL-TRV", "-12.50%", -6000)]
    assert pc["failed_lines"] == [1, 2] and pc["offsetting"] and pc["subtotal_difference_cents"] == 0
    assert (pc["tax_cents"], pc["gross_cents"]) == (34800, 382800)
    assert pc["lines"][0]["invoice_evidence"]["method"] == "native" and pc["lines"][0]["po_source"] == "po_lines.csv row 2"
    assert client.get("/ledger").json() == before
    assert client.post(f"/runs/{run_id}/finalize").json()["allocations_for_run"] == 0
    assert client.get("/ledger").json() == before
    assert client.get(f"/ui/runs/{run_id}").status_code == 200
