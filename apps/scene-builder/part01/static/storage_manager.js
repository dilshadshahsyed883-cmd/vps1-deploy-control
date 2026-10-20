/* CW-025: Storage Manager read-only active cleanup dashboard.
 * Independent deployment GC and app-owned Drive cleanup never share a delete control.
 * All network operations in this view are GETs.
 */
(() => {
  'use strict';
  const root = document.getElementById('storage-app');
  if (!root) return;
  const get = id => document.getElementById(id);
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
      ? parsed.toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'})
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
    text('deployment-gc-state', active ? 'Active · every 12 hours'
      : info?.status === 'inactive' ? 'Not active' : 'Status unavailable');
    get('deployment-gc-state').className = active ? 'sm-auto-active' : 'sm-auto-attention';
    text('deployment-gc-detail', active
      ? 'Uses a 12-hour inactive-release grace; preserves the live release and rollback copies.'
      : 'Cannot confirm an active timer from the live host. No app data will be deleted from this page.');
    text('gc-next-run', presentTime(info?.next_run_at));
    const audit = info?.last_cleanup || null;
    text('gc-last-run', presentTime(audit?.timestamp));
    text('gc-last-result', audit
      ? (audit.deleted_count ?? 0) + ' obsolete release(s); ' + (audit.skipped_count ?? 0) + ' skipped'
      : 'No readable audit record. This does not mean cleanup never ran.');
    text('gc-last-freed', audit && Number.isFinite(audit.measured_freed_bytes)
      ? bytes(audit.measured_freed_bytes) : 'Unavailable');
    text('gc-eligible', info && Number.isFinite(info.eligible_bytes) ? bytes(info.eligible_bytes) : 'Not measured');
    text('gc-eligible-detail', info && Number.isFinite(info.eligible_bytes)
      ? 'Last verified deployment-only scan: ' + presentTime(info.eligible_measured_at) + '. Amount may change after new releases.'
      : 'No privileged inventory available from this read-only web service; do not interpret as 0 B.');
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
      text('reclaim-detail', 'GitHub deployment artifacts only; project data is excluded');
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
  get('refresh-metrics')?.addEventListener('click', () => { loadMetrics(); loadStatus(); });
  get('refresh-cleanup-status')?.addEventListener('click', loadStatus);
  loadMetrics();
  loadStatus();
  window.setInterval(loadMetrics, 30000);
  window.setInterval(loadStatus, 60000);
})();
