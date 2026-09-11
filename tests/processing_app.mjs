// SPDX-License-Identifier: GPL-3.0-only
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
  let delayedEditResponses = 0;
  await page.route('**/api/render', async route => {
    const request = route.request().postDataJSON();
    const delayFilm = request?.params?.print_exposure === 1.6 && request.name.endsWith('a.png');
    const delayEdit = request?.name?.endsWith('edit-flat.png') && request.grade?.exposure === 1 &&
      request.masks?.some(mask => mask.grade?.texture === 1);
    if (delayFilm || delayEdit) {
      const response = await route.fetch();
      if (delayFilm) delayedFilmResponses++;
      if (delayEdit) delayedEditResponses++;
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
    const drawCompare = GradeRenderer.prototype.drawCompare;
    const setImage = GradeRenderer.prototype.setImage;
    GradeRenderer.prototype.setImage = function(image, ...args) {
      this.processingSourceURL = image.currentSrc || image.src;
      return setImage.call(this, image, ...args);
    };
    window.processingFrames = 0;
    function captureFrame() {
      if (this.canvas.id !== 'cv' || !this.ready) return;
      window.processingRenderer = this;
      const gl = this.gl;
      if (gl.getParameter(gl.FRAMEBUFFER_BINDING) !== null) return;
      const pixels = new Uint8Array(this.canvas.width * this.canvas.height * 4);
      gl.readPixels(0, 0, this.canvas.width, this.canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      if (gl.getError()) throw Error('App framebuffer readback failed');
      window.processingFrame = {width: this.canvas.width, height: this.canvas.height,
        pixels: Array.from(pixels), grade: structuredClone(this.processingGrade), frame:processingFrames + 1,
        sourceURL: this.processingSourceURL};
      window.processingFrames++;
    }
    GradeRenderer.prototype.draw = function(...args) {
      draw.apply(this, args);
      this.processingGrade = args[0];
      captureFrame.call(this);
    };
    // An on-demand Original can arrive after the grade frame. Capture the
    // actual comparison presentation too, without drawing a test-only frame.
    GradeRenderer.prototype.drawCompare = function(...args) {
      const result = drawCompare.apply(this, args);
      captureFrame.call(this);
      return result;
    };
  });
  const results = [];
  const post = async (path, body) => page.evaluate(async ({path, body}) => {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok || result.error || result.ok === false) throw Error(JSON.stringify(result));
    return result;
  }, {path, body});
  const waitForRecipe = async (name, recipe) => {
    const render = await page.evaluate(() => __lightTablePerf.renders.at(-1));
    const response = await page.request.post(config.baseUrl + '/api/render', {
      data:{name, ...recipe, w:render.width, engine:render.engine, native:false}, timeout:120000});
    if (!response.ok()) throw Error(await response.text());
    const base = await response.json();
    if (!base.img) throw Error('Expected browser source surface');
    const source = new URL(base.img, config.baseUrl).href;
    const grade = {exposure:0, sharpness:0, ...(base.gradeEditsBaked ? {} : recipe.grade)};
    // A navigation or prior edit can complete on the same photo while the
    // requested recipe is pending. Match the texture actually uploaded by the
    // app, then verify the finishing grade applied to that particular source.
    await page.waitForFunction(({source, grade}) => processingFrame.sourceURL === source &&
      Object.entries(grade).every(([key, value]) =>
        Math.abs(processingFrame.grade[key] - value) < 0.0001), {source, grade}, {timeout:120000});
  };
  const captureBefore = async (test, frame, name) => {
    await page.evaluate(() => document.activeElement?.blur());
    const beforeHold = await page.evaluate(() => processingFrames);
    await page.keyboard.down('b');
    await page.waitForFunction(() => processingRenderer.originalReady, null, {timeout:30000});
    await page.waitForFunction(before => processingFrames > before, beforeHold);
    const held = await page.evaluate(() => processingFrame);
    fs.writeFileSync(test.beforeRaw, Buffer.from(held.pixels));
    const original = await page.request.post(config.baseUrl + '/api/render/file',
      {data:{name, w:frame.width, format:'png', before:true, state:{params:test.params}}, timeout:120000});
    if (!original.ok()) throw Error(`Original reference: ${await original.text()}`);
    fs.writeFileSync(test.beforeReference, await original.body());
    const beforeRelease = await page.evaluate(() => processingFrames);
    await page.keyboard.up('b');
    await page.waitForFunction(before => processingFrames > before, beforeRelease);
    fs.writeFileSync(test.releasedRaw, Buffer.from(await page.evaluate(() => processingFrame.pixels)));
  };
  for (const [index, test] of config.cases.entries()) {
    process.stdout.write(JSON.stringify({event:'case', name:test.name}) + '\n');
    // Catalog enumeration order varies with metadata availability. Select the
    // named fixtures so the baseline always covers color detail and portrait
    // navigation, and the delayed render targets the intended photo.
    const filename = test.photoName || (test.photo === 1 ? 'b.png' : 'a.png');
    const name = images.find(image => image.name === filename ||
      image.name.endsWith(':' + filename))?.name;
    if (!name) throw Error('Missing fixture photo');
    await post('/api/ui/command', {command:'goto', args:{name}});
    await page.waitForFunction(name => __lightTablePerf.renders.at(-1)?.image === name &&
      !document.querySelector('#zoomwrap').classList.contains('photo-pending'), name, {timeout:120000});
    if (test.edits) {
      const recipe = {params:test.params, grade:test.grade || {}, masks:test.masks || [],
        heals:test.heals || [], optics:test.optics || {}, crop:null};
      if (!test.clearMasks) {
        // External state uses the same supported route as the CLI; its event
        // updates the open app. Do not inject internal S or draw a test renderer.
        const accepted = await page.request.post(config.baseUrl + '/api/state', {
          headers:{Origin:config.baseUrl}, data:{name, ...recipe, origin:'processing-regression'}});
        if (!accepted.ok()) throw Error(await accepted.text());
        // A grade-only patch redraws WebGL without another physical render.
        // Match the displayed source and grade before operating its sliders;
        // a pending navigation render must not make this wait pass by chance.
        await waitForRecipe(name, recipe);
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
      for (const [key, value] of Object.entries(test.gradeSliders || {})) {
        const slider = page.locator(`[data-g="${key}"]`);
        await slider.evaluate(element => { element.closest('details').open = true; });
        await slider.scrollIntoViewIfNeeded();
        await slider.evaluate((element, value) => { element.value = String(value); }, value);
        await slider.dispatchEvent('input');
        await slider.dispatchEvent('change');
        recipe.grade[key] = value;
        await page.waitForFunction(async ({name, key, value}) => {
          const saved = await (await fetch(`/api/state?name=${encodeURIComponent(name)}`)).json();
          return saved.grade?.[key] === value && processingFrame?.grade?.[key] === value;
        }, {name, key, value}, {timeout:15000});
      }
      await waitForRecipe(name, recipe);
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
      await captureBefore(test, frame, name);
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
    await waitForRecipe(name, {params:test.params, grade:{exposure:test.exposure}});
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
    await captureBefore(test, frame, name);
    results.push({name:test.name, photo:name, bounds, ...frame});
  }
  if (!delayedFilmResponses) throw Error('Slow physical-render regression was not exercised');
  if (!delayedEditResponses) throw Error('Slow edited response regression was not exercised');
  fs.writeFileSync(config.result, JSON.stringify({records:results, delayedFilmResponses, delayedEditResponses}));
} finally { await browser.close(); }
