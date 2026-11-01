/* Short Uploader only: split an arbitrarily large token selection into safe HTTP batches. */
(() => {
  const form = document.querySelector('[data-short-vault-token-form]');
  if (!form) return;
  const status = document.querySelector('[data-short-vault-progress]');
  const input = form.querySelector('input[type=file][name=files]');
  const button = form.querySelector('button[type=submit]');
  if (!input || !status || !button) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const selected = Array.from(input.files || []);
    if (!selected.length) { status.textContent = 'Select token JSON files.'; return; }
    // This is a transfer chunk size, NOT a user file-count restriction.
    const chunkSize = 32;
    const aggregate = {installed: 0, kept: 0, invalid: 0, unmatched: 0};
    const failures = [];
    button.disabled = true;
    try {
      for (let offset = 0; offset < selected.length; offset += chunkSize) {
        const part = selected.slice(offset, offset + chunkSize);
        const data = new FormData();
        data.append('csrf_token', form.querySelector('[name=csrf_token]').value);
        data.append('secret_kind', 'token');
        data.append('replace_existing', form.querySelector('[name=replace_existing]').checked ? '1' : '0');
        for (const file of part) data.append('files', file, file.name);
        status.textContent = 'Processing ' + Math.min(offset + part.length, selected.length) +
          ' of ' + selected.length + ' selected JSON files…';
        const response = await fetch(form.action, {
          method: 'POST', body: data, credentials: 'same-origin',
          headers: {'Accept': 'application/json'},
        });
        if (!response.ok) throw new Error('Batch rejected (HTTP ' + response.status + ')');
        const result = await response.json();
        for (const key of Object.keys(aggregate)) aggregate[key] += Number(result[key] || 0);
        for (const reason of (result.failures || [])) if (failures.length < 5) failures.push(reason);
      }
      status.textContent = 'Finished ' + selected.length + ' files: ' +
        aggregate.installed + ' installed, ' + aggregate.kept + ' kept, ' +
        aggregate.invalid + ' invalid, ' + aggregate.unmatched + ' unmatched.' +
        (failures.length ? ' Details: ' + failures.join('; ') : ' Refresh to see updated inventory.');
    } catch (error) {
      status.textContent = 'Stopped after a partial batch: ' + error.message +
        '. Already accepted tokens were preserved; retry without Replace to skip valid ones.';
    } finally {
      button.disabled = false;
    }
  });
})();