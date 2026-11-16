"""Pydantic models shared by extraction, decision logic and the API."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Method = Literal["native", "ocr"]


class Token(BaseModel):
    """A word with its position, in PDF points (origin top-left)."""
    text: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 100.0  # 0-100; native text is 100
    method: Method
    group: Optional[str] = None  # OCR engine's own line grouping, used for skew estimation

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


class PageInfo(BaseModel):
    page: int
    method: Method
    native_chars: int
    token_count: int
    mean_conf: Optional[float] = None
    skew_slope: float = 0.0
    duration_ms: int


class Evidence(BaseModel):
    page: int
    method: Method
    text: str  # raw source text as printed/recognised
    label: Optional[str] = None  # the label the value was read next to
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points
    conf: float  # min word confidence over the value tokens


class Field_(BaseModel):
    """An extracted field: raw string, normalized value, and its evidence."""
    raw: Optional[str] = None
    value: Any = None
    evidence: Optional[Evidence] = None
    note: Optional[str] = None


class LineItem(BaseModel):
    row_index: int
    sku: Field_
    description: Field_
    quantity: Field_  # value: str(Decimal)
    unit_price: Field_  # value: str(Decimal)
    amount: Field_  # value: int cents


class ExtractedInvoice(BaseModel):
    supplier_name: Field_ = Field(default_factory=Field_)
    invoice_number: Field_ = Field(default_factory=Field_)
    invoice_date: Field_ = Field(default_factory=Field_)  # value: ISO date
    po_reference: Field_ = Field(default_factory=Field_)
    currency: Field_ = Field(default_factory=Field_)
    net: Field_ = Field(default_factory=Field_)  # cents
    tax: Field_ = Field(default_factory=Field_)  # cents
    gross: Field_ = Field(default_factory=Field_)  # cents
    lines: list[LineItem] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    invoice: ExtractedInvoice
    pages: list[PageInfo]
    duration_ms: int
    model_calls: int = 0


class Decision(BaseModel):
    """Output of the pure decision function."""
    decision: Literal["READY", "HOLD", "BLOCKED"]
    reason_code: str
    reason: str
    next_action: str
    vendor_id: Optional[str] = None
    vendor_name: Optional[str] = None
    po_id: Optional[str] = None
    candidate_po_ids: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    allocation: Optional[dict[str, Any]] = None  # present only for READY
    prior_allocations: list[dict[str, Any]] = Field(default_factory=list)
    review_field: Optional[str] = None
    # Every reason code that applied, primary first (e.g. money and quantity cap breaches together).
    reason_codes: list[str] = Field(default_factory=list)
    # Cap comparisons, breaches, prior accepted invoices and the PO balance as seen at decision time.
    authorization: Optional[dict[str, Any]] = None
    # Business-key lookup (vendor + printed invoice number) and the field comparison with the accepted invoice.
    duplicate: Optional[dict[str, Any]] = None
    policy_version: str
