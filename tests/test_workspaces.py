"""Workspace routing for uploads and workspace-scoped history (stale browser tab regression)."""
from __future__ import annotations

import re

from tests.conftest import wait_for
from tests.pdfgen import LINES_CSV, POS_CSV, VENDORS_CSV, invoice_pdf


def _import(client) -> str:
    r = client.post("/procurement/import", files={
        "vendors": ("vendors.csv", VENDORS_CSV), "purchase_orders": ("purchase_orders.csv", POS_CSV),
        "po_lines": ("po_lines.csv", LINES_CSV)})
    assert r.status_code == 201
    return r.json()["workspace_id"]


def _upload(client, pdf: bytes, workspace_id=None):
    data = {"workspace_id": workspace_id} if workspace_id is not None else {}
    return client.post("/runs", files={"file": ("inv.pdf", pdf, "application/pdf")}, data=data)


def _ledger(client, ws):
    led = client.get("/ledger", params={"workspace_id": ws}).json()
    return led["allocation_count"], next(p for p in led["purchase_orders"] if p["po_id"] == "PO-7781")["remaining_net_cents"]


def test_stale_page_upload_goes_to_the_workspace_it_names(client):
    """Page loaded on A, B imported elsewhere, then the A page uploads: the run and allocation belong to A."""
    ws_a = _import(client)
    page_a = client.get("/").text
    ws_b = _import(client)
    assert ws_a != ws_b and client.get("/procurement").json()["workspace_id"] == ws_b
    field = re.search(r'<input type="hidden" name="workspace_id" value="([^"]+)">', page_a)
    assert field and field.group(1) == ws_a  # the browser form carries the workspace it shows
    r = _upload(client, invoice_pdf(), workspace_id=field.group(1))
    assert r.status_code == 202 and r.json()["workspace_id"] == ws_a
    run = wait_for(client, r.json()["run_id"])
    assert run["workspace_id"] == ws_a and run["decision"] == "READY" and run["allocation_id"] is not None
    assert _ledger(client, ws_a) == (1, 348000)
    assert _ledger(client, ws_b) == (0, 696000)
    assert [x["run_id"] for x in client.get("/runs", params={"workspace_id": ws_a}).json()["runs"]] == [run["run_id"]]
    assert client.get("/runs", params={"workspace_id": ws_b}).json()["runs"] == []


def test_api_upload_without_workspace_uses_the_latest(client):
    _import(client)
    ws_b = _import(client)
    r = _upload(client, invoice_pdf())
    assert r.status_code == 202 and r.json()["workspace_id"] == ws_b
    assert wait_for(client, r.json()["run_id"])["workspace_id"] == ws_b
    assert _ledger(client, ws_b)[0] == 1


def test_unknown_workspace_is_rejected_and_nothing_is_written(client, data_dir):
    ws_a = _import(client)
    ws_b = _import(client)
    r = _upload(client, invoice_pdf(), workspace_id="ws_doesnotexist")
    assert r.status_code == 422
    assert "ws_doesnotexist does not exist" in r.json()["detail"] and "Nothing was uploaded" in r.json()["detail"]
    assert client.get("/runs").json()["runs"] == []
    assert _ledger(client, ws_a) == (0, 696000) and _ledger(client, ws_b) == (0, 696000)
    assert not list((data_dir / "uploads").glob("*.pdf"))


def test_no_workspace_at_all_still_asks_for_an_import(client):
    r = _upload(client, invoice_pdf())
    assert r.status_code == 409 and "import procurement" in r.json()["detail"]
    assert client.get("/ui/history").status_code == 200  # empty history renders without a workspace


def test_history_is_scoped_to_the_selected_workspace(client):
    ws_a = _import(client)
    run_a = wait_for(client, _upload(client, invoice_pdf(number="HCI-0101"), ws_a).json()["run_id"])
    ws_b = _import(client)
    held = wait_for(client, _upload(client, invoice_pdf(number="HCI-0202", po="PO-0000"), ws_b).json()["run_id"])
    ok_b = wait_for(client, _upload(client, invoice_pdf(number="HCI-0303"), ws_b).json()["run_id"])
    assert (run_a["decision"], held["decision"], ok_b["decision"]) == ("READY", "HOLD", "READY")

    # Default: redirect to the latest workspace, pinned in the URL so a refresh keeps it.
    r = client.get("/ui/history", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/ui/history?workspace_id={ws_b}"

    page_a = client.get("/ui/history", params={"workspace_id": ws_a}).text
    page_b = client.get("/ui/history", params={"workspace_id": ws_b}).text
    for page, ws in ((page_a, ws_a), (page_b, ws_b)):
        assert f'<code id="current-ws">{ws}</code>' in page
        assert re.search(rf'<option value="{ws}" selected>', page)
        assert f"PO ledger <span class=\"muted\">(workspace {ws})" in page
    assert run_a["run_id"] in page_a and held["run_id"] not in page_a and ok_b["run_id"] not in page_a
    assert held["run_id"] in page_b and ok_b["run_id"] in page_b and run_a["run_id"] not in page_b
    assert ">READY 1</span>" in page_a and ">HOLD" not in page_a
    assert ">READY 1</span>" in page_b and ">HOLD 1</span>" in page_b
    assert "USD 3,480.00" in page_a and "USD 3,480.00" in page_b  # each ledger reflects its own single allocation
    assert _ledger(client, ws_a) == (1, 348000) and _ledger(client, ws_b) == (1, 348000)

    assert client.get("/ui/history", params={"workspace_id": "ws_doesnotexist"}).status_code == 404


def test_run_page_links_to_its_own_workspace_history(client):
    ws_a = _import(client)
    run_id = _upload(client, invoice_pdf(), ws_a).json()["run_id"]
    _import(client)  # a newer workspace must not change where the run page points
    wait_for(client, run_id)
    page = client.get(f"/ui/runs/{run_id}").text
    assert f'href="/ui/history?workspace_id={ws_a}"' in page
    home = client.get("/").text  # the home page is now bound to the newer workspace and says so
    assert "Uploads from this page go to workspace" in home and ws_a not in home
