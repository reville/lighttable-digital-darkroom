// SPDX-License-Identifier: GPL-3.0-only
import { readFile } from 'node:fs/promises';
import { context, delegate, findInstalled, install } from './installer.mjs';

export const HELP = `LightTable desktop installer and command-line interface

  lighttable install                   Install this package's desktop release
  lighttable install --update          Replace an existing app, keeping a backup
  lighttable install --install-dir DIR Install into DIR on macOS
  lighttable install --help            Show installer help
  lighttable [CLI arguments]           Run the installed app's bundled CLI

macOS default: ~/Applications/LightTable.app (uses /Applications if installed)
Windows default: %LOCALAPPDATA%\\Programs\\LightTable
Supported: macOS Apple silicon and Windows x64, Node.js 20 or newer.
Installing this npm package alone does not download the desktop app.
`;

export function parseInstallArgs(args) {
  const options = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--update') options.update = true;
    else if (args[i] === '--install-dir') {
      if (!args[i + 1] || args[i + 1].startsWith('--')) throw new Error('--install-dir requires a directory.');
      options.installDir = args[++i];
    } else throw new Error(`Unknown installer option: ${args[i]}. Run lighttable install --help.`);
  }
  return options;
}

export async function main(args, ctx = context()) {
  if (args[0] === 'install') {
    if (args.includes('--help') || args.includes('-h')) { ctx.log(HELP); return; }
    const options = parseInstallArgs(args.slice(1));
    const metadata = JSON.parse(await readFile(new URL('../release.json', import.meta.url), 'utf8'));
    return install(metadata, options, ctx);
  }
  if ((!args.length || (args.length === 1 && ['--help', '-h'].includes(args[0]))) && !await findInstalled(ctx)) {
    ctx.log(HELP);
    return;
  }
  return delegate(args, ctx);
}
