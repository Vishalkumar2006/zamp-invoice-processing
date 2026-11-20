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
      out.textContent = `Accepted in ${Math.round(performance.now() - t0)} ms → ${body.run_id} (workspace ${body.workspace_id})`;
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
  // Same workspace the upload form is bound to (none before the first import).
  const wsField = document.querySelector("#upload-form input[name=workspace_id]");
  const scope = wsField ? `&workspace_id=${encodeURIComponent(wsField.value)}` : "";
  const data = await (await fetch(`/runs?limit=10${scope}`)).json();
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
  const candIds = d.candidate_po_ids || [];
  const cands = candIds.length ? ` (candidates: ${esc(candIds.join(", "))})` : "";
  const alloc = d.allocation
    ? `#${esc(run.allocation_id)} · ${money(d.allocation.net_cents, d.allocation.currency)} net on ${esc(d.allocation.po_id)}`
    : "none";
  const extraCodes = (d.reason_codes || []).filter((c) => c !== d.reason_code);
  document.getElementById("decision").innerHTML = `
    <div class="decision-big ${cls}">${esc(d.decision)} <span class="muted" style="font-size:13px">${esc(d.reason_code)}${
      extraCodes.length ? ` + ${esc(extraCodes.join(", "))}` : ""}</span></div>
    <p>${esc(d.reason)}</p>
    ${renderDuplicate(d)}
    ${renderCandidates(d)}
    ${renderPrices(d)}
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
  const rows = (d.checks || []).map((c) => {
    const src = c.compared && c.compared.source ? ` <span class="muted">(${esc(c.compared.source)})</span>` : "";
    return `<tr class="${esc(c.status)}"><td>${esc(c.rule)}</td><td>${esc(c.status)}</td><td>${esc(c.detail)}${src}</td></tr>`;
  }).join("");
  document.getElementById("checks").innerHTML =
    `<table><thead><tr><th>Rule</th><th>Result</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table>`;
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
  const rows = dup.comparison.map((r) => `<tr class="${r.status === "conflict" ? "fail" : ""}"><td>${esc(r.field)}</td>
    <td>${val(r, r.incoming)}</td><td>${val(r, r.accepted)}</td><td>${esc(label[r.status] || r.status)}</td></tr>`).join("");
  const title = { corroborated: "Duplicate of an accepted invoice", conflict: "Same invoice number, different content",
    identity_uncertain: "Possible duplicate (uncertain OCR identity)" }[dup.outcome] || "Duplicate check";
  return `<h3 class="sub">${esc(title)}</h3>
    <dl class="kv">
      <dt>Business key</dt><dd>vendor <strong>${esc(k.vendor_id)}</strong> ${esc(k.vendor_name || "")} + invoice number
        <strong>${esc(k.invoice_number)}</strong>${k.invoice_number_raw && k.invoice_number_raw !== k.invoice_number
          ? ` (printed “${esc(k.invoice_number_raw)}”)` : ""}</dd>
      <dt>Accepted invoice</dt><dd><a href="/ui/runs/${encodeURIComponent(a.run_id)}">${esc(a.invoice_number)}</a>
        ${a.invoice_date ? `dated ${esc(a.invoice_date)}` : ""} · ${money(a.gross_cents, cur)} on ${esc(a.po_id)}
        · accepted ${esc((a.accepted_at || "").replace("T", " ").slice(0, 16))} · allocation #${esc(a.allocation_id)}</dd>
    </dl>
    <div class="tablewrap"><table><thead><tr><th>Field</th><th>This document</th><th>Accepted invoice</th><th>Result</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

// Unit price vs PO price on every line, as recorded with the decision. Shown in full on a price hold.
function renderPrices(d) {
  const pc = d.price_check;
  if (!pc || !pc.lines || !pc.lines.length || d.reason_code !== "PRICE_VARIANCE") return "";
  const cur = pc.currency;
  const signed = (c) => (c > 0 ? "+" : c < 0 ? "-" : "") + money(Math.abs(c), cur);
  const rate = (r) => `${esc(cur)} ${esc(Number(r).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 }))}`;
  const src = (ev) => (ev ? `p${ev.page} · ${esc(ev.method.toUpperCase())} · conf ${esc(ev.conf)} · “${esc(ev.text)}”` : "—");
  const rows = pc.lines.map((l) => `<tr class="${l.within_tolerance ? "" : "fail"}">
    <td>${esc(l.line)}</td><td>${esc(l.sku)}</td><td class="num">${esc(l.quantity)}</td>
    <td class="num">${rate(l.invoice_unit_price)}</td><td class="num">${rate(l.po_unit_price)}</td>
    <td class="num"><strong>${esc(l.variance_display)}</strong></td>
    <td class="num"><strong>${signed(l.amount_difference_cents)}</strong></td>
    <td>${l.within_tolerance ? "within 1%" : "OUTSIDE 1%"}</td>
    <td><span class="src">invoice ${src(l.invoice_evidence)}<br>PO ${esc(l.po_id)} line ${esc(l.po_line_id)} · ${esc(l.po_source)}</span></td></tr>`).join("");
  const offset = pc.offsetting
    ? `<p><strong>The differences offset.</strong> Subtotal ${money(pc.subtotal_cents, cur)} at invoice prices vs
       ${money(pc.subtotal_at_po_prices_cents, cur)} at PO prices (difference ${signed(pc.subtotal_difference_cents)}),
       so the total alone does not reveal the price changes. Each line is checked against its own PO price.</p>`
    : `<p>Subtotal ${money(pc.subtotal_cents, cur)} at invoice prices vs ${money(pc.subtotal_at_po_prices_cents, cur)}
       at PO prices (difference ${signed(pc.subtotal_difference_cents)}).</p>`;
  return `<h3 class="sub">Unit prices vs ${esc(pc.po_id)} (${esc(pc.failed_lines.length)} of ${esc(pc.lines.length)} lines outside the
      ${esc(pc.tolerance_pct)}% per-line tolerance, inclusive)</h3>
    <div class="tablewrap"><table><thead><tr><th>Line</th><th>SKU</th><th class="num">Qty</th><th class="num">Invoice price</th>
      <th class="num">PO price</th><th class="num">Variance</th><th class="num">Difference at invoiced qty (pre-tax)</th>
      <th>Result</th><th>Source</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${offset}
    <p class="muted">Tax ${money(pc.tax_cents, cur)} is shown separately (total ${money(pc.gross_cents, cur)});
      ${esc(pc.note)}</p>`;
}

// Missing printed PO: every vendor PO compared with the invoice lines, side by side, as recorded.
function renderCandidates(d) {
  const pc = d.po_candidates;
  if (!pc || !pc.candidates || !pc.candidates.length) return "";
  const cur = pc.currency;
  const cs = pc.candidates;
  const ok = (v) => (v ? "✓" : "✗");
  const cell = (c, html, good) => `<td class="${good === false ? "bad" : ""}">${html}</td>`;
  const row = (label, fn) => `<tr><th>${label}</th>${cs.map(fn).join("")}</tr>`;
  const lineRows = pc.invoice_lines.map((il, idx) => {
    const L = (c) => c.lines[idx] || {};
    return [
      row(`Line ${il.line}: SKU ${esc(il.sku)}`, (c) => cell(c, L(c).sku_ok
        ? `${ok(true)} PO line ${esc(L(c).po_line_id)}<br><span class="muted">${esc(L(c).source)}</span>`
        : (L(c).repeated_mapping_of ? `${ok(false)} PO line ${esc(L(c).po_line_id)} already used by invoice line ${esc(L(c).repeated_mapping_of)}` : `${ok(false)} not on PO`), L(c).sku_ok)),
      row(`Unit price ${esc(il.unit_price)}`, (c) => cell(c, L(c).sku_ok
        ? `${ok(L(c).price_ok)} PO ${esc(L(c).po_unit_price)} (${esc(L(c).variance_pct)}%)` : "—", L(c).price_ok)),
      row(`Quantity ${esc(il.quantity)}`, (c) => cell(c, L(c).sku_ok
        ? `${ok(L(c).quantity_ok)} ${esc(L(c).remaining_quantity)} left of ${esc(L(c).authorized_quantity)}` : "—",
        L(c).quantity_ok)),
      row(`Amount ${money(il.amount_cents, cur)}`, (c) => cell(c, L(c).sku_ok
        ? `${ok(L(c).amount_ok)} ${money(L(c).remaining_net_cents, cur)} left on line` : "—", L(c).amount_ok)),
    ].join("");
  }).join("");
  const viable = new Set(pc.viable_po_ids);
  const head = cs.map((c) => `<th class="${viable.has(c.po_id) ? "fit" : ""}">${esc(c.po_id)}<br>
    <span class="muted">${viable.has(c.po_id) ? "fits" : "does not fit"}</span></th>`).join("");
  return `<h3 class="sub">PO candidates (no PO printed on the invoice)</h3>
    <p class="muted">Compared at decision time on: ${esc(pc.criteria)}.
      ${pc.delivery_reference ? `Delivery reference on the invoice: <strong>${esc(pc.delivery_reference)}</strong>.` : "No delivery reference could be read reliably."}
      None of these POs is selected.</p>
    <div class="tablewrap"><table class="cands"><thead><tr><th></th>${head}</tr></thead><tbody>
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
    ["PO reference", "po_reference"], ["Delivery reference", "delivery_reference"], ["Currency", "currency"], ["Net / subtotal", "net"], ["Tax", "tax"], ["Total", "gross"]];
  const val = (k, f) => {
    if (f.value === null || f.value === undefined) return `<span class="muted">${esc(f.note || "—")}</span>`;
    return ["net", "tax", "gross"].includes(k) ? money(f.value, cur) : esc(f.value);
  };
  const pages = x.pages.map((p) => {
    const conf = p.mean_conf !== null ? ` (mean conf ${p.mean_conf}, skew ${p.skew_slope})` : "";
    return `page ${p.page}: ${p.method.toUpperCase()}${conf}, ${p.duration_ms} ms`;
  }).join("; ");
  const frows = fields.map(([label, k]) => `<tr><td>${label}</td><td>${val(k, inv[k] || {})}</td><td>${evidenceCell(inv[k])}</td></tr>`).join("");
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
