"""Missing printed PO: candidate evidence and specific hold reasons (pure decision function)."""
from __future__ import annotations

from app.models import Evidence, Field_
from app.procurement import norm_name
from app.rules import Snapshot, evaluate
from tests.test_rules import f, invoice, line, native, prior


def po(po_id, *, status="OPEN", currency="USD", qty="24", price="125", net=300000, row=2):
    header = {"po_id": po_id, "vendor_id": "V1", "status": status, "currency": currency, "issued_on": "2026-10-29",
              "description": f"{po_id} maintenance", "authorized_net_cents": net, "source_file": "purchase_orders.csv",
              "source_row": row}
    lines = [{"po_id": po_id, "line_id": "10", "sku": "FLT", "unit_price": price, "authorized_quantity": qty,
              "authorized_net_cents": net, "source_file": "po_lines.csv", "source_row": row}]
    return header, lines


def snap(*pos, allocations=None):
    return Snapshot(
        vendors=[{"vendor_id": "V1", "legal_name": "ACME Testing Co", "legal_name_norm": norm_name("ACME Testing Co"),
                  "status": "APPROVED", "source_file": "vendors.csv", "source_row": 2}],
        pos={h["po_id"]: h for h, _ in pos}, po_lines={h["po_id"]: ls for h, ls in pos},
        allocations=allocations or [])


def no_po_invoice(qty="24", price="125", amount=300000, delivery=None, delivery_conf=100.0):
    inv = invoice(po_reference=Field_(), net=f(amount), gross=f(amount), lines=[line("FLT", qty, price, amount)])
    if delivery:
        inv.delivery_reference = Field_(raw=delivery, value=delivery, evidence=Evidence(
            page=1, method="native", text=f"Delivery reference {delivery}.", bbox=(0, 0, 1, 1), conf=delivery_conf))
    return inv


def run(inv, s):
    return evaluate(inv, s, min_conf=80, policy_version="t")


def test_two_viable_candidates_are_ambiguous_and_nothing_is_selected():
    d = run(no_po_invoice(delivery="DN-4410"), snap(po("PO-A", row=2), po("PO-B", row=3)))
    assert d.decision == "HOLD" and d.reason_code == "PO_AMBIGUOUS"
    assert d.po_id is None and d.allocation is None and d.authorization is None
    assert d.candidate_po_ids == ["PO-A", "PO-B"]
    pc = d.po_candidates
    assert pc["viable_po_ids"] == ["PO-A", "PO-B"] and pc["delivery_reference"] == "DN-4410"
    a, b = pc["candidates"]
    assert (a["source"], b["source"]) == ("purchase_orders.csv row 2", "purchase_orders.csv row 3")
    for c in (a, b):
        assert c["viable"] and c["failures"] == [] and c["remaining_net_cents"] == 300000 and c["total_ok"]
        ln = c["lines"][0]
        assert (ln["sku_ok"], ln["price_ok"], ln["quantity_ok"], ln["amount_ok"]) == (True, True, True, True)
        assert ln["source"].startswith("po_lines.csv row ") and ln["remaining_quantity"] == "24"
    assert "DN-4410" in d.next_action and "PO-A or PO-B" in d.next_action and "procurement" in d.next_action
    assert d.review_field == "po_reference"


def test_one_viable_candidate_is_suggested_but_still_held():
    used = [{"id": 9, "run_id": "run_x", "vendor_id": "V1", "invoice_number_norm": "AT-001", "po_id": "PO-B",
             "invoice_date": "2026-04-01", "created_at": "2026-04-02T00:00:00", "currency": "USD", "net_cents": 100000,
             "tax_cents": 0, "gross_cents": 100000,
             "lines": [{"line_id": "10", "sku": "FLT", "quantity": "8", "unit_price": "125", "net_cents": 100000}]}]
    d = run(no_po_invoice(), snap(po("PO-A"), po("PO-B"), po("PO-C", status="CLOSED"), allocations=used))
    assert d.decision == "HOLD" and d.reason_code == "PO_MISSING" and d.allocation is None and d.po_id is None
    assert d.candidate_po_ids == ["PO-A"]
    by = {c["po_id"]: c for c in d.po_candidates["candidates"]}
    assert by["PO-B"]["viable"] is False and any("remaining" in x for x in by["PO-B"]["failures"])
    assert by["PO-C"]["failures"] == ["status is CLOSED"]
    assert "confirm that PO-A" in d.next_action


def test_no_viable_candidate_holds_with_reasons():
    d = run(no_po_invoice(price="140", amount=336000),
            snap(po("PO-A"), po("PO-B", currency="EUR"), po("PO-C", qty="10", net=125000)))
    assert d.decision == "HOLD" and d.reason_code == "PO_MISSING_NO_MATCH" and d.candidate_po_ids == []
    by = {c["po_id"]: c["failures"] for c in d.po_candidates["candidates"]}
    assert any("over 1%" in x for x in by["PO-A"])
    assert any("currency EUR" in x for x in by["PO-B"])
    assert any("quantity 24 > remaining 10" in x for x in by["PO-C"])
    assert d.allocation is None


def test_price_within_tolerance_still_counts_as_viable():
    d = run(no_po_invoice(price="126.25", amount=303000), snap(po("PO-A", net=400000, qty="40")))
    assert d.candidate_po_ids == ["PO-A"] and d.po_candidates["candidates"][0]["lines"][0]["variance_pct"] == "1.00"


def test_valid_explicit_reference_keeps_existing_checks():
    s = snap(po("PO-A"), po("PO-B"))
    d = run(invoice(po_reference=native("PO-B"), net=f(300000), gross=f(300000), lines=[line("FLT", "24", "125", 300000)]), s)
    assert d.decision == "READY" and d.po_id == "PO-B" and d.po_candidates is None
    assert d.allocation["po_id"] == "PO-B"
    wrong = run(invoice(po_reference=native("PO-Z"), net=f(300000), gross=f(300000),
                        lines=[line("FLT", "24", "125", 300000)]), s)
    assert wrong.reason_code == "PO_NOT_FOUND"


def test_low_confidence_delivery_reference_is_not_shown_as_reliable():
    d = run(no_po_invoice(delivery="DN-44l0", delivery_conf=61.0), snap(po("PO-A"), po("PO-B")))
    assert d.po_candidates["delivery_reference"] is None
    assert "DN-44l0" not in d.next_action


# --------------------------------------------------------------------------- narrowed OCR near match

SAME = dict(net=f(300000), gross=f(300000), lines=[line("SVC", "30", "100", 300000)])


def _one_po():
    from tests.test_rules import snapshot
    return snapshot([prior(300000, "30", number="AT-100", date="2026-05-01")])


def test_near_match_with_corroborating_date_and_billing_stays_uncertain():
    d = evaluate(invoice(invoice_number=f("AT-1OO"), invoice_date=f("2026-05-01"), **SAME), _one_po(),
                 min_conf=80, policy_version="t")
    assert d.decision == "HOLD" and d.reason_code == "DUPLICATE_IDENTITY_UNCERTAIN" and d.allocation is None
    assert "matching: invoice_date" in d.reason


def test_near_match_with_later_date_and_different_billing_proceeds():
    distinct = dict(net=f(200000), gross=f(200000), lines=[line("SVC", "20", "100", 200000)])
    d = evaluate(invoice(invoice_number=f("AT-1OO"), invoice_date=f("2026-06-01"), **distinct), _one_po(),
                 min_conf=80, policy_version="t")
    assert d.decision == "READY", (d.reason_code, d.reason)
    nm = d.duplicate["near_matches"][0]
    assert nm["outcome"] == "distinct" and nm["accepted"]["invoice_number"] == "AT-100"


def test_near_match_with_only_one_kind_of_difference_fails_closed():
    later_same_billing = invoice(invoice_number=f("AT-1OO"), invoice_date=f("2026-06-01"), **SAME)
    same_date_new_billing = invoice(invoice_number=f("AT-1OO"), invoice_date=f("2026-05-01"), net=f(200000),
                                    gross=f(200000), lines=[line("SVC", "20", "100", 200000)])
    earlier_new_billing = invoice(invoice_number=f("AT-1OO"), invoice_date=f("2026-04-01"), net=f(200000),
                                  gross=f(200000), lines=[line("SVC", "20", "100", 200000)])
    for inv in (later_same_billing, same_date_new_billing, earlier_new_billing):
        d = evaluate(inv, _one_po(), min_conf=80, policy_version="t")
        assert d.reason_code == "DUPLICATE_IDENTITY_UNCERTAIN", inv.invoice_date.value


def test_near_match_number_below_confidence_floor_holds_on_confidence_first():
    d = evaluate(invoice(invoice_number=f("AT-1OO", conf=75.0), invoice_date=f("2026-06-01"), net=f(200000),
                         gross=f(200000), lines=[line("SVC", "20", "100", 200000)]), _one_po(),
                 min_conf=80, policy_version="t")
    assert d.reason_code == "LOW_CONFIDENCE_FIELD" and d.review_field == "invoice_number"


# --------------------------------------------------------------------------- HTTP flow, real extraction

from tests.conftest import wait_for  # noqa: E402
from tests.pdfgen import invoice_pdf  # noqa: E402

TWIN_VENDORS = b"vendor_id,legal_name,status\nV-900,Harborline Calibration Inc,APPROVED\n"
TWIN_POS = (b"po_id,vendor_id,issued_on,status,currency,description,authorized_net\n"
            b"PO-5101,V-900,2026-02-01,OPEN,USD,Site North,3000.00\n"
            b"PO-5102,V-900,2026-02-01,OPEN,USD,Site South,3000.00\n")
TWIN_LINES = (b"po_id,line_id,sku,description,authorized_quantity,unit_price,authorized_net\n"
              b"PO-5101,1,CAL-STD,Standard calibration,12,250.00,3000.00\n"
              b"PO-5102,1,CAL-STD,Standard calibration,12,250.00,3000.00\n")


def test_ambiguous_po_over_http_leaves_ledger_untouched(client):
    r = client.post("/procurement/import", files={"vendors": ("vendors.csv", TWIN_VENDORS),
                                                  "purchase_orders": ("purchase_orders.csv", TWIN_POS),
                                                  "po_lines": ("po_lines.csv", TWIN_LINES)})
    assert r.status_code == 201
    before = client.get("/ledger").json()
    pdf = invoice_pdf(number="HCR-04600", po=None,
                      lines=(("CAL-STD", "Standard calibration", "12", "$250.00", "$3,000.00"),),
                      subtotal="$3,000.00", total="$3,000.00", footer="Delivery reference DN-5512. Page 1 of 1")
    run_id = client.post("/runs", files={"file": ("inv.pdf", pdf, "application/pdf")}).json()["run_id"]
    run = wait_for(client, run_id)
    assert run["decision"] == "HOLD" and run["reason_code"] == "PO_AMBIGUOUS" and run["po_id"] is None
    assert run["extraction"]["invoice"]["delivery_reference"]["value"] == "DN-5512"
    pc = run["decision_detail"]["po_candidates"]
    assert pc["viable_po_ids"] == ["PO-5101", "PO-5102"] and pc["delivery_reference"] == "DN-5512"
    assert [c["source"] for c in pc["candidates"]] == ["purchase_orders.csv row 2", "purchase_orders.csv row 3"]
    assert "DN-5512" in run["decision_detail"]["next_action"]
    assert run["allocation_id"] is None and client.get("/ledger").json() == before
    r = client.post(f"/runs/{run_id}/finalize").json()
    assert r["already_finalized"] and r["allocations_for_run"] == 0
    assert client.get("/ledger").json() == before
    assert client.get(f"/ui/runs/{run_id}").status_code == 200
