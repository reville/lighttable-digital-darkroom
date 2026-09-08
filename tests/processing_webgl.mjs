// Production WebGL renderer, synchronous framebuffer readback, and composited
// canvas screenshots. The interactive canvas keeps preserveDrawingBuffer=false.
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {chromium} = await import(pathToFileURL(config.module).href);
const browser = await chromium.launch({headless: true,
  executablePath: config.browser || undefined,
  args: ['--enable-unsafe-swiftshader', '--force-color-profile=srgb']});
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, async () => {await browser.close(); process.exit(1);});
try {
  const page = await browser.newPage({viewport: {width: 640, height: 480}, deviceScaleFactor: 1});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(config.baseUrl);
  await page.evaluate(async () => {
    const {GradeRenderer} = await import('/web/gl.js');
    document.body.innerHTML = '<canvas id="preview"></canvas>';
    document.body.style.cssText = 'margin:0;background:#333';
    const canvas = document.querySelector('canvas');
    canvas.style.display = 'block';
    window.renderer = new GradeRenderer(canvas);
    if (!renderer.gl) throw Error('WebGL unavailable');
  });
  const records = [];
  for (const test of config.cases) {
    const output = await page.evaluate(async (test) => {
      const image = new Image();
      image.src = test.source;
      await image.decode();
      renderer.setImage(image, {cacheKey: test.source});
      renderer.clearOriginalImage();
      if (test.original) {
        const original = new Image(); original.src = test.original;
        await original.decode(); renderer.setOriginalImage(original);
      }
      renderer.comparePosition = test.compare || 0;
      renderer.draw(test.grade);
      const gl = renderer.gl, canvas = renderer.canvas;
      const rgba = new Uint8Array(canvas.width * canvas.height * 4);
      // Read immediately in the draw task, before the nonpersistent buffer retires.
      gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
      const error = gl.getError();
      if (error) throw Error(`WebGL error ${error}`);
      const debug = gl.getExtension('WEBGL_debug_renderer_info');
      window.drawForCapture = () => renderer.draw(test.grade);
      return {width: canvas.width, height: canvas.height, pixels: Array.from(rgba),
        renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)};
    }, test);
    // Keep a live frame available during Playwright's animation-stability check.
    await page.evaluate(() => {
      window.captureActive = true;
      const draw = () => { if (window.captureActive) {drawForCapture(); requestAnimationFrame(draw);} };
      requestAnimationFrame(draw);
    });
    await page.locator('#preview').screenshot({path: test.screenshot, timeout: 15000});
    await page.evaluate(() => { window.captureActive = false; });
    fs.writeFileSync(test.raw, Buffer.from(output.pixels));
    delete output.pixels;
    records.push({name: test.name, ...output});
  }
  if (errors.length) throw Error(errors.join('\n'));
  fs.writeFileSync(config.result, JSON.stringify({records, browserVersion: browser.version()}));
} finally {
  await browser.close();
}
