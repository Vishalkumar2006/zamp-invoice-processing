"use strict";
/* AP Invoice Processing UI. Plain JS; every figure shown comes from the existing API responses. */

const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
const enc = encodeURIComponent;
// Integer cents -> "6,000.00". Display only; no arithmetic is ever done on the formatted string.
const amount = (c) => {
  const a = Math.abs(c);
  return `${c < 0 ? "-" : ""}${Math.floor(a / 100).toLocaleString("en-US")}.${String(a % 100).padStart(2, "0")}`;
};
// A missing currency is shown as unresolved, never silently as USD.
const money = (c, cur) => {
  if (c === null || c === undefined) return "—";
  return cur ? `${esc(cur)} ${amount(c)}` : `${amount(c)} <span class="tag warn">currency unresolved</span>`;
};
const icon = (id, cls = "") => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const DECISION_ICON = { READY: "check-circle", HOLD: "alert", BLOCKED: "block", FAILED: "x-circle" };
const pill = (d) => (d ? `<span class="badge ${esc(d.toLowerCase())}">${icon(DECISION_ICON[d] || "info")}${esc(d)}</span>` : "—");
const fmtTs = (ts) => (ts ? ts.slice(11, 23) : "");
const fmtTime = (ts) => (ts ? ts.slice(11, 19) : "");
const fmtDateTime = (ts) => (ts ? `${ts.slice(0, 10)} ${ts.slice(11, 16)}` : "");
const fmtBytes = (n) => (n === null || n === undefined ? "" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const secs = (ms) => `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
const msBetween = (a, b) => (a && b ? new Date(b).getTime() - new Date(a).getTime() : null);
const plural = (n, word, many) => `${n} ${n === 1 ? word : many || word + "s"}`;
const FINAL = new Set(["COMPLETED", "FAILED"]);
// Reason and rule codes shown as words ("AMOUNT_EXCEEDS_PO" -> "Amount exceeds PO"); the exact code stays in the audit detail.
const KEEP_UPPER = { PO: "PO", POS: "POs", ID: "ID", SKU: "SKU" };
const humanize = (code) => String(code || "").split(/[_\s]+/).filter(Boolean).map((w, i) => {
  const u = w.toUpperCase();
  if (KEEP_UPPER[u]) return KEEP_UPPER[u];
  const l = w.toLowerCase();
  return i === 0 ? l.charAt(0).toUpperCase() + l.slice(1) : l;
}).join(" ");
const reasonLabel = (code) => (code ? `<span class="reason-label" title="Reason code ${esc(code)}">${esc(humanize(code))}</span>` : "");
// Plain-language names for the recorded processing stages (the raw stage name is kept in the audit detail).
const STAGE_TEXT = {
  upload: "Invoice received", queue: "Waiting to start", claim: "Processing started", extract: "Reading the invoice",
  parse: "Capturing invoice details", match_and_validate: "Validating vendor, purchase order and amounts",
  decide: "Decision recorded", complete: "Processing complete", failed: "Processing stopped", recover: "Resumed after a restart",
  finalize_retry: "Decision re-checked",
};

// Assign innerHTML only when it changed, so polling never resets scroll, focus or open disclosures.
function setHTML(el, html) {
  if (el && el._html !== html) { el.innerHTML = html; el._html = html; }
}

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${url} returned ${res.status}`);
  return res.json();
}

async function postForm(url, form) {
  const res = await fetch(url, { method: "POST", body: new FormData(form) });
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  return { res, body };
}

function alertBox(kind, iconId, html) {
  return `<div class="alert ${kind}">${icon(iconId)}<div>${html}</div></div>`;
}

function outcomeBadge(r) {
  if (r.decision) return pill(r.decision);
  if (r.status === "FAILED") return pill("FAILED");
  return `<span class="badge running"><span class="spinner"></span>${r.status === "QUEUED" ? "Waiting" : "Processing"}</span>`;
}

// ------------------------------------------------------------------------------------------- shell

function initShell() {
  // Table rows marked .row-link open their invoice; real links and buttons inside keep their own behaviour.
  document.addEventListener("click", (e) => {
    const tr = e.target.closest("tr.row-link");
    if (!tr || e.target.closest("a, button, input, select, label, summary")) return;
    if (String(window.getSelection && window.getSelection()).length) return;
    if (e.ctrlKey || e.metaKey) window.open(tr.dataset.href, "_blank", "noopener");
    else location.href = tr.dataset.href;
  });
}

// ------------------------------------------------------------------------------------------- home

function initHome() {
  const wsField = document.querySelector("#upload-form input[name=workspace_id]");
  const ws = wsField ? wsField.value : null;
  initImport();
  initUpload();
  if (ws) { loadMetrics(ws); loadSettings(ws); }
  loadRecent();
}

function initImport() {
  const imp = document.getElementById("import-form");
  const panel = document.getElementById("import-panel");
  const out = document.getElementById("import-result");
  const btn = document.getElementById("import-btn");
  document.getElementById("open-import").addEventListener("click", () => {
    panel.open = true;
    panel.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
    const first = imp.querySelector("input[type=file]");
    if (first) first.focus({ preventScroll: true });
  });
  const slots = [...imp.querySelectorAll(".file-slot")];
  const checkSlot = (slot) => {
    const input = slot.querySelector("input[type=file]");
    const state = slot.querySelector(".fs-state");
    const f = input.files && input.files[0];
    slot.classList.remove("has-file", "invalid");
    if (!f) { state.textContent = state.dataset.empty; slot.querySelector(".fs-action").textContent = "Choose"; return false; }
    const ok = /\.csv$/i.test(f.name);
    slot.classList.add(ok ? "has-file" : "invalid");
    state.textContent = ok ? `${f.name} · ${fmtBytes(f.size)}` : `${f.name} is not a .csv file`;
    slot.querySelector(".fs-action").textContent = "Change";
    return ok;
  };
  slots.forEach((slot) => slot.querySelector("input").addEventListener("change", () => { checkSlot(slot); setHTML(out, ""); }));
  imp.addEventListener("submit", async (e) => {
    e.preventDefault();
    const problems = slots.filter((s) => !checkSlot(s)).map((s) => s.querySelector(".fs-name").textContent);
    if (problems.length) {
      setHTML(out, alertBox("err", "alert", `<strong>Please choose a CSV file for each item.</strong> Missing or not a CSV: ${esc(problems.join(", "))}.`));
      return;
    }
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span><span>Loading procurement data…</span>`;
    setHTML(out, "");
    let res, body;
    try { ({ res, body } = await postForm("/procurement/import", imp)); } catch (err) { res = { ok: false, status: 0 }; body = null; }
    if (res.ok) {
      const st = body.settings || {};
      const notes = [st.date_order ? `numeric dates read as ${st.date_order === "MDY" ? "month/day/year" : "day/month/year"}` : "",
        st.default_currency ? `${st.default_currency} used when no currency is printed` : ""].filter(Boolean).join("; ");
      setHTML(out, alertBox("ok", "check-circle", `<strong>Procurement data loaded</strong>: ${esc(body.vendors)} vendors,
        ${esc(body.purchase_orders)} purchase orders, ${esc(body.po_lines)} PO lines${notes ? ` (${esc(notes)})` : ""}. Switching to the new import…`));
      setTimeout(() => location.reload(), 900);
    } else {
      const errs = (body && body.errors) || [body && body.detail ? String(body.detail) : (res.status ? `The server returned status ${res.status}.` : "The connection failed. Please try again.")];
      setHTML(out, alertBox("err", "alert", `<strong>The procurement data was not loaded.</strong> Please correct the following and try again:<ul>${errs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`));
      btn.disabled = false;
      btn.innerHTML = `${icon("db")}<span>Set up procurement data</span>`;
    }
  });
}

function initUpload() {
  const up = document.getElementById("upload-form");
  const input = document.getElementById("invoice-file");
  const zone = document.getElementById("dropzone");
  const pillEl = document.getElementById("file-pill");
  const out = document.getElementById("upload-result");
  const btn = document.getElementById("upload-btn");
  const focusBtn = document.getElementById("focus-upload");
  const isPdf = (f) => /\.pdf$/i.test(f.name) || f.type === "application/pdf";
  const showFile = () => {
    const f = input.files && input.files[0];
    pillEl.hidden = !f;
    setHTML(out, "");
    if (!f) return;
    document.getElementById("file-name").textContent = f.name;
    document.getElementById("file-size").textContent = `${fmtBytes(f.size)}${isPdf(f) ? " · ready to submit" : ""}`;
    if (!isPdf(f)) setHTML(out, alertBox("err", "alert", `<strong>${esc(f.name)}</strong> is not a PDF. Please choose the invoice as a PDF file.`));
  };
  input.addEventListener("change", showFile);
  ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("dragging"); }));
  ["dragleave", "dragend"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("dragging")));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragging");
    if (input.disabled || !e.dataTransfer || !e.dataTransfer.files.length) return;
    input.files = e.dataTransfer.files;
    showFile();
  });
  document.getElementById("file-clear").addEventListener("click", () => { input.value = ""; showFile(); input.focus(); });
  focusBtn.addEventListener("click", () => {
    document.getElementById("process").scrollIntoView({ behavior: "auto", block: "start" });
    input.focus({ preventScroll: true });
    if (!input.files.length) input.click();
  });
  up.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = input.files && input.files[0];
    if (!f) { setHTML(out, alertBox("err", "alert", "Please choose an invoice PDF first.")); input.focus(); return; }
    if (!isPdf(f)) { setHTML(out, alertBox("err", "alert", `<strong>${esc(f.name)}</strong> is not a PDF.`)); return; }
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span><span>Submitting…</span>`;
    setHTML(out, "");
    const t0 = performance.now();
    let res, body;
    try { ({ res, body } = await postForm("/runs", up)); } catch (err) { res = { status: 0 }; body = null; }
    if (res.status === 202) {
      setHTML(out, alertBox("ok", "check-circle", `Invoice received in ${Math.round(performance.now() - t0)} ms. Opening the processing view…`));
      location.href = body.links.ui;
    } else {
      setHTML(out, alertBox("err", "alert", `<strong>The invoice could not be submitted.</strong> ${esc((body && body.detail) || (res.status ? `The server returned status ${res.status}.` : "The connection failed. Please try again."))}`));
      btn.disabled = false;
      btn.innerHTML = `${icon("arrow-right")}<span>Upload and process</span>`;
    }
  });
}

function groupLedger(pos) {
  const by = new Map();
  for (const p of pos) {
    const g = by.get(p.currency) || { cur: p.currency, auth: 0, alloc: 0, remain: 0, n: 0, allocs: 0 };
    g.auth += p.authorized_net_cents; g.alloc += p.allocated_net_cents; g.remain += p.remaining_net_cents;
    g.n += 1; g.allocs += p.allocation_count;
    by.set(p.currency, g);
  }
  return [...by.values()];
}

async function loadMetrics(ws) {
  const box = document.getElementById("metrics");
  if (!box) return;
  let runs, ledger;
  try {
    [runs, ledger] = await Promise.all([getJSON(`/runs?workspace_id=${enc(ws)}&limit=100000`), getJSON(`/ledger?workspace_id=${enc(ws)}`)]);
  } catch (err) {
    console.error(err);
    box.querySelectorAll("[data-m]").forEach((el) => { if (el.querySelector(".skeleton")) el.textContent = "—"; });
    return;
  }
  const n = {};
  for (const r of runs.runs) { const k = r.decision || r.status; n[k] = (n[k] || 0) + 1; }
  const busy = (n.RUNNING || 0) + (n.QUEUED || 0);
  const set = (k, html) => setHTML(box.querySelector(`[data-m="${k}"]`), html);
  set("total", String(runs.runs.length));
  set("total-sub", busy ? `<strong>${busy}</strong> processing now` : "for the latest import");
  set("ready", String(n.READY || 0));
  set("hold", String(n.HOLD || 0));
  set("blocked", String((n.BLOCKED || 0) + (n.FAILED || 0)));
  set("blocked-sub", `<strong>${n.BLOCKED || 0}</strong> blocked · <strong>${n.FAILED || 0}</strong> failed`);
  const groups = groupLedger(ledger.purchase_orders);
  let html;
  if (!groups.length) html = `<div class="metric-value">—</div><div class="metric-sub">No purchase orders in this procurement data</div>`;
  else if (groups.length === 1) {
    const g = groups[0];
    const pct = g.auth ? Math.min(100, (100 * g.alloc) / g.auth) : 0;
    html = `<div class="metric-value"><span class="cur">${esc(g.cur)}</span>${amount(g.remain)}</div>
      <div class="metric-sub"><strong>${money(g.alloc, g.cur)}</strong> allocated of <strong>${money(g.auth, g.cur)}</strong> authorized ·
        ${plural(g.n, "PO")}</div>
      <div class="meter sm" role="img" aria-label="${pct.toFixed(1)}% of authorization allocated"><span style="width:${pct.toFixed(2)}%"></span></div>`;
  } else {
    html = `<div class="metric-multi">${groups.map((g) => `<div><span>${esc(g.cur)} · ${plural(g.n, "PO")}</span>
      <strong>${money(g.remain, g.cur)}</strong></div>`).join("")}</div>
      <div class="metric-sub">Remaining per currency (amounts in different currencies are not added together)</div>`;
  }
  set("money", html);
  box.setAttribute("aria-busy", "false");
}

async function loadSettings(ws) {
  const dl = document.getElementById("ws-settings");
  if (!dl) return;
  const put = (k, html) => setHTML(dl.querySelector(`[data-s="${k}"]`), html);
  try {
    const p = await getJSON(`/procurement?workspace_id=${enc(ws)}`);
    const s = p.settings || {};
    put("date_order", s.date_order
      ? `<strong>${s.date_order === "MDY" ? "Month/day/year" : "Day/month/year"}</strong> <span class="muted">· e.g. 03/04/2026 = ${s.date_order === "MDY" ? "4 Mar" : "3 Apr"} 2026</span>`
      : `Not set <span class="muted">· ambiguous dates are sent for review</span>`);
    put("default_currency", s.default_currency
      ? `<strong>${esc(s.default_currency)}</strong> <span class="muted">· used only when no currency is printed</span>`
      : `Not set <span class="muted">· invoices without a printed currency are sent for review</span>`);
  } catch (err) {
    console.error(err);
    put("date_order", "—"); put("default_currency", "—");
  }
}

let recentTimer = null;
async function loadRecent() {
  const el = document.getElementById("recent");
  // Same workspace the upload form is bound to (none before the first import).
  const wsField = document.querySelector("#upload-form input[name=workspace_id]");
  const scope = wsField ? `&workspace_id=${enc(wsField.value)}` : "";
  let data;
  try { data = await getJSON(`/runs?limit=10${scope}`); } catch (err) {
    console.error(err);
    setHTML(el, `<div class="empty"><h3>Recent invoices could not be loaded</h3><p>Refresh the page to try again.</p></div>`);
    return;
  }
  if (!data.runs.length) {
    setHTML(el, `<div class="empty"><span class="empty-icon">${icon("receipt", "lg")}</span><h3>No invoices processed yet</h3>
      <p>${wsField ? "Drop an invoice PDF above to validate your first invoice." : "Set up procurement data, then submit an invoice PDF."}</p></div>`);
    return;
  }
  const rows = data.runs.map((r) => `<tr class="row-link" data-href="/ui/runs/${esc(r.run_id)}">
      <td><a class="cell-main" href="/ui/runs/${esc(r.run_id)}">${esc(r.invoice_number || (FINAL.has(r.status) ? "No invoice number" : "Reading invoice…"))}</a>
        <span class="cell-sub" title="${esc(r.filename)}">${esc(r.filename)}</span></td>
      <td class="hide-sm">${esc(r.vendor_id || "—")}</td>
      <td class="hide-sm"><span style="white-space:nowrap">${esc(fmtDateTime(r.created_at))}</span> <span class="muted">UTC</span></td>
      <td class="num money">${money(r.gross_cents, r.currency)}</td>
      <td class="reason-cell">${outcomeBadge(r)}${r.reason_code ? ` ${reasonLabel(r.reason_code)}` : ""}</td>
      <td class="num"><a class="view-link" href="/ui/runs/${esc(r.run_id)}" aria-label="Open invoice details (${esc(r.run_id)})">View details${icon("arrow-right", "sm")}</a></td></tr>`).join("");
  setHTML(el, `<div class="tablewrap"><table class="table-lg"><thead><tr><th>Invoice</th><th class="hide-sm">Vendor</th><th class="hide-sm">Submitted</th>
    <th class="num">Amount</th><th>Outcome</th><th><span class="sr-only">Open</span></th></tr></thead><tbody>${rows}</tbody></table></div>`);
  clearTimeout(recentTimer);
  if (data.runs.some((r) => !FINAL.has(r.status))) {
    recentTimer = setTimeout(() => { loadRecent(); if (wsField) loadMetrics(wsField.value); }, 2500);
  }
}

// ------------------------------------------------------------------------------------------- run: evidence helpers

const methodName = (m) => (m === "ocr" ? "scanned text (OCR)" : m === "native" ? "digital text" : String(m));

function evidenceCell(f) {
  if (f && !f.evidence && f.source === "workspace_config") return '<span class="src">workspace setting (not printed on the invoice)</span>';
  if (!f || !f.evidence) return '<span class="muted">not found on the invoice</span>';
  const ev = f.evidence;
  const label = ev.label ? ` · next to “${esc(ev.label)}”` : "";
  const box = ev.bbox.map((n) => Math.round(n)).join(", ");
  return `<span class="src">page ${esc(ev.page)} · ${esc(methodName(ev.method))} · confidence ${esc(ev.conf)}${label}<br>printed “${esc(ev.text)}” · position [${box}] pt</span>`;
}

// Business-duplicate comparison as recorded with the decision. Older runs have no `duplicate`.
function renderDuplicate(d) {
  const dup = d.duplicate;
  if (!dup || !dup.accepted) return "";
  const a = dup.accepted;
  const k = dup.business_key;
  const cur = a.currency;
  const val = (r, v) => {
    if (v === null || v === undefined) return '<span class="muted">not printed</span>';
    if (r.money) return money(v, cur);
    return esc(Array.isArray(v) ? v.join(", ") : v);
  };
  const label = { match: "match", conflict: "CONFLICT", not_printed: "not on this copy" };
  const tagCls = { match: "good", conflict: "bad", not_printed: "" };
  const rows = dup.comparison.map((r) => `<tr class="${r.status === "conflict" ? "fail" : ""}"><td>${esc(r.field)}</td>
    <td>${val(r, r.incoming)}</td><td>${val(r, r.accepted)}</td><td><span class="tag ${tagCls[r.status] || ""}">${esc(label[r.status] || r.status)}</span></td></tr>`).join("");
  const title = { corroborated: "Duplicate of an accepted invoice", conflict: "Same invoice number, different content",
    identity_uncertain: "Possible duplicate (uncertain OCR identity)",
    variant_corroborated: "Duplicate of an accepted invoice (number printed with leading zeros)",
    variant_uncertain: "Possible duplicate (number differs only by leading zeros; different date)",
    variant_period_unclear: "Possible duplicate (number differs only by leading zeros; service period unclear)" }[dup.outcome]
    || "Duplicate check";
  return `<h3 class="sub">${esc(title)}</h3>
    <dl class="facts" style="margin-bottom:12px">
      <dt>Business key</dt><dd>vendor <strong>${esc(k.vendor_id)}</strong> ${esc(k.vendor_name || "")} + invoice number
        <strong>${esc(k.invoice_number)}</strong>${k.invoice_number_raw && k.invoice_number_raw !== k.invoice_number
          ? ` (printed “${esc(k.invoice_number_raw)}”)` : ""}</dd>
      <dt>Accepted invoice</dt><dd><a href="/ui/runs/${enc(a.run_id)}">${esc(a.invoice_number)}</a>
        ${a.invoice_date ? `dated ${esc(a.invoice_date)}` : ""} · ${money(a.gross_cents, cur)} on ${esc(a.po_id)}
        · accepted ${esc((a.accepted_at || "").replace("T", " ").slice(0, 16))} · allocation #${esc(a.allocation_id)}</dd>
    </dl>
    <div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>Field</th><th>This document</th><th>Accepted invoice</th><th>Result</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

// Unit price vs PO price on every line, as recorded with the decision. Shown in full on a price hold.
function renderPrices(d) {
  const pc = d.price_check;
  if (!pc || !pc.lines || !pc.lines.length || d.reason_code !== "PRICE_VARIANCE") return "";
  const cur = pc.currency;
  const signed = (c) => (c > 0 ? "+" : c < 0 ? "-" : "") + money(Math.abs(c), cur);
  const rate = (r) => `${esc(cur)} ${esc(Number(r).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 }))}`;
  const src = (ev) => (ev ? `p${esc(ev.page)} · ${esc(ev.method.toUpperCase())} · conf ${esc(ev.conf)} · “${esc(ev.text)}”` : "—");
  const rows = pc.lines.map((l) => `<tr class="${l.within_tolerance ? "" : "fail"}">
    <td>${esc(l.line)}</td><td>${esc(l.sku)}</td><td class="num">${esc(l.quantity)}</td>
    <td class="num">${rate(l.invoice_unit_price)}</td><td class="num">${rate(l.po_unit_price)}</td>
    <td class="num"><strong>${esc(l.variance_display)}</strong></td>
    <td class="num"><strong>${signed(l.amount_difference_cents)}</strong></td>
    <td>${l.within_tolerance ? '<span class="tag good">within 1%</span>' : '<span class="tag bad">OUTSIDE 1%</span>'}</td>
    <td><span class="src">invoice ${src(l.invoice_evidence)}<br>PO ${esc(l.po_id)} line ${esc(l.po_line_id)} · ${esc(l.po_source)}</span></td></tr>`).join("");
  const offset = pc.offsetting
    ? `<div class="fin-verdict over" style="margin-top:12px">${icon("alert")}<div><strong>The differences offset.</strong> Subtotal ${money(pc.subtotal_cents, cur)} at invoice prices vs
       ${money(pc.subtotal_at_po_prices_cents, cur)} at PO prices (difference ${signed(pc.subtotal_difference_cents)}),
       so the total alone does not reveal the price changes. Each line is checked against its own PO price.</div></div>`
    : `<p class="note">Subtotal ${money(pc.subtotal_cents, cur)} at invoice prices vs ${money(pc.subtotal_at_po_prices_cents, cur)}
       at PO prices (difference ${signed(pc.subtotal_difference_cents)}).</p>`;
  return `<h3 class="sub">Unit prices vs ${esc(pc.po_id)} (${esc(pc.failed_lines.length)} of ${esc(pc.lines.length)} lines outside the
      ${esc(pc.tolerance_pct)}% per-line tolerance, inclusive)</h3>
    <div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>Line</th><th>SKU</th><th class="num">Qty</th><th class="num">Invoice price</th>
      <th class="num">PO price</th><th class="num">Variance</th><th class="num">Difference at invoiced qty (pre-tax)</th>
      <th>Result</th><th>Source</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${offset}
    <p class="note">Tax ${money(pc.tax_cents, cur)} is shown separately (total ${money(pc.gross_cents, cur)});
      ${esc(pc.note)}</p>`;
}

// Missing printed PO: every vendor PO compared with the invoice lines, side by side, as recorded.
function renderCandidates(d) {
  const pc = d.po_candidates;
  if (!pc || !pc.candidates || !pc.candidates.length) return "";
  const cur = pc.currency;
  const cs = pc.candidates;
  const ok = (v) => (v ? '<span class="ok-mark" aria-label="yes">✓</span>' : '<span class="bad-mark" aria-label="no">✗</span>');
  const cell = (c, html, good) => `<td class="${good === false ? "bad" : ""}">${html}</td>`;
  const row = (label, fn) => `<tr><th scope="row">${label}</th>${cs.map(fn).join("")}</tr>`;
  // Older stored decisions have only sku_ok; newer ones add `mapped` and, for vendor part numbers, product evidence.
  const mapped = (l) => (l.mapped !== undefined ? l.mapped : l.sku_ok);
  const productCell = (l) => {
    const comps = (l.product_comparison || []).map((x) => `<div class="${x.qualifies ? "" : "muted"}" style="margin-top:4px">
      ${x.qualifies ? ok(true) : ok(false)} ${esc(x.po_sku)} “${esc(x.po_description)}”:
      ${x.qualifies ? `product ${esc(x.shared_words.join(", "))} · specs ${esc(x.shared_specs.join(", "))}` : esc(x.problems.join("; "))}</div>`).join("");
    const head = mapped(l)
      ? `${ok(true)} as PO line ${esc(l.po_line_id)} ${esc(l.po_sku)} (inferred from product and specifications)`
      : `${ok(false)} no unique PO line by product and specifications`;
    return `${head}<br><span class="muted">${l.source ? esc(l.source) : ""}</span>${comps}`;
  };
  const lineRows = pc.invoice_lines.map((il, idx) => {
    const L = (c) => c.lines[idx] || {};
    return [
      row(`Line ${esc(il.line)}: ${il.description !== undefined ? "printed part" : "SKU"} ${esc(il.sku)}${il.description ? `<br><span class="muted">${esc(il.description)}</span>` : ""}`,
        (c) => cell(c, L(c).match_method === "product_specs" ? productCell(L(c)) : (L(c).sku_ok
        ? `${ok(true)} PO line ${esc(L(c).po_line_id)}<br><span class="muted">${esc(L(c).source)}</span>`
        : (L(c).repeated_mapping_of ? `${ok(false)} PO line ${esc(L(c).po_line_id)} already used by invoice line ${esc(L(c).repeated_mapping_of)}` : `${ok(false)} not on PO`)), mapped(L(c)))),
      row(`Unit price ${esc(il.unit_price)}`, (c) => cell(c, mapped(L(c))
        ? `${ok(L(c).price_ok)} PO ${esc(L(c).po_unit_price)} (${esc(L(c).variance_pct)}%)` : "—", L(c).price_ok)),
      row(`Quantity ${esc(il.quantity)}`, (c) => cell(c, mapped(L(c))
        ? `${ok(L(c).quantity_ok)} ${esc(L(c).remaining_quantity)} left of ${esc(L(c).authorized_quantity)}` : "—",
        L(c).quantity_ok)),
      row(`Amount ${money(il.amount_cents, cur)}`, (c) => cell(c, mapped(L(c))
        ? `${ok(L(c).amount_ok)} ${money(L(c).remaining_net_cents, cur)} left on line` : "—", L(c).amount_ok)),
    ].join("");
  }).join("");
  const viable = new Set(pc.viable_po_ids);
  const equal = new Set(pc.equal_total_ineligible_po_ids || []);
  const head = cs.map((c) => `<th scope="col" class="${viable.has(c.po_id) ? "fit" : equal.has(c.po_id) ? "ineligible" : ""}">${esc(c.po_id)}<br>
    <span style="font-weight:550">${viable.has(c.po_id) ? "Suggested match: needs confirmation" : equal.has(c.po_id) ? "Not eligible: same total only" : "Does not match"}</span></th>`).join("");
  return `<h3 class="sub">Possible purchase orders (no PO number printed on the invoice)</h3>
    <p class="note" style="margin:0 0 10px">Compared at decision time on: ${esc(pc.criteria)}.
      ${pc.delivery_reference ? `Delivery reference on the invoice: <strong>${esc(pc.delivery_reference)}</strong>.` : "No delivery reference could be read reliably."}
      <strong>Purchase order requires confirmation: none has been assigned automatically.</strong></p>
    <div class="tablewrap subtle-box"><table class="cands dense"><thead><tr><th scope="col"><span class="sr-only">Criterion</span></th>${head}</tr></thead><tbody>
      ${row("Source", (c) => `<td>${esc(c.source)}</td>`)}
      ${row("Description", (c) => `<td>${esc(c.description)}<br><span class="muted">issued ${esc(c.issued_on || "—")}</span></td>`)}
      ${row("Status / currency", (c) => cell(c, `${esc(c.status)} · ${esc(c.currency)}`, c.status === "OPEN" && c.currency === cur))}
      ${lineRows}
      ${row(`Invoice net ${money(pc.invoice_net_cents, cur)}`, (c) => cell(c,
        `${ok(c.total_ok)} ${money(c.remaining_net_cents, c.currency)} left of ${money(c.authorized_net_cents, c.currency)}
         (${esc(c.allocation_count)} allocation(s))`, c.total_ok))}
      ${row("Result", (c) => `<td>${c.viable ? "<strong>fits every criterion</strong>" : esc(c.failures.join("; "))}</td>`)}
    </tbody></table></div>`;
}

function renderRemittance(d) {
  const r = d.remittance;
  if (!r || r.outcome === "consistent" || r.outcome === "notice_verified") return "";
  const rows = r.comparison.map((c) => `<tr><td>${esc(c.label)}</td><td>${esc(c.invoice || "—")}</td>
    <td>${esc(c.on_file || "none on file")}</td><td>${esc(c.status)}</td>
    <td class="muted">${c.invoice_evidence ? `invoice p${esc(c.invoice_evidence.page)} · ${esc(c.invoice_evidence.method)}
      · conf ${esc(c.invoice_evidence.conf)}` : "—"} / ${esc(c.on_file_source)}</td></tr>`
    + (c.readings || []).map((x) => `<tr class="${c.status === "conflict" && x.clear ? "fail" : ""}"><td class="muted">↳ printed</td>
      <td>${esc(x.invoice)}</td><td></td><td>${esc(x.vs_file)}${x.clear ? "" : " (unclear read)"}</td>
      <td class="muted">${x.invoice_evidence ? `invoice p${esc(x.invoice_evidence.page)} · ${esc(x.invoice_evidence.method)}
        · conf ${esc(x.invoice_evidence.conf)} · “${esc(x.invoice_evidence.text)}”` : "—"}</td></tr>`).join("")).join("");
  const n = r.change_notice;
  const contact = r.verification_contact
    ? `Verify by phone with the number in the vendor master: <strong>${esc(r.verification_contact.phone)}</strong>
       (${esc(r.verification_contact.source)}). Do not use contact details printed on the invoice.`
    : "No verification phone in the vendor master: ask procurement for a trusted contact.";
  return `<h3 class="sub">Remittance details vs vendor master (account numbers masked)</h3>
    <div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>Field</th><th>Invoice</th><th>Vendor master</th><th>Result</th>
    <th>Source</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${n ? `<p class="note">Change notice on the invoice: “${esc(n.text)}” (page ${esc((n.evidence || {}).page)})</p>` : ""}
    <div class="fin-verdict over" style="margin-top:10px">${icon("shield")}<div>${contact}</div></div>`;
}

function vendorMatch(d) {
  const m = d.vendor_match;
  if (!m || m.method === "exact") return "";
  return `<br><span class="muted">matched by legal-suffix variant: printed “${esc(m.printed)}” vs legal name
    “${esc(m.legal_name)}” (${esc(m.source)}); unique among ${esc(m.vendors_compared)} vendors</span>`;
}

function lineMatches(d) {
  const rows = (d.line_matches || []).filter((m) => m.method === "description");
  if (!rows.length) return "";
  return `<dt>Line mapping</dt><dd>${rows.map((m) => m.po_line_id
    ? `Line ${esc(m.line)} (no SKU printed) → ${esc(m.po_id)} line ${esc(m.po_line_id)} ${esc(m.po_sku)}
       (${esc(m.source)}) by description: contains every identifying word of the PO line
       (${esc((m.shared_words || []).join(", "))}), no conflicting qualifier; the only qualifying line`
    : `Line ${esc(m.line)} (no SKU printed): ${esc(m.status === "ambiguous" ? "description fits more than one PO line"
       : "description matches no PO line")}${(m.candidates || []).map((c) => `<br><span class="muted">vs line
       ${esc(c.po_line_id)} ${esc(c.po_sku)} “${esc(c.po_description)}”: ${esc((c.problems || []).join(", ") || "qualifies")}</span>`).join("")}`).join("<br>")}</dd>`;
}

// ------------------------------------------------------------------------------------------- run: decision

const DECISION_COPY = {
  READY: { title: "Validated and allocated", icon: "check-circle",
    note: "This invoice has not been paid. Its pre-tax amount is allocated to the purchase order, reserving that authorization until payment is made through your normal process." },
  HOLD: { title: "Manual review required", icon: "alert",
    note: "No amount was allocated and nothing has been paid. The purchase order balance is unchanged." },
  BLOCKED: { title: "Cannot proceed", icon: "block",
    note: "No amount was allocated and nothing has been paid. The purchase order balance is unchanged." },
};
const LONG_REASON = 240;

function fieldName(f) {
  return String(f || "").replace(/_/g, " ").replace(/\bpo\b/gi, "PO");
}

function bindReasonToggle() {
  const btn = document.getElementById("reason-toggle");
  if (!btn || btn._bound) return;
  btn._bound = true;
  btn.addEventListener("click", () => {
    const box = document.getElementById("decision-reason");
    const open = btn.getAttribute("aria-expanded") !== "true";
    box.classList.toggle("clamped", !open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.querySelector("span").textContent = open ? "Show less" : "Show full reason";
  });
}

function renderDecision(run, d) {
  const card = document.getElementById("decision-card");
  const cls = d.decision.toLowerCase();
  card.className = `card decision ${cls}`;
  const copy = DECISION_COPY[d.decision] || { title: d.decision, icon: "info", note: "" };
  const candIds = d.candidate_po_ids || [];
  const a = d.allocation;
  const alloc = a
    ? `<strong>${money(a.net_cents, a.currency)}</strong> (pre-tax) to ${esc(a.po_id)}
       ${a.balance_before && a.balance_after ? `<br><span class="muted">PO balance ${money(a.balance_before.remaining_net_cents, a.currency)} →
       ${money(a.balance_after.remaining_net_cents, a.currency)}</span>` : ""}`
    : `<span class="muted">None. The purchase order balance is unchanged.</span>`;
  let po;
  if (d.po_id) po = `<strong>${esc(d.po_id)}</strong>`;
  else if (candIds.length === 1) po = `<strong>Purchase order requires confirmation</strong><br><span class="muted">Suggested: ${esc(candIds[0])} (not assigned)</span>`;
  else if (candIds.length > 1) po = `<strong>Purchase order requires confirmation</strong><br><span class="muted">Possible matches: ${esc(candIds.join(", "))} (none assigned)</span>`;
  else po = `<span class="muted">Not identified</span>`;
  const vendor = d.vendor_name ? `${esc(d.vendor_name)} <span class="muted">(${esc(d.vendor_id)})</span>` : esc(d.vendor_id || "Not identified");
  const extra = (d.reason_codes || []).filter((c) => c !== d.reason_code);
  const long = (d.reason || "").length > LONG_REASON;
  setHTML(document.getElementById("decision"), `
    <div class="decision-band">
      <span class="decision-icon">${icon(copy.icon)}</span>
      <div style="min-width:0">
        <div class="decision-eyebrow">Decision</div>
        <h2 class="decision-title" id="decision-heading">${esc(copy.title)}</h2>
        <div class="decision-codes">${pill(d.decision)}${reasonLabel(d.reason_code)}${extra.map((c) => reasonLabel(c)).join("")}</div>
      </div>
    </div>
    <div class="decision-body">
      <div class="next-action">${icon("arrow-right")}<div><div class="na-label">Recommended next action</div><p>${esc(d.next_action)}</p></div></div>
      <div class="reason-block">
        <div class="na-label muted-label">Why this decision</div>
        <p class="decision-reason${long ? " clamped" : ""}" id="decision-reason">${esc(d.reason)}</p>
        ${long ? `<button type="button" class="link-btn" id="reason-toggle" aria-expanded="false" aria-controls="decision-reason">${icon("chevron", "sm")}<span>Show full reason</span></button>` : ""}
      </div>
      <dl class="facts">
        <dt>Vendor</dt><dd>${vendor}${vendorMatch(d)}</dd>
        ${lineMatches(d)}
        <dt>Purchase order</dt><dd>${po}</dd>
        ${d.review_field ? `<dt>Item to review</dt><dd>${esc(fieldName(d.review_field))}</dd>` : ""}
        <dt>Amount allocated to purchase order</dt><dd>${alloc}</dd>
      </dl>
      <p class="no-pay">${icon("info")}<span>${esc(copy.note)}</span></p>
    </div>`);
  bindReasonToggle();
}

function renderFailed(run) {
  const card = document.getElementById("decision-card");
  card.className = "card decision failed";
  setHTML(document.getElementById("decision"), `
    <div class="decision-band">
      <span class="decision-icon">${icon("x-circle")}</span>
      <div style="min-width:0">
        <div class="decision-eyebrow">Processing status</div>
        <h2 class="decision-title" id="decision-heading">Processing could not be completed</h2>
        <div class="decision-codes">${pill("FAILED")}<span class="reason-label">Technical problem</span></div>
      </div>
    </div>
    <div class="decision-body">
      <div class="next-action">${icon("arrow-right")}<div><div class="na-label">Recommended next action</div>
        <p>Retry processing. If it fails again, check that the file is a complete, readable invoice PDF and submit it again.</p></div></div>
      <p class="decision-reason">This is a technical processing problem, not a business decision about the invoice.
        No decision was made, no amount was allocated and nothing has been paid.</p>
      <details class="disclosure" style="margin-bottom:14px"><summary>${icon("info")}Technical details${icon("chevron", "chev")}</summary>
        <div class="disc-body"><p class="src" style="margin:0">${esc(run.error)}</p><p class="note">Attempts: ${esc(run.attempts)}</p></div></details>
      <p style="margin:0"><button type="button" class="btn btn-primary" id="retry-btn">${icon("refresh")}Retry processing</button></p>
    </div>`);
  const btn = document.getElementById("retry-btn");
  if (btn && !btn._bound) {
    btn._bound = true;
    btn.onclick = () => {
      btn.disabled = true;
      fetch(`/runs/${enc(run.run_id)}/retry`, { method: "POST" }).then(() => location.reload());
    };
  }
}

// ------------------------------------------------------------------------------------------- run: financial validation

function capVisual(c, cur, d, poId) {
  const prior = c.previously_allocated;
  const incoming = c.incoming;
  const room = Math.max(c.remaining, 0);
  const within = Math.min(incoming, room);
  const over = c.status === "fail";
  const scale = Math.max(c.authorized, c.projected, 1);
  const w = (v) => `${((100 * Math.max(v, 0)) / scale).toFixed(3)}%`;
  const capPct = (100 * c.authorized) / scale;
  const after = c.remaining - incoming;
  const allocated = d.decision === "READY";
  const priorN = (d.authorization.prior_invoices || []).length;
  let verdict;
  if (over) {
    verdict = `<div class="fin-verdict over">${icon("alert")}<div>This invoice's pre-tax net of <strong>${money(incoming, cur)}</strong> exceeds the
      <strong>${money(c.remaining, cur)}</strong> remaining on ${esc(poId)} by <strong>${money(c.excess, cur)}</strong>
      (${money(prior, cur)} already allocated of ${money(c.authorized, cur)} authorized). No allocation was recorded.</div></div>`;
  } else if (allocated) {
    verdict = `<div class="fin-verdict fits">${icon("check-circle")}<div>Fits the remaining authorization: <strong>${money(incoming, cur)}</strong> allocated,
      leaving <strong>${money(after, cur)}</strong> on ${esc(poId)}.</div></div>`;
  } else {
    verdict = `<p class="note">${icon("info", "sm")} The PO total would not be exceeded (${money(after, cur)} would remain), but no amount
      was allocated because of the ${esc(d.decision)} decision above.</p>`;
  }
  return `
    <div class="fin-figures">
      <div><div class="fl">Authorized</div><div class="fv">${money(c.authorized, cur)}</div><div class="fs">PO total, pre-tax</div></div>
      <div><div class="fl"><span class="swatch sw-prior"></span>Allocated before</div><div class="fv">${money(prior, cur)}</div>
        <div class="fs">${plural(priorN, "earlier invoice")}</div></div>
      <div><div class="fl"><span class="swatch sw-room"></span>Remaining before</div><div class="fv">${money(c.remaining, cur)}</div>
        <div class="fs">available for this invoice</div></div>
      <div class="incoming ${over ? "over" : "fits"}"><div class="fl"><span class="swatch ${over ? "sw-in-over" : "sw-in"}"></span>This invoice</div>
        <div class="fv">${money(incoming, cur)}</div><div class="fs">${over ? `exceeds by ${money(c.excess, cur)}` : `leaves ${money(after, cur)}`}</div></div>
    </div>
    <div class="capbar-wrap" role="img" aria-label="Authorized ${esc(money(c.authorized, cur))}; allocated before ${esc(money(prior, cur))}; this invoice ${esc(money(incoming, cur))}${over ? `; exceeds by ${esc(money(c.excess, cur))}` : ""}">
      <div class="capbar">
        <span class="seg prior" style="width:${w(prior)}"></span>
        <span class="seg ${over ? "in-over" : "in"}" style="width:${w(within)}"></span>
        ${over ? `<span class="seg excess" style="width:${w(c.excess)}"></span>` : ""}
        <span class="cap ${capPct > 85 ? "at-end" : ""}" style="left:${capPct.toFixed(3)}%"><span>Authorized ${esc(amount(c.authorized))}</span></span>
      </div>
      <div class="cap-legend">
        <span><span class="swatch sw-prior"></span>Allocated before</span>
        <span><span class="swatch ${over ? "sw-in-over" : "sw-in"}"></span>This invoice${over ? " (within authorization)" : ""}</span>
        ${over ? `<span><span class="swatch sw-excess"></span>Excess over authorization</span>` : ""}
        ${!over && after > 0 ? `<span><span class="swatch sw-room"></span>Still available</span>` : ""}
      </div>
    </div>
    ${verdict}`;
}

// Cap comparisons exactly as recorded with the decision (not the live ledger).
function renderAuthorization(run, d) {
  const a = d.authorization;
  if (!a) return "";
  const cur = a.currency;
  const fmt = (c, v) => (c.measure === "net" ? money(v, cur) : esc(v));
  const shown = a.comparisons;
  if (!shown.length) return "";
  const failed = shown.filter((c) => c.status === "fail").length;
  const rows = shown.map((c) => `<tr class="${esc(c.status)}"><td>${esc(c.label)}<br><span class="muted">${
      c.measure === "net" ? "net amount" : "quantity"}</span></td>
    <td class="num">${fmt(c, c.authorized)}</td><td class="num">${fmt(c, c.previously_allocated)}</td>
    <td class="num">${fmt(c, c.remaining)}</td><td class="num">${fmt(c, c.incoming)}</td>
    <td class="num">${fmt(c, c.projected)}</td><td class="num"><strong>${c.status === "fail" ? fmt(c, c.excess) : "—"}</strong></td>
    <td>${c.status === "fail" ? '<span class="tag bad">exceeds</span>' : '<span class="tag good">within</span>'}</td></tr>`).join("");
  const prior = a.prior_invoices.length
    ? `<p class="note">Previously accepted on ${esc(a.po_id)}: ${a.prior_invoices.map((p) =>
        `<a href="/ui/runs/${enc(p.run_id)}">${esc(p.invoice_number)}</a>${p.invoice_date ? ` dated ${esc(p.invoice_date)}` : ""}
         (${money(p.net_cents, cur)} net${p.lines.map((l) => `, ${esc(l.quantity)} × ${esc(l.sku)}`).join("")})`).join("; ")}</p>`
    : `<p class="note">No earlier accepted invoices on ${esc(a.po_id)}.</p>`;
  return `<h3 class="sub">Authorization limits${failed ? ` <span class="tag bad">${failed} failed</span>` : ` <span class="tag good">all within</span>`}</h3>
    <div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>Cap</th><th class="num">Authorized</th><th class="num">Previously allocated</th>
      <th class="num">Remaining</th><th class="num">This invoice (pre-tax)</th><th class="num">Projected</th><th class="num">Excess</th><th>Result</th></tr></thead>
      <tbody>${rows}</tbody></table></div>${prior}`;
}

// Live ledger summary (left column); the per-line table is in the full-width evidence card.
function renderBalance(bal, d) {
  const a = d && d.allocation;
  const pct = bal.authorized_net_cents ? Math.min(100, (100 * bal.allocated_net_cents) / bal.authorized_net_cents) : 0;
  const cur = bal.currency;
  const thisInv = a
    ? `<p class="note">This invoice: remaining before ${money(a.balance_before.remaining_net_cents, cur)} → after ${money(a.balance_after.remaining_net_cents, cur)}.</p>`
    : `<p class="note">No amount was allocated for this invoice, so this balance did not change.</p>`;
  return `<h3 class="sub">Current PO balance · ${esc(bal.po_id)}</h3>
    <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:13px">
      <span><strong>${money(bal.remaining_net_cents, cur)}</strong> remaining of ${money(bal.authorized_net_cents, cur)} authorized</span>
      <span class="muted">${plural(bal.allocation_count, "allocation")} to date</span></div>
    <div class="meter" style="margin:8px 0 4px" role="img" aria-label="${pct.toFixed(1)}% allocated"><span style="width:${pct.toFixed(2)}%"></span></div>
    ${thisInv}`;
}

function renderBalanceLines(bal) {
  const cur = bal.currency;
  const rows = bal.lines.map((l) => `<tr><td>${esc(l.line_id)}</td><td>${esc(l.sku)}</td><td class="num">${esc(l.unit_price)}</td>
    <td class="num">${esc(l.authorized_quantity)} / ${esc(l.allocated_quantity)} / ${esc(l.remaining_quantity)}</td>
    <td class="num">${money(l.authorized_net_cents, cur)}</td><td class="num">${money(l.allocated_net_cents, cur)}</td>
    <td class="num">${money(l.remaining_net_cents, cur)}</td></tr>`).join("");
  return `<h3 class="sub">Current PO lines · ${esc(bal.po_id)} <span class="muted" style="font-weight:500">(${plural(bal.allocation_count, "allocation")})</span></h3>
    <div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>Line</th><th>SKU</th><th class="num">Unit price</th><th class="num">Qty auth / used / left</th>
      <th class="num">Net auth</th><th class="num">Net used</th><th class="num">Net left</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderFinancial(run, d) {
  const a = d && d.authorization;
  const bal = run.po_balance_now;
  if (!a && !bal) return;
  document.getElementById("balance-card").hidden = false;
  let html = "";
  if (a) {
    const po = (a.comparisons || []).find((c) => c.measure === "net" && c.scope === "po");
    if (po) html += capVisual(po, a.currency, d, a.po_id);
    const failed = a.comparisons.filter((c) => c.status === "fail").length;
    html += `<p class="note">${plural(a.comparisons.length, "authorization limit")} checked (PO total, line amounts and quantities): ${
      failed ? `<strong style="color:var(--blocked)">${failed} exceeded</strong>` : "all within"}. Full calculation under <em>Supporting evidence</em>.</p>`;
  }
  if (bal) html += `${a ? '<hr class="divider">' : ""}${renderBalance(bal, d)}`;
  setHTML(document.getElementById("balance-sub"), esc(a
    ? `As of the decision${run.decided_at ? ` (${run.decided_at.replace("T", " ").slice(0, 16)} UTC)` : ""}; current balance below`
    : "Current PO balance"));
  setHTML(document.getElementById("balance"), html);
}

// ------------------------------------------------------------------------------------------- run: extracted data

const FIELD_DEFS = [["Supplier", "supplier_name"], ["Invoice number", "invoice_number"], ["Invoice date", "invoice_date"],
  ["PO reference", "po_reference"], ["Service period", "service_period"], ["Delivery reference", "delivery_reference"],
  ["Currency", "currency"], ["Net / subtotal", "net"], ["Tax", "tax"], ["Freight", "freight"], ["Total", "gross"]];
const MONEY_FIELDS = ["net", "tax", "gross", "freight"];

function pagesSummary(x) {
  return x.pages.map((p) => {
    const conf = p.mean_conf !== null ? ` (mean conf ${p.mean_conf}, skew ${p.skew_slope})` : "";
    return `page ${p.page}: ${p.method.toUpperCase()}${conf}, ${p.duration_ms} ms`;
  }).join("; ");
}

function docKind(x) {
  const ocr = x.pages.some((p) => p.method === "ocr");
  const nat = x.pages.some((p) => p.method !== "ocr");
  return ocr && nat ? "Partly scanned PDF" : ocr ? "Scanned PDF" : "Digital PDF";
}

function renderKeyFields(x) {
  document.getElementById("fields-card").hidden = false;
  const inv = x.invoice;
  const cur = inv.currency.value;
  const optional = new Set(["service_period", "delivery_reference", "freight"]);
  const cells = FIELD_DEFS.filter(([, k]) => !optional.has(k) || (inv[k] && inv[k].value !== null && inv[k].value !== undefined))
    .map(([label, k]) => {
      const f = inv[k] || {};
      const has = f.value !== null && f.value !== undefined;
      const shown = has ? (MONEY_FIELDS.includes(k) ? money(f.value, cur) : esc(f.value)) : esc(f.note || "Not found on the invoice");
      const note = has && (f.source === "workspace_config" || (f.note && (k === "invoice_date" || k === "currency")))
        ? `<div class="fg-note">${esc(f.note || "from the workspace settings")}</div>` : "";
      return `<div class="${k === "gross" ? "total" : ""}"><div class="fg-label"><span>${label}</span></div>
        <div class="fg-value ${has ? "" : "missing"}">${shown}</div>${note}</div>`;
    });
  if (cells.length % 2) cells.push("<div></div>");
  setHTML(document.getElementById("fields-sub"), esc(`${docKind(x)} · ${plural(x.pages.length, "page")}`));
  setHTML(document.getElementById("fields"), `<div class="fields-grid">${cells.join("")}</div>
    ${inv.issues.length ? `<div class="alert warn" style="margin-top:12px">${icon("alert")}<div><strong>Could not be read reliably:</strong> ${esc(inv.issues.join("; "))}</div></div>` : ""}
    <p class="note">Where each value was found on the invoice${x.pages.some((p) => p.method === "ocr") ? ", with text-recognition confidence," : ""}
      is shown under <em>Invoice details &amp; sources</em> below.</p>`);
}

function renderEvidence(x) {
  const inv = x.invoice;
  const cur = inv.currency.value;
  const val = (k, f) => {
    if (f.value === null || f.value === undefined) return `<span class="muted">${esc(f.note || "—")}</span>`;
    const shown = MONEY_FIELDS.includes(k) ? money(f.value, cur) : esc(f.value);
    // Values resolved from the workspace configuration (default currency, numeric date order) say so.
    return f.source === "workspace_config" || (f.note && (k === "invoice_date" || k === "currency"))
      ? `${shown}<br><span class="muted">${esc(f.note || "from the workspace configuration")}</span>` : shown;
  };
  const frows = FIELD_DEFS.map(([label, k]) => `<tr><td>${label}</td><td>${val(k, inv[k] || {})}</td><td>${evidenceCell(inv[k])}</td></tr>`).join("");
  setHTML(document.getElementById("evidence"), `
    <p class="note" style="margin:0 0 12px">${esc(docKind(x))}: ${esc(pagesSummary(x))}.</p>
    <div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>Field</th><th>Value</th><th>Where it was found</th></tr></thead><tbody>${frows}</tbody></table></div>
    ${(inv.currency_indicators || []).length ? `<p class="note">Currency markers printed: ${esc([...new Set(inv.currency_indicators
      .map((c) => `${c.text} → ${c.currency === "$" ? "$ (dollar)" : c.currency} (p${c.page}, ${c.kind})`))].slice(0, 8).join("; "))}</p>` : ""}
    ${inv.issues.length ? `<p class="warn" style="margin-top:8px">Could not be read reliably: ${esc(inv.issues.join("; "))}</p>` : ""}`);
}

function renderLines(x) {
  const inv = x.invoice;
  const cur = inv.currency.value;
  document.getElementById("count-lines").textContent = inv.lines.length;
  if (!inv.lines.length) { setHTML(document.getElementById("lines"), '<p class="muted" style="margin:0">No line items were read from this invoice.</p>'); return; }
  const lrows = inv.lines.map((l) => `<tr><td><strong>${esc(l.sku.value)}</strong></td><td>${esc(l.description.value)}</td>
    <td class="num">${esc(l.quantity.value)}</td><td class="num">${esc(l.unit_price.raw || l.unit_price.value)}</td><td class="num"><strong>${money(l.amount.value, cur)}</strong></td></tr>
    <tr class="subrow"><td colspan="5"><span class="src">qty ${evidenceCell(l.quantity)}<br>rate ${evidenceCell(l.unit_price)}${l.unit_price.note ? ` (${esc(l.unit_price.note)})` : ""}<br>amount ${evidenceCell(l.amount)}</span></td></tr>`).join("");
  setHTML(document.getElementById("lines"), `<div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>SKU</th><th>Description</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead>
    <tbody>${lrows}</tbody></table></div>`);
}

function renderChecks(d) {
  const checks = (d && d.checks) || [];
  const count = document.getElementById("count-checks");
  const failed = checks.filter((c) => c.status === "fail").length;
  count.textContent = failed ? `${failed} not met` : checks.length;
  count.className = "count" + (failed ? " bad" : "");
  if (!d) return;
  const rows = checks.map((c) => {
    const src = c.compared && c.compared.source ? ` <span class="muted">(${esc(c.compared.source)})</span>` : "";
    const st = c.status === "pass" ? `${icon("check", "sm")}Passed` : c.status === "fail" ? `${icon("x", "sm")}Not met` : esc(c.status);
    return `<tr class="${esc(c.status)}"><td><span class="rule-label" title="Check ${esc(c.rule)}">${esc(humanize(c.rule))}</span></td><td><span class="status-cell ${esc(c.status)}">${st}</span></td><td>${esc(c.detail)}${src}</td></tr>`;
  }).join("");
  setHTML(document.getElementById("checks"), checks.length
    ? `<p class="note" style="margin:0 0 10px">${plural(checks.length, "check")} in the order they were applied · ${checks.length - failed} passed · ${failed} not met.</p>
       <div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="muted" style="margin:0">No validation checks were recorded for this decision.</p>');
}

function renderEvents(events) {
  document.getElementById("count-events").textContent = events.length;
  const rows = events.map((e) => {
    const st = e.status === "completed" ? `${icon("check", "sm")}Completed` : e.status === "failed" ? `${icon("x", "sm")}Failed`
      : e.status === "started" ? "Started" : esc(humanize(e.status));
    const cls = e.status === "completed" ? "pass" : e.status === "failed" ? "fail" : "info";
    return `<tr><td class="money">${esc(fmtTs(e.ts))}</td><td><span title="Stage ${esc(e.stage)}">${esc(STAGE_TEXT[e.stage] || humanize(e.stage))}</span></td>
      <td><span class="status-cell ${cls}">${st}</span></td><td class="muted">${esc(e.message)}</td></tr>`;
  }).join("");
  setHTML(document.getElementById("events"), `<p class="note" style="margin:0 0 10px">Every recorded processing step, oldest first. Times are UTC.</p>
    <div class="tablewrap subtle-box"><table class="dense"><thead><tr><th>Time (UTC)</th><th>Step</th><th>Status</th><th>Recorded detail</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

// Identifiers and versions for audit and support; deliberately kept off the main decision view.
function renderAudit(run) {
  const d = run.decision_detail;
  const x = run.extraction;
  const ts = (v) => (v ? `${esc(v.replace("T", " ").slice(0, 23))} UTC` : "—");
  const rows = [
    ["Processing reference", `<code>${esc(run.run_id)}</code>`],
    ["Procurement data reference (workspace ID)", `<code>${esc(run.workspace_id)}</code>`],
    ["Outcome", run.decision ? `${esc(run.decision)}` : esc(run.status)],
    ["Reason codes", d ? [d.reason_code, ...(d.reason_codes || []).filter((c) => c !== d.reason_code)].map((c) => `<code>${esc(c)}</code>`).join(", ") : "—"],
    ["Rules policy version", d ? `<code>${esc(d.policy_version)}</code>` : (run.policy_version ? `<code>${esc(run.policy_version)}</code>` : "—")],
    ["Allocation number", run.allocation_id ? `#${esc(run.allocation_id)}` : "None"],
    ["Model calls", esc(run.model_calls)],
    ["File", `${esc(run.filename)} · ${esc(fmtBytes(run.size_bytes))}`],
    ["File fingerprint (SHA-256)", `<code style="overflow-wrap:anywhere">${esc(run.sha256)}</code>`],
    ["Submitted", ts(run.created_at)],
    ["Processing started", ts(run.started_at)],
    ["Decision recorded", ts(run.decided_at)],
    ["Finished", ts(run.finished_at)],
    ["Processing time", run.processing_ms !== null ? esc(secs(run.processing_ms)) : "—"],
    ["Attempts", esc(run.attempts)],
    ["Pages read", x ? esc(pagesSummary(x)) : "—"],
    ["Reading time", x ? `${esc(x.duration_ms)} ms` : "—"],
  ];
  setHTML(document.getElementById("audit"), `<p class="note" style="margin:0 0 10px">Reference data for audit and support.</p>
    <dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`);
}

function renderEvidenceDetail(run, d) {
  const html = [renderRemittance(d), renderDuplicate(d), renderCandidates(d), renderPrices(d), renderAuthorization(run, d),
    run.po_balance_now ? renderBalanceLines(run.po_balance_now) : ""].filter(Boolean).join("");
  document.getElementById("evidence-detail-card").hidden = !html;
  if (html) setHTML(document.getElementById("evidence-detail"), html);
}

// ------------------------------------------------------------------------------------------- run: timeline

const STEPS = [
  { key: "received", label: "Received", icon: "inbox" },
  { key: "queued", label: "Started", icon: "clock" },
  { key: "extract", label: "Reading invoice", icon: "scan" },
  { key: "parse", label: "Capturing details", icon: "list" },
  { key: "checks", label: "Validation checks", icon: "shield" },
  { key: "decision", label: "Decision", icon: "flag" },
];

// Every step state below is derived from persisted run events; nothing is simulated.
function stepEvents(run) {
  const ev = run.events;
  const last = (list, stage, status) => { for (let i = list.length - 1; i >= 0; i--) if (list[i].stage === stage && list[i].status === status) return list[i]; return null; };
  let q = -1;
  ev.forEach((e, i) => { if (e.stage === "queue") q = i; });
  const attempt = q >= 0 ? ev.slice(q) : ev; // the latest queueing (a retry re-queues the run)
  const decide = last(ev, "decide", "completed");
  return {
    received: { done: last(ev, "upload", "completed") },
    queued: { start: q >= 0 ? ev[q] : null, done: last(attempt, "claim", "completed") },
    extract: { start: last(ev, "extract", "started"), done: last(ev, "extract", "completed") },
    parse: { done: last(ev, "parse", "completed") },
    checks: { start: last(ev, "match_and_validate", "started"), done: decide },
    decision: { done: decide },
  };
}

function stepDetail(key, info, run) {
  const d = run.decision_detail;
  if (key === "received") return `PDF · ${esc(fmtBytes(run.size_bytes))}`;
  if (key === "queued") return run.attempts > 1 ? `Attempt ${esc(run.attempts)}` : "Processing started";
  if (key === "extract") {
    const data = info.done.data || {};
    const pages = data.pages || [];
    const ocr = pages.some((p) => p.method === "ocr");
    return esc(`${ocr ? "Scanned" : "Digital"} PDF · ${plural(pages.length, "page")}${data.duration_ms !== undefined ? ` · ${secs(data.duration_ms)}` : ""}`);
  }
  if (key === "parse") {
    const inv = run.extraction && run.extraction.invoice;
    if (!inv) return "Invoice details captured";
    const n = inv.invoice_number.value;
    return esc(`${n ? `Invoice ${n}` : "No invoice number"} · ${plural(inv.lines.length, "line item")}${inv.po_reference.value ? ` · PO ${inv.po_reference.value}` : " · no PO number"}`);
  }
  if (key === "checks" && d) {
    const failed = (d.checks || []).filter((c) => c.status === "fail").length;
    return esc(`${plural((d.checks || []).length, "check")} · ${failed ? `${failed} not met` : "all passed"}`);
  }
  if (key === "decision" && d) return `${esc(d.decision)} · ${esc(humanize(d.reason_code))}`;
  return "";
}

function renderTimeline(run) {
  const info = stepEvents(run);
  const t0 = info.received.done ? info.received.done.ts : run.created_at;
  const firstOpen = STEPS.findIndex((s) => !info[s.key].done);
  const items = STEPS.map((s, i) => {
    const st = info[s.key];
    let state = "pending";
    if (st.done) state = "done";
    else if (i === firstOpen && run.status === "FAILED") state = "failed";
    else if (i === firstOpen && !FINAL.has(run.status)) state = "active";
    const dot = state === "done" ? icon("check") : state === "failed" ? icon("x") : state === "active" ? '<span class="spinner"></span>' : icon(s.icon);
    let stateText = { pending: "Pending", active: "In progress", failed: "Failed" }[state];
    if (state === "done") {
      const ms = msBetween(t0, st.done.ts);
      stateText = s.key === "received" ? `${fmtTime(st.done.ts)} UTC` : `+${secs(Math.max(ms, 0))}`;
    } else if (state === "active" && s.key === "queued") stateText = "Waiting to start";
    const detail = state === "done" ? stepDetail(s.key, st, run) : state === "failed" ? "Could not be completed (technical problem)" : "";
    return `<li class="step ${state}" ${state === "active" ? 'aria-current="step"' : ""}>
      <span class="step-dot">${dot}</span>
      <div class="step-label">${esc(s.label)}<span class="sr-only"> (${state})</span></div>
      <div class="step-state">${esc(stateText)}</div>
      ${detail ? `<div class="step-detail" title="${detail.replace(/<[^>]*>/g, "")}">${detail}</div>` : ""}</li>`;
  }).join("");
  setHTML(document.getElementById("stages"), items);

  const live = document.getElementById("live-line");
  const lastEv = run.events[run.events.length - 1];
  if (run.status === "COMPLETED") {
    live.className = "live-line ok";
    setHTML(live, `${icon("check-circle")}<span>Processing complete${run.processing_ms !== null ? ` in ${secs(run.processing_ms)}` : ""}${
      run.decision ? ` · decision <strong>${esc(run.decision)}</strong>` : ""}.</span>`);
  } else if (run.status === "FAILED") {
    live.className = "live-line err";
    setHTML(live, `${icon("x-circle")}<span>Processing could not be completed because of a technical problem. Details are below.</span>`);
  } else {
    live.className = "live-line";
    const step = lastEv ? (STAGE_TEXT[lastEv.stage] || "Processing") : "Waiting to start";
    setHTML(live, `<span class="spinner"></span><span>${esc(step)}${lastEv && lastEv.status === "started" ? "…" : ""}</span>`);
  }
}

// ------------------------------------------------------------------------------------------- run: page

function renderRun(run) {
  const d = run.decision_detail;
  const inv = run.extraction ? run.extraction.invoice : null;
  const number = run.invoice_number || (inv && inv.invoice_number.value) || null;
  const vendor = (d && d.vendor_name) || (inv && inv.supplier_name.value) || null;
  const title = number ? `Invoice ${number}` : run.filename || run.run_id;
  setHTML(document.getElementById("run-title"), esc(title));
  document.title = `${title} · Invoice Operations`;
  setHTML(document.getElementById("crumb-current"), esc(number || run.run_id));
  setHTML(document.getElementById("meta-file"), `${icon("file")}<span>${esc(run.filename)}${vendor ? ` · ${esc(vendor)}` : ""}</span>`);
  const metaPo = document.getElementById("meta-po");
  if (run.po_id) { metaPo.hidden = false; setHTML(metaPo, `${icon("ledger")}PO <code>${esc(run.po_id)}</code>`); }

  const st = document.getElementById("status");
  if (run.status === "COMPLETED" && run.decision) {
    st.className = `badge lg ${run.decision.toLowerCase()}`;
    setHTML(st, `${icon(DECISION_ICON[run.decision] || "info")}${esc(run.decision)}`);
  } else if (run.status === "FAILED") {
    st.className = "badge lg failed";
    setHTML(st, `${icon("x-circle")}FAILED`);
  } else {
    st.className = "badge lg running";
    setHTML(st, `<span class="spinner"></span>${run.status === "QUEUED" ? "Waiting" : "Processing"}`);
  }
  setHTML(document.getElementById("timing"), run.processing_ms !== null
    ? `${icon("clock")}Processed in ${secs(run.processing_ms)}`
    : `${icon("clock")}Submitted ${esc(fmtTime(run.created_at))} UTC`);

  renderTimeline(run);
  if (d) renderDecision(run, d);
  else if (run.status === "FAILED") renderFailed(run);
  renderFinancial(run, d);
  if (run.extraction) { renderKeyFields(run.extraction); renderEvidence(run.extraction); renderLines(run.extraction); }
  if (d) renderEvidenceDetail(run, d);
  renderChecks(d);
  renderEvents(run.events);
  renderAudit(run);
}

function initTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (t, focus) => {
    tabs.forEach((x) => {
      const on = x === t;
      x.setAttribute("aria-selected", on ? "true" : "false");
      x.tabIndex = on ? 0 : -1;
      document.getElementById(x.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) t.focus();
  };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => select(t, false));
    t.addEventListener("keydown", (e) => {
      const k = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (k === undefined) return;
      e.preventDefault();
      select(tabs[(k + tabs.length) % tabs.length], true);
    });
  });
}

// Name the procurement data a run was checked against: the latest import, or an earlier one (via the existing /procurement).
async function labelImport(workspaceId) {
  try {
    const latest = await getJSON("/procurement");
    const name = latest.workspace_id === workspaceId ? "Latest import" : "Earlier import";
    document.querySelectorAll(".ws-import-label").forEach((el) => { el.textContent = ` · ${name}`; el.hidden = false; });
    document.querySelectorAll(".ws-import-name").forEach((el) => { el.textContent = name; });
  } catch (err) {
    console.error(err);
  }
}

function initRun(runId) {
  initTabs();
  let labelled = false;
  let timer = null;
  let busy = false;
  const tick = async () => {
    if (busy) return;
    busy = true;
    try {
      const run = await getJSON(`/runs/${enc(runId)}`);
      renderRun(run);
      if (!labelled) { labelled = true; labelImport(run.workspace_id); }
      if (FINAL.has(run.status)) clearInterval(timer);
    } catch (err) {
      console.error(err);
    } finally {
      busy = false;
    }
  };
  tick();
  timer = setInterval(tick, 700);
}

// ------------------------------------------------------------------------------------------- history

function initHistory() {
  document.querySelectorAll(".alloc-toggle").forEach((b) => b.addEventListener("click", () => {
    const open = b.getAttribute("aria-expanded") !== "true";
    b.setAttribute("aria-expanded", open ? "true" : "false");
    document.getElementById(b.getAttribute("aria-controls")).hidden = !open;
  }));
  const rows = [...document.querySelectorAll("#runs-table tbody tr[data-outcome]")];
  const search = document.getElementById("run-search");
  if (!search) return;
  const chips = [...document.querySelectorAll(".filter-chip")];
  const count = document.getElementById("run-count");
  const none = document.getElementById("runs-nomatch");
  let filter = "";
  const apply = () => {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    rows.forEach((r) => {
      const ok = (!filter || r.dataset.outcome === filter) && (!q || r.dataset.search.includes(q));
      r.hidden = !ok;
      if (ok) shown += 1;
    });
    none.hidden = shown > 0;
    count.textContent = shown === rows.length ? plural(rows.length, "invoice") : `Showing ${shown} of ${rows.length}`;
  };
  search.addEventListener("input", apply);
  chips.forEach((c) => c.addEventListener("click", () => {
    filter = c.dataset.filter;
    chips.forEach((x) => x.setAttribute("aria-pressed", x === c ? "true" : "false"));
    apply();
  }));
  // While runs are still processing, refresh the server-rendered page (only when no filter is in use).
  if (rows.some((r) => r.dataset.outcome === "RUNNING" || r.dataset.outcome === "QUEUED")) {
    setTimeout(() => { if (!search.value && !filter) location.reload(); }, 3000);
  }
}
