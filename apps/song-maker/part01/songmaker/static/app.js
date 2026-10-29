'use strict';
const $ = id => document.getElementById(id);
const el = (tag, value) => {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = String(value);
  return node;
};
let state = {}, busy = false, imageURL, audioURL;
const notice = message => { $('notice').textContent = message; };
const allowedTabs = ['input', 'pairs', 'queue', 'settingsTab'];

async function api(path, options = {}) {
  const response = await fetch('/song-maker/api/' + path, {
    credentials: 'same-origin', ...options,
    headers: {'X-Song-Maker': '1', ...options.headers}
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || 'Request failed');
  return payload;
}
function tab(name) {
  if (!allowedTabs.includes(name)) name = 'input';
  for (const section of allowedTabs) $(section).hidden = section !== name;
  document.querySelectorAll('[data-tab]').forEach(button =>
    button.setAttribute('aria-selected', String(button.dataset.tab === name)));
  sessionStorage.setItem('song-maker-tab', name);
}
document.querySelectorAll('[data-tab]').forEach(button =>
  button.addEventListener('click', () => tab(button.dataset.tab)));
tab(sessionStorage.getItem('song-maker-tab') || 'input');

function action(label, handler) {
  const button = el('button', label);
  button.type = 'button';
  button.onclick = async () => {
    button.disabled = true;
    try { await handler(); await refresh(); }
    catch (error) { notice(error.message); }
    finally { button.disabled = false; }
  };
  return button;
}
function formatSeconds(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  const seconds = Math.max(0, Math.round(Number(value)));
  const minutes = Math.floor(seconds / 60);
  return minutes ? minutes + 'm ' + String(seconds % 60).padStart(2, '0') + 's' : seconds + 's';
}
function preview(job) {
  if (!job.valid) return;
  const url = '/song-maker/api/jobs/' + encodeURIComponent(job.id) + '/video';
  $('previewTitle').textContent = job.project || job.local_name;
  $('renderedVideo').src = url;
  $('downloadMP4').href = url + '?download=true';
  $('driveMP4').hidden = !job.drive_link;
  if (job.drive_link) $('driveMP4').href = job.drive_link;
  $('previewDialog').showModal();
}

function showJobs() {
  const rows = $('jobs');
  rows.replaceChildren();
  const jobs = state.jobs || [];
  const paused = Boolean((state.queue || {}).paused);
  $('pauseQueue').textContent = paused ? 'Resume queue' : 'Pause pending';
  $('pauseQueue').classList.toggle('primary', paused);
  const active = jobs.filter(j => j.lane).length;
  const pending = jobs.filter(j => ['Queued', 'Ready to Upload'].includes(j.stage)).length;
  const finished = jobs.filter(j => j.stage === 'Completed').length;
  const failed = jobs.filter(j => ['Failed', 'Waiting for Drive', 'Needs Attention'].includes(j.stage)).length;
  const metrics = $('queueMetrics');
  metrics.replaceChildren();
  for (const [key, value] of [
    ['Total', jobs.length], ['Active', active], ['Pending', pending],
    ['Completed', finished], ['Needs Attention', failed],
    ['Queue', paused ? 'Paused' : 'Running']
  ]) metrics.append(el('span', key + ': ' + value));
  $('retryFailed').disabled = !jobs.some(j => ['Failed', 'Cancelled', 'Waiting for Drive', 'Needs Attention'].includes(j.stage) && !j.lane);
  $('cancelPending').disabled = pending === 0;
  $('clearCompleted').disabled = finished === 0;
  for (const job of jobs) {
    const tr = el('tr');
    const media = el('td', job.image_name + ' / ' + job.audio_name);
    for (const [label, value] of [
      ['Project', job.project], ['Media', media],
      ['Duration', formatSeconds(job.duration)], ['Stage', job.stage]
    ]) {
      const td = value instanceof Node ? value : el('td', value || '—');
      td.dataset.label = label;
      tr.append(td);
    }
    const progressCell = el('td'), bar = el('progress');
    bar.max = 100; bar.value = Number(job.progress) || 0;
    bar.setAttribute('aria-label', 'Render progress');
    progressCell.append(bar, el('small', ' ' + (job.progress || 0) + '%'));
    progressCell.dataset.label = 'Progress';
    tr.append(progressCell);
    const rendering = job.stage === 'Rendering';
    const details = [
      ['FPS', job.render_fps != null ? Number(job.render_fps).toFixed(1) : '—'],
      ['Render speed', job.render_speed != null ? Number(job.render_speed).toFixed(2) + 'x' : '—'],
      ['Elapsed', job.render_elapsed != null ? formatSeconds(job.render_elapsed) : '—'],
      ['ETA', rendering ? formatSeconds(job.render_eta) : '—'],
      ['Output', job.remote_name || job.local_name],
      ['Drive', job.drive_status || 'Pending']
    ];
    for (const [label, value] of details) {
      const td = el('td', value || '—'); td.dataset.label = label;
      tr.append(td);
    }
    const buttons = el('td'); buttons.dataset.label = 'Actions';
    buttons.append(action('Details', () => {
      $('detailText').textContent = JSON.stringify(job, null, 2);
      $('details').showModal();
    }));
    if (job.valid) buttons.append(action('Preview / Download', () => preview(job)));
    for (const [label, command] of [['Retry', 'retry'], ['Cancel', 'cancel'], ['Delete', 'delete']]) {
      if (label === 'Retry' && !['Failed', 'Cancelled', 'Waiting for Drive', 'Needs Attention'].includes(job.stage)) continue;
      if (label === 'Cancel' && ['Completed', 'Cancelled'].includes(job.stage)) continue;
      if (label === 'Delete' && (!['Completed', 'Failed', 'Cancelled', 'Waiting for Drive', 'Needs Attention'].includes(job.stage) || job.lane)) continue;
      buttons.append(action(label, async () => {
        if (command === 'delete' && !confirm('Delete local project and MP4? Drive uploads are retained.')) return;
        await api('jobs/' + job.id + '/' + command, {method: 'POST'});
      }));
    }
    tr.append(buttons);
    rows.append(tr);
  }
  if (!jobs.length) {
    const tr = el('tr'), td = el('td', 'Your queued projects will appear here.');
    td.colSpan = 12; tr.append(td); rows.append(tr);
  }
}

function showPairs() {
  const sync = state.sync || {}, pairs = sync.pairs || [];
  const metrics = $('metrics'); metrics.replaceChildren();
  for (const [label, value] of [
    ['Images', sync.images || 0], ['Audio', sync.audio || 0],
    ['Matched', sync.matched || 0],
    ['Waiting for Audio', pairs.filter(p => p.stage === 'Waiting for Audio').length],
    ['Waiting for Image', pairs.filter(p => p.stage === 'Waiting for Image').length],
    ['Conflicts', pairs.filter(p => p.stage.startsWith('Conflict')).length],
    ['Auto-created', pairs.filter(p => p.job_id).length]
  ]) metrics.append(el('span', label + ': ' + value));
  const list = $('pairList'); list.replaceChildren();
  for (const pair of pairs) {
    const line = el('div');
    line.append(el('p', pair.key + ' · ' + pair.stage + (pair.job_stage ? ' · ' + pair.job_stage : '')),
                el('p', 'Image: ' + pair.images.map(x => x.Name).join(', ') +
                  ' | Audio: ' + pair.audio.map(x => x.Name).join(', ')));
    if (pair.stage.startsWith('Conflict') && pair.images.length && pair.audio.length) {
      const image = el('select'), audio = el('select');
      image.setAttribute('aria-label', 'Choose image for ' + pair.key);
      audio.setAttribute('aria-label', 'Choose audio for ' + pair.key);
      for (const [selector, items] of [[image, pair.images], [audio, pair.audio]]) {
        for (const item of items) {
          const opt = el('option', item.Name); opt.value = item.ID;
          selector.append(opt);
        }
      }
      line.append(image, audio, action('Queue selected pair', () =>
        api('pairs/select', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({key: pair.key, image_id: image.value, audio_id: audio.value})})));
    }
    list.append(line);
  }
  if (!pairs.length) list.append(el('p', 'Run a Drive sync to detect matching filenames.'));
}

async function refresh() {
  if (busy || document.hidden) return;
  try {
    state = await api('state');
    showJobs(); showPairs();
    const sync = state.sync || {}, settings = state.settings || {};
    $('syncStatus').textContent =
      (settings.auto_sync ? 'Automatic sync on' : 'Automatic sync off') +
      (settings.sync_requested ? ' · Scan queued' : '') +
      ' · Last scan: ' + (sync.last_sync ?
        new Date(sync.last_sync * 1000).toLocaleString() : 'Not yet scanned') +
      (sync.error ? ' · ' + sync.error : '');
    $('folderInfo').textContent = settings.output_folder ?
      'Drive output folder: ' + settings.output_folder : 'Configure a Drive output folder in Settings.';
    $('syncNow').disabled = !state.sync_access;
    $('adminSettings').hidden = !state.admin;
  } catch (error) { notice(error.message); }
}

$('manual').onsubmit = async event => {
  event.preventDefault(); busy = true;
  const button = event.target.querySelector('button[type="submit"]'); button.disabled = true;
  try {
    const job = await api('jobs', {method: 'POST', body: new FormData(event.target)});
    notice('Queued: ' + job.local_name); tab('queue');
  } catch (error) { notice(error.message); }
  finally { busy = false; button.disabled = false; await refresh(); }
};
const imageInput = $('manual').elements.image, audioInput = $('manual').elements.audio;
imageInput.onchange = () => {
  if (imageURL) URL.revokeObjectURL(imageURL);
  const file = imageInput.files[0]; $('preview').hidden = !file;
  if (file) $('preview').src = imageURL = URL.createObjectURL(file);
};
audioInput.onchange = () => {
  if (audioURL) URL.revokeObjectURL(audioURL);
  const file = audioInput.files[0]; if (!file) return;
  const sound = new Audio(audioURL = URL.createObjectURL(file));
  $('audioInfo').textContent = file.name;
  sound.onloadedmetadata = () => {
    if (Number.isFinite(sound.duration))
      $('audioInfo').textContent = file.name + ' · ' + sound.duration.toFixed(2) + ' seconds';
  };
};

$('exportSettings').onsubmit = async event => {
  event.preventDefault();
  try {
    const data = Object.fromEntries(new FormData(event.target));
    await api('export', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)});
    notice('Export defaults saved. New queued jobs will use these settings.');
    await refresh();
  } catch (error) { notice(error.message); }
};
$('settings').onsubmit = async event => {
  event.preventDefault();
  try {
    const data = Object.fromEntries(new FormData(event.target));
    data.auto_sync = event.target.elements.auto_sync.checked;
    await api('settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)});
    notice('Drive settings saved'); await refresh();
  } catch (error) { notice(error.message); }
};
$('syncNow').onclick = async () => {
  const button = $('syncNow'); button.disabled = true;
  try {
    await api('sync', {method: 'POST'});
    notice('Drive scan requested. Matching runs in the background, including after closing this page.');
    await refresh();
  } catch (error) { notice(error.message); }
  finally { button.disabled = !state.sync_access; }
};
$('pauseQueue').onclick = async () => {
  try {
    const command = (state.queue || {}).paused ? 'resume' : 'pause';
    await api('queue/' + command, {method: 'POST'});
    notice(command === 'pause' ? 'Pending queue paused; running jobs continue.' : 'Queue resumed.');
    await refresh();
  } catch (error) { notice(error.message); }
};
for (const [id, command, message] of [
  ['retryFailed', 'retry-failed', 'Retry eligible failed jobs?'],
  ['cancelPending', 'cancel-pending', 'Cancel all pending jobs? Active jobs are unchanged.'],
  ['clearCompleted', 'clear-completed', 'Delete ALL completed local MP4s? Uploaded Drive files stay intact.']
]) {
  $(id).onclick = async () => {
    if (!confirm(message)) return;
    try {
      const result = await api('queue/' + command, {method: 'POST'});
      notice(result.count + ' jobs updated.'); await refresh();
    } catch (error) { notice(error.message); }
  };
}
$('closeDetails').onclick = () => $('details').close();
$('closePreview').onclick = () => {
  $('renderedVideo').pause(); $('renderedVideo').removeAttribute('src');
  $('renderedVideo').load(); $('previewDialog').close();
};
async function start() {
  await refresh();
  const form = $('exportSettings');
  for (const [key, value] of Object.entries(state.export || {}))
    if (form.elements[key]) form.elements[key].value = value;
  if (state.admin) {
    try {
      const profiles = await api('profiles'), driveForm = $('settings');
      for (const name of profiles) {
        const option = el('option', name); option.value = name;
        driveForm.elements.remote.append(option);
      }
      for (const [key, value] of Object.entries(state.settings || {})) {
        if (!driveForm.elements[key]) continue;
        if (key === 'auto_sync') driveForm.elements[key].checked = value;
        else driveForm.elements[key].value = value;
      }
    } catch (error) { notice(error.message); }
  }
}
start();
setInterval(refresh, 4000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
