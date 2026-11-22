"""Server-rendered UI contract: page structure, workspace scoping, figures and assets. Synthetic data only."""
from __future__ import annotations

import re

from app.money import fmt_cents
from tests.conftest import wait_for
from tests.pdfgen import LINES_CSV, POS_CSV, VENDORS_CSV, invoice_items, invoice_pdf, make_pdf


def _import(client, **settings) -> str:
    r = client.post("/procurement/import", files={
        "vendors": ("vendors.csv", VENDORS_CSV), "purchase_orders": ("purchase_orders.csv", POS_CSV),
        "po_lines": ("po_lines.csv", LINES_CSV)}, data=settings)
    assert r.status_code == 201, r.text
    return r.json()["workspace_id"]


def _run(client, ws, pdf):
    r = client.post("/runs", files={"file": ("inv.pdf", pdf, "application/pdf")}, data={"workspace_id": ws})
    assert r.status_code == 202
    return wait_for(client, r.json()["run_id"])


def test_home_without_workspace_guides_import_and_disables_upload(client):
    page = client.get("/").text
    assert 'id="import-form"' in page and 'id="import-panel" open' in page
    for name in ("vendors", "purchase_orders", "po_lines", "date_order", "default_currency"):
        assert f'name="{name}"' in page
    assert 'name="workspace_id"' not in page  # nothing to bind an upload to yet
    assert re.search(r'<button type="submit" class="btn btn-primary" id="upload-btn"\s+disabled', page)
    assert 'id="metrics"' not in page  # no fabricated metrics before data exists


def test_home_binds_upload_and_metrics_to_the_newest_workspace_only(client):
    old = _import(client)
    ws = _import(client)
    page = client.get("/").text
    assert f'<input type="hidden" name="workspace_id" value="{ws}">' in page
    assert f'id="metrics" data-workspace="{ws}"' in page
    assert 'name="file" accept="application/pdf,.pdf"' in page and 'id="upload-form"' in page
    assert old not in page
    assert 'id="import-panel" open' not in page  # import collapses once data is loaded


def test_run_page_has_live_view_containers_and_plain_pdf_url(client):
    ws = _import(client)
    run = _run(client, ws, invoice_pdf())
    rid = run["run_id"]
    page = client.get(f"/ui/runs/{rid}").text
    for el in ("stages", "live-line", "status", "timing", "decision-card", "balance", "fields", "evidence-detail",
               "checks", "evidence", "lines", "events"):
        assert f'id="{el}"' in page, el
    assert f'initRun("{rid}")' in page
    assert f'src="/runs/{rid}/pdf#navpanes=0&amp;view=FitH"' in page  # same-origin, no credentials in the URL
    assert "@" not in re.search(r'<iframe[^>]*>', page).group(0)
    assert f'href="/ui/history?workspace_id={ws}"' in page
    assert client.get(f"/runs/{rid}/pdf").headers["content-type"] == "application/pdf"


def test_history_filters_offer_only_outcomes_that_exist(client):
    ws = _import(client)
    _run(client, ws, invoice_pdf(number="HCI-0101"))
    _run(client, ws, invoice_pdf(number="HCI-0202", po="PO-0000"))
    page = client.get("/ui/history", params={"workspace_id": ws}).text
    assert 'data-filter="READY"' in page and 'data-filter="HOLD"' in page
    assert 'data-filter="BLOCKED"' not in page and 'data-filter="FAILED"' not in page
    assert ">All 2</button>" in page
    assert page.count('class="row-link"') == 2
    assert 'id="run-search"' in page


def test_history_ledger_figures_match_the_ledger_api(client):
    ws = _import(client)
    _run(client, ws, invoice_pdf())
    page = client.get("/ui/history", params={"workspace_id": ws}).text
    led = client.get("/ledger", params={"workspace_id": ws}).json()
    for p in led["purchase_orders"]:
        for k in ("authorized_net_cents", "allocated_net_cents", "remaining_net_cents"):
            assert fmt_cents(p[k], p["currency"]) in page, (p["po_id"], k)
    # Totals are summed per currency in exact cents.
    usd = [p for p in led["purchase_orders"] if p["currency"] == "USD"]
    assert fmt_cents(sum(p["authorized_net_cents"] for p in usd), "USD") in page
    assert fmt_cents(sum(p["remaining_net_cents"] for p in usd), "USD") in page


def test_unresolved_currency_is_never_displayed_as_usd(client):
    ws = _import(client)
    pdf = make_pdf(invoice_items(number="HCI-0303", lines=(("CAL-STD", "Standard calibration", "1", "250.00", "250.00"),),
                                 subtotal="250.00", tax="0.00", total="250.00", currency_label="Amount due"))
    run = _run(client, ws, pdf)
    assert run["decision"] == "HOLD" and run["currency"] is None and run["gross_cents"] == 25000
    page = client.get("/ui/history", params={"workspace_id": ws}).text
    row = re.search(r'<tr class="row-link".*?</tr>', page, re.S).group(0)
    assert "250.00" in row and "currency unresolved" in row and "USD 250.00" not in row


def test_pages_use_only_local_assets(client):
    _import(client)
    for path in ("/", "/ui/history?workspace_id=" + client.get("/procurement").json()["workspace_id"]):
        page = client.get(path).text
        external = [u for u in re.findall(r'(?:src|href)="(https?:[^"]+)"', page)]
        assert external == [], external
    for asset in ("/static/app.js", "/static/style.css"):
        r = client.get(asset)
        assert r.status_code == 200 and "http://" not in r.text.replace("http://www.w3.org/2000/svg", "")
    js = client.get("/static/app.js").text
    for fn in ("function initShell", "function initHome", "function initRun", "function initHistory"):
        assert fn in js
