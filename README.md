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

This checkpoint is **Milestone 1** (the vertical slice): procurement import, upload and job lifecycle, native and OCR
extraction with page-level evidence, exact vendor and PO matching, atomic allocation, and live and history views.

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
2. Upload an invoice PDF. You land on the live run view, which shows stages from persisted events, the
   decision, the next action, the PO balance before and after, every extracted field with its page, method
   (`native`/`ocr`), confidence and position, every rule check, and the original PDF side by side.
3. **History** lists runs, outcomes and the PO ledger.

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
| POST | `/runs` | PDF upload. Stores the file and queues the run, then returns `202` with `run_id`. |
| GET | `/runs` | Run history |
| GET | `/runs/{id}` | Events, extraction plus evidence, decision, allocation, current PO balance, timings |
| GET | `/runs/{id}/pdf` | The original uploaded PDF |
| POST | `/runs/{id}/finalize` | Retries finalization from the stored extraction. Idempotent: never a second allocation. |
| POST | `/runs/{id}/retry` | Re-queues a `FAILED` run |
| GET | `/ledger` | Read-only ledger: authorized, allocated and remaining net and quantity per PO and line, plus allocations |

## How it works

```
upload ─► persist PDF + run (QUEUED) ─► worker claims run
        ─► per page: native text (pdfplumber) if usable, else Poppler render + Tesseract OCR
        ─► layout parser: labelled header fields + line-item table, each value with page/bbox/confidence
        ─► store extraction (outside the ledger transaction)
        ─► BEGIN IMMEDIATE: reread PO + allocations, run pure rules, write decision + allocation + event, COMMIT
```

- **Extraction** (`app/extraction/`) only produces an `ExtractedInvoice`. Values come only from a labelled
  header field (for example `Invoice number`, `Purchase order`, `Subtotal`, `Total due (USD)`) or from a row of the
  line-item table. A number printed elsewhere is never used. Scans are skew-corrected using Tesseract's own line
  groupings before tokens are grouped into rows. Missing or conflicting values stay `null` and are listed as issues.
  The app makes no model calls at runtime.
- **Decision** (`app/rules.py`) is a pure function, `evaluate(invoice, snapshot)`, with no I/O. Rules run in priority order:
  evidence (every critical value present and at or above OCR confidence 80), unique approved vendor by
  normalized legal name, business duplicate (vendor and invoice number), printed PO (exists, same vendor, same
  currency, OPEN), arithmetic (qty × rate = amount, lines = net, net + tax = gross), SKU-to-PO-line mapping, unit
  price within **1% inclusive**, and a **strict cumulative cap** on net and quantity, counting prior allocations.
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
- finalize retry and concurrent finalize (one allocation);
- database uniqueness;
- restart persistence;
- corrupt-PDF failure;
- OCR on a rotated, image-only scan.

## Scope and assumptions (Milestone 1)

- One invoice per PDF, one PO per invoice, USD only, explicit line amounts, lines matched by SKU.
- An invoice with no printed PO goes to `HOLD` with candidate POs. It is never auto-allocated.
- If no ISO code is printed, a `$` sign is read as USD. The note is kept on the currency field.
- A held or blocked run contributes zero to PO balances.
- Each CSV import creates a new workspace, and new runs go to the latest workspace. This isolates test scenarios.
- Not yet built: reviewer confirmation and re-check of held invoices, overlay of evidence boxes on the PDF,
  deployment, and the remaining exception scenarios planned for the next milestone.
