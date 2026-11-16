"""Native-text extraction on synthetic invoices with a layout unlike the supplied fixtures."""
from __future__ import annotations

from app.extraction import extract
from tests.pdfgen import invoice_pdf


def _extract(tmp_path, **kw):
    p = tmp_path / "inv.pdf"
    p.write_bytes(invoice_pdf(**kw))
    return extract(p)


def test_native_invoice_fields_and_evidence(tmp_path):
    r = _extract(tmp_path)
    inv = r.invoice
    assert r.pages[0].method == "native" and r.model_calls == 0
    assert inv.supplier_name.value == "Harborline Calibration Inc"
    assert inv.invoice_number.value == "HCI-0042" and inv.invoice_number.evidence.label == "Invoice no."
    assert inv.invoice_date.value == "2026-03-04"
    assert inv.po_reference.value == "PO-7781"
    assert inv.currency.value == "USD"
    assert (inv.net.value, inv.tax.value, inv.gross.value) == (348000, 0, 348000)
    assert [(l.sku.value, l.quantity.value, l.unit_price.value, l.amount.value) for l in inv.lines] == [
        ("CAL-STD", "12", "250", 300000), ("CAL-TRV", "1", "480", 48000)]
    assert inv.issues == []
    ev = inv.gross.evidence
    assert ev.page == 1 and ev.method == "native" and ev.text == "$3,480.00" and ev.label.startswith("Amount due")


def test_terms_value_is_not_read_as_net(tmp_path):
    # "Terms  Net 45" must not be taken as a net total of 45.
    inv = _extract(tmp_path).invoice
    assert inv.net.value == 348000 and not any("net" in i for i in inv.issues)


def test_missing_po_and_tax_are_null_not_invented(tmp_path):
    inv = _extract(tmp_path, po=None, tax=None).invoice
    assert inv.po_reference.value is None and inv.po_reference.raw is None
    assert inv.tax.value is None


def test_unclear_decimal_separator_is_flagged(tmp_path):
    inv = _extract(tmp_path, total="$3.480,00").invoice
    assert inv.gross.value is None
    assert any(i.startswith("gross") for i in inv.issues)
