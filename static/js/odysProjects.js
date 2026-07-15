// static/js/odysProjects.js — Odys Projects panel
// Scan D:/Project, index repo, track changes.
// Standalone — no dependency on ui.js import.

let _deps = {};
let _projects = [];
let _selectedId = null;

function el(id) { return document.getElementById(id); }
function esc(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

async function _fetchJson(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.message || body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function loadProjects() {
  const data = await _fetchJson('/api/odys/projects');
  _projects = Array.isArray(data.projects) ? data.projects : [];
  return _projects;
}

async function scanProjects() {
  const data = await _fetchJson('/api/odys/projects/scan', { method: 'POST' });
  _projects = Array.isArray(data.projects) ? data.projects : [];
  return data;
}

async function indexProject(id) {
  return _fetchJson(`/api/odys/projects/${encodeURIComponent(id)}/index`, { method: 'POST' });
}

async function getDetail(id) {
  return _fetchJson(`/api/odys/projects/${encodeURIComponent(id)}/detail`);
}

async function togglePin(id) {
  return _fetchJson(`/api/odys/projects/${encodeURIComponent(id)}/pin`, { method: 'POST' });
}

function _fmtDate(ts) {
  if (!ts) return '—';
  try { return new Date(ts).toLocaleString(); } catch (_) { return ts; }
}

function _renderList() {
  const list = el('odys-project-list');
  if (!list) return;
  if (!_projects.length) {
    list.innerHTML = '<div class="admin-empty">No projects found. Click Scan to discover.</div>';
    return;
  }
  list.innerHTML = _projects.map(p => {
    const stack = (p.detected_stack || []).join(' · ') || p.detected_type || '—';
    const changes = p.recent_changes || {};
    const cCount = (changes.new_count || 0) + (changes.modified_count || 0) + (changes.deleted_count || 0);
    const isSelected = p.id === _selectedId;
    return `<div class="odys-project-card ${isSelected ? 'odys-project-card--selected' : ''}" data-project-id="${esc(p.id)}" style="cursor:pointer;border:1px solid ${isSelected ? 'var(--accent,var(--red))' : 'var(--border)'};border-radius:6px;padding:10px;background:var(--bg);transition:border-color .15s;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;">
        <div>
          <div style="font-weight:600;font-size:13px;">${esc(p.name)}</div>
          <div style="font-size:11px;opacity:0.6;margin-top:2px;">${esc(stack)} · ${p.file_count || 0} files</div>
        </div>
        <div style="font-size:10px;opacity:0.5;text-align:right;">
          ${p.last_activity_at ? _fmtDate(p.last_activity_at) : ''}
          ${cCount > 0 ? `<span style="color:var(--accent,var(--red))"> · ${cCount} changes</span>` : ''}
        </div>
      </div>
      <div style="font-size:10px;margin-top:4px;opacity:0.5;">
        ${p.path ? esc(p.path) : ''}
      </div>
    </div>`;
  }).join('');
}

function _selectProject(id) {
  _selectedId = id;
  _renderList();
  const detail = el('odys-project-detail');
  const nameEl = el('odys-detail-name');
  const metaEl = el('odys-detail-meta');
  const changesEl = el('odys-detail-changes');
  const msgEl = el('odys-detail-msg');
  if (msgEl) msgEl.textContent = '';

  const p = _projects.find(x => x.id === id);
  if (!p) {
    if (detail) detail.style.display = 'none';
    return;
  }

  if (detail) detail.style.display = 'block';
  if (nameEl) nameEl.textContent = p.name;
  if (metaEl) {
    metaEl.innerHTML = `${esc(p.detected_type || 'Project')} · ${esc((p.detected_stack || []).join(' · ') || '—')}<br>
    Path: ${esc(p.path || '—')}<br>
    Files: ${p.file_count || 0} ${p.pinned ? '· 📌 Pinned' : ''}`;
  }
  if (changesEl) {
    const ch = p.recent_changes || {};
    const files = (ch.recent_files || []).slice(0, 8);
    changesEl.innerHTML = `
      <div style="margin-bottom:4px;font-weight:500;">Recent Activity</div>
      ${files.length ? files.map(f => `<div style="font-size:10px;font-family:monospace;opacity:0.7;">· ${esc(f)}</div>`).join('') : '<div style="font-size:11px;opacity:0.5;">No recent changes tracked. Run Index.</div>'}`;
  }
}

async function _renderDetail(id) {
  const data = await getDetail(id);
  if (!data.ok) return;

  const detail = el('odys-project-detail');
  const nameEl = el('odys-detail-name');
  const metaEl = el('odys-detail-meta');
  const changesEl = el('odys-detail-changes');
  const msgEl = el('odys-detail-msg');
  if (msgEl) msgEl.textContent = '';

  const p = data.project || {};
  if (nameEl) nameEl.textContent = p.name || 'Project';
  if (metaEl) {
    metaEl.innerHTML = `
    ${esc(p.detected_type || 'Project')} · ${esc((p.detected_stack || []).join(' · ') || '—')}<br>
    Path: ${esc(p.path || '—')}<br>
    Files: ${p.file_count || 0}<br>
    Score: ${data.score?.overall ?? '—'}/100 · Stage: ${esc(data.stage?.current || '—')}`;
  }
  if (changesEl) {
    const ch = data.recent_changes || {};
    const files = (ch.recent_files || []).slice(0, 10);
    changesEl.innerHTML = `
      <div style="margin-bottom:4px;font-weight:500;">
        Changes · ${ch.new_count || 0} new, ${ch.modified_count || 0} modified
        ${ch.last_modified ? ` · ${_fmtDate(ch.last_modified)}` : ''}
      </div>
      ${files.length ? files.map(f => `<div style="font-size:10px;font-family:monospace;opacity:0.7;">· ${esc(f)}</div>`).join('') : '<div style="font-size:11px;opacity:0.5;">No recent changes</div>'}`;
  }
  if (detail) detail.style.display = 'block';
}

async function _handleScan() {
  const msg = el('odys-detail-msg');
  if (msg) msg.textContent = '⏳ Scanning...';
  try {
    const data = await scanProjects();
    _renderList();
    if (msg) msg.textContent = data.message || 'Scan complete';
  } catch (e) {
    if (msg) msg.textContent = `❌ ${e.message}`;
  }
}

async function _handleIndexAll() {
  const msg = el('odys-detail-msg');
  if (msg) msg.textContent = '⏳ Indexing all...';
  try {
    let count = 0;
    for (const p of _projects) {
      await indexProject(p.id);
      count++;
    }
    await loadProjects();
    _renderList();
    if (_selectedId) await _renderDetail(_selectedId);
    if (msg) msg.textContent = `✅ Indexed ${count} project(s)`;
  } catch (e) {
    if (msg) msg.textContent = `❌ ${e.message}`;
  }
}

function _bindEvents() {
  const scanBtn = el('odys-scan-btn');
  if (scanBtn) scanBtn.addEventListener('click', () => _handleScan());

  const idxBtn = el('odys-index-all-btn');
  if (idxBtn) idxBtn.addEventListener('click', () => _handleIndexAll());

  const detailIdx = el('odys-detail-index-btn');
  if (detailIdx) {
    detailIdx.addEventListener('click', async () => {
      if (!_selectedId) return;
      const msg = el('odys-detail-msg');
      if (msg) msg.textContent = '⏳ Indexing...';
      try {
        await indexProject(_selectedId);
        await loadProjects();
        _renderList();
        await _renderDetail(_selectedId);
        if (msg) msg.textContent = '✅ Indexed';
      } catch (e) {
        if (msg) msg.textContent = `❌ ${e.message}`;
      }
    });
  }

  const pinBtn = el('odys-detail-pin-btn');
  if (pinBtn) {
    pinBtn.addEventListener('click', async () => {
      if (!_selectedId) return;
      const msg = el('odys-detail-msg');
      try {
        await togglePin(_selectedId);
        await loadProjects();
        _renderList();
        if (msg) msg.textContent = '✅ Toggle pin';
      } catch (e) {
        if (msg) msg.textContent = `❌ ${e.message}`;
      }
    });
  }

  const list = el('odys-project-list');
  if (list) {
    list.addEventListener('click', (e) => {
      const card = e.target.closest('[data-project-id]');
      if (card) _selectProject(card.dataset.projectId);
    });
  }
}

export async function initOdysProjects(deps = {}) {
  _deps = deps;
  _bindEvents();
  try {
    await loadProjects();
    _renderList();
  } catch (e) {
    const list = el('odys-project-list');
    if (list) list.innerHTML = `<div class="admin-empty">❌ Load failed: ${e.message}</div>`;
  }
}

const odysProjectsModule = { initOdysProjects };
export default odysProjectsModule;
