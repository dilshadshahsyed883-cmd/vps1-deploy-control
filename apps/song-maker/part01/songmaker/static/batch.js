/* Local-folder batch intake. Reuses the authenticated, idempotent /jobs endpoint,
   so batches never introduce a second renderer, queue, database or Drive path. */
(function () {
  'use strict';
  const byId = id => document.getElementById(id);
  const folder = byId('batchFolder');
  if (!folder) return;
  const addButton = byId('batchAdd');
  const retryButton = byId('batchRetry');
  const cancelButton = byId('batchCancel');
  const summary = byId('batchSummary');
  const list = byId('batchList');
  const overall = byId('batchOverall');
  const overallText = byId('batchOverallText');
  const progressArea = byId('batchProgressArea');
  const MIB = 1024 * 1024;
  const imageExts = new Set(['.jpg', '.jpeg', '.png', '.webp']);
  const audioExts = new Set(['.mp3', '.wav', '.m4a']);
  let pairs = [];
  let ignored = 0;
  let running = false;
  let aborted = false;
  let currentRequest = null;

  function extension(name) {
    const dot = name.lastIndexOf('.');
    return dot < 0 ? '' : name.substring(dot).toLowerCase();
  }
  function matchKey(name) {
    const dot = name.lastIndexOf('.');
    return (dot < 0 ? name : name.substring(0, dot)).normalize('NFC').toLowerCase();
  }
  function make(tag, value, cls) {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = value;
    if (cls) element.className = cls;
    return element;
  }
  function sizeText(size) { return (size / MIB).toFixed(1) + ' MB'; }

  // No whole-file reads in JavaScript. The browser streams each multipart pair
  // while XHR reports real transmitted bytes, not estimated server progress.
  function uploadPair(image, audio, onProgress, onRequest) {
    const data = new FormData();
    data.append('image', image, image.name);
    data.append('audio', audio, audio.name);
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      if (onRequest) onRequest(xhr);
      xhr.open('POST', '/song-maker/api/jobs');
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-Song-Maker', '1');
      xhr.timeout = 30 * 60 * 1000;
      xhr.upload.onprogress = event => {
        if (event.lengthComputable && event.total > 0 && onProgress) {
          onProgress(Math.min(99, Math.floor(event.loaded * 100 / event.total)), event.loaded, event.total);
        }
      };
      xhr.onload = () => {
        let data;
        try { data = JSON.parse(xhr.responseText); } catch (_) { data = {}; }
        if (xhr.status >= 200 && xhr.status < 300) {
          if (onProgress) onProgress(100);
          resolve(data);
        } else {
          reject(new Error(typeof data.detail === 'string' ? data.detail : 'Upload failed (HTTP ' + xhr.status + ')'));
        }
      };
      xhr.onerror = () => reject(new Error('Network error. Check your connection and retry this pair.'));
      xhr.ontimeout = () => reject(new Error('Upload timed out. Retry is safe; existing jobs are deduplicated.'));
      xhr.onabort = () => reject(new Error('Upload cancelled'));
      xhr.send(data);
    });
  }
  window.songMakerUploadPair = uploadPair;

  function stateLabel(pair, label) {
    pair.status.textContent = label;
  }
  function buttons() {
    const hasReady = pairs.some(pair => pair.state === 'Ready');
    const hasFailed = pairs.some(pair => pair.state === 'Failed');
    addButton.disabled = running || !hasReady;
    retryButton.disabled = running || !hasFailed;
    cancelButton.disabled = !running;
    folder.disabled = running;
  }
  function display() {
    list.replaceChildren();
    progressArea.hidden = pairs.length === 0;
    overall.value = 0;
    overallText.textContent = '';
    let ready = 0, conflicts = 0, missing = 0, tooLarge = 0;
    for (const pair of pairs) {
      if (pair.state === 'Ready') ready++;
      if (pair.state === 'Conflict') conflicts++;
      if (pair.state === 'Missing') missing++;
      if (pair.state === 'Too large') tooLarge++;
      const line = make('div', undefined, 'batchPair');
      const files = make('div', undefined, 'batchPairFiles');
      files.append(
        make('strong', pair.name),
        make('small', (pair.images.map(f => f.name).join(', ') || 'No image') +
          '  |  ' + (pair.audio.map(f => f.name).join(', ') || 'No audio'))
      );
      const info = make('div', undefined, 'batchPairProgress');
      const bar = make('progress');
      bar.max = 100;
      bar.value = 0;
      bar.setAttribute('aria-label', 'Upload progress for ' + pair.name);
      const status = make('small', pair.state);
      info.append(bar, status);
      line.append(files, info);
      list.append(line);
      pair.bar = bar;
      pair.status = status;
    }
    summary.textContent = ready + ' matched pairs · ' + missing + ' unmatched · ' +
      conflicts + ' conflicts · ' + tooLarge + ' over size limit' +
      (ignored ? ' · ' + ignored + ' other files ignored' : '') +
      '. Only valid one-image/one-audio pairs will be queued.';
    buttons();
  }
  function prepare(files) {
    const grouped = new Map();
    ignored = 0;
    for (const file of files) {
      const ext = extension(file.name);
      const kind = imageExts.has(ext) ? 'images' : audioExts.has(ext) ? 'audio' : null;
      if (!kind) { ignored++; continue; }
      const key = matchKey(file.name);
      if (!grouped.has(key)) grouped.set(key, {name: file.name.substring(0, file.name.length - ext.length), images: [], audio: []});
      grouped.get(key)[kind].push(file);
    }
    pairs = [...grouped.values()].sort((a, b) => a.name.localeCompare(b.name)).map(group => {
      const valid = group.images.length === 1 && group.audio.length === 1;
      let state = 'Ready';
      if (group.images.length > 1 || group.audio.length > 1) state = 'Conflict';
      else if (!valid) state = 'Missing';
      else if (group.images[0].size > 50 * MIB || group.audio[0].size > 1024 * MIB ||
               group.images[0].size + group.audio[0].size > 1100 * MIB) state = 'Too large';
      return {...group, state, bar: null, status: null, jobId: null};
    });
    display();
  }
  folder.addEventListener('change', () => {
    if (running) return;
    prepare([...folder.files]);
  });

  async function submit(target) {
    if (running || !target.length) return;
    running = true;
    aborted = false;
    buttons();
    const total = target.reduce((sum, pair) => sum + pair.images[0].size + pair.audio[0].size, 0);
    let finishedBytes = 0;
    let sent = 0, failed = 0;
    for (const pair of target) {
      if (aborted) break;
      const weight = pair.images[0].size + pair.audio[0].size;
      pair.state = 'Uploading';
      pair.bar.value = 0;
      stateLabel(pair, 'Uploading…');
      const update = (percent, loaded, uploadTotal) => {
        pair.bar.value = percent;
        stateLabel(pair, percent === 100 ? 'Queued' : percent >= 99 ? 'Uploaded · validating…' : 'Uploading ' + percent + '%');
        const currentBytes = percent === 100 ? weight :
          (Number.isFinite(loaded) && uploadTotal ? weight * loaded / uploadTotal : weight * percent / 100);
        overall.value = total ? Math.min(100, Math.floor(100 * (finishedBytes + currentBytes) / total)) : 100;
        overallText.textContent = (sent + failed) + '/' + target.length + ' processed · ' +
          sizeText(finishedBytes + currentBytes) + ' / ' + sizeText(total) + ' transferred/processed';
      };
      try {
        const job = await uploadPair(pair.images[0], pair.audio[0], update, xhr => { currentRequest = xhr; });
        pair.state = 'Queued';
        pair.jobId = job.id;
        pair.bar.value = 100;
        stateLabel(pair, 'Queued · ' + (job.local_name || pair.name + '.mp4'));
        sent++;
      } catch (error) {
        pair.state = aborted ? 'Cancelled' : 'Failed';
        stateLabel(pair, pair.state + ' · ' + error.message);
        if (!aborted) failed++;
      } finally {
        currentRequest = null;
        finishedBytes += weight;
        overall.value = total ? Math.min(100, Math.round(100 * finishedBytes / total)) : 100;
        overallText.textContent = (sent + failed) + '/' + target.length + ' processed · ' +
          sent + ' queued · ' + failed + ' failed' + (aborted ? ' · cancelled' : '');
        buttons();
      }
    }
    running = false;
    if (aborted) {
      for (const pair of target) if (pair.state === 'Ready') stateLabel(pair, 'Not uploaded (cancelled)');
    }
    buttons();
    summary.textContent = sent + ' queued · ' + failed + ' failed' +
      (aborted ? ' · remaining uploads cancelled' : '') +
      '. Existing Song Maker queue handles rendering and Drive delivery.';
    // Refresh safely without changing the user's current tab or clearing the folder.
    if (typeof refresh === 'function') await refresh();
  }
  addButton.addEventListener('click', () => submit(pairs.filter(pair => pair.state === 'Ready')));
  retryButton.addEventListener('click', () => submit(pairs.filter(pair => pair.state === 'Failed')));
  cancelButton.addEventListener('click', () => {
    aborted = true;
    if (currentRequest) currentRequest.abort();
  });
  buttons();
})();
