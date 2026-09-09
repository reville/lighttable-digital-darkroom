import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const shell = readFileSync(new URL('../windows-shell/src/main.rs', import.meta.url), 'utf8');
const commands = shell.slice(shell.indexOf('fn handle_command('), shell.indexOf('\nfn folder_dialog_response('));
const supported = new Set([...commands.matchAll(/^\s*((?:"[^"\n]+"\s*\|\s*)*"[^"\n]+")\s*=>/gm)]
  .flatMap(match => [...match[1].matchAll(/"([^"\n]+)"/g)].map(value => value[1])));

test('shared export, backup and recovery buttons have Windows/Linux native command handlers', () => {
  const required = new Map([
    ['chooseExportFolder', 'app.js'],
    ['choosePreferenceFolder', 'settings.js'],
    ['showServerLog', 'recovery.js'],
    ['openRecoveryFolder', 'recovery.js'],
  ]);
  for (const [action, file] of required) {
    const source = readFileSync(new URL(`../web/${file}`, import.meta.url), 'utf8');
    assert(source.includes(`('${action}'`), `${file} no longer sends ${action}; reevaluate this contract`);
    assert(supported.has(action), `${action} is sent by shared UI but silently ignored by the Windows/Linux host`);
  }
});
