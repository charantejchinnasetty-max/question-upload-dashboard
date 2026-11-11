(() => {
  const $ = (id) => document.getElementById(id);
  const STORE = 'asc-agent-chat-v2';
  const FILTER_IDS = ['agent-search', 'agent-state', 'agent-batch', 'agent-college', 'agent-status'];
  const MONEY = /(fee|paid|due|discount|amount)/i;
  const LABELS = { students: 'Students', sum_due: 'Total due', sum_paid: 'Total paid', sum_total_fee: 'Total fee', avg_due: 'Avg due', label: 'Name', events_attended: 'Classes attended', attended_events: 'Classes', paid_amount: 'Paid', due_amount: 'Due', batch_name: 'Batch', college_state: 'State' };
  let history = [];
  let busy = false;

  const esc = (v) => String(v ?? '').replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
  const inr = (n) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(Number(n || 0));
  const num = (n) => Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  const label = (c) => LABELS[c] || c.replace(/_/g, ' ').replace(/^\w/, (x) => x.toUpperCase());
  const cell = (col, v) => {
    if (v === null || v === undefined || v === '') return { html: '—' };
    if (Array.isArray(v)) return { html: `<div class="ag-tags">${v.map((x) => `<span>${esc(x)}</span>`).join('')}</div>` };
    if (typeof v === 'number') return { html: MONEY.test(col) ? inr(v) : num(v), num: true };
    return { html: esc(v) };
  };

  // ── Minimal, safe markdown (everything is escaped first) ──
  function inline(s) {
    return s.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>').replace(/(^|\s)_([^_\n]+)_(?=\s|[.,!?]|$)/g, '$1<em>$2</em>');
  }
  function markdown(src) {
    const lines = esc(src || '').split('\n');
    let html = '', i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const row = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
        html += `<table><thead><tr>${row(line).map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>`;
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { html += `<tr>${row(lines[i]).map((c) => `<td>${c}</td>`).join('')}</tr>`; i++; }
        html += '</tbody></table>'; continue;
      }
      if (/^\s*[-*•]\s+/.test(line)) {
        html += '<ul>'; while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) { html += `<li>${inline(lines[i].replace(/^\s*[-*•]\s+/, ''))}</li>`; i++; } html += '</ul>'; continue;
      }
      if (/^\s*\d+[.)]\s+/.test(line)) {
        html += '<ol>'; while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) { html += `<li>${inline(lines[i].replace(/^\s*\d+[.)]\s+/, ''))}</li>`; i++; } html += '</ol>'; continue;
      }
      const h = line.match(/^#{1,4}\s+(.*)/);
      if (h) { html += `<h3>${inline(h[1])}</h3>`; i++; continue; }
      if (!line.trim()) { i++; continue; }
      let para = [];
      while (i < lines.length && lines[i].trim() && !/^\s*([-*•]|\d+[.)]|#|\|)/.test(lines[i])) { para.push(inline(lines[i])); i++; }
      if (!para.length) { para = [inline(line)]; i++; }
      html += `<p>${para.join('<br>')}</p>`;
    }
    return html;
  }

  // ── Results: chart + table + CSV ──
  function chartHtml(chart) {
    if (!chart?.items?.length) return '';
    const max = Math.max(...chart.items.map((x) => Number(x.value) || 0), 1);
    const money = MONEY.test(chart.value);
    return `<div class="ag-chart">${chart.items.map((x) => `<div class="ag-bar"><span title="${esc(x.label)}">${esc(x.label)}</span><i style="width:${Math.max(1, (Number(x.value) / max) * 100)}%"></i><b>${money ? inr(x.value) : num(x.value)}</b></div>`).join('')}</div>`;
  }
  function tableHtml(rows, columns) {
    const cols = (columns?.length ? columns : Object.keys(rows[0]));
    return `<div class="ag-table-wrap"><table><thead><tr>${cols.map((c) => `<th>${esc(label(c))}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${cols.map((c) => { const x = cell(c, r[c]); return `<td class="${x.num ? 'num' : ''}">${x.html}</td>`; }).join('')}</tr>`).join('')}</tbody></table></div>`;
  }
  function csv(rows, columns) {
    const cols = columns?.length ? columns : Object.keys(rows[0]);
    const q = (v) => `"${String(Array.isArray(v) ? v.join('; ') : v ?? '').replace(/"/g, '""')}"`;
    return [cols.map((c) => q(label(c))).join(','), ...rows.map((r) => cols.map((c) => q(r[c])).join(','))].join('\n');
  }
  function download(name, text) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob(['﻿' + text], { type: 'text/csv;charset=utf-8' }));
    a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ── Rendering ──
  const scrollDown = () => { const s = $('ag-scroll'); s.scrollTop = s.scrollHeight; };
  function setWelcome() { $('ag-welcome').hidden = $('agent-messages').children.length > 0; }

  function renderUser(text) {
    const n = document.createElement('div');
    n.className = 'ag-msg me';
    n.innerHTML = `<span class="ag-avatar" aria-hidden="true">You</span><div class="ag-bubble">${esc(text)}</div>`;
    $('agent-messages').append(n); setWelcome(); scrollDown();
  }

  function renderBot(data, question) {
    const n = document.createElement('div');
    n.className = 'ag-msg bot';
    const rows = data.rows || [];
    const showChart = data.chart?.items?.length > 1;
    const resultId = 'r' + Math.random().toString(36).slice(2, 8);
    const result = rows.length ? `
      <div class="ag-result">
        <div class="ag-result-head"><span>${rows.length.toLocaleString('en-IN')} row${rows.length === 1 ? '' : 's'} from your data</span>
          <span>${showChart ? `<button type="button" class="ag-chip-btn" data-toggle="${resultId}">Show table</button> ` : ''}<button type="button" class="ag-chip-btn" data-csv="1">⬇ CSV</button></span></div>
        ${showChart ? `<div id="${resultId}-chart">${chartHtml(data.chart)}</div><div id="${resultId}-table" hidden>${tableHtml(rows, data.columns)}</div>` : tableHtml(rows, data.columns)}
      </div>` : '';
    const steps = (data.steps || []).length ? `<details class="ag-steps"><summary>How I found this (${data.steps.length} data lookup${data.steps.length > 1 ? 's' : ''})</summary><ul>${data.steps.map((s) => `<li>${esc(s.tool.replace(/_/g, ' '))}${s.error ? ` — <span class="ag-error">${esc(s.error)}</span>` : ''}</li>`).join('')}</ul></details>` : '';
    n.innerHTML = `<span class="ag-avatar" aria-hidden="true">✦</span><div class="ag-bubble">
      <div class="ag-md">${data.error ? `<p class="ag-error">${esc(data.error)}</p>` : markdown(data.answer)}</div>
      ${result}${steps}
      ${data.error ? '' : `<div class="ag-meta"><span class="ag-badge ${data.mode === 'ai' ? '' : 'basic'}">${data.mode === 'ai' ? '✦ AI answer' : 'Basic mode'}</span>${data.ai_error ? `<span class="ag-badge basic" title="${esc(data.ai_error)}">AI unavailable — basic answer</span>` : ''}<button type="button" class="ag-chip-btn" data-copy="1">Copy</button>${question ? '<button type="button" class="ag-chip-btn" data-retry="1">↻ Ask again</button>' : ''}</div>`}
    </div>`;
    n.querySelector('[data-copy]')?.addEventListener('click', (e) => { navigator.clipboard.writeText(data.answer || ''); e.target.textContent = 'Copied ✓'; setTimeout(() => (e.target.textContent = 'Copy'), 1500); });
    n.querySelector('[data-retry]')?.addEventListener('click', () => ask(question));
    n.querySelector('[data-csv]')?.addEventListener('click', () => download(`student-agent-${new Date().toISOString().slice(0, 10)}.csv`, csv(rows, data.columns)));
    n.querySelector('[data-toggle]')?.addEventListener('click', (e) => {
      const t = $(`${resultId}-table`), c = $(`${resultId}-chart`);
      t.hidden = !t.hidden; c.hidden = !c.hidden; e.target.textContent = t.hidden ? 'Show table' : 'Show chart';
    });
    $('agent-messages').append(n);
    if (data.followups?.length) {
      const f = document.createElement('div');
      f.className = 'ag-followups';
      f.innerHTML = data.followups.map((q) => `<button type="button">${esc(q)}</button>`).join('');
      f.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { f.remove(); ask(b.textContent); }));
      $('agent-messages').append(f);
    }
    setWelcome(); scrollDown();
  }

  function typing() {
    const n = document.createElement('div');
    n.className = 'ag-msg bot';
    const phrases = ['Reading your student data…', 'Matching batches and colleges…', 'Calculating totals…', 'Preparing your answer…'];
    n.innerHTML = `<span class="ag-avatar" aria-hidden="true">✦</span><div class="ag-bubble"><div class="ag-typing"><span class="ag-dots"><i></i><i></i><i></i></span><span>${phrases[0]}</span></div></div>`;
    $('agent-messages').append(n); scrollDown();
    let k = 0; const t = setInterval(() => { k = (k + 1) % phrases.length; const s = n.querySelector('.ag-typing span:last-child'); if (s) s.textContent = phrases[k]; }, 1800);
    return () => { clearInterval(t); n.remove(); };
  }

  // ── Conversation ──
  const filters = () => ({ search: $('agent-search').value.trim(), state: $('agent-state').value, batch: $('agent-batch').value, college: $('agent-college').value, status: $('agent-status').value });
  function save(entry) {
    try { const all = JSON.parse(sessionStorage.getItem(STORE) || '[]'); all.push(entry); sessionStorage.setItem(STORE, JSON.stringify(all.slice(-40))); } catch { /* storage full or blocked */ }
  }

  async function ask(question) {
    question = (question || '').trim();
    if (!question || busy) return;
    busy = true; $('ag-send').disabled = true;
    document.querySelectorAll('.ag-followups').forEach((f) => f.remove());
    renderUser(question); save({ type: 'user', text: question });
    $('agent-question').value = ''; grow();
    const stop = typing();
    let data;
    try {
      const res = await fetch('/api/students/agent', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question, filters: filters(), history: history.slice(-8) }) });
      data = await res.json().catch(() => ({}));
      if (!res.ok) data = { error: data.error || `Something went wrong (${res.status}). Please try again.` };
    } catch {
      data = { error: 'Could not reach the server. Check your connection and try again.' };
    }
    stop();
    if (!data.error) history.push({ role: 'user', content: question }, { role: 'assistant', content: data.answer });
    renderBot(data, question); save({ type: 'bot', data, question });
    busy = false; $('ag-send').disabled = false; $('agent-question').focus();
  }

  function restore() {
    try {
      for (const e of JSON.parse(sessionStorage.getItem(STORE) || '[]')) {
        if (e.type === 'user') { renderUser(e.text); history.push({ role: 'user', content: e.text }); }
        else { renderBot(e.data, e.question); if (!e.data.error) history.push({ role: 'assistant', content: e.data.answer }); }
      }
      document.querySelectorAll('.ag-followups').forEach((f, i, all) => { if (i < all.length - 1) f.remove(); });
    } catch { sessionStorage.removeItem(STORE); }
    setWelcome();
  }

  // ── Setup ──
  function grow() { const t = $('agent-question'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 160) + 'px'; }
  function options(id, values) { const el = $(id), first = el.options[0].outerHTML; el.innerHTML = first + values.map((r) => `<option value="${esc(r.label)}">${esc(r.label)}</option>`).join(''); }
  function filterCount() {
    const n = FILTER_IDS.filter((id) => $(id).value.trim()).length;
    $('ag-filter-count').hidden = !n; $('ag-filter-count').textContent = n;
  }

  async function load() {
    const h = new Date().getHours();
    $('ag-greeting').textContent = `${h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening'}! What would you like to know about your students?`;
    fetch('/api/students/agent/status').then((r) => r.json()).then((s) => {
      const m = $('ag-mode');
      m.textContent = s.mode === 'ai' ? `● AI mode · ${s.provider || 'AI'} · understands any question` : '● Basic mode · connect an AI service for full answers';
      m.className = s.mode === 'ai' ? 'ai' : 'basic';
    }).catch(() => { $('ag-mode').textContent = 'Status unavailable'; });
    try {
      const res = await fetch('/api/students/dashboard?include_filters=1');
      const d = await res.json();
      if (!res.ok) throw new Error(d.error);
      ['states', 'batches', 'colleges', 'statuses'].forEach((k, i) => options(['agent-state', 'agent-batch', 'agent-college', 'agent-status'][i], d.filters[k] || []));
      const s = d.summary || {};
      $('ag-stats').innerHTML = s.students ? [
        [Number(s.students).toLocaleString('en-IN'), 'students'], [s.batches, 'batches'], [s.colleges, 'colleges'], [inr(s.due_amount), 'pending dues'],
      ].map(([v, l]) => `<div class="ag-stat"><b>${esc(v)}</b><span>${l}</span></div>`).join('') : '<div class="ag-stat"><b>No data yet</b><span>Import a student Excel in Student Dashboard</span></div>';
    } catch {
      $('ag-stats').innerHTML = '<div class="ag-stat"><b>Database unavailable</b><span>Check DATABASE_URL</span></div>';
    }
  }

  $('agent-form').addEventListener('submit', (e) => { e.preventDefault(); ask($('agent-question').value); });
  $('agent-question').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask($('agent-question').value); } });
  $('agent-question').addEventListener('input', grow);
  document.querySelectorAll('.ag-cat button').forEach((b) => b.addEventListener('click', () => ask(b.dataset.q)));
  $('ag-filter-toggle').addEventListener('click', (e) => { const p = $('ag-filters'); p.hidden = !p.hidden; e.currentTarget.setAttribute('aria-expanded', String(!p.hidden)); });
  FILTER_IDS.forEach((id) => $(id).addEventListener('change', filterCount));
  $('agent-search').addEventListener('input', filterCount);
  $('agent-reset').addEventListener('click', () => { FILTER_IDS.forEach((id) => { $(id).value = ''; }); filterCount(); });
  $('ag-new').addEventListener('click', () => { sessionStorage.removeItem(STORE); history = []; $('agent-messages').innerHTML = ''; setWelcome(); $('agent-question').focus(); });

  restore();
  load();
})();
