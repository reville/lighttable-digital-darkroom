import { bootLanguage, showBootError } from './locale-bootstrap.js';
async function start() {
  try {
    await bootLanguage();
    await import('./loupe.js');
    document.body.classList.remove('locale-loading');
  } catch (error) { showBootError(error, start); }
}
start();
