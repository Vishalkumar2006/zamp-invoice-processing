"""Recurring service invoices: tax wording, legal-suffix vendor variants, description-only lines, service
periods and a resent copy whose number differs only by leading zeros. All values are synthetic."""
from __future__ import annotations

import pytest

from app.extraction import extract
from app.extraction.parser import parse_period
from app.models import Field_, LineItem
from app.procurement import norm_name
from app.rules import Snapshot, evaluate
from tests.conftest import wait_for
from tests.pdfgen import Text, make_pdf
from tests.test_rules import f, invoice

LEGAL = "Corvane Network Services LLC"
PRINTED = "Corvane Network Services"
MSP_DESC = "Managed network services - monthly fee (FY2026)"


def vendor(vid, name, status="APPROVED", row=2):
    return {"vendor_id": vid, "legal_name": name, "legal_name_norm": norm_name(name), "status": status,
            "source_file": "vendors.csv", "source_row": row}


def po_line(line_id, sku, desc, qty, price, net, row):
    return {"po_id": "PO-S1", "line_id": line_id, "sku": sku, "description": desc, "unit_price": price,
            "authorized_quantity": qty, "authorized_net_cents": net, "source_file": "po_lines.csv", "source_row": row}


def snap(vendors=None, lines=None, allocations=None, po_vendor="V1") -> Snapshot:
    lines = lines or [po_line("1", "NET-MSP", MSP_DESC, "12", "1800", 2160000, 2),
                      po_line("2", "NET-HW", "Network hardware replacement parts", "10", "250", 250000, 3)]
    return Snapshot(vendors=vendors or [vendor("V1", LEGAL)],
                    pos={"PO-S1": {"po_id": "PO-S1", "vendor_id": po_vendor, "status": "OPEN", "currency": "USD",
                                   "authorized_net_cents": sum(l["authorized_net_cents"] for l in lines),
                                   "source_file": "purchase_orders.csv", "source_row": 2}},
                    po_lines={"PO-S1": lines}, allocations=allocations or [])


def svc_line(desc="Managed network services fee - March 2026 Monitoring, firewall updates", rate="1800", qty="1",
             amount=180000, desc_conf=95.0):
    return LineItem(row_index=0, sku=Field_(), description=f(desc, conf=desc_conf), quantity=f(qty), unit_price=f(rate),
                    amount=f(amount))


def svc_invoice(number="CNS-118", period="2026-03", date="2026-03-31", supplier=PRINTED, line=None, po="PO-S1", **kw):
    ln = line or svc_line()
    return invoice(supplier_name=f(supplier), invoice_number=f(number), invoice_date=f(date),
                   po_reference=f(po) if po else Field_(), service_period=f(period) if period else Field_(),
                   net=f(ln.amount.value), tax=f(0, raw="Not applicable"), gross=f(ln.amount.value), lines=[ln], **kw)


def accepted(number="CNS-118", period="2026-03", date="2026-03-31", qty="1", net=180000):
    return {"id": 1, "run_id": "run_prev", "vendor_id": "V1", "invoice_number_norm": number, "po_id": "PO-S1",
            "invoice_date": date, "service_period": period, "created_at": "2026-04-02T10:00:00.000+00:00",
            "currency": "USD", "net_cents": net, "tax_cents": 0, "gross_cents": net,
            "lines": [{"line_id": "1", "sku": "NET-MSP", "quantity": qty, "unit_price": "1800", "net_cents": net}]}


def run(inv, s):
    return evaluate(inv, s, min_conf=80, policy_version="t")


# --------------------------------------------------------------------------- vendor resolution

def test_shortened_supplier_name_resolves_to_unique_legal_suffix_variant():
    d = run(svc_invoice(), snap())
    assert d.decision == "READY" and d.vendor_id == "V1"
    m = d.vendor_match
    assert (m["method"], m["printed"], m["legal_name"], m["suffix_printed"], m["suffix_on_legal_name"]) == (
        "legal_suffix_variant", PRINTED, LEGAL, None, "llc")
    assert m["source"] == "vendors.csv row 2" and m["vendors_compared"] == 1
    assert "legal-suffix variant" in next(c["detail"] for c in d.checks if c["rule"] == "vendor")


def test_exact_legal_name_takes_precedence_over_a_suffix_variant():
    d = run(svc_invoice(), snap(vendors=[vendor("V1", LEGAL), vendor("V2", PRINTED, row=3)]))
    assert d.vendor_id == "V2" and d.vendor_match["method"] == "exact"


def test_two_suffix_variants_are_ambiguous_and_hold():
    d = run(svc_invoice(), snap(vendors=[vendor("V1", LEGAL), vendor("V3", "Corvane Network Services Inc", row=3)]))
    assert (d.decision, d.reason_code, d.allocation) == ("HOLD", "VENDOR_AMBIGUOUS", None)
    assert "V1" in d.reason and "V3" in d.reason


@pytest.mark.parametrize("printed", ["Corvane Network Services Inc", "Corvane"])
def test_different_suffix_or_too_short_a_name_does_not_match(printed):
    d = run(svc_invoice(supplier=printed), snap(vendors=[vendor("V1", LEGAL if printed != "Corvane" else "Corvane LLC")]))
    assert (d.decision, d.reason_code) == ("HOLD", "VENDOR_NOT_FOUND")


def test_suffix_variant_of_an_inactive_vendor_is_still_not_approved():
    d = run(svc_invoice(), snap(vendors=[vendor("V1", LEGAL, status="INACTIVE")]))
    assert (d.decision, d.reason_code) == ("HOLD", "VENDOR_NOT_APPROVED")


# --------------------------------------------------------------------------- description-only lines

def test_description_only_line_maps_to_a_unique_po_line_and_keeps_sku_null():
    inv = svc_invoice()
    d = run(inv, snap())
    assert d.decision == "READY" and inv.lines[0].sku.value is None
    m = d.line_matches[0]
    assert (m["method"], m["invoice_sku"], m["po_line_id"], m["po_sku"], m["source"]) == (
        "description", None, "1", "NET-MSP", "po_lines.csv row 2")
    assert m["shared_words"] == ["fee", "managed", "network", "services"]
    assert d.allocation["lines"] == [{"line_id": "1", "sku": "NET-MSP", "quantity": "1", "unit_price": "1800",
                                      "net_cents": 180000}]
    assert d.price_check["lines"][0]["invoice_sku"] is None and d.price_check["lines"][0]["sku"] == "NET-MSP"


def test_description_matching_two_po_lines_holds():
    lines = [po_line("1", "NET-MSP", MSP_DESC, "12", "1800", 2160000, 2),
             po_line("2", "NET-MSP-B", "Managed network services monthly fee - site B", "12", "1800", 2160000, 3)]
    d = run(svc_invoice(), snap(lines=lines))
    assert (d.decision, d.reason_code, d.allocation) == ("HOLD", "LINE_NOT_ON_PO", None)
    assert "more than one line" in d.reason and d.line_matches[0]["status"] == "ambiguous"


def test_description_matching_no_po_line_holds():
    d = run(svc_invoice(line=svc_line(desc="Office chairs, ergonomic")), snap())
    assert (d.decision, d.reason_code) == ("HOLD", "LINE_NOT_ON_PO")
    assert d.line_matches[0]["status"] == "no_match"


def test_description_match_still_gets_the_price_check():
    d = run(svc_invoice(line=svc_line(rate="1890", amount=189000)), snap())  # +5% on 1800
    assert (d.decision, d.reason_code) == ("HOLD", "PRICE_VARIANCE")


def test_description_match_still_gets_the_quantity_cap():
    d = run(svc_invoice(number="CNS-140"), snap(allocations=[accepted(qty="12", net=2160000)]))
    assert d.decision == "HOLD" and "QUANTITY_EXCEEDS_PO" in d.reason_codes


def test_description_only_line_without_a_printed_po_is_not_matched():
    d = run(svc_invoice(po=None), snap())
    assert (d.decision, d.reason_code, d.candidate_po_ids) == ("HOLD", "PO_MISSING_NO_MATCH", [])
    assert "has no SKU" in d.reason


def test_unclear_description_holds_at_the_evidence_gate():
    d = run(svc_invoice(line=svc_line(desc_conf=60.0)), snap())
    assert (d.decision, d.reason_code, d.review_field) == ("HOLD", "LOW_CONFIDENCE_FIELD", "line 1 description")


# --------------------------------------------------------------------------- recurring charges and resent copies

def test_next_month_with_a_new_number_and_the_same_amount_is_a_separate_charge():
    d = run(svc_invoice(number="CNS-131", period="2026-04", date="2026-04-30"), snap(allocations=[accepted()]))
    assert d.decision == "READY" and d.duplicate["outcome"] == "no_match"


def test_leading_zero_variant_corroborated_by_period_and_billing_is_blocked():
    d = run(svc_invoice(number="CNS-0118"), snap(allocations=[accepted()]))
    assert (d.decision, d.reason_code, d.allocation) == ("BLOCKED", "DUPLICATE_INVOICE", None)
    dup = d.duplicate
    assert dup["outcome"] == "variant_corroborated" and dup["accepted"]["run_id"] == "run_prev"
    assert dup["number_variant"] == {"printed": "CNS-0118", "accepted": "CNS-118", "rule": "equal apart from leading zeros"}
    assert all(r["status"] == "match" for r in dup["comparison"])
    assert {r["field"] for r in dup["comparison"]} >= {"service_period", "po_reference", "net", "gross", "invoice_date"}
    assert "run_prev" in d.reason and "CNS-118" in d.next_action


def test_leading_zero_variant_with_a_different_date_is_held_not_blocked():
    d = run(svc_invoice(number="CNS-0118", date="2026-04-14"), snap(allocations=[accepted()]))
    assert (d.decision, d.reason_code) == ("HOLD", "DUPLICATE_IDENTITY_UNCERTAIN")
    assert d.duplicate["outcome"] == "variant_uncertain" and d.duplicate["accepted"]["run_id"] == "run_prev"


@pytest.mark.parametrize("period", ["2026-04", None])
def test_leading_zero_variant_without_period_corroboration_runs_the_normal_checks(period):
    # Same vendor, PO and amount, but a different or unprinted service period: not called a duplicate.
    d = run(svc_invoice(number="CNS-0118", period=period), snap(allocations=[accepted()]))
    assert d.decision == "READY" and d.duplicate["outcome"] == "no_match"
    assert d.duplicate["near_matches"][0]["outcome"] == "variant_not_corroborated"


def test_exact_invoice_number_is_still_blocked_by_the_existing_rule():
    d = run(svc_invoice(), snap(allocations=[accepted()]))
    assert (d.decision, d.reason_code, d.duplicate["outcome"]) == ("BLOCKED", "DUPLICATE_INVOICE", "corroborated")


# --------------------------------------------------------------------------- extraction

@pytest.mark.parametrize("text,value", [("September 2026", "2026-09"), ("Sep 2026", "2026-09"), ("2026-09", "2026-09"),
                                        ("2026-09-01 to 2026-09-30", "2026-09-01/2026-09-30"), ("Q3 2026", None)])
def test_service_period_formats(text, value):
    assert parse_period(text)[0] == value


def service_pdf(*, number="CNS-118", date="2026-03-31", period="March 2026", month="March", amount="$1,800.00",
                subtotal="$1,800.00", tax="Not applicable", total="$1,800.00") -> bytes:
    # Made-up geometry: the quantity figure sits right of its header text (column-band path); rate and amount
    # figures overlap their headers.
    t = [Text(44, 36, PRINTED, 17, True), Text(462, 36, "Tax Invoice", 14, True),
         Text(44, 58, "41 Relay Street, Suite 9 · billing@corvane.example", 9),
         Text(44, 104, "Billed to", 9), Text(342, 104, "Invoice no.", 9), Text(478, 104, number, 9),
         Text(342, 119, "Issue date", 9), Text(478, 119, date, 9),
         Text(342, 134, "Service period", 9), Text(478, 134, period, 9),
         Text(342, 149, "Purchase order", 9), Text(478, 149, "PO-S1", 9),
         Text(48, 206, "Description", 9, True), Text(372, 206, "Qty", 9, True), Text(418, 206, "Rate", 9, True),
         Text(488, 206, "Amount", 9, True),
         Text(48, 223, f"Managed network services fee - {month} 2026", 9), Text(406, 223, "1", 9),
         Text(436, 223, amount, 9), Text(508, 223, amount, 9),
         Text(52, 238, "Monitoring, firewall updates", 9),
         Text(402, 268, "Subtotal", 10), Text(508, 268, subtotal, 10),
         Text(398, 283, "Sales tax", 10), Text(490, 283, tax, 10),
         Text(352, 298, "Amount due (USD)", 10), Text(508, 298, total, 10)]
    return make_pdf(t)


def test_tax_wording_reads_as_zero_when_totals_reconcile_and_keeps_the_text(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(service_pdf())
    inv = extract(p).invoice
    assert inv.issues == []
    assert (inv.tax.value, inv.tax.raw, inv.tax.evidence.text, inv.tax.evidence.label) == (
        0, "Not applicable", "Not applicable", "Sales tax")
    assert "subtotal equals the total" in inv.tax.note
    assert (inv.service_period.value, inv.service_period.raw, inv.service_period.evidence.label) == (
        "2026-03", "March 2026", "Service period")
    assert inv.lines[0].sku.value is None and inv.lines[0].quantity.value == "1"
    assert inv.lines[0].description.value == "Managed network services fee - March 2026 Monitoring, firewall updates"


def test_tax_wording_with_totals_that_differ_stays_unresolved(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(service_pdf(total="$1,950.00"))
    inv = extract(p).invoice
    assert inv.tax.value is None and inv.tax.raw == "Not applicable"
    assert "tax: printed as 'Not applicable' but the subtotal and total differ" in inv.issues


# --------------------------------------------------------------------------- full flow

VENDORS = f"vendor_id,legal_name,status\nV-CNS,{LEGAL},APPROVED\nV-OTH,Corvane Metals Supply Co,APPROVED\n".encode()
POS = b"po_id,vendor_id,issued_on,status,currency,description,authorized_net\nPO-S1,V-CNS,2026-01-05,OPEN,USD,,21600.00\n"
PO_LINES = (b"po_id,line_id,sku,description,authorized_quantity,unit_price,authorized_net\n"
            b"PO-S1,1,NET-MSP,Managed network services - monthly fee (FY2026),12,1800.00,21600.00\n")


def test_recurring_months_then_a_resent_copy(client):
    r = client.post("/procurement/import", files={"vendors": ("v.csv", VENDORS), "purchase_orders": ("p.csv", POS),
                                                  "po_lines": ("l.csv", PO_LINES)})
    ws = r.json()["workspace_id"]

    def submit(pdf):
        rid = client.post("/runs", files={"file": ("i.pdf", pdf, "application/pdf")}, data={"workspace_id": ws}).json()["run_id"]
        return wait_for(client, rid)

    def ledger():
        led = client.get("/ledger", params={"workspace_id": ws}).json()
        po = led["purchase_orders"][0]
        return led["allocation_count"], po["remaining_net_cents"], po["lines"][0]["remaining_quantity"]

    march = submit(service_pdf())
    assert (march["decision"], march["po_id"]) == ("READY", "PO-S1")
    assert march["decision_detail"]["vendor_match"]["method"] == "legal_suffix_variant"
    assert march["decision_detail"]["line_matches"][0]["po_sku"] == "NET-MSP"
    assert ledger() == (1, 1980000, "11")

    april = submit(service_pdf(number="CNS-131", date="2026-04-30", period="April 2026", month="April"))
    assert april["decision"] == "READY" and ledger() == (2, 1800000, "10")

    resend = submit(service_pdf(number="CNS-0131", date="2026-04-30", period="April 2026", month="April"))
    assert (resend["decision"], resend["reason_code"], resend["allocation_id"]) == ("BLOCKED", "DUPLICATE_INVOICE", None)
    assert resend["invoice_number"] == "CNS-0131"  # printed number kept, leading zero included
    assert resend["decision_detail"]["duplicate"]["accepted"]["run_id"] == april["run_id"]
    assert ledger() == (2, 1800000, "10")
    assert client.get(f"/ui/runs/{resend['run_id']}").status_code == 200
