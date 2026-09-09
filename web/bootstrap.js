import { bootLanguage, showBootError } from './locale-bootstrap.js';
async function start() {
  try {
    await bootLanguage();
    await import('./help-panel.js');
    await import('./dropdown.js');
    await import('./app.js');
    await import('./slider-ux.js');
    const { installAppearanceTester } = await import('./appearance-tester.js');
    installAppearanceTester();
    document.body.classList.remove('locale-loading');
  } catch (error) { showBootError(error, start); }
}
start();
