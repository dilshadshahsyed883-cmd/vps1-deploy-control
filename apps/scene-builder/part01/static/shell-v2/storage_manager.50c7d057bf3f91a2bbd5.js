export const CONTRACT = 2;
/* CW-025: Storage Manager read-only active cleanup dashboard.
 * Independent deployment GC and app-owned Drive cleanup never share a delete control.
 * Status is read-only. One admin-only CSRF-protected action queues the guarded systemd cleaner.
 */
export function mount(root, context = {}) {
  if (!root) throw new Error('Storage root required');
  const lifetime = new AbortController();
  const get = id => root.querySelector('#' + id);
  const fetch = (url, options = {}) => window.fetch(url, {...options, signal:lifetime.signal});
  const bytes = value => {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'Unavailable';
    let n = value;
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let pos = 0;
    while (n >= 1024 && pos < units.length - 1) { n /= 1024; pos += 1; }
    return (pos ? n.toFixed(n >= 10 ? 1 : 2) : Math.round(n)) + ' ' + units[pos];
  };
  const presentTime = value => {
    if (value === null || value === undefined || value === '' || value === 'n/a') return 'Unavailable';
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime())
      ? parsed.toLocaleString('en-GB', {dateStyle: 'medium', timeStyle: 'short', timeZone:'Asia/Karachi'})+' PKT'
      : String(value);
  };
  const text = (id, value) => { const node = get(id); if (node) node.textContent = String(value); };
  const bar = (id, value) => {
    const node = get(id);
    if (node) node.style.width = Math.min(100, Math.max(0, Number(value) || 0)) + '%';
  };
  async function api(path) {
    const response = await fetch(path, {method:'GET', cache:'no-store',
      credentials:'same-origin', headers:{Accept:'application/json'}});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    if (!data || data.ok !== true) throw new Error('Invalid read-only response');
    return data;
  }

  function renderMetrics(data, fallback) {
    const vps = fallback ? (data.vps || {}) : data;
    const cpu = fallback ? vps.cpu_percent : vps.cpu?.percent;
    const cores = fallback ? vps.cpu_cores : vps.cpu?.cores;
    const memory = fallback ? vps.ram : vps.memory;
    const disk = vps.disk || {};
    text('cpu-value', cpu == null ? '—' : cpu + '%');
    text('cpu-detail', (cores ?? '—') + ' cores');
    bar('cpu-bar', cpu);
    const ramPercent = fallback ? memory?.percent : memory?.percent;
    text('ram-value', ramPercent == null ? '—' : ramPercent + '%');
    text('ram-detail', fallback
      ? (memory?.used_gb ?? '—') + ' / ' + (memory?.total_gb ?? '—') + ' GB'
      : bytes(memory?.used) + ' / ' + bytes(memory?.total));
    bar('ram-bar', ramPercent);
    text('disk-value', disk.percent == null ? '—' : disk.percent + '%');
    text('disk-detail', fallback
      ? (disk.free_gb ?? '—') + ' GB free / ' + (disk.total_gb ?? '—') + ' GB'
      : bytes(disk.free) + ' free / ' + bytes(disk.total));
    bar('disk-bar', disk.percent);
  }

  async function loadMetrics() {
    try {
      const metrics = await api('/api/admin/storage/metrics');
      renderMetrics(metrics, false);
      text('manager-status', 'Storage Manager connected · read-only dashboard');
    } catch (_) {
      try {
        const res = await fetch('/api/dashboard/v1', {method:'GET',cache:'no-store',
          credentials:'same-origin',headers:{Accept:'application/json'}});
        if (!res.ok) throw new Error('Dashboard unavailable');
        renderMetrics(await res.json(), true);
        text('manager-status', 'Live VPS metrics · Storage Manager unavailable');
      } catch (_) {
        text('manager-status', 'Live metrics unavailable');
      }
    }
  }

  function renderCleanupStatus(info) {
    const active = info && info.status === 'active' && info.active === true && info.enabled === true;
    text('deployment-gc-state', active ? (info.next_run_at?'Active · every 12 hours':'Timer active · next run unknown')
      : info?.status === 'inactive' ? 'Not active' : 'Status unavailable');
    get('deployment-gc-state').className = active ? 'sm-auto-active' : 'sm-auto-attention';
    text('deployment-gc-detail', active
      ? (info.next_run_at?'Uses a 12-hour inactive-release grace; preserves the live release and rollback copies.':'The timer is active, but systemd has not published its next run. Refresh cleanup status to check again.')
      : 'Cannot confirm an active timer from the live host. No app data will be deleted from this page.');
    text('gc-next-run', presentTime(info?.next_run_at));
    if (manualButton) manualButton.disabled = manualRunning || !active || info?.manual_available !== true || info?.manual_pending === true;
    if (!manualRunning && info?.manual_pending === true) text('gc-manual-result', 'Manual cleanup queued; all protected checks remain active.');
    const audit = info?.last_cleanup || null;
    text('gc-last-run', presentTime(audit?.timestamp));
    text('gc-last-result', audit
      ? (audit.deleted_count ?? 0) + ' obsolete release(s); ' + (audit.skipped_count ?? 0) + ' skipped'
      : 'No readable audit record. This does not mean cleanup never ran.');
    const skipLabels = {
      'deployment-active': 'Deployment lock active',
      'control-verification-blocked': 'Control verification blocked',
      'service-inactive': 'Service inactive',
      'process-in-use-or-unverifiable': 'Runtime in use / verification unavailable',
      'revalidation-changed': 'Eligibility changed',
      'revalidation-or-unknown': 'Revalidation / unknown'
    };
    const counts = audit?.skip_reason_counts;
    const summary = counts && typeof counts === 'object' && !Array.isArray(counts)
      ? Object.entries(skipLabels).filter(([key]) => Number.isSafeInteger(counts[key]) && counts[key] > 0)
          .map(([key, label]) => label + ': ' + counts[key]).join(' · ')
      : '';
    text('gc-skip-reasons', !audit ? 'No verified audit available.'
      : summary ? 'Why skipped: ' + summary
      : audit.skipped_count > 0 ? 'Skip reasons unavailable for this run; protections remain active.'
      : 'No skipped candidates.');
    text('gc-last-freed', audit && Number.isFinite(audit.measured_freed_bytes)
      ? bytes(audit.measured_freed_bytes) : 'Unavailable');
    text('gc-eligible', info && Number.isFinite(info.eligible_bytes) ? bytes(info.eligible_bytes) : 'Not measured');
    text('gc-eligible-detail', info && Number.isFinite(info.eligible_bytes)
      ? 'Read-only candidate estimate (' + (Number.isSafeInteger(info.eligible_candidate_count) ? info.eligible_candidate_count + ' items, ' : '') + 'measured ' + presentTime(info.eligible_measured_at) + '). Not a promise of deletable space; live checks can skip candidates.'
      : 'No privileged inventory available from this read-only web service; do not interpret as 0 B.');
  }

  const manualButton = get('gc-manual-clean');
  let manualRunning = false;
  async function requestManualCleanup() {
    if (!manualButton || manualRunning || manualButton.disabled) return;
    if (!window.confirm('Run safe deployment cleanup now? Active releases, rollback copies, media and queues remain protected.')) return;
    const csrf = root.querySelector('#storage-app')?.dataset.csrf || root.dataset.csrf;
    if (!csrf) { text('gc-manual-result', 'Admin token unavailable. Reload Storage Manager.'); return; }
    manualRunning = true;
    manualButton.disabled = true;
    text('gc-manual-result', 'Sending guarded cleanup request…');
    try {
      const response = await fetch('/api/admin/storage/run-safe-cleanup', {
        method:'POST', credentials:'same-origin', cache:'no-store',
        headers:{'Content-Type':'application/json','Accept':'application/json','X-Storage-CSRF':csrf},
        body:JSON.stringify({confirmation:'RUN SAFE CLEANUP'})});
      const body = await response.json();
      if (!response.ok || body.ok !== true || body.status !== 'queued') throw new Error(body.detail || body.message || 'Request failed');
      text('gc-manual-result', 'Queued safely. Refresh status for audited results; timer stays active.');
      await loadStatus();
    } catch (error) {
      text('gc-manual-result', 'Not started: ' + (error?.message || 'unavailable'));
    } finally {
      manualRunning = false;
    }
  }

  let pending = false;
  async function loadStatus() {
    if (pending) return;
    pending = true;
    const button = get('refresh-cleanup-status');
    if (button) { button.disabled = true; button.textContent = 'Checking…'; }
    try {
      const data = await api('/api/admin/storage/snapshot');
      if (data.read_only !== true || data.partial !== true) throw new Error('Unexpected mutable snapshot');
      renderCleanupStatus(data.deployment_cleanup);
      text('gc-checked', 'Checked ' + new Date().toLocaleTimeString());
      text('reclaim-value', data.deployment_cleanup &&
        Number.isFinite(data.deployment_cleanup.eligible_bytes)
        ? bytes(data.deployment_cleanup.eligible_bytes) : 'Not measured');
      text('reclaim-detail', 'Read-only estimate; final deletion requires live rechecks. Project data excluded.');
    } catch (_) {
      renderCleanupStatus(null);
      text('gc-checked', 'Status unavailable');
      text('reclaim-value', 'Unavailable');
      text('reclaim-detail', 'No authorized reclaim inventory');
    } finally {
      pending = false;
      if (button) { button.disabled = false; button.textContent = 'Refresh cleanup status'; }
    }
  }
  get('refresh-metrics')?.addEventListener('click', () => { loadMetrics(); loadStatus(); }, {signal:lifetime.signal});
  get('refresh-cleanup-status')?.addEventListener('click', loadStatus, {signal:lifetime.signal});
  manualButton?.addEventListener('click', requestManualCleanup, {signal:lifetime.signal});
  loadMetrics();
  loadStatus();
  const timers = [window.setInterval(() => {if (!document.hidden) loadMetrics();}, 30000),
    window.setInterval(() => {if (!document.hidden) loadStatus();}, 60000)];
  return {unmount() { lifetime.abort(); timers.forEach(window.clearInterval); }, dispose() {}};
}

// Legacy document boundary; the persistent adapter mounts its own shadow root.
if (!document.getElementById('vps1-shell-outlet')) {
  const root = document.getElementById('storage-app');
  if (root) {
    const view = mount(root);
    window.addEventListener('pagehide', () => view.unmount(), {once:true});
  }
}
