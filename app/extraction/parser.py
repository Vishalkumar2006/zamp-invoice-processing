"""Layout + label based invoice parser.

Turns positioned tokens (native or OCR) into one structured invoice. Each value keeps the
raw printed text and its page/bbox/confidence evidence. Values are only taken from their
labelled header field or from a row of the line-item table; a number printed elsewhere on
the page is never used. Ambiguity is recorded in `issues` rather than guessed.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable, Optional

from ..models import Evidence, ExtractedInvoice, Field_, LineItem, Token
from ..money import norm_decimal, parse_decimal_amount, parse_money_cents, parse_quantity
from ..remittance import digits as _digits


# ----------------------------------------------------------------------------- geometry

def _norm(text: str, keep_hash: bool = False) -> str:
    """Lower-case, collapse spaces and drop trailing label punctuation. With keep_hash, a trailing
    "#" survives ("Invoice #:" -> "invoice #"), because there it is the word that names the field."""
    s = text.lower().replace("–", "-").replace("—", "-")
    s = re.sub(r":+$" if keep_hash else r"[:#]+$", "", s.strip())
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .:")


def estimate_skew(tokens: list[Token]) -> float:
    """Median dy/dx over the OCR engine's own text lines (0 for native text)."""
    groups: dict[str, list[Token]] = {}
    for t in tokens:
        if t.group:
            groups.setdefault(t.group, []).append(t)
    slopes: list[float] = []
    for toks in groups.values():
        if len(toks) < 2:
            continue
        xs = [t.cx for t in toks]
        ys = [t.cy for t in toks]
        if max(xs) - min(xs) < 100:
            continue
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            continue
        slopes.append(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den)
    if not slopes:
        return 0.0
    slope = statistics.median(slopes)
    return slope if abs(slope) <= 0.05 else 0.0


@dataclass
class Segment:
    tokens: list[Token]

    @property
    def text(self) -> str:
        return " ".join(t.text for t in self.tokens)

    @property
    def x0(self) -> float:
        return min(t.x0 for t in self.tokens)

    @property
    def x1(self) -> float:
        return max(t.x1 for t in self.tokens)

    @property
    def height(self) -> float:
        return statistics.median(t.h for t in self.tokens)


@dataclass
class Row:
    page: int
    y: float  # skew-corrected centre
    tokens: list[Token] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(t.text for t in self.tokens)


def build_rows(tokens: list[Token], slope: float) -> list[Row]:
    if not tokens:
        return []
    med_h = statistics.median(t.h for t in tokens)
    tol = 0.55 * med_h
    corr = sorted(tokens, key=lambda t: t.cy - slope * t.cx)
    rows: list[Row] = []
    for t in corr:
        y = t.cy - slope * t.cx
        if rows and abs(y - rows[-1].y) <= tol:
            r = rows[-1]
            r.tokens.append(t)
            r.y = statistics.fmean(tt.cy - slope * tt.cx for tt in r.tokens)
        else:
            rows.append(Row(page=t.page, y=y, tokens=[t]))
    for r in rows:
        r.tokens.sort(key=lambda t: t.x0)
        row_h = statistics.median(t.h for t in r.tokens)
        gap_limit = 1.1 * row_h
        segs: list[list[Token]] = [[r.tokens[0]]]
        for prev, cur in zip(r.tokens, r.tokens[1:]):
            if cur.x0 - prev.x1 > gap_limit:
                segs.append([cur])
            else:
                segs[-1].append(cur)
        r.segments = [Segment(s) for s in segs]
    return rows


def evidence_for(tokens: list[Token], label: Optional[str] = None) -> Evidence:
    return Evidence(
        page=tokens[0].page, method=tokens[0].method, text=" ".join(t.text for t in tokens), label=label,
        bbox=(round(min(t.x0 for t in tokens), 1), round(min(t.y0 for t in tokens), 1),
              round(max(t.x1 for t in tokens), 1), round(max(t.y1 for t in tokens), 1)),
        conf=round(min(t.conf for t in tokens), 1),
    )


# ----------------------------------------------------------------------------- labels

_CUR = r"(?: \((?P<cur>[a-z]{3})\))?"
LABELS: dict[str, re.Pattern[str]] = {
    "invoice_number": re.compile(r"^(invoice (number|no\.?|num|#)|inv\.? (no\.?|#)|invoice id)$"),
    "invoice_date": re.compile(r"^(invoice date|date of issue|issue date|date issued|date)$"),
    # The buyer's own PO, also printed by vendors as "Customer PO", "Client P.O. #", "Your PO no." etc.
    "po_reference": re.compile(
        r"^((customer|client|buyer|your) )?"
        r"(purchase order( (number|no\.?|#|ref(erence)?))?|p\.?o\.?( (number|no\.?|#|ref(erence)?))?)$"),
    "net": re.compile(r"^(sub ?total|net( total| amount)?|total before tax|subtotal \(excl\.? tax\)|merchandise( total)?)"
                      + _CUR + "$"),
    "tax": re.compile(r"^((sales )?tax|vat|gst)( \(?[\d.]+ ?%\)?)?" + _CUR + "$"),
    "gross": re.compile(r"^((grand |invoice )?total( due| amount)?|(amount|balance) due)" + _CUR + "$"),
    "service_period": re.compile(r"^(service period|billing period|period of service|service dates?)$"),
}
TOTAL_KEYS = ("net", "tax", "gross")
# Spaces in label patterns are optional so OCR word splits/joins ("S ubtotal", "Invoicenumber")
# still match when the label text is compared with its spaces removed.
LABELS = {k: re.compile(p.pattern.replace(" ", " ?")) for k, p in LABELS.items()}
# Labels we do not extract but whose values must not be mistaken for other labels
# (e.g. "Terms  Net 30" must not read as a "Net" total of 30).
OTHER_LABELS = re.compile(
    r"^((payment )?terms|payment due|due date|due|ship(ped)? to|bill(ed)? to|sold to|customer( (no\.?|id|number))?|"
    r"account( (no\.?|number))?|delivery note|reference|ref|page)$")


_ID_LIKE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/._-]*\d[A-Za-z0-9/._-]*")


def _match_label(seg: Segment, include_other: bool = False) -> Optional[tuple[str, int, Optional[str]]]:
    """Return (field, n_label_tokens, currency_in_label) when the segment starts with a label."""
    toks = seg.tokens
    for n in range(min(len(toks), 5), 0, -1):
        # "Label value" in one segment: every value we extract contains a digit, so a
        # remainder without one ("TAX INVOICE") means this is not a label.
        if n < len(toks) and not any(ch.isdigit() for t in toks[n:] for ch in t.text):
            continue
        text = " ".join(t.text for t in toks[:n])
        forms = []
        for f in (_norm(text, keep_hash=True), _norm(text)):
            forms += [f, f.replace(" ", "")]
        if include_other and n == len(toks) and any(OTHER_LABELS.match(f) for f in forms):
            return "_other", n, None
        for key, pat in LABELS.items():
            m = next((m for f in forms if (m := pat.match(f))), None)
            if m:
                cur = m.groupdict().get("cur") if "cur" in pat.groupindex else None
                return key, n, cur.upper() if cur else None
    return None


# ----------------------------------------------------------------------------- value parsers

_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%B %d %Y")


def parse_date(text: str) -> tuple[Optional[str], Optional[str]]:
    s = re.sub(r"\s+", " ", text.strip().rstrip("."))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat(), None
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a <= 12 and b <= 12 and a != b:
            return None, "ambiguous numeric date (day/month order unclear)"
        try:
            return date(y, a, b).isoformat(), None
        except ValueError:
            try:
                return date(y, b, a).isoformat(), None
            except ValueError:
                return None, "invalid date"
    return None, "unrecognised date format"


_PERIOD_MONTH_FORMATS = ("%B %Y", "%b %Y", "%b. %Y", "%Y-%m")
_ISO_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2}) ?(?:-|–|—|to|through|thru) ?(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)


def parse_period(text: str) -> tuple[Optional[str], Optional[str]]:
    """Service period: a calendar month ("September 2026" -> "2026-09") or an ISO date range
    ("2026-09-01 to 2026-09-30" -> "2026-09-01/2026-09-30"). Anything else stays unparsed."""
    s = re.sub(r"\s+", " ", text.strip().rstrip("."))
    for fmt in _PERIOD_MONTH_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m"), None
        except ValueError:
            continue
    m = _ISO_RANGE.match(s)
    if m and m.group(1) <= m.group(2):
        return f"{m.group(1)}/{m.group(2)}", None
    return None, "unrecognised period format"


# Printed wording that states no tax is charged. Read as zero only when subtotal equals total.
_NO_TAX = re.compile(r"^(not applicable|n/?a|none|nil|exempt|tax exempt|not taxable|no tax|zero rated)$")


# Payment-instruction phrases. Only rows that carry a routing or account number are read as remittance details.
_ROUTING_RE = re.compile(r"\b(?:ach\s+|aba\s+|wire\s+)?(?:routing|aba|rtn)(?:\s*(?:no\.?|number|#))?\s*[:#]?\s*"
                         r"([0-9*xX•]{4,12})(?![0-9])", re.IGNORECASE)
_ACCOUNT_RE = re.compile(r"\b(?:account|acct\.?|a/c)(?:\s*(?:no\.?|number|#))?\s*[:#]?\s*"
                         r"([0-9*xX•][0-9*xX•-]{3,24})(?![0-9])", re.IGNORECASE)
_BANK_WORD = re.compile(r"\b(bank|credit union|savings|bancorp)\b", re.IGNORECASE)
_PART_SEP = re.compile(r"\s*[·•|;,]\s*|\s+[-–—]\s+")
# Explicit notices that the remittance details changed. Ordinary "Remit to:" / "Pay by ACH:" lines are not notices.
_CHANGE_NOTICE_RE = re.compile(
    r"\b(updated|new|changed|revised)\s+(remittance|remit|banking|bank)\b"
    r"|\b(updated|new|changed|revised)\s+(payment|account|ach|wire)\s+(details|information|instructions)\b"
    r"|\bchange\s+(?:of|to|in)\s+(?:our\s+)?(bank|banking|remittance|account|payment)\b"
    r"|\bmoved\s+our\s+bank(?:ing)?\b"
    r"|\bbank(?:ing)?\s+(?:details|information|account)\s+(?:has|have)\s+changed\b", re.IGNORECASE)


def norm_identifier(text: str) -> str:
    """Conservative ID normalisation: trim, uppercase, collapse inner spaces. Keeps leading
    zeros and punctuation; unifies dash variants."""
    s = text.strip().upper().replace("–", "-").replace("—", "-").replace("‐", "-")
    return re.sub(r"\s+", "", s)


# ----------------------------------------------------------------------------- parser

COLUMN_KEYS: dict[str, re.Pattern[str]] = {
    "sku": re.compile(r"^(item|sku|item code|product code|code|part( no\.?| number)?)$"),
    "description": re.compile(r"^(description|details|service|item description)$"),
    "quantity": re.compile(r"^(qty|quantity|hours|hrs|units)$"),
    "unit_price": re.compile(r"^(unit price|rate|price|unit cost|unit rate)( \([a-z]{3}\))?$"),
    "amount": re.compile(r"^(amount|line total|total|net amount|net|ext|extended|ext(ended)? (amount|price)|extension)"
                         r"( \([a-z]{3}\))?$"),
}
NUMERIC_COLS = ("quantity", "unit_price", "amount")


def _column_of(seg: Segment) -> Optional[str]:
    t = _norm(seg.text)
    for key, pat in COLUMN_KEYS.items():
        if pat.match(t):
            return key
    return None


class InvoiceParser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.rows: list[Row] = []
        self.slopes: dict[int, float] = {}
        for page in sorted({t.page for t in tokens}):
            ptoks = [t for t in tokens if t.page == page]
            slope = estimate_skew(ptoks)
            self.slopes[page] = slope
            self.rows.extend(build_rows(ptoks, slope))
        self.inv = ExtractedInvoice()

    # -- header fields
    def _collect_labelled(self) -> dict[str, list[tuple[list[Token], list[Token], Optional[str]]]]:
        """field -> [(value_tokens, label_tokens, currency_in_label)]."""
        found: dict[str, list[tuple[list[Token], list[Token], Optional[str]]]] = {}
        for row in self.rows:
            consumed: set[int] = set()
            for i, seg in enumerate(row.segments):
                if i in consumed:
                    continue
                # An "INVOICE <id>" heading: the bare word followed by exactly one ID-like token in the same cell.
                # Kept out of LABELS so a lone "INVOICE" title is never a label ("TAX INVOICE", "Invoice 1 of 2").
                if (len(seg.tokens) == 2 and _norm(seg.tokens[0].text) == "invoice"
                        and _ID_LIKE.fullmatch(seg.tokens[1].text)):
                    found.setdefault("invoice_number", []).append(([seg.tokens[1]], [seg.tokens[0]], None))
                    continue
                m = _match_label(seg, include_other=True)
                if not m:
                    continue
                key, n, cur = m
                label_toks = seg.tokens[:n]
                value_toks = seg.tokens[n:]
                if not value_toks and i + 1 < len(row.segments):
                    nxt = row.segments[i + 1]
                    nm = _match_label(nxt, include_other=True)
                    # A known non-extracted label (e.g. "Terms") owns the next segment unless
                    # that segment is itself a bare label ("Bill to | Invoice number").
                    if nm is None or (key == "_other" and nm[1] < len(nxt.tokens)):
                        value_toks = nxt.tokens
                        consumed.add(i + 1)
                if value_toks and key != "_other":
                    found.setdefault(key, []).append((value_toks, label_toks, cur))
        return found

    def _assign(self, key: str, cands: list[tuple[list[Token], list[Token], Optional[str]]],
                parse: Callable[[str], tuple[object, Optional[str]]]) -> None:
        if not cands:
            return  # label absent: value stays null (callers decide whether that matters)
        parsed = []
        for toks, label_toks, _cur in cands:
            label = " ".join(t.text for t in label_toks)
            raw = " ".join(t.text for t in toks)
            value, err = parse(raw)
            parsed.append((raw, value, err, toks, label))
        good = [p for p in parsed if p[2] is None]
        target: Field_ = getattr(self.inv, key)
        if not good:
            raw, _v, err, toks, label = parsed[0]
            target.raw, target.evidence, target.note = raw, evidence_for(toks, label), err
            self.inv.issues.append(f"{key}: could not parse {raw!r} ({err})")
            return
        values = {repr(p[1]) for p in good}
        raw, value, _e, toks, label = good[0]
        target.raw, target.value, target.evidence = raw, value, evidence_for(toks, label)
        if len(values) > 1:
            target.value = None
            target.note = "conflicting values: " + ", ".join(sorted({p[0] for p in good}))
            self.inv.issues.append(f"{key}: {target.note}")

    def _supplier(self) -> None:
        # Header zone: rows above the first row that holds a labelled field or "bill to".
        stop_y = None
        for row in self.rows:
            if row.page != 1:
                continue
            if any(_match_label(s) for s in row.segments) or re.search(r"\bbill(ed)? to\b", row.text.lower()):
                stop_y = row.y
                break
        cands: list[Segment] = []
        for row in self.rows:
            if row.page != 1 or (stop_y is not None and row.y >= stop_y):
                continue
            for seg in row.segments:
                t = _norm(seg.text)
                if re.fullmatch(r"(tax |commercial )?invoice", t) or "@" in t or re.search(r"\d", t):
                    continue
                if len(t) < 3:
                    continue
                cands.append(seg)
        if not cands:
            self.inv.issues.append("supplier_name: no supplier name found in the header zone")
            return
        best = max(cands, key=lambda s: (round(s.height, 0), -s.tokens[0].y0))
        self.inv.supplier_name = Field_(raw=best.text, value=best.text.strip(), evidence=evidence_for(best.tokens))

    def _currency(self, labelled) -> None:
        codes: dict[str, list[Token]] = {}
        for key in ("gross", "net", "tax"):
            for _toks, label_toks, cur in labelled.get(key, []):
                if cur:
                    codes.setdefault(cur, label_toks)
        if not codes:
            for t in self.tokens:
                m = re.fullmatch(r"\(?(USD|EUR|GBP|CAD|AUD|INR)\)?[.,:]?", t.text)
                if m:
                    codes.setdefault(m.group(1), [t])
        if len(codes) > 1:
            self.inv.currency.note = "conflicting currency codes: " + ", ".join(sorted(codes))
            self.inv.issues.append(f"currency: {self.inv.currency.note}")
            return
        if codes:
            code, toks = next(iter(codes.items()))
            self.inv.currency = Field_(raw=code, value=code, evidence=evidence_for(toks))
            return
        dollar = [t for t in self.tokens if t.text.startswith("$")]
        if dollar:
            self.inv.currency = Field_(raw="$", value="USD", evidence=evidence_for([dollar[0]]),
                                       note="inferred from '$' symbol; no ISO code printed")
        else:
            self.inv.issues.append("currency: no currency code or symbol found")

    def _no_tax_wording(self) -> None:
        """A tax cell printed as words ("Not applicable", "Exempt") is zero tax only if the printed
        subtotal and total agree; the words stay as the raw value and evidence. Otherwise it stays unparsed."""
        tax = self.inv.tax
        if tax.value is not None or tax.raw is None or not _NO_TAX.match(_norm(tax.raw)):
            return
        net, gross = self.inv.net.value, self.inv.gross.value
        issue = next((i for i in self.inv.issues if i.startswith("tax: could not parse")), None)
        if net is not None and net == gross:
            tax.value = 0
            tax.note = f"printed as {tax.raw!r}; read as 0 because the subtotal equals the total"
            if issue:
                self.inv.issues.remove(issue)
        elif issue:
            self.inv.issues[self.inv.issues.index(issue)] = (
                f"tax: printed as {tax.raw!r} but the subtotal and total differ")

    def _remittance(self) -> None:
        """Printed payment instructions (bank name, routing and account numbers) and any explicit change notice,
        each with its page and position. Informational for extraction: never blocks it; the rules compare the
        values with the vendor master. Conflicting values across rows leave the field empty with a note."""
        found: dict[str, list[tuple[str, list[Token]]]] = {"remit_bank_name": [], "remit_routing": [], "remit_account": []}
        for row in self.rows:
            text = " ".join(t.text for t in row.tokens)
            hits = []
            for key, rx in (("remit_routing", _ROUTING_RE), ("remit_account", _ACCOUNT_RE)):
                for m in rx.finditer(text):
                    value = re.sub(r"[-\s]", "", m.group(1))
                    if len(_digits(value)) < 4:
                        continue
                    tail = _digits(value)[-4:]
                    toks = [t for t in row.tokens if tail in t.text] or row.tokens
                    hits.append((key, value, toks))
            if not hits:
                continue
            found["remit_routing"] += [(v, t) for k, v, t in hits if k == "remit_routing"]
            found["remit_account"] += [(v, t) for k, v, t in hits if k == "remit_account"]
            for part in _PART_SEP.split(text):
                if _BANK_WORD.search(part) and not re.search(r"\d", part):
                    name = part.rsplit(":", 1)[-1].strip(" .")
                    if name:
                        words = name.split()
                        toks = [t for t in row.tokens if t.text in words] or row.tokens
                        found["remit_bank_name"].append((name, toks))
        for key, cands in found.items():
            if not cands:
                continue
            value, toks = cands[0]
            label = {"remit_bank_name": "bank", "remit_routing": "routing", "remit_account": "account"}[key]
            f = Field_(raw=value, value=value, evidence=evidence_for(toks, label))
            distinct = {(c[0].upper() if key != "remit_bank_name" else _norm(c[0])) for c in cands}
            if len(distinct) > 1:
                f.value, f.note = None, f"{len(distinct)} different {label} values printed"
            setattr(self.inv, key, f)
        for row in self.rows:
            text = " ".join(t.text for t in row.tokens)
            m = _CHANGE_NOTICE_RE.search(text)
            if m:
                self.inv.remittance_notice = Field_(raw=text, value=m.group(0),
                                                    evidence=evidence_for(row.tokens, "remittance change notice"))
                break

    def _service_period(self, cands) -> None:
        """Informational: never blocks extraction. An unparsed period keeps its raw text and evidence."""
        if not cands:
            return
        parsed = []
        for toks, label_toks, _cur in cands:
            raw = " ".join(t.text for t in toks)
            value, err = parse_period(raw)
            parsed.append((raw, value, err, toks, " ".join(t.text for t in label_toks)))
        raw, value, err, toks, label = next((p for p in parsed if p[2] is None), parsed[0])
        f = Field_(raw=raw, value=value, evidence=evidence_for(toks, label), note=err)
        if len({p[1] for p in parsed if p[2] is None}) > 1:
            f.value, f.note = None, "conflicting service periods: " + ", ".join(sorted({p[0] for p in parsed}))
        self.inv.service_period = f

    _DELIVERY_RE = re.compile(
        r"deliver(?:y)?\s*(?:note|ref(?:erence)?\.?|receipt)\s*(?:no\.?|number|#)?\s*[:#]?\s*"
        r"(?P<ref>[A-Z]{1,6}[-/]?\d[A-Z0-9/-]*)", re.IGNORECASE)

    def _delivery_reference(self) -> None:
        """Delivery note reference, from a labelled phrase anywhere on the page. Tolerates words run
        together by the PDF producer ("DeliveryreferenceDN-4410."). Informational only."""
        found: list[tuple[str, list[Token]]] = []
        for row in self.rows:
            text = " ".join(t.text for t in row.tokens)
            for m in self._DELIVERY_RE.finditer(text):
                ref = m.group("ref").rstrip(".,;:-/")
                toks = [t for t in row.tokens if ref.lower() in t.text.lower()]
                if not toks:  # value split across tokens: keep the tokens that make up the phrase
                    toks = row.tokens
                found.append((norm_identifier(ref), toks))
        if not found:
            return
        values = sorted({v for v, _ in found})
        ref, toks = found[0]
        f = Field_(raw=ref, value=ref, evidence=evidence_for(toks, "delivery reference"))
        if len(values) > 1:
            f.value, f.note = None, "conflicting delivery references: " + ", ".join(values)
        self.inv.delivery_reference = f

    def _lines(self) -> None:
        header_idx = None
        cols: list[tuple[str, Segment]] = []
        for idx, row in enumerate(self.rows):
            mapped = [(c, s) for s in row.segments if (c := _column_of(s))]
            kinds = {c for c, _ in mapped}
            if len(kinds) >= 3 and "amount" in kinds and len(kinds) == len(mapped):
                header_idx, cols = idx, mapped
                break
        if header_idx is None:
            self.inv.issues.append("lines: no line-item table header found")
            return
        header_row = self.rows[header_idx]
        # Header cells we do not extract (e.g. "Ln", "UOM") still bound a column, so their contents are
        # dropped instead of merging into the neighbouring column.
        cols += [("_ignored", s) for s in header_row.segments if not _column_of(s)]
        cols.sort(key=lambda cs: cs[1].x0)
        slack = 0.5 * statistics.median(t.h for t in header_row.tokens)
        text_cols = [(c, s) for c, s in cols if c not in NUMERIC_COLS]

        def band_column(tok: Token) -> Optional[str]:
            """Column whose band - from its header's left edge to the next header's left edge -
            holds the whole token. Covers a left-aligned header over right-aligned figures that end
            past the header text, such as a short "Qty" header above a wider quantity column."""
            for i, (c, s) in enumerate(cols):
                hi = cols[i + 1][1].x0 - slack if i + 1 < len(cols) else float("inf")
                if tok.x0 >= s.x0 - slack and tok.x1 <= hi:
                    return c
            return None

        items: list[dict[str, list[Token]]] = []
        for row in self.rows[header_idx + 1:]:
            if row.page != header_row.page:
                break
            if any((m := _match_label(s)) and m[0] in TOTAL_KEYS for s in row.segments):
                break
            cells: dict[str, list[Token]] = {}
            for tok in row.tokens:
                placed = False
                for c, s in cols:
                    if c in NUMERIC_COLS and tok.x1 >= s.x0 - slack and tok.x0 <= s.x1 + slack:
                        if parse_decimal_amount(tok.text) is not None:
                            cells.setdefault(c, []).append(tok)
                            placed = True
                            break
                if not placed and parse_decimal_amount(tok.text) is not None:
                    # A figure under no header: use the column band. Numbers inside a text column's
                    # band (dimensions or counts in a description) stay text.
                    c = band_column(tok)
                    if c in NUMERIC_COLS:
                        cells.setdefault(c, []).append(tok)
                        placed = True
                if placed:
                    continue
                # Text column: the right-most text column whose header starts left of the token.
                target = None
                for c, s in text_cols:
                    if tok.x0 >= s.x0 - slack:
                        target = c
                if target is None and text_cols:
                    target = text_cols[0][0]
                if target:
                    cells.setdefault(target, []).append(tok)
            if "amount" in cells:
                items.append(cells)
            elif items and cells and not any(c in cells for c in NUMERIC_COLS):
                for c, toks in cells.items():  # wrapped description line
                    items[-1].setdefault(c, []).extend(toks)
            elif items:
                break  # table ended
        if not items:
            self.inv.issues.append("lines: table header found but no line rows with an amount")
        for i, cells in enumerate(items):
            self.inv.lines.append(self._line(i, cells))

    def _line(self, i: int, cells: dict[str, list[Token]]) -> LineItem:
        def text_field(c: str) -> Field_:
            toks = cells.get(c)
            if not toks:
                return Field_()
            raw = " ".join(t.text for t in toks)
            return Field_(raw=raw, value=raw, evidence=evidence_for(toks, c))

        def num_field(c: str, fn) -> Field_:
            toks = cells.get(c)
            if not toks:
                return Field_(note="missing")
            if len(toks) > 1:
                raw = " ".join(t.text for t in toks)
                self.inv.issues.append(f"line {i + 1} {c}: multiple values {raw!r}")
                return Field_(raw=raw, evidence=evidence_for(toks, c), note="multiple values in cell")
            raw = toks[0].text
            v = fn(raw)
            f = Field_(raw=raw, value=v, evidence=evidence_for(toks, c))
            if v is None:
                f.note = "unparseable"
                self.inv.issues.append(f"line {i + 1} {c}: could not parse {raw!r}")
            return f

        sku = text_field("sku")
        if sku.value:
            sku.value = norm_identifier(sku.value)
        q = num_field("quantity", lambda r: (lambda d: norm_decimal(d) if d is not None else None)(parse_quantity(r)))
        up = num_field("unit_price",
                       lambda r: (lambda d: norm_decimal(d) if d is not None else None)(parse_decimal_amount(r)))
        amt = num_field("amount", parse_money_cents)
        return LineItem(row_index=i, sku=sku, description=text_field("description"), quantity=q,
                        unit_price=up, amount=amt)

    def parse(self) -> ExtractedInvoice:
        labelled = self._collect_labelled()
        self._supplier()
        self._assign("invoice_number", labelled.get("invoice_number", []),
                     lambda r: (norm_identifier(r), None) if re.search(r"\d", r) else (None, "no digits"))
        self._assign("invoice_date", labelled.get("invoice_date", []), parse_date)
        self._assign("po_reference", labelled.get("po_reference", []),
                     lambda r: (norm_identifier(r), None) if re.search(r"\d", r) else (None, "no digits"))
        for key in TOTAL_KEYS:
            # Use the right-most money token of the value segment.
            cands = []
            for toks, label, cur in labelled.get(key, []):
                money = [t for t in toks if parse_decimal_amount(t.text) is not None]
                cands.append((money[-1:] or toks, label, cur))
            self._assign(key, cands,
                         lambda r: (lambda c: (c, None) if c is not None else (None, "not a money amount"))(
                             parse_money_cents(r)))
        self._no_tax_wording()
        self._service_period(labelled.get("service_period", []))
        self._remittance()
        self._currency(labelled)
        self._delivery_reference()
        self._lines()
        for key in ("invoice_number", "invoice_date", "net", "gross"):
            if getattr(self.inv, key).value is None and not getattr(self.inv, key).note:
                self.inv.issues.append(f"{key}: not found")
        return self.inv


def parse_invoice(tokens: Iterable[Token]) -> tuple[ExtractedInvoice, dict[int, float]]:
    p = InvoiceParser(list(tokens))
    return p.parse(), p.slopes


def quantity_decimal(f: Field_) -> Optional[Decimal]:
    return Decimal(f.value) if f.value is not None else None
