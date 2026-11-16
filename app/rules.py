"""Deterministic decision logic. Pure: no I/O, no database, no model calls.

evaluate(invoice, snapshot) -> Decision

Priority order (first failing rule decides):
  1. evidence      critical fields present, unambiguous and above the confidence floor
  2. vendor        unique approved vendor by normalised legal name
  3. duplicate     vendor + invoice number already accepted -> BLOCKED (corroborated copy) /
                   HOLD (material conflict, or OCR-confusable near match)
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
    ctx: dict[str, Any] = {"vendor": None, "po": None, "candidates": [], "prior": [], "authorization": None,
                           "reason_codes": [], "duplicate": None}

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
            reason_codes=ctx["reason_codes"] or [stop.code], authorization=ctx["authorization"],
            duplicate=ctx["duplicate"],
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
        checks=checks, allocation=allocation, prior_allocations=ctx["prior"],
        reason_codes=["MATCHED_WITHIN_AUTHORIZATION"], authorization=ctx["authorization"],
        duplicate=ctx["duplicate"],
        policy_version=policy_version,
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


_OCR_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "S": "5", "B": "8",
                                 "Z": "2", "G": "6"})


def _ocr_canonical(number: str) -> str:
    """Collapse characters OCR commonly confuses. Used only to detect *uncertain* identity, never
    to declare two invoice numbers equal."""
    return number.upper().translate(_OCR_CONFUSABLE)


def _accepted_record(a: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": a["run_id"], "allocation_id": a.get("id"), "invoice_number": a["invoice_number_norm"],
            "invoice_date": a.get("invoice_date"), "accepted_at": a.get("created_at"), "po_id": a["po_id"],
            "currency": a["currency"], "net_cents": a["net_cents"], "tax_cents": a["tax_cents"],
            "gross_cents": a["gross_cents"]}


def _compare_with_accepted(inv: ExtractedInvoice, a: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-by-field comparison of the incoming document with the accepted invoice.

    status: match | conflict | not_printed (an optional value absent on the incoming copy)."""
    rows: list[dict[str, Any]] = []

    def row(field: str, incoming: Any, accepted: Any, *, optional: bool = False, money: bool = False) -> None:
        if incoming is None and optional:
            status = "not_printed"
        else:
            status = "match" if incoming == accepted else "conflict"
        rows.append({"field": field, "incoming": incoming, "accepted": accepted, "status": status, "money": money})

    row("invoice_date", inv.invoice_date.value, a.get("invoice_date"))
    row("currency", inv.currency.value, a["currency"])
    row("net", inv.net.value, a["net_cents"], money=True)
    row("tax", inv.tax.value if inv.tax.value is not None else 0, a["tax_cents"], money=True)
    row("gross", inv.gross.value, a["gross_cents"], money=True)
    row("po_reference", inv.po_reference.value, a["po_id"], optional=True)
    acc_lines = {l["sku"]: l for l in a.get("lines", [])}
    inc_skus = [l.sku.value for l in inv.lines]
    row("line_skus", sorted(inc_skus), sorted(acc_lines))
    for line in inv.lines:
        al = acc_lines.get(line.sku.value)
        if al is None:
            continue
        sku = line.sku.value
        row(f"{sku} quantity", norm_decimal(Decimal(line.quantity.value)), norm_decimal(Decimal(al["quantity"])))
        row(f"{sku} unit_price", norm_decimal(Decimal(line.unit_price.value)), norm_decimal(Decimal(al["unit_price"])))
        row(f"{sku} amount", line.amount.value, al["net_cents"], money=True)
    return rows


def _duplicate(inv, snap: Snapshot, vendor, check, ctx) -> None:
    """Business duplicate: approved vendor ID + conservatively normalised printed invoice number,
    looked up against previously *accepted* (allocated) invoices only. Held or blocked runs never
    count, so a corrected resubmission after a HOLD is evaluated normally."""
    key = inv.invoice_number.value
    vid = vendor["vendor_id"]
    business_key = {"vendor_id": vid, "vendor_name": vendor["legal_name"], "invoice_number": key,
                    "invoice_number_raw": inv.invoice_number.raw}
    accepted = [a for a in snap.allocations if a["vendor_id"] == vid]
    exact = [a for a in accepted if a["invoice_number_norm"] == key]
    dup: dict[str, Any] = {"business_key": business_key, "accepted": None, "comparison": [], "outcome": "no_match"}
    ctx["duplicate"] = dup

    if not exact:
        # Fail closed on uncertain identity: an OCR-read number that differs from an accepted number
        # of the same vendor only by OCR-confusable characters is not a verified distinct number.
        ev = inv.invoice_number.evidence
        if ev is not None and ev.method == "ocr":
            near = [a for a in accepted if _ocr_canonical(a["invoice_number_norm"]) == _ocr_canonical(key)]
            if near:
                a = near[0]
                dup.update(accepted=_accepted_record(a), comparison=_compare_with_accepted(inv, a),
                           outcome="identity_uncertain")
                ctx["po"] = snap.pos.get(a["po_id"])  # the PO the accepted invoice was allocated to
                ctx["prior"] = [{"run_id": a["run_id"], "invoice_number_norm": a["invoice_number_norm"],
                                 "po_id": a["po_id"], "net_cents": a["net_cents"], "gross_cents": a["gross_cents"]}]
                check("duplicate", False, f"OCR read {key!r}; accepted {a['invoice_number_norm']!r} differs only by "
                      f"OCR-confusable characters", accepted_run=a["run_id"])
                raise _Stop("HOLD", "DUPLICATE_IDENTITY_UNCERTAIN",
                            f"The scanned invoice number {key!r} differs from accepted invoice "
                            f"{a['invoice_number_norm']!r} (run {a['run_id']}) only by characters OCR often confuses.",
                            f"Compare the printed invoice number on the PDF with {a['invoice_number_norm']}. If it is "
                            f"the same invoice, close this as a duplicate; if not, confirm the number and re-submit.",
                            review_field="invoice_number")
        check("duplicate", True, f"no accepted invoice for {vid} / {key}")
        return

    a = exact[0]
    comparison = _compare_with_accepted(inv, a)
    conflicts = [r for r in comparison if r["status"] == "conflict"]
    dup.update(accepted=_accepted_record(a), comparison=comparison,
               outcome="conflict" if conflicts else "corroborated")
    ctx["po"] = snap.pos.get(a["po_id"])  # the PO the accepted invoice was allocated to
    ctx["prior"] = [{"run_id": a["run_id"], "invoice_number_norm": a["invoice_number_norm"], "po_id": a["po_id"],
                     "net_cents": a["net_cents"], "gross_cents": a["gross_cents"]}]
    when = f" dated {a.get('invoice_date')}" if a.get("invoice_date") else ""
    if not conflicts:
        matched = ", ".join(r["field"] for r in comparison if r["status"] == "match")
        check("duplicate", False, f"{vid} / {key} already accepted in run {a['run_id']}; corroborated: {matched}",
              accepted_run=a["run_id"])
        raise _Stop("BLOCKED", "DUPLICATE_INVOICE",
                    f"Invoice {key}{when} from {vendor['legal_name']} was already accepted on "
                    f"{(a.get('created_at') or '')[:10]} (run {a['run_id']}). This document repeats it: "
                    f"{matched} all match.",
                    f"Do not pay again. Close this submission as a copy of {key} and tell the vendor the original "
                    f"is already in process.")

    def show(r: dict[str, Any]) -> str:
        fmt = (lambda v: fmt_cents(v, a["currency"])) if r["money"] else (lambda v: str(v))
        return f"{r['field']} (accepted {fmt(r['accepted'])}, this document {fmt(r['incoming'])})"

    check("duplicate", False, f"{vid} / {key} already accepted in run {a['run_id']} with different "
          + ", ".join(r["field"] for r in conflicts), accepted_run=a["run_id"],
          conflicts=[r["field"] for r in conflicts])
    raise _Stop("HOLD", "DUPLICATE_NUMBER_CONFLICT",
                f"Invoice number {key} from {vendor['legal_name']} was already accepted (run {a['run_id']}{when}), "
                f"but this document differs: " + "; ".join(show(r) for r in conflicts) + ".",
                f"Do not pay. Ask {vendor['legal_name']} whether this is a correction of {key}; a correction needs a "
                f"credit note for the original and a new invoice number, a new charge needs its own number.",
                review_field="invoice_" + ("date" if conflicts[0]["field"] == "invoice_date" else "number"))


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


def _prior_invoices(snap: Snapshot, po_id: str) -> list[dict[str, Any]]:
    """Previously accepted (allocated) invoices on this PO, oldest first."""
    out = []
    for a in snap.allocations:
        if a["po_id"] != po_id:
            continue
        out.append({"run_id": a["run_id"], "invoice_number": a["invoice_number_norm"],
                    "invoice_date": a.get("invoice_date"), "accepted_at": a.get("created_at"),
                    "net_cents": a["net_cents"], "gross_cents": a["gross_cents"],
                    "lines": [{"line_id": l["line_id"], "sku": l["sku"], "quantity": l["quantity"],
                               "net_cents": l["net_cents"]} for l in a["lines"]]})
    return out


def _authorization(inv, snap: Snapshot, po, mapping, check, ctx) -> dict[str, Any]:
    """Strict cumulative cap on net (per PO line and PO total) and quantity (per PO line).

    Every comparison is evaluated, with no early exit, so a hold shows all breaches. The figures
    are returned inside the decision so an old hold still explains itself after the ledger moves.
    """
    before = po_balance(snap, po["po_id"])
    cur = po["currency"]
    prior_invoices = _prior_invoices(snap, po["po_id"])
    if prior_invoices and not ctx["prior"]:
        ctx["prior"] = [{"run_id": a["run_id"], "invoice_number_norm": a["invoice_number"], "po_id": po["po_id"],
                         "net_cents": a["net_cents"], "gross_cents": a["gross_cents"]} for a in prior_invoices]
    bal_lines = {b["line_id"]: b for b in before["lines"]}
    comparisons: list[dict[str, Any]] = []

    def record(c: dict[str, Any], detail: str) -> None:
        comparisons.append(c)
        check("authorization", c["status"] == "pass", detail,
              **{k: c[k] for k in ("measure", "scope", "line_id", "authorized", "previously_allocated", "remaining",
                                   "incoming", "projected", "excess")})

    def money_cmp(scope: str, label: str, authorized: int, prior: int, incoming: int,
                  line_id: str | None = None, sku: str | None = None) -> None:
        projected = prior + incoming
        c = {"measure": "net", "scope": scope, "label": label, "line_id": line_id, "sku": sku, "unit": cur,
             "authorized": authorized, "previously_allocated": prior, "remaining": authorized - prior,
             "incoming": incoming, "projected": projected, "excess": max(projected - authorized, 0),
             "status": "pass" if projected <= authorized else "fail"}
        record(c, f"{label} net: authorized {fmt_cents(authorized, cur)}, previously allocated "
                  f"{fmt_cents(prior, cur)}, incoming {fmt_cents(incoming, cur)}, projected {fmt_cents(projected, cur)}"
                  + (f", excess {fmt_cents(c['excess'], cur)}" if c["excess"] else ""))

    def qty_cmp(label: str, authorized: Decimal, prior: Decimal, incoming: Decimal, line_id: str, sku: str) -> None:
        projected = prior + incoming
        excess = max(projected - authorized, Decimal(0))
        c = {"measure": "quantity", "scope": "line", "label": label, "line_id": line_id, "sku": sku, "unit": "units",
             "authorized": norm_decimal(authorized), "previously_allocated": norm_decimal(prior),
             "remaining": norm_decimal(authorized - prior), "incoming": norm_decimal(incoming),
             "projected": norm_decimal(projected), "excess": norm_decimal(excess),
             "status": "pass" if projected <= authorized else "fail"}
        record(c, f"{label} quantity: authorized {c['authorized']}, previously allocated {c['previously_allocated']}, "
                  f"incoming {c['incoming']}, projected {c['projected']}" + (f", excess {c['excess']}" if excess else ""))

    alloc_lines = []
    line_row: dict[str, int] = {}
    for line, pl in mapping:
        b = bal_lines[pl["line_id"]]
        label = f"{po['po_id']} line {pl['line_id']} {pl['sku']}"
        q = Decimal(line.quantity.value)
        line_row[pl["line_id"]] = line.row_index + 1
        qty_cmp(label, Decimal(b["authorized_quantity"]), Decimal(b["allocated_quantity"]), q, pl["line_id"], pl["sku"])
        money_cmp("line", label, b["authorized_net_cents"], b["allocated_net_cents"], line.amount.value,
                  pl["line_id"], pl["sku"])
        alloc_lines.append({"line_id": pl["line_id"], "sku": pl["sku"], "quantity": norm_decimal(q),
                            "unit_price": line.unit_price.value, "net_cents": line.amount.value})
    net = inv.net.value
    money_cmp("po", f"{po['po_id']} total", before["authorized_net_cents"], before["allocated_net_cents"], net)

    breaches = [c for c in comparisons if c["status"] == "fail"]
    ctx["authorization"] = {"po_id": po["po_id"], "currency": cur, "comparisons": comparisons, "breaches": breaches,
                            "prior_invoices": prior_invoices, "balance_at_decision": before}
    if breaches:
        _authorization_hold(inv, po, cur, comparisons, breaches, prior_invoices, line_row, ctx)

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


def _authorization_hold(inv, po, cur, comparisons, breaches, prior_invoices, line_row, ctx) -> None:
    money = [c for c in breaches if c["measure"] == "net"]
    qty = [c for c in breaches if c["measure"] == "quantity"]
    # Deterministic primary code: any money breach outranks a quantity-only breach.
    code = "AMOUNT_EXCEEDS_PO" if money else "QUANTITY_EXCEEDS_PO"
    ctx["reason_codes"] = (["AMOUNT_EXCEEDS_PO"] if money else []) + (["QUANTITY_EXCEEDS_PO"] if qty else [])

    parts = [f"{c['label']} net {fmt_cents(c['previously_allocated'], cur)} already allocated + "
             f"{fmt_cents(c['incoming'], cur)} on this invoice = {fmt_cents(c['projected'], cur)}, "
             f"{fmt_cents(c['excess'], cur)} over the {fmt_cents(c['authorized'], cur)} authorized" for c in money]
    parts += [f"{c['label']} quantity {c['previously_allocated']} already allocated + {c['incoming']} on this invoice = "
              f"{c['projected']}, {c['excess']} over the {c['authorized']} authorized" for c in qty]
    earlier = ", ".join(f"{a['invoice_number']} ({fmt_cents(a['net_cents'], cur)})" for a in prior_invoices)
    reason = (f"Invoice {inv.invoice_number.value} would take {po['po_id']} past its authorization"
              + (f" after earlier accepted invoice(s) {earlier}" if earlier else "") + ": " + "; ".join(parts) + ".")

    asks = []
    if money:
        asks.append(f"at least {fmt_cents(max(c['excess'] for c in money), cur)} of net authorization")
    asks += [f"{c['excess']} units of {c['sku']}" for c in qty]
    net_room = max(min(c["remaining"] for c in comparisons if c["measure"] == "net"), 0)
    room = [f"{fmt_cents(net_room, cur)} net"] + [
        f"{c['remaining']} units of {c['sku']}" for c in comparisons if c["measure"] == "quantity"]
    action = (f"Do not pay. Ask the PO owner for a change order on {po['po_id']} adding {' and '.join(asks)}, "
              f"or ask {ctx['vendor']['legal_name']} for a corrected invoice or credit note that fits the remaining "
              f"{' / '.join(room)}. Re-submit when either arrives.")

    if money:
        primary = next((c for c in money if c["scope"] == "po"), money[0])
    else:
        primary = qty[0]
    if primary["scope"] == "po":
        review = "net"
    else:
        review = f"line {line_row[primary['line_id']]} " + ("amount" if primary["measure"] == "net" else "quantity")
    raise _Stop("HOLD", code, reason, action, review_field=review)
