import { availableLocales, currentLocale, initializeLanguage, loadLocale, saveLanguage,
  t, translateStatic, useCatalog } from './i18n.js';
import { sendNative } from './native-bridge.js';

export function applyDocumentLanguage() {
  const info = availableLocales().find(item => item.code === currentLocale());
  document.documentElement.lang = currentLocale();
  document.documentElement.dir = info?.dir || 'ltr';
  translateStatic();
}

function createPicker() {
  const dialog = document.createElement('dialog');
  dialog.id = 'languageDialog';
  dialog.className = 'language-dialog';
  dialog.setAttribute('aria-labelledby', 'languageTitle');
  // All prose is set through textContent below; translated text is never HTML.
  dialog.innerHTML = '<form method="dialog"><span class="language-brand">LightTable</span><h1 id="languageTitle"></h1><p id="languageIntro"></p><label for="languageChoice" id="languageLabel"></label><select id="languageChoice" data-no-i18n></select><p id="languageError" role="alert"></p><button id="languageContinue" type="submit" class="accent-btn"></button></form>';
  document.body.append(dialog);
  return dialog;
}
export function chooseLanguage(suggested, locales) {
  const dialog = createPicker();
  const select = dialog.querySelector('select');
  const submit = dialog.querySelector('button');
  const error = dialog.querySelector('[role="alert"]');
  locales.forEach(item => {
    const option = new Option(item.nativeName, item.code);
    option.lang = item.code; option.dir = item.dir;
    select.append(option);
  });
  select.value = suggested;
  const updateCopy = () => {
    dialog.dir = locales.find(item => item.code === currentLocale())?.dir || 'ltr';
    dialog.lang = currentLocale();
    dialog.querySelector('h1').textContent = t('Choose your language');
    dialog.querySelector('#languageIntro').textContent = t('Use LightTable and its help in your preferred language. You can change this later in Settings.');
    dialog.querySelector('label').textContent = t('Language');
    submit.textContent = t('Continue');
  };
  let generation = 0;
  let loading = Promise.resolve();
  const preview = () => {
    const revision = ++generation;
    const code = select.value;
    submit.disabled = true; error.textContent = '';
    loading = loadLocale(code).then(next => {
      if (revision !== generation) return;
      useCatalog(code, next); updateCopy(); submit.disabled = false;
    }).catch(() => {
      if (revision !== generation) return;
      error.textContent = t('Could not load this language. Try again.');
      // Continue retries the load, retaining the user's selection.
      submit.disabled = false;
    });
  };
  select.addEventListener('change', preview);
  dialog.addEventListener('cancel', event => event.preventDefault());
  updateCopy(); dialog.showModal(); select.focus(); preview();
  return new Promise(resolve => {
    dialog.querySelector('form').addEventListener('submit', async event => {
      event.preventDefault();
      await loading;
      submit.disabled = true; select.disabled = true; error.textContent = '';
      try {
        const code = select.value;
        const next = await saveLanguage(code);
        useCatalog(code, next);
        sendNative('localizationChanged', { locale: code });
        dialog.close(); dialog.remove(); resolve(code);
      } catch (failure) {
        error.textContent = failure.message;
        submit.disabled = false; select.disabled = false; submit.focus();
      }
    });
  });
}
export async function bootLanguage() {
  const languages = window.__LIGHTTABLE_SYSTEM_LANGUAGES__ || navigator.languages || [navigator.language];
  await initializeLanguage({ languages, choose: chooseLanguage });
  applyDocumentLanguage();
}
export function showBootError(error, retry) {
  const panel = document.createElement('div');
  panel.className = 'language-boot-error';
  const title = document.createElement('h1'); title.textContent = 'LightTable';
  const message = document.createElement('p'); message.textContent = t('LightTable could not finish starting. Your photos and edits are unchanged.');
  const detail = document.createElement('p'); detail.textContent = error.message;
  const button = document.createElement('button'); button.textContent = t('Try again');
  button.onclick = () => { panel.remove(); retry(); };
  panel.append(title, message, detail, button); document.body.append(panel);
}
export async function changeLanguage(code) {
  if (code === currentLocale()) return;
  // The same close barrier used by the shell flushes edits and history, and
  // prevents further editing during the save/reload boundary.
  try {
    const ready = await window.lightTablePrepareToClose?.();
    if (ready === false) throw new Error(t('Save your pending edits before changing language. Use Retry in the save status, then try again.'));
    await saveLanguage(code);
    sendNative('localizationChanged', { locale: code });
    window.location.reload();
  } catch (error) {
    window.lightTableCancelClose?.();
    throw error;
  }
}
