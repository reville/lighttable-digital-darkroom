// SPDX-License-Identifier: GPL-3.0-only
import { createHash } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { rm } from 'node:fs/promises';
import { Transform, Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

const HOSTS = new Set(['github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com']);
const MAX_BYTES = 4 * 1024 ** 3;

export async function download(asset, destination, { fetchImpl = fetch } = {}) {
  let url = asset.url;
  let response;
  let created = false;
  const signal = AbortSignal.timeout(10 * 60 * 1000);
  try {
    for (let redirects = 0; redirects <= 5; redirects++) {
      const parsed = new URL(url);
      if (parsed.protocol !== 'https:' || !HOSTS.has(parsed.hostname) || parsed.username || parsed.password || (parsed.port && parsed.port !== '443')) throw new Error('Refusing an untrusted LightTable download URL.');
      response = await fetchImpl(url, { redirect: 'manual', signal, headers: { 'User-Agent': 'lighttable-npm-installer' } });
      if ([301, 302, 303, 307, 308].includes(response.status)) {
        const location = response.headers.get('location');
        await response.body?.cancel();
        if (!location || redirects === 5) throw new Error('Too many or invalid LightTable download redirects.');
        url = new URL(location, url).href;
        continue;
      }
      break;
    }
    if (!response.ok || !response.body) throw new Error(`LightTable release download failed (HTTP ${response.status}). The release may not yet be available.`);
    if (Number(response.headers.get('content-length')) > MAX_BYTES) throw new Error('LightTable download exceeds the size limit.');
    const hash = createHash('sha256');
    let bytes = 0;
    const verify = new Transform({ transform(chunk, encoding, done) {
      bytes += chunk.length;
      if (bytes > MAX_BYTES) return done(new Error('LightTable download exceeds the size limit.'));
      hash.update(chunk);
      done(null, chunk);
    } });
    const output = createWriteStream(destination, { flags: 'wx', mode: 0o600 });
    output.once('open', () => { created = true; });
    await pipeline(Readable.fromWeb(response.body), verify, output, { signal });
    if (hash.digest('hex') !== asset.sha256) throw new Error('LightTable download checksum mismatch; nothing was installed.');
  } catch (error) {
    if (response?.body && !response.body.locked) await response.body.cancel().catch(() => {});
    if (created) await rm(destination, { force: true });
    throw error;
  }
}
