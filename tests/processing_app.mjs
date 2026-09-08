// End-to-end: real server, real app UI, presented WebGL frame, CLI render API.
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {chromium} = await import(pathToFileURL(config.module).href);
const browser = await chromium.launch({headless: true, executablePath: config.browser || undefined,
  args: ['--enable-unsafe-swiftshader', '--force-color-profile=srgb']});
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, async () => {await browser.close(); process.exit(1);});
try {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}, deviceScaleFactor: 1});
  let delayedFilmResponses = 0;
  await page.route('**/api/render', async route => {
    const request = route.request().postDataJSON();
    if (request?.params?.print_exposure === 1.6 && request.name.endsWith('a.png')) {
      const response = await route.fetch();
      delayedFilmResponses++;
      await new Promise(resolve => setTimeout(resolve, 750));
      await route.fulfill({response});
    } else await route.continue();
  });
  // A separate reference page sends the captured RGB8 pixels through the same
  // browser compositor. This avoids approximating fractional CSS transforms
  // with an integer-sized Pillow resize. It is a reference, never app proof.
  const displayReference = await browser.newPage({viewport: {width: 1440, height: 1000}, deviceScaleFactor: 1});
  await displayReference.setContent('<style>body{margin:0}canvas{position:absolute;display:block}</style><canvas></canvas>');
  const captureDisplay = async (test, frame) => {
    await page.screenshot({path:test.screenshot});
    const canvas = page.locator('#cv');
    if (!await canvas.isVisible()) throw Error('App preview canvas is hidden');
    const bounds = await canvas.boundingBox();
    await canvas.screenshot({path:test.display, timeout:15000});
    const background = await canvas.evaluate(element => {
      for (let parent = element.parentElement; parent; parent = parent.parentElement) {
        const color = getComputedStyle(parent).backgroundColor;
        if (color !== 'rgba(0, 0, 0, 0)' && color !== 'transparent') return color;
      }
      return 'white';
    });
    await displayReference.evaluate(({width, height, pixels, bounds, background}) => {
      const canvas = document.querySelector('canvas');
      canvas.width = width; canvas.height = height;
      Object.assign(canvas.style, {left:bounds.x+'px', top:bounds.y+'px',
        width:bounds.width+'px', height:bounds.height+'px'});
      document.body.style.background = background;
      const rgba = new Uint8ClampedArray(pixels.length);
      for (let y = 0; y < height; y++) {
        rgba.set(pixels.slice((height-y-1)*width*4, (height-y)*width*4), y*width*4);
      }
      canvas.getContext('2d', {alpha:false}).putImageData(new ImageData(rgba, width, height), 0, 0);
    }, {width:frame.width, height:frame.height, pixels:Array.from(fs.readFileSync(test.raw)), bounds, background});
    await displayReference.locator('canvas').screenshot({path:test.displayReference, timeout:15000});
    return bounds;
  };
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/state') {
      const state = request.postDataJSON();
      process.stdout.write(JSON.stringify({event:'save', name:state.name,
        film:state.params?.profile_enabled, print:state.params?.print_exposure,
        exposure:state.grade?.exposure}) + '\n');
    }
  });
  // Establish the normal HttpOnly instance cookie without starting app scripts.
  await page.request.get(config.baseUrl);
  const images = (await (await page.request.get(config.baseUrl + '/api/images')).json()).images;
  if (!images || images.length !== 6) throw Error('Expected exactly six isolated fixture photos');
  for (const image of images) {
    const response = await page.request.post(config.baseUrl + '/api/state', {headers:{Origin:config.baseUrl}, data:{name:image.name,
      params:config.cases[0].params, grade:{}, masks:[], heals:[], optics:{}, crop:null}});
    if (!response.ok()) throw Error(await response.text());
  }
  await page.goto(config.baseUrl, {waitUntil: 'domcontentloaded'});
  await page.waitForFunction(() => window.__lightTablePerf?.renders.length > 0, null, {timeout: 120000});
  await page.evaluate(async () => {
    const {GradeRenderer} = await import('/web/gl.js');
    const draw = GradeRenderer.prototype.draw;
    window.processingFrames = 0;
    GradeRenderer.prototype.draw = function(...args) {
      draw.apply(this, args);
      if (this.canvas.id !== 'cv' || !this.ready) return;
      const gl = this.gl;
      if (gl.getParameter(gl.FRAMEBUFFER_BINDING) !== null) return;
      const pixels = new Uint8Array(this.canvas.width * this.canvas.height * 4);
      gl.readPixels(0, 0, this.canvas.width, this.canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      if (gl.getError()) throw Error('App framebuffer readback failed');
      window.processingFrame = {width: this.canvas.width, height: this.canvas.height,
        pixels: Array.from(pixels), grade: structuredClone(args[0]), frame:processingFrames + 1};
      window.processingFrames++;
    };
  });
  const results = [];
  const post = async (path, body) => page.evaluate(async ({path, body}) => {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok || result.error || result.ok === false) throw Error(JSON.stringify(result));
    return result;
  }, {path, body});
  for (const [index, test] of config.cases.entries()) {
    process.stdout.write(JSON.stringify({event:'case', name:test.name}) + '\n');
    const name = test.photoName ? images.find(image => image.name.endsWith(test.photoName))?.name
      : images[test.photo || 0].name;
    if (!name) throw Error('Missing fixture photo');
    await post('/api/ui/command', {command:'goto', args:{name}});
    await page.waitForFunction(name => __lightTablePerf.renders.at(-1)?.image === name &&
      !document.querySelector('#zoomwrap').classList.contains('photo-pending'), name, {timeout:120000});
    if (test.edits) {
      const recipe = {params:test.params, grade:test.grade || {}, masks:test.masks || [],
        heals:test.heals || [], optics:test.optics || {}, crop:null};
      if (!test.clearMasks) {
        const beforeRender = await page.evaluate(() => __lightTablePerf.renders.length);
        // External state uses the same supported route as the CLI; its event
        // updates the open app. Do not inject internal S or draw a test renderer.
        const accepted = await page.request.post(config.baseUrl + '/api/state', {
          headers:{Origin:config.baseUrl}, data:{name, ...recipe, origin:'processing-regression'}});
        if (!accepted.ok()) throw Error(await accepted.text());
        await page.waitForFunction(({count, name}) => __lightTablePerf.renders.length > count &&
          __lightTablePerf.renders.at(-1).image === name &&
          document.querySelector('#rstat').className !== 'busy', {count:beforeRender, name}, {timeout:120000});
      }
      if (test.sliderExposure != null || test.clearMasks) {
        const count = await page.evaluate(() => __lightTablePerf.renders.length);
        if (test.clearMasks) {
          await post('/api/ui/command', {command:'resetMasks'});
          recipe.masks = [];
        } else {
          await post('/api/ui/command', {command:'slider', args:{key:'exposure', value:test.sliderExposure}});
          recipe.grade.exposure = test.sliderExposure;
        }
        await page.waitForFunction(count => __lightTablePerf.renders.length > count &&
          document.querySelector('#rstat').className !== 'busy', count, {timeout:120000});
      }
      // doRender queues drawGrade on the animation scheduler before recording
      // its timing. Let that actual presentation run before reading its frame.
      await page.evaluate(() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const frame = await page.evaluate(() => processingFrame);
      fs.writeFileSync(test.raw, Buffer.from(frame.pixels)); delete frame.pixels;
      const reference = await page.request.post(config.baseUrl + '/api/render/file',
        {data:{name, w:frame.width, format:'png', state:recipe}, timeout:120000});
      if (!reference.ok()) throw Error(await reference.text());
      fs.writeFileSync(test.reference, await reference.body());
      const bounds = await captureDisplay(test, frame);
      results.push({name:test.name, photo:name, bounds, ...frame});
      continue;
    }
    const filmEnabled = await page.locator('#filmProfileToggle').getAttribute('aria-checked');
    const physicalChange = async command => {
      const count = await page.evaluate(() => __lightTablePerf.renders.length);
      await post('/api/ui/command', command);
      await page.waitForFunction(count => __lightTablePerf.renders.length > count &&
        !document.querySelector('#rstat').classList.contains('busy'), count, {timeout:120000});
      await page.evaluate(() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve))));
    };
    // A finishing slider can draw against the preceding film base while the
    // physical render is pending. A saved recipe or ready-photo flag alone
    // does not prove that the new film/print pixels have reached the screen.
    if (filmEnabled !== String(test.params.profile_enabled)) {
      await physicalChange({command:'filmToggle'});
    }
    if (Number(await page.locator('#print_exposure').inputValue()) !== test.params.print_exposure) {
      await physicalChange({command:'slider', args:{key:'print_exposure', value:test.params.print_exposure}});
    }
    // Actual slider event takes the normal app UI -> grade -> preview -> save route.
    const before = await page.evaluate(() => processingFrames);
    await post('/api/ui/command', {command:'slider', args:{key:'exposure', value:test.exposure}});
    await page.waitForFunction(({before, exposure}) => processingFrames > before &&
      Math.abs(processingFrame.grade.exposure - exposure) < 0.0001 &&
      !document.querySelector('#zoomwrap').classList.contains('photo-pending'),
      {before, exposure:test.exposure}, {timeout:120000});
    // Poll authoritative saved state, so the CLI reference uses the accepted recipe.
    let saved;
    for (let attempt = 0; attempt < 120; attempt++) {
      saved = await page.evaluate(async name => (await (await fetch(`/api/state?name=${encodeURIComponent(name)}`)).json()), name);
      if (Math.abs(saved.grade?.exposure - test.exposure) < 0.0001 &&
          Object.entries(test.params).every(([key,value]) => JSON.stringify(saved.params?.[key]) === JSON.stringify(value))) break;
      await page.waitForTimeout(250);
    }
    if (Math.abs(saved.grade?.exposure - test.exposure) >= 0.0001) throw Error('Grade did not persist');
    for (const [key, value] of Object.entries(test.params)) {
      if (JSON.stringify(saved.params[key]) !== JSON.stringify(value)) throw Error(`Recipe drift: ${key} expected ${JSON.stringify(value)}, got ${JSON.stringify(saved.params[key])}`);
    }
    const state = await post('/api/ui/command', {command:'slider', args:{key:'exposure', value:test.exposure}});
    const ui = state.result?.result || state.result || state;
    if (ui.current !== name || ui.render?.name !== name || ui.render?.state !== 'ready' || ui.render?.backend !== 'webgl') {
      throw Error(`Wrong/stale app presentation: ${JSON.stringify(state)}`);
    }
    const frame = await page.evaluate(() => processingFrame);
    fs.writeFileSync(test.raw, Buffer.from(frame.pixels));
    delete frame.pixels;
    // This is the real CLI render endpoint, which runs Python postprocessing.
    const reference = await page.request.post(config.baseUrl + '/api/render/file',
      {data:{name, w:frame.width, format:'png', state:{params:test.params,
        grade:{exposure:test.exposure}, masks:[], heals:[], optics:{}, crop:null}}, timeout:120000});
    if (!reference.ok()) throw Error(`CLI reference: ${await reference.text()}`);
    fs.writeFileSync(test.reference, await reference.body());
    const bounds = await captureDisplay(test, frame);
    results.push({name:test.name, photo:name, bounds, ...frame});
  }
  if (!delayedFilmResponses) throw Error('Slow physical-render regression was not exercised');
  fs.writeFileSync(config.result, JSON.stringify({records:results, delayedFilmResponses}));
} finally { await browser.close(); }
