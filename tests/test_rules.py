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


def prior(net_cents, qty, number="AT-099"):
    return {"id": 1, "run_id": "run_prev", "vendor_id": "V1", "invoice_number_norm": number, "po_id": "PO-1",
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
    changed = invoice(net=f(400000), gross=f(400000), lines=[line("SVC", "40", "100", 400000)])
    assert run(changed, snap).reason_code == "DUPLICATE_NUMBER_CONFLICT"


def test_extraction_issues_hold_and_evaluate_is_pure():
    inv = invoice()
    inv.issues = ["net: conflicting values: $1.00, $2.00"]
    assert run(inv, snapshot()).reason_code == "EXTRACTION_ISSUE"
    snap = snapshot()
    before = copy.deepcopy(snap)
    run(invoice(), snap)
    assert snap == before
