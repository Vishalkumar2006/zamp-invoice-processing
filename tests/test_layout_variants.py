"""Label variants ("Invoice #:", "Customer PO:") and right-aligned figures under left-aligned table headers.

All values are synthetic. The layout mirrors a common vendor template: header labels and values in separate
cells, and Qty / Unit Price / Amount figures right-aligned to a column edge past the header text.
"""
from __future__ import annotations

import pytest

from app.extraction import extract
from app.extraction.parser import Segment, _match_label
from app.models import Token
from tests.conftest import wait_for
from tests.pdfgen import Text, make_pdf

VENDOR = "Norvale Hydraulics Co"
# Helvetica advance widths (per 1000 em) for the characters used in right-aligned figures.
_W = {**{d: 556 for d in "0123456789"}, "$": 556, ",": 278, ".": 278, "-": 333}

# Left-aligned headers; figures right-aligned to each column's right edge. Quantity and amount figures sit
# entirely right of their header text; unit prices overlap theirs.
COL = {"sku": 48, "description": 110, "qty_hdr": 352, "price_hdr": 410, "amount_hdr": 482,
       "qty_right": 398, "price_right": 470, "amount_right": 556}


def _right(x_right: float, y: float, text: str, size: float = 9) -> Text:
    width = sum(_W.get(ch, 556) for ch in text) * size / 1000
    return Text(x_right - width, y, text, size)


def split_po_pdf(*, number="NHC-7731", date="2026-08-14", po="PO-5501", lines=(), subtotal="", tax="", total="",
                 extra=()) -> bytes:
    """lines: (sku, description, qty or None, unit price, amount, optional (x_right, text) for a stray figure)."""
    t = [Text(50, 40, VENDOR.upper(), 16, True), Text(470, 40, "INVOICE", 16, True),
         Text(50, 62, "18 Quarry Lane"), Text(50, 74, "Millbrook, OH 43000"),
         Text(443, 80, "Invoice #:", 9), _right(562, 80, number),
         Text(429, 92, "Invoice Date:", 9), _right(562, 92, date),
         Text(454, 116, "Terms:", 9), Text(535, 116, "Net 30", 9)]
    if po is not None:
        t += [Text(425, 104, "Customer PO:", 9), _right(562, 104, po)]
    t += [Text(50, 150, "BILL TO", 9, True), Text(50, 162, "Demo Buyer Corp", 9)]
    y = 250
    t += [Text(COL["sku"], y, "Item", 9, True), Text(COL["description"], y, "Description", 9, True),
          Text(COL["qty_hdr"], y, "Qty", 9, True), Text(COL["price_hdr"], y, "Unit Price", 9, True),
          Text(COL["amount_hdr"], y, "Amount", 9, True)]
    for sku, desc, qty, price, amount, *stray in lines:
        y += 16
        t += [Text(COL["sku"], y, sku, 9), Text(COL["description"], y, desc, 9),
              _right(COL["price_right"], y, price), _right(COL["amount_right"], y, amount)]
        if qty is not None:
            t.append(_right(COL["qty_right"], y, qty))
        for x_right, text in stray:
            t.append(_right(x_right, y, text))
    y += 28
    t += [Text(415, y, "Subtotal", 10), _right(562, y, subtotal, 10)]
    t += [Text(375, y + 15, "Sales Tax (8.0%)", 10), _right(562, y + 15, tax, 10)]
    t += [Text(395, y + 30, "TOTAL DUE", 10, True), _right(562, y + 30, total, 10)]
    t += list(extra)
    return make_pdf(t)


FIRST = dict(number="NHC-7731", date="2026-08-14", lines=(
    ("PIPE-34", "Steel pipe 3/4 in x 20 ft, pack of 25", "60", "$12.50", "$750.00"),
    ("CPL-12", "Coupling 12 mm brass", "250", "$1.75", "$437.50")),
    subtotal="$1,187.50", tax="$95.00", total="$1,282.50")
SECOND = dict(number="NHC-7790", date="2026-09-02", lines=(
    ("PIPE-34", "Steel pipe 3/4 in x 20 ft, pack of 25", "55", "$12.50", "$687.50"),
    ("CPL-12", "Coupling 12 mm brass", "150", "$1.75", "$262.50")),
    subtotal="$950.00", tax="$76.00", total="$1,026.00")


def _extract(tmp_path, **kw):
    p = tmp_path / "inv.pdf"
    p.write_bytes(split_po_pdf(**kw))
    return extract(p).invoice


# --------------------------------------------------------------------------- labels

def _seg(text: str) -> Segment:
    toks, x = [], 400.0
    for word in text.split():
        toks.append(Token(text=word, page=1, x0=x, y0=100, x1=x + 6 * len(word), y1=109, method="native"))
        x += 6 * len(word) + 3
    return Segment(toks)


@pytest.mark.parametrize("label,field", [
    ("Invoice #:", "invoice_number"), ("Invoice#:", "invoice_number"), ("Invoice # :", "invoice_number"),
    ("Invoice No.:", "invoice_number"), ("INVOICE NUMBER", "invoice_number"),
    ("Customer PO:", "po_reference"), ("Customer P.O. #", "po_reference"), ("Client PO No.", "po_reference"),
    ("Your PO number:", "po_reference"), ("PO #:", "po_reference"), ("Purchase Order:", "po_reference"),
])
def test_label_variants_name_their_field(label, field):
    m = _match_label(_seg(label), include_other=True)
    assert m is not None and m[0] == field and m[1] == len(label.split())


@pytest.mark.parametrize("label", ["Invoice:", "Customer:", "Customer No.:", "Shipment:"])
def test_near_miss_labels_are_not_invoice_number_or_po(label):
    m = _match_label(_seg(label), include_other=True)
    assert m is None or m[0] not in ("invoice_number", "po_reference")


# --------------------------------------------------------------------------- extraction

def test_hash_and_customer_po_labels_and_right_aligned_quantities(tmp_path):
    inv = _extract(tmp_path, **FIRST)
    assert inv.issues == []
    assert inv.invoice_number.value == "NHC-7731" and inv.invoice_number.evidence.label == "Invoice #:"
    assert inv.invoice_number.evidence.text == "NHC-7731" and inv.invoice_number.evidence.page == 1
    assert inv.po_reference.value == "PO-5501" and inv.po_reference.evidence.label == "Customer PO:"
    assert inv.invoice_date.value == "2026-08-14"
    assert (inv.net.value, inv.tax.value, inv.gross.value) == (118750, 9500, 128250)
    assert [(l.sku.value, l.description.value, l.quantity.value, l.unit_price.value, l.amount.value)
            for l in inv.lines] == [
        ("PIPE-34", "Steel pipe 3/4 in x 20 ft, pack of 25", "60", "12.5", 75000),  # 20 and 25 stay in the text
        ("CPL-12", "Coupling 12 mm brass", "250", "1.75", 43750)]
    for line in inv.lines:
        ev = line.quantity.evidence
        assert ev.label == "quantity" and ev.method == "native" and ev.page == 1
        # Right of the "Qty" header text, before the "Unit Price" header: assigned by column band.
        assert ev.bbox[0] > COL["qty_hdr"] + 15 and ev.bbox[2] < COL["price_hdr"]


def test_figure_left_of_the_quantity_column_is_not_taken_as_quantity(tmp_path):
    # A number printed inside the description band is never a quantity: the line has no quantity.
    inv = _extract(tmp_path, lines=(("PIPE-34", "Steel pipe 3/4 in x 20 ft", None, "$12.50", "$750.00", (330, "60")),),
                   subtotal="$750.00", tax="$60.00", total="$810.00")
    line = inv.lines[0]
    assert line.quantity.value is None and line.quantity.note == "missing"
    assert line.description.value == "Steel pipe 3/4 in x 20 ft 60"


def test_two_figures_in_the_quantity_column_are_ambiguous(tmp_path):
    inv = _extract(tmp_path, lines=(("PIPE-34", "Steel pipe", "60", "$12.50", "$750.00", (378, "6")),),
                   subtotal="$750.00", tax="$60.00", total="$810.00")
    q = inv.lines[0].quantity
    assert q.value is None and q.note == "multiple values in cell"
    assert any("quantity: multiple values" in i for i in inv.issues)


# --------------------------------------------------------------------------- full flow

VENDORS = f"vendor_id,legal_name,status\nV-NHC,{VENDOR},APPROVED\n".encode()
POS = (b"po_id,vendor_id,issued_on,status,currency,description,authorized_net\n"
       b"PO-5501,V-NHC,2026-08-01,OPEN,USD,,1950.00\n")
PO_LINES = (b"po_id,line_id,sku,description,authorized_quantity,unit_price,authorized_net\n"
            b"PO-5501,1,PIPE-34,Steel pipe 3/4 in x 20 ft,100,12.50,1250.00\n"
            b"PO-5501,2,CPL-12,Coupling 12 mm brass,400,1.75,700.00\n")


def _import(client):
    r = client.post("/procurement/import", files={"vendors": ("vendors.csv", VENDORS),
                                                  "purchase_orders": ("purchase_orders.csv", POS),
                                                  "po_lines": ("po_lines.csv", PO_LINES)})
    assert r.status_code == 201
    return r.json()["workspace_id"]


def _submit(client, ws, pdf):
    r = client.post("/runs", files={"file": ("inv.pdf", pdf, "application/pdf")}, data={"workspace_id": ws})
    return wait_for(client, r.json()["run_id"])


def _po(client, ws):
    led = client.get("/ledger", params={"workspace_id": ws}).json()
    po = led["purchase_orders"][0]
    return led["allocation_count"], po["remaining_net_cents"], {l["sku"]: l["remaining_quantity"] for l in po["lines"]}


def test_split_po_shipments_ready_then_overbilling_hold(client):
    ws = _import(client)
    first = _submit(client, ws, split_po_pdf(**FIRST))
    assert (first["decision"], first["reason_code"], first["po_id"]) == ("READY", "MATCHED_WITHIN_AUTHORIZATION", "PO-5501")
    assert first["decision_detail"]["allocation"]["net_cents"] == 118750  # pre-tax; tax 95.00 is not allocated
    after_first = _po(client, ws)
    assert after_first == (1, 76250, {"PIPE-34": "40", "CPL-12": "150"})

    second = _submit(client, ws, split_po_pdf(**SECOND))
    assert (second["decision"], second["reason_code"], second["allocation_id"]) == ("HOLD", "AMOUNT_EXCEEDS_PO", None)
    d = second["decision_detail"]
    assert d["reason_codes"] == ["AMOUNT_EXCEEDS_PO", "QUANTITY_EXCEEDS_PO"]
    breaches = {(b["measure"], b["scope"], b.get("sku")): b for b in d["authorization"]["breaches"]}
    assert set(breaches) == {("quantity", "line", "PIPE-34"), ("net", "line", "PIPE-34"), ("net", "po", None)}
    assert breaches[("quantity", "line", "PIPE-34")]["excess"] == "15"
    assert breaches[("net", "line", "PIPE-34")]["excess"] == 18750 and breaches[("net", "po", None)]["excess"] == 18750
    assert "PIPE-34 quantity 60 already allocated + 55 on this invoice = 115, 15 over the 100 authorized" in second["reason"]
    assert "USD 187.50 over" in second["reason"] and "CPL-12" not in second["reason"]
    assert _po(client, ws) == after_first  # ledger unchanged by the hold


def test_missing_quantity_in_right_aligned_table_holds(client):
    ws = _import(client)
    run = _submit(client, ws, split_po_pdf(lines=(("PIPE-34", "Steel pipe 3/4 in x 20 ft", None, "$12.50", "$750.00"),),
                                           subtotal="$750.00", tax="$60.00", total="$810.00"))
    assert (run["decision"], run["reason_code"], run["allocation_id"]) == ("HOLD", "MISSING_OR_UNCLEAR_FIELD", None)
    assert "quantity" in run["decision_detail"]["review_field"]
    assert _po(client, ws) == (0, 195000, {"PIPE-34": "100", "CPL-12": "400"})


def test_ambiguous_quantity_in_right_aligned_table_holds(client):
    ws = _import(client)
    run = _submit(client, ws, split_po_pdf(lines=(("PIPE-34", "Steel pipe", "60", "$12.50", "$750.00", (378, "6")),),
                                           subtotal="$750.00", tax="$60.00", total="$810.00"))
    assert (run["decision"], run["reason_code"], run["allocation_id"]) == ("HOLD", "MISSING_OR_UNCLEAR_FIELD", None)
    assert _po(client, ws)[0] == 0
