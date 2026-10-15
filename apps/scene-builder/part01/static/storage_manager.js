(() => {
  const app = document.getElementById('storage-app');
  if (!app) return;
  const csrf = app.dataset.csrf || '';
  let snapshot = null;
  let policy = null;
  let busy = false;
  const startingServices = new Set();
  const serviceErrors = new Map();

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

  // STORAGE_MANAGER_SERVICE_ACTIVATION_JS_V6
  const serviceLabels = {
    'scene-builder-web':'Scene Builder Web','auto-caption-web':'Auto Caption Web','auto-caption-worker':'Auto Caption Worker',
    'auto-caption-renderer':'Auto Caption Renderer','auto-caption-compressor':'Auto Caption Compressor',
    'auto-caption-drive-uploader':'Auto Caption Drive Uploader','algrow-vps-chrome':'Algrow Chrome',
    'algrow-voice-sidecar':'Algrow Voice Sidecar','algrow-vps-viewer':'Algrow Viewer',
    'algrow-vps-x11vnc':'Algrow VNC Backend','algrow-vps-xvfb':'Algrow Virtual Display','vps-storage-manager':'Storage Manager'
  };
  const serviceGroups = {
    'scene-builder-web':'Scene Builder','auto-caption-web':'Auto Caption Studio','auto-caption-worker':'Auto Caption Studio',
    'auto-caption-renderer':'Auto Caption Studio','auto-caption-compressor':'Auto Caption Studio',
    'auto-caption-drive-uploader':'Auto Caption Studio','algrow-vps-chrome':'Algrow','algrow-voice-sidecar':'Algrow',
    'algrow-vps-viewer':'Algrow','algrow-vps-x11vnc':'Algrow','algrow-vps-xvfb':'Algrow','vps-storage-manager':'Infrastructure'
  };

  function serviceStateClass(state) {
    const value = String(state || 'unknown').toLowerCase();
    if (value === 'active') return 'active';
    if (value === 'activating') return 'starting';
    if (value === 'failed') return 'failed';
    return 'inactive';
  }

  function renderServiceHealth(data) {
    const grid = document.getElementById('service-grid');
    if (!grid) return;
    const startable = new Set(data.startable_services || []);
    grid.innerHTML = Object.entries(data.services || {}).map(([key, state]) => {
      const stateClass = serviceStateClass(state);
      const isActive = String(state).toLowerCase() === 'active';
      const isStarting = startingServices.has(key);
      const canStart = startable.has(key) && !isActive;
      const savedError = serviceErrors.get(key) || '';
      const action = canStart ? `<button class="button secondary sm-service-activate" type="button" data-service="${escapeHtml(key)}" ${isStarting ? 'disabled' : ''}>${isStarting ? 'Starting...' : 'Activate'}</button>` : '';
      return `<article class="sm-service sm-service-${stateClass}" data-service-card="${escapeHtml(key)}">
        <div class="sm-service-main"><span class="sm-service-indicator" aria-hidden="true"></span><div class="sm-service-copy"><span class="sm-service-group">${escapeHtml(serviceGroups[key] || 'System')}</span><strong>${escapeHtml(serviceLabels[key] || key)}</strong><small>${escapeHtml(key)}</small></div></div>
        <div class="sm-service-controls"><b class="sm-service-state ${stateClass}">${escapeHtml(isStarting ? 'starting' : state)}</b>${action}</div>
        <div class="sm-service-inline-error" data-service-error="${escapeHtml(key)}" ${savedError ? '' : 'hidden'}>${escapeHtml(savedError)}</div>
      </article>`;
    }).join('');
    grid.querySelectorAll('.sm-service-activate').forEach(button => button.addEventListener('click', () => activateService(button.dataset.service, button)));
  }

  function setServiceInlineError(service, message = '') {
    const safeService = window.CSS && CSS.escape ? CSS.escape(service) : service.replace(/[^a-zA-Z0-9_-]/g, '');
    const node = document.querySelector(`[data-service-error="${safeService}"]`);
    if (!node) return;
    node.hidden = !message;
    node.textContent = message;
  }

  async function activateService(service, button) {
    if (!service || startingServices.has(service)) return;
    startingServices.add(service); serviceErrors.delete(service); setServiceInlineError(service, '');
    if (button) { button.disabled = true; button.textContent = 'Starting...'; }
    try {
      const result = await api(`/api/admin/storage/services/${encodeURIComponent(service)}/start`, {method:'POST', body:'{}'});
      serviceErrors.delete(service);
      toast(result.message || `${serviceLabels[service] || service} is active.`);
      await loadMetrics(false);
    } catch (error) {
      serviceErrors.set(service, error.message);
      setServiceInlineError(service, error.message);
      toast(error.message, true);
      if (button) { button.disabled = false; button.textContent = 'Activate'; }
    } finally {
      startingServices.delete(service);
    }
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
    renderServiceHealth(data);
    document.getElementById('process-list').innerHTML = data.processes.length ? data.processes.map(item => `
      <div class="sm-process"><code>${escapeHtml(item.pid)}</code><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.cpu)}% CPU</span><span>${bytes(Number(item.rss_kb || 0) * 1024)}</span></div>
    `).join('') : '<div class="sm-empty">No process data available.</div>';
  }

  async function loadMetrics(showError = false) {
    try {
      renderMetrics(await api('/api/admin/storage/metrics'));
    } catch (error) {
      const manager = document.getElementById('manager-status');
      manager.className = 'sm-status error';
      manager.innerHTML = '<i></i> Manager unavailable';
      if (showError) toast(error.message, true);
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
    document.getElementById('reclaim-button').disabled = busy || !selected.size || total <= 0;
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
    updateSelected();
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
    busy = true; updateSelected();
    const button = document.getElementById('run-scan');
    button.disabled = true; button.textContent = refresh ? 'Scanning…' : 'Loading…';
    try {
      const data = refresh
        ? await api('/api/admin/storage/scan', {method: 'POST', body: '{}'})
        : await api('/api/admin/storage/snapshot');
      renderSnapshot(data);
    } catch (error) {
      toast(error.message, true);
      document.getElementById('category-grid').innerHTML = `<div class="sm-empty">${escapeHtml(error.message)}</div>`;
    } finally {
      busy = false; button.disabled = false; button.textContent = 'Scan reclaimable space'; updateSelected();
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
    const categories = selectedCategories();
    const total = categories.reduce((sum, key) => sum + Number(snapshot.categories.find(x => x.id === key)?.bytes || 0), 0);
    document.getElementById('confirm-summary').textContent = `${bytes(total)} across ${categories.length} selected categories`;
    confirmText.value = ''; confirmRun.disabled = true; dialog.showModal();
  });
  confirmRun.addEventListener('click', async event => {
    event.preventDefault();
    if (confirmText.value !== 'RECLAIM') return;
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
    fullCleanButton.disabled = running;
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
        fullCleanButton.disabled = false;
        fullCleanButton.textContent = 'Full Production Clean';
        toast(error.message, true);
      }
    });
  }

  Promise.all([loadMetrics(false), loadSnapshot(false), loadHistory(), loadFullCleanStatus(false)]);
  window.setInterval(() => loadMetrics(false), 10000);
})();
