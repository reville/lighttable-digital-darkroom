import * as fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { download } from './download.mjs';
import { PLATFORMS, releaseAsset } from './release.mjs';

export function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { shell: false, stdio: 'inherit', ...options });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (code === 0) return resolve(0);
      const error = new Error(`${path.basename(command)} failed (${signal ?? code}).`);
      error.exitCode = code ?? 1;
      reject(error);
    });
  });
}

export function context(overrides = {}) {
  const platform = overrides.platform ?? process.platform;
  const env = overrides.env ?? process.env;
  const home = overrides.home ?? os.homedir();
  return {
    platform, arch: overrides.arch ?? process.arch, env, home,
    fs, run, download, log: console.log, temp: os.tmpdir(),
    applications: '/Applications',
    ...overrides,
    stateFile: overrides.stateFile ?? (platform === 'win32'
      ? path.join(env.LOCALAPPDATA ?? path.join(home, 'AppData', 'Local'), 'LightTable', 'npm-install.json')
      : path.join(home, '.config', 'lighttable', 'npm-install.json')),
  };
}

async function exists(file, ctx) {
  try { await ctx.fs.lstat(file); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

export function defaultDestination(ctx) {
  if (ctx.platform === 'darwin') return path.join(ctx.home, 'Applications', 'LightTable.app');
  if (ctx.platform === 'win32') return path.join(ctx.env.LOCALAPPDATA ?? path.join(ctx.home, 'AppData', 'Local'), 'Programs', 'LightTable');
  throw new Error(`LightTable is not supported on ${ctx.platform}-${ctx.arch}.`);
}

export async function findInstalled(ctx) {
  const candidates = [];
  try {
    const state = JSON.parse(await ctx.fs.readFile(ctx.stateFile, 'utf8'));
    if (typeof state.destination === 'string' && path.isAbsolute(state.destination)) candidates.push(state.destination);
  } catch (error) { if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error; }
  if (ctx.platform === 'darwin') candidates.push(path.join(ctx.applications, 'LightTable.app'));
  if (PLATFORMS[`${ctx.platform}-${ctx.arch}`]) candidates.push(defaultDestination(ctx));
  for (const destination of candidates) {
    const entry = ctx.platform === 'darwin' ? path.join(destination, 'Contents', 'MacOS', 'lighttable-cli') : path.join(destination, 'Python', 'python.exe');
    if (await exists(entry, ctx)) return destination;
  }
  return null;
}

async function remember(destination, ctx) {
  await ctx.fs.mkdir(path.dirname(ctx.stateFile), { recursive: true });
  const temporary = `${ctx.stateFile}.${randomUUID()}.tmp`;
  try {
    await ctx.fs.writeFile(temporary, `${JSON.stringify({ destination }, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
    await ctx.fs.rename(temporary, ctx.stateFile);
  } finally { await ctx.fs.rm(temporary, { force: true }); }
}

async function verifyMac(bundle, ctx) {
  const requirement = 'identifier "com.reville.lighttable" and anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists';
  await ctx.run('/usr/bin/codesign', ['--verify', '--deep', '--strict', '-R', requirement, bundle]);
  await ctx.run('/usr/sbin/spctl', ['--assess', '--type', 'execute', '--verbose=2', bundle]);
  if (!await exists(path.join(bundle, 'Contents', 'MacOS', 'lighttable-cli'), ctx)) throw new Error('The verified app is missing its bundled command-line interface.');
}

async function installMac(archive, destination, update, work, ctx) {
  const extracted = path.join(work, 'extracted');
  await ctx.fs.mkdir(extracted);
  await ctx.run('/usr/bin/ditto', ['-x', '-k', archive, extracted]);
  const bundle = path.join(extracted, 'LightTable.app');
  await verifyMac(bundle, ctx);
  await ctx.fs.mkdir(path.dirname(destination), { recursive: true });
  const staging = await ctx.fs.mkdtemp(path.join(path.dirname(destination), '.lighttable-install-'));
  const stagedBundle = path.join(staging, 'LightTable.app');
  let backup;
  try {
    await ctx.run('/usr/bin/ditto', [bundle, stagedBundle]);
    await verifyMac(stagedBundle, ctx);
    if (await exists(destination, ctx)) {
      if (!update) throw new Error('LightTable already exists at this location. Use lighttable install --update to replace it.');
      backup = `${destination}.backup-${randomUUID()}`;
      await ctx.fs.rename(destination, backup);
    }
    try { await ctx.fs.rename(stagedBundle, destination); }
    catch (error) {
      if (backup) await ctx.fs.rename(backup, destination);
      throw error;
    }
    if (backup) ctx.log(`Previous app preserved at ${backup}`);
  } finally { await ctx.fs.rm(staging, { recursive: true, force: true }); }
}

async function installWindows(installer, destination, update, ctx) {
  await ctx.run('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    "$signature = Get-AuthenticodeSignature -LiteralPath $env:LIGHTTABLE_INSTALLER_VERIFY_PATH; if ($signature.Status -ne 'Valid') { Write-Error 'LightTable installer does not have a valid trusted Authenticode signature'; exit 1 }"],
  { env: { ...ctx.env, LIGHTTABLE_INSTALLER_VERIFY_PATH: installer } });
  let backup;
  if (await exists(destination, ctx)) {
    if (!update) throw new Error('LightTable already exists. Use lighttable install --update to replace it.');
    backup = `${destination}.backup-${randomUUID()}`;
    await ctx.fs.cp(destination, backup, { recursive: true, errorOnExist: true, force: false });
  }
  try {
    await ctx.run(installer, ['/S']);
    if (!await exists(path.join(destination, 'Python', 'python.exe'), ctx)) throw new Error('The Windows installer finished without the bundled CLI runtime.');
  } catch (error) {
    if (backup) {
      if (await exists(destination, ctx)) await ctx.fs.rename(destination, `${destination}.failed-${randomUUID()}`);
      await ctx.fs.rename(backup, destination);
    }
    throw error;
  }
  if (backup) ctx.log(`Previous app preserved at ${backup}`);
}

export async function install(metadata, options = {}, ctx = context()) {
  const key = `${ctx.platform}-${ctx.arch}`;
  if (!PLATFORMS[key]) releaseAsset(metadata, key);
  if (options.installDir && ctx.platform !== 'darwin') throw new Error('--install-dir is supported on macOS only; Windows uses its standard per-user installer location.');
  const installed = await findInstalled(ctx);
  const destination = options.installDir ? path.join(path.resolve(options.installDir), 'LightTable.app') : installed ?? defaultDestination(ctx);
  if (!options.update && await exists(destination, ctx)) {
    if (installed === destination) {
      await remember(destination, ctx);
      ctx.log(`Using installed LightTable at ${destination}. Use lighttable install --update to install this npm package's desktop version.`);
      return { destination, installed: false };
    }
    throw new Error('The installation destination already exists. Use --update explicitly to replace it.');
  }
  const asset = releaseAsset(metadata, key);
  const work = await ctx.fs.mkdtemp(path.join(ctx.temp, 'lighttable-download-'));
  try {
    const artifact = path.join(work, asset.name);
    ctx.log(`Downloading LightTable ${metadata.version} for ${key}…`);
    await ctx.download(asset, artifact);
    if (ctx.platform === 'darwin') await installMac(artifact, destination, options.update, work, ctx);
    else await installWindows(artifact, destination, options.update, ctx);
    await remember(destination, ctx);
    ctx.log(`Installed LightTable at ${destination}. Run lighttable --help for its CLI.`);
    return { destination, installed: true };
  } finally { await ctx.fs.rm(work, { recursive: true, force: true }); }
}

export async function delegate(args, ctx = context()) {
  const destination = await findInstalled(ctx);
  if (!destination) throw new Error('LightTable desktop is not installed. Run lighttable install first.');
  if (ctx.platform === 'darwin') return ctx.run(path.join(destination, 'Contents', 'MacOS', 'lighttable-cli'), args, { env: ctx.env });
  const resources = path.join(destination, 'Resources', 'LightTable');
  return ctx.run(path.join(destination, 'Python', 'python.exe'), ['-B', '-m', 'lighttable_cli', ...args], {
    cwd: resources, env: { ...ctx.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
}
