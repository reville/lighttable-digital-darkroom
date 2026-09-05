#!/usr/bin/env node
import * as fs from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assetName, PLATFORMS, VERSION_RE } from '../lib/release.mjs';

export async function prepareRelease(version, artifactDirectory, packageDirectory = fileURLToPath(new URL('..', import.meta.url))) {
  if (!VERSION_RE.test(version ?? '') || !artifactDirectory) throw new Error('Usage: node scripts/prepare-release.mjs VERSION ARTIFACT_DIRECTORY');
  const metadata = { schema: 1, version, platforms: {} };
  for (const platform of Object.keys(PLATFORMS)) {
    const file = path.join(artifactDirectory, assetName(version, platform));
    try {
      const stat = await fs.stat(file);
      if (!stat.isFile() || !stat.size) throw new Error(`Release asset must be a nonempty file: ${file}`);
    } catch (error) { if (error.code === 'ENOENT') continue; throw error; }
    const hash = createHash('sha256');
    for await (const chunk of createReadStream(file)) hash.update(chunk);
    metadata.platforms[platform] = { sha256: hash.digest('hex') };
  }
  if (!Object.keys(metadata.platforms).length) throw new Error('No actual versioned LightTable release assets found; refusing to prepare an npm release.');
  const packageFile = path.join(packageDirectory, 'package.json');
  const pkg = JSON.parse(await fs.readFile(packageFile, 'utf8'));
  await fs.copyFile(path.join(packageDirectory, '..', '..', 'LICENSE'), path.join(packageDirectory, 'LICENSE'));
  pkg.version = version;
  await fs.writeFile(path.join(packageDirectory, 'release.json'), `${JSON.stringify(metadata, null, 2)}\n`);
  await fs.writeFile(packageFile, `${JSON.stringify(pkg, null, 2)}\n`);
  return metadata;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 4) throw new Error('Usage: node scripts/prepare-release.mjs VERSION ARTIFACT_DIRECTORY');
    const metadata = await prepareRelease(process.argv[2], process.argv[3]);
    console.log(`Prepared LightTable ${metadata.version}: ${Object.keys(metadata.platforms).join(', ')}. Publish these exact assets to GitHub before npm publication.`);
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
