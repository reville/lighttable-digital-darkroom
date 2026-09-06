/* First-run orientation. Completion is durable; opening a picker is not. */
export function shouldShowSetup(prefs, library, nativeFirstRun = null) {
  if (!prefs || !library || prefs.firstRunSetup?.version >= 1) return false;
  if (nativeFirstRun !== null) return nativeFirstRun;
  return Number(library.total || library.images?.length || 0) === 0
    && !(library.catalog?.sources || []).some((source) => source.count > 0);
}

export function installFirstRunSetup({ el, post, sendNative, nativeBridge,
  openCatalog, reloadLibrary, onComplete }) {
  const dialog = el('firstRunDialog');
  let prefs = null, library = null, nativeFirstRun = null;
  let nativeSetup = false, photosAvailable = false;
  let page = 'choices', busy = false, catalogPending = false;
  let catalogResult = null;
  let resultSource = null, returnFocus = null;
  const inertElements = new Map();
  const isOpen = () => dialog.classList.contains('on');

  function show(visible) {
    if (visible === isOpen()) return;
    dialog.classList.toggle('on', visible);
    dialog.setAttribute('aria-hidden', String(!visible));
    document.body.classList.toggle('setup-open', visible);
    if (visible) {
      returnFocus = document.activeElement;
      for (const node of document.body.children) {
        if (node === dialog || node.tagName === 'SCRIPT') continue;
        inertElements.set(node, node.inert);
        node.inert = true;
      }
      focusPage();
    } else {
      for (const [node, wasInert] of inertElements) node.inert = wasInert;
      inertElements.clear();
      returnFocus?.focus?.({ preventScroll: true });
    }
  }

  function focusPage() {
    requestAnimationFrame(() => {
      if (isOpen()) (page === 'choices' ? el('setupWelcome')
        : dialog.querySelector(`[data-setup-page="${page}"] h2`))?.focus();
    });
  }

  function setPage(next) {
    page = next;
    el('setupPanel').setAttribute('aria-labelledby', next === 'choices' ? 'setupWelcome' : `setupHeading-${next}`);
    for (const node of dialog.querySelectorAll('[data-setup-page]')) {
      node.hidden = node.dataset.setupPage !== next;
    }
    el('setupBack').hidden = next === 'choices' || busy;
    el('setupLater').hidden = busy || next === 'result';
    el('setupError').textContent = '';
    focusPage();
  }

  function maybeShow() {
    // The Mac shell knows whether this is a new installation, even before a
    // large existing catalog has completed its first background scan.
    if (nativeBridge() && window.__LIGHTTABLE_PLATFORM__ !== 'windows'
        && nativeFirstRun === null) return;
    if (shouldShowSetup(prefs, library, nativeFirstRun) && !catalogPending) show(true);
  }

  function capabilities() {
    el('setupPhotosAll').disabled = !photosAvailable;
    el('setupPhotosSelected').hidden = !nativeBridge()
      || window.__LIGHTTABLE_PLATFORM__ === 'windows';
    el('setupPhotosAvailability').hidden = photosAvailable;
    el('setupFolderPathField').hidden = nativeSetup;
    el('setupFolderChoose').textContent = nativeSetup ? 'Choose folder…' : 'Add folder';
  }

  async function finish(status = 'completed', source = resultSource) {
    if (busy) return;
    busy = true;
    el('setupDone').disabled = el('setupLater').disabled = true;
    try {
      const value = { version: 1, status, source: source || null };
      const patch = { firstRunSetup: value };
      // Folder archives and Photos originals can live entirely in subfolders.
      if (status === 'completed') patch.includeSubfolders = true;
      const result = await post('/api/prefs', patch);
      if (!result?.ok || result.error) throw new Error(result?.error || 'Could not save setup.');
      prefs = { ...prefs, firstRunSetup: value };
      sendNative('completeFirstRun');
      if (status === 'completed') onComplete?.();
      show(false);
    } catch (error) {
      el('setupError').textContent = `${error.message} Please try again.`;
    } finally {
      busy = false;
      el('setupDone').disabled = el('setupLater').disabled = false;
    }
  }

  function showResult(source, title, message) {
    busy = false;
    resultSource = source;
    el('setupHeading-result').textContent = title;
    el('setupResultMessage').textContent = message;
    setPage('result');
    show(true);
  }

  function photosEvent(event) {
    const running = event.state === 'running';
    if (running) {
      busy = true;
      setPage('photos-progress');
      show(true);
      el('setupPhotosProgress').max = Math.max(1, event.total || 1);
      el('setupPhotosProgress').value = event.completed || 0;
      el('setupPhotosProgressText').textContent = (event.message || 'Importing originals…')
        + (event.total ? ` ${event.completed || 0} of ${event.total} originals` : '');
      el('setupPhotosCancel').disabled = false;
      return;
    }
    busy = false;
    if (event.state === 'completed' && event.failures > 0
        && !(event.imported > 0 || event.existing > 0)) {
      setPage('photos'); show(true);
      el('setupError').textContent = `None of the ${event.failures} originals could be imported. Check your connection and available disk space, then try again.`;
    } else if (event.state === 'completed') {
      showResult('photos', event.failures ? 'Your Photos import is ready to review' : 'Your photos are ready',
        `${event.imported || 0} originals imported · ${event.existing || 0} already added`
        + (event.failures ? ` · ${event.failures} could not be imported. Run this import again to retry.` : '.')
        + ' Add new photos later by running this import again.');
    } else if (event.state === 'cancelled') {
      setPage('photos'); show(true);
      el('setupError').textContent = event.message || 'Import stopped. Any completed copies are kept; you can resume by importing again.';
    } else if (event.state === 'error') {
      setPage('photos'); show(true);
      el('setupError').textContent = event.message || 'Photos could not be imported. Try again or choose a folder.';
    }
  }

  el('setupLightroom').onclick = () => setPage('lightroom');
  el('setupPhotos').onclick = () => { capabilities(); setPage('photos'); };
  el('setupFolder').onclick = () => { capabilities(); setPage('folder'); };
  el('setupBack').onclick = () => { if (!busy) setPage('choices'); };
  el('setupLater').onclick = () => finish('skipped', null);
  el('setupDone').onclick = () => finish();
  el('setupCatalogChoose').onclick = () => {
    catalogPending = true;
    catalogResult = null;
    show(false);
    openCatalog();
  };
  el('setupPhotosAll').onclick = () => {
    el('setupError').textContent = '';
    if (!photosAvailable || !sendNative('importApplePhotosLibrary')) return;
    busy = true;
    setPage('photos-progress');
    el('setupPhotosProgress').removeAttribute('value');
    el('setupPhotosProgressText').textContent = 'Waiting for access to Photos…';
  };
  el('setupPhotosCancel').onclick = () => {
    sendNative('cancelApplePhotosLibraryImport');
    el('setupPhotosCancel').disabled = true;
    el('setupPhotosProgressText').textContent = 'Stopping import…';
  };
  el('setupPhotosSelected').onclick = () => sendNative('importApplePhotos');
  el('setupFolderChoose').onclick = async () => {
    if (nativeSetup) { sendNative('setupChooseFolder'); return; }
    const path = el('setupFolderPath').value.trim();
    if (!path) { el('setupFolderPath').focus(); return; }
    el('setupFolderChoose').disabled = true;
    try {
      const result = await post('/api/catalog/sources', { action: 'add', path });
      if (!result?.ok || result.error) throw new Error(result?.error || 'Could not add that folder.');
      await reloadLibrary();
      showResult('folder', 'Your folder is ready', 'Photos stay in their current folder. You can add more folders whenever you like.');
    } catch (error) { el('setupError').textContent = error.message; }
    finally { el('setupFolderChoose').disabled = false; }
  };
  el('setupReopen').onclick = () => {
    window.LightTableSettings?.close();
    capabilities(); setPage('choices'); show(true);
  };
  // Capture before editor shortcuts; Tab stays in the active setup page.
  document.addEventListener('keydown', (event) => {
    if (!isOpen()) return;
    event.stopImmediatePropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      if (!busy) page === 'choices' ? finish('skipped', null) : setPage('choices');
    } else if (event.key === 'Tab') {
      const targets = [...dialog.querySelectorAll('button, input, [tabindex="0"]')]
        .filter((node) => !node.disabled && node.getClientRects().length);
      const index = targets.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) { event.preventDefault(); targets.at(-1)?.focus(); }
      else if (!event.shiftKey && (index < 0 || index === targets.length - 1)) {
        event.preventDefault(); targets[0]?.focus();
      }
    }
  }, true);
  capabilities();
  return {
    setPrefs(value) { prefs = value; maybeShow(); },
    setLibrary(value) { library = value; maybeShow(); },
    catalogCompleted(result) {
      if (!catalogPending) return;
      if (!(result.matched > 0)) {
        setPage('lightroom');
        el('setupError').textContent = result.images
          ? 'No originals could be found. Reconnect the photo folders and import the catalog again.'
          : 'This catalog has no photos. Choose another catalog or start with a folder.';
        return;
      }
      catalogResult = result;
      resultSource = 'lightroom';
      el('setupHeading-result').textContent = 'Your Lightroom catalog is imported';
      el('setupResultMessage').textContent = `${result.matched || 0} photos added. Your original catalog is unchanged.`
        + (result.unmatched ? ` ${result.unmatched} originals could not be found; reconnect their folders to edit them.` : '');
      setPage('result');
    },
    catalogClosed() {
      if (!catalogPending) return;
      catalogPending = false;
      if (catalogResult && nativeSetup) {
        sendNative('setupCatalogImported', { paths: catalogResult.sourceFolders || [],
          matched: catalogResult.matched, unmatched: catalogResult.unmatched });
        catalogResult = null;
        return;
      }
      show(true);
    },
    nativeEvent(event) {
      if (event?.type === 'sources') {
        if (typeof event.firstRun === 'boolean') nativeFirstRun = event.firstRun;
        else if (nativeFirstRun === null) nativeFirstRun = false;
        nativeSetup = typeof event.firstRun === 'boolean';
        photosAvailable = event.photosLibraryImportAvailable === true;
        capabilities();
        if (prefs?.firstRunSetup?.version >= 1 && nativeFirstRun) sendNative('completeFirstRun');
        maybeShow();
      } else if (event?.type === 'photosLibraryImport') photosEvent(event);
      else if (event?.type === 'setupFolderSelected') {
        showResult('folder', 'Your folder is ready', 'Photos stay in their current folder. You can add more folders whenever you like.');
      } else if (event?.type === 'setupCatalogImported') {
        showResult('lightroom', 'Your Lightroom catalog is imported',
          `${event.matched || 0} photos added. Your original catalog is unchanged.`
          + (event.unmatched ? ` ${event.unmatched} originals could not be found; reconnect their folders to edit them.` : ''));
      } else if (event?.type === 'setupFolderCancelled') {
        setPage('folder'); show(true);
      } else if (event?.type === 'photosImported' && event.count > 0
          && (isOpen() || nativeFirstRun)) {
        showResult('photos', 'Your photos are ready', `${event.count} photos imported.`
          + (event.failures ? ` ${event.failures} could not be imported.` : ''));
      } else if (event?.type === 'error' && isOpen()) {
        el('setupError').textContent = event.message || 'Something went wrong. Please try again.';
      }
    },
  };
}
