"""Deterministic decision logic. Pure: no I/O, no database, no model calls.

evaluate(invoice, snapshot) -> Decision

Priority order (first failing rule decides):
  1. evidence      critical fields present, unambiguous and above the confidence floor
  2. vendor        unique approved vendor by normalised legal name
  3. duplicate     vendor + invoice number already allocated -> BLOCKED (identical) / HOLD (conflict)
  4. po            printed PO exists, belongs to the vendor, same currency, OPEN
  5. arithmetic    qty x rate = line amount, lines = net, net + tax = gross
  6. line mapping  every invoice line maps by SKU to exactly one PO line
  7. price         unit price within 1% (inclusive) of the PO unit price
  8. authorization cumulative net and quantity (prior allocations + this invoice) <= PO authorization
  -> READY with one allocation
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from .models import Decision, ExtractedInvoice, Field_
from .money import decimal_to_cents, fmt_cents, norm_decimal
from .procurement import norm_name

PRICE_TOLERANCE = Decimal("0.01")  # 1%, inclusive
SUPPORTED_CURRENCIES = {"USD"}


@dataclass
class Snapshot:
    vendors: list[dict[str, Any]]
    pos: dict[str, dict[str, Any]]
    po_lines: dict[str, list[dict[str, Any]]]
    allocations: list[dict[str, Any]] = field(default_factory=list)  # each with "lines"


class _Stop(Exception):
    def __init__(self, decision: str, code: str, reason: str, action: str, review_field: Optional[str] = None):
        self.decision, self.code, self.reason, self.action, self.review_field = (
            decision, code, reason, action, review_field)


def po_balance(snap: Snapshot, po_id: str) -> dict[str, Any]:
    """Authorized, allocated and remaining net (cents) and quantities for one PO."""
    po = snap.pos[po_id]
    allocs = [a for a in snap.allocations if a["po_id"] == po_id]
    used_net = sum(a["net_cents"] for a in allocs)
    lines = []
    for pl in snap.po_lines.get(po_id, []):
        lq = sum((Decimal(al["quantity"]) for a in allocs for al in a["lines"] if al["line_id"] == pl["line_id"]),
                 Decimal(0))
        ln = sum(al["net_cents"] for a in allocs for al in a["lines"] if al["line_id"] == pl["line_id"])
        aq = Decimal(pl["authorized_quantity"])
        lines.append({
            "line_id": pl["line_id"], "sku": pl["sku"], "unit_price": pl["unit_price"],
            "authorized_quantity": norm_decimal(aq), "allocated_quantity": norm_decimal(lq),
            "remaining_quantity": norm_decimal(aq - lq),
            "authorized_net_cents": pl["authorized_net_cents"], "allocated_net_cents": ln,
            "remaining_net_cents": pl["authorized_net_cents"] - ln,
        })
    return {"po_id": po_id, "currency": po["currency"], "authorized_net_cents": po["authorized_net_cents"],
            "allocated_net_cents": used_net, "remaining_net_cents": po["authorized_net_cents"] - used_net,
            "allocation_count": len(allocs), "lines": lines}


def evaluate(inv: ExtractedInvoice, snap: Snapshot, *, min_conf: float, policy_version: str) -> Decision:
    checks: list[dict[str, Any]] = []
    ctx: dict[str, Any] = {"vendor": None, "po": None, "candidates": [], "prior": []}

    def check(rule: str, ok: bool, detail: str, **compared: Any) -> bool:
        checks.append({"rule": rule, "status": "pass" if ok else "fail", "detail": detail, "compared": compared})
        return ok

    try:
        _evidence(inv, min_conf, check)
        vendor = _vendor(inv, snap, check)
        ctx["vendor"] = vendor
        _duplicate(inv, snap, vendor, check, ctx)
        po = _po(inv, snap, vendor, check, ctx)
        ctx["po"] = po
        _arithmetic(inv, check)
        mapping = _line_mapping(inv, snap, po, check)
        _price(mapping, check)
        allocation = _authorization(inv, snap, po, mapping, check, ctx)
    except _Stop as stop:
        return Decision(
            decision=stop.decision, reason_code=stop.code, reason=stop.reason, next_action=stop.action,
            vendor_id=ctx["vendor"]["vendor_id"] if ctx["vendor"] else None,
            vendor_name=ctx["vendor"]["legal_name"] if ctx["vendor"] else None,
            po_id=ctx["po"]["po_id"] if ctx["po"] else None, candidate_po_ids=ctx["candidates"],
            checks=checks, prior_allocations=ctx["prior"], review_field=stop.review_field,
            policy_version=policy_version,
        )
    cur = po["currency"]
    return Decision(
        decision="READY", reason_code="MATCHED_WITHIN_AUTHORIZATION",
        reason=(f"Invoice {inv.invoice_number.value} matches {po['po_id']} for {ctx['vendor']['legal_name']}: "
                f"arithmetic reconciles, unit prices are within 1%, and {fmt_cents(allocation['net_cents'], cur)} "
                f"fits the remaining authorization of {fmt_cents(allocation['balance_before']['remaining_net_cents'], cur)}."),
        next_action="Pass to the next AP step for approval and payment scheduling.",
        vendor_id=ctx["vendor"]["vendor_id"], vendor_name=ctx["vendor"]["legal_name"], po_id=po["po_id"],
        checks=checks, allocation=allocation, prior_allocations=ctx["prior"], policy_version=policy_version,
    )


# ----------------------------------------------------------------------------- rules

def _evidence(inv: ExtractedInvoice, min_conf: float, check) -> None:
    critical: list[tuple[str, Field_]] = [
        ("supplier_name", inv.supplier_name), ("invoice_number", inv.invoice_number),
        ("invoice_date", inv.invoice_date), ("currency", inv.currency), ("net", inv.net), ("gross", inv.gross),
    ]
    if inv.tax.value is not None or inv.tax.raw is not None:
        critical.append(("tax", inv.tax))
    if inv.po_reference.raw is not None:
        critical.append(("po_reference", inv.po_reference))
    for i, line in enumerate(inv.lines, start=1):
        for name in ("sku", "quantity", "unit_price", "amount"):
            critical.append((f"line {i} {name}", getattr(line, name)))
    for name, f in critical:
        if f.value is None or f.evidence is None:
            why = f.note or "not found on the document"
            check("evidence", False, f"{name}: {why}", field=name, raw=f.raw)
            raise _Stop("HOLD", "MISSING_OR_UNCLEAR_FIELD", f"Required field '{name}' could not be verified ({why}).",
                        f"Review the PDF and confirm the value of '{name}'.", review_field=name)
        if f.evidence.conf < min_conf:
            check("evidence", False, f"{name}: OCR confidence {f.evidence.conf} below {min_conf}",
                  field=name, raw=f.raw, conf=f.evidence.conf)
            raise _Stop("HOLD", "LOW_CONFIDENCE_FIELD",
                        f"'{name}' was read as {f.raw!r} with OCR confidence {f.evidence.conf} (minimum {min_conf}).",
                        f"Confirm '{name}' against the PDF.", review_field=name)
    if inv.tax.value is None and inv.net.value != inv.gross.value:
        check("evidence", False, "tax not printed and net != gross", net=inv.net.value, gross=inv.gross.value)
        raise _Stop("HOLD", "MISSING_OR_UNCLEAR_FIELD", "Tax is not printed and net differs from gross.",
                    "Confirm the tax amount on the invoice.", review_field="tax")
    if not inv.lines:
        check("evidence", False, "no line items found")
        raise _Stop("HOLD", "MISSING_OR_UNCLEAR_FIELD", "No line items could be read from the invoice table.",
                    "Review the line items on the PDF.", review_field="lines")
    if inv.issues:
        check("evidence", False, "; ".join(inv.issues), issues=inv.issues)
        raise _Stop("HOLD", "EXTRACTION_ISSUE", "Extraction found ambiguity: " + "; ".join(inv.issues),
                    "Review the flagged fields on the PDF.", review_field=inv.issues[0].split(":")[0])
    if inv.currency.value not in SUPPORTED_CURRENCIES:
        check("evidence", False, f"unsupported currency {inv.currency.value}", currency=inv.currency.value)
        raise _Stop("HOLD", "UNSUPPORTED_CURRENCY", f"Currency {inv.currency.value} is not supported.",
                    "Route to manual processing.", review_field="currency")
    check("evidence", True, f"{len(critical)} critical values found with confidence >= {min_conf}",
          min_conf_observed=min(f.evidence.conf for _, f in critical))


def _vendor(inv: ExtractedInvoice, snap: Snapshot, check) -> dict[str, Any]:
    name = inv.supplier_name.value
    n = norm_name(name)
    matches = [v for v in snap.vendors if v["legal_name_norm"] == n]
    if len(matches) != 1:
        check("vendor", False, f"{len(matches)} vendors match {name!r}", supplier=name, normalized=n)
        raise _Stop("HOLD", "VENDOR_NOT_FOUND" if not matches else "VENDOR_AMBIGUOUS",
                    f"Supplier {name!r} does not match exactly one vendor in the vendor master.",
                    "Confirm the vendor or add it to the vendor master.", review_field="supplier_name")
    v = matches[0]
    if v["status"] != "APPROVED":
        check("vendor", False, f"{v['vendor_id']} status is {v['status']}", vendor_id=v["vendor_id"])
        raise _Stop("HOLD", "VENDOR_NOT_APPROVED", f"Vendor {v['legal_name']} ({v['vendor_id']}) is {v['status']}.",
                    "Confirm vendor approval with procurement.")
    check("vendor", True, f"{name!r} -> {v['vendor_id']} {v['legal_name']}", vendor_id=v["vendor_id"],
          source=f"{v['source_file']} row {v['source_row']}")
    return v


def _duplicate(inv, snap: Snapshot, vendor, check, ctx) -> None:
    key = inv.invoice_number.value
    prior = [a for a in snap.allocations if a["vendor_id"] == vendor["vendor_id"] and a["invoice_number_norm"] == key]
    if not prior:
        check("duplicate", True, f"no earlier allocation for {vendor['vendor_id']} / {key}")
        return
    p = prior[0]
    ctx["prior"] = [{k: p[k] for k in ("run_id", "invoice_number_norm", "po_id", "net_cents", "gross_cents")}]
    same = (p["gross_cents"] == inv.gross.value and p["net_cents"] == inv.net.value
            and p["currency"] == inv.currency.value and p["po_id"] == (inv.po_reference.value or p["po_id"]))
    check("duplicate", False, f"invoice {key} already allocated in run {p['run_id']}", earlier_run=p["run_id"],
          earlier_gross=p["gross_cents"], incoming_gross=inv.gross.value)
    if same:
        raise _Stop("BLOCKED", "DUPLICATE_INVOICE",
                    f"Invoice {key} from {vendor['legal_name']} was already accepted (run {p['run_id']}).",
                    "Do not pay again; link to the earlier record.")
    raise _Stop("HOLD", "DUPLICATE_NUMBER_CONFLICT",
                f"Invoice number {key} was already accepted (run {p['run_id']}) with different amounts or PO.",
                "Ask the vendor whether this is a correction or a new invoice.", review_field="invoice_number")


def _po(inv, snap: Snapshot, vendor, check, ctx) -> dict[str, Any]:
    ref = inv.po_reference.value
    if ref is None:
        cands = sorted(p["po_id"] for p in snap.pos.values()
                       if p["vendor_id"] == vendor["vendor_id"] and p["status"] == "OPEN"
                       and p["currency"] == inv.currency.value)
        ctx["candidates"] = cands
        check("po", False, "no PO reference printed", candidates=cands)
        raise _Stop("HOLD", "PO_MISSING", "The invoice has no printed PO reference. "
                    + (f"Candidate open POs for this vendor: {', '.join(cands)}." if cands else "No open PO found."),
                    "Confirm the PO with the requester before allocation.", review_field="po_reference")
    po = snap.pos.get(ref)
    if po is None:
        check("po", False, f"{ref} not in purchase orders", po_reference=ref)
        raise _Stop("HOLD", "PO_NOT_FOUND", f"Printed PO {ref} does not exist in the PO data.",
                    "Ask the vendor for the correct PO number.", review_field="po_reference")
    problems = []
    if po["vendor_id"] != vendor["vendor_id"]:
        problems.append(("PO_VENDOR_MISMATCH", f"{ref} belongs to vendor {po['vendor_id']}, not {vendor['vendor_id']}"))
    if po["currency"] != inv.currency.value:
        problems.append(("PO_CURRENCY_MISMATCH", f"{ref} is in {po['currency']}, invoice is {inv.currency.value}"))
    if po["status"] != "OPEN":
        problems.append(("PO_NOT_OPEN", f"{ref} status is {po['status']}"))
    if problems:
        ctx["po"] = po
        check("po", False, "; ".join(p[1] for p in problems), po_id=ref)
        raise _Stop("HOLD", problems[0][0], problems[0][1] + ".", "Resolve the PO conflict with procurement.",
                    review_field="po_reference")
    check("po", True, f"{ref} is OPEN, {po['currency']}, vendor {po['vendor_id']}", po_id=ref,
          source=f"{po['source_file']} row {po['source_row']}")
    return po


def _arithmetic(inv: ExtractedInvoice, check) -> None:
    for i, line in enumerate(inv.lines, start=1):
        q, r = Decimal(line.quantity.value), Decimal(line.unit_price.value)
        expected = decimal_to_cents(q * r)
        if not check("arithmetic", expected == line.amount.value,
                     f"line {i}: {q} x {r} = {fmt_cents(expected)} vs printed {fmt_cents(line.amount.value)}",
                     line=i, expected_cents=expected, printed_cents=line.amount.value):
            raise _Stop("HOLD", "LINE_ARITHMETIC_MISMATCH",
                        f"Line {i}: {q} x {r} = {fmt_cents(expected)} but the invoice prints {fmt_cents(line.amount.value)}.",
                        "Ask the vendor to correct the line amount.", review_field=f"line {i} amount")
    lines_total = sum(l.amount.value for l in inv.lines)
    if not check("arithmetic", lines_total == inv.net.value,
                 f"sum of lines {fmt_cents(lines_total)} vs net {fmt_cents(inv.net.value)}",
                 lines_total_cents=lines_total, net_cents=inv.net.value):
        raise _Stop("HOLD", "TOTALS_MISMATCH",
                    f"Line amounts sum to {fmt_cents(lines_total)} but the subtotal is {fmt_cents(inv.net.value)}.",
                    "Ask the vendor to correct the invoice totals.", review_field="net")
    tax = inv.tax.value or 0
    if not check("arithmetic", inv.net.value + tax == inv.gross.value,
                 f"net {fmt_cents(inv.net.value)} + tax {fmt_cents(tax)} vs total {fmt_cents(inv.gross.value)}",
                 net_cents=inv.net.value, tax_cents=tax, gross_cents=inv.gross.value):
        raise _Stop("HOLD", "TOTALS_MISMATCH",
                    f"Net {fmt_cents(inv.net.value)} + tax {fmt_cents(tax)} does not equal total {fmt_cents(inv.gross.value)}.",
                    "Ask the vendor to correct the invoice totals.", review_field="gross")


def _line_mapping(inv, snap: Snapshot, po, check) -> list[tuple[Any, dict[str, Any]]]:
    by_sku = {pl["sku"]: pl for pl in snap.po_lines.get(po["po_id"], [])}
    mapping = []
    seen: set[str] = set()
    for i, line in enumerate(inv.lines, start=1):
        pl = by_sku.get(line.sku.value)
        if pl is None or pl["line_id"] in seen:
            check("line_mapping", False, f"line {i} SKU {line.sku.value} has no unique line on {po['po_id']}",
                  sku=line.sku.value, po_skus=sorted(by_sku))
            raise _Stop("HOLD", "LINE_NOT_ON_PO",
                        f"Invoice line {i} ({line.sku.value}) does not map to a line on {po['po_id']}.",
                        "Confirm which PO line this charge belongs to.", review_field=f"line {i} sku")
        seen.add(pl["line_id"])
        mapping.append((line, pl))
        check("line_mapping", True, f"line {i} {line.sku.value} -> {po['po_id']} line {pl['line_id']}",
              sku=line.sku.value, po_line=pl["line_id"], source=f"{pl['source_file']} row {pl['source_row']}")
    return mapping


def _price(mapping, check) -> None:
    for line, pl in mapping:
        inv_rate, po_rate = Decimal(line.unit_price.value), Decimal(pl["unit_price"])
        diff = abs(inv_rate - po_rate)
        ok = diff <= po_rate * PRICE_TOLERANCE
        pct = (diff / po_rate * 100) if po_rate else Decimal(0)
        if not check("price", ok, f"{line.sku.value}: invoice {inv_rate} vs PO {po_rate} ({pct:.2f}% variance, limit 1%)",
                     sku=line.sku.value, invoice_rate=str(inv_rate), po_rate=str(po_rate), variance_pct=f"{pct:.4f}"):
            raise _Stop("HOLD", "PRICE_VARIANCE",
                        f"{line.sku.value} unit price {inv_rate} differs from PO price {po_rate} by {pct:.2f}% (limit 1%).",
                        "Ask the buyer to approve the price or the vendor to re-bill.",
                        review_field=f"line {line.row_index + 1} unit_price")


def _authorization(inv, snap: Snapshot, po, mapping, check, ctx) -> dict[str, Any]:
    before = po_balance(snap, po["po_id"])
    cur = po["currency"]
    prior = [a for a in snap.allocations if a["po_id"] == po["po_id"]]
    if prior and not ctx["prior"]:
        ctx["prior"] = [{k: a[k] for k in ("run_id", "invoice_number_norm", "po_id", "net_cents", "gross_cents")}
                        for a in prior]
    bal_lines = {b["line_id"]: b for b in before["lines"]}
    alloc_lines = []
    for line, pl in mapping:
        b = bal_lines[pl["line_id"]]
        q = Decimal(line.quantity.value)
        rem_q = Decimal(b["remaining_quantity"])
        if not check("authorization", q <= rem_q,
                     f"{pl['sku']}: prior {b['allocated_quantity']} + incoming {norm_decimal(q)} vs authorized "
                     f"{b['authorized_quantity']}", sku=pl["sku"], prior_qty=b["allocated_quantity"],
                     incoming_qty=norm_decimal(q), authorized_qty=b["authorized_quantity"]):
            excess = q - rem_q
            raise _Stop("HOLD", "QUANTITY_EXCEEDS_PO",
                        f"{pl['sku']}: {b['allocated_quantity']} already billed + {norm_decimal(q)} on this invoice = "
                        f"{norm_decimal(Decimal(b['allocated_quantity']) + q)}, above the {b['authorized_quantity']} "
                        f"authorized on {po['po_id']} by {norm_decimal(excess)}.",
                        "Ask the buyer for a PO change order or the vendor for a corrected invoice.",
                        review_field=f"line {line.row_index + 1} quantity")
        if not check("authorization", line.amount.value <= b["remaining_net_cents"],
                     f"{pl['sku']}: prior {fmt_cents(b['allocated_net_cents'], cur)} + incoming "
                     f"{fmt_cents(line.amount.value, cur)} vs authorized {fmt_cents(b['authorized_net_cents'], cur)}",
                     sku=pl["sku"], prior_net_cents=b["allocated_net_cents"], incoming_net_cents=line.amount.value,
                     authorized_net_cents=b["authorized_net_cents"]):
            raise _Stop("HOLD", "AMOUNT_EXCEEDS_PO",
                        f"{pl['sku']}: {fmt_cents(b['allocated_net_cents'], cur)} already billed + "
                        f"{fmt_cents(line.amount.value, cur)} exceeds the {fmt_cents(b['authorized_net_cents'], cur)} "
                        f"authorized by {fmt_cents(line.amount.value - b['remaining_net_cents'], cur)}.",
                        "Ask the buyer for a PO change order or the vendor for a corrected invoice.",
                        review_field=f"line {line.row_index + 1} amount")
        alloc_lines.append({"line_id": pl["line_id"], "sku": pl["sku"], "quantity": norm_decimal(q),
                            "unit_price": line.unit_price.value, "net_cents": line.amount.value})
    net = inv.net.value
    if not check("authorization", net <= before["remaining_net_cents"],
                 f"PO {po['po_id']}: prior {fmt_cents(before['allocated_net_cents'], cur)} + incoming {fmt_cents(net, cur)} "
                 f"vs authorized {fmt_cents(before['authorized_net_cents'], cur)}",
                 prior_net_cents=before["allocated_net_cents"], incoming_net_cents=net,
                 authorized_net_cents=before["authorized_net_cents"]):
        raise _Stop("HOLD", "AMOUNT_EXCEEDS_PO",
                    f"{po['po_id']}: {fmt_cents(before['allocated_net_cents'], cur)} already billed + {fmt_cents(net, cur)} "
                    f"exceeds the {fmt_cents(before['authorized_net_cents'], cur)} authorized by "
                    f"{fmt_cents(net - before['remaining_net_cents'], cur)}.",
                    "Ask the buyer for a PO change order or the vendor for a corrected invoice.", review_field="net")
    after = dict(before)
    after["allocated_net_cents"] = before["allocated_net_cents"] + net
    after["remaining_net_cents"] = before["remaining_net_cents"] - net
    after["allocation_count"] = before["allocation_count"] + 1
    after["lines"] = []
    inc = {al["line_id"]: al for al in alloc_lines}
    for b in before["lines"]:
        nb = dict(b)
        if b["line_id"] in inc:
            q = Decimal(inc[b["line_id"]]["quantity"])
            nb["allocated_quantity"] = norm_decimal(Decimal(b["allocated_quantity"]) + q)
            nb["remaining_quantity"] = norm_decimal(Decimal(b["remaining_quantity"]) - q)
            nb["allocated_net_cents"] = b["allocated_net_cents"] + inc[b["line_id"]]["net_cents"]
            nb["remaining_net_cents"] = b["remaining_net_cents"] - inc[b["line_id"]]["net_cents"]
        after["lines"].append(nb)
    return {"po_id": po["po_id"], "currency": cur, "net_cents": net, "tax_cents": inv.tax.value or 0,
            "gross_cents": inv.gross.value, "lines": alloc_lines, "balance_before": before, "balance_after": after}
