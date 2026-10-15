(() => {
  const $ = (id) => document.getElementById(id);
  const state = {catalog: null};
  const optionize = (select, values) => { select.innerHTML = ''; (values || []).forEach(item => { const o = document.createElement('option'); o.value = item.value; o.textContent = item.label || item.value; select.appendChild(o); }); };
  const message = (text, kind='') => { const el = $('af-validation'); el.textContent = text; el.className = `af-message ${kind}`; };
  const load = async () => {
    const [catalog, ledger] = await Promise.all([fetch('/api/atomic-flow/catalog'), fetch('/api/atomic-flow/state')]);
    state.catalog = await catalog.json(); const runs = await ledger.json();
    optionize($('af-group'), state.catalog.groups); optionize($('af-script-profile'), state.catalog.script_profiles); optionize($('af-voice-profile'), state.catalog.voice_profiles); optionize($('af-acs-preset'), state.catalog.acs_presets);
    $('af-feature-badge').textContent = state.catalog.feature_enabled ? 'Dispatch enabled' : 'Safe prepare mode'; $('af-feature-badge').classList.toggle('ready', !!state.catalog.feature_enabled);
    $('af-pipeline').innerHTML = state.catalog.pipeline.map((s, i) => `<li><span class="stage-number">0${i+1}</span><span class="stage-label">${s.label}</span><span class="stage-owner">${s.owner}</span></li>`).join('');
    renderRuns(runs.runs || []);
  };
  const renderRuns = (runs) => { $('af-runs').innerHTML = runs.length ? runs.map(r => `<tr><td>${r.flow_id}</td><td>${r.status}</td><td>${r.stage}</td><td>${new Date(r.created_at).toLocaleString()}</td></tr>`).join('') : '<tr><td colspan="4" class="muted">No runs yet.</td></tr>'; };
  const links = () => $('af-links').value;
  $('af-validate').addEventListener('click', async () => { message('Validating…'); const r = await fetch('/api/atomic-flow/validate', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({links:links()})}); const d = await r.json(); message(d.valid ? `${d.items.length} link(s) ready. Permanent IDs confirmed from the Google Sheet.` : 'Add a permanent ID to every line using PERMANENT_ID | URL.', d.valid ? 'success' : 'error'); });
  $('af-start').addEventListener('click', async () => { message('Preparing run…'); const payload = {group:$('af-group').value, script_profile:$('af-script-profile').value, voice_profile:$('af-voice-profile').value, acs_preset:$('af-acs-preset').value, links:links()}; const r = await fetch('/api/atomic-flow/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); const d = await r.json(); if (!r.ok) { message(d.detail || 'Unable to start run.', 'error'); return; } message(`${d.flow_id} created: ${d.status.replaceAll('_',' ')}. Existing queues were not touched.`, 'success'); load(); });
  $('af-refresh').addEventListener('click', load); load().catch(() => message('Atomic Flow status is temporarily unavailable.', 'error'));
})();
