import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { context, install, delegate, findInstalled, run } from '../lib/installer.mjs';
import { download } from '../lib/download.mjs';
import { assetName, releaseAsset } from '../lib/release.mjs';
import { parseInstallArgs, main } from '../lib/cli.mjs';
import { prepareRelease } from '../scripts/prepare-release.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const release = { schema: 1, version: '1.2.3', platforms: {
  'darwin-arm64': { sha256: hash('Mac fixture') },
  'win32-x64': { sha256: hash('Windows fixture') },
} };

async function temporary(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'lighttable-npm-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

async function macBundle(directory, content = 'new') {
  await fs.mkdir(path.join(directory, 'Contents', 'MacOS'), { recursive: true });
  await fs.writeFile(path.join(directory, 'Contents', 'MacOS', 'lighttable-cli'), content);
}

async function setup(t, extra = {}) {
  const root = await temporary(t);
  const calls = [];
  const messages = [];
  const ctx = context({ home: path.join(root, 'home'), temp: root,
    applications: path.join(root, 'Applications'), platform: 'darwin', arch: 'arm64', env: {},
    log: message => messages.push(message),
    download: async (asset, destination) => { calls.push(['download', asset]); await fs.writeFile(destination, 'fixture'); },
    run: async (command, args, options) => {
      calls.push([command, args, options]);
      if (command === '/usr/bin/ditto' && args[0] === '-x') await macBundle(path.join(args[3], 'LightTable.app'));
      else if (command === '/usr/bin/ditto') await fs.cp(args[0], args[1], { recursive: true });
      return 0;
    }, ...extra,
  });
  return { root, calls, messages, ctx };
}

test('metadata constructs only the fixed versioned repository URL', () => {
  const asset = releaseAsset(release, 'darwin-arm64');
  assert.equal(asset.url, 'https://github.com/reville/lighttable-digital-darkroom/releases/download/v1.2.3/LightTable-1.2.3-macos-arm64.zip');
  assert.throws(() => assetName('../../escape', 'darwin-arm64'), /Invalid/);
  assert.throws(() => releaseAsset({ ...release, platforms: { 'darwin-arm64': { sha256: '0'.repeat(64) } } }, 'darwin-arm64'), /invalid/);
  assert.throws(() => releaseAsset({ schema: 1, version: null, platforms: {} }, 'darwin-arm64'), /not yet published/);
});

test('unsupported systems and unpublished release never start a download', async t => {
  const { ctx, calls } = await setup(t);
  await assert.rejects(install(release, {}, { ...ctx, platform: 'linux' }), /not supported/);
  await assert.rejects(install({ schema: 1, version: null }, {}, ctx), /not yet published/);
  assert.equal(calls.length, 0);
});

test('Mac install verifies both the extracted and final staged app before placement', async t => {
  const { ctx, calls, root } = await setup(t);
  const result = await install(release, {}, ctx);
  assert.equal(await fs.readFile(path.join(result.destination, 'Contents/MacOS/lighttable-cli'), 'utf8'), 'new');
  assert.equal(calls.filter(call => call[0] === '/usr/bin/codesign').length, 2);
  assert.equal(calls.filter(call => call[0] === '/usr/sbin/spctl').length, 2);
  assert.match(calls.find(call => call[0] === '/usr/bin/codesign')[1].join(' '), /certificate leaf\[field/);
  assert.equal(await findInstalled(ctx), result.destination);
  assert.deepEqual((await fs.readdir(root)).filter(name => name.startsWith('lighttable-download-')), []);
});

test('Mac signature failure leaves the existing app untouched and removes temporary files', async t => {
  const { ctx, root } = await setup(t);
  const old = path.join(ctx.home, 'Applications', 'LightTable.app');
  await macBundle(old, 'old');
  const realRun = ctx.run;
  ctx.run = async (command, ...args) => {
    if (command === '/usr/sbin/spctl') throw new Error('Gatekeeper rejected app');
    return realRun(command, ...args);
  };
  await assert.rejects(install(release, { update: true }, ctx), /Gatekeeper/);
  assert.equal(await fs.readFile(path.join(old, 'Contents/MacOS/lighttable-cli'), 'utf8'), 'old');
  assert.deepEqual((await fs.readdir(root)).filter(name => name.startsWith('lighttable-download-')), []);
});

test('existing /Applications app is reused without download or replacement', async t => {
  const { ctx, calls } = await setup(t);
  const old = path.join(ctx.applications, 'LightTable.app');
  await macBundle(old, 'old');
  assert.deepEqual(await install({ schema: 1, version: null }, {}, ctx), { destination: old, installed: false });
  assert.equal(calls.length, 0);
});

test('Mac explicit update keeps a recoverable prior app', async t => {
  const { ctx } = await setup(t);
  const old = path.join(ctx.home, 'Applications', 'LightTable.app');
  await macBundle(old, 'old');
  await install(release, { update: true }, ctx);
  const backup = (await fs.readdir(path.dirname(old))).find(name => name.startsWith('LightTable.app.backup-'));
  assert.ok(backup);
  assert.equal(await fs.readFile(path.join(path.dirname(old), backup, 'Contents/MacOS/lighttable-cli'), 'utf8'), 'old');
  assert.equal(await fs.readFile(path.join(old, 'Contents/MacOS/lighttable-cli'), 'utf8'), 'new');
});

test('failed Mac placement restores the prior app', async t => {
  const { ctx } = await setup(t);
  const old = path.join(ctx.home, 'Applications', 'LightTable.app');
  await macBundle(old, 'old');
  ctx.fs = { ...fs, rename: async (source, destination) => {
    if (source.includes('.lighttable-install-') && destination === old) throw new Error('placement failed');
    return fs.rename(source, destination);
  } };
  await assert.rejects(install(release, { update: true }, ctx), /placement failed/);
  assert.equal(await fs.readFile(path.join(old, 'Contents/MacOS/lighttable-cli'), 'utf8'), 'old');
});

test('custom Mac install destination is remembered and CLI args remain literal', async t => {
  const { ctx, calls, root } = await setup(t);
  const installDir = path.join(root, 'path with spaces; $(echo nope)');
  const { destination } = await install(release, { installDir }, ctx);
  await delegate(['photos', 'list', '--where', 'path="$(echo nope) & file.jpg"'], ctx);
  const last = calls.at(-1);
  assert.equal(last[0], path.join(destination, 'Contents/MacOS/lighttable-cli'));
  assert.deepEqual(last[1], ['photos', 'list', '--where', 'path="$(echo nope) & file.jpg"']);
});

test('Windows verifies Authenticode before starting per-user installer', async t => {
  const { ctx, calls, root } = await setup(t);
  Object.assign(ctx, { platform: 'win32', arch: 'x64', env: { LOCALAPPDATA: root } });
  const destination = path.join(root, 'Programs', 'LightTable');
  ctx.run = async (command, args, options) => {
    calls.push([command, args, options]);
    if (command.endsWith('-setup.exe')) {
      await fs.mkdir(path.join(destination, 'Python'), { recursive: true });
      await fs.writeFile(path.join(destination, 'Python', 'python.exe'), 'new');
    }
  };
  await install(release, {}, ctx);
  assert.equal(calls[1][0], 'powershell.exe');
  assert.match(calls[1][2].env.LIGHTTABLE_INSTALLER_VERIFY_PATH, /-setup.exe$/);
  assert.deepEqual(calls[2][1], ['/S']);
  await delegate(['edit', 'set', 'a & b $(no).jpg'], ctx);
  assert.equal(calls.at(-1)[0], path.join(destination, 'Python', 'python.exe'));
  assert.deepEqual(calls.at(-1)[1], ['-B', '-m', 'lighttable_cli', 'edit', 'set', 'a & b $(no).jpg']);
  assert.equal(calls.at(-1)[2].cwd, path.join(destination, 'Resources', 'LightTable'));
});

test('Windows rejects an unsigned installer without executing it', async t => {
  const { ctx, calls, root } = await setup(t);
  Object.assign(ctx, { platform: 'win32', arch: 'x64', env: { LOCALAPPDATA: root } });
  ctx.run = async command => { calls.push([command]); throw new Error('untrusted Authenticode signature'); };
  await assert.rejects(install(release, {}, ctx), /Authenticode/);
  assert.equal(calls.length, 2);
  assert.equal(calls[1][0], 'powershell.exe');
  await assert.rejects(install(release, { installDir: root }, ctx), /macOS only/);
});

test('failed Windows update restores a prior application', async t => {
  const { ctx, root } = await setup(t);
  Object.assign(ctx, { platform: 'win32', arch: 'x64', env: { LOCALAPPDATA: root } });
  const destination = path.join(root, 'Programs', 'LightTable');
  await fs.mkdir(path.join(destination, 'Python'), { recursive: true });
  await fs.writeFile(path.join(destination, 'Python', 'python.exe'), 'old');
  ctx.run = async command => {
    if (command.endsWith('-setup.exe')) {
      await fs.writeFile(path.join(destination, 'Python', 'python.exe'), 'partial');
      throw new Error('installer failed');
    }
  };
  await assert.rejects(install(release, { update: true }, ctx), /installer failed/);
  assert.equal(await fs.readFile(path.join(destination, 'Python', 'python.exe'), 'utf8'), 'old');
  assert.ok((await fs.readdir(path.dirname(destination))).some(name => name.startsWith('LightTable.failed-')));
});

test('downloads stream through a local mock transport and require the shipped checksum', async t => {
  const root = await temporary(t);
  const server = http.createServer((request, response) => response.end('Mac fixture'));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const fetchImpl = (url, options) => fetch(`http://127.0.0.1:${server.address().port}`, options);
  const destination = path.join(root, 'download.zip');
  await download(releaseAsset(release, 'darwin-arm64'), destination, { fetchImpl });
  assert.equal(await fs.readFile(destination, 'utf8'), 'Mac fixture');
  const failed = path.join(root, 'wrong.zip');
  await assert.rejects(download({ ...releaseAsset(release, 'darwin-arm64'), sha256: hash('wrong') }, failed, { fetchImpl }), /checksum mismatch/);
  await assert.rejects(fs.stat(failed), { code: 'ENOENT' });
});

test('downloads reject untrusted redirect hosts and missing releases', async t => {
  const root = await temporary(t);
  const asset = releaseAsset(release, 'darwin-arm64');
  let count = 0;
  await assert.rejects(download(asset, path.join(root, 'bad.zip'), { fetchImpl: async () => {
    count++;
    return new Response(null, { status: 302, headers: { location: 'https://example.com/evil.zip' } });
  } }), /untrusted/);
  assert.equal(count, 1);
  await assert.rejects(download(asset, path.join(root, '404.zip'), { fetchImpl: async () => new Response('missing', { status: 404 }) }), /HTTP 404/);
  assert.deepEqual(await fs.readdir(root), []);
});

test('download failure does not delete a preexisting destination', async t => {
  const root = await temporary(t);
  const destination = path.join(root, 'existing.zip');
  await fs.writeFile(destination, 'previous');
  await assert.rejects(download(releaseAsset(release, 'darwin-arm64'), destination, { fetchImpl: async () => new Response('Mac fixture') }), { code: 'EEXIST' });
  assert.equal(await fs.readFile(destination, 'utf8'), 'previous');
});

test('preparation hashes only actual versioned assets and copies the project license', async t => {
  const root = await temporary(t);
  const directory = path.join(root, 'packaging', 'npm');
  await fs.mkdir(directory, { recursive: true });
  await fs.writeFile(path.join(directory, 'package.json'), JSON.stringify({ name: 'lighttable', version: '0.0.0-unreleased' }));
  await fs.writeFile(path.join(root, 'LICENSE'), 'test license fixture');
  await assert.rejects(prepareRelease('1.2.3', root, directory), /No actual/);
  await fs.writeFile(path.join(root, assetName('1.2.3', 'darwin-arm64')), 'Mac fixture');
  const metadata = await prepareRelease('1.2.3', root, directory);
  assert.deepEqual(metadata.platforms, { 'darwin-arm64': { sha256: hash('Mac fixture') } });
  assert.equal(JSON.parse(await fs.readFile(path.join(directory, 'package.json'), 'utf8')).version, '1.2.3');
  assert.equal(await fs.readFile(path.join(directory, 'LICENSE'), 'utf8'), 'test license fixture');
  assert.throws(() => releaseAsset(metadata, 'win32-x64'), /does not include/);
});

test('installer options validate missing values and unknown flags', () => {
  assert.deepEqual(parseInstallArgs(['--update', '--install-dir', '/tmp/A B']), { update: true, installDir: '/tmp/A B' });
  assert.throws(() => parseInstallArgs(['--install-dir', '--update']), /requires a directory/);
  assert.throws(() => parseInstallArgs(['--ignore-signature']), /Unknown installer option/);
});

test('help works before release publication without any download', async t => {
  const { ctx, calls, messages } = await setup(t);
  await main(['install', '--help'], ctx);
  await main(['--help'], ctx);
  assert.equal(calls.length, 0);
  assert.equal(messages.length, 2);
  assert.match(messages[0], /does not download/);
});

test('process runner forwards arguments without a shell and preserves failure codes', async t => {
  const root = await temporary(t);
  const output = path.join(root, 'literal.json');
  const argument = 'photo $(echo SHOULD_NOT_RUN); & *.jpg';
  await run(process.execPath, ['-e', 'require("fs").writeFileSync(process.argv[1], JSON.stringify(process.argv[2]))', output, argument]);
  assert.equal(JSON.parse(await fs.readFile(output, 'utf8')), argument);
  await assert.rejects(run(process.execPath, ['-e', 'process.exit(7)']), error => error.exitCode === 7);
});

test('optional lifecycle hook skips all installation when requested', async () => {
  await run(process.execPath, [fileURLToPath(new URL('../scripts/postinstall.mjs', import.meta.url))], {
    env: { ...process.env, LIGHTTABLE_SKIP_DOWNLOAD: '1' }, stdio: 'pipe',
  });
});
