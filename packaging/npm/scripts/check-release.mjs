// SPDX-License-Identifier: GPL-3.0-only
import { readFile } from 'node:fs/promises';
import { PLATFORMS, releaseAsset } from '../lib/release.mjs';

try {
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  const metadata = JSON.parse(await readFile(new URL('../release.json', import.meta.url), 'utf8'));
  if (!metadata.version || metadata.version !== pkg.version) throw new Error('Prepare this npm package from real versioned desktop release assets before publishing.');
  if (!Object.keys(metadata.platforms ?? {}).length) throw new Error('No verified desktop release assets are recorded.');
  for (const platform of Object.keys(metadata.platforms)) {
    if (!PLATFORMS[platform]) throw new Error(`Unknown release platform: ${platform}`);
    releaseAsset(metadata, platform);
  }
  if (!pkg.license || pkg.license === 'UNLICENSED') throw new Error('Set the package license only after the project owner has chosen the distribution license.');
  await readFile(new URL('../LICENSE', import.meta.url), 'utf8');
  console.log(`LightTable ${pkg.version} npm release metadata is ready.`);
} catch (error) { console.error(error.message); process.exitCode = 1; }
