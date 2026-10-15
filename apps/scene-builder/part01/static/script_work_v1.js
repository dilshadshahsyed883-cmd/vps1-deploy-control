(() => {
  const app = document.getElementById('script-work-app');
  if (!app) return;
  const $ = selector => app.querySelector(selector);
  const $$ = selector => [...app.querySelectorAll(selector)];
  const toast = message => {
    const el = $('#sw-toast'); el.textContent = message; el.classList.add('show');
    clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 2600);
  };

  const requestedStoryTab = new URLSearchParams(window.location.search).get('tab');
  const storyTab = requestedStoryTab === 'processor' ? 'processor' : 'extractor';
  $$('[data-sw-panel]').forEach(panel => {
    const on = panel.dataset.swPanel === storyTab;
    panel.classList.toggle('active', on);
    panel.hidden = !on;
  });

  $$('[data-extractor-tab]').forEach(button => button.addEventListener('click', () => {
    const name = button.dataset.extractorTab;
    $$('[data-extractor-tab]').forEach(item => item.classList.toggle('active', item === button));
    $$('[data-extractor-view]').forEach(view => { const on = view.dataset.extractorView === name; view.classList.toggle('active', on); view.hidden = !on; });
  }));

  let groups = [{name: 'Urdu-1', links: [], doc: ''}];
  const groupSelect = $('#sw-group-select');
  const docBatch = $('#sw-doc-batch');
  const renderGroups = () => {
    const selected = groupSelect.value;
    const options = '<option value="">Select group</option>' + groups.map(group => `<option${group.name === selected ? ' selected' : ''}>${escapeHtml(group.name)}</option>`).join('');
    groupSelect.innerHTML = options;
    docBatch.innerHTML = '<option value="">Select batch from Step 1</option>' + groups.map(group => `<option>${escapeHtml(group.name)}</option>`).join('');
  };
  const escapeHtml = value => String(value).replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
  const renderLinks = () => {
    const body = $('#sw-link-table');
    if (!groups.length) body.innerHTML = '<tr class="empty-row"><td colspan="5">No YouTube groups created.</td></tr>';
    else body.innerHTML = groups.map((group, index) => `<tr><td><strong>${escapeHtml(group.name)}</strong><small>${group.links.length ? escapeHtml(group.links[0]) + (group.links.length > 1 ? ` +${group.links.length - 1} more` : '') : 'No links saved'}</small></td><td><strong>${group.links.length}</strong></td><td>${group.doc ? '<span class="sw-pill">Assigned</span>' : '<span class="sw-pill">Not assigned</span>'}</td><td><span class="sw-pill">${group.links.length ? 'Pending' : 'Setup'}</span></td><td><div class="sw-group-actions"><button type="button" data-group-select="${index}">Select</button><button type="button" data-group-clear="${index}">Clear Links</button><button class="danger" type="button" data-group-remove="${index}">Remove</button></div></td></tr>`).join('');
    $('#sw-count-pending').textContent = groups.reduce((total, group) => total + group.links.length, 0);
  };

  $('#sw-add-group').addEventListener('click', () => {
    const name = prompt('Enter a new group name');
    if (!name || !name.trim()) return;
    const clean = name.trim();
    if (groups.some(item => item.name.toLowerCase() === clean.toLowerCase())) return toast('That group already exists.');
    groups.push({name: clean, links: [], doc: ''}); renderGroups(); groupSelect.value = clean; docBatch.value = clean; renderLinks(); toast(`Group ${clean} added.`);
  });
  $('#sw-remove-group').addEventListener('click', () => {
    const name = groupSelect.value; if (!name) return toast('Select a group first.');
    if (!confirm(`Remove ${name} from this preview?`)) return;
    groups = groups.filter(item => item.name !== name); renderGroups(); renderLinks(); $('#sw-link-input').value = ''; toast('Group removed.');
  });
  $('#sw-add-links').addEventListener('click', () => {
    const group = groupSelect.value; if (!group) return toast('Select a group first.');
    const raw = $('#sw-link-input').value.split(/\s+/).map(value => value.trim()).filter(Boolean);
    const valid = raw.filter(url => /^(https?:\/\/)?(www\.)?(youtube\.com|youtu\.be)\//i.test(url));
    const batch = groups.find(item => item.name === group); if (!batch) return;
    const unique = [...new Set(valid)]; batch.links = unique;
    renderLinks(); toast(unique.length ? `${unique.length} link${unique.length === 1 ? '' : 's'} saved in ${group}.` : 'No valid YouTube links found.');
  });
  $('#sw-clear-links').addEventListener('click', () => { $('#sw-link-input').value = ''; });
  $('#sw-link-table').addEventListener('click', event => {
    const select = event.target.closest('[data-group-select]');
    const clear = event.target.closest('[data-group-clear]');
    const remove = event.target.closest('[data-group-remove]');
    if (select) { const group = groups[Number(select.dataset.groupSelect)]; groupSelect.value = group.name; docBatch.value = group.name; $('#sw-link-input').value = group.links.join('\n'); toast(`${group.name} selected.`); }
    if (clear) { const group = groups[Number(clear.dataset.groupClear)]; if (confirm(`Clear all links from ${group.name}?`)) { group.links = []; renderLinks(); if (groupSelect.value === group.name) $('#sw-link-input').value = ''; } }
    if (remove) { const group = groups[Number(remove.dataset.groupRemove)]; if (confirm(`Remove group ${group.name}?`)) { groups.splice(Number(remove.dataset.groupRemove), 1); renderGroups(); renderLinks(); } }
  });
  groupSelect.addEventListener('change', () => { const group = groups.find(item => item.name === groupSelect.value); docBatch.value = groupSelect.value; $('#sw-link-input').value = group ? group.links.join('\n') : ''; });
  $('#sw-validate-doc').addEventListener('click', () => {
    const value = $('#sw-doc-link').value.trim(); toast(value.includes('docs.google.com/document/d/') || /^[A-Za-z0-9_-]{20,}$/.test(value) ? 'Document format is valid.' : 'Enter a valid Google Docs link or ID.');
  });
  $('#sw-save-doc').addEventListener('click', () => {
    const batch = docBatch.value, doc = $('#sw-doc-link').value.trim();
    if (!batch || !doc) return toast('Select a batch and enter a Google Doc.');
    const group = groups.find(item => item.name === batch); if (group) group.doc = doc;
    $('#sw-assigned-batch').textContent = batch; $('#sw-assigned-doc').textContent = doc.length > 45 ? doc.slice(0, 42) + '…' : doc; toast('Document assignment saved in this preview.');
    renderLinks();
  });

  $('#sw-browser-refresh').addEventListener('click', () => toast('Dedicated extractor browser refresh control is ready for backend wiring.'));
  $('#sw-browser-fullscreen').addEventListener('click', () => { const stage = $('#sw-browser-stage'); if (stage.requestFullscreen) stage.requestFullscreen(); });
  $('#sw-add-intro').addEventListener('click', () => $('#sw-intro-file').click());
  $('#sw-intro-file').addEventListener('change', event => {
    const file = event.target.files[0]; if (!file) return;
    const select = $('#sw-intro-select');
    if (![...select.options].some(option => option.value === file.name)) select.add(new Option(file.name, file.name));
    select.value = file.name; toast(`${file.name} selected for VPS upload.`);
  });
  $('#sw-remove-intro').addEventListener('click', () => {
    const select = $('#sw-intro-select'); if (!select.value) return toast('Select a saved intro first.');
    if (!confirm(`Remove ${select.value} from saved VPS intros?`)) return;
    select.remove(select.selectedIndex); select.value = ''; toast('Saved intro removed from this preview.');
  });
  $$('[data-preview-action]').forEach(button => button.addEventListener('click', () => toast(`${button.dataset.previewAction} control is ready for backend wiring.`)));
  $('#sw-process-preview').addEventListener('click', () => toast('Successful TXT files will upload as one verified batch ZIP; failed scripts will be skipped and listed.'));
  renderGroups(); renderLinks();
})();
