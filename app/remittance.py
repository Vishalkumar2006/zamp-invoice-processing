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


def redact_text(text: Optional[str]) -> Optional[str]:
    """Mask every run of five or more digits in free text (evidence snippets, notices)."""
    if text is None:
        return None
    return re.sub(r"\d{5,}", lambda m: mask(m.group()), text)


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


def redact_field(field: dict[str, Any], identifier: bool) -> dict[str, Any]:
    """A serialized Field_ with identifiers masked in raw/value and long digit runs masked in evidence text."""
    out = dict(field)
    if identifier:
        out["raw"] = mask(out.get("raw")) if out.get("raw") else out.get("raw")
        out["value"] = mask(out.get("value")) if out.get("value") else out.get("value")
    else:
        out["raw"] = redact_text(out.get("raw"))
        out["value"] = redact_text(out.get("value")) if isinstance(out.get("value"), str) else out.get("value")
    if out.get("evidence"):
        ev = dict(out["evidence"])
        ev["text"] = redact_text(ev.get("text"))
        out["evidence"] = ev
    out["note"] = redact_text(out.get("note"))
    return out


REMITTANCE_FIELDS = {"remit_bank_name": False, "remit_routing": True, "remit_account": True, "remittance_notice": False}


def redact_extraction(extraction: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Copy of a stored extraction with remittance identifiers masked, for API and UI output."""
    if not extraction or "invoice" not in extraction:
        return extraction
    out = dict(extraction)
    inv = dict(out["invoice"])
    for name, identifier in REMITTANCE_FIELDS.items():
        if inv.get(name):
            inv[name] = redact_field(inv[name], identifier)
    inv["issues"] = [redact_text(i) for i in inv.get("issues", [])]
    out["invoice"] = inv
    return out


def redact_vendor(vendor: dict[str, Any]) -> dict[str, Any]:
    out = dict(vendor)
    for k in ("routing_number", "account_number"):
        if out.get(k):
            out[k] = mask(out[k])
    return out
