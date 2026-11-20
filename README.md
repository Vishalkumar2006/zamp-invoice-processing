# Invoice processing: from PDF to decision

An AP workflow for a team whose purchase orders live in spreadsheets. An operator imports vendor and PO data
as CSVs and uploads an invoice PDF. The app reads the PDF, using OCR for scanned pages, matches the vendor
and PO, checks the numbers against the PO and what has already been billed, and records one decision with
its evidence:

| Outcome | Meaning |
|---|---|
| `READY` | All checks pass. One allocation is written against the PO ledger. |
| `HOLD` | A reviewer must act (missing or unclear field, unknown vendor or PO, arithmetic, price or authorization problem). No allocation. |
| `BLOCKED` | The same vendor and invoice number were already accepted. Links to the earlier run. No allocation. |
| `FAILED` | A technical error, such as an unreadable PDF or OCR timeout. Run status only, with a retry button. No allocation. |

Current checkpoint: **Milestone 1** (the vertical slice: procurement import, upload and job lifecycle, native and OCR
extraction with page-level evidence, exact vendor and PO matching, atomic allocation, live and history views) plus
the **cumulative-overbilling** hold (a later invoice that, together with earlier accepted invoices, exceeds the PO)
**duplicate resubmission** handling (a re-sent or scanned copy of an accepted invoice), **ambiguous PO**
explanations (no PO printed, several open POs fit), and **offsetting line prices** (per-line price differences
that cancel out in the subtotal).

## Run it

```bash
docker compose up --build -d          # http://localhost:8000
curl http://localhost:8000/health
```

Data (SQLite database and original PDFs) lives in the `zamp_data` Docker volume at `/data`, so it survives
`docker compose restart` and `docker compose down`/`up`. Run `docker compose down -v` for a clean database.
To require HTTP basic auth, set `APP_PASSWORD` (and optionally `APP_USERNAME`, default `ap`).

In the UI:

1. **Upload**: import `vendors.csv`, `purchase_orders.csv` and `po_lines.csv` into a fresh workspace.
2. Upload an invoice PDF. The upload goes to the workspace named on the page, even if another tab has imported a
   newer one since. You land on the live run view, which shows stages from persisted events, the
   decision, the next action, the PO balance before and after, every extracted field with its page, method
   (`native`/`ocr`), confidence and position, every rule check, and the original PDF side by side.
3. **History** lists runs, outcome counts and the PO ledger for one workspace, picked from a selector (default:
   the latest; the choice stays in the URL). Each run page links to the history of its own workspace.

The same flow over HTTP:

```bash
curl -F vendors=@vendors.csv -F purchase_orders=@purchase_orders.csv -F po_lines=@po_lines.csv \
     http://localhost:8000/procurement/import
curl -F "file=@invoice.pdf;type=application/pdf" http://localhost:8000/runs    # 202 {"run_id": ...}
curl http://localhost:8000/runs/<run_id>                                      # poll until COMPLETED/FAILED
curl http://localhost:8000/ledger                                             # independent balances
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | DB and OCR tool check |
| POST | `/procurement/import` | Three CSVs, validated together. Creates a new workspace, or returns 422 with every error. |
| GET | `/procurement` | Imported rows with their source file and row |
| POST | `/runs` | PDF upload. Stores the file and queues the run, then returns `202` with `run_id` and `workspace_id`. Optional form field `workspace_id` (the UI always sends it); omitted means the latest workspace; an unknown ID returns 422 and writes nothing. |
| GET | `/runs` | Run history (`?workspace_id=` to scope) |
| GET | `/runs/{id}` | Events, extraction plus evidence, decision, allocation, current PO balance, timings |
| GET | `/runs/{id}/pdf` | The original uploaded PDF |
| POST | `/runs/{id}/finalize` | Retries finalization from the stored extraction. Idempotent: never a second allocation. |
| POST | `/runs/{id}/retry` | Re-queues a `FAILED` run |
| GET | `/ledger` | Read-only ledger (`?workspace_id=`, default latest): authorized, allocated and remaining net and quantity per PO and line, plus allocations |

## How it works

```
upload ─► persist PDF + run (QUEUED) ─► worker claims run
        ─► per page: native text (pdfplumber) if usable, else Poppler render + Tesseract OCR
        ─► layout parser: labelled header fields + line-item table, each value with page/bbox/confidence
        ─► store extraction (outside the ledger transaction)
        ─► BEGIN IMMEDIATE: reread PO + allocations, run pure rules, write decision + allocation + event, COMMIT
```

- **Extraction** (`app/extraction/`) only produces an `ExtractedInvoice`. Values come only from a labelled
  header field (for example `Invoice number`, `Invoice #:`, `Purchase order`, `Customer PO:`, `Subtotal`,
  `Total due (USD)`) or from a row of the line-item table. A number printed elsewhere is never used. Table figures go
  to the column whose header they overlap; a figure under no header text goes to the column band that holds it
  (from its header's left edge to the next header's), so right-aligned quantities past a left-aligned `Qty` header
  are read as quantities, while numbers inside a description (dimensions, pack sizes) stay in the description. Two
  figures in one cell, or none, leave the value empty and the run on hold. Scans are skew-corrected using Tesseract's own line
  groupings before tokens are grouped into rows. Missing or conflicting values stay `null` and are listed as issues.
  The app makes no model calls at runtime.
- **Decision** (`app/rules.py`) is a pure function, `evaluate(invoice, snapshot)`, with no I/O. Rules run in priority order:
  evidence (every critical value present and at or above OCR confidence 80), unique approved vendor by
  normalized legal name (exact first, then a unique legal-suffix variant), business duplicate (vendor and invoice number), printed PO (exists, same vendor, same
  currency, OPEN), arithmetic (qty × rate = amount, lines = net, net + tax = gross), SKU-to-PO-line mapping, unit
  price within **1% inclusive**, and a **strict cumulative cap** on net and quantity, counting prior allocations.
- **Cumulative overbilling.** Every cap is compared, with no early exit: quantity and net for each affected PO line,
  and net for the PO total. Each comparison records authorized, previously allocated, remaining, incoming pre-tax,
  projected and excess figures. If any fail, the run is `HOLD`. The primary `reason_code` is deterministic:
  `AMOUNT_EXCEEDS_PO` when any money cap fails, otherwise `QUANTITY_EXCEEDS_PO`. `reason_codes` lists every code
  that applied. The hold names and links the earlier accepted invoices and gives a next action sized from the excess
  (change order amount and units, or a corrected invoice within what remains). These figures, the prior invoices and
  the PO balance at decision time are stored with the decision. Reopening an old hold shows what triggered it,
  even after the live ledger has moved. The live balance is shown separately.
- **Business duplicates.** The key is the approved vendor ID plus the printed invoice number, normalized
  conservatively: uppercase, inner spaces removed, dash variants unified. Leading zeros and punctuation are kept,
  so `INV-00417`, `INV-417` and `INV00417` are three different keys (see leading-zero variants below). Only previously *accepted* (allocated)
  invoices are looked up, so a corrected resubmission after a `HOLD` is evaluated normally. On a key match, the
  document is compared field by field with the accepted invoice: invoice date, currency, net, tax, total, PO, and
  each line's SKU, quantity, unit price and amount. If everything matches, it's a corroborated copy:
  `BLOCKED / DUPLICATE_INVOICE`, whatever the file bytes or layout (a scanned portal copy blocks the same way). If
  anything differs, it's `HOLD / DUPLICATE_NUMBER_CONFLICT`, naming the conflicting fields. A different verified
  number goes through the normal PO and authorization checks, even with the same vendor, PO and amount. To fail
  closed on uncertain identity, a number read by OCR that differs from an accepted number only by characters OCR
  often confuses (O/0, I/1, S/5, B/8, Z/2) is held as `DUPLICATE_IDENTITY_UNCERTAIN`. That applies when the
  verified date and billing details corroborate a possible copy, or when the evidence is mixed. A confidently read
  number with a *later* invoice date *and* different billing details (amounts, lines, currency or PO) is treated as
  a separate invoice. It goes through the normal checks, and the near match is recorded with the decision. The comparison is stored
  with the decision and shown in the run view, with a link to the accepted invoice. The lookup runs inside the
  same `BEGIN IMMEDIATE` transaction as the allocation, and the database rejects a second allocation for the same
  vendor and invoice number. If that ever fired, finalization re-evaluates against fresh state instead of failing.
- **Missing printed PO.** Every PO of the approved vendor is compared with the verified invoice lines: OPEN status,
  same currency, each SKU present, unit price within 1%, quantity and net within the line's remaining authorization,
  and net within the PO's remaining authorization. Each result carries its CSV source row. Two or more fitting
  POs give `HOLD / PO_AMBIGUOUS`. Exactly one gives `HOLD / PO_MISSING`, a suggestion that is never assigned
  automatically. None gives `HOLD / PO_MISSING_NO_MATCH`. No PO is selected and nothing is allocated. If the invoice
  prints a delivery reference and it reads at confidence 80 or above, it's included in the next action (ask
  procurement which PO covers that delivery). The run view shows the candidates side by side. Everything is stored
  with the decision.
- **Unit prices, line by line.** Every mapped line is checked before the price decision: the invoice unit price must
  be within **1% (inclusive)** of its PO line price. If any line fails, the run is `HOLD / PRICE_VARIANCE` with
  zero allocations. Each line records the invoice and PO prices, the signed variance (for example `+5.00%`,
  `-7.50%`), the signed pre-tax amount difference at the invoiced quantity, the invoice page evidence and the PO
  line's CSV row. Percentages are shown to two decimals, with more added near the limit so the figure stays on the
  same side of 1% as the exact comparison (`+1.001%` is held, `+0.9999%` passes, exactly `+1.00%` passes); the
  display never decides anything. The decision also records the subtotal at invoice prices against the subtotal at PO prices. When
  line differences cancel out, the hold says so explicitly. Tax is reported separately; authorization and
  allocations stay pre-tax. The next action names each affected line and its PO price.
- **Leading-zero variants.** A printed number equal to an accepted number of the same vendor apart from leading
  zeros (`INV-0417` vs `INV-417`) is kept as printed and compared with the accepted invoice: PO, service period
  (printed on both), currency, net, tax, total and lines. Fully corroborated with the same invoice date, it is
  `BLOCKED / DUPLICATE_INVOICE`, linked to the accepted run; corroborated but re-dated, it is
  `HOLD / DUPLICATE_IDENTITY_UNCERTAIN`. Otherwise the variant is recorded and the normal checks run: the same
  vendor, PO and amount alone never make a duplicate.
- **Vendor names.** The exact normalized legal name always wins. With no exact match, a printed name that differs
  from one legal name only by a legal suffix on one side (`Acme Field Services` vs `Acme Field Services Inc.`) matches if it is
  unique across the whole vendor master and the remaining name has at least two words. Two candidates hold as
  `VENDOR_AMBIGUOUS`; different suffixes (`… LLC` vs `… Inc`) never match. The method and compared forms are stored.
- **Description-only lines.** A line with no printed SKU keeps its SKU null. On a printed PO, it maps to the one PO
  line whose description shares at least two and at least 60% of its meaningful words (months, years and numbers
  ignored); none or several qualifying lines hold as `LINE_NOT_ON_PO`. The resolved PO line, shared words and CSV
  row are stored, and price, quantity and authorization checks apply as usual. Without a printed PO such a line is
  never matched.
- **Tax wording and service period.** A tax cell printed as `Not applicable`, `N/A`, `Exempt` and similar reads
  as zero only when the printed subtotal equals the total; the words stay as the raw value and evidence. A printed
  `Service period` (`September 2026`, or an ISO date range) is extracted with evidence.
- **Overbilling next action** shows the PO's remaining net under its own label and each line's remaining net and
  quantity labelled by SKU.
- **Candidate line mapping** uses the same rule as an explicit PO: each invoice line needs its own PO line. A
  candidate PO that would map two invoice rows to one PO line is shown as ineligible, with the reason.
- **Money** is stored as integer cents. Rates, quantities and tolerance use `Decimal`.
- **Ledger safety** (`app/ledger.py`): `finalize` runs in one `BEGIN IMMEDIATE` transaction. It rereads state, returns
  the existing decision unchanged if the run is already decided, and retries on lock contention with a bound. The
  database enforces `UNIQUE(run_id)` and `UNIQUE(workspace, vendor, invoice number)` on allocations.
- **Worker** (`app/worker.py`) is one background thread over the persisted `runs` queue. It claims runs
  atomically. At startup it re-queues interrupted runs, or fails them once the attempt limit is reached.

## Tests

```bash
# Full suite, including the real OCR path (needs Poppler + Tesseract): run inside the image
docker compose build && docker run --rm zamp-invoice-processing:latest python -m pytest -q

# Locally (OCR test is skipped if tesseract/pdftoppm are missing)
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q
```

The tests use synthetic vendors, POs and generated PDFs (`tests/pdfgen.py`) with a layout different from any
supplied sample. They cover:

- rule boundaries: exactly 1% vs above, net at the cap vs one cent over, quantity at the cap vs one unit over;
- missing and low-confidence fields;
- the HTTP flow end to end;
- finalize retry and concurrent finalize (one allocation), and two distinct invoices that each fit the fresh PO
  finalized at the same moment (one `READY`, one `HOLD / AMOUNT_EXCEEDS_PO` citing the winner, one allocation);
- database uniqueness;
- restart persistence;
- cumulative overbilling across two distinct invoices that each fit the fresh PO but together exceed the
  quantity cap, the line net cap and the PO net cap (all three breaches reported, zero allocation, explanation
  unchanged after the ledger moves);
- corrupt-PDF failure;
- duplicates: a native original then a rasterized copy with a different SHA-256 (`BLOCKED`), a legitimate
  same-amount recurrence (`READY`), same-key conflicts on date or amount (`HOLD` naming the fields), a corrected
  resubmission after a hold, a race between two runs for the same business invoice (one allocation), and older
  stored decisions that still render;
- missing PO: several, one or no fitting candidates, a price within tolerance, a valid explicit PO among several,
  a low-confidence delivery reference, and an ambiguous invoice over HTTP that leaves the ledger byte-identical;
- OCR near matches: a scan whose look-alike number corroborates a copy (held), and a scan with a distinct number,
  later date and new amounts (accepted);
- unit prices: offsetting differences with an unchanged subtotal and separate tax (both lines reported, over HTTP
  too), exactly +1% and -1% accepted, +1.01% and -1.01% held, a single failing line among passing ones, and
  near-limit variances that round to 1.00% (`+1.001%`/`-1.001%` held and displayed as such, `+0.9999%` accepted)
  in the reason, check detail, candidate table and over HTTP;
- a repeated SKU against a candidate PO, ineligible just like an explicit PO;
- service invoices: tax wording with and without reconciling totals, service periods, legal-suffix vendor
  variants (unique, ambiguous, exact precedence, different suffix, inactive), description-only lines (unique,
  ambiguous, no match, price and quantity still checked, no printed PO, unclear description), a next-month charge
  with the same amount, a resent copy with leading zeros (blocked when corroborated, held when re-dated, normal
  checks without period corroboration), and the existing exact-number block;
- layout variants: `Invoice #:` and `Customer PO:` style labels, right-aligned figures under left-aligned headers,
  numbers inside descriptions, a missing or doubled quantity (held), and a two-shipment PO where the second
  shipment is held for exceeding one SKU's quantity and the PO total;
- workspace routing: an upload from a page loaded before a newer import still lands in the page's workspace, an API
  upload without `workspace_id` uses the latest, an unknown ID is rejected with nothing written, and history (run
  list, counts, ledger) is scoped to the selected workspace;
- OCR on a rotated, image-only scan. The scan is drawn with the font bundled in Pillow (pinned), not rendered
  from a non-embedded-font PDF, so the test input is the same on every host. Rendering through Poppler depended
  on host fonts: with none installed, Poppler 26 produced a blank page.

## Scope and assumptions

- One invoice per PDF, one PO per invoice, USD only, explicit line amounts, lines matched by SKU.
- An invoice with no printed PO goes to `HOLD` with candidate POs. It is never auto-allocated.
- If no ISO code is printed, a `$` sign is read as USD. The note is kept on the currency field.
- A held or blocked run contributes zero to PO balances.
- Each CSV import creates a new workspace. This isolates test scenarios. Browser uploads go to the workspace shown
  on the upload page; API uploads without `workspace_id` go to the latest workspace.
- Not yet built: reviewer confirmation and re-check of held invoices, overlay of evidence boxes on the PDF,
  deployment, and the remaining exception scenarios planned for the next milestone.
