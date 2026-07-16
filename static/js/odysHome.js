// static/js/odysHome.js — Odys Home Dashboard
// Briefing, status, recent projects, Active Thoughts (neurons).

let _deps = {};
let _data = null;

function el(id) { return document.getElementById(id); }
function esc(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function _fmtDate(ts) {
  if (!ts) return '—';
  try { return new Date(ts).toLocaleString(); } catch (_) { return String(ts).slice(0, 16); }
}

function _statusDot(ok) {
  return ok
    ? '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:4px;"></span>'
    : '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#ef4444;margin-right:4px;"></span>';
}

function _typeBadge(t) {
  const colors = {
    memory: '#60a5fa',
    vault_note: '#a78bfa',
    project: '#00b3a0',
  };
  const c = colors[t] || 'var(--fg)';
  return `<span style="font-size:10px;padding:1px 5px;border-radius:3px;border:1px solid ${c};color:${c};opacity:0.9;">${esc(t)}</span>`;
}

async function _fetchBriefing() {
  const res = await fetch('/api/odys/home/briefing', { credentials: 'same-origin' });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || body.message || `HTTP ${res.status}`);
  }
  return res.json();
}

async function _postNeuron(path) {
  const res = await fetch(`/api/odys/neurons/${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || body.message || `HTTP ${res.status}`);
  return body;
}

function _renderNeurons() {
  const box = el('odys-home-neurons');
  const statsEl = el('odys-home-neuron-stats');
  if (!box) return;

  const n = (_data && _data.neurons) || {};
  const stats = n.stats || {};
  if (statsEl) {
    const cold = stats.cold_start ? ' · cold' : '';
    statsEl.textContent = n.ok
      ? `${stats.node_count || 0}n · ${stats.edge_count || 0}e${cold}`
      : (n.message || 'offline');
  }

  const active = n.active || [];
  if (!n.ok) {
    box.innerHTML = `<div class="admin-empty" style="font-size:12px;">Neurons offline. ${esc(n.message || '')}</div>`;
    return;
  }
  if (!active.length) {
    box.innerHTML = '<div class="admin-empty" style="font-size:12px;">No active thoughts yet. Chat or Sync Vault.</div>';
    return;
  }
  box.innerHTML = active.map(a => `
    <div style="font-size:12px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);display:flex;flex-direction:column;gap:2px;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
        <span style="font-weight:600;">${esc(a.label)}</span>
        <span style="opacity:0.55;font-size:11px;">${(a.score ?? 0).toFixed(2)}</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
        ${_typeBadge(a.type)}
        <span style="font-size:10px;opacity:0.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%;">${esc(a.why || a.ref || '')}</span>
      </div>
    </div>`).join('');
}

function _render() {
  if (!_data) return;

  const greeting = el('odys-home-greeting');
  const headline = el('odys-home-headline');
  const rec = el('odys-home-rec');
  const status = el('odys-home-status');
  const priorities = el('odys-home-priorities');
  const projects = el('odys-home-projects');
  const msg = el('odys-home-msg');

  if (greeting) greeting.textContent = _data.greeting || 'Halo';
  if (headline) headline.textContent = _data.headline || '';
  if (rec) rec.textContent = _data.recommendation || '';

  if (status) {
    const s = _data.status || {};
    const server = s.server || {};
    const bridge = s.bridge || {};
    status.innerHTML = `
      <div style="display:flex;flex-wrap:wrap;gap:12px;font-size:12px;">
        <div>${_statusDot(server.ok)} Server: ${esc(server.status || '—')}</div>
        <div>${_statusDot(bridge.ok)} Bridge: ${esc(bridge.status || '—')}</div>
        <div style="opacity:0.5;">${_fmtDate(s.timestamp)}</div>
      </div>`;
  }

  if (priorities) {
    const items = _data.priorities || [];
    if (!items.length) {
      priorities.innerHTML = '<div class="admin-empty" style="font-size:12px;">Belum ada proyek. Scan dulu di Odys Projects.</div>';
    } else {
      priorities.innerHTML = items.map(p => `
        <div style="border:1px solid var(--border);border-radius:6px;padding:8px 10px;background:var(--bg);">
          <div style="font-weight:600;font-size:13px;">${esc(p.name)}</div>
          <div style="font-size:11px;opacity:0.6;margin-top:2px;">
            ${esc(p.stack)} · ${p.files || 0} files
            ${p.activity ? ' · ' + _fmtDate(p.activity) : ''}
          </div>
        </div>`).join('');
    }
  }

  if (projects) {
    const list = _data.projects || [];
    projects.innerHTML = list.length
      ? list.map(p => `
        <div style="font-size:12px;padding:4px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;">
          <span>${esc(p.name)} <span style="opacity:0.5;">${esc((p.detected_stack || [])[0] || '')}</span></span>
          <span style="opacity:0.5;font-size:11px;">${p.file_count || 0} files</span>
        </div>`).join('')
      : '<div class="admin-empty" style="font-size:12px;">No projects</div>';
  }

  _renderNeurons();
  if (msg) msg.textContent = '';
}

async function refresh() {
  const msg = el('odys-home-msg');
  if (msg) msg.textContent = '⏳ Loading...';
  try {
    _data = await _fetchBriefing();
    _render();
    if (msg) msg.textContent = '';
  } catch (e) {
    if (msg) msg.textContent = `❌ ${e.message}`;
  }
}

function _bindEvents() {
  const refreshBtn = el('odys-home-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', () => refresh());

  const openProjectsBtn = el('odys-home-open-projects-btn');
  if (openProjectsBtn) {
    openProjectsBtn.addEventListener('click', () => {
      const homeModal = el('odys-home-modal');
      if (homeModal) homeModal.classList.add('hidden');
      document.getElementById('tool-odys-btn')?.click();
    });
  }

  const speakBtn = el('odys-home-speak-btn');
  if (speakBtn) {
    speakBtn.addEventListener('click', () => {
      const text = _data?.spoken || _data?.headline;
      if (!text) return;
      if (window.speechSynthesis) {
        const u = new SpeechSynthesisUtterance(text);
        u.lang = 'id-ID';
        window.speechSynthesis.speak(u);
      }
    });
  }

  const syncBtn = el('odys-home-sync-vault-btn');
  if (syncBtn) {
    syncBtn.addEventListener('click', async () => {
      const msg = el('odys-home-msg');
      if (msg) msg.textContent = '⏳ Syncing vault → neurons...';
      try {
        const r = await _postNeuron('sync-vault');
        if (msg) {
          msg.textContent = `✅ Vault: ${r.upserted || 0} notes · ${r.wikilink_edges || 0} links · ${r.files_scanned || 0} files`;
        }
        await refresh();
      } catch (e) {
        if (msg) msg.textContent = `❌ ${e.message}`;
      }
    });
  }

  const decayBtn = el('odys-home-decay-btn');
  if (decayBtn) {
    decayBtn.addEventListener('click', async () => {
      const msg = el('odys-home-msg');
      if (msg) msg.textContent = '⏳ Running decay...';
      try {
        const r = await _postNeuron('decay');
        if (msg) {
          msg.textContent = `✅ Decay: dropped ${r.dropped_edges || 0} edges · archived ${r.archived_nodes || 0}`;
        }
        await refresh();
      } catch (e) {
        if (msg) msg.textContent = `❌ ${e.message}`;
      }
    });
  }
}

export async function initOdysHome(deps = {}) {
  _deps = deps;
  _bindEvents();
  await refresh();
}

const odysHomeModule = { initOdysHome, refresh };
export default odysHomeModule;
