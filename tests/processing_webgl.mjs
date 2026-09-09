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
  const page = await browser.newPage({viewport: {width: 1280, height: 800}, deviceScaleFactor: 1});
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
      const draw = () => renderer.draw(test.grade, [], undefined, null, test.spotVisualization);
      draw();
      const gl = renderer.gl, canvas = renderer.canvas;
      const rgba = new Uint8Array(canvas.width * canvas.height * 4);
      // Read immediately in the draw task, before the nonpersistent buffer retires.
      gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
      if (test.spotVisualization?.enabled) {
        if (test.fixture === 'spots' && !Object.keys(test.grade).length) {
          const reference = document.createElement('canvas');
          reference.width = canvas.width; reference.height = canvas.height;
          const ctx = reference.getContext('2d');
          const t = test.spotVisualization.threshold;
          ctx.filter = `grayscale(1) invert(1) contrast(${2 + t * 7}) brightness(${0.72 + t * 0.35})`;
          ctx.drawImage(image, 0, 0);
          const expected = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
          for (let y = 0; y < canvas.height; y++) for (let x = 0; x < canvas.width; x++) {
            const i = (y * canvas.width + x) * 4;
            const j = ((canvas.height - 1 - y) * canvas.width + x) * 4;
            if (Math.abs(rgba[j] - expected[i]) > 2) {
              ctx.filter = 'none'; ctx.drawImage(image, 0, 0);
              const input = Array.from(ctx.getImageData(x,y,1,1).data);
              throw Error(`Visualization changed existing filter appearance at ${test.name} ${x},${y}: ${rgba[j]} vs ${expected[i]}, input ${input}`);
            }
          }
        }
        const pixel = renderer.samplePixel(0.5, 0.5);
        const histogram = Uint8Array.from(renderer.sample().px);
        // The sampling calls must restore display uniforms, including for a
        // compare-only redraw that does not upload the grade again.
        renderer.drawCompare(0);
        const restored = new Uint8Array(rgba.length);
        gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, restored);
        if (restored.some((v, i) => v !== rgba[i])) throw Error('Sampling changed the displayed visualization');
        renderer.draw(test.grade);
        const normalPixel = renderer.samplePixel(0.5, 0.5);
        const normalHistogram = renderer.sample().px;
        if (pixel.some((v, i) => v !== normalPixel[i]) ||
            histogram.some((v, i) => v !== normalHistogram[i])) {
          throw Error('Visualize Spots contaminated color sampling or histogram');
        }
        draw();
      }
      const error = gl.getError();
      if (error) throw Error(`WebGL error ${error}`);
      const debug = gl.getExtension('WEBGL_debug_renderer_info');
      window.drawForCapture = draw;
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
