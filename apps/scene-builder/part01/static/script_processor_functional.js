(function () {
  'use strict';
  const panel = document.querySelector('[data-sw-panel="processor"]');
  const extractorPanel = document.querySelector('[data-sw-panel="extractor"]');
  const processorMode = new URLSearchParams(window.location.search).get('tab') === 'processor';
  if (panel && extractorPanel) {
    panel.hidden = !processorMode;
    panel.style.display = processorMode ? 'block' : 'none';
    extractorPanel.hidden = processorMode;
    extractorPanel.style.display = processorMode ? 'none' : 'block';
  }
  if (!panel || !processorMode) return;
  const esc = v => String(v == null ? '' : v).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const toast = message => { const node = document.querySelector('#sw-toast'); if (node) { node.textContent = message; node.classList.add('show'); setTimeout(() => node.classList.remove('show'), 3200); } };
  const api = async (url, options = {}) => { const response = await fetch(url, {credentials:'same-origin', headers:{'Content-Type':'application/json', ...(options.headers || {})}, ...options}); const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.detail || data.message || `Request failed (${response.status})`); return data; };
  const field = label => Array.from(panel.querySelectorAll('label')).find(x => x.textContent.toLowerCase().includes(label.toLowerCase()))?.querySelector('input,select,textarea');
  const check = label => Array.from(panel.querySelectorAll('label')).find(x => x.textContent.toLowerCase().includes(label.toLowerCase()))?.querySelector('input[type="checkbox"]');
  const group = panel.querySelector('select'), doc = field('Google Doc link'), batch = field('Batch ID'), category = field('Category'), intro = field('Saved Intro TXT'), serial = field('Start serial'), minimum = field('Minimum size'), maximum = field('Maximum size'), words = field('Removal words');
  const duplicate = check('Duplicate check'), remove = check('Remove words'), skip = check('Skip failed scripts');
  let selected = group?.value || '', state = {};
  const status = () => Array.from(panel.querySelectorAll('*')).find(x => x.children.length === 0 && /Ready|Select a batch|ZIP/i.test(x.textContent || ''));
  const render = () => { const groups = state.groups || []; if (!selected || !groups.some(x => x.name === selected)) selected = state.selected_group?.name || groups[0]?.name || ''; if (group) group.innerHTML = '<option value="">Select extracted batch</option>' + groups.map(x => `<option value="${esc(x.name)}" ${x.name === selected ? 'selected' : ''}>${esc(x.name)}</option>`).join(''); const g = groups.find(x => x.name === selected) || state.selected_group || {}, c = state.config || {}; if (doc) doc.value = g.document_url || ''; if (batch) batch.value = c.batch_id || ''; if (category && c.category != null) category.value = c.category; if (intro) intro.value = ''; if (serial) serial.value = c.start_serial ?? 1; if (minimum) minimum.value = c.minimum_size_kb ?? 0; if (maximum) maximum.value = c.maximum_size_kb ?? 0; if (words) words.value = c.removal_words || ''; if (duplicate) duplicate.checked = c.duplicate_check !== false; if (remove) remove.checked = !!c.remove_words; if (skip) skip.checked = c.skip_failed_scripts !== false; const node = status(); if (node) node.textContent = state.output?.ready ? `ZIP ready: ${state.output.name}` : (selected ? 'Ready' : 'Select a batch to process'); };
  const load = async () => { try { state = await api(`/api/script-work/processor/state?group=${encodeURIComponent(selected)}`); render(); } catch (e) { toast(e.message); } };
  const payload = () => ({group:selected, batch_id:batch?.value || '', category:category?.value || 'general', intro_filename:intro?.files?.[0]?.name || '', start_serial:Number(serial?.value || 1), minimum_size_kb:Number(minimum?.value || 0), maximum_size_kb:Number(maximum?.value || 0), duplicate_check:!!duplicate?.checked, remove_words:!!remove?.checked, removal_words:words?.value || '', skip_failed_scripts:skip?.checked !== false});
  const save = async () => { if (!selected) throw new Error('Select an extracted batch first'); state = await api('/api/script-work/processor/config', {method:'POST', body:JSON.stringify(payload())}); render(); };
  group?.addEventListener('change', () => { selected = group.value; load(); });
  panel.querySelectorAll('input,select,textarea').forEach(node => node.addEventListener('change', () => { if (selected) save().catch(e => toast(e.message)); }));
  const buttons = Array.from(panel.querySelectorAll('button')), find = text => buttons.find(b => (b.textContent || '').toLowerCase().includes(text));
  find('process')?.addEventListener('click', async () => { try { if (!selected) throw new Error('Select an extracted batch first'); state = await api('/api/script-work/processor/process', {method:'POST', body:JSON.stringify(payload())}); render(); toast(`ZIP created: ${state.zip_name}`); } catch (e) { toast(e.message); } });
  find('download')?.addEventListener('click', () => { if (!selected) return toast('Select an extracted batch first'); window.location.href = `/api/script-work/processor/download?group=${encodeURIComponent(selected)}`; });
  find('open drive')?.addEventListener('click', () => { const url = state.selected_group?.document_url; if (!url) return toast('No Google Doc is assigned to this batch'); window.open(url, '_blank', 'noopener'); });
  panel.querySelectorAll('[data-preview-action]').forEach(button => button.removeAttribute('data-preview-action'));
  load();
})();
