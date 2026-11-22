"""A printed currency is never replaced by the workspace default. Synthetic vendor, PO and invoices only.

Every scenario runs the full HTTP flow (import, upload, worker, finalize retry) and checks the ledger.
"""
from __future__ import annotations

import shutil

import pytest

from tests.conftest import wait_for
from tests.pdfgen import invoice_items, invoice_pdf, make_pdf, scan_pdf

VENDORS = b"vendor_id,legal_name,status\nV-KLG,Kestrel Lab Goods Ltd,APPROVED\n"
POS = b"po_id,vendor_id,issued_on,status,currency,description,authorized_net\nPO-6600,V-KLG,2026-02-01,OPEN,USD,,1000.00\n"
LINES = (b"po_id,line_id,sku,description,authorized_quantity,unit_price,authorized_net\n"
         b"PO-6600,1,KL-10,Sample vials,50,20.00,1000.00\n")


def items(before="", after="", label="Amount due", total=None):
    """One line, 10 x 20.00 = 200.00 against a matching USD PO. Only the currency marking varies."""
    m = lambda v: f"{before}{v}{after}"
    return invoice_items(supplier="Kestrel Lab Goods Ltd", number="KLG-101", po="PO-6600",
                         lines=(("KL-10", "Sample vials", "10", m("20.00"), m("200.00")),),
                         subtotal=m("200.00"), tax=m("0.00"), total=total or m("200.00"), currency_label=label)


def _import(client, **settings):
    r = client.post("/procurement/import", files={"vendors": ("v.csv", VENDORS), "purchase_orders": ("p.csv", POS),
                                                  "po_lines": ("l.csv", LINES)}, data=settings)
    assert r.status_code == 201, r.text
    return r.json()["workspace_id"]


def _submit(client, pdf, **settings):
    """Upload into a fresh workspace, retry finalize, and return (run, ledger before, ledger after retry)."""
    ws = _import(client, **settings)
    before = client.get("/ledger", params={"workspace_id": ws}).json()
    rid = client.post("/runs", files={"file": ("i.pdf", pdf, "application/pdf")}, data={"workspace_id": ws}).json()["run_id"]
    run = wait_for(client, rid)
    retry = client.post(f"/runs/{rid}/finalize").json()
    assert retry["already_finalized"] is True
    return run, before, client.get("/ledger", params={"workspace_id": ws}).json()


def _held_untouched(run, before, after):
    assert run["decision"] == "HOLD" and run["allocation_id"] is None
    assert after == before and after["allocation_count"] == 0


USD_DEFAULT = {"default_currency": "USD"}


@pytest.mark.parametrize("marking,code,printed", [
    ({"before": "£ "}, "GBP", "£"),                    # 1. GBP symbol as its own token
    ({"before": "£"}, "GBP", "£20.00"),                # GBP symbol fixed to the amount
    ({"before": "€ "}, "EUR", "€"),                    # 2. EUR symbol
    ({"after": " €"}, "EUR", "€"),                     # EUR symbol after the amount
    ({"label": "Amount due (£)"}, "GBP", "(£)"),       # symbol only in a label
])
def test_printed_foreign_symbol_is_never_read_as_the_usd_default(client, marking, code, printed):
    run, before, after = _submit(client, make_pdf(items(**marking)), **USD_DEFAULT)
    _held_untouched(run, before, after)
    assert run["reason_code"] == "UNSUPPORTED_CURRENCY" and f"Currency {code} is not supported" in run["reason"]
    cur = run["extraction"]["invoice"]["currency"]
    assert (cur["value"], cur["source"], cur["raw"]) == (code, None, printed)        # raw evidence kept, no default
    assert cur["evidence"]["page"] == 1
    assert any(i["currency"] == code for i in run["extraction"]["invoice"]["currency_indicators"])


@pytest.mark.parametrize("marking,code", [({"label": "Amount due (CHF)"}, "CHF"), ({"before": "JPY "}, "JPY")])
def test_unsupported_iso_code_holds_in_a_usd_default_workspace(client, marking, code):   # 3.
    run, before, after = _submit(client, make_pdf(items(**marking)), **USD_DEFAULT)
    _held_untouched(run, before, after)
    assert run["reason_code"] == "UNSUPPORTED_CURRENCY" and run["extraction"]["invoice"]["currency"]["value"] == code


@pytest.mark.parametrize("marking", [
    {"before": "£ ", "label": "Amount due (USD)"},     # ISO code with a conflicting symbol
    {"before": "$", "total": "€ 200.00"},              # dollar amounts with a euro total
    {"before": "€ ", "label": "Amount due (GBP)"},     # two foreign markers
])
def test_conflicting_currency_markers_hold_for_review(client, marking):                   # 4.
    for settings in (USD_DEFAULT, {}):
        run, before, after = _submit(client, make_pdf(items(**marking)), **settings)
        _held_untouched(run, before, after)
        cur = run["extraction"]["invoice"]["currency"]
        assert cur["value"] is None and cur["note"].startswith("conflicting currency indicators")
        assert "currency" in run["reason"] and run["decision_detail"]["review_field"] == "currency"
        if settings:
            assert "the workspace default USD is not applied" in cur["note"]


def test_unmarked_invoice_uses_the_configured_usd_default(client):                       # 5.
    run, before, after = _submit(client, make_pdf(items()), **USD_DEFAULT)
    assert (run["decision"], after["allocation_count"]) == ("READY", 1)
    cur = run["extraction"]["invoice"]["currency"]
    assert (cur["value"], cur["source"], cur["evidence"]) == ("USD", "workspace_config", None)
    assert run["extraction"]["invoice"]["currency_indicators"] == []


@pytest.mark.parametrize("marking", [{"before": "$"}, {"label": "Amount due (USD)"}, {"before": "US$"}])
def test_explicit_usd_still_works_with_and_without_the_default(client, marking):           # 6.
    for settings in (USD_DEFAULT, {}):
        run, _before, after = _submit(client, make_pdf(items(**marking)), **settings)
        assert (run["decision"], after["allocation_count"]) == ("READY", 1)
        assert run["extraction"]["invoice"]["currency"]["source"] is None


def test_existing_fixture_style_invoice_with_dollars_and_usd_label_is_unchanged(client):
    run, _before, after = _submit(client, invoice_pdf(supplier="Kestrel Lab Goods Ltd", number="KLG-102", po="PO-6600",
                                                       lines=(("KL-10", "Sample vials", "10", "$20.00", "$200.00"),),
                                                       subtotal="$200.00", tax="$0.00", total="$200.00"))
    assert (run["decision"], after["allocation_count"]) == ("READY", 1)
    assert run["extraction"]["invoice"]["currency"]["note"] == "'$' is consistent with USD"


def test_unconfigured_workspace_keeps_the_missing_currency_hold(client):                  # 7.
    run, before, after = _submit(client, make_pdf(items()))
    _held_untouched(run, before, after)
    assert run["reason_code"] in ("MISSING_OR_UNCLEAR_FIELD", "EXTRACTION_ISSUE")
    assert "currency: no currency code or symbol found" in run["extraction"]["invoice"]["issues"]


def test_finalize_retries_after_a_currency_hold_never_touch_the_ledger(client):           # 8.
    ws = _import(client, **USD_DEFAULT)
    before = client.get("/ledger", params={"workspace_id": ws}).json()
    rid = client.post("/runs", files={"file": ("i.pdf", make_pdf(items(before="£ ")), "application/pdf")},
                      data={"workspace_id": ws}).json()["run_id"]
    run = wait_for(client, rid)
    assert run["reason_code"] == "UNSUPPORTED_CURRENCY"
    for _ in range(3):
        r = client.post(f"/runs/{rid}/finalize").json()
        assert (r["already_finalized"], r["allocations_for_run"]) == (True, 0)
    assert client.get("/ledger", params={"workspace_id": ws}).json() == before
    assert client.get(f"/runs/{rid}").json()["decision"] == "HOLD"


ocr = pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")),
                         reason="needs Tesseract and Poppler (run in the Docker image)")


@ocr
def test_scanned_invoice_with_a_euro_symbol_is_not_allocated_as_usd(client):
    run, before, after = _submit(client, scan_pdf(items(before="€ ")), **USD_DEFAULT)
    _held_untouched(run, before, after)
    assert run["extraction"]["invoice"]["currency"]["source"] != "workspace_config"


@ocr
def test_scanned_invoice_with_dollars_is_still_ready(client):
    run, _before, after = _submit(client, scan_pdf(items(before="$")), **USD_DEFAULT)
    assert (run["decision"], after["allocation_count"]) == ("READY", 1)
    assert run["extraction"]["invoice"]["currency"]["value"] == "USD"
