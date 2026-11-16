"""Procurement CSV import (vendors, purchase orders, PO lines) into a fresh workspace.

All three files are validated together and committed in one transaction, or not at all.
Every stored row keeps its source filename and row number for evidence.
"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .db import now_iso, transaction
from .money import csv_money_cents, norm_decimal

VENDOR_COLS = ["vendor_id", "legal_name", "status"]
PO_COLS = ["po_id", "vendor_id", "issued_on", "status", "currency", "description", "authorized_net"]
LINE_COLS = ["po_id", "line_id", "sku", "description", "authorized_quantity", "unit_price", "authorized_net"]


class ImportError_(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def norm_name(name: str) -> str:
    """Normalise a legal name for comparison: case, punctuation and whitespace insensitive."""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _read(content: bytes, filename: str, required: list[str], errors: list[str]) -> list[tuple[int, dict]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        errors.append(f"{filename}: not UTF-8 text")
        return []
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in required if c not in header]
    if missing:
        errors.append(f"{filename}: missing columns {missing}")
        return []
    rows = []
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        rows.append((i, row))
    if not rows:
        errors.append(f"{filename}: no data rows")
    return rows


def _dec(value: str, where: str, errors: list[str]) -> Decimal | None:
    try:
        d = Decimal(value)
    except InvalidOperation:
        errors.append(f"{where}: {value!r} is not a number")
        return None
    if d < 0:
        errors.append(f"{where}: negative value {value!r}")
        return None
    return d


def _cents(value: str, where: str, errors: list[str]) -> int | None:
    d = _dec(value, where, errors)
    if d is None:
        return None
    try:
        return csv_money_cents(value)
    except ValueError as exc:
        errors.append(f"{where}: {exc}")
        return None


@dataclass
class Parsed:
    vendors: list[dict[str, Any]] = field(default_factory=list)
    pos: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)


def validate(files: dict[str, tuple[str, bytes]]) -> Parsed:
    """files: role -> (filename, content); roles are vendors, purchase_orders, po_lines."""
    errors: list[str] = []
    out = Parsed()
    vf, vc = files["vendors"]
    pf, pc = files["purchase_orders"]
    lf, lc = files["po_lines"]

    seen_v: set[str] = set()
    for rn, r in _read(vc, vf, VENDOR_COLS, errors):
        where = f"{vf} row {rn}"
        if not r["vendor_id"] or not r["legal_name"]:
            errors.append(f"{where}: vendor_id and legal_name are required")
            continue
        if r["vendor_id"] in seen_v:
            errors.append(f"{where}: duplicate vendor_id {r['vendor_id']}")
            continue
        seen_v.add(r["vendor_id"])
        out.vendors.append({"vendor_id": r["vendor_id"], "legal_name": r["legal_name"],
                            "legal_name_norm": norm_name(r["legal_name"]), "status": r["status"].upper(),
                            "source_file": vf, "source_row": rn})

    seen_po: set[str] = set()
    for rn, r in _read(pc, pf, PO_COLS, errors):
        where = f"{pf} row {rn}"
        if not r["po_id"]:
            errors.append(f"{where}: po_id is required")
            continue
        if r["po_id"] in seen_po:
            errors.append(f"{where}: duplicate po_id {r['po_id']}")
            continue
        seen_po.add(r["po_id"])
        if r["vendor_id"] not in seen_v:
            errors.append(f"{where}: vendor_id {r['vendor_id']!r} not in {vf}")
        if not re.fullmatch(r"[A-Z]{3}", r["currency"]):
            errors.append(f"{where}: currency {r['currency']!r} is not a 3-letter ISO code")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["issued_on"]):
            errors.append(f"{where}: issued_on {r['issued_on']!r} is not YYYY-MM-DD")
        cents = _cents(r["authorized_net"], f"{where} authorized_net", errors)
        out.pos.append({"po_id": r["po_id"], "vendor_id": r["vendor_id"], "issued_on": r["issued_on"],
                        "status": r["status"].upper(), "currency": r["currency"], "description": r["description"],
                        "authorized_net_cents": cents, "source_file": pf, "source_row": rn})

    seen_l: set[tuple[str, str]] = set()
    seen_sku: set[tuple[str, str]] = set()
    for rn, r in _read(lc, lf, LINE_COLS, errors):
        where = f"{lf} row {rn}"
        key = (r["po_id"], r["line_id"])
        if key in seen_l:
            errors.append(f"{where}: duplicate line {key}")
            continue
        seen_l.add(key)
        if r["po_id"] not in seen_po:
            errors.append(f"{where}: po_id {r['po_id']!r} not in {pf}")
        sku = r["sku"].strip().upper()
        if not sku:
            errors.append(f"{where}: sku is required")
        elif (r["po_id"], sku) in seen_sku:
            errors.append(f"{where}: sku {sku} repeated on {r['po_id']}")
        seen_sku.add((r["po_id"], sku))
        qty = _dec(r["authorized_quantity"], f"{where} authorized_quantity", errors)
        price = _dec(r["unit_price"], f"{where} unit_price", errors)
        cents = _cents(r["authorized_net"], f"{where} authorized_net", errors)
        if qty is not None and price is not None and cents is not None and qty * price * 100 != cents:
            errors.append(f"{where}: authorized_quantity x unit_price ({qty * price}) != authorized_net "
                          f"({r['authorized_net']})")
        out.lines.append({"po_id": r["po_id"], "line_id": r["line_id"], "sku": sku, "description": r["description"],
                          "authorized_quantity": norm_decimal(qty) if qty is not None else None,
                          "unit_price": norm_decimal(price) if price is not None else None,
                          "authorized_net_cents": cents, "source_file": lf, "source_row": rn})

    # Header-to-line reconciliation.
    for po in out.pos:
        total = sum(l["authorized_net_cents"] or 0 for l in out.lines if l["po_id"] == po["po_id"])
        if not any(l["po_id"] == po["po_id"] for l in out.lines):
            errors.append(f"{pf}: {po['po_id']} has no lines in {lf}")
        elif po["authorized_net_cents"] is not None and total != po["authorized_net_cents"]:
            errors.append(f"{pf}: {po['po_id']} authorized_net {po['authorized_net_cents'] / 100:.2f} != "
                          f"sum of line authorized_net {total / 100:.2f}")
    if errors:
        raise ImportError_(errors)
    return out


def import_procurement(conn, files: dict[str, tuple[str, bytes]]) -> dict[str, Any]:
    parsed = validate(files)
    ws = "ws_" + uuid.uuid4().hex[:12]
    with transaction(conn):
        conn.execute("INSERT INTO workspaces (id, created_at, source_files_json) VALUES (?,?,?)",
                     (ws, now_iso(), json.dumps({role: f[0] for role, f in files.items()})))
        conn.executemany(
            "INSERT INTO vendors VALUES (:ws,:vendor_id,:legal_name,:legal_name_norm,:status,:source_file,:source_row)",
            [{**v, "ws": ws} for v in parsed.vendors])
        conn.executemany(
            "INSERT INTO purchase_orders VALUES (:ws,:po_id,:vendor_id,:issued_on,:status,:currency,:description,"
            ":authorized_net_cents,:source_file,:source_row)", [{**p, "ws": ws} for p in parsed.pos])
        conn.executemany(
            "INSERT INTO po_lines VALUES (:ws,:po_id,:line_id,:sku,:description,:authorized_quantity,:unit_price,"
            ":authorized_net_cents,:source_file,:source_row)", [{**l, "ws": ws} for l in parsed.lines])
    return {"workspace_id": ws, "vendors": len(parsed.vendors), "purchase_orders": len(parsed.pos),
            "po_lines": len(parsed.lines)}
