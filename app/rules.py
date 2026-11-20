"""Deterministic decision logic. Pure: no I/O, no database, no model calls.

evaluate(invoice, snapshot) -> Decision

Priority order (first failing rule decides):
  1. evidence      critical fields present, unambiguous and above the confidence floor
  2. vendor        unique approved vendor by normalised legal name (exact first; else a unique
                   legal-suffix variant, e.g. printed "Acme Tools" for legal "Acme Tools Inc.")
  3. duplicate     vendor + invoice number already accepted -> BLOCKED (corroborated copy) /
                   HOLD (material conflict, or OCR-confusable near match). A number that differs only
                   in leading zeros is BLOCKED when fully corroborated, else HOLD.
  4. po            printed PO exists, belongs to the vendor, same currency, OPEN
  5. arithmetic    qty x rate = line amount, lines = net, net + tax = gross
  6. line mapping  every invoice line maps by SKU to exactly one PO line; a line with no printed SKU
                   maps by its verified description to a unique line on the printed PO
  7. price         unit price within 1% (inclusive) of the PO unit price
  8. authorization cumulative net and quantity (prior allocations + this invoice) <= PO authorization
  -> READY with one allocation
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
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
                           "reason_codes": [], "duplicate": None, "po_candidates": None, "price_check": None,
                           "vendor_match": None, "line_matches": None}

    def check(rule: str, ok: bool, detail: str, **compared: Any) -> bool:
        checks.append({"rule": rule, "status": "pass" if ok else "fail", "detail": detail, "compared": compared})
        return ok

    try:
        _evidence(inv, min_conf, check)
        vendor = _vendor(inv, snap, check, ctx)
        ctx["vendor"] = vendor
        _duplicate(inv, snap, vendor, check, ctx, min_conf)
        po = _po(inv, snap, vendor, check, ctx)
        ctx["po"] = po
        _arithmetic(inv, check)
        mapping = _line_mapping(inv, snap, po, check, ctx)
        _price(inv, po, mapping, check, ctx)
        allocation = _authorization(inv, snap, po, mapping, check, ctx)
    except _Stop as stop:
        return Decision(
            decision=stop.decision, reason_code=stop.code, reason=stop.reason, next_action=stop.action,
            vendor_id=ctx["vendor"]["vendor_id"] if ctx["vendor"] else None,
            vendor_name=ctx["vendor"]["legal_name"] if ctx["vendor"] else None,
            po_id=ctx["po"]["po_id"] if ctx["po"] else None, candidate_po_ids=ctx["candidates"],
            checks=checks, prior_allocations=ctx["prior"], review_field=stop.review_field,
            reason_codes=ctx["reason_codes"] or [stop.code], authorization=ctx["authorization"],
            duplicate=ctx["duplicate"], po_candidates=ctx["po_candidates"], price_check=ctx["price_check"],
            vendor_match=ctx["vendor_match"], line_matches=ctx["line_matches"], policy_version=policy_version,
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
        duplicate=ctx["duplicate"], price_check=ctx["price_check"],
        vendor_match=ctx["vendor_match"], line_matches=ctx["line_matches"], policy_version=policy_version,
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
        # No SKU printed at all: the verified description identifies the charge instead (mapped later,
        # only on a printed PO). A printed but unreadable SKU still holds here.
        ident = "description" if line.sku.raw is None and line.description.value else "sku"
        for name in (ident, "quantity", "unit_price", "amount"):
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


LEGAL_SUFFIXES = ("incorporated", "inc", "l l c", "llc", "limited", "ltd", "company", "co", "corporation", "corp",
                  "plc", "llp", "lp", "pllc", "gmbh", "pty")


def split_legal_suffix(normalized: str) -> tuple[str, str]:
    """("acme field services inc") -> ("acme field services", "inc"); no suffix -> (name, "")."""
    words = normalized.split()
    for suf in sorted(LEGAL_SUFFIXES, key=lambda s: -len(s.split())):
        sw = suf.split()
        if len(words) > len(sw) and words[-len(sw):] == sw:
            return " ".join(words[:-len(sw)]), suf
    return normalized, ""


def _vendor(inv: ExtractedInvoice, snap: Snapshot, check, ctx) -> dict[str, Any]:
    """Exact normalised legal name first. Only if nothing matches exactly: a legal-suffix variant, where one
    side carries a legal suffix and the other none and the remaining names are equal (at least two words).
    It must be unique across the whole vendor master; different suffixes never match."""
    name = inv.supplier_name.value
    n = norm_name(name)
    source = lambda v: f"{v['source_file']} row {v['source_row']}"
    matches = [v for v in snap.vendors if v["legal_name_norm"] == n]
    method, extra = "exact", {}
    if not matches:
        base, suf = split_legal_suffix(n)
        variants = []
        if len(base.split()) >= 2:
            for v in snap.vendors:
                vbase, vsuf = split_legal_suffix(v["legal_name_norm"])
                if vbase == base and (suf == "") != (vsuf == ""):
                    variants.append((v, vsuf))
        if len(variants) > 1:
            names = ", ".join(f"{v['vendor_id']} {v['legal_name']}" for v, _ in variants)
            check("vendor", False, f"no exact match for {name!r}; {len(variants)} legal-suffix variants: {names}",
                  supplier=name, normalized=n, candidates=[v["vendor_id"] for v, _ in variants])
            raise _Stop("HOLD", "VENDOR_AMBIGUOUS",
                        f"Supplier {name!r} has no exact match and could be any of {names} (same name, different "
                        f"legal suffix).", "Confirm which vendor issued the invoice.", review_field="supplier_name")
        if len(variants) == 1:
            v, vsuf = variants[0]
            matches, method = [v], "legal_suffix_variant"
            extra = {"name_without_suffix": base, "suffix_printed": suf or None, "suffix_on_legal_name": vsuf or None,
                     "vendors_compared": len(snap.vendors)}
    if len(matches) != 1:
        check("vendor", False, f"{len(matches)} vendors match {name!r}", supplier=name, normalized=n)
        raise _Stop("HOLD", "VENDOR_NOT_FOUND" if not matches else "VENDOR_AMBIGUOUS",
                    f"Supplier {name!r} does not match exactly one vendor in the vendor master.",
                    "Confirm the vendor or add it to the vendor master.", review_field="supplier_name")
    v = matches[0]
    ctx["vendor_match"] = {"method": method, "printed": name, "printed_normalized": n, "vendor_id": v["vendor_id"],
                           "legal_name": v["legal_name"], "legal_name_normalized": v["legal_name_norm"],
                           "source": source(v), "evidence": inv.supplier_name.evidence.model_dump()
                           if inv.supplier_name.evidence else None, **extra}
    if v["status"] != "APPROVED":
        check("vendor", False, f"{v['vendor_id']} status is {v['status']}", vendor_id=v["vendor_id"])
        raise _Stop("HOLD", "VENDOR_NOT_APPROVED", f"Vendor {v['legal_name']} ({v['vendor_id']}) is {v['status']}.",
                    "Confirm vendor approval with procurement.")
    how = "" if method == "exact" else (
        f" (legal-suffix variant: the printed name {'omits' if extra['suffix_on_legal_name'] else 'adds'} "
        f"'{extra['suffix_on_legal_name'] or extra['suffix_printed']}'; unique among {len(snap.vendors)} vendors)")
    check("vendor", True, f"{name!r} -> {v['vendor_id']} {v['legal_name']}{how}", vendor_id=v["vendor_id"],
          source=source(v), match_method=method)
    return v


_OCR_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "S": "5", "B": "8",
                                 "Z": "2", "G": "6"})


def _ocr_canonical(number: str) -> str:
    """Collapse characters OCR commonly confuses. Used only to detect *uncertain* identity, never
    to declare two invoice numbers equal."""
    return number.upper().translate(_OCR_CONFUSABLE)


_BILLING_FIELDS_EXCLUDED = {"invoice_date"}

_MONTH_WORDS = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
                "january", "february", "march", "april", "june", "july", "august", "september", "october",
                "november", "december"}
_DESC_STOP = {"the", "and", "for", "with", "per", "from", "each", "all"}


def description_words(text: Optional[str]) -> set[str]:
    """Meaningful words of a line description: 3+ characters, not a bare number, month or filler word.
    Months and years are dropped so the same service matches in any billing month."""
    return {w for w in norm_name(text or "").split()
            if len(w) >= 3 and not w.isdigit() and w not in _MONTH_WORDS and w not in _DESC_STOP}


# Billing words that say how a charge is billed, not what the service is. They never identify a line.
_GENERIC_WORDS = {"fee", "fees", "charge", "charges", "monthly", "weekly", "quarterly", "annual", "annually", "yearly",
                  "retainer", "subscription", "plan", "service", "services", "recurring", "billing", "period",
                  "renewal", "cost", "costs", "rate", "rates", "misc", "miscellaneous", "general", "item", "items"}
_PERIOD_TOKEN = re.compile(r"^(fy|q[1-4]|h[12])?\d{2,4}$")
# Mutually exclusive qualifiers: a PO line naming one and an invoice naming another (and not the first) conflict.
_EXCLUSIVE = {w: (g, c) for g, members in {
    "frequency": {"monthly": "monthly", "weekly": "weekly", "quarterly": "quarterly", "annual": "annual",
                  "annually": "annual", "yearly": "annual"},
    "location": {"onsite": "onsite", "offsite": "offsite", "remote": "remote"},
    "role": {"primary": "primary", "secondary": "secondary", "backup": "backup"},
    "tier": {"basic": "basic", "standard": "standard", "premium": "premium", "advanced": "advanced",
             "enterprise": "enterprise"},
}.items() for w, c in members.items()}
_NEGATING_PREFIXES = ("un", "non", "dis", "in", "de", "anti", "semi", "bi", "self")
_NEGATION_WORDS = {"non", "not", "without", "excluding", "except", "excl", "excludes", "excluded"}


def _description_conflicts(inv_text: Optional[str], inv_words: set[str], po_words: set[str]) -> list[str]:
    """Qualifiers on the invoice that contradict the PO line: a negated or prefixed form of a PO word
    ("unmanaged" vs "managed"), a negation word, or a different member of an exclusive group (monthly vs annual)."""
    out = []
    for w in sorted(inv_words - po_words):
        for p in sorted(po_words):
            if any(w == pre + p or p == pre + w for pre in _NEGATING_PREFIXES):
                out.append(f"'{w}' (invoice) vs '{p}' (PO)")
    tokens = set(norm_name(inv_text or "").split())
    out += [f"negation '{t}' in the invoice description" for t in sorted(tokens & _NEGATION_WORDS)]
    po_groups = {}
    for p in po_words:
        if p in _EXCLUSIVE:
            g, c = _EXCLUSIVE[p]
            po_groups.setdefault(g, set()).add(c)
    for w in sorted(inv_words):
        if w in _EXCLUSIVE:
            g, c = _EXCLUSIVE[w]
            if g in po_groups and c not in po_groups[g]:
                out.append(f"'{w}' (invoice) vs '{'/'.join(sorted(po_groups[g]))}' (PO) [{g}]")
    return out


def match_description(description: Optional[str], po_lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Map a line with no printed SKU to a PO line by description.

    A PO line's meaningful words split into generic billing words (fee, monthly, services, ...) and identity
    words (what the service is). The line qualifies only if it has at least 2 identity words, the invoice
    description contains every one of them, and no invoice qualifier contradicts the PO line (negated forms,
    negation words, a different member of an exclusive group). Overlapping generic words never decide.
    Exactly one line on the PO must qualify; otherwise there is no match (none) or no defensible choice
    (ambiguous). Each candidate keeps the full comparison."""
    inv_words = description_words(description)
    cands = []
    for pl in po_lines:
        meaningful = {w for w in description_words(pl.get("description")) if not _PERIOD_TOKEN.match(w)}
        identity = meaningful - _GENERIC_WORDS
        present = sorted(identity & inv_words)
        missing = sorted(identity - inv_words)
        conflicts = _description_conflicts(description, inv_words, meaningful)
        problems = ([f"only {len(identity)} identifying word(s) on the PO line"] if len(identity) < 2 else []) + (
            [f"missing {', '.join(missing)}"] if missing else []) + ([f"conflicting: {'; '.join(conflicts)}"]
                                                                     if conflicts else [])
        cands.append({"po_line_id": pl["line_id"], "po_sku": pl["sku"], "po_description": pl.get("description"),
                      "po_words": sorted(identity), "generic_words": sorted(meaningful & _GENERIC_WORDS),
                      "shared_words": present, "missing_words": missing, "conflicts": conflicts,
                      "qualifies": not problems, "problems": problems,
                      "source": f"{pl['source_file']} row {pl['source_row']}" if "source_file" in pl else None})
    ok = [c for c in cands if c["qualifies"]]
    status = "matched" if len(ok) == 1 else ("ambiguous" if ok else "no_match")
    return {"status": status, "match": ok[0] if len(ok) == 1 else None, "candidates": cands,
            "invoice_words": sorted(inv_words)}


def _unpadded(number: str) -> str:
    """Invoice number with leading zeros removed from each digit group ("INV-0417" -> "INV-417"). Used only
    to find a *variant* of an accepted number; never to declare two numbers equal."""
    return re.sub(r"\d+", lambda m: str(int(m.group())), number)


def _clearly_distinct(inv: ExtractedInvoice, a: dict[str, Any], comparison: list[dict[str, Any]]) -> bool:
    """An OCR near match is a separate invoice only with clear evidence: a later verified invoice
    date AND at least one different billing detail (amounts, lines, currency or PO). Anything less
    (same date, or identical billing) stays uncertain and fails closed."""
    acc_date, inc_date = a.get("invoice_date"), inv.invoice_date.value
    later = bool(acc_date and inc_date and inc_date > acc_date)
    billing_differs = any(r["status"] == "conflict" and r["field"] not in _BILLING_FIELDS_EXCLUDED
                          for r in comparison)
    return later and billing_differs


def _accepted_record(a: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": a["run_id"], "allocation_id": a.get("id"), "invoice_number": a["invoice_number_norm"],
            "invoice_date": a.get("invoice_date"), "accepted_at": a.get("created_at"), "po_id": a["po_id"],
            "currency": a["currency"], "net_cents": a["net_cents"], "tax_cents": a["tax_cents"],
            "gross_cents": a["gross_cents"], "service_period": a.get("service_period"),
            "service_period_conf": a.get("service_period_conf")}


def _compare_with_accepted(inv: ExtractedInvoice, a: dict[str, Any],
                           snap: Optional[Snapshot] = None) -> list[dict[str, Any]]:
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
    # Lines with no printed SKU are compared through the PO line their description resolves to on the
    # accepted invoice's PO (same matcher as allocation); an unresolved line compares as "?".
    po_lines = snap.po_lines.get(a["po_id"], []) if snap else []
    keys = []
    for line in inv.lines:
        k = line.sku.value
        if k is None and po_lines:
            m = match_description(line.description.value, po_lines)
            k = m["match"]["po_sku"] if m["match"] else None
        keys.append(k)
    described = any(line.sku.value is None for line in inv.lines)
    row("po_line_skus (by description)" if described else "line_skus", sorted(k or "?" for k in keys),
        sorted(acc_lines))
    for line, sku in zip(inv.lines, keys):
        al = acc_lines.get(sku)
        if al is None:
            continue
        row(f"{sku} quantity", norm_decimal(Decimal(line.quantity.value)), norm_decimal(Decimal(al["quantity"])))
        row(f"{sku} unit_price", norm_decimal(Decimal(line.unit_price.value)), norm_decimal(Decimal(al["unit_price"])))
        row(f"{sku} amount", line.amount.value, al["net_cents"], money=True)
    return rows


def _duplicate(inv, snap: Snapshot, vendor, check, ctx, min_conf: float = 80) -> None:
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
        _number_variant(inv, snap, vendor, accepted, key, check, ctx, dup, min_conf)
        # Fail closed on uncertain identity: an OCR-read number that differs from an accepted number
        # of the same vendor only by OCR-confusable characters is not a verified distinct number.
        ev = inv.invoice_number.evidence
        if ev is not None and ev.method == "ocr":
            near = [a for a in accepted if _ocr_canonical(a["invoice_number_norm"]) == _ocr_canonical(key)]
            for a in near:
                comparison = _compare_with_accepted(inv, a, snap)
                if _clearly_distinct(inv, a, comparison):
                    # A confidently read, different number backed by a later date and different
                    # billing details is a separate invoice: record why, then run the normal checks.
                    dup.setdefault("near_matches", []).append(
                        {"accepted": _accepted_record(a), "comparison": comparison, "outcome": "distinct"})
                    continue
                dup.update(accepted=_accepted_record(a), comparison=comparison, outcome="identity_uncertain")
                ctx["po"] = snap.pos.get(a["po_id"])  # the PO the accepted invoice was allocated to
                ctx["prior"] = [{"run_id": a["run_id"], "invoice_number_norm": a["invoice_number_norm"],
                                 "po_id": a["po_id"], "net_cents": a["net_cents"], "gross_cents": a["gross_cents"]}]
                check("duplicate", False, f"OCR read {key!r}; accepted {a['invoice_number_norm']!r} differs only by "
                      f"OCR-confusable characters", accepted_run=a["run_id"])
                same = [r["field"] for r in comparison if r["status"] == "match"]
                diff = [r["field"] for r in comparison if r["status"] == "conflict"]
                raise _Stop("HOLD", "DUPLICATE_IDENTITY_UNCERTAIN",
                            f"The scanned invoice number {key!r} differs from accepted invoice "
                            f"{a['invoice_number_norm']!r} (run {a['run_id']}) only by characters OCR often confuses, "
                            f"and the document does not clearly differ (matching: {', '.join(same) or 'none'}"
                            + (f"; differing: {', '.join(diff)}" if diff else "") + ").",
                            f"Compare the printed invoice number on the PDF with {a['invoice_number_norm']}. If it is "
                            f"the same invoice, close this as a duplicate; if not, confirm the number and re-submit.",
                            review_field="invoice_number")
        check("duplicate", True, f"no accepted invoice for {vid} / {key}")
        return

    a = exact[0]
    comparison = _compare_with_accepted(inv, a, snap)
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


def _period_row(inv: ExtractedInvoice, a: dict[str, Any], min_conf: float) -> dict[str, Any]:
    """Service period compared only on reliable evidence: parsed and read at confidence >= min_conf on BOTH
    the incoming document and the accepted invoice. status: match | conflict (both reliable) | unclear (printed
    on either side but unparsed or below the floor) | missing (printed on neither)."""
    sp = inv.service_period
    inc_conf = sp.evidence.conf if sp.evidence else None
    inc_ok = sp.value is not None and inc_conf is not None and inc_conf >= min_conf
    acc_value, acc_conf, acc_raw = a.get("service_period"), a.get("service_period_conf"), a.get("service_period_raw")
    acc_ok = acc_value is not None and acc_conf is not None and acc_conf >= min_conf
    if inc_ok and acc_ok:
        status = "match" if sp.value == acc_value else "conflict"
    elif (sp.raw is not None and not inc_ok) or ((acc_value is not None or acc_raw is not None) and not acc_ok):
        status = "unclear"
    else:
        status = "missing"
    return {"field": "service_period", "incoming": sp.value, "accepted": acc_value, "money": False, "status": status,
            "incoming_raw": sp.raw, "incoming_conf": inc_conf, "accepted_raw": acc_raw, "accepted_conf": acc_conf,
            "min_conf": min_conf}


def _number_variant(inv, snap: Snapshot, vendor, accepted, key, check, ctx, dup, min_conf: float = 80) -> None:
    """A printed number equal to an accepted number of the same vendor apart from leading zeros
    ("INV-0417" vs "INV-417"). Compared on PO and billing details (currency, net, tax, gross, lines) and on the
    service period, which counts only when read reliably on both records (see _period_row).
      - billing matches, period matches reliably, same invoice date -> BLOCKED DUPLICATE_INVOICE (linked);
      - billing matches, period matches reliably, different date (re-dated copy) -> HOLD DUPLICATE_IDENTITY_UNCERTAIN;
      - billing matches but the period is unclear on either record -> HOLD DUPLICATE_IDENTITY_UNCERTAIN;
      - otherwise (different reliable period, period printed on neither, billing differs) -> recorded, and the
        normal checks run: same vendor, PO and amount alone are not enough to call it a duplicate."""
    variants = [a for a in accepted
                if a["invoice_number_norm"] != key and _unpadded(a["invoice_number_norm"]) == _unpadded(key)]
    for a in variants:
        comparison = _compare_with_accepted(inv, a, snap)
        period = _period_row(inv, a, min_conf)
        comparison.append(period)
        acc_p = a.get("service_period")
        billing_match = all(r["status"] == "match" for r in comparison if r["field"] not in ("invoice_date", "service_period"))
        same_date = next(r for r in comparison if r["field"] == "invoice_date")["status"] == "match"
        num = a["invoice_number_norm"]
        record = {"accepted": _accepted_record(a), "comparison": comparison,
                  "number_variant": {"printed": key, "accepted": num, "rule": "equal apart from leading zeros"}}
        if billing_match and period["status"] == "unclear":
            dup.update(record, outcome="variant_period_unclear")
            ctx["po"] = snap.pos.get(a["po_id"])
            ctx["prior"] = [{"run_id": a["run_id"], "invoice_number_norm": num, "po_id": a["po_id"],
                             "net_cents": a["net_cents"], "gross_cents": a["gross_cents"]}]
            show = lambda raw, val, conf: (f"\"{raw}\"" if raw else "not printed") + (
                f" read as {val}" if val else "") + (f" at confidence {conf}" if conf is not None else "")
            check("duplicate", False, f"{vendor['vendor_id']} / {key} matches accepted {num} (run {a['run_id']}) apart "
                  f"from leading zeros, but the service period is unclear", accepted_run=a["run_id"])
            raise _Stop("HOLD", "DUPLICATE_IDENTITY_UNCERTAIN",
                        f"Invoice {key} matches accepted invoice {num} (run {a['run_id']}) apart from leading zeros, with "
                        f"the same PO {a['po_id']} and billing details" + (" and invoice date" if same_date else "")
                        + f", but the service period cannot be verified (minimum confidence {min_conf}): this document "
                        f"{show(period['incoming_raw'], period['incoming'], period['incoming_conf'])}; accepted invoice "
                        f"{show(period['accepted_raw'], period['accepted'], period['accepted_conf'])}. A resend can be "
                        f"neither confirmed nor ruled out.",
                        f"Check the service period on this PDF and on {num} (run {a['run_id']}). Same period: close "
                        f"this as a duplicate. Different period: ask {vendor['legal_name']} for a distinct invoice number.",
                        review_field="service_period")
        if not (billing_match and period["status"] == "match"):
            dup.setdefault("near_matches", []).append({**record, "outcome": "variant_not_corroborated"})
            continue
        dup.update(record, outcome="variant_corroborated" if same_date else "variant_uncertain")
        ctx["po"] = snap.pos.get(a["po_id"])
        ctx["prior"] = [{"run_id": a["run_id"], "invoice_number_norm": num, "po_id": a["po_id"],
                         "net_cents": a["net_cents"], "gross_cents": a["gross_cents"]}]
        matched = ", ".join(r["field"] for r in comparison if r["status"] == "match")
        accepted_on = (a.get("created_at") or "")[:10]
        if same_date:
            check("duplicate", False, f"{vendor['vendor_id']} / {key} repeats accepted {num} (run {a['run_id']}); "
                  f"numbers equal apart from leading zeros; corroborated: {matched}", accepted_run=a["run_id"])
            raise _Stop("BLOCKED", "DUPLICATE_INVOICE",
                        f"Invoice {key} from {vendor['legal_name']} repeats invoice {num}, accepted on {accepted_on} "
                        f"(run {a['run_id']}). The numbers are the same apart from leading zeros, and the PO "
                        f"{a['po_id']}, service period {acc_p}, invoice date and billing details all match: {matched}.",
                        f"Do not pay again. Close this submission as a copy of {num} (run {a['run_id']}) and tell "
                        f"the vendor the original is already in process.", review_field="invoice_number")
        check("duplicate", False, f"{vendor['vendor_id']} / {key} matches accepted {num} (run {a['run_id']}) apart "
              f"from leading zeros and the invoice date", accepted_run=a["run_id"])
        raise _Stop("HOLD", "DUPLICATE_IDENTITY_UNCERTAIN",
                    f"Invoice {key} matches accepted invoice {num} (run {a['run_id']}) apart from leading zeros: same "
                    f"PO {a['po_id']}, service period {acc_p} and billing details, but a different invoice date "
                    f"(accepted {a.get('invoice_date')}, this document {inv.invoice_date.value}).",
                    f"Compare this document with {num} (run {a['run_id']}). If it is a re-dated copy, close it as a "
                    f"duplicate; if it is a new charge, ask {vendor['legal_name']} for a distinct invoice number.",
                    review_field="invoice_number")


def _candidate_evidence(inv: ExtractedInvoice, snap: Snapshot, po: dict[str, Any], vendor) -> dict[str, Any]:
    """Compare one vendor PO with the verified invoice lines. Pure; used only to explain a hold."""
    bal = po_balance(snap, po["po_id"])
    by_sku = {pl["sku"]: pl for pl in snap.po_lines.get(po["po_id"], [])}
    bal_lines = {b["line_id"]: b for b in bal["lines"]}
    failures: list[str] = []
    if po["status"] != "OPEN":
        failures.append(f"status is {po['status']}")
    if po["currency"] != inv.currency.value:
        failures.append(f"currency {po['currency']} vs invoice {inv.currency.value}")
    lines = []
    mapped_by: dict[str, int] = {}  # PO line id -> invoice line already mapped to it
    for i, line in enumerate(inv.lines, start=1):
        sku = line.sku.value
        pl = by_sku.get(sku)
        row: dict[str, Any] = {"invoice_line": i, "sku": sku, "invoice_quantity": line.quantity.value,
                               "invoice_unit_price": line.unit_price.value, "invoice_amount_cents": line.amount.value,
                               "po_line_id": None, "source": None}
        if pl is None:
            row.update(sku_ok=False, price_ok=False, quantity_ok=False, amount_ok=False)
            failures.append(f"no {sku} line" if sku else
                            f"invoice line {i} has no SKU (a description-only line is matched only on a printed PO)")
            lines.append(row)
            continue
        if pl["line_id"] in mapped_by:
            # Each invoice line must map to its own PO line (the rule used for an explicit PO).
            first = mapped_by[pl["line_id"]]
            row.update(po_line_id=pl["line_id"], source=f"{pl['source_file']} row {pl['source_row']}", sku_ok=False,
                       price_ok=False, quantity_ok=False, amount_ok=False, repeated_mapping_of=first)
            failures.append(f"invoice lines {first} and {i} both map to {sku} PO line {pl['line_id']} "
                            f"(each invoice line needs its own PO line)")
            lines.append(row)
            continue
        mapped_by[pl["line_id"]] = i
        b = bal_lines[pl["line_id"]]
        inv_rate, po_rate = Decimal(line.unit_price.value), Decimal(pl["unit_price"])
        variance = abs(inv_rate - po_rate)
        price_ok = variance <= po_rate * PRICE_TOLERANCE
        qty = Decimal(line.quantity.value)
        qty_ok = qty <= Decimal(b["remaining_quantity"])
        amount_ok = line.amount.value <= b["remaining_net_cents"]
        row.update(po_line_id=pl["line_id"], source=f"{pl['source_file']} row {pl['source_row']}", sku_ok=True,
                   po_unit_price=pl["unit_price"],
                   variance_pct=f"{_pct_figure(variance, po_rate):f}", price_ok=price_ok,
                   authorized_quantity=b["authorized_quantity"], allocated_quantity=b["allocated_quantity"],
                   remaining_quantity=b["remaining_quantity"], quantity_ok=qty_ok,
                   remaining_net_cents=b["remaining_net_cents"], amount_ok=amount_ok)
        if not price_ok:
            failures.append(f"{sku} price {inv_rate} vs {po_rate} (over 1%)")
        if not qty_ok:
            failures.append(f"{sku} quantity {norm_decimal(qty)} > remaining {b['remaining_quantity']}")
        if not amount_ok:
            failures.append(f"{sku} amount exceeds remaining {fmt_cents(b['remaining_net_cents'], po['currency'])}")
        lines.append(row)
    total_ok = inv.net.value <= bal["remaining_net_cents"]
    if not total_ok:
        failures.append(f"net {fmt_cents(inv.net.value, po['currency'])} exceeds remaining "
                        f"{fmt_cents(bal['remaining_net_cents'], po['currency'])}")
    return {
        "po_id": po["po_id"], "vendor_id": po["vendor_id"], "status": po["status"], "currency": po["currency"],
        "issued_on": po.get("issued_on"), "description": po.get("description", ""),
        "source": f"{po['source_file']} row {po['source_row']}",
        "authorized_net_cents": bal["authorized_net_cents"], "allocated_net_cents": bal["allocated_net_cents"],
        "remaining_net_cents": bal["remaining_net_cents"], "allocation_count": bal["allocation_count"],
        "incoming_net_cents": inv.net.value, "total_ok": total_ok, "lines": lines,
        "viable": not failures, "failures": failures,
    }


def _missing_po(inv, snap: Snapshot, vendor, check, ctx) -> None:
    """No printed PO: explain every PO of this vendor against the verified invoice lines, then hold.

    Never selects a PO. One fitting candidate is a suggestion; two or more is an ambiguity."""
    cands = [_candidate_evidence(inv, snap, po, vendor)
             for po in sorted(snap.pos.values(), key=lambda p: p["po_id"]) if po["vendor_id"] == vendor["vendor_id"]]
    viable = [c["po_id"] for c in cands if c["viable"]]
    dn = inv.delivery_reference
    delivery = None
    if dn.value and dn.evidence is not None and dn.evidence.conf >= 80:  # same floor as critical fields
        delivery = dn.value
    ctx["candidates"] = viable
    ctx["po_candidates"] = {
        "delivery_reference": delivery,
        "delivery_reference_evidence": dn.evidence.model_dump() if dn.evidence is not None else None,
        "invoice_lines": [{"line": i, "sku": l.sku.value, "quantity": l.quantity.value, "unit_price": l.unit_price.value,
                           "amount_cents": l.amount.value} for i, l in enumerate(inv.lines, start=1)],
        "invoice_net_cents": inv.net.value, "currency": inv.currency.value,
        "criteria": "approved vendor, OPEN status, same currency, every invoice SKU on the PO, unit price within 1%, "
                    "quantity and net within remaining authorization",
        "candidates": cands, "viable_po_ids": viable,
    }
    for c in cands:
        check("po_candidate", c["viable"], f"{c['po_id']}: " + ("fits every criterion" if c["viable"]
                                                                 else "; ".join(c["failures"])),
              po_id=c["po_id"], source=c["source"])
    dn_text = f"delivery {delivery}" if delivery else "this delivery"
    lines_text = ", ".join(f"{l.quantity.value} x {l.sku.value} at {l.unit_price.value}" for l in inv.lines)
    if len(viable) >= 2:
        raise _Stop("HOLD", "PO_AMBIGUOUS",
                    f"No PO is printed on the invoice and {len(viable)} open POs for {vendor['legal_name']} fit it "
                    f"equally ({', '.join(viable)}): each covers {lines_text} within its remaining authorization. "
                    f"The invoice does not say which one applies.",
                    f"Ask procurement which PO covers {dn_text}: {' or '.join(viable)}. "
                    f"Re-submit with the confirmed PO; nothing is allocated until then.",
                    review_field="po_reference")
    if len(viable) == 1:
        raise _Stop("HOLD", "PO_MISSING",
                    f"No PO is printed on the invoice. One open PO fits ({viable[0]}), but a missing PO is never "
                    f"assigned automatically.",
                    f"Ask procurement to confirm that {viable[0]} covers {dn_text}, then re-submit "
                    f"with the PO.", review_field="po_reference")
    why = "; ".join(f"{c['po_id']}: {', '.join(c['failures'])}" for c in cands) or "the vendor has no POs"
    raise _Stop("HOLD", "PO_MISSING_NO_MATCH",
                f"No PO is printed on the invoice and no PO for {vendor['legal_name']} fits it ({why}).",
                f"Ask procurement which PO covers {dn_text}, or whether a new PO is needed.",
                review_field="po_reference")


def _po(inv, snap: Snapshot, vendor, check, ctx) -> dict[str, Any]:
    ref = inv.po_reference.value
    if ref is None:
        _missing_po(inv, snap, vendor, check, ctx)
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


def _line_mapping(inv, snap: Snapshot, po, check, ctx) -> list[tuple[Any, dict[str, Any]]]:
    po_lines = snap.po_lines.get(po["po_id"], [])
    by_sku = {pl["sku"]: pl for pl in po_lines}
    by_id = {pl["line_id"]: pl for pl in po_lines}
    mapping = []
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    ctx["line_matches"] = records
    for i, line in enumerate(inv.lines, start=1):
        if line.sku.value is None and line.sku.raw is None:
            # No SKU printed: resolve by verified description on the printed PO; the extracted SKU stays null.
            m = match_description(line.description.value, po_lines)
            ev = line.description.evidence
            rec = {"line": i, "method": "description", "invoice_sku": None, "invoice_description": line.description.value,
                   "invoice_description_evidence": ev.model_dump() if ev else None, "status": m["status"],
                   "invoice_words": m["invoice_words"], "candidates": m["candidates"], "po_id": po["po_id"],
                   "rule": "every identifying word of the PO line (at least 2) present, no conflicting qualifier; "
                           "exactly one qualifying line"}
            records.append(rec)
            hit = m["match"]
            if hit is None or hit["po_line_id"] in seen:
                compared = "; ".join(f"line {c['po_line_id']} {c['po_sku']} \"{c['po_description']}\": "
                                     + ", ".join(c["problems"]) for c in m["candidates"] if c["problems"])
                why = ("its description fits more than one line: " + ", ".join(
                    f"line {c['po_line_id']} {c['po_sku']}" for c in m["candidates"] if c["qualifies"])
                       if m["status"] == "ambiguous" else
                       "it is already mapped to another invoice line" if hit else
                       f"its description \"{line.description.value}\" does not clearly match any line ({compared})")
                check("line_mapping", False, f"line {i} (no SKU printed) on {po['po_id']}: {why}",
                      description=line.description.value, candidates=m["candidates"])
                raise _Stop("HOLD", "LINE_NOT_ON_PO",
                            f"Invoice line {i} has no SKU and {why} on {po['po_id']}.",
                            "Confirm which PO line this charge belongs to.", review_field=f"line {i} description")
            pl = by_id[hit["po_line_id"]]
            seen.add(pl["line_id"])
            mapping.append((line, pl))
            rec.update(po_line_id=pl["line_id"], po_sku=pl["sku"], po_description=pl.get("description"),
                       shared_words=hit["shared_words"], po_words=hit["po_words"], missing_words=hit["missing_words"],
                       conflicts=hit["conflicts"], generic_words=hit["generic_words"],
                       source=f"{pl['source_file']} row {pl['source_row']}")
            check("line_mapping", True, f"line {i} (no SKU printed) -> {po['po_id']} line {pl['line_id']} {pl['sku']} "
                  f"by description: contains every identifying word of the PO line ({', '.join(hit['shared_words'])}), "
                  f"no conflicting qualifier; the only qualifying line",
                  po_line=pl["line_id"], source=rec["source"], match_method="description")
            continue
        pl = by_sku.get(line.sku.value)
        if pl is None or pl["line_id"] in seen:
            check("line_mapping", False, f"line {i} SKU {line.sku.value} has no unique line on {po['po_id']}",
                  sku=line.sku.value, po_skus=sorted(by_sku))
            raise _Stop("HOLD", "LINE_NOT_ON_PO",
                        f"Invoice line {i} ({line.sku.value}) does not map to a line on {po['po_id']}.",
                        "Confirm which PO line this charge belongs to.", review_field=f"line {i} sku")
        seen.add(pl["line_id"])
        mapping.append((line, pl))
        records.append({"line": i, "method": "sku", "invoice_sku": line.sku.value, "po_line_id": pl["line_id"],
                        "po_sku": pl["sku"], "source": f"{pl['source_file']} row {pl['source_row']}"})
        check("line_mapping", True, f"line {i} {line.sku.value} -> {po['po_id']} line {pl['line_id']}",
              sku=line.sku.value, po_line=pl["line_id"], source=f"{pl['source_file']} row {pl['source_row']}")
    return mapping


_MAX_PCT_PLACES = 6


def _pct_figure(delta: Decimal, po_rate: Decimal, places: int = 2) -> Decimal:
    """Variance of `delta` against `po_rate` in percent, rounded for display only.

    Starts at `places` decimals and adds more until the figure sits on the same side of the tolerance
    limit as the exact values (above, at, or within), so +1.001% never reads as "+1.00%" and +0.9999%
    never reads as "+1.00%" either. The side comes from the same exact Decimal comparison the price
    check uses; the rounded figure never decides anything."""
    if not po_rate:
        return Decimal(0).quantize(Decimal(1).scaleb(-places))
    limit_amount = po_rate * PRICE_TOLERANCE
    side = (abs(delta) > limit_amount) - (abs(delta) < limit_amount)
    pct, limit_pct = delta / po_rate * 100, PRICE_TOLERANCE * 100
    for p in range(places, max(places, _MAX_PCT_PLACES) + 1):
        q = pct.quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP)
        if (abs(q) > limit_pct) - (abs(q) < limit_pct) == side:
            break
    else:  # still level with the limit after rounding: round away from it, towards the exact side
        q = pct.quantize(Decimal(1).scaleb(-p), rounding=ROUND_UP if side > 0 else ROUND_DOWN)
    return abs(q) if q == 0 else q  # no "-0.00"


def _signed_pct(delta: Decimal, po_rate: Decimal) -> str:
    q = _pct_figure(delta, po_rate)
    return f"{'+' if q > 0 else ''}{q:f}%"


def _fmt_rate(rate: str, cur: str) -> str:
    """Exact unit price with at least two decimals (never rounded to cents)."""
    d = Decimal(rate)
    q = d if -d.as_tuple().exponent > 2 else d.quantize(Decimal("0.01"))
    return f"{cur} {q:,}"


def _signed_money(cents: int, cur: str) -> str:
    return ("+" if cents > 0 else "") + fmt_cents(cents, cur)


def _price(inv, po, mapping, check, ctx) -> None:
    """Unit price vs PO price on every mapped line (inclusive 1% per line), with no early exit.

    Each comparison keeps the invoice evidence and the PO line's CSV source row. Line differences are
    pre-tax and measured at the invoiced quantity; tax is reported separately."""
    cur = po["currency"]
    rows: list[dict[str, Any]] = []
    for line, pl in mapping:
        qty = Decimal(line.quantity.value)
        inv_rate, po_rate = Decimal(line.unit_price.value), Decimal(pl["unit_price"])
        delta = inv_rate - po_rate
        ok = abs(delta) <= po_rate * PRICE_TOLERANCE
        at_po_price = decimal_to_cents(qty * po_rate)
        diff_cents = line.amount.value - at_po_price
        ev = line.unit_price.evidence
        row = {"line": line.row_index + 1, "sku": line.sku.value or pl["sku"], "invoice_sku": line.sku.value,
               "po_id": po["po_id"], "po_line_id": pl["line_id"],
               "quantity": norm_decimal(qty), "invoice_unit_price": norm_decimal(inv_rate),
               "po_unit_price": norm_decimal(po_rate), "variance_pct": f"{_pct_figure(delta, po_rate, 4):f}",
               "variance_display": _signed_pct(delta, po_rate),
               "invoice_amount_cents": line.amount.value, "amount_at_po_price_cents": at_po_price,
               "amount_difference_cents": diff_cents, "within_tolerance": ok,
               "invoice_evidence": ev.model_dump() if ev is not None else None,
               "invoice_amount_evidence": line.amount.evidence.model_dump() if line.amount.evidence else None,
               "po_source": f"{pl['source_file']} row {pl['source_row']}"}
        rows.append(row)
        check("price", ok, f"line {row['line']} {row['sku']}: invoice {row['invoice_unit_price']} vs PO "
              f"{row['po_unit_price']} ({row['variance_display']}, limit 1%); {_signed_money(diff_cents, cur)} at "
              f"{row['quantity']} units", sku=row["sku"], invoice_rate=row["invoice_unit_price"],
              po_rate=row["po_unit_price"], variance_pct=row["variance_pct"], amount_difference_cents=diff_cents,
              source=row["po_source"])
    failed = [r for r in rows if not r["within_tolerance"]]
    at_po_total = sum(r["amount_at_po_price_cents"] for r in rows)
    net_diff = inv.net.value - at_po_total
    gross_line_diff = sum(abs(r["amount_difference_cents"]) for r in failed)
    offsetting = bool(failed) and abs(net_diff) < gross_line_diff
    ctx["price_check"] = {
        "po_id": po["po_id"], "currency": cur, "tolerance_pct": "1", "tolerance_rule": "per line, inclusive",
        "lines": rows, "failed_lines": [r["line"] for r in failed],
        "subtotal_cents": inv.net.value, "subtotal_at_po_prices_cents": at_po_total, "subtotal_difference_cents": net_diff,
        "offsetting": offsetting, "tax_cents": inv.tax.value or 0, "gross_cents": inv.gross.value,
        "note": "Line differences are pre-tax at the invoiced quantity. PO authorization and allocations are pre-tax; "
                "tax is shown separately.",
    }
    if not failed:
        return
    parts = [f"line {r['line']} {r['sku']} at {_fmt_rate(r['invoice_unit_price'], cur)} vs "
             f"PO {_fmt_rate(r['po_unit_price'], cur)} ({r['variance_display']}, "
             f"{_signed_money(r['amount_difference_cents'], cur)} at {r['quantity']} units)" for r in failed]
    n = len(failed)
    reason = (f"{n} line{'s are' if n > 1 else ' is'} outside the 1% unit-price tolerance on {po['po_id']}: "
              + "; ".join(parts) + ".")
    if offsetting:
        reason += (f" The differences offset: the subtotal {fmt_cents(inv.net.value, cur)} is "
                   + ("the same as" if net_diff == 0 else f"only {_signed_money(net_diff, cur)} from")
                   + f" the {fmt_cents(at_po_total, cur)} the lines would cost at PO prices, but each line is checked "
                   f"on its own price.")
    reason += f" Tax {fmt_cents(inv.tax.value or 0, cur)} is separate and not part of the comparison."
    items = [f"{r['sku']} (line {r['line']}) at {_fmt_rate(r['po_unit_price'], cur)}" for r in failed]
    rebill = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    po_lines = " and ".join(f"line {r['po_line_id']} ({r['sku']})" for r in failed)
    action = (f"Do not pay. Ask {ctx['vendor']['legal_name']} to re-bill at the PO price{'s' if len(items) > 1 else ''}: "
              f"{rebill}, or ask the buyer "
              f"to approve new prices on {po['po_id']} {po_lines}. Do not accept the lines netted against each other.")
    raise _Stop("HOLD", "PRICE_VARIANCE", reason, action, review_field=f"line {failed[0]['line']} unit_price")


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
    po_room = next(c for c in comparisons if c["measure"] == "net" and c["scope"] == "po")
    line_net = {c["line_id"]: c for c in comparisons if c["measure"] == "net" and c["scope"] == "line"}
    limits = [f"{c['sku']} line: {fmt_cents(max(line_net[c['line_id']]['remaining'], 0), cur)} net / "
              f"{norm_decimal(max(Decimal(c['remaining']), Decimal(0)))} units"
              for c in comparisons if c["measure"] == "quantity"]
    room = f"{po['po_id']} total: {fmt_cents(max(po_room['remaining'], 0), cur)} net remaining" + (
        "; " + "; ".join(limits) if limits else "")
    action = (f"Do not pay. Ask the PO owner for a change order on {po['po_id']} adding {' and '.join(asks)}, "
              f"or ask {ctx['vendor']['legal_name']} for a corrected invoice or credit note that fits the remaining "
              f"authorization ({room}). Re-submit when either arrives.")

    if money:
        primary = next((c for c in money if c["scope"] == "po"), money[0])
    else:
        primary = qty[0]
    if primary["scope"] == "po":
        review = "net"
    else:
        review = f"line {line_row[primary['line_id']]} " + ("amount" if primary["measure"] == "net" else "quantity")
    raise _Stop("HOLD", code, reason, action, review_field=review)
