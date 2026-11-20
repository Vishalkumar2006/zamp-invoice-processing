"""Workspace date-order / default-currency settings and PO suggestions from line evidence (vendor part numbers).

All vendors, part numbers, POs and descriptions are synthetic.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import config
from app.db import db, init_db, workspace_settings
from app.extraction import extract
from app.extraction.parser import parse_date
from app.rules import match_product, product_terms
from tests.conftest import wait_for
from tests.pdfgen import Text, make_pdf
from tests.test_layout_variants import _right

VENDOR = "Norvale Hardware Supply"
LINES = (("NH-55A", "SCREW SOCKET CAP GR5 3/8-16 X 2 PLAIN", "400", "0.875", "350.00"),
         ("NH-77W", "WASHER LOCK SPLIT GR5 3/8 PLAIN", "400", "0.125", "50.00"))


def hw_pdf(*, date="03/04/2026", po=None, freight="0.00", total="424.00", currency_mark="", lines=LINES) -> bytes:
    """A terse distributor layout: "INVOICE <no>" heading, PART NO / DESCRIPTION / QTY / EA / EXT table,
    MERCH / TAX / FREIGHT / PAY THIS AMT totals, no currency marker unless one is given."""
    t = [Text(50, 45, VENDOR.upper(), 12, True), Text(420, 45, "INVOICE NH-7781", 12, True),
         Text(420, 62, f"DATE {date}", 9), Text(50, 62, "40 Quarry Rd, Elmton OH 44100", 9),
         Text(50, 113, "SOLD TO: DEMO BUYER CORP", 9)]
    if po:
        t.append(Text(420, 79, f"PO {po}", 9))
    t += [Text(53, 171, "PART NO", 9, True), Text(138, 171, "DESCRIPTION", 9, True), Text(388, 171, "QTY", 9, True),
          Text(443, 171, "EA", 9, True), Text(498, 171, "EXT", 9, True)]
    for y, (part, desc, qty, rate, ext) in zip((187, 203, 219), lines):
        t += [Text(53, y, part, 9), Text(138, y, desc, 9), _right(436, y, qty), _right(491, y, rate),
              _right(558, y, currency_mark + ext)]
    t += [Text(455, 241, "MERCH", 9), _right(562, 241, "400.00"), Text(439, 254, "TAX 6.0%", 9), _right(562, 254, "24.00"),
          Text(444, 267, "FREIGHT", 9), _right(562, 267, freight), Text(410, 280, "PAY THIS AMT", 9),
          _right(562, 280, currency_mark + total)]
    return make_pdf(t)


VENDORS = f"vendor_id,legal_name,status\nV-NHS,{VENDOR},APPROVED\n".encode()
PO_HEAD = "po_id,vendor_id,issued_on,status,currency,description,authorized_net\n"
LINE_HEAD = "po_id,line_id,sku,description,authorized_quantity,unit_price,authorized_net\n"
# The right PO: same products and specifications under the buyer's own SKUs.
FIT = ("PO-8101", [("1", "SC-516", "Grade 5 socket cap screw 3/8-16 x 2 in", "400", "0.875", "350.00"),
                   ("2", "LW-38", "Grade 5 split lock washer 3/8 in", "400", "0.125", "50.00")])
# Same total (400.00) and the same kinds of parts, but a different material and metric sizes.
EQUAL_TOTAL = ("PO-8102", [("1", "SC-M8", "Stainless socket cap screw M8 x 20", "500", "0.70", "350.00"),
                           ("2", "LW-M8", "Stainless split lock washer M8", "500", "0.10", "50.00")])
# Two lines that the screw fits equally well: no defensible one-to-one mapping.
AMBIGUOUS = ("PO-8103", [("1", "SC-516A", "Grade 5 socket cap screw 3/8-16 x 2 in", "400", "0.875", "350.00"),
                         ("2", "SC-516B", "Grade 5 socket cap screw 3/8-16 x 2 in", "400", "0.875", "350.00"),
                         ("3", "LW-38", "Grade 5 split lock washer 3/8 in", "400", "0.125", "50.00")])


def _csvs(*pos):
    p, l = PO_HEAD, LINE_HEAD
    for po_id, lines in pos:
        p += f"{po_id},V-NHS,2026-02-01,OPEN,USD,,{sum(float(x[5]) for x in lines):.2f}\n"
        l += "".join(f"{po_id},{a},{b},{c},{d},{e},{f}\n" for a, b, c, d, e, f in lines)
    return p.encode(), l.encode()


def _import(client, *pos, **settings):
    p, l = _csvs(*pos)
    r = client.post("/procurement/import", files={"vendors": ("vendors.csv", VENDORS), "purchase_orders": ("p.csv", p),
                                                  "po_lines": ("l.csv", l)}, data=settings)
    assert r.status_code == 201, r.text
    return r.json()["workspace_id"]


def _run(client, ws, pdf):
    rid = client.post("/runs", files={"file": ("i.pdf", pdf, "application/pdf")}, data={"workspace_id": ws}).json()["run_id"]
    return wait_for(client, rid)


def _ledger(client, ws):
    return client.get("/ledger", params={"workspace_id": ws}).json()


# --------------------------------------------------------------------------- date order and default currency

def test_parse_date_uses_a_configured_order_and_never_reinterprets():
    assert parse_date("03/04/2026") == (None, "ambiguous numeric date (day/month order unclear)")
    assert parse_date("03/04/2026", "MDY") == ("2026-03-04", None)
    assert parse_date("03/04/2026", "DMY") == ("2026-04-03", None)
    assert parse_date("13/04/2026", "MDY") == (None, "not a valid date in the workspace date order MDY")
    assert parse_date("13/04/2026") == ("2026-04-13", None)                 # unconfigured: unchanged behaviour
    assert parse_date("2026-03-04", "DMY") == ("2026-03-04", None)          # ISO dates ignore the setting


def _extract(tmp_path, pdf, settings=None):
    p = tmp_path / "i.pdf"
    p.write_bytes(pdf)
    return extract(p, settings).invoice


def test_extraction_reads_the_distributor_layout(tmp_path):
    inv = _extract(tmp_path, hw_pdf(), {"date_order": "MDY", "default_currency": "USD"})
    assert inv.issues == []
    assert (inv.invoice_number.value, inv.net.value, inv.net.evidence.label, inv.tax.value, inv.gross.value,
            inv.gross.evidence.label, inv.freight.value) == ("NH-7781", 40000, "MERCH", 2400, 42400, "PAY THIS AMT", 0)
    assert [(l.sku.value, l.quantity.value, l.unit_price.raw, l.unit_price.value, l.amount.value) for l in inv.lines] == [
        ("NH-55A", "400", "0.875", "0.875", 35000), ("NH-77W", "400", "0.125", "0.125", 5000)]
    assert inv.lines[0].unit_price.evidence.label == "unit_price" and inv.lines[0].description.value == LINES[0][1]


def test_unconfigured_workspace_keeps_both_holds_in_extraction(tmp_path):
    inv = _extract(tmp_path, hw_pdf())
    assert inv.invoice_date.raw == "03/04/2026" and inv.invoice_date.value is None
    assert any(i.startswith("invoice_date: could not parse") for i in inv.issues)
    assert "currency: no currency code or symbol found" in inv.issues


def test_configured_settings_are_recorded_with_their_source(tmp_path):
    inv = _extract(tmp_path, hw_pdf(), {"date_order": "DMY", "default_currency": "USD"})
    d, c = inv.invoice_date, inv.currency
    assert (d.raw, d.value, d.source) == ("03/04/2026", "2026-04-03", "workspace_config") and "DMY" in d.note
    assert (c.value, c.source, c.evidence) == ("USD", "workspace_config", None) and "workspace configuration" in c.note


def test_a_printed_currency_takes_precedence_over_the_workspace_default(tmp_path):
    inv = _extract(tmp_path, hw_pdf(currency_mark="$"), {"default_currency": "CAD"})
    assert (inv.currency.value, inv.currency.source) == ("USD", None)
    assert "takes precedence over the workspace default CAD" in inv.currency.note


def test_workspace_settings_are_validated_stored_and_exposed(client):
    ws = _import(client, FIT, date_order="mdy", default_currency="usd")
    assert client.get("/procurement", params={"workspace_id": ws}).json()["settings"] == {
        "date_order": "MDY", "default_currency": "USD"}
    plain = _import(client, FIT)
    assert client.get("/procurement", params={"workspace_id": plain}).json()["settings"] == {}
    p, l = _csvs(FIT)
    r = client.post("/procurement/import", files={"vendors": ("v.csv", VENDORS), "purchase_orders": ("p.csv", p),
                                                  "po_lines": ("l.csv", l)}, data={"date_order": "YMD"})
    assert r.status_code == 422 and "date_order must be one of MDY, DMY" in r.json()["errors"]


def test_older_databases_gain_the_settings_columns(data_dir):
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source_files_json TEXT NOT NULL)")
    conn.execute("INSERT INTO workspaces VALUES ('ws_old', '2026-01-01T00:00:00', '{}')")
    conn.commit()
    conn.close()
    init_db()
    with db() as c:
        assert workspace_settings(c, "ws_old") == {}


def test_unconfigured_http_upload_holds_on_the_ambiguous_date(client):
    ws = _import(client, FIT)
    run = _run(client, ws, hw_pdf())
    assert (run["decision"], run["reason_code"], run["allocation_id"]) == ("HOLD", "MISSING_OR_UNCLEAR_FIELD", None)
    assert "invoice_date" in run["reason"]


# --------------------------------------------------------------------------- line evidence for a missing PO

def test_product_terms_normalise_abbreviations_units_and_plurals():
    assert product_terms("SCREW SOCKET CAP GR5 3/8-16 X 2 PLAIN") == (
        {"screw", "socket", "cap", "grade", "plain"}, {"5", "3/8-16", "2"})
    assert product_terms('Grade 5 socket cap screws 3/8-16 x 2"') == ({"grade", "socket", "cap", "screw"}, {"5", "3/8-16", "2"})
    assert product_terms("SS hex nut M8") == ({"stainless", "hex", "nut"}, {"m8"})


def test_suggestion_from_line_evidence_with_an_equal_total_alternative(client):
    ws = _import(client, FIT, EQUAL_TOTAL, date_order="MDY", default_currency="USD")
    before = _ledger(client, ws)
    run = _run(client, ws, hw_pdf())
    d = run["decision_detail"]
    assert (run["decision"], run["reason_code"], run["po_id"], run["allocation_id"]) == ("HOLD", "PO_MISSING", None, None)
    assert d["candidate_po_ids"] == ["PO-8101"] and d["allocation"] is None
    assert "pending PO confirmation" in run["reason"] and "never assigned automatically" in run["reason"]
    pc = d["po_candidates"]
    assert pc["viable_po_ids"] == ["PO-8101"] and pc["equal_total_ineligible_po_ids"] == ["PO-8102"]
    fit = next(c for c in pc["candidates"] if c["po_id"] == "PO-8101")
    # Printed part numbers stay as printed; the inferred PO line is recorded beside them.
    assert [(r["sku"], r["match_method"], r["po_sku"], r["shared_specs"]) for r in fit["lines"]] == [
        ("NH-55A", "product_specs", "SC-516", ["2", "3/8-16", "5"]), ("NH-77W", "product_specs", "LW-38", ["3/8", "5"])]
    assert all(r["price_ok"] and r["quantity_ok"] and r["amount_ok"] for r in fit["lines"])
    alt = next(c for c in pc["candidates"] if c["po_id"] == "PO-8102")
    assert not alt["viable"] and not any(r["mapped"] for r in alt["lines"])
    assert "PO-8102 has the same total (USD 400.00) but is not eligible" in run["reason"]
    assert "the invoice does not name stainless" in run["reason"] and "PO specification 20, m8 not on the invoice" in run["reason"]
    assert "NH-55A as SC-516" in d["next_action"]
    assert client.post(f"/runs/{run['run_id']}/finalize").json()["allocations_for_run"] == 0
    assert _ledger(client, ws) == before


def test_an_equal_total_alone_is_never_a_suggestion(client):
    ws = _import(client, EQUAL_TOTAL, date_order="MDY", default_currency="USD")
    run = _run(client, ws, hw_pdf())
    assert (run["reason_code"], run["decision_detail"]["candidate_po_ids"]) == ("PO_MISSING_NO_MATCH", [])
    assert run["decision_detail"]["po_candidates"]["equal_total_ineligible_po_ids"] == ["PO-8102"]


def test_ambiguous_line_evidence_is_surfaced_not_resolved(client):
    ws = _import(client, AMBIGUOUS, date_order="MDY", default_currency="USD")
    run = _run(client, ws, hw_pdf())
    assert (run["reason_code"], run["allocation_id"]) == ("PO_MISSING_NO_MATCH", None)
    cand = run["decision_detail"]["po_candidates"]["candidates"][0]
    assert any(f.startswith("ambiguous line evidence") and "SC-516A, SC-516B" in f for f in cand["failures"])
    assert cand["lines"][0]["product_options"] == ["1", "2"] and not cand["lines"][0]["mapped"]


def test_two_suggestions_from_line_evidence_are_ambiguous(client):
    twin = ("PO-8104", FIT[1])
    ws = _import(client, FIT, twin, date_order="MDY", default_currency="USD")
    run = _run(client, ws, hw_pdf())
    assert (run["reason_code"], run["decision_detail"]["candidate_po_ids"]) == ("PO_AMBIGUOUS", ["PO-8101", "PO-8104"])


def test_a_price_outside_tolerance_still_rules_out_a_product_match(client):
    dear = ("PO-8105", [FIT[1][0][:4] + ("0.80", "320.00"), FIT[1][1]])
    ws = _import(client, dear, date_order="MDY", default_currency="USD")
    run = _run(client, ws, hw_pdf())
    cand = run["decision_detail"]["po_candidates"]["candidates"][0]
    assert run["reason_code"] == "PO_MISSING_NO_MATCH" and cand["lines"][0]["mapped"] and not cand["lines"][0]["price_ok"]
    assert any("NH-55A (as SC-516) price 0.875 vs 0.8 (over 1%)" in f for f in cand["failures"])


def test_a_printed_po_keeps_ordinary_validation(client):
    ws = _import(client, FIT, date_order="MDY", default_currency="USD")
    run = _run(client, ws, hw_pdf(po="PO-8101"))
    assert (run["decision"], run["reason_code"], run["allocation_id"]) == ("HOLD", "LINE_NOT_ON_PO", None)


# --------------------------------------------------------------------------- freight

def test_zero_freight_is_verified_and_a_nonzero_charge_holds(client):
    ws = _import(client, FIT, date_order="MDY", default_currency="USD")
    ok = _run(client, ws, hw_pdf())
    assert any(c["detail"] == "freight printed as USD 0.00" and c["status"] == "pass" for c in ok["decision_detail"]["checks"])
    charged = _run(client, ws, hw_pdf(freight="25.00", total="449.00"))
    assert (charged["decision"], charged["reason_code"], charged["allocation_id"]) == ("HOLD", "TOTALS_MISMATCH", None)
    assert "freight of USD 25.00" in charged["reason"] and charged["decision_detail"]["review_field"] == "freight"
    assert _ledger(client, ws)["allocation_count"] == 0


def test_product_matching_requires_identity_words_and_specifications():
    po = [{"line_id": "1", "sku": "X", "description": "Socket cap screw"},
          {"line_id": "2", "sku": "Y", "description": "Grade 5 socket cap screw 3/8-16 x 2 in"}]
    res = match_product("SCREW SOCKET CAP GR5 3/8-16 X 2", po)
    assert [r["qualifies"] for r in res] == [False, True]
    assert res[0]["problems"] == ["no specification on the PO line to confirm the product"]
    assert match_product("NON-GRADE SCREW SOCKET CAP 3/8-16 X 2 5", po[1:])[0]["qualifies"] is False
