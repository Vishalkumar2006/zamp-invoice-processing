"""Tiny dependency-free PDF writer for synthetic test invoices (Helvetica text at coordinates)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Text:
    x: float
    y: float  # from the top of the page, in points
    text: str
    size: float = 10
    bold: bool = False


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(items: list[Text], width: float = 612, height: float = 792) -> bytes:
    ops = []
    for it in items:
        font = "/F2" if it.bold else "/F1"
        ops.append(f"BT {font} {it.size} Tf {it.x:.2f} {height - it.y - it.size:.2f} Td ({_esc(it.text)}) Tj ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
         f"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>").encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def invoice_items(*, supplier="Harborline Calibration Inc", number="HCI-0042", date="March 4, 2026",
                po="PO-7781", lines=(("CAL-STD", "Standard calibration", "12", "$250.00", "$3,000.00"),
                                     ("CAL-TRV", "Technician travel", "1", "$480.00", "$480.00")),
                subtotal="$3,480.00", tax="$0.00", total="$3,480.00", currency_label="Amount due (USD)",
                footer="Thank you for your business. Page 1 of 1") -> list[Text]:
    """Layout items for a synthetic invoice (shared by the native and scanned variants)."""
    t: list[Text] = [
        Text(40, 40, supplier, 16, True),
        Text(40, 62, "12 Pier Road, Unit 4"),
        Text(450, 40, "TAX INVOICE", 18, True),
        Text(40, 120, "Billed to", bold=True),
        Text(40, 136, "Demo Buyer Corp"),
        Text(330, 120, "Invoice no.", bold=True), Text(440, 120, number),
        Text(330, 136, "Date of issue", bold=True), Text(440, 136, date),
        Text(330, 152, "Terms", bold=True), Text(440, 152, "Net 45"),
    ]
    if po is not None:
        t += [Text(330, 168, "PO number", bold=True), Text(440, 168, po)]
    y = 230
    t += [Text(40, y, "SKU", bold=True), Text(130, y, "Details", bold=True), Text(350, y, "Quantity", bold=True),
          Text(430, y, "Rate", bold=True), Text(520, y, "Line total", bold=True)]
    for sku, desc, qty, rate, amt in lines:
        y += 20
        t += [Text(40, y, sku), Text(130, y, desc), Text(360, y, qty), Text(430, y, rate), Text(520, y, amt)]
    y += 40
    t += [Text(430, y, "Subtotal"), Text(520, y, subtotal)]
    if tax is not None:
        y += 18
        t += [Text(430, y, "Sales tax"), Text(520, y, tax)]
    y += 22
    t += [Text(380, y, currency_label, bold=True), Text(520, y, total, bold=True)]
    t.append(Text(40, 740, footer, 8))
    return t


def invoice_pdf(**kw) -> bytes:
    """A native-text invoice with a layout that differs from the supplied fixtures."""
    return make_pdf(invoice_items(**kw))


def scan_pdf(items: list[Text], *, dpi: int = 200, angle: float = 0.6, width: float = 612,
             height: float = 792) -> bytes:
    """An image-only PDF that looks like a slightly rotated scan of the same layout.

    Text is drawn with the font bundled inside Pillow (not a system font), so the pixels do not
    depend on which fonts the host has or how Poppler substitutes a non-embedded Helvetica.
    """
    import io

    from PIL import Image, ImageDraw, ImageFont

    scale = dpi / 72.0
    img = Image.new("L", (round(width * scale), round(height * scale)), 255)
    draw = ImageDraw.Draw(img)
    for it in items:
        font = ImageFont.load_default(size=it.size * scale)
        # Bold is drawn at regular weight: a stroked "fake bold" smears glyphs (OCR read
        # "Details" as "Detalls"), which is an artefact of the generator, not of real scans.
        draw.text((it.x * scale, it.y * scale), it.text, fill=0, font=font)
    img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=dpi)
    return buf.getvalue()


VENDORS_CSV = b"vendor_id,legal_name,status\nV-900,Harborline Calibration Inc,APPROVED\nV-901,Other Supplies LLC,APPROVED\n"
POS_CSV = (b"po_id,vendor_id,issued_on,status,currency,description,authorized_net\n"
           b"PO-7781,V-900,2026-02-01,OPEN,USD,Annual calibration,6960.00\n"
           b"PO-7790,V-901,2026-02-03,OPEN,USD,Supplies,100.00\n")
LINES_CSV = (b"po_id,line_id,sku,description,authorized_quantity,unit_price,authorized_net\n"
             b"PO-7781,1,CAL-STD,Standard calibration,24,250.00,6000.00\n"
             b"PO-7781,2,CAL-TRV,Technician travel,2,480.00,960.00\n"
             b"PO-7790,1,SUP-1,Supplies,10,10.00,100.00\n")
