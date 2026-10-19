(() => {
  const app = document.getElementById('storage-app');
  if (!app) return;
  const csrf = app.dataset.csrf || '';
  let snapshot = null;
  let policy = null;
  let busy = false;
  // Disable all write controls until the authenticated manager explicitly
  // advertises non-candidate capabilities. A missing manager is read-only.
  let managerFallback = true;
  let managerCanModify = false;
  let initialized = false;

  const labels = {
    acs_projects: 'Auto Caption projects',
    algrow_projects: 'Algrow delivered projects',
    scene_projects: 'Scene Builder projects',
    docker_build_cache: 'Docker build cache',
    apt_cache: 'APT cache',
    pip_cache: 'Python package cache',
    journals: 'Expired system journals',
    snap_cache: 'Snap cache',
    algrow_browser_cache: 'Algrow browser cache',
    temporary_files: 'Expired temporary files'
  };
  const projectCategories = new Set(['acs_projects', 'algrow_projects', 'scene_projects']);

  const bytes = value => {
    let n = Number(value || 0);
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${i === 0 ? Math.round(n) : n.toFixed(n >= 10 ? 1 : 2)} ${units[i]}`;
  };
  const ago = value => {
    if (!value) return 'Never';
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`;
    return `${Math.floor(seconds / 86400)} days ago`;
  };
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));

  async function api(path, options = {}) {
    const headers = {...(options.headers || {}), 'Accept': 'application/json'};
    if ((options.method || 'GET') !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-Storage-CSRF'] = csrf;
    }
    const response = await fetch(path, {...options, headers});
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(payload.detail || payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function toast(message, error = false) {
    const box = document.getElementById('storage-toast');
    box.textContent = message;
    box.className = `sm-toast${error ? ' error' : ''}`;
    box.hidden = false;
    window.clearTimeout(box._timer);
    box._timer = window.setTimeout(() => { box.hidden = true; }, 6000);
  }

  function setBar(id, value) {
    const element = document.getElementById(id);
    if (element) element.style.width = `${Math.max(0, Math.min(100, Number(value || 0)))}%`;
  }

  function applyManagerControls() {
    const writable = !managerFallback && managerCanModify;
    for (const id of ['run-scan', 'select-recommended']) {
      const control = document.getElementById(id);
      if (control) control.disabled = !writable;
    }
    const form = document.getElementById('policy-form');
    if (form) form.querySelectorAll('input, select, button').forEach(el => { el.disabled = !writable; });
    document.querySelectorAll('.sm-category input').forEach(el => {
      el.disabled = !writable || Number(el.dataset.bytes || 0) <= 0;
    });
    const fullClean = document.getElementById('full-production-clean');
    if (fullClean && !writable) fullClean.disabled = true;
    updateSelected();
  }

  function showDisconnectedStorageData() {
    snapshot = null;
    document.getElementById('reclaim-value').textContent = 'Unavailable';
    document.getElementById('reclaim-detail').textContent = 'Dedicated Storage Manager is not connected';
    document.getElementById('category-grid').innerHTML = '<div class="sm-empty">Cleanup scan unavailable. Live VPS metrics above remain read-only.</div>';
    document.getElementById('project-candidates').innerHTML = '<div class="sm-empty">Verified project candidates require the dedicated manager.</div>';
    document.getElementById('history-list').innerHTML = '<div class="sm-empty">Cleanup history requires the dedicated manager.</div>';
    document.getElementById('scan-time').textContent = 'Manager disconnected';
    applyManagerControls();
  }

  function scanUnavailableMessage(error) {
    const raw = String(error?.message || error || 'Unknown scan error');
    if (/permission denied|errno\\s*13|access denied/i.test(raw)) {
      return 'Storage scan unavailable: protected database read permission is missing. Cleanup remains disabled.';
    }
    return `Storage scan unavailable: ${raw}`;
  }

  function renderMetrics(data) {
    const manager = document.getElementById('manager-status');
    manager.className = 'sm-status online';
    manager.innerHTML = '<i></i> Manager online';
    document.getElementById('cpu-value').textContent = `${data.cpu.percent}%`;
    document.getElementById('cpu-detail').textContent = `${data.cpu.cores} cores · load ${data.cpu.load.map(x => Number(x).toFixed(2)).join(' / ')}`;
    setBar('cpu-bar', data.cpu.percent);
    document.getElementById('ram-value').textContent = `${data.memory.percent}%`;
    document.getElementById('ram-detail').textContent = `${bytes(data.memory.used)} of ${bytes(data.memory.total)}`;
    setBar('ram-bar', data.memory.percent);
    document.getElementById('disk-value').textContent = `${data.disk.percent}%`;
    document.getElementById('disk-detail').textContent = `${bytes(data.disk.free)} free of ${bytes(data.disk.total)}`;
    setBar('disk-bar', data.disk.percent);

  }

  async function readOnlyVpsMetrics() {
    // Scene's own authenticated /proc metrics remain available even if the
    // dedicated Storage Manager has not been provisioned. No cleanup calls.
    const response = await fetch('/api/dashboard/v1', {
      cache: 'no-store', credentials: 'same-origin', headers: {Accept: 'application/json'}
    });
    if (!response.ok) throw new Error('Dashboard HTTP ' + response.status);
    const data = await response.json();
    const vps = data.vps || {}, ram = vps.ram || {}, disk = vps.disk || {};
    const manager = document.getElementById('manager-status');
    manager.className = 'sm-status error';
    manager.textContent = 'Manager setup pending · live VPS metrics (read-only)';
    document.getElementById('cpu-value').textContent = (vps.cpu_percent ?? '—') + '%';
    document.getElementById('cpu-detail').textContent = (vps.cpu_cores ?? '—') + ' cores';
    setBar('cpu-bar', vps.cpu_percent);
    document.getElementById('ram-value').textContent = (ram.percent ?? '—') + '%';
    document.getElementById('ram-detail').textContent = (ram.used_gb ?? '—') + ' of ' + (ram.total_gb ?? '—') + ' GB';
    setBar('ram-bar', ram.percent);
    document.getElementById('disk-value').textContent = (disk.percent ?? '—') + '%';
    document.getElementById('disk-detail').textContent = (disk.free_gb ?? '—') + ' GB free of ' + (disk.total_gb ?? '—') + ' GB';
    setBar('disk-bar', disk.percent);
    // Missing backend never enables destructive actions.
    applyManagerControls();
  }

  async function loadMetrics(showError = false) {
    const wasFallback = managerFallback;
    try {
      const metrics = await api('/api/admin/storage/metrics');
      managerFallback = false;
      // Old candidate managers expose metrics, but all POST operations are
      // blocked. An absent capability endpoint also stays read-only.
      try {
        const capabilities = await api('/api/admin/storage/capabilities');
        managerCanModify = capabilities.can_modify === true;
      } catch (_) {
        managerCanModify = false;
      }
      renderMetrics(metrics);
      if (!managerCanModify) {
        const manager = document.getElementById('manager-status');
        manager.textContent = 'Manager connected · read-only controls';
      }
      applyManagerControls();
      if (wasFallback && initialized) {
        await Promise.all([loadSnapshot(false), loadHistory(), loadFullCleanStatus(false)]);
      }
    } catch (error) {
      managerFallback = true;
      managerCanModify = false;
      try {
        await readOnlyVpsMetrics();
      } catch (_) {
        const manager = document.getElementById('manager-status');
        manager.className = 'sm-status error';
        manager.textContent = 'Manager and dashboard metrics unavailable';
      }
      if (!wasFallback || !initialized) showDisconnectedStorageData();
      applyManagerControls();
      if (showError) toast('Storage Manager disconnected. Cleanup is disabled; available VPS metrics are read-only.', true);
    }
  }

  function selectedCategories() {
    return [...document.querySelectorAll('.sm-category input:checked')].map(input => input.value);
  }

  function updateSelected() {
    const selected = new Set(selectedCategories());
    let total = 0;
    document.querySelectorAll('.sm-category').forEach(card => {
      const input = card.querySelector('input');
      card.classList.toggle('selected', input.checked);
      if (input.checked) total += Number(input.dataset.bytes || 0);
    });
    document.getElementById('selected-total').textContent = `${bytes(total)} selected`;
    document.getElementById('reclaim-button').disabled = managerFallback || !managerCanModify || busy || !snapshot || !selected.size || total <= 0;
  }

  function renderCandidates(categories) {
    const rows = [];
    categories.filter(category => projectCategories.has(category.id)).forEach(category => {
      (category.items || []).forEach(item => rows.push(`
        <div class="sm-table-row">
          <span><b class="sm-app-badge">${escapeHtml(labels[category.id])}</b></span>
          <span><strong>${escapeHtml(item.name || item.project_id)}</strong><small>${escapeHtml(item.project_id)}</small></span>
          <span><strong>${bytes(item.bytes)}</strong></span>
          <span><strong>${escapeHtml(category.safety)}</strong><small>${escapeHtml(item.uploaded_at || item.handoff_status || 'Verified')}</small></span>
        </div>
      `));
    });
    document.getElementById('project-candidates').innerHTML = rows.length ? rows.join('') : '<div class="sm-empty">No uploaded project data currently meets the retention and verification rules.</div>';
  }

  function renderSnapshot(data) {
    snapshot = data;
    policy = data.policy || policy;
    document.getElementById('reclaim-value').textContent = bytes(data.reclaimable_bytes);
    document.getElementById('reclaim-detail').textContent = `${bytes(data.reclaimable_project_bytes)} projects · ${bytes(data.reclaimable_cache_bytes)} caches`;
    document.getElementById('scan-time').textContent = `Scanned ${ago(data.generated_at)}`;
    const categories = data.categories || [];
    document.getElementById('category-grid').innerHTML = categories.map(category => `
      <label class="sm-category">
        <input type="checkbox" value="${escapeHtml(category.id)}" data-bytes="${Number(category.bytes || 0)}" ${Number(category.bytes || 0) <= 0 ? 'disabled' : ''}>
        <strong>${escapeHtml(category.label || labels[category.id] || category.id)}</strong>
        <b>${bytes(category.bytes)}</b>
        <small>${Number(category.item_count || 0)} eligible item${Number(category.item_count || 0) === 1 ? '' : 's'} · ${escapeHtml(category.safety || '')}</small>
        ${category.id === 'algrow_browser_cache' ? '<small class="sm-warning">Briefly restarts only Algrow Chrome when selected.</small>' : ''}
      </label>
    `).join('');
    document.querySelectorAll('.sm-category input').forEach(input => input.addEventListener('change', updateSelected));
    renderCandidates(categories);
    renderPolicy(policy || {});
    applyManagerControls();
  }

  function renderPolicy(value) {
    if (!value) return;
    policy = value;
    document.getElementById('auto-enabled').checked = Boolean(value.auto_enabled);
    document.getElementById('auto-mode').value = value.auto_mode || 'low_space';
    document.getElementById('minimum-free').value = value.minimum_free_gb ?? 20;
    document.getElementById('retention-hours').value = value.retention_hours ?? 48;
    document.getElementById('scan-interval').value = value.scan_interval_minutes ?? 10;
    document.getElementById('journal-max').value = value.journal_max_mb ?? 300;
    document.getElementById('temp-days').value = value.temporary_retention_days ?? 2;
    const selected = new Set(value.auto_categories || []);
    document.getElementById('auto-categories').innerHTML = Object.entries(labels)
      .filter(([key]) => key !== 'algrow_browser_cache')
      .map(([key, label]) => `<label><input type="checkbox" value="${key}" ${selected.has(key) ? 'checked' : ''}>${escapeHtml(label)}</label>`).join('');
  }

  async function loadSnapshot(refresh = false) {
    if (managerFallback || (refresh && !managerCanModify)) return;
    busy = true; updateSelected();
    const button = document.getElementById('run-scan');
    button.disabled = true; button.textContent = refresh ? 'Scanning…' : 'Loading…';
    try {
      const data = refresh
        ? await api('/api/admin/storage/scan', {method: 'POST', body: '{}'})
        : await api('/api/admin/storage/snapshot');
      renderSnapshot(data);
    } catch (error) {
      const message = scanUnavailableMessage(error);
      if (refresh) toast(message, true);
      document.getElementById('reclaim-value').textContent = 'Unavailable';
      document.getElementById('reclaim-detail').textContent = 'Cleanup remains disabled until the read-only scan is available';
      document.getElementById('scan-time').textContent = 'Scan unavailable';
      document.getElementById('category-grid').innerHTML = `<div class="sm-empty">${escapeHtml(message)}</div>`;
      document.getElementById('project-candidates').innerHTML = '<div class="sm-empty">Verified project candidates are unavailable until the scan succeeds.</div>';
      snapshot = null;
    } finally {
      busy = false; button.textContent = 'Scan reclaimable space'; applyManagerControls();
    }
  }

  async function loadHistory() {
    try {
      const data = await api('/api/admin/storage/history');
      document.getElementById('history-list').innerHTML = data.operations.length ? data.operations.map(item => `
        <div class="sm-history"><div><strong>${escapeHtml(item.mode)} cleanup · ${bytes(item.reclaimed_bytes)}</strong><b class="${String(item.status).startsWith('failed') ? 'failed' : ''}">${escapeHtml(item.status)}</b></div><small>${escapeHtml(item.started_at)} · ${Number(item.item_count || 0)} items · ${(item.categories || []).map(x => escapeHtml(labels[x] || x)).join(', ')}</small>${item.error_text ? `<small>${escapeHtml(item.error_text)}</small>` : ''}</div>
      `).join('') : '<div class="sm-empty">No cleanup operations have run yet.</div>';
    } catch (error) {
      document.getElementById('history-list').innerHTML = `<div class="sm-empty">${escapeHtml(error.message)}</div>`;
    }
  }

  document.getElementById('refresh-metrics').addEventListener('click', () => loadMetrics(true));
  document.getElementById('run-scan').addEventListener('click', () => loadSnapshot(true));
  document.getElementById('select-recommended').addEventListener('click', () => {
    if (managerFallback || !managerCanModify) return;
    document.querySelectorAll('.sm-category input:not(:disabled)').forEach(input => {
      input.checked = input.value !== 'algrow_browser_cache';
    });
    updateSelected();
  });

  const dialog = document.getElementById('confirm-dialog');
  const confirmText = document.getElementById('confirm-text');
  const confirmRun = document.getElementById('confirm-run');
  confirmText.addEventListener('input', () => { confirmRun.disabled = confirmText.value !== 'RECLAIM'; });
  document.getElementById('reclaim-button').addEventListener('click', () => {
    if (managerFallback || !managerCanModify || !snapshot) return;
    const categories = selectedCategories();
    const total = categories.reduce((sum, key) => sum + Number(snapshot.categories.find(x => x.id === key)?.bytes || 0), 0);
    document.getElementById('confirm-summary').textContent = `${bytes(total)} across ${categories.length} selected categories`;
    confirmText.value = ''; confirmRun.disabled = true; dialog.showModal();
  });
  confirmRun.addEventListener('click', async event => {
    event.preventDefault();
    if (confirmText.value !== 'RECLAIM' || managerFallback || !managerCanModify) return;
    const categories = selectedCategories();
    dialog.close(); busy = true; updateSelected();
    const button = document.getElementById('reclaim-button');
    button.textContent = 'Cleanup running…'; button.disabled = true;
    try {
      const result = await api('/api/admin/storage/cleanup', {
        method: 'POST',
        body: JSON.stringify({categories, confirmation: 'RECLAIM'})
      });
      const message = `Cleanup finished: ${bytes(result.reclaimed_bytes)} reclaimed across ${result.items_cleaned} items.`;
      toast(message, Boolean(result.warnings?.length));
      await Promise.all([loadMetrics(false), loadHistory()]);
      await loadSnapshot(true);
    } catch (error) {
      toast(error.message, true);
    } finally {
      busy = false; button.textContent = 'Reclaim selected storage'; updateSelected();
    }
  });

  document.getElementById('policy-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (managerFallback || !managerCanModify) return;
    const box = document.getElementById('policy-result');
    box.hidden = false; box.className = 'sm-result'; box.textContent = 'Saving policy…';
    const payload = {
      auto_enabled: document.getElementById('auto-enabled').checked,
      auto_mode: document.getElementById('auto-mode').value,
      minimum_free_gb: Number(document.getElementById('minimum-free').value),
      retention_hours: Number(document.getElementById('retention-hours').value),
      scan_interval_minutes: Number(document.getElementById('scan-interval').value),
      journal_max_mb: Number(document.getElementById('journal-max').value),
      temporary_retention_days: Number(document.getElementById('temp-days').value),
      auto_categories: [...document.querySelectorAll('#auto-categories input:checked')].map(x => x.value)
    };
    try {
      const result = await api('/api/admin/storage/policy', {method: 'POST', body: JSON.stringify(payload)});
      renderPolicy(result.policy); box.className = 'sm-result success'; box.textContent = 'Automatic cleanup policy saved.';
    } catch (error) {
      box.className = 'sm-result error'; box.textContent = error.message;
    }
  });

  // STORAGE_MANAGER_FULL_PRODUCTION_CLEAN_V174
  const fullCleanButton = document.getElementById('full-production-clean');
  let fullCleanPollTimer = null;
  let fullCleanWasRunning = false;

  function scheduleFullCleanPoll(delay = 1800) {
    if (fullCleanPollTimer) window.clearTimeout(fullCleanPollTimer);
    fullCleanPollTimer = window.setTimeout(() => loadFullCleanStatus(true), delay);
  }

  function renderFullCleanStatus(data, fromPoll = false) {
    if (!fullCleanButton) return;
    const state = String((data && data.status) || 'never_run');
    const running = Boolean(data && data.running) || state === 'queued' || state === 'running' || state === 'verifying';
    fullCleanButton.disabled = managerFallback || !managerCanModify || running;
    fullCleanButton.textContent = running ? (state === 'verifying' ? 'Verifying clean…' : 'Production clean running…') : 'Full Production Clean';
    if (running) {
      fullCleanWasRunning = true;
      scheduleFullCleanPoll(1800);
      return;
    }
    if (fullCleanPollTimer) { window.clearTimeout(fullCleanPollTimer); fullCleanPollTimer = null; }
    if (fromPoll && fullCleanWasRunning && state === 'completed') {
      const reclaimed = bytes(Number(data.reclaimed_bytes || 0));
      toast(`Full production clean verified: ${reclaimed} reclaimed.`);
      fullCleanWasRunning = false;
      Promise.all([loadMetrics(false), loadHistory()]).catch(() => {});
      loadSnapshot(true).catch(() => {});
    } else if (fromPoll && fullCleanWasRunning && state === 'failed') {
      toast(data.message || 'Full production clean failed verification.', true);
      fullCleanWasRunning = false;
    }
  }

  async function loadFullCleanStatus(fromPoll = false) {
    if (!fullCleanButton) return;
    try {
      const data = await api('/api/admin/storage/full-clean/status');
      renderFullCleanStatus(data, fromPoll);
    } catch (error) {
      // The web service is intentionally restarted during a full clean. Keep polling through the outage.
      if (fullCleanWasRunning || fromPoll) scheduleFullCleanPoll(2500);
    }
  }

  if (fullCleanButton) {
    fullCleanButton.addEventListener('click', async () => {
      if (managerFallback || !managerCanModify) return;
      const confirmed = window.confirm(
        'This permanently deletes ALL local production jobs/history/runtime media for Scene Builder, Auto Caption Studio and Algrow, clears production caches, and removes old Scene Builder Docker generations. Current releases, configuration, models and login profiles are preserved. Continue?'
      );
      if (!confirmed) return;
      fullCleanButton.disabled = true;
      fullCleanButton.textContent = 'Starting clean…';
      fullCleanWasRunning = true;
      try {
        const data = await api('/api/admin/storage/full-clean', {
          method: 'POST',
          body: JSON.stringify({confirmation: 'FULL_PRODUCTION_CLEAN'})
        });
        renderFullCleanStatus(data, false);
        toast('Full production clean started. Production services will restart briefly.');
        scheduleFullCleanPoll(700);
      } catch (error) {
        fullCleanWasRunning = false;
        fullCleanButton.disabled = managerFallback || !managerCanModify;
        fullCleanButton.textContent = 'Full Production Clean';
        toast(error.message, true);
      }
    });
  }

  async function initializeStorage() {
    await loadMetrics(false);
    if (!managerFallback) {
      await Promise.all([loadSnapshot(false), loadHistory(), loadFullCleanStatus(false)]);
    }
    initialized = true;
  }
  applyManagerControls();
  initializeStorage();
  window.setInterval(() => loadMetrics(false), 10000);
})();
