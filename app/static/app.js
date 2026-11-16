"use strict";
const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
const money = (c, cur) => {
  if (c === null || c === undefined) return "—";
  const a = Math.abs(c);
  return `${cur || "USD"} ${c < 0 ? "-" : ""}${Math.floor(a / 100).toLocaleString("en-US")}.${String(a % 100).padStart(2, "0")}`;
};
const fmtTs = (ts) => (ts ? ts.slice(11, 23) : "");
const pill = (d) => (d ? `<span class="pill ${d.toLowerCase()}">${esc(d)}</span>` : "—");

async function postForm(url, form) {
  const res = await fetch(url, { method: "POST", body: new FormData(form) });
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  return { res, body };
}

function initHome() {
  const imp = document.getElementById("import-form");
  imp.addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = document.getElementById("import-result");
    out.className = "result";
    out.textContent = "Importing…";
    const { res, body } = await postForm("/procurement/import", imp);
    if (res.ok) {
      out.textContent = `Imported into ${body.workspace_id}: ${body.vendors} vendors, ${body.purchase_orders} POs, ${body.po_lines} lines.`;
      setTimeout(() => location.reload(), 700);
    } else {
      out.className = "result err";
      out.textContent = "Import rejected:\n" + ((body && body.errors) || [JSON.stringify(body)]).join("\n");
    }
  });
  const up = document.getElementById("upload-form");
  up.addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = document.getElementById("upload-result");
    out.className = "result";
    out.textContent = "Uploading…";
    const t0 = performance.now();
    const { res, body } = await postForm("/runs", up);
    if (res.status === 202) {
      out.textContent = `Accepted in ${Math.round(performance.now() - t0)} ms → ${body.run_id}`;
      location.href = body.links.ui;
    } else {
      out.className = "result err";
      out.textContent = `Upload failed (${res.status}): ${(body && body.detail) || ""}`;
    }
  });
  loadRecent();
}

async function loadRecent() {
  const el = document.getElementById("recent");
  const data = await (await fetch("/runs?limit=10")).json();
  if (!data.runs.length) { el.innerHTML = '<p class="muted">No runs yet.</p>'; return; }
  const rows = data.runs.map((r) => `<tr><td><a href="/ui/runs/${esc(r.run_id)}">${esc(r.run_id)}</a></td>
    <td>${esc(r.filename)}</td><td>${esc(r.invoice_number || "—")}</td><td class="num">${money(r.gross_cents, r.currency)}</td>
    <td>${esc(r.status)}</td><td>${pill(r.decision)}</td></tr>`).join("");
  el.innerHTML = `<div class="tablewrap"><table><thead><tr><th>Run</th><th>File</th><th>Invoice</th><th class="num">Amount</th>
    <th>Status</th><th>Decision</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function evidenceCell(f) {
  if (!f || !f.evidence) return '<span class="muted">not found</span>';
  const ev = f.evidence;
  const label = ev.label ? ` · next to “${esc(ev.label)}”` : "";
  const box = ev.bbox.map((n) => Math.round(n)).join(", ");
  return `<span class="src">p${ev.page} · ${ev.method.toUpperCase()} · conf ${ev.conf}${label}<br>“${esc(ev.text)}” @ [${box}] pt</span>`;
}

function renderDecision(run, d) {
  document.getElementById("decision-card").hidden = false;
  const cls = d.decision.toLowerCase();
  const cands = d.candidate_po_ids.length ? ` (candidates: ${esc(d.candidate_po_ids.join(", "))})` : "";
  const alloc = d.allocation
    ? `#${esc(run.allocation_id)} · ${money(d.allocation.net_cents, d.allocation.currency)} net on ${esc(d.allocation.po_id)}`
    : "none";
  const extraCodes = (d.reason_codes || []).filter((c) => c !== d.reason_code);
  document.getElementById("decision").innerHTML = `
    <div class="decision-big ${cls}">${esc(d.decision)} <span class="muted" style="font-size:13px">${esc(d.reason_code)}${
      extraCodes.length ? ` + ${esc(extraCodes.join(", "))}` : ""}</span></div>
    <p>${esc(d.reason)}</p>
    ${renderAuthorization(run, d)}
    <dl class="kv">
      <dt>Next action</dt><dd><strong>${esc(d.next_action)}</strong></dd>
      <dt>Vendor</dt><dd>${esc(d.vendor_id || "—")} ${esc(d.vendor_name || "")}</dd>
      <dt>Matched PO</dt><dd>${esc(d.po_id || "—")}${cands}</dd>
      ${d.review_field ? `<dt>Field to review</dt><dd>${esc(d.review_field)}</dd>` : ""}
      <dt>Allocation</dt><dd>${alloc}</dd>
      <dt>Policy</dt><dd>${esc(d.policy_version)} · model calls: ${esc(run.model_calls)}</dd>
    </dl>`;
  document.getElementById("checks-card").hidden = false;
  const rows = d.checks.map((c) => {
    const src = c.compared && c.compared.source ? ` <span class="muted">(${esc(c.compared.source)})</span>` : "";
    return `<tr class="${esc(c.status)}"><td>${esc(c.rule)}</td><td>${esc(c.status)}</td><td>${esc(c.detail)}${src}</td></tr>`;
  }).join("");
  document.getElementById("checks").innerHTML =
    `<table><thead><tr><th>Rule</th><th>Result</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// Cap comparisons exactly as recorded with the decision (not the live ledger).
function renderAuthorization(run, d) {
  const a = d.authorization;
  if (!a) return "";
  const cur = a.currency;
  const fmt = (c, v) => (c.measure === "net" ? money(v, cur) : esc(v));
  const shown = d.decision === "READY" ? a.comparisons : a.comparisons.filter((c) => c.status === "fail");
  if (!shown.length) return "";
  const rows = shown.map((c) => `<tr class="${esc(c.status)}"><td>${esc(c.label)}<br><span class="muted">${
      c.measure === "net" ? "net amount" : "quantity"}</span></td>
    <td class="num">${fmt(c, c.authorized)}</td><td class="num">${fmt(c, c.previously_allocated)}</td>
    <td class="num">${fmt(c, c.remaining)}</td><td class="num">${fmt(c, c.incoming)}</td>
    <td class="num">${fmt(c, c.projected)}</td><td class="num"><strong>${c.status === "fail" ? fmt(c, c.excess) : "—"}</strong></td></tr>`).join("");
  const prior = a.prior_invoices.length
    ? `<p>Previously accepted on ${esc(a.po_id)}: ${a.prior_invoices.map((p) =>
        `<a href="/ui/runs/${encodeURIComponent(p.run_id)}">${esc(p.invoice_number)}</a>${p.invoice_date ? ` dated ${esc(p.invoice_date)}` : ""}
         (${money(p.net_cents, cur)} net${p.lines.map((l) => `, ${esc(l.quantity)} × ${esc(l.sku)}`).join("")})`).join("; ")}</p>`
    : `<p class="muted">No earlier accepted invoices on ${esc(a.po_id)}.</p>`;
  const title = d.decision === "READY" ? "Authorization checks" : `Failed authorization checks (${shown.length})`;
  return `<h3 class="sub">${title}</h3>
    <p class="muted">Figures as recorded at decision time${run.decided_at ? ` (${esc(run.decided_at.replace("T", " ").slice(0, 19))} UTC)` : ""};
      the live ledger is shown separately below.</p>
    <div class="tablewrap"><table><thead><tr><th>Cap</th><th class="num">Authorized</th><th class="num">Previously allocated</th>
      <th class="num">Remaining</th><th class="num">This invoice (pre-tax)</th><th class="num">Projected</th><th class="num">Excess</th></tr></thead>
      <tbody>${rows}</tbody></table></div>${prior}`;
}

function renderBalance(bal, d) {
  document.getElementById("balance-card").hidden = false;
  const a = d && d.allocation;
  const pct = bal.authorized_net_cents ? Math.round((100 * bal.allocated_net_cents) / bal.authorized_net_cents) : 0;
  const cur = bal.currency;
  const rows = bal.lines.map((l) => `<tr><td>${esc(l.line_id)}</td><td>${esc(l.sku)}</td><td class="num">${esc(l.unit_price)}</td>
    <td class="num">${esc(l.authorized_quantity)} / ${esc(l.allocated_quantity)} / ${esc(l.remaining_quantity)}</td>
    <td class="num">${money(l.authorized_net_cents, cur)}</td><td class="num">${money(l.allocated_net_cents, cur)}</td>
    <td class="num">${money(l.remaining_net_cents, cur)}</td></tr>`).join("");
  const thisInv = a
    ? `<p class="muted">This invoice: remaining before ${money(a.balance_before.remaining_net_cents, cur)} → after ${money(a.balance_after.remaining_net_cents, cur)}.</p>`
    : "";
  document.getElementById("balance").innerHTML = `
    <div><strong>${esc(bal.po_id)}</strong>: ${money(bal.remaining_net_cents, cur)} remaining of ${money(bal.authorized_net_cents, cur)}
      authorized (${bal.allocation_count} allocation(s), live ledger now)</div>
    <div class="bar"><span style="width:${Math.min(pct, 100)}%"></span></div>
    ${thisInv}
    <table><thead><tr><th>Line</th><th>SKU</th><th class="num">Unit price</th><th class="num">Qty auth / used / left</th>
      <th class="num">Net auth</th><th class="num">Net used</th><th class="num">Net left</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderEvidence(x) {
  document.getElementById("evidence-card").hidden = false;
  const inv = x.invoice;
  const cur = inv.currency.value;
  const fields = [["Supplier", "supplier_name"], ["Invoice number", "invoice_number"], ["Invoice date", "invoice_date"],
    ["PO reference", "po_reference"], ["Currency", "currency"], ["Net / subtotal", "net"], ["Tax", "tax"], ["Total", "gross"]];
  const val = (k, f) => {
    if (f.value === null || f.value === undefined) return `<span class="muted">${esc(f.note || "—")}</span>`;
    return ["net", "tax", "gross"].includes(k) ? money(f.value, cur) : esc(f.value);
  };
  const pages = x.pages.map((p) => {
    const conf = p.mean_conf !== null ? ` (mean conf ${p.mean_conf}, skew ${p.skew_slope})` : "";
    return `page ${p.page}: ${p.method.toUpperCase()}${conf}, ${p.duration_ms} ms`;
  }).join("; ");
  const frows = fields.map(([label, k]) => `<tr><td>${label}</td><td>${val(k, inv[k])}</td><td>${evidenceCell(inv[k])}</td></tr>`).join("");
  const lrows = inv.lines.map((l) => `<tr><td>${esc(l.sku.value)}</td><td>${esc(l.description.value)}</td>
    <td class="num">${esc(l.quantity.value)}</td><td class="num">${esc(l.unit_price.value)}</td><td class="num">${money(l.amount.value, cur)}</td></tr>
    <tr><td colspan="5">qty ${evidenceCell(l.quantity)}<br>amount ${evidenceCell(l.amount)}</td></tr>`).join("");
  document.getElementById("evidence").innerHTML = `
    <p class="muted">${esc(pages)} · extraction ${x.duration_ms} ms · model calls ${x.model_calls}</p>
    <table><thead><tr><th>Field</th><th>Value</th><th>Source</th></tr></thead><tbody>${frows}</tbody></table>
    <h3 style="font-size:13px;margin:12px 0 4px">Line items</h3>
    <table><thead><tr><th>SKU</th><th>Description</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead>
    <tbody>${lrows}</tbody></table>
    ${inv.issues.length ? `<p class="warn">Issues: ${esc(inv.issues.join("; "))}</p>` : ""}`;
}

function renderRun(run) {
  const st = document.getElementById("status");
  st.textContent = run.decision ? `${run.status} · ${run.decision}` : run.status;
  st.className = "pill " + (run.decision || run.status).toLowerCase();
  document.getElementById("timing").textContent =
    run.processing_ms !== null ? `processed in ${(run.processing_ms / 1000).toFixed(2)} s` : "";
  document.getElementById("stages").innerHTML = run.events.map((e) =>
    `<li class="${esc(e.status)}"><span class="st">${esc(e.stage)}</span> ${esc(e.message)} <span class="ts">${fmtTs(e.ts)}</span></li>`).join("");
  const d = run.decision_detail;
  if (d) renderDecision(run, d);
  if (run.po_balance_now) renderBalance(run.po_balance_now, d);
  if (run.extraction) renderEvidence(run.extraction);
  if (run.status === "FAILED") {
    document.getElementById("decision-card").hidden = false;
    document.getElementById("decision").innerHTML = `<div class="decision-big blocked">FAILED</div><p>${esc(run.error)}</p>
      <p>No allocation was recorded.</p><button id="retry-btn">Retry processing</button>`;
    document.getElementById("retry-btn").onclick = () =>
      fetch(`/runs/${encodeURIComponent(run.run_id)}/retry`, { method: "POST" }).then(() => location.reload());
  }
}

function initRun(runId) {
  let timer = null;
  const tick = async () => {
    try {
      const run = await (await fetch(`/runs/${encodeURIComponent(runId)}`)).json();
      renderRun(run);
      if (run.status === "COMPLETED" || run.status === "FAILED") clearInterval(timer);
    } catch (err) {
      console.error(err);
    }
  };
  tick();
  timer = setInterval(tick, 700);
}
