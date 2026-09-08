export function createEnhancePanel({ el, getPhoto, getCapabilities, run, onComplete }) {
  const button = el('enhanceRun');
  const select = el('enhanceMode');
  const note = el('enhanceNote');
  let capabilities = null, busy = false, message = '', generation = 0;

  function sync() {
    for (const option of select.options) {
      option.disabled = busy || !capabilities?.available || !capabilities.modes?.[option.value];
    }
    if (!busy && !capabilities?.modes?.[select.value]) {
      const available = [...select.options].find(option => !option.disabled);
      if (available) select.value = available.value;
    }
    select.disabled = ![...select.options].some(option => !option.disabled);
    button.disabled = busy || !getPhoto() || !capabilities?.available
      || !capabilities.modes?.[select.value];
    if (busy) note.textContent = 'Working…';
    else if (!capabilities) note.textContent = 'Checking available enhancements…';
    else if (!capabilities.available) note.textContent = capabilities.reason || 'Not available.';
    else if (!getPhoto()) note.textContent = 'Select a photo first.';
    else if (message) note.textContent = message;
    else note.textContent = 'Writes a new 16-bit master; the original is untouched.'
      + (!capabilities.modes?.upscale ? ' Super resolution needs an installed model.' : '');
  }

  async function refresh() {
    if (busy) return;
    const request = ++generation;
    capabilities = null;
    message = '';
    sync();
    try {
      const result = await getCapabilities();
      if (request !== generation) return;
      capabilities = result?.error ? { available: false, reason: result.error } : result;
    } catch (_) {
      if (request !== generation) return;
      capabilities = { available: false, reason: 'Could not check available enhancements.' };
    }
    sync();
  }

  select.addEventListener('change', () => { message = ''; sync(); });
  button.onclick = async () => {
    const photo = getPhoto();
    if (busy || !photo || !capabilities?.available || !capabilities.modes?.[select.value]) return;
    busy = true;
    message = '';
    const mode = select.value;
    sync();
    try {
      const result = await run({ name: photo.name, mode });
      if (result.ok) await onComplete(result);
      else message = result.error || 'Enhance failed';
    } catch (error) {
      message = error.message || 'Enhance failed';
    } finally {
      busy = false;
      sync();
    }
  };
  sync();
  return { refresh, sync };
}
