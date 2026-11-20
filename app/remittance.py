"""Remittance (payment instruction) helpers: masking and comparison with the vendor master.

Full bank identifiers (routing and account numbers) are kept only in the database. Anything shown to a
person or written into a decision uses the masked form, which keeps the last four digits.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_MASK_CHARS = "*xX•"


def digits(value: Optional[str]) -> str:
    return re.sub(r"\D", "", value or "")


def is_masked(value: str) -> bool:
    return any(c in value for c in _MASK_CHARS)


def mask(identifier: Optional[str]) -> Optional[str]:
    """"000123456789" -> "****6789"; an already masked "****7734" stays "****7734"."""
    if not identifier:
        return None
    tail = digits(identifier)[-4:]
    return "****" + tail if tail else "****"


def same_identifier(a: str, b: str) -> bool:
    """One identifier printed two ways: equal digits once spaces/hyphens are dropped, or a masked reference whose
    visible digits (at least four) end the full number."""
    da, db_ = digits(a), digits(b)
    if is_masked(a) and is_masked(b):
        return da == db_
    if is_masked(a) or is_masked(b):
        shown, full = (da, db_) if is_masked(a) else (db_, da)
        return len(shown) >= 4 and full.endswith(shown)
    return da == db_


def distinct_identifiers(values: list[str]) -> list[str]:
    """Distinct identifiers in printed order; a full number is kept in preference to a masked copy of it."""
    out: list[str] = []
    for v in values:
        i = next((k for k, o in enumerate(out) if same_identifier(o, v)), None)
        if i is None:
            out.append(v)
        elif is_masked(out[i]) and not is_masked(v):
            out[i] = v
    return out


def _identifier_pattern(full: str) -> re.Pattern[str]:
    # The same digits in any grouping: contiguous, hyphenated, spaced (one separator at most between digits).
    return re.compile(r"(?<![0-9])" + r"[ \-‐–—]?".join(full) + r"(?![0-9])")


def redact_text(text: Optional[str], identifiers: Any = ()) -> Optional[str]:
    """Mask recognized identifiers (full routing/account numbers read from the invoice) in any grouping, then every
    remaining run of five or more digits. Grouped numbers that are not recognized identifiers (phone numbers,
    dates) are left readable."""
    if text is None:
        return None
    for full in sorted(identifiers, key=len, reverse=True):
        text = _identifier_pattern(full).sub(mask(full), text)
    return re.sub(r"\d{5,}", lambda m: mask(m.group()), text)


def redact_identifiers(obj: Any, identifiers: Any) -> Any:
    """Copy of a JSON-like value with recognized identifiers masked in every string, at any depth."""
    if not identifiers:
        return obj
    if isinstance(obj, str):
        for full in sorted(identifiers, key=len, reverse=True):
            obj = _identifier_pattern(full).sub(mask(full), obj)
        return obj
    if isinstance(obj, dict):
        return {k: redact_identifiers(v, identifiers) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_identifiers(v, identifiers) for v in obj]
    return obj


def compare_identifier(printed: Optional[str], on_file: Optional[str]) -> str:
    """match | masked_match (printed masked, visible digits end the number on file) | mismatch |
    not_printed | not_on_file | inconclusive (masked with fewer than four visible digits)."""
    if not printed:
        return "not_printed"
    if not on_file:
        return "not_on_file"
    pd, fd = digits(printed), digits(on_file)
    if is_masked(printed):
        if len(pd) < 4:
            return "inconclusive"
        return "masked_match" if fd.endswith(pd) else "mismatch"
    return "match" if pd == fd else "mismatch"


def redact_field(field: dict[str, Any], identifier: bool, identifiers: Any = ()) -> dict[str, Any]:
    """A serialized Field_ with identifiers masked in raw/value and in evidence text and notes."""
    out = dict(field)
    if identifier:
        out["raw"] = mask(out.get("raw")) if out.get("raw") else out.get("raw")
        out["value"] = mask(out.get("value")) if out.get("value") else out.get("value")
    else:
        out["raw"] = redact_text(out.get("raw"), identifiers)
        out["value"] = redact_text(out.get("value"), identifiers) if isinstance(out.get("value"), str) else out.get("value")
    if out.get("evidence"):
        ev = dict(out["evidence"])
        ev["text"] = redact_text(ev.get("text"), identifiers)
        out["evidence"] = ev
    out["note"] = redact_text(out.get("note"), identifiers)
    return out


REMITTANCE_FIELDS = {"remit_bank_name": False, "remit_routing": True, "remit_account": True, "remittance_notice": False}
IDENTIFIER_FIELDS = ("remit_routing", "remit_account")


def extraction_identifiers(invoice: Any) -> set[str]:
    """Full (unmasked) routing/account numbers read from an invoice, as digit strings of five or more digits.
    Accepts an ExtractedInvoice or its serialized dict (older extractions without `remit_readings` work too)."""
    inv = invoice.model_dump() if hasattr(invoice, "model_dump") else (invoice or {})
    found: set[str] = set()
    for name in IDENTIFIER_FIELDS:
        fields = [inv.get(name) or {}] + list((inv.get("remit_readings") or {}).get(name) or [])
        for f in fields:
            for v in (f.get("raw"), f.get("value")):
                if isinstance(v, str) and not is_masked(v) and len(digits(v)) >= 5:
                    found.add(digits(v))
    return found


def redact_extraction(extraction: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Copy of a stored extraction with remittance identifiers masked, for API and UI output."""
    if not extraction or "invoice" not in extraction:
        return extraction
    out = dict(extraction)
    inv = dict(out["invoice"])
    idents = extraction_identifiers(inv)
    for name, identifier in REMITTANCE_FIELDS.items():
        if inv.get(name):
            inv[name] = redact_field(inv[name], identifier, idents)
    if inv.get("remit_readings"):
        inv["remit_readings"] = {k: [redact_field(f, True, idents) for f in v] for k, v in inv["remit_readings"].items()}
    inv["issues"] = [redact_text(i, idents) for i in inv.get("issues", [])]
    out["invoice"] = redact_identifiers(inv, idents)
    return out


def redact_vendor(vendor: dict[str, Any]) -> dict[str, Any]:
    out = dict(vendor)
    for k in ("routing_number", "account_number"):
        if out.get(k):
            out[k] = mask(out[k])
    return out
