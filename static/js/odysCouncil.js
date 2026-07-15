// static/js/odysCouncil.js — Odys Council UI
// 5 agents: research, business, architect, developer, marketing.
// Pods + agent office + report viewer.

let _deps = {};
let _agents = [];
let _reports = [];
let _queue = { pending: [], waiting_for_approval: [], completed_today: [] };
let _activeAgentId = null;
let _activeReportId = null;

function el(id) { return document.getElementById(id); }
function esc(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function _fmtDate(ts) {
  if (!ts) return '';
  try { return new Date(ts).toLocaleString(); } catch (_) { return String(ts).slice(0, 16); }
}

async function _fetchJson(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || body.message || `HTTP ${res.status}`);
  }
  return res.json();
}

async function refresh() {
  try {
    const [agentsData, reportsData] = await Promise.all([
      _fetchJson('/api/odys/council/agents'),
      _fetchJson('/api/odys/council/reports'),
    ]);
    _agents = Array.isArray(agentsData.agents) ? agentsData.agents : [];
    _reports = Array.isArray(reportsData.reports) ? reportsData.reports : [];
    _queue = reportsData.queue || _queue;
    _renderPods();
    _renderQueue();
    _renderReportList();
  } catch (e) {
    console.error('Council refresh failed', e);
  }
}

function _renderPods() {
  const wrap = el('odys-council-pods');
  if (!wrap) return;
  if (!_agents.length) {
    wrap.innerHTML = '<div class="admin-empty">Council agents not available.</div>';
    return;
  }
  wrap.innerHTML = _agents.map(a => {
    const waiting = a.waiting_count || 0;
    const task = a.current_task ? esc(a.current_task) : '—';
    return `<div class="odys-council-pod" data-agent-id="${esc(a.id)}" style="cursor:pointer;border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--bg);transition:border-color .15s;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
        <span style="font-size:20px;opacity:0.7;">${esc(a.icon || '●')}</span>
        <div style="flex:1;">
          <div style="font-weight:600;font-size:13px;">${esc(a.name)} ${a.status === 'working' ? '⚡' : '✓'}</div>
          <div style="font-size:10px;opacity:0.5;">${esc(a.short || a.id)} · ${esc(a.description || '')}</div>
        </div>
      </div>
      <div style="font-size:11px;opacity:0.6;">Task: ${task}</div>
      <div style="font-size:10px;opacity:0.4;margin-top:4px;">Reports: ${a.report_count || 0} ${waiting ? `· ${waiting} pending` : ''}</div>
    </div>`;
  }).join('');
}

function _renderQueue() {
  const queueEl = el('odys-council-queue');
  if (!queueEl) return;
  const waiting = _queue.waiting_for_approval || [];
  const completed = _queue.completed_today || [];
  queueEl.innerHTML = `
    <div style="margin-bottom:6px;"><strong>Waiting Approval</strong> ${waiting.length ? `<span style="color:var(--accent,var(--red,#00b3a0))">(${waiting.length})</span>` : ''}</div>
    ${waiting.length ? waiting.slice(0, 5).map(r => `<div style="font-size:11px;padding:4px 0;border-bottom:1px solid var(--border);">${esc(r.title)}</div>`).join('') : '<div style="font-size:11px;opacity:0.5;">None</div>'}
    <div style="margin-top:10px;margin-bottom:4px;"><strong>Completed Today</strong> <span style="opacity:0.5;font-size:11px;">${completed.length}</span></div>
    ${completed.length ? completed.slice(0, 5).map(r => `<div style="font-size:11px;padding:4px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;"><span>${esc(r.title)}</span><span style="opacity:0.4;font-size:10px;">${esc(r.agent_name || r.agent_id)}</span></div>`).join('') : '<div style="font-size:11px;opacity:0.5;">None</div>'}`;
}

function _renderReportList() {
  const list = el('odys-council-report-list');
  if (!list) return;
  if (!_reports.length) {
    list.innerHTML = '<div style="padding:8px;font-size:11px;opacity:0.5;">No reports yet. Click an agent.</div>';
    return;
  }
  list.innerHTML = _reports.map(r => `<div data-report-id="${esc(r.id)}" style="cursor:pointer;padding:6px 8px;font-size:11px;border-bottom:1px solid var(--border);transition:background .1s;">${esc(r.title)} <span style="float:right;opacity:0.4;">${esc(r.agent_name || r.agent_id)}</span></div>`).join('');
}

async function _runAction(agentId, action) {
  const msg = el('odys-council-msg');
  if (msg) msg.textContent = `⏳ Running ${action}...`;
  try {
    const data = await _fetchJson('/api/odys/council/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: agentId, action }),
    });
    if (data.ok) {
      if (msg) msg.textContent = `✅ ${data.message}`;
      await refresh();
    } else {
      if (msg) msg.textContent = `❌ ${data.message}`;
    }
  } catch (e) {
    if (msg) msg.textContent = `❌ ${e.message}`;
  }
}

function _openReport(id) {
  _activeReportId = id;
  const detail = el('odys-council-report');
  const nameEl = el('odys-council-report-name');
  const bodyEl = el('odys-council-report-body');
  if (!detail || !nameEl || !bodyEl) return;
  detail.style.display = 'block';
  const r = _reports.find(x => x.id === id);
  if (!r) { nameEl.textContent = 'Not found'; bodyEl.textContent = ''; return; }
  nameEl.textContent = r.title || 'Report';
  bodyEl.innerHTML = (r.body || 'No body').split('\n').map(l => l.startsWith('# ') ? `<h3 style="margin:12px 0 6px;">${esc(l.slice(2))}</h3>`
    : l.startsWith('## ') ? `<h4 style="margin:8px 0 4px;opacity:0.9;">${esc(l.slice(3))}</h4>`
    : l.startsWith('- ') ? `<div style="font-size:12px;padding-left:12px;margin:2px 0;">· ${esc(l.slice(2))}</div>`
    : l.startsWith('[') && l.endsWith(']') ? `<div style="font-size:11px;opacity:0.5;">${esc(l)}</div>`
    : l.trim() ? `<div style="font-size:12px;margin:3px 0;">${esc(l)}</div>`
    : '<br>').join('');
}

function _closeReport() {
  const detail = el('odys-council-report');
  if (detail) detail.style.display = 'none';
  _activeReportId = null;
}

function _bindEvents() {
  const wrap = el('odys-council-pods');
  if (wrap) {
    wrap.addEventListener('click', (e) => {
      const pod = e.target.closest('[data-agent-id]');
      if (!pod) return;
      const agentId = pod.dataset.agentId;
      const agent = _agents.find(a => a.id === agentId);
      if (!agent) return;
      const actions = agent.actions || [];
      if (actions.length === 0) return;
      // Show action picker
      const msg = el('odys-council-msg');
      if (msg) {
        msg.innerHTML = `<div style="font-size:12px;font-weight:500;margin-bottom:4px;">${esc(agent.name)} — pilih action:</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;">
            ${actions.map(a => `<button type="button" class="admin-btn-sm" data-run-action="${esc(agentId)}:${esc(a.id)}">${esc(a.label)}</button>`).join('')}
            <button type="button" class="admin-btn-sm" style="opacity:0.5;" data-run-action-cancel>Cancel</button>
          </div>`;
        // Bind action buttons
        msg.querySelectorAll('[data-run-action]').forEach(btn => {
          btn.addEventListener('click', () => {
            const [aid, act] = btn.dataset.runAction.split(':');
            _runAction(aid, act);
            if (msg) msg.innerHTML = '';
          });
        });
        msg.querySelector('[data-run-action-cancel]')?.addEventListener('click', () => {
          if (msg) msg.innerHTML = '';
        });
      }
    });
  }

  // Report list
  const list = el('odys-council-report-list');
  if (list) {
    list.addEventListener('click', (e) => {
      const row = e.target.closest('[data-report-id]');
      if (row) {
        _openReport(row.dataset.reportId);
        // Deselect others
        list.querySelectorAll('[data-report-id]').forEach(x => x.classList.remove('selected'));
        row.classList.add('selected');
      }
    });
  }

  const closeBtn = el('odys-council-report-close');
  if (closeBtn) closeBtn.addEventListener('click', () => _closeReport());
}

export async function initOdysCouncil(deps = {}) {
  _deps = deps;
  _bindEvents();
  await refresh();
}

const odysCouncilModule = { initOdysCouncil, refresh };
export default odysCouncilModule;
