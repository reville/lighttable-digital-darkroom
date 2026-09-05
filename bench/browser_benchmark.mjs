#!/usr/bin/env node
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const modulePath = process.env.LIGHTTABLE_PLAYWRIGHT_MODULE;
if (!modulePath) throw new Error('LIGHTTABLE_PLAYWRIGHT_MODULE is not set');
const { chromium } = await import(pathToFileURL(modulePath).href);

const browser = await chromium.launch({
  headless: true,
  executablePath: config.browserExecutable,
});
const page = await browser.newPage({ viewport: { width: 1728, height: 1117 } });
const diagnostics = [];
page.on('pageerror', (error) => diagnostics.push(`pageerror: ${error.message}`));
page.on('requestfailed', (request) => diagnostics.push(
  `requestfailed: ${request.method()} ${request.url()} ${request.failure()?.errorText || ''}`));
page.on('console', (message) => {
  if (['error', 'warning'].includes(message.type())) {
    diagnostics.push(`console.${message.type()}: ${message.text()}`);
  }
});
await page.addInitScript(() => { window.__LIGHTTABLE_BENCHMARK__ = true; });

async function renderCount() {
  return page.evaluate(() => window.__lightTablePerf?.renders.length || 0);
}

async function renderSnapshot() {
  return page.evaluate(() => ({
    count: window.__lightTablePerf?.renders.length || 0,
    latest: window.__lightTablePerf?.renders.at(-1) || null,
  }));
}

async function waitForRender(after, timeout = 180_000) {
  await page.waitForFunction(
    (count) => (window.__lightTablePerf?.renders.length || 0) > count,
    after,
    { timeout },
  );
  return page.evaluate((count) => window.__lightTablePerf.renders.slice(count).at(-1), after);
}

async function settleFrame(entry, requestedWidth) {
  let current = entry;
  for (let attempt = 0; current && attempt < 8; attempt += 1) {
    const snapshot = await renderSnapshot();
    current = snapshot.latest || current;
    const progressive = current.phase === 'interactive' || current.width !== requestedWidth;
    if (!current.refining && !progressive) break;
    current = await waitForRender(snapshot.count);
  }
  return current;
}

const output = {
  schema: 1,
  userAgent: '',
  startup: {},
  widths: {},
  navigation: { first: [], settled: [] },
};
try {
  const navigationStartedAt = Date.now();
  await page.goto(config.baseUrl, { waitUntil: 'domcontentloaded', timeout: 90_000 });
  await page.waitForFunction(() => (window.__lightTablePerf?.renders.length || 0) > 0,
    null, { timeout: 180_000 });
  const first = await page.evaluate(() => window.__lightTablePerf.renders[0]);
  output.userAgent = await page.evaluate(() => navigator.userAgent);
  output.startup = {
    navigationToFirstFrameMs: Date.now() - navigationStartedAt,
    firstRender: first,
    resource: await page.evaluate(() => {
      const nav = performance.getEntriesByType('navigation')[0];
      return nav ? {
        domContentLoadedMs: nav.domContentLoadedEventEnd,
        loadEventMs: nav.loadEventEnd,
        transferredBytes: nav.transferSize,
      } : {};
    }),
  };

  const beforeSetup = await renderCount();
  await page.evaluate(({ imageName, width }) => {
    window.__lightTablePerf.prepare(imageName, width, 'rs');
  }, { imageName: config.imageName, width: config.widths[0] });
  await settleFrame(await waitForRender(beforeSetup), config.widths[0]);

  for (const width of config.widths) {
    const beforeWidth = await renderCount();
    await page.evaluate((value) => {
      const select = document.querySelector('#pw');
      select.value = String(value);
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }, width);
    await settleFrame(await waitForRender(beforeWidth), width);
    await page.evaluate(() => window.__lightTablePerf.reset());

    const firstFrames = [];
    const settledFrames = [];
    for (let index = 0; index < config.iterations; index += 1) {
      const before = await renderCount();
      const value = 0.333 + index * (1.777 / Math.max(1, config.iterations - 1));
      await page.evaluate((nextValue) => {
        const slider = document.querySelector('#print_exposure');
        slider.value = String(nextValue);
        slider.dispatchEvent(new Event('input', { bubbles: true }));
        slider.dispatchEvent(new Event('change', { bubbles: true }));
      }, value);
      const firstFrame = await waitForRender(before);
      firstFrames.push(firstFrame);
      settledFrames.push(await settleFrame(firstFrame, width));
    }
    output.widths[String(width)] = { first: firstFrames, settled: settledFrames };
  }

  await page.evaluate(() => window.__lightTablePerf.reset());
  for (let index = 0; index < config.iterations; index += 1) {
    const before = await renderCount();
    await page.keyboard.press(index % 2 === 0 ? 'ArrowRight' : 'ArrowLeft');
    const firstFrame = await waitForRender(before);
    output.navigation.first.push(firstFrame);
    output.navigation.settled.push(await settleFrame(firstFrame, config.widths.at(-1)));
  }
} catch (error) {
  const snapshot = await page.evaluate(() => ({
    readyState: document.readyState,
    renderCount: window.__lightTablePerf?.renders.length || 0,
    status: document.querySelector('#rstat')?.textContent || null,
  })).catch(() => ({}));
  process.stderr.write(`${JSON.stringify({
    error: String(error?.message || error), snapshot,
    diagnostics: diagnostics.slice(-30),
  }, null, 2)}\n`);
  throw error;
} finally {
  await browser.close();
}

const serialized = `${JSON.stringify(output)}\n`;
if (config.output) fs.writeFileSync(config.output, serialized);
process.stdout.write(serialized);
