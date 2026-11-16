"""FastAPI application: API + AP-facing UI."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .db import add_event, db, init_db, latest_workspace_id, now_iso, transaction
from .extraction import tool_versions
from .ledger import finalize, ledger_report, load_snapshot
from .money import fmt_cents
from .procurement import ImportError_, import_procurement
from .rules import po_balance
from .worker import Worker, recover_interrupted

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["money"] = lambda c, cur="USD": fmt_cents(c, cur or "USD")

worker: Optional[Worker] = None


def _wake_worker() -> None:
    if worker is not None:
        worker.wake()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global worker
    init_db()
    recovered = recover_interrupted()
    if recovered:
        logging.getLogger("startup").info("recovered interrupted runs: %s", recovered)
    worker = Worker()
    worker.start()
    yield
    worker.stop()
    worker = None


app = FastAPI(title="Zamp PS-1 invoice processing", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.APP_PASSWORD and request.url.path != "/health":
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.APP_USERNAME or "ap") and secrets.compare_digest(
                    pw, config.APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="ap"'})
    return await call_next(request)


# ----------------------------------------------------------------------------- helpers

def _ms_between(a: Optional[str], b: Optional[str]) -> Optional[int]:
    if not a or not b:
        return None
    return int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() * 1000)


def _run_summary(r) -> dict[str, Any]:
    return {
        "run_id": r["id"], "workspace_id": r["workspace_id"], "filename": r["filename"], "status": r["status"],
        "decision": r["decision"], "reason_code": r["reason_code"], "reason": r["reason"],
        "vendor_id": r["vendor_id"], "po_id": r["po_id"], "invoice_number": r["invoice_number"],
        "invoice_date": r["invoice_date"], "gross_cents": r["gross_cents"], "currency": r["currency"],
        "created_at": r["created_at"], "finished_at": r["finished_at"],
        "processing_ms": _ms_between(r["created_at"], r["finished_at"]),
    }


def _run_detail(conn, run_id: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "run not found")
    events = [
        {"id": e["id"], "ts": e["ts"], "stage": e["stage"], "status": e["status"], "message": e["message"],
         "data": json.loads(e["data_json"]) if e["data_json"] else None}
        for e in conn.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,))
    ]
    out = _run_summary(r)
    out.update({
        "sha256": r["sha256"], "size_bytes": r["size_bytes"], "attempts": r["attempts"], "error": r["error"],
        "started_at": r["started_at"], "decided_at": r["decided_at"], "policy_version": r["policy_version"],
        "events": events,
        "extraction": json.loads(r["extraction_json"]) if r["extraction_json"] else None,
        "decision_detail": json.loads(r["decision_json"]) if r["decision_json"] else None,
        "pdf_url": f"/runs/{run_id}/pdf",
    })
    alloc = conn.execute("SELECT id, net_cents, po_id FROM allocations WHERE run_id=?", (run_id,)).fetchone()
    out["allocation_id"] = alloc["id"] if alloc else None
    if r["po_id"]:
        snap = load_snapshot(conn, r["workspace_id"])
        if r["po_id"] in snap.pos:
            out["po_balance_now"] = po_balance(snap, r["po_id"])
    out["model_calls"] = (out["extraction"] or {}).get("model_calls", 0)
    return out


# ----------------------------------------------------------------------------- API

@app.get("/health")
def health() -> dict[str, Any]:
    with db() as conn:
        conn.execute("SELECT 1").fetchone()
        queued = conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
    tools = tool_versions()
    return {"status": "ok" if all(tools.values()) else "degraded", "db": "ok", "queue_depth": queued,
            "tools": tools, "policy_version": config.POLICY_VERSION, "data_dir": str(config.DATA_DIR)}


@app.post("/procurement/import", status_code=201)
async def procurement_import(vendors: UploadFile = File(...), purchase_orders: UploadFile = File(...),
                             po_lines: UploadFile = File(...)):
    files = {
        "vendors": (vendors.filename or "vendors.csv", await vendors.read()),
        "purchase_orders": (purchase_orders.filename or "purchase_orders.csv", await purchase_orders.read()),
        "po_lines": (po_lines.filename or "po_lines.csv", await po_lines.read()),
    }
    with db() as conn:
        try:
            return import_procurement(conn, files)
        except ImportError_ as exc:
            return JSONResponse(status_code=422, content={"detail": "import rejected", "errors": exc.errors})


@app.get("/procurement")
def procurement(workspace_id: Optional[str] = None) -> dict[str, Any]:
    with db() as conn:
        ws = workspace_id or latest_workspace_id(conn)
        if not ws:
            raise HTTPException(404, "no procurement data imported")
        snap = load_snapshot(conn, ws)
        return {"workspace_id": ws, "vendors": snap.vendors, "purchase_orders": list(snap.pos.values()),
                "po_lines": [l for ls in snap.po_lines.values() for l in ls]}


@app.post("/runs", status_code=202)
async def create_run(file: UploadFile = File(...), workspace_id: Optional[str] = Form(None)):
    content = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file exceeds {config.MAX_UPLOAD_BYTES} bytes")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(415, "file is not a PDF")
    with db() as conn:
        ws = workspace_id or latest_workspace_id(conn)
        if not ws or conn.execute("SELECT 1 FROM workspaces WHERE id=?", (ws,)).fetchone() is None:
            raise HTTPException(409, "import procurement CSVs before submitting invoices")
        run_id = "run_" + uuid.uuid4().hex[:12]
        path = config.UPLOAD_DIR / f"{run_id}.pdf"
        path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        with transaction(conn):
            conn.execute(
                "INSERT INTO runs (id, workspace_id, created_at, filename, sha256, size_bytes, pdf_path, status)"
                " VALUES (?,?,?,?,?,?,?, 'QUEUED')",
                (run_id, ws, now_iso(), Path(file.filename or "invoice.pdf").name, sha, len(content), str(path)))
            same = conn.execute("SELECT id FROM runs WHERE workspace_id=? AND sha256=? AND id<>? ORDER BY created_at",
                                (ws, sha, run_id)).fetchall()
            add_event(conn, run_id, "upload", "completed", f"Stored {len(content):,} bytes, sha256 {sha[:12]}…",
                      {"sha256": sha, "same_file_runs": [s["id"] for s in same]})
            add_event(conn, run_id, "queue", "completed", "Queued for processing")
    _wake_worker()
    return {"run_id": run_id, "status": "QUEUED", "workspace_id": ws, "links": {
        "self": f"/runs/{run_id}", "ui": f"/ui/runs/{run_id}"}}


@app.get("/runs")
def list_runs(workspace_id: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    with db() as conn:
        q = "SELECT * FROM runs" + (" WHERE workspace_id=?" if workspace_id else "") + " ORDER BY created_at DESC LIMIT ?"
        args = (workspace_id, limit) if workspace_id else (limit,)
        return {"runs": [_run_summary(r) for r in conn.execute(q, args)]}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    with db() as conn:
        return _run_detail(conn, run_id)


@app.get("/runs/{run_id}/pdf")
def get_pdf(run_id: str):
    with db() as conn:
        r = conn.execute("SELECT pdf_path, filename FROM runs WHERE id=?", (run_id,)).fetchone()
    if r is None or not Path(r["pdf_path"]).exists():
        raise HTTPException(404, "not found")
    return FileResponse(r["pdf_path"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{r["filename"]}"'})


@app.post("/runs/{run_id}/finalize")
def refinalize(run_id: str) -> dict[str, Any]:
    """Retry finalization from the stored extraction. Safe to call repeatedly: an already decided
    run is returned unchanged and never receives a second allocation."""
    with db() as conn:
        r = conn.execute("SELECT extraction_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "run not found")
        if not r["extraction_json"]:
            raise HTTPException(409, "run has no completed extraction yet")
        out = finalize(conn, run_id)
        if out["already_finalized"]:
            add_event(conn, run_id, "finalize_retry", "info", "Finalize retried; decision already committed, no change")
        else:
            with transaction(conn):
                conn.execute("UPDATE runs SET status='COMPLETED', finished_at=COALESCE(finished_at, ?) WHERE id=?",
                             (now_iso(), run_id))
        alloc_count = conn.execute("SELECT COUNT(*) FROM allocations WHERE run_id=?", (run_id,)).fetchone()[0]
        return {**out, "run_id": run_id, "allocations_for_run": alloc_count}


@app.post("/runs/{run_id}/retry", status_code=202)
def retry_run(run_id: str) -> dict[str, Any]:
    """Re-queue a FAILED run (technical failure). Decided runs cannot be re-run."""
    with db() as conn, transaction(conn):
        r = conn.execute("SELECT status, decision FROM runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "run not found")
        if r["status"] != "FAILED" or r["decision"] is not None:
            raise HTTPException(409, f"only FAILED runs can be retried (status {r['status']})")
        conn.execute("UPDATE runs SET status='QUEUED', attempts=0, error=NULL, finished_at=NULL WHERE id=?", (run_id,))
        add_event(conn, run_id, "queue", "completed", "Manually re-queued after failure")
    _wake_worker()
    return {"run_id": run_id, "status": "QUEUED"}


@app.get("/ledger")
def ledger(workspace_id: Optional[str] = None) -> dict[str, Any]:
    with db() as conn:
        ws = workspace_id or latest_workspace_id(conn)
        if not ws:
            raise HTTPException(404, "no procurement data imported")
        return ledger_report(conn, ws)


# ----------------------------------------------------------------------------- UI

@app.get("/", response_class=HTMLResponse)
def ui_home(request: Request):
    with db() as conn:
        ws = latest_workspace_id(conn)
        summary = None
        if ws:
            snap = load_snapshot(conn, ws)
            summary = {"workspace_id": ws, "vendors": len(snap.vendors), "pos": len(snap.pos),
                       "lines": sum(len(v) for v in snap.po_lines.values())}
    return templates.TemplateResponse(request, "index.html", {"ws": summary})


@app.get("/ui/runs/{run_id}", response_class=HTMLResponse)
def ui_run(request: Request, run_id: str):
    with db() as conn:
        if conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise HTTPException(404, "run not found")
    return templates.TemplateResponse(request, "run.html", {"run_id": run_id})


@app.get("/ui/history", response_class=HTMLResponse)
def ui_history(request: Request):
    with db() as conn:
        runs = [_run_summary(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 200")]
        ws = latest_workspace_id(conn)
        ledger_data = ledger_report(conn, ws) if ws else None
    counts: dict[str, int] = {}
    for r in runs:
        k = r["decision"] or r["status"]
        counts[k] = counts.get(k, 0) + 1
    return templates.TemplateResponse(request, "history.html", {"runs": runs, "counts": counts, "ledger": ledger_data})
