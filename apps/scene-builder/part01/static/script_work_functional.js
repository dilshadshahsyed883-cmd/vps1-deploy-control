(function () {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const app = $('#script-work-app');
  if (!app) return;
  const esc = value => String(value == null ? '' : value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  let toastTimer;
  const toast = message => {
    const node = $('#sw-toast');
    if (!node) return;
    clearTimeout(toastTimer);
    node.textContent = message;
    node.classList.add('show');
    toastTimer = setTimeout(() => node.classList.remove('show'), 4000);
  };
  const api = async (url, options = {}) => {
    const headers = {...(options.headers || {})};
    if (options.body && !(options.body instanceof Blob) && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const response = await fetch(url, {credentials: 'same-origin', ...options, headers});
    const contentType = response.headers.get('content-type') || '';
    const data = contentType.includes('json') ? await response.json().catch(() => ({})) : {};
    if (!response.ok) throw new Error(data.detail || data.message || `Request failed (${response.status})`);
    return data;
  };
  const mode = new URLSearchParams(location.search).get('tab') === 'processor' ? 'processor' : 'extractor';
  const extractorPanel = $('[data-sw-panel="extractor"]');
  const processorPanel = $('[data-sw-panel="processor"]');
  if (extractorPanel && processorPanel) {
    extractorPanel.hidden = mode !== 'extractor';
    processorPanel.hidden = mode !== 'processor';
  }

  function initExtractor() {
    if (mode !== 'extractor' || !extractorPanel) return;
    let selected = '';
    let selectedBatch = '';
    let browserLoaded = false;
    let state = {groups: [], batches: [], selected_group: null, selected_batch: null, jobs: [], counts: {}};
    const groupSelect = $('#sw-group-select');
    const docBatch = $('#sw-doc-batch');
    const linkInput = $('#sw-link-input');
    const docInput = $('#sw-doc-link');

    $$('[data-extractor-tab]').forEach(button => button.addEventListener('click', () => {
      const name = button.dataset.extractorTab;
      $$('[data-extractor-tab]').forEach(node => node.classList.toggle('active', node === button));
      $$('[data-extractor-view]').forEach(view => {
        const active = view.dataset.extractorView === name;
        view.hidden = !active;
        view.classList.toggle('active', active);
      });
      if (name === 'browser' && !browserLoaded) {
        const frame = $('#sw-browser-frame');
        if (frame?.dataset.src) {
          frame.src = frame.dataset.src;
          browserLoaded = true;
        }
      }
    }));

    const render = () => {
      const groups = state.groups || [];
      if (!selected || !groups.some(group => group.name === selected)) {
        selected = state.selected_group?.name || groups[0]?.name || '';
      }
      if (groupSelect) groupSelect.innerHTML = '<option value="">Select Group</option>' + groups.map(group =>
        `<option value="${esc(group.name)}" ${group.name === selected ? 'selected' : ''}>${esc(group.name)} (${Number(group.batch_count || 0)} batches)</option>`
      ).join('');
      const batches = state.batches || [];
      if (!selectedBatch || !batches.some(batch => batch.batch_id === selectedBatch)) {
        selectedBatch = state.selected_batch?.batch_id || batches[0]?.batch_id || '';
      }
      if (docBatch) docBatch.innerHTML = '<option value="">Select Batch</option>' + batches.map(batch =>
        `<option value="${esc(batch.batch_id)}" ${batch.batch_id === selectedBatch ? 'selected' : ''}>${esc(batch.batch_id)}</option>`
      ).join('');
      const table = $('#sw-link-table');
      if (table) {
        table.innerHTML = batches.length ? batches.map(batch => `
          <tr>
            <td><strong>${esc(batch.batch_id)}</strong><small>${esc(batch.group_name)}</small></td>
            <td><strong>${Number(batch.link_count || 0)}</strong></td>
            <td>${batch.document_url ? `<a href="${esc(batch.document_url)}" target="_blank" rel="noopener">Extracted</a>` : '<span class="sw-pill">Not ready</span>'}${batch.links_document_url ? ` · <a href="${esc(batch.links_document_url)}" target="_blank" rel="noopener">Links</a>` : ''}</td>
            <td><span class="sw-pill">${esc(batch.status || 'ready')}</span></td>
            <td><div class="sw-group-actions"><button type="button" data-batch-select="${esc(batch.batch_id)}">Select</button></div></td>
          </tr>`).join('') : '<tr class="empty-row"><td colspan="5">No Batches created for this Group.</td></tr>';
      }
      const batch = batches.find(item => item.batch_id === selectedBatch) || state.selected_batch;
      if (batch && docInput && !docInput.matches(':focus')) docInput.value = batch.document_url || batch.document_id || '';
      $('#sw-assigned-batch').textContent = batch?.batch_id || 'Not selected';
      $('#sw-assigned-doc').textContent = batch?.document_title || 'Not assigned';
      ['pending', 'processing', 'completed', 'failed'].forEach(key => {
        const node = $(`#sw-count-${key}`);
        if (node) node.textContent = Number(state.counts?.[key] || 0);
      });
      const status = $('#sw-extractor-status');
      if (status) {
        const failure = state.jobs?.find(job => job.status === 'failed');
        const batchStatus = batch?.status || 'ready';
        const control = batch?.control || 'ready';
        const label = batchStatus === 'completed' ? 'Completed' : control.charAt(0).toUpperCase() + control.slice(1);
        status.querySelector('strong').textContent = failure ? 'Failed item needs review' : label;
        status.querySelector('small').textContent = failure ? `${failure.permanent_id}: ${failure.error}` : `${state.jobs?.length || 0} persistent job(s) in this Batch.`;
      }
    };
    const load = async () => {
      try {
        state = await api(`/api/script-work/state?group=${encodeURIComponent(selected)}&batch=${encodeURIComponent(selectedBatch)}`);
        selected = state.selected_group?.name || selected;
        selectedBatch = state.selected_batch?.batch_id || selectedBatch;
        render();
      } catch (error) {
        toast(error.message);
      }
    };
    const groupAction = async path => {
      if (!selectedBatch) return toast('Select or create a Batch first.');
      try {
        state = await api(`/api/script-work/batches/${encodeURIComponent(selectedBatch)}${path}`, {method: 'POST'});
        render();
      } catch (error) {
        toast(error.message);
      }
    };

    $('#sw-add-group')?.addEventListener('click', async () => {
      const name = prompt('New group name');
      if (!name?.trim()) return;
      try {
        state = await api('/api/script-work/groups', {method: 'POST', body: JSON.stringify({name: name.trim()})});
        selected = state.selected_group?.name || name.trim();
        selectedBatch = '';
        render();
        toast('Group created.');
      } catch (error) { toast(error.message); }
    });
    $('#sw-remove-group')?.addEventListener('click', async () => {
      if (!selected) return toast('Select a group first.');
      if (!confirm(`Remove group ${selected} and its queued jobs?`)) return;
      try {
        state = await api(`/api/script-work/groups/${encodeURIComponent(selected)}`, {method: 'DELETE'});
        selected = '';
        selectedBatch = '';
        render();
        toast('Group removed.');
      } catch (error) { toast(error.message); }
    });
    $('#sw-save-links')?.addEventListener('click', async () => {
      if (!selected) return toast('Select or create a group first.');
      try {
        state = await api(`/api/script-work/groups/${encodeURIComponent(selected)}/links`, {method: 'POST', body: JSON.stringify({text: linkInput?.value || ''})});
        selectedBatch = state.created_batch_id || state.selected_batch?.batch_id || '';
        if (linkInput) linkInput.value = '';
        render();
        const invalid = state.invalid_links?.length || 0;
        toast(`${state.saved_links || 0} unique link(s) saved${invalid ? `; ${invalid} invalid line(s) ignored` : ''}.`);
      } catch (error) { toast(error.message); }
    });
    $('#sw-clear-links')?.addEventListener('click', () => { if (linkInput) linkInput.value = ''; });
    groupSelect?.addEventListener('change', () => { selected = groupSelect.value; selectedBatch = ''; load(); });
    docBatch?.addEventListener('change', () => { selectedBatch = docBatch.value; load(); });
    $('#sw-link-table')?.addEventListener('click', async event => {
      const choose = event.target.closest('[data-batch-select]');
      if (choose) {
        selectedBatch = choose.dataset.batchSelect;
        await load();
      }
    });
    $('#sw-create-doc')?.addEventListener('click', async () => {
      if (!selectedBatch) return toast('Select a Batch first.');
      try {
        state = await api(`/api/script-work/batches/${encodeURIComponent(selectedBatch)}/document/create`, {method: 'POST'});
        render();
        toast('Extracted Batch document is ready.');
      } catch (error) { toast(error.message); }
    });
    $('#sw-validate-doc')?.addEventListener('click', async () => {
      if (!selectedBatch) return toast('Select a Batch first.');
      try {
        const result = await api(`/api/script-work/batches/${encodeURIComponent(selectedBatch)}/document/validate`, {method: 'POST', body: JSON.stringify({document: docInput?.value || ''})});
        toast(`Access verified: ${result.title} (${result.tabs.length} tab(s))`);
      } catch (error) { toast(error.message); }
    });
    $('#sw-save-doc')?.addEventListener('click', async () => {
      if (!selectedBatch) return toast('Select a Batch first.');
      try {
        state = await api(`/api/script-work/batches/${encodeURIComponent(selectedBatch)}/document`, {method: 'POST', body: JSON.stringify({document: docInput?.value || ''})});
        render();
        toast('Document assigned.');
      } catch (error) { toast(error.message); }
    });
    $('#sw-start')?.addEventListener('click', () => groupAction('/start'));
    $('#sw-pause')?.addEventListener('click', () => groupAction('/pause'));
    $('#sw-resume')?.addEventListener('click', () => groupAction('/resume'));
    $('#sw-stop')?.addEventListener('click', () => groupAction('/stop'));
    $('#sw-retry')?.addEventListener('click', () => groupAction('/retry-failed'));
    $('#sw-browser-refresh')?.addEventListener('click', async () => {
      try {
        await api('/api/script-work/browser/refresh', {method: 'POST'});
        const frame = $('#sw-browser-frame');
        if (frame?.dataset.src) {
          frame.src = frame.dataset.src + '&t=' + Date.now();
          browserLoaded = true;
        }
        toast('Browser page refreshed.');
      } catch (error) { toast(error.message); }
    });
    $('#sw-browser-fullscreen')?.addEventListener('click', async () => {
      try { await $('#sw-browser-stage')?.requestFullscreen(); } catch (error) { toast('Fullscreen is blocked by this browser.'); }
    });
    const pollServices = async () => {
      try {
        const [browser, google] = await Promise.all([
          api('/api/script-work/browser/status'),
          api('/api/script-work/google/status')
        ]);
        const browserStatus = $('#sw-browser-status');
        if (browserStatus) {
          browserStatus.textContent = browser.state === 'ready' || browser.state === 'extracting'
            ? `Chrome connected · ${browser.current_url || 'ready for use'}`
            : browser.last_error || 'Chrome is starting…';
          browserStatus.classList.toggle('error', browser.state === 'stopped' || browser.state === 'error');
        }
        const googleStatus = $('#sw-google-status');
        if (googleStatus) {
          googleStatus.textContent = google.connected
            ? 'Fixed server Google account is ready.'
            : 'Fixed server Google account is not configured. Document actions are unavailable.';
          googleStatus.classList.toggle('error', !google.connected);
        }
      } catch (_) {}
    };
    load();
    pollServices();
    setInterval(load, 3000);
    setInterval(pollServices, 8000);
  }

  function initProcessor() {
    if (mode !== 'processor' || !processorPanel) return;
    let selected = '';
    let state = {};
    const group = $('#sw-processor-group');
    const fields = {
      batch_id: $('#sw-batch-id'),
      category: $('#sw-category'),
      intro_filename: $('#sw-intro-select'),
      start_serial: $('#sw-start-serial'),
      minimum_size: $('#sw-min-size'),
      minimum_unit: $('#sw-min-unit'),
      maximum_size: $('#sw-max-size'),
      maximum_unit: $('#sw-max-unit'),
      duplicate_check: $('#sw-duplicate'),
      remove_words: $('#sw-remove-words'),
      removal_words: $('#sw-removal-words'),
      skip_failed_scripts: $('#sw-skip-failed'),
      auto_process_extracted_batch: $('#sw-auto-process')
    };
    const status = (title, message, isError = false) => {
      const node = $('#sw-processor-status');
      if (!node) return;
      node.querySelector('strong').textContent = title;
      node.querySelector('small').textContent = message;
      node.classList.toggle('error', isError);
    };
    const render = () => {
      const groups = state.batches || state.groups || [];
      if (!selected || !groups.some(item => item.batch_id === selected)) {
        selected = state.selected_batch?.batch_id || state.selected_group?.batch_id || groups[0]?.batch_id || '';
      }
      if (group) group.innerHTML = '<option value="">Select extracted batch</option>' + groups.map(item =>
        `<option value="${esc(item.batch_id)}" ${item.batch_id === selected ? 'selected' : ''}>${esc(item.batch_id)} (${Number(item.link_count || 0)})</option>`
      ).join('');
      const selectedGroup = groups.find(item => item.batch_id === selected) || state.selected_batch || state.selected_group || {};
      $('#sw-processor-doc').value = selectedGroup.document_url || '';
      const config = state.config || {};
      const categories = state.categories || ['general'];
      fields.category.innerHTML = categories.map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join('');
      const intros = state.intros || [];
      fields.intro_filename.innerHTML = '<option value="">No intro selected</option>' + intros.map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join('');
      Object.entries(fields).forEach(([key, node]) => {
        if (!node || config[key] == null) return;
        if (node.type === 'checkbox') node.checked = Boolean(config[key]);
        else node.value = config[key];
      });
      if (state.output?.ready) {
        status('Processed Doc ready', `${state.output.name} · ${state.output.processed} processed · ${state.output.skipped} skipped`);
      } else {
        status(selected ? 'Ready' : 'Select a batch', selected ? 'Configure the rules, then create the processed Google Doc.' : 'Choose an extracted batch first.');
      }
      $('#sw-download').disabled = !state.output?.ready;
      $('#sw-open-drive').disabled = !state.output?.drive_url;
    };
    const load = async () => {
      try {
        state = await api(`/api/script-work/processor/state?group=${encodeURIComponent(selected)}`);
        render();
      } catch (error) { toast(error.message); }
    };
    const payload = () => ({
      group: selected,
      batch_id: fields.batch_id.value.trim(),
      category: fields.category.value || 'general',
      intro_filename: fields.intro_filename.value || '',
      start_serial: Number(fields.start_serial.value || 1),
      minimum_size: Number(fields.minimum_size.value || 0),
      minimum_unit: fields.minimum_unit.value,
      maximum_size: Number(fields.maximum_size.value || 0),
      maximum_unit: fields.maximum_unit.value,
      duplicate_check: fields.duplicate_check.checked,
      remove_words: fields.remove_words.checked,
      removal_words: fields.removal_words.value,
      skip_failed_scripts: fields.skip_failed_scripts.checked,
      auto_process_extracted_batch: fields.auto_process_extracted_batch.checked
    });
    const save = async () => {
      if (!selected) return;
      state = await api('/api/script-work/processor/config', {method: 'POST', body: JSON.stringify(payload())});
      render();
    };
    group?.addEventListener('change', () => { selected = group.value; load(); });
    Object.values(fields).forEach(node => node?.addEventListener('change', () => save().catch(error => toast(error.message))));
    $('#sw-generate-batch')?.addEventListener('click', () => {
      fields.batch_id.value = selected;
      toast('The original Batch ID follows the scripts automatically.');
    });
    $('#sw-add-category')?.addEventListener('click', async () => {
      const name = prompt('New category name');
      if (!name?.trim()) return;
      try {
        await api('/api/script-work/processor/categories', {method: 'POST', body: JSON.stringify({action: 'add', name: name.trim()})});
        await load();
        fields.category.value = name.trim();
        await save();
      } catch (error) { toast(error.message); }
    });
    $('#sw-delete-category')?.addEventListener('click', async () => {
      const name = fields.category.value;
      if (!name || !confirm(`Delete category "${name}" from the menu? Existing duplicate history is preserved.`)) return;
      try {
        await api('/api/script-work/processor/categories', {method: 'POST', body: JSON.stringify({action: 'delete', name})});
        await load();
      } catch (error) { toast(error.message); }
    });
    const introFile = $('#sw-intro-file');
    $('#sw-add-intro')?.addEventListener('click', () => introFile?.click());
    introFile?.addEventListener('change', async () => {
      const file = introFile.files?.[0];
      if (!file) return;
      try {
        const result = await api('/api/script-work/processor/intros', {
          method: 'POST',
          headers: {'Content-Type': 'text/plain; charset=utf-8', 'X-Filename': file.name},
          body: file
        });
        await load();
        fields.intro_filename.value = result.filename;
        await save();
        toast('Intro saved on the VPS.');
      } catch (error) { toast(error.message); }
      introFile.value = '';
    });
    $('#sw-remove-intro')?.addEventListener('click', async () => {
      const name = fields.intro_filename.value;
      if (!name || !confirm(`Remove intro "${name}"?`)) return;
      try {
        await api(`/api/script-work/processor/intros/${encodeURIComponent(name)}`, {method: 'DELETE'});
        fields.intro_filename.value = '';
        await load();
        await save();
        toast('Intro removed.');
      } catch (error) { toast(error.message); }
    });
    $('#sw-process')?.addEventListener('click', async event => {
      if (!selected) return toast('Select an extracted batch first.');
      const button = event.currentTarget;
      button.disabled = true;
      status('Processing…', 'Reading extracted tabs, cleaning scripts, and updating the processed Google Doc.');
      try {
        state = await api('/api/script-work/processor/process', {method: 'POST', body: JSON.stringify(payload())});
        render();
        toast(`Success: ${state.processed} processed, ${state.skipped} skipped.`);
      } catch (error) {
        status('Processing failed', error.message, true);
        toast(error.message);
      } finally {
        button.disabled = false;
      }
    });
    $('#sw-download')?.addEventListener('click', () => {
      if (!selected || !state.output?.document_url) return toast('No processed Google Doc is ready.');
      window.open(state.output.document_url, '_blank', 'noopener');
    });
    $('#sw-open-drive')?.addEventListener('click', () => {
      if (!state.output?.drive_url) return toast('Processed Scripts folder is not ready.');
      window.open(state.output.drive_url, '_blank', 'noopener');
    });
    load();
  }

  initExtractor();
  initProcessor();
})();
