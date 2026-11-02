// Tally workspace: one page with hash routes (#/overview, #/inbox, #/review, #/doc/<id>,
// #/lines, #/vendors, #/bench, #/settings). Every number comes from server.py. Money arrives as
// strings and is parsed to a number only to format it or size a bar; nothing is summed here.
// Features the architecture can't support yet are shown greyed out with "Coming soon".

const state = {
  meta: null, docs: [], currency: null, month: null, months: [], currencies: [],
  search: '', inboxTab: 'all', linesCategory: 'all', showReviewed: false, limit: 100, view: null,
};
const PAGE = 100;
const TOP_VENDORS = 12;
const NEUTRAL = '#a3a29c';
const BUCKET_LABELS = {tax: 'Tax', adjustments: 'Tips, fees & discounts', rounding: 'Rounding',
                       uncategorized: 'Uncategorized'};

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const soon = '<span class="soon">Coming soon</span>';

// --- API ---------------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch('/api' + path, opts);
  const body = (res.headers.get('content-type') || '').includes('json') ? await res.json() : null;
  if (!res.ok) throw new Error(errorText(body) || res.statusText);
  return body;
}
function errorText(body) {
  if (!body) return '';
  if (typeof body.detail === 'string') return body.detail;
  if (Array.isArray(body.detail)) return body.detail.map(d => `${d.loc.slice(2).join('.') || d.loc.join('.')}: ${d.msg}`).join('; ');
  return '';
}
const send = (method, path, body) => api(path, {method, headers: {'Content-Type': 'application/json'},
                                                body: body === undefined ? undefined : JSON.stringify(body)});

// --- formatting --------------------------------------------------------------------------

function money(value, currency = state.currency) {
  if (value == null || value === '') return '—';
  const n = Number(value);
  try {
    return n.toLocaleString('en-US', {style: 'currency', currency: currency || 'USD'});
  } catch {  // not an ISO code the browser knows
    return `${currency ?? ''} ${n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  }
}
const monthName = m => m === 'all' ? 'All time' : new Date(m + '-01T00:00').toLocaleDateString('en-US', {month: 'long', year: 'numeric'});
const shortMonth = m => new Date(m + '-01T00:00').toLocaleDateString('en-US', {month: 'short', year: '2-digit'});
const catLabel = name => BUCKET_LABELS[name] || (name ? name[0].toUpperCase() + name.slice(1) : '—');
const catColor = name => state.meta?.categories.find(c => c.name === name)?.color || NEUTRAL;
const initials = name => (name || '?').replace(/[^\p{L}\p{N} ]/gu, '').split(/\s+/).filter(Boolean)
  .slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?';
const avatar = (name, color) => `<span class="avatar" style="background:${color || NEUTRAL}">${esc(initials(name))}</span>`;
const matches = (...fields) => !state.search || fields.some(f => String(f ?? '').toLowerCase().includes(state.search));
const needsReview = d => !d.review && (d.status === 'flagged' || d.status === 'duplicate');

function badge(d) {
  if (d.review === 'approve') return '<span class="badge verified">Approved</span>';
  if (d.review === 'exclude') return '<span class="badge reviewed">Excluded</span>';
  if (d.review === 'not_duplicate') return '<span class="badge verified">Not a duplicate</span>';
  if (d.status === 'verified') return d.escalated ? '<span class="badge escalated">Verified · large model</span>'
                                                  : '<span class="badge verified">Verified</span>';
  return d.status === 'duplicate' ? '<span class="badge duplicate">Duplicate</span>'
                                  : '<span class="badge flagged">Needs review</span>';
}

function toast(message, error = false) {
  const t = $('#toast');
  t.textContent = message;
  t.className = 'toast' + (error ? ' err' : '');
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 4000);
}

// --- shared state ------------------------------------------------------------------------

async function refresh() {
  [state.meta, state.docs] = await Promise.all([api('/meta'), api('/docs')]);
  $('#n-inbox').textContent = state.docs.length || '';
  $('#n-review').textContent = state.docs.filter(needsReview).length || '';
  showJob(state.meta.job);
}

const period = () => new URLSearchParams(Object.entries({currency: state.currency, month: state.month})
  .filter(([, v]) => v)).toString();

function syncPeriod(s) {
  Object.assign(state, {currency: s.currency, month: s.month, months: s.months, currencies: s.currencies});
  $('#month').innerHTML = `<option value="all">All time</option>` +
    [...s.months].reverse().map(m => `<option value="${m}">${monthName(m)}</option>`).join('');
  $('#month').value = s.month;
  $('#currency').innerHTML = s.currencies.map(c => `<option>${esc(c)}</option>`).join('');
  $('#currency').value = s.currency || '';
  $('#currency').hidden = s.currencies.length < 2;
}

function stepMonth(step) {
  const months = state.months;
  if (!months.length) return;
  let i = state.month === 'all' ? months.length : months.indexOf(state.month);
  i = Math.min(Math.max(i + step, 0), months.length - 1);
  state.month = months[i];
  render();
}

// --- processing and upload ---------------------------------------------------------------

let polling = null;
let dismissed = null;

function showJob(job) {
  const bar = $('#job-bar');
  if (job.running) {
    bar.className = 'job-bar';
    bar.innerHTML = `<b>Processing ${job.done} of ${job.total}</b>
      <div class="track"><div class="fill" style="width:${job.total ? job.done / job.total * 100 : 0}%"></div></div>
      <span class="muted">Each document is read, its math checked, then added to your books.</span>`;
    bar.hidden = false;
    polling = polling || setInterval(pollJob, 1500);
  } else if (job.error && job.finished !== dismissed) {
    bar.className = 'job-bar err';
    bar.innerHTML = `Processing stopped: ${esc(job.error)} <button class="btn btn-sm" id="dismiss-job">Dismiss</button>`;
    bar.hidden = false;
    $('#dismiss-job').onclick = () => { dismissed = job.finished; bar.hidden = true; };
  } else {
    bar.hidden = true;
  }
}

async function pollJob() {
  const job = await api('/job').catch(() => null);
  if (!job) return;
  if (job.running) {
    showJob(job);
    if (state.view === 'inbox' && job.done !== state.jobDone) {  // documents are saved as they finish
      state.jobDone = job.done;
      await refresh();
      render();
    }
    return;
  }
  clearInterval(polling);
  polling = null;
  await refresh();
  toast(job.error ? 'Processing failed' : `Processed ${job.total} document${job.total === 1 ? '' : 's'}`, !!job.error);
  render();
}

async function processFolder(limit) {
  const all = limit === 'all';
  if (all && !confirm(`Process all ${state.meta.unprocessed} documents? Every document costs model calls.`)) return;
  try {
    showJob(await send('POST', '/process', {limit: all ? null : Number(limit)}));
  } catch (e) { toast(e.message, true); }
}

async function uploadFiles(files) {
  if (!files.length) return;
  const form = new FormData();
  [...files].forEach(f => form.append('files', f));
  try {
    showJob(await api('/upload', {method: 'POST', body: form}));
    location.hash = '#/inbox';
  } catch (e) { toast(e.message, true); }
}

function keyNote() {
  return state.meta.has_api_key ? '' :
    `<div class="note">FIREWORKS_API_KEY isn't set in <code>.env</code>, so new documents can't be processed. Everything already processed still works.</div>`;
}

function processButtons() {
  const n = state.meta.unprocessed, off = !n || !state.meta.has_api_key || state.meta.job.running ? 'disabled' : '';
  return `<button class="btn btn-sm btn-primary" data-process="25" ${off}>Process ${Math.min(25, n) || 25}</button>
          <button class="btn btn-sm" data-process="all" ${off}>Process all ${n || ''}</button>`;
}

// --- overview ----------------------------------------------------------------------------

async function overview(el) {
  if (!state.docs.length) {
    el.innerHTML = `<h1>Welcome to Tally</h1>
      <p class="lede">Add invoices and receipts. Each one is read by a small model, its math is checked in code, and it's added to your spend.</p>
      ${keyNote()}
      <div class="card empty"><b>No documents yet</b>
        ${state.meta.unprocessed} files are waiting in <code>data/docs</code>. You can also upload your own.
        <div class="actions">${processButtons()}<button class="btn btn-sm" data-upload>Upload files</button></div>
      </div>`;
    return;
  }
  const s = await api(`/spend?${period()}`);
  syncPeriod(s);
  if (!s.currency) {
    el.innerHTML = `<h1>Overview</h1><p class="lede">No verified invoices yet.</p><div class="hero">${attentionCard()}</div>`;
    return;
  }
  const vendorCategory = mainCategories(s.flows);
  el.innerHTML = `
    <h1>${s.month === 'all' ? 'All-time' : monthName(s.month)} spend</h1>
    <p class="lede">${s.summary.invoices} verified invoice${s.summary.invoices === 1 ? '' : 's'} in ${esc(s.currency)}${s.currencies.length > 1 ? ' · other currencies are kept separate, never converted' : ''}</p>
    <div class="hero">
      <div class="card card-body">
        <div class="card-note">Verified spend · ${esc(s.currency)}</div>
        <div class="hero-num num">${money(s.summary.amount)}</div>
        ${deltaLine(s)}
        <div class="row">
          <div><b class="num">${s.summary.invoices}</b>invoices</div>
          <div><b class="num">${s.summary.vendors}</b>vendors</div>
          <div><b class="num">${money(s.summary.tax)}</b>tax</div>
        </div>
      </div>
      ${attentionCard()}
    </div>
    <div class="card">
      <div class="card-head"><div class="card-title">Where the money went</div>
        <div class="card-note">Category → vendor · top ${TOP_VENDORS} vendors · hover for amounts</div></div>
      <div class="card-body sankey-wrap" id="sankey"></div>
    </div>
    <div class="grid3">
      <div class="card">
        <div class="card-head"><div class="card-title">Monthly spend</div><div class="card-note">${esc(s.currency)} · verified only · click a month</div></div>
        <div class="card-body">${monthBars(s)}</div>
      </div>
      <div class="card">
        <div class="card-head"><div class="card-title">By category</div></div>
        <div class="card-body">${categoryList(s)}</div>
      </div>
    </div>
    <div class="grid2">
      <div class="card">
        <div class="card-head"><div class="card-title">Top vendors</div><a href="#/vendors" class="card-note">All vendors →</a></div>
        <table><tbody>${s.by_vendor.slice(0, 6).map(v => `<tr>
          <td><div class="vendor-cell">${avatar(v.vendor, catColor(vendorCategory[v.vendor]))}<div>${esc(v.vendor)}<small>${v.invoices} invoice${v.invoices === 1 ? '' : 's'}</small></div></div></td>
          <td class="r num">${money(v.amount)}</td></tr>`).join('')}</tbody></table>
      </div>
      <div class="card ghost" title="Not available yet: needs a scheduler and email sending">
        <div class="card-head"><div class="card-title">Weekly digest</div>${soon}</div>
        <div class="card-body">
          <p style="margin:0 0 10px;color:var(--ink-2)">A Monday email to the team with:</p>
          <div class="check"><span>·</span>New spend since last week, by category</div>
          <div class="check"><span>·</span>Items that need review, with links</div>
          <div class="check"><span>·</span>Vendors seen for the first time</div>
          <div class="check"><span>·</span>Categories over budget</div>
          <div class="actions"><button class="btn btn-sm" disabled>Preview email</button><button class="btn btn-sm" disabled>Edit recipients</button></div>
        </div>
      </div>
    </div>`;
  drawSankey($('#sankey'), s, vendorCategory);
  $$('.bars .b[data-month]', el).forEach(b => { b.onclick = () => { state.month = b.dataset.month; render(); }; });
}

function deltaLine(s) {
  if (!s.previous) return `<div class="delta">${s.months.length} month${s.months.length === 1 ? '' : 's'} with invoices · ${monthName(s.months[0])} to ${monthName(s.months[s.months.length - 1])}</div>`;
  const now = Number(s.summary.amount), before = Number(s.previous.amount);
  if (!before) return `<div class="delta">No verified spend in ${monthName(s.previous.month)}</div>`;
  const diff = now - before, pct = Math.abs(diff / before * 100).toFixed(1);
  return `<div class="delta num">${diff >= 0 ? '▲' : '▼'} ${money(Math.abs(diff))} (${pct}%) vs ${monthName(s.previous.month)}</div>`;
}

function attentionCard() {
  const todo = state.docs.filter(needsReview);
  const items = todo.slice(0, 3).map(d => `<div class="attn-item">
      <div class="attn-ic ${d.status === 'duplicate' ? 'dup">⧉' : 'crit">!'}</div>
      <div class="t">${esc(d.vendor || d.file)}<small>${esc(d.reasons[0] || '')}</small></div>
      <a class="btn btn-sm" href="#/doc/${encodeURIComponent(d.doc_id)}">Review</a></div>`).join('');
  return `<div class="card card-body">
    <div class="card-title" style="margin-bottom:6px">Needs attention${todo.length > 3 ? ` <span class="card-note">· ${todo.length} items</span>` : ''}</div>
    ${items || `<div class="attn-item"><div class="attn-ic ok">✓</div><div class="t">Nothing to review<small>Every document passed its checks or has been reviewed</small></div></div>`}
    ${todo.length > 3 ? `<a href="#/review" class="card-note">See all ${todo.length} →</a>` : ''}
  </div>`;
}

function mainCategories(flows) {
  const best = {};
  for (const f of flows) {
    if (!best[f.vendor] || Number(f.amount) > Number(best[f.vendor].amount)) best[f.vendor] = f;
  }
  return Object.fromEntries(Object.entries(best).map(([v, f]) => [v, f.category]));
}

function monthBars(s) {
  const amounts = Object.fromEntries(s.by_month.map(r => [r.month, Number(r.amount)]));
  const end = s.month === 'all' ? s.months[s.months.length - 1] : s.month;
  const months = [];
  let [y, m] = end.split('-').map(Number);
  for (let i = 0; i < 12; i++) {
    months.unshift(`${y}-${String(m).padStart(2, '0')}`);
    if (--m === 0) { m = 12; y--; }
  }
  const max = Math.max(...months.map(k => amounts[k] || 0), 1);
  return `<div class="bars">${months.map(k => {
      const v = amounts[k] || 0;
      return `<button class="b ${k === s.month ? 'cur' : ''}" ${v ? `data-month="${k}"` : 'disabled'} title="${monthName(k)}: ${money(v)}">
        <span class="v num">${k === s.month ? money(v) : ''}</span><div class="fill" style="height:${v / max * 100}%"></div></button>`;
    }).join('')}</div>
    <div class="bar-labels">${months.map(k => `<span>${shortMonth(k)}</span>`).join('')}</div>`;
}

function categoryList(s) {
  const total = Number(s.summary.amount) || 1;
  const positive = s.by_category.filter(c => Number(c.amount) > 0);
  return `<div class="stack">${positive.map(c => `<div title="${esc(catLabel(c.category))}: ${money(c.amount)}" style="flex:${Number(c.amount)};background:${catColor(c.category)}"></div>`).join('')}</div>` +
    s.by_category.map(c => `<div class="cat-row"><span class="swatch" style="background:${catColor(c.category)}"></span>${esc(catLabel(c.category))}
      <span class="amt num">${money(c.amount)}</span><span class="pct num">${(Number(c.amount) / total * 100).toFixed(1)}%</span></div>`).join('');
}

// Total -> category -> vendor. Tax, adjustments and rounding end at the category column.
function drawSankey(el, s, vendorCategory) {
  const cats = s.by_category.filter(c => Number(c.amount) > 0);
  const catIndex = Object.fromEntries(cats.map((c, i) => [c.category, i]));
  const vendorTotals = {};
  for (const f of s.flows) vendorTotals[f.vendor] = (vendorTotals[f.vendor] || 0) + Number(f.amount);
  const vendors = Object.keys(vendorTotals).sort((a, b) =>
    (catIndex[vendorCategory[a]] ?? 99) - (catIndex[vendorCategory[b]] ?? 99) || vendorTotals[b] - vendorTotals[a]);
  const nodes = [{id: 't', label: 'Verified spend', col: 0, color: '#1f4d3a'},
    ...cats.map(c => ({id: 'c:' + c.category, label: catLabel(c.category), col: 1, color: catColor(c.category)})),
    ...vendors.map(v => ({id: 'v:' + v, label: v, col: 2, color: v === 'Other vendors' ? NEUTRAL : catColor(vendorCategory[v])}))];
  const links = [...cats.map(c => ({s: 't', t: 'c:' + c.category, v: Number(c.amount)})),
    ...s.flows.map(f => ({s: 'c:' + f.category, t: 'v:' + f.vendor, v: Number(f.amount)}))];
  if (!links.length) { el.innerHTML = '<div class="empty">Nothing to draw yet.</div>'; return; }

  const width = 900, nodeW = 10, pad = 12, labelW = 240;
  const height = Math.max(320, Math.max(cats.length, vendors.length) * 26);
  const byId = Object.fromEntries(nodes.map(n => [n.id, {...n, in: 0, out: 0}]));
  links.forEach(l => { byId[l.s].out += l.v; byId[l.t].in += l.v; });
  const all = Object.values(byId);
  all.forEach(n => { n.v = Math.max(n.in, n.out); });
  const cols = [0, 1, 2].map(c => all.filter(n => n.col === c));
  const k = Math.min(...cols.filter(ns => ns.length).map(ns =>
    (height - pad * (ns.length - 1)) / ns.reduce((a, n) => a + n.v, 0)));
  const span = width - labelW - nodeW;
  cols.forEach((ns, i) => {
    let y = 0;
    ns.forEach(n => { n.x = i * span / 2; n.y = y; n.h = Math.max(n.v * k, 2); n.sy = y; n.ty = y; y += n.h + pad; });
  });
  const fullH = Math.max(...all.map(n => n.y + n.h)) + 8;
  let svg = `<svg viewBox="0 0 ${width} ${fullH}" role="img" aria-label="Spend by category and vendor">`;
  links.forEach(l => {
    const a = byId[l.s], b = byId[l.t], h = Math.max(l.v * k, 1);
    const x0 = a.x + nodeW, x1 = b.x, xm = (x0 + x1) / 2, y0 = a.sy, y1 = b.ty;
    a.sy += h; b.ty += h;
    svg += `<path d="M${x0},${y0}C${xm},${y0} ${xm},${y1} ${x1},${y1}L${x1},${y1 + h}C${xm},${y1 + h} ${xm},${y0 + h} ${x0},${y0 + h}Z"
      style="fill:${a.col === 0 ? b.color : a.color};fill-opacity:.3"><title>${esc(a.label)} → ${esc(b.label)}: ${money(l.v)}</title></path>`;
  });
  all.forEach(n => {
    svg += `<rect x="${n.x}" y="${n.y}" width="${nodeW}" height="${n.h}" rx="2" style="fill:${n.color}"><title>${esc(n.label)}: ${money(n.v)}</title></rect>
      <text x="${n.x + nodeW + 6}" y="${n.y + n.h / 2}" dominant-baseline="middle">${esc(n.label.length > 30 ? n.label.slice(0, 29) + '…' : n.label)} <tspan class="amt">${money(n.v)}</tspan></text>`;
  });
  el.innerHTML = svg + '</svg>';
}

// --- inbox -------------------------------------------------------------------------------

const INBOX_TABS = {
  all: ['All', () => true],
  verified: ['Verified', d => d.status === 'verified'],
  review: ['Needs review', needsReview],
  duplicate: ['Duplicates', d => d.pipeline_status === 'duplicate'],
  reviewed: ['Reviewed', d => !!d.review],
};

async function inbox(el) {
  const m = state.meta;
  const rows = state.docs.filter(INBOX_TABS[state.inboxTab][1])
    .filter(d => matches(d.vendor, d.file, d.doc_id, d.total, d.reasons.join(' ')));
  el.innerHTML = `
    <h1>Inbox</h1>
    <p class="lede">Each document is read, its math checked, then it's added to your books. Already-processed files are never read twice.</p>
    ${keyNote()}
    <div class="ingest">
      <div class="card"><div class="drop" id="drop"><b>Drop files here</b><br><span class="muted">PDF, JPG, PNG · or click to browse</span></div></div>
      <div class="card"><div class="card-title">data/docs folder</div>
        <div class="card-note">${m.unprocessed ? `${m.unprocessed} file${m.unprocessed === 1 ? '' : 's'} not processed yet` : 'Everything here is processed'}</div>
        <div class="conn">${processButtons()}</div></div>
      <div class="card ghost" title="Not available yet: needs an inbound mail server"><div class="card-title">Email forwarding</div>
        <div class="card-note">Forward invoices to a Tally address</div><div class="conn">${soon}</div></div>
      <div class="card ghost" title="Not available yet: needs OAuth and a background sync"><div class="card-title">Gmail & Google Drive</div>
        <div class="card-note">Pull invoices in automatically</div><div class="conn">${soon}</div></div>
    </div>
    <div class="card">
      <div class="tabs">${Object.entries(INBOX_TABS).map(([key, [label, test]]) =>
        `<button data-tab="${key}" class="${state.inboxTab === key ? 'on' : ''}">${label} ${state.docs.filter(test).length}</button>`).join('')}</div>
      ${rows.length ? `<div class="scroll-x"><table>
        <thead><tr><th>Vendor</th><th>Date</th><th>Status</th><th>Why</th><th class="r">Model cost</th><th class="r">Amount</th></tr></thead>
        <tbody>${rows.slice(0, state.limit).map(d => `<tr class="link" data-doc="${esc(d.doc_id)}">
          <td><div class="vendor-cell">${avatar(d.vendor || d.file)}<div>${esc(d.vendor || 'Unknown vendor')}<small>${esc(d.file)}</small></div></div></td>
          <td class="num">${esc(d.date || '—')}</td>
          <td>${badge(d)}</td>
          <td class="muted" style="max-width:280px;font-size:12.5px">${esc(d.reasons[0] || '')}</td>
          <td class="r num muted">${d.cost_usd == null ? '—' : '$' + Number(d.cost_usd).toFixed(4)}</td>
          <td class="r num">${money(d.total, d.currency)}</td></tr>`).join('')}</tbody></table></div>
        ${rows.length > state.limit ? `<div class="more"><button class="btn btn-sm" id="more">Show more (${rows.length - state.limit} left)</button></div>` : ''}`
      : `<div class="empty">${state.docs.length ? 'No documents match.' : 'No documents processed yet.'}</div>`}
    </div>`;

  const drop = $('#drop');
  drop.onclick = () => $('#upload-input').click();
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); uploadFiles(e.dataTransfer.files); };
  $$('[data-tab]', el).forEach(b => { b.onclick = () => { state.inboxTab = b.dataset.tab; state.limit = PAGE; render(); }; });
  $$('[data-doc]', el).forEach(r => { r.onclick = () => { location.hash = '#/doc/' + encodeURIComponent(r.dataset.doc); }; });
  if ($('#more')) $('#more').onclick = () => { state.limit += PAGE; render(); };
}

// --- review ------------------------------------------------------------------------------

const FIELDS = [['vendor_name', 'Vendor'], ['invoice_number', 'Invoice #'], ['invoice_date', 'Date (YYYY-MM-DD)'],
                ['currency', 'Currency (ISO)'], ['subtotal', 'Subtotal'], ['tax', 'Tax'], ['total', 'Total']];

function queue() {
  return state.docs.filter(d => needsReview(d) || (state.showReviewed && d.review));
}

async function review(el) {
  const first = queue()[0];
  if (first) { location.replace('#/doc/' + encodeURIComponent(first.doc_id)); return; }
  el.innerHTML = `<h1>Review</h1><p class="lede">Tally won't guess. Flagged and duplicate documents stay out of your totals until someone decides.</p>
    <div class="card empty"><b>Nothing to review</b>Every document passed its checks or has been reviewed.
    <div class="actions"><button class="btn btn-sm" id="show-reviewed">Show reviewed documents</button></div></div>`;
  $('#show-reviewed').onclick = () => { state.showReviewed = true; render(); };
}

async function docView(el, id) {
  const d = await api('/docs/' + encodeURIComponent(id));
  const row = d.row, inv = d.doc.invoice || {line_items: [], adjustments: []};
  const items = queue();
  el.innerHTML = `
    <h1>Review</h1>
    <p class="lede">Tally won't guess. Flagged and duplicate documents stay out of your totals until someone decides.</p>
    <div class="review-layout">
      <div class="queue">
        ${items.map(q => `<a href="#/doc/${encodeURIComponent(q.doc_id)}" class="${q.doc_id === id ? 'on' : ''}">
          <b>${esc(q.vendor || q.file)}</b> ${q.review ? badge(q) : ''}<small>${esc(q.reasons[0] || q.file)}</small></a>`).join('')
          || '<div class="card-note">Nothing left to review.</div>'}
        <label class="card-note"><input type="checkbox" id="show-reviewed" ${state.showReviewed ? 'checked' : ''}> Show reviewed</label>
      </div>
      <div class="card doc-view">
        <div class="doc-preview">
          ${d.pages ? Array.from({length: d.pages}, (_, i) => `<img src="/api/docs/${encodeURIComponent(id)}/pages/${i + 1}" alt="Page ${i + 1}" loading="lazy">`).join('')
                    : '<div class="empty">This file can\'t be shown.</div>'}
          <a class="card-note" href="/api/docs/${encodeURIComponent(id)}/file" target="_blank">Open original · ${esc(row.file)}</a>
        </div>
        <div class="form" id="form">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap">
            <b style="font-size:16px">${esc(row.vendor || 'Unknown vendor')}</b>${badge(row)}
          </div>
          ${reasonBox(d)}
          ${FIELDS.map(([f, label]) => `<div class="field"><label for="f-${f}">${label}</label>
            <input type="text" id="f-${f}" data-field="${f}" value="${esc(inv[f] ?? '')}"></div>`).join('')}
          <div class="field"><label>Tax included</label><label class="card-note"><input type="checkbox" id="f-tax_inclusive" ${inv.tax_inclusive ? 'checked' : ''}> prices already include tax</label></div>
          <h3>Line items</h3>
          <div class="scroll-x"><table class="items"><thead><tr><th>Description</th><th>Qty</th><th>Unit</th><th>Amount</th><th>Category</th><th></th></tr></thead>
            <tbody id="lines">${inv.line_items.map(lineRow).join('')}</tbody></table></div>
          <button class="btn btn-sm" id="add-line">+ Line</button>
          <h3>Adjustments <span class="card-note">discounts (negative), shipping, tips</span></h3>
          <table class="items"><tbody id="adjustments">${inv.adjustments.map(adjustmentRow).join('')}</tbody></table>
          <button class="btn btn-sm" id="add-adjustment">+ Adjustment</button>
          <h3>Checks</h3>
          <div id="checks">${checksHtml(d.failures, [])}</div>
          <div class="meta-list">
            <span>Model</span><span>${esc(d.doc.model || '—')}${d.doc.escalated ? ' · escalated' : ''}</span>
            <span>Cost · time</span><span class="num">${d.doc.cost_usd == null ? '—' : '$' + Number(d.doc.cost_usd).toFixed(4)} · ${row.latency_s}s</span>
            ${d.doc.uncertain_fields.length ? `<span>Model unsure</span><span>${esc(d.doc.uncertain_fields.join(', '))}</span>` : ''}
            ${d.original.duplicate_of ? `<span>Duplicate of</span><span><a href="#/doc/${encodeURIComponent(d.original.duplicate_of)}">${esc(d.original.duplicate_of)}</a></span>` : ''}
          </div>
          ${d.review ? `<div class="callout info">${reviewText(d.review)}</div>` : ''}
          <div class="field" style="margin-top:12px"><label for="note">Note</label><input type="text" id="note" placeholder="Optional, saved with the decision"></div>
          <div class="actions">
            <button class="btn btn-primary" id="approve">${row.status === 'verified' && !d.review ? 'Save corrections' : 'Approve'}</button>
            ${row.pipeline_status === 'duplicate' && !d.review ? '<button class="btn" id="not-dup">Not a duplicate</button>' : ''}
            ${d.review?.action !== 'exclude' ? '<button class="btn btn-danger" id="exclude">Exclude</button>' : ''}
            ${d.review ? '<button class="btn" id="reopen">Undo decision</button>' : ''}
            <button class="btn" disabled title="Not available yet: needs email sending">Email vendor ${soon}</button>
          </div>
        </div>
      </div>
    </div>`;

  const form = $('#form');
  let timer;
  const recheck = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const r = await send('POST', '/check', {invoice: collect(inv)});
        $('#checks').innerHTML = checksHtml(r.failures, r.missing);
        FIELDS.forEach(([f]) => $(`#f-${f}`).classList.toggle('bad', r.missing.includes(f)));
      } catch (e) {
        $('#checks').innerHTML = `<div class="check"><span class="x">✕</span>${esc(e.message)}</div>`;
      }
    }, 300);
  };
  form.addEventListener('input', recheck);
  form.addEventListener('click', e => {
    if (e.target.matches('.items .x')) { e.target.closest('tr').remove(); recheck(); }
  });
  $('#add-line').onclick = () => { $('#lines').insertAdjacentHTML('beforeend', lineRow({})); };
  $('#add-adjustment').onclick = () => { $('#adjustments').insertAdjacentHTML('beforeend', adjustmentRow({})); };
  $('#show-reviewed').onchange = e => { state.showReviewed = e.target.checked; render(); };

  const decide = async (method, body, message) => {
    try {
      await send(method, `/docs/${encodeURIComponent(id)}/review`, body);
      await refresh();
      toast(message);
      const next = queue().find(q => q.doc_id !== id);
      if (next && method === 'POST') location.hash = '#/doc/' + encodeURIComponent(next.doc_id);
      else render();
    } catch (e) { toast(e.message, true); }
  };
  const note = () => $('#note').value.trim() || null;
  $('#approve').onclick = () => decide('POST', {action: 'approve', invoice: collect(inv), note: note()}, 'Approved: counted in your totals');
  if ($('#not-dup')) $('#not-dup').onclick = () => decide('POST', {action: 'not_duplicate', note: note()}, 'Marked as not a duplicate');
  if ($('#exclude')) $('#exclude').onclick = () => decide('POST', {action: 'exclude', note: note()}, 'Excluded from totals');
  if ($('#reopen')) $('#reopen').onclick = () => decide('DELETE', undefined, 'Decision undone');
}

function reasonBox(d) {
  const reasons = d.doc.reasons;
  if (!reasons.length) return '';
  const cls = d.row.status === 'verified' ? 'ok' : d.row.status === 'duplicate' ? 'info' : '';
  return `<div class="callout ${cls}">${reasons.map(esc).join('<br>')}</div>`;
}

function reviewText(r) {
  const what = {approve: 'Approved', exclude: 'Excluded', not_duplicate: 'Marked as not a duplicate'}[r.action];
  const when = new Date(r.at).toLocaleString('en-US', {dateStyle: 'medium', timeStyle: 'short'});
  return `<b>${what}</b> on ${esc(when)}${r.changed_fields.length ? ` · changed ${esc(r.changed_fields.join(', '))}` : ''}${r.note ? ` · “${esc(r.note)}”` : ''}`;
}

function checksHtml(failures, missing) {
  const rows = failures.map(f => `<div class="check"><span class="x">✕</span><span>${esc(f)}</span></div>`);
  if (missing.length) rows.push(`<div class="check"><span class="x">✕</span><span>Needed to count it: ${esc(missing.join(', '))}</span></div>`);
  return rows.join('') || '<div class="check"><span class="ok">✓</span>All checks pass</div>';
}

function categoryOptions(selected) {
  return `<option value="">—</option>` + state.meta.categories.map(c =>
    `<option value="${esc(c.name)}" ${c.name === selected ? 'selected' : ''}>${esc(catLabel(c.name))}</option>`).join('');
}

function lineRow(item) {
  const cell = (f, w) => `<td style="width:${w}"><input type="text" data-line="${f}" value="${esc(item[f] ?? '')}"></td>`;
  return `<tr data-page="${item.page ?? ''}">${cell('description', '32%')}${cell('quantity', '9%')}${cell('unit_price', '17%')}${cell('amount', '17%')}
    <td style="width:22%"><select data-line="category">${categoryOptions(item.category)}</select></td><td><button class="x" title="Remove">✕</button></td></tr>`;
}

function adjustmentRow(adj) {
  return `<tr><td><input type="text" data-adj="label" placeholder="e.g. Discount" value="${esc(adj.label ?? '')}"></td>
    <td style="width:30%"><input type="text" data-adj="amount" value="${esc(adj.amount ?? '')}"></td><td><button class="x" title="Remove">✕</button></td></tr>`;
}

// The edited invoice: blank inputs become null, so checks.py sees what's missing.
function collect(base) {
  const val = el => el.value.trim() === '' ? null : el.value.trim();
  const inv = structuredClone(base);
  FIELDS.forEach(([f]) => { inv[f] = val($(`#f-${f}`)); });
  inv.tax_inclusive = $('#f-tax_inclusive').checked;
  inv.line_items = $$('#lines tr').map(tr => {
    const item = {page: tr.dataset.page ? Number(tr.dataset.page) : null};
    $$('[data-line]', tr).forEach(el => { item[el.dataset.line] = val(el); });
    return item;
  });
  inv.adjustments = $$('#adjustments tr').map(tr => Object.fromEntries($$('[data-adj]', tr).map(el => [el.dataset.adj, val(el)])));
  return inv;
}

// --- line items --------------------------------------------------------------------------

async function lines(el) {
  const key = period();
  if (state.linesKey !== key) {
    const s = await api(`/spend?${key}`);
    syncPeriod(s);
    state.linesKey = period();
    state.lines = await api(`/lines?${state.linesKey}`);
  }
  const all = state.lines;
  const categories = [...new Set(all.map(r => r.category))];
  if (state.linesCategory !== 'all' && !categories.includes(state.linesCategory)) state.linesCategory = 'all';
  const rows = all.filter(r => state.linesCategory === 'all' || r.category === state.linesCategory)
    .filter(r => matches(r.description, r.vendor, r.doc_id, r.category, r.amount));
  el.innerHTML = `
    <h1>Line items</h1>
    <p class="lede">${monthName(state.month)} · ${esc(state.currency || '')} · every row links to its source document and page. Tax and adjustments are their own rows, so the column adds up to the invoices' printed totals.</p>
    <div class="card">
      <div class="tabs"><button data-cat="all" class="${state.linesCategory === 'all' ? 'on' : ''}">All ${all.length}</button>${categories.map(c =>
        `<button data-cat="${esc(c)}" class="${state.linesCategory === c ? 'on' : ''}">${esc(catLabel(c))} ${all.filter(r => r.category === c).length}</button>`).join('')}</div>
      ${rows.length ? `<div class="scroll-x"><table>
        <thead><tr><th>Description</th><th>Vendor</th><th>Category</th><th class="r">Qty</th><th class="r">Unit price</th><th class="r">Amount</th><th>Source</th></tr></thead>
        <tbody>${rows.slice(0, state.limit).map(r => `<tr>
          <td>${esc(r.description || '—')}</td><td>${esc(r.vendor)}</td>
          <td><span class="swatch" style="background:${catColor(r.category)};margin-right:6px"></span>${esc(catLabel(r.category))}</td>
          <td class="r num">${r.quantity == null ? '' : Number(r.quantity).toLocaleString('en-US')}</td>
          <td class="r num">${r.unit_price == null ? '' : money(r.unit_price)}</td>
          <td class="r num"><b>${money(r.amount)}</b></td>
          <td><a href="#/doc/${encodeURIComponent(r.doc_id)}">${esc(r.doc_id)}${r.page ? ` · p${r.page}` : ''}</a></td></tr>`).join('')}</tbody></table></div>
        ${rows.length > state.limit ? `<div class="more"><button class="btn btn-sm" id="more">Show more (${rows.length - state.limit} left)</button></div>` : ''}`
      : '<div class="empty">No line items match.</div>'}
    </div>`;
  $$('[data-cat]', el).forEach(b => { b.onclick = () => { state.linesCategory = b.dataset.cat; state.limit = PAGE; render(); }; });
  if ($('#more')) $('#more').onclick = () => { state.limit += PAGE; render(); };
}

// --- vendors -----------------------------------------------------------------------------

async function vendors(el) {
  const list = await api(`/vendors?${new URLSearchParams(state.currency ? {currency: state.currency} : {})}`);
  const rows = list.filter(v => matches(v.vendor, v.aliases.join(' '), v.category));
  el.innerHTML = `
    <h1>Vendors</h1>
    <p class="lede">All time · ${esc(state.currency || '')}. Different spellings of the same vendor are merged automatically; unclear matches are decided by the model and remembered.</p>
    <div class="card">${rows.length ? `<div class="scroll-x"><table>
      <thead><tr><th>Vendor</th><th>Also seen as</th><th>Usual category</th><th class="r">Invoices</th><th class="r">Documents</th><th class="r">Spend</th></tr></thead>
      <tbody>${rows.slice(0, state.limit).map(v => `<tr>
        <td><div class="vendor-cell">${avatar(v.vendor, catColor(v.category))}${esc(v.vendor)}</div></td>
        <td class="muted">${esc(v.aliases.join(', ') || '—')}</td>
        <td>${v.category ? `<span class="swatch" style="background:${catColor(v.category)};margin-right:6px"></span>${esc(catLabel(v.category))}` : '—'}</td>
        <td class="r num">${v.invoices}</td><td class="r num">${v.documents}</td><td class="r num">${money(v.amount)}</td></tr>`).join('')}</tbody></table></div>
      ${rows.length > state.limit ? `<div class="more"><button class="btn btn-sm" id="more">Show more (${rows.length - state.limit} left)</button></div>` : ''}`
    : '<div class="empty">No vendors yet.</div>'}</div>`;
  if ($('#more')) $('#more').onclick = () => { state.limit += PAGE; render(); };
}

// --- benchmark ---------------------------------------------------------------------------

async function bench(el) {
  const s = await api('/stats');
  const n = s.documents || 1, pct = x => `${(x / n * 100).toFixed(1)}%`;
  const kpi = (label, value, foot) => `<div class="card kpi"><div class="card-note">${label}</div><div class="v num">${value}</div><div class="card-note">${foot}</div></div>`;
  el.innerHTML = `
    <h1>Model benchmark</h1>
    <p class="lede">How the small model plus math checks is doing on your documents, and how it compares with a large model on labeled ones.</p>
    <div class="kpis">
      ${kpi('Documents processed', s.documents, `${s.reviewed} reviewed by a person`)}
      ${kpi('Sent to the large model', pct(s.escalated), `${s.escalated} escalated after failed checks`)}
      ${kpi('Flagged for a person', pct(s.flagged_by_pipeline), `${s.flagged_by_pipeline} documents`)}
      ${kpi('Model cost', '$' + Number(s.cost_usd).toFixed(2), `$${(Number(s.cost_usd) / n).toFixed(4)} per document${s.cost_unknown ? ` · ${s.cost_unknown} unpriced` : ''}`)}
    </div>
    <div class="grid2" style="margin-top:0">
      <div class="card card-body">
        <div class="card-title">Speed and tokens</div>
        <div class="meta-list">
          <span>Median time per document</span><span class="num">${s.latency_p50 ?? '—'}s</span>
          <span>95th percentile</span><span class="num">${s.latency_p95 ?? '—'}s</span>
          <span>Input tokens</span><span class="num">${s.input_tokens.toLocaleString('en-US')}</span>
          <span>Output tokens</span><span class="num">${s.output_tokens.toLocaleString('en-US')}</span>
        </div>
      </div>
      <div class="card card-body">
        <div class="card-title">Models whose extraction was used</div>
        <div class="meta-list">${Object.entries(s.models).map(([m, c]) => `<span>${esc(m)}</span><span class="num">${c} documents</span>`).join('') || '<span>—</span><span></span>'}</div>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-head"><div class="card-title">Small vs. large model on labeled documents</div><div class="card-note">out/results.csv</div></div>
      ${s.eval ? evalTable(s.eval)
        : `<div class="empty"><b>No eval results yet</b>Label documents with <code>streamlit run label_app.py</code>, then run
           <code>python -m eval.run_eval --config all</code>. Its results appear here.</div>`}
    </div>`;
}

// The columns of out/results.csv worth a glance; the CSV has them all. Blank = doesn't apply.
const EVAL_COLUMNS = [
  ['config', 'Config'], ['docs', 'Docs', 'int'], ['field_accuracy', 'Field accuracy', 'pct'],
  ['line_item_accuracy', 'Line items', 'pct'], ['total_spend_accuracy', 'Total spend', 'pct'],
  ['dollar_error', '$ error by category', 'usd'], ['escalated', 'Escalated', 'int'], ['flagged', 'Flagged', 'int'],
  ['cost_per_doc_usd', 'Cost / doc', 'cost'], ['p50_latency_s', 'p50', 'sec'], ['p95_latency_s', 'p95', 'sec'],
  ['notes', 'Notes'],
];
const EVAL_FORMAT = {
  pct: v => `${(Number(v) * 100).toFixed(1)}%`, usd: v => money(v, 'USD'), int: v => String(Math.round(Number(v))),
  cost: v => `$${Number(v).toFixed(4)}`, sec: v => `${Number(v).toFixed(1)}s`,
};

function evalTable(rows) {
  const columns = EVAL_COLUMNS.filter(([key]) => key in rows[0]);
  return `<div class="scroll-x"><table>
    <thead><tr>${columns.map(([key, label, kind]) => `<th class="${kind ? 'r' : ''}">${label}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr${r.config === 'small_escalate' ? ' style="background:var(--lime-soft)"' : ''}>${columns.map(([key, , kind]) =>
      `<td class="${kind ? 'r num' : ''}">${r[key] === '' ? '<span class="muted">—</span>' : kind ? EVAL_FORMAT[kind](r[key]) : esc(r[key])}</td>`).join('')}</tr>`).join('')}</tbody>
  </table></div>`;
}

// --- settings ----------------------------------------------------------------------------

async function settings(el) {
  const c = state.meta.config;
  const ghost = (title, sub) => `<div class="set-row ghost" title="Not available yet"><div class="t">${title} ${soon}<small>${sub}</small></div><div class="toggle"></div></div>`;
  el.innerHTML = `
    <h1>Settings</h1>
    <p class="lede">How documents are read and categorized. Pipeline settings come from <code>.env</code>; restart the server after changing them.</p>
    <div class="grid2" style="margin-top:0">
      <div class="card card-body">
        <div class="card-title">Pipeline</div>
        <div class="set-row"><div class="t">Fireworks API key<small>${state.meta.has_api_key ? 'Set' : 'Not set: new documents can’t be processed'}</small></div><span class="badge ${state.meta.has_api_key ? 'verified' : 'flagged'}">${state.meta.has_api_key ? 'Set' : 'Missing'}</span></div>
        <div class="set-row"><div class="t">Zoom into hard-to-read regions<small>ZOOM_ENABLED · more accurate on photos, slower</small></div><div class="toggle ${c.ZOOM_ENABLED ? 'on' : ''}" title="Set in .env"></div></div>
        <div class="meta-list">
          <span>Text model</span><span>${esc(c.TEXT_MODEL || '—')}</span>
          <span>Vision model</span><span>${esc(c.VISION_MODEL || '—')}</span>
          <span>Escalation model</span><span>${esc(c.ESCALATION_MODEL || '—')}</span>
          <span>Retries before escalating</span><span>${c.MAX_RETRIES}</span>
          <span>Documents at once</span><span>${c.WORKERS}</span>
        </div>
      </div>
      <div class="card card-body">
        <div class="card-title">Categories</div>
        <div class="card-note" style="margin-bottom:8px">From <code>categories.yaml</code>. Each description tells the model what belongs there.</div>
        ${state.meta.categories.map(cat => `<div class="cat-row"><span class="swatch" style="background:${cat.color}"></span><b>${esc(catLabel(cat.name))}</b><span class="muted" style="margin-left:6px">${esc(cat.description)}</span></div>`).join('')}
      </div>
      <div class="card card-body">
        <div class="card-title">Automation</div>
        ${ghost('Process new inbox items automatically', 'Needs connected sources and a background worker')}
        ${ghost('Weekly digest email', 'Needs a scheduler and email sending')}
        ${ghost('Monthly close report', 'xlsx emailed on the 1st of each month')}
      </div>
      <div class="card card-body">
        <div class="card-title">Budgets</div>
        ${ghost('Monthly budget per category', 'Show spent vs. budget and flag categories that go over')}
      </div>
    </div>`;
}

// --- routing -----------------------------------------------------------------------------

const VIEWS = {overview, inbox, review, doc: docView, lines, vendors, bench, settings};
const MONTHLY = new Set(['overview', 'lines']);       // views the month picker applies to
const BY_CURRENCY = new Set(['overview', 'lines', 'vendors']);

async function render() {
  const [name, arg] = location.hash.replace(/^#\/?/, '').split('/');
  const view = VIEWS[name] ? name : 'overview';
  if (view !== state.view) state.limit = PAGE;
  state.view = view;
  $$('.nav').forEach(n => n.classList.toggle('on', n.dataset.view === (view === 'doc' ? 'review' : view)));
  $('.period').hidden = !MONTHLY.has(view) || !state.months.length;
  $('#currency').hidden = !BY_CURRENCY.has(view) || state.currencies.length < 2;
  try {
    await VIEWS[view]($('#view'), arg && decodeURIComponent(arg));
  } catch (e) {
    $('#view').innerHTML = `<div class="card empty"><b>Something went wrong</b>${esc(e.message)}</div>`;
  }
  $('.period').hidden = !MONTHLY.has(view) || !state.months.length;
}

function bindTopbar() {
  $('#month').onchange = e => { state.month = e.target.value; render(); };
  $('#currency').onchange = e => { state.currency = e.target.value; state.month = null; render(); };
  $('#prev-month').onclick = () => stepMonth(-1);
  $('#next-month').onclick = () => stepMonth(1);
  $('#upload-btn').onclick = () => $('#upload-input').click();
  $('#upload-input').onchange = e => { uploadFiles(e.target.files); e.target.value = ''; };
  let timer;
  $('#search').oninput = e => {
    state.search = e.target.value.trim().toLowerCase();
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!['inbox', 'lines', 'vendors'].includes(state.view)) location.hash = '#/inbox';
      else { state.limit = PAGE; render(); }
    }, 200);
  };
  document.addEventListener('click', e => {
    const p = e.target.closest('[data-process]');
    if (p && !p.disabled) processFolder(p.dataset.process);
    if (e.target.closest('[data-upload]')) $('#upload-input').click();
  });
  window.addEventListener('hashchange', () => { render(); window.scrollTo(0, 0); });
}

async function init() {
  bindTopbar();
  await refresh();
  if (state.docs.length) syncPeriod(await api('/spend'));
  render();
}

init().catch(e => {
  $('#view').innerHTML = `<div class="card empty"><b>Can't reach the server</b>${esc(e.message)}</div>`;
});
