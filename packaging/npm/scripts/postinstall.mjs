// SPDX-License-Identifier: GPL-3.0-only
// Optional lifecycle hook. It is deliberately not enabled in package.json.
import { readFile } from 'node:fs/promises';
import { context, install } from '../lib/installer.mjs';

if (process.env.LIGHTTABLE_SKIP_DOWNLOAD && process.env.LIGHTTABLE_SKIP_DOWNLOAD !== '0') {
  console.log('LightTable: desktop download skipped (LIGHTTABLE_SKIP_DOWNLOAD).');
} else {
  try {
    const metadata = JSON.parse(await readFile(new URL('../release.json', import.meta.url), 'utf8'));
    await install(metadata, {}, context());
  } catch (error) {
    console.error(`LightTable: ${error.message}`);
    process.exitCode = 1;
  }
}
