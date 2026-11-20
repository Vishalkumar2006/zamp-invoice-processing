"""Remittance safeguard: trusted bank data in the vendor master, printed payment instructions, change notices.

All names, bank names and numbers are synthetic. Identifiers have leading zeros on purpose.
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest

from app import config
from app.db import db, init_db
from app.extraction import extract
from app.models import Evidence, Field_
from app.remittance import compare_identifier, mask, redact_text
from tests.conftest import wait_for
from tests.pdfgen import Text, make_pdf
from tests.test_layout_variants import _right
from tests.test_rules import f, invoice, line, snapshot
from app.rules import evaluate

VENDOR = "Quillon Lab Supplies Co."
ON_FILE = {"bank": "Riverbend Savings Bank", "routing": "000111222", "account": "000123456789", "phone": "(555) 010-2233"}
NEW = {"bank": "Coastline Trust Bank", "routing": "000555666", "account": "000987650001"}
FULL_IDS = (ON_FILE["routing"], ON_FILE["account"], NEW["routing"], NEW["account"])

FOOTERS = {
    "changed": [(600, "NOTICE: UPDATED REMITTANCE INFORMATION", 10),
                (616, "From today, kindly send payments to the account below:", 9),
                (630, f"{NEW['bank']} · ACH Routing {NEW['routing']} · Account {NEW['account']}", 9),
                (644, "Queries about this change: quillon.payments@freemail.example", 9)],
    "matching": [(630, f"Remit to: {ON_FILE['bank']} · ACH routing {ON_FILE['routing']} · acct ****6789", 9)],
    "notice_only": [(600, "Our banking details have changed. New remittance details will follow by post.", 9)],
    "none": [],
}


def remit_pdf(footer="matching") -> bytes:
    """Same financial content for every footer: 2 lines, subtotal 3,130.00, tax 250.40, total 3,380.40."""
    t = [Text(196, 40, VENDOR.upper(), 14, True), Text(110, 58, "7 Beacon Way · Dover, OH 44000 · billing@quillon.example", 9),
         Text(46, 92, "INVOICE QL-5512", 12, True), Text(320, 92, "Sold to:", 9), Text(320, 106, "Demo Buyer Corp", 9),
         Text(46, 112, "Date: 2026-07-14", 9), Text(46, 126, "Your PO: PO-7700", 9), Text(46, 140, "Terms: Net 30", 9),
         Text(48, 200, "Ln", 9, True), Text(72, 200, "Part", 9, True), Text(136, 200, "Description", 9, True),
         Text(350, 200, "Qty", 9, True), Text(404, 200, "Price", 9, True), Text(476, 200, "Ext.", 9, True)]
    for y, (ln, part, desc, qty, price, ext) in zip((216, 232), (
            ("1", "QL-200", "Nitrile bench mats 24x36", "500", "$3.20", "$1,600.00"),
            ("2", "QL-310", "Pipette tips 200uL, box of 96", "120", "$12.75", "$1,530.00"))):
        t += [Text(48, y, ln, 9), Text(72, y, part, 9), Text(136, y, desc, 9),
              _right(398, y, qty), _right(468, y, price), _right(548, y, ext)]
    t += [Text(380, 260, "Merchandise", 10), _right(552, 260, "$3,130.00", 10),
          Text(380, 275, "Sales tax 8%", 10), _right(552, 275, "$250.40", 10),
          Text(380, 290, "Invoice total", 10), _right(552, 290, "$3,380.40", 10)]
    t += [Text(56, y, text, size) for y, text, size in FOOTERS[footer]]
    return make_pdf(t)


def vendors_csv(bank=True, phone=True, phone_header="verification_phone") -> bytes:
    if not bank:
        return f"vendor_id,legal_name,status\nV-QLS,{VENDOR},APPROVED\n".encode()
    return (f"vendor_id,legal_name,status,bank_name,routing_number,account_number,{phone_header}\n"
            f"V-QLS,{VENDOR},APPROVED,{ON_FILE['bank']},{ON_FILE['routing']},{ON_FILE['account']},"
            f"{ON_FILE['phone'] if phone else ''}\n").encode()


POS = b"po_id,vendor_id,issued_on,status,currency,description,authorized_net\nPO-7700,V-QLS,2026-06-01,OPEN,USD,,3130.00\n"
PO_LINES = (b"po_id,line_id,sku,description,authorized_quantity,unit_price,authorized_net\n"
            b"PO-7700,1,QL-200,Nitrile bench mats 24x36,500,3.20,1600.00\n"
            b"PO-7700,2,QL-310,Pipette tips 200uL box of 96,120,12.75,1530.00\n")


def _import(client, vendors=None):
    r = client.post("/procurement/import", files={"vendors": ("vendors.csv", vendors or vendors_csv()),
                                                  "purchase_orders": ("purchase_orders.csv", POS),
                                                  "po_lines": ("po_lines.csv", PO_LINES)})
    assert r.status_code == 201, r.text
    return r.json()["workspace_id"]


def _submit(client, ws, footer):
    rid = client.post("/runs", files={"file": ("i.pdf", remit_pdf(footer), "application/pdf")},
                      data={"workspace_id": ws}).json()["run_id"]
    return wait_for(client, rid)


def _vendor_rows(ws):
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM vendors WHERE workspace_id=? ORDER BY vendor_id", (ws,))]


def _ledger(client, ws):
    led = client.get("/ledger", params={"workspace_id": ws}).json()
    return led["allocation_count"], led["purchase_orders"][0]["remaining_net_cents"]


# --------------------------------------------------------------------------- helpers

def test_masking_and_identifier_comparison():
    assert mask("000123456789") == "****6789" and mask("****7734") == "****7734"
    assert redact_text("Account 000987650001 · Routing 000555666") == "Account ****0001 · Routing ****5666"
    assert compare_identifier("000123456789", "000123456789") == "match"
    assert compare_identifier("123456789", "000123456789") == "mismatch"  # leading zeros are significant
    assert compare_identifier("****6789", "000123456789") == "masked_match"
    assert compare_identifier("****0001", "000123456789") == "mismatch"
    assert compare_identifier("**89", "000123456789") == "inconclusive"
    assert compare_identifier("000123456789", None) == "not_on_file"


# --------------------------------------------------------------------------- vendor master import

def test_vendor_master_keeps_bank_identifiers_as_strings_and_masks_them_in_the_api(client):
    ws = _import(client)
    row = _vendor_rows(ws)[0]
    assert (row["bank_name"], row["routing_number"], row["account_number"], row["verification_phone"]) == (
        ON_FILE["bank"], "000111222", "000123456789", ON_FILE["phone"])
    api = client.get("/procurement", params={"workspace_id": ws}).json()["vendors"][0]
    assert (api["routing_number"], api["account_number"], api["bank_name"]) == ("****1222", "****6789", ON_FILE["bank"])
    assert not any(i in json.dumps(api) for i in FULL_IDS)


def test_phone_on_file_header_is_accepted_and_legacy_csvs_still_import(client):
    ws = _import(client, vendors_csv(phone_header="phone_on_file"))
    assert _vendor_rows(ws)[0]["verification_phone"] == ON_FILE["phone"]
    legacy = _import(client, vendors_csv(bank=False))
    row = _vendor_rows(legacy)[0]
    assert (row["bank_name"], row["routing_number"], row["account_number"], row["verification_phone"]) == (None,) * 4


def test_malformed_bank_identifiers_are_rejected(client):
    bad = vendors_csv().replace(b"000111222", b"11122")
    r = client.post("/procurement/import", files={"vendors": ("v.csv", bad), "purchase_orders": ("p.csv", POS),
                                                  "po_lines": ("l.csv", PO_LINES)})
    assert r.status_code == 422 and any("routing_number must be 9 digits" in e for e in r.json()["errors"])


def test_existing_database_gains_the_new_vendor_columns(data_dir):
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("CREATE TABLE vendors (workspace_id TEXT NOT NULL, vendor_id TEXT NOT NULL, legal_name TEXT NOT NULL,"
                 " legal_name_norm TEXT NOT NULL, status TEXT NOT NULL, source_file TEXT NOT NULL,"
                 " source_row INTEGER NOT NULL, PRIMARY KEY (workspace_id, vendor_id))")
    conn.execute("INSERT INTO vendors VALUES ('ws_old','V1','Old Vendor Co','old vendor co','APPROVED','vendors.csv',2)")
    conn.commit()
    conn.close()
    init_db()
    conn = sqlite3.connect(config.DB_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(vendors)")]
    row = conn.execute("SELECT vendor_id, bank_name, routing_number, account_number, verification_phone FROM vendors").fetchone()
    conn.close()
    assert {"bank_name", "routing_number", "account_number", "verification_phone"} <= set(cols)
    assert row == ("V1", None, None, None, None)


# --------------------------------------------------------------------------- extraction

def test_case_style_layout_and_payment_instructions_are_extracted(tmp_path):
    p = tmp_path / "i.pdf"
    p.write_bytes(remit_pdf("changed"))
    inv = extract(p).invoice
    assert inv.issues == []
    assert (inv.invoice_number.value, inv.invoice_number.evidence.label) == ("QL-5512", "INVOICE")
    assert (inv.invoice_date.value, inv.po_reference.value) == ("2026-07-14", "PO-7700")
    assert (inv.net.value, inv.net.evidence.label, inv.tax.value, inv.gross.value) == (313000, "Merchandise", 25040, 338040)
    assert [(l.sku.value, l.description.value, l.quantity.value, l.unit_price.value, l.amount.value) for l in inv.lines] == [
        ("QL-200", "Nitrile bench mats 24x36", "500", "3.2", 160000),       # "Ln" column dropped, not merged
        ("QL-310", "Pipette tips 200uL, box of 96", "120", "12.75", 153000)]
    assert (inv.remit_bank_name.value, inv.remit_routing.value, inv.remit_account.value) == (
        NEW["bank"], NEW["routing"], NEW["account"])
    assert inv.remit_account.evidence.page == 1 and inv.remit_account.evidence.label == "account"
    assert inv.remittance_notice.value.lower() == "updated remittance"


def test_ordinary_instruction_keeps_the_masked_account_as_printed(tmp_path):
    p = tmp_path / "i.pdf"
    p.write_bytes(remit_pdf("matching"))
    inv = extract(p).invoice
    assert (inv.remit_bank_name.value, inv.remit_routing.value, inv.remit_account.value) == (
        ON_FILE["bank"], ON_FILE["routing"], "****6789")
    assert inv.remittance_notice.value is None


# --------------------------------------------------------------------------- paired regression (same financial data)

def test_changed_remittance_holds_and_matching_remittance_is_ready(client):
    ws = _import(client)
    vendors_before = _vendor_rows(ws)
    changed = _submit(client, ws, "changed")
    assert (changed["decision"], changed["reason_code"], changed["allocation_id"]) == ("HOLD", "REMITTANCE_MISMATCH", None)
    assert _ledger(client, ws) == (0, 313000)
    rem = changed["decision_detail"]["remittance"]
    assert [(r["label"], r["invoice"], r["on_file"], r["status"]) for r in rem["comparison"]] == [
        ("bank", NEW["bank"], ON_FILE["bank"], "mismatch"), ("routing number", "****5666", "****1222", "mismatch"),
        ("account number", "****0001", "****6789", "mismatch")]
    assert all(r["on_file_source"] == "vendors.csv row 2" for r in rem["comparison"])
    assert rem["verification_contact"] == {"phone": ON_FILE["phone"], "source": "vendors.csv row 2 (verification_phone)"}
    assert ON_FILE["phone"] in changed["decision_detail"]["next_action"]
    assert "freemail" not in changed["decision_detail"]["next_action"]   # never the contact printed on the invoice
    assert "account number ****0001 on the invoice vs ****6789 on file" in changed["reason"]
    assert not any(i in json.dumps(changed) for i in FULL_IDS)           # whole API run detail, extraction included
    assert _vendor_rows(ws) == vendors_before                             # uploads never change the vendor master

    ws2 = _import(client)
    matching = _submit(client, ws2, "matching")
    assert (matching["decision"], matching["reason_code"]) == ("READY", "MATCHED_WITHIN_AUTHORIZATION")
    assert matching["decision_detail"]["allocation"]["net_cents"] == 313000 and _ledger(client, ws2) == (1, 0)
    statuses = {r["field"]: r["status"] for r in matching["decision_detail"]["remittance"]["comparison"]}
    assert statuses == {"bank_name": "match", "routing": "match", "account": "masked_match"}


def test_change_notice_without_verifiable_details_goes_to_review(client):
    ws = _import(client)
    run = _submit(client, ws, "notice_only")
    assert (run["decision"], run["reason_code"], run["allocation_id"]) == ("HOLD", "REMITTANCE_CHANGE_UNVERIFIED", None)
    assert "cannot be verified" in run["reason"] and ON_FILE["phone"] in run["decision_detail"]["next_action"]
    assert _ledger(client, ws) == (0, 313000)


def test_without_a_verification_phone_the_reviewer_goes_to_procurement(client):
    ws = _import(client, vendors_csv(phone=False))
    run = _submit(client, ws, "changed")
    assert run["reason_code"] == "REMITTANCE_MISMATCH" and run["decision_detail"]["remittance"]["verification_contact"] is None
    assert "ask procurement for a trusted contact" in run["decision_detail"]["next_action"]


def test_no_payment_instructions_and_unknown_bank_data_keep_the_normal_workflow(client):
    ws = _import(client)
    assert _submit(client, ws, "none")["decision"] == "READY"
    legacy = _import(client, vendors_csv(bank=False))
    run = _submit(client, legacy, "matching")
    assert run["decision"] == "READY"
    assert {r["status"] for r in run["decision_detail"]["remittance"]["comparison"]} == {"not_on_file"}


# --------------------------------------------------------------------------- rules-level edge cases

def _vendor_snapshot(**bank):
    s = snapshot()
    s.vendors[0].update({"bank_name": ON_FILE["bank"], "routing_number": ON_FILE["routing"],
                         "account_number": ON_FILE["account"], "verification_phone": ON_FILE["phone"], **bank})
    return s


def _with_remit(account, routing=None, notice=False, conf=95.0):
    inv = invoice()
    inv.remit_account = Field_(raw=account, value=account,
                               evidence=Evidence(page=1, method="ocr", text=account, bbox=(0, 0, 1, 1), conf=conf, label="account"))
    if routing:
        inv.remit_routing = f(routing)
    if notice:
        inv.remittance_notice = f("updated remittance", raw="Updated remittance details")
    return inv


def test_change_notice_whose_details_already_match_the_vendor_master_proceeds():
    d = evaluate(_with_remit(ON_FILE["account"], ON_FILE["routing"], notice=True), _vendor_snapshot(), min_conf=80, policy_version="t")
    assert d.decision == "READY" and d.remittance["outcome"] == "notice_verified"


def test_change_notice_with_only_a_masked_account_is_not_enough():
    d = evaluate(_with_remit("****6789", notice=True), _vendor_snapshot(), min_conf=80, policy_version="t")
    assert (d.decision, d.reason_code) == ("HOLD", "REMITTANCE_CHANGE_UNVERIFIED")


def test_unclear_read_without_a_notice_is_recorded_but_not_decisive():
    d = evaluate(_with_remit("000987650001", conf=60.0), _vendor_snapshot(), min_conf=80, policy_version="t")
    assert d.decision == "READY"
    assert {r["field"]: r["status"] for r in d.remittance["comparison"]}["account"] == "low_confidence"


def test_verified_mismatch_without_a_notice_still_holds():
    d = evaluate(_with_remit("000987650001"), _vendor_snapshot(), min_conf=80, policy_version="t")
    assert (d.decision, d.reason_code, d.allocation) == ("HOLD", "REMITTANCE_MISMATCH", None)
    assert "000987650001" not in d.reason and "****0001" in d.reason


# --------------------------------------------------------------------------- conflicting payment instructions

def _grouped(n, sep):
    return sep.join(n[i:i + 4] for i in range(0, len(n), 4))


FOOTERS.update({
    # Two different, clearly printed accounts and no change notice (the account on file plus another one).
    "two_accounts": [(616, f"Remit to: {ON_FILE['bank']} · ACH routing {ON_FILE['routing']} · Account {ON_FILE['account']}", 9),
                     (632, f"Wire transfers: Account {NEW['account']}", 9)],
    # The account on file printed three ways: contiguous, hyphen-grouped and masked. One identifier, not a conflict.
    "same_account_reformatted": [
        (616, f"Remit to: {ON_FILE['bank']} · ACH routing {ON_FILE['routing']} · Account {ON_FILE['account']}", 9),
        (632, f"Wire transfers: Account {_grouped(ON_FILE['account'], '-')}", 9),
        (648, "Statement reference: acct ****6789", 9)],
})
for _name, _sep in (("contiguous", ""), ("hyphenated", "-"), ("spaced", " ")):
    _r = NEW["routing"]
    FOOTERS[f"changed_{_name}"] = [
        (630, f"{NEW['bank']} · ACH Routing {_r[:3] + _sep + _r[3:6] + _sep + _r[6:]}"
              f" · Account {_grouped(NEW['account'], _sep)}", 9)]


def _leaks(obj) -> list[str]:
    """Full identifiers present in any grouping (contiguous, hyphenated, spaced) anywhere in a serialized value."""
    body = json.dumps(obj)
    return [i for i in FULL_IDS if re.search(r"(?<![0-9])" + r"[ \-]?".join(i) + r"(?![0-9])", body)]


def test_redaction_masks_recognized_identifiers_in_any_grouping_but_keeps_phones_and_dates():
    ids = {NEW["account"], NEW["routing"]}
    for printed in ("000987650001", "0009-8765-0001", "0009 8765 0001", "000 987 650 001"):
        assert redact_text(f"Account {printed} · call (555) 010-2233 by 2026-07-14", ids) == (
            "Account ****0001 · call (555) 010-2233 by 2026-07-14")
    assert redact_text("Routing 000-555-666", ids) == "Routing ****5666"
    assert redact_text("Routing 000-555-666") == "Routing 000-555-666"   # not recognized: only long digit runs masked


def test_one_identifier_printed_in_different_formats_counts_once(tmp_path):
    p = tmp_path / "i.pdf"
    p.write_bytes(remit_pdf("same_account_reformatted"))
    inv = extract(p).invoice
    assert inv.remit_account.value == ON_FILE["account"] and inv.remit_readings == {}
    for name in ("contiguous", "hyphenated", "spaced"):
        p.write_bytes(remit_pdf(f"changed_{name}"))
        inv = extract(p).invoice
        assert (inv.remit_routing.value, inv.remit_account.value) == (NEW["routing"], NEW["account"]), name
        assert inv.remit_account.evidence.text.replace(" ", "").replace("-", "") == NEW["account"], name


def test_two_different_accounts_are_extracted_as_separate_readings(tmp_path):
    p = tmp_path / "i.pdf"
    p.write_bytes(remit_pdf("two_accounts"))
    inv = extract(p).invoice
    assert inv.remit_account.value is None and inv.remit_account.note == "2 different account values printed"
    readings = inv.remit_readings["remit_account"]
    assert [r.value for r in readings] == [ON_FILE["account"], NEW["account"]]
    assert [r.evidence.page for r in readings] == [1, 1] and readings[0].evidence.bbox != readings[1].evidence.bbox
    assert inv.remit_routing.value == ON_FILE["routing"] and "remit_routing" not in inv.remit_readings


def test_conflicting_accounts_hold_for_remittance_review_and_change_nothing(client):
    ws = _import(client)
    vendors_before, ledger_before = _vendor_rows(ws), client.get("/ledger", params={"workspace_id": ws}).json()
    run = _submit(client, ws, "two_accounts")
    assert (run["decision"], run["reason_code"], run["allocation_id"]) == ("HOLD", "REMITTANCE_CONFLICT", None)
    detail = run["decision_detail"]
    assert detail["review_field"] == "remittance" and detail["allocation"] is None
    assert client.post(f"/runs/{run['run_id']}/finalize").json()["allocations_for_run"] == 0
    assert client.get("/ledger", params={"workspace_id": ws}).json() == ledger_before
    assert _ledger(client, ws) == (0, 313000)
    assert _vendor_rows(ws) == vendors_before
    # Masked evidence explains the hold: both printed accounts, each with its page and result against the file.
    rem = detail["remittance"]
    assert rem["outcome"] == "conflict" and rem["change_notice"] is None
    acct = next(r for r in rem["comparison"] if r["field"] == "account")
    assert (acct["status"], acct["invoice"], acct["on_file"]) == ("conflict", "****6789 / ****0001", "****6789")
    assert [(x["invoice"], x["vs_file"], x["clear"], x["invoice_evidence"]["page"]) for x in acct["readings"]] == [
        ("****6789", "match", True, 1), ("****0001", "mismatch", True, 1)]
    assert ("2 different account numbers: ****6789 (page 1; match against the vendor master), ****0001 (page 1; "
            "mismatch against the vendor master)") in run["reason"]
    assert ON_FILE["phone"] in detail["next_action"] and "which of the printed accounts" in detail["next_action"]
    assert not _leaks(run) and not _leaks(client.get("/runs").json())


def test_conflicting_accounts_without_a_verification_phone_go_to_procurement(client):
    ws = _import(client, vendors_csv(phone=False))
    run = _submit(client, ws, "two_accounts")
    assert run["reason_code"] == "REMITTANCE_CONFLICT"
    assert "ask procurement for a trusted contact" in run["decision_detail"]["next_action"]


def test_the_same_account_repeated_in_other_formats_keeps_the_normal_workflow(client):
    ws = _import(client)
    run = _submit(client, ws, "same_account_reformatted")
    assert (run["decision"], _ledger(client, ws)) == ("READY", (1, 0))
    assert {r["field"]: r["status"] for r in run["decision_detail"]["remittance"]["comparison"]}["account"] == "match"
    assert not _leaks(run)


@pytest.mark.parametrize("fmt", ["contiguous", "hyphenated", "spaced"])
def test_grouped_identifiers_are_masked_in_the_whole_run_response(client, fmt):
    ws = _import(client)
    pdf = remit_pdf(f"changed_{fmt}")
    rid = client.post("/runs", files={"file": ("i.pdf", pdf, "application/pdf")}, data={"workspace_id": ws}).json()["run_id"]
    run = wait_for(client, rid)
    assert (run["decision"], run["reason_code"], run["allocation_id"]) == ("HOLD", "REMITTANCE_MISMATCH", None)
    assert _leaks(run) == [] and _leaks(client.get("/runs", params={"workspace_id": ws}).json()) == []
    ev_texts = [run["extraction"]["invoice"][k]["evidence"]["text"] for k in ("remit_routing", "remit_account")]
    assert ev_texts == ["****5666", "****0001"]
    acct = next(r for r in run["decision_detail"]["remittance"]["comparison"] if r["field"] == "account")
    assert acct["invoice_evidence"]["text"] == "****0001"
    assert "account number ****0001 on the invoice vs ****6789 on file" in run["reason"]   # still explains the hold
    assert client.get(f"/runs/{rid}/pdf").content == pdf                                 # source evidence kept as uploaded


def test_one_clear_reading_among_unclear_copies_is_compared_not_treated_as_a_conflict():
    def reading(v, conf):
        return Field_(raw=v, value=v, evidence=Evidence(page=1, method="ocr", text=v, bbox=(0, 0, 1, 1), conf=conf,
                                                         label="account"))
    inv = invoice()
    inv.remit_account = Field_(raw=ON_FILE["account"], note="2 different account values printed")
    inv.remit_readings = {"remit_account": [reading(ON_FILE["account"], 95.0), reading("000123458789", 55.0)]}
    d = evaluate(inv, _vendor_snapshot(), min_conf=80, policy_version="t")
    assert d.decision == "READY"
    acct = next(r for r in d.remittance["comparison"] if r["field"] == "account")
    assert acct["status"] == "match" and [x["clear"] for x in acct["readings"]] == [True, False]
    inv.remit_readings["remit_account"][0].evidence.conf = 60.0                # both unclear: recorded only
    d = evaluate(inv, _vendor_snapshot(), min_conf=80, policy_version="t")
    assert d.decision == "READY" and d.remittance["comparison"][2]["status"] == "low_confidence"
    for r in inv.remit_readings["remit_account"]:                              # both clear and different: conflict
        r.evidence.conf = 95.0
    d = evaluate(inv, _vendor_snapshot(), min_conf=80, policy_version="t")
    assert (d.decision, d.reason_code, d.allocation) == ("HOLD", "REMITTANCE_CONFLICT", None)
