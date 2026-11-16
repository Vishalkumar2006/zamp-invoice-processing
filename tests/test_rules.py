"""Pure decision-function tests with verified invoice fields (no OCR, no database)."""
from __future__ import annotations

import copy

import pytest

from app.models import Evidence, ExtractedInvoice, Field_, LineItem
from app.procurement import norm_name
from app.rules import Snapshot, evaluate

POLICY = "test-policy"


def f(value, raw=None, conf=95.0):
    return Field_(raw=raw if raw is not None else str(value), value=value,
                  evidence=Evidence(page=1, method="ocr", text=str(raw or value), bbox=(0, 0, 1, 1), conf=conf))


def line(sku, qty, rate, amount_cents, idx=0):
    return LineItem(row_index=idx, sku=f(sku), description=f("desc"), quantity=f(qty), unit_price=f(rate),
                    amount=f(amount_cents))


def invoice(**over) -> ExtractedInvoice:
    inv = ExtractedInvoice(
        supplier_name=f("Acme Testing Co."), invoice_number=f("AT-100"), invoice_date=f("2026-05-01"),
        po_reference=f("PO-1"), currency=f("USD"), net=f(500000), tax=f(0), gross=f(500000),
        lines=[line("SVC", "50", "100", 500000)],
    )
    for k, v in over.items():
        setattr(inv, k, v)
    return inv


def snapshot(allocations=None, status="OPEN", vendor_status="APPROVED") -> Snapshot:
    return Snapshot(
        vendors=[{"vendor_id": "V1", "legal_name": "ACME Testing Co", "legal_name_norm": norm_name("ACME Testing Co"),
                  "status": vendor_status, "source_file": "vendors.csv", "source_row": 2}],
        pos={"PO-1": {"po_id": "PO-1", "vendor_id": "V1", "status": status, "currency": "USD",
                      "authorized_net_cents": 1000000, "source_file": "purchase_orders.csv", "source_row": 2}},
        po_lines={"PO-1": [{"po_id": "PO-1", "line_id": "1", "sku": "SVC", "unit_price": "100",
                            "authorized_quantity": "100", "authorized_net_cents": 1000000,
                            "source_file": "po_lines.csv", "source_row": 2}]},
        allocations=allocations or [],
    )


def native(value):
    return Field_(raw=str(value), value=value,
                  evidence=Evidence(page=1, method="native", text=str(value), bbox=(0, 0, 1, 1), conf=100.0))


def prior(net_cents, qty, number="AT-099", date="2026-05-01"):
    return {"id": 1, "run_id": "run_prev", "vendor_id": "V1", "invoice_number_norm": number, "po_id": "PO-1",
            "invoice_date": date, "created_at": "2026-05-02T10:00:00.000+00:00",
            "currency": "USD", "net_cents": net_cents, "tax_cents": 0, "gross_cents": net_cents,
            "lines": [{"line_id": "1", "sku": "SVC", "quantity": qty, "unit_price": "100", "net_cents": net_cents}]}


def run(inv, snap):
    return evaluate(inv, snap, min_conf=80, policy_version=POLICY)


def test_happy_path_ready_with_allocation_and_balance():
    d = run(invoice(), snapshot())
    assert d.decision == "READY" and d.po_id == "PO-1" and d.vendor_id == "V1"
    a = d.allocation
    assert a["net_cents"] == 500000
    assert a["balance_before"]["remaining_net_cents"] == 1000000
    assert a["balance_after"]["remaining_net_cents"] == 500000
    assert a["balance_after"]["lines"][0]["remaining_quantity"] == "50"


def test_net_exactly_at_cap_is_ready_and_one_cent_over_holds():
    d = run(invoice(), snapshot([prior(500000, "50")]))
    assert d.decision == "READY"
    assert d.allocation["balance_after"]["remaining_net_cents"] == 0
    # Prior 99 units / 990,000c. Incoming 1 unit at 100.01 (0.01% variance) = 10,001c -> one cent over.
    over = invoice(net=f(10001), gross=f(10001), lines=[line("SVC", "1", "100.01", 10001)])
    d2 = run(over, snapshot([prior(990000, "99")]))
    assert d2.decision == "HOLD" and d2.reason_code == "AMOUNT_EXCEEDS_PO"
    assert d2.allocation is None
    at = invoice(net=f(10000), gross=f(10000), lines=[line("SVC", "1", "100", 10000)])
    d3 = run(at, snapshot([prior(990000, "99")]))
    assert d3.decision == "READY" and d3.allocation["balance_after"]["remaining_net_cents"] == 0


def test_quantity_at_cap_ready_and_one_unit_over_holds():
    assert run(invoice(), snapshot([prior(400000, "50")])).decision == "READY"
    d = run(invoice(), snapshot([prior(400000, "51")]))
    assert d.decision == "HOLD" and d.reason_code == "QUANTITY_EXCEEDS_PO"


def test_cumulative_breach_of_money_and_quantity_reports_every_comparison():
    # Prior accepted 60 units / 600,000c. Incoming 45 units / 450,000c: each fits a fresh PO alone,
    # together they exceed the 100-unit and 1,000,000c caps.
    inv = invoice(invoice_number=f("AT-101"), net=f(450000), gross=f(450000), lines=[line("SVC", "45", "100", 450000)])
    d = run(inv, snapshot([prior(600000, "60", number="AT-100")]))
    assert d.decision == "HOLD" and d.allocation is None
    assert d.reason_code == "AMOUNT_EXCEEDS_PO"  # money outranks quantity, deterministically
    assert d.reason_codes == ["AMOUNT_EXCEEDS_PO", "QUANTITY_EXCEEDS_PO"]
    auth = d.authorization
    breaches = {(b["measure"], b["scope"]): b for b in auth["breaches"]}
    assert set(breaches) == {("quantity", "line"), ("net", "line"), ("net", "po")}
    po_net = breaches[("net", "po")]
    assert (po_net["authorized"], po_net["previously_allocated"], po_net["remaining"], po_net["incoming"],
            po_net["projected"], po_net["excess"]) == (1000000, 600000, 400000, 450000, 1050000, 50000)
    q = breaches[("quantity", "line")]
    assert (q["authorized"], q["previously_allocated"], q["remaining"], q["incoming"], q["projected"], q["excess"]) == (
        "100", "60", "40", "45", "105", "5")
    assert [p["invoice_number"] for p in auth["prior_invoices"]] == ["AT-100"]
    assert auth["balance_at_decision"]["remaining_net_cents"] == 400000
    assert "AT-100" in d.reason and "USD 500.00" in d.reason and "105" in d.reason
    assert "change order on PO-1" in d.next_action and "USD 500.00" in d.next_action and "5 units of SVC" in d.next_action
    assert d.review_field == "net"
    # Same inputs, same output.
    assert run(inv, snapshot([prior(600000, "60", number="AT-100")])) == d


def test_quantity_only_breach_keeps_quantity_code():
    d = run(invoice(), snapshot([prior(400000, "51")]))
    assert d.reason_codes == ["QUANTITY_EXCEEDS_PO"]
    assert [b["measure"] for b in d.authorization["breaches"]] == ["quantity"]
    assert d.review_field == "line 1 quantity"


def test_ready_decision_records_passing_comparisons():
    d = run(invoice(), snapshot())
    assert d.authorization["breaches"] == []
    assert len(d.authorization["comparisons"]) == 3 and all(c["status"] == "pass" for c in d.authorization["comparisons"])


def test_price_tolerance_one_percent_inclusive():
    at = invoice(net=f(505000), gross=f(505000), lines=[line("SVC", "50", "101", 505000)])
    assert run(at, snapshot()).decision == "READY"
    above = invoice(net=f(505050), gross=f(505050), lines=[line("SVC", "50", "101.01", 505050)])
    d = run(above, snapshot())
    assert d.decision == "HOLD" and d.reason_code == "PRICE_VARIANCE"


@pytest.mark.parametrize("field", ["invoice_number", "invoice_date", "gross", "net", "supplier_name"])
def test_missing_critical_field_holds(field):
    d = run(invoice(**{field: Field_(note="not found")}), snapshot())
    assert d.decision == "HOLD" and d.reason_code == "MISSING_OR_UNCLEAR_FIELD" and d.review_field == field


def test_low_confidence_value_holds():
    d = run(invoice(gross=f(500000, conf=55.0)), snapshot())
    assert d.decision == "HOLD" and d.reason_code == "LOW_CONFIDENCE_FIELD" and d.review_field == "gross"


def test_arithmetic_mismatches_hold():
    d = run(invoice(lines=[line("SVC", "50", "100", 499000)]), snapshot())
    assert d.reason_code == "LINE_ARITHMETIC_MISMATCH"
    d = run(invoice(gross=f(510000)), snapshot())
    assert d.reason_code == "TOTALS_MISMATCH"


def test_vendor_and_po_problems_hold():
    assert run(invoice(supplier_name=f("Unknown Vendor LLC")), snapshot()).reason_code == "VENDOR_NOT_FOUND"
    assert run(invoice(), snapshot(vendor_status="SUSPENDED")).reason_code == "VENDOR_NOT_APPROVED"
    assert run(invoice(po_reference=f("PO-404")), snapshot()).reason_code == "PO_NOT_FOUND"
    assert run(invoice(), snapshot(status="CLOSED")).reason_code == "PO_NOT_OPEN"
    d = run(invoice(po_reference=Field_()), snapshot())
    assert d.reason_code == "PO_MISSING" and d.candidate_po_ids == ["PO-1"] and d.allocation is None
    assert run(invoice(lines=[line("OTHER", "50", "100", 500000)]), snapshot()).reason_code == "LINE_NOT_ON_PO"


def test_duplicate_identical_blocks_and_conflict_holds():
    snap = snapshot([prior(500000, "50", number="AT-100")])
    d = run(invoice(), snap)
    assert d.decision == "BLOCKED" and d.reason_code == "DUPLICATE_INVOICE"
    assert d.prior_allocations[0]["run_id"] == "run_prev"
    dup = d.duplicate
    assert dup["outcome"] == "corroborated" and d.po_id == "PO-1"
    assert dup["business_key"] == {"vendor_id": "V1", "vendor_name": "ACME Testing Co", "invoice_number": "AT-100",
                                   "invoice_number_raw": "AT-100"}
    assert dup["accepted"]["run_id"] == "run_prev" and dup["accepted"]["invoice_date"] == "2026-05-01"
    assert {r["status"] for r in dup["comparison"]} == {"match"}
    assert "Do not pay again" in d.next_action
    changed = invoice(net=f(400000), gross=f(400000), lines=[line("SVC", "40", "100", 400000)])
    c = run(changed, snap)
    assert c.decision == "HOLD" and c.reason_code == "DUPLICATE_NUMBER_CONFLICT" and c.allocation is None
    assert [r["field"] for r in c.duplicate["comparison"] if r["status"] == "conflict"] == [
        "net", "gross", "SVC quantity", "SVC amount"]


def test_same_key_and_amounts_with_different_printed_date_is_a_conflict():
    snap = snapshot([prior(500000, "50", number="AT-100", date="2026-05-01")])
    d = run(invoice(invoice_date=f("2026-06-01")), snap)
    assert d.decision == "HOLD" and d.reason_code == "DUPLICATE_NUMBER_CONFLICT"
    conflicts = [r for r in d.duplicate["comparison"] if r["status"] == "conflict"]
    assert [(r["field"], r["accepted"], r["incoming"]) for r in conflicts] == [
        ("invoice_date", "2026-05-01", "2026-06-01")]
    assert d.review_field == "invoice_date" and "2026-06-01" in d.reason and d.allocation is None


def test_copy_without_printed_po_still_corroborates():
    snap = snapshot([prior(500000, "50", number="AT-100")])
    d = run(invoice(po_reference=Field_()), snap)
    assert d.decision == "BLOCKED"
    assert {r["field"]: r["status"] for r in d.duplicate["comparison"]}["po_reference"] == "not_printed"


def test_leading_zeros_and_punctuation_make_distinct_invoice_numbers():
    snap = snapshot([prior(300000, "30", number="AT-100")])
    for other in ("AT-0100", "AT100", "AT-100A", "AT-100/1"):
        d = run(invoice(invoice_number=native(other), net=f(300000), gross=f(300000),
                        lines=[line("SVC", "30", "100", 300000)]), snap)
        assert d.decision == "READY", (other, d.reason_code)
        assert d.duplicate["outcome"] == "no_match"


def test_distinct_number_with_same_vendor_po_and_amount_goes_through_normal_checks():
    snap = snapshot([prior(300000, "30", number="AT-100")])
    same_amount = dict(net=f(300000), gross=f(300000), lines=[line("SVC", "30", "100", 300000)])
    assert run(invoice(invoice_number=native("AT-101"), **same_amount), snap).decision == "READY"
    # ... and is still subject to the authorization cap.
    big = snapshot([prior(800000, "80", number="AT-100")])
    d = run(invoice(invoice_number=native("AT-101"), **same_amount), big)
    assert d.decision == "HOLD" and d.reason_code == "AMOUNT_EXCEEDS_PO"


def test_ocr_confusable_near_match_fails_closed_but_native_text_does_not():
    snap = snapshot([prior(300000, "30", number="AT-100")])
    same_amount = dict(net=f(300000), gross=f(300000), lines=[line("SVC", "30", "100", 300000)])
    d = run(invoice(invoice_number=f("AT-1OO"), **same_amount), snap)  # OCR read letter O
    assert d.decision == "HOLD" and d.reason_code == "DUPLICATE_IDENTITY_UNCERTAIN" and d.allocation is None
    assert d.duplicate["accepted"]["invoice_number"] == "AT-100"
    assert run(invoice(invoice_number=native("AT-1OO"), **same_amount), snap).decision == "READY"


def test_held_invoice_does_not_count_for_duplicates():
    # Only accepted allocations are in the snapshot; a previously HELD AT-100 leaves no trace,
    # so a corrected resubmission with the same number is evaluated normally.
    assert run(invoice(), snapshot([])).decision == "READY"


def test_extraction_issues_hold_and_evaluate_is_pure():
    inv = invoice()
    inv.issues = ["net: conflicting values: $1.00, $2.00"]
    assert run(inv, snapshot()).reason_code == "EXTRACTION_ISSUE"
    snap = snapshot()
    before = copy.deepcopy(snap)
    run(invoice(), snap)
    assert snap == before
