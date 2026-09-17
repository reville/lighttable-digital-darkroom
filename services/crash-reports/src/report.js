// SPDX-License-Identifier: GPL-3.0-only
// Crash report schema 1: validation, grouping, and GitHub issue text.
// Mirrors crash_reports.py. Reports are untrusted input; every string that
// reaches GitHub has passed one of these patterns, none of which allows
// backticks, "@" or "#", so values cannot leave their code spans or mention
// people or issues.

export const MAX_BODY_BYTES = 64 * 1024;

const PATTERNS = {
  uuid: /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  version: /^[0-9]{1,4}(\.[0-9]{1,4}){1,3}([-+][A-Za-z0-9.]{1,32})?$/,
  build: /^[A-Za-z0-9._-]{1,32}$/,
  revision: /^[0-9a-f]{40}$/,
  osVersion: /^[0-9A-Za-z._ -]{1,40}$/,
  osBuild: /^[0-9A-Za-z._-]{1,40}$/,
  distribution: /^[a-z0-9._-]{1,32}( [0-9A-Za-z._-]{1,20})?$/,
  arch: /^[A-Za-z0-9_]{1,16}$/,
  model: /^[A-Za-z0-9,._ -]{1,40}$/,
  day: /^\d{4}-\d{2}-\d{2}$/,
  signal: /^SIG[A-Z0-9]{2,8}$/,
  fatalError: /^(<other>|[A-Za-z0-9 _.,:()'-]{1,100})$/,
  file: /^(<other>|(app|lib|site|code|frozen):[A-Za-z0-9_.\/-]{1,200})$/,
  function: /^(<other>|<module>|<lambda>|<listcomp>|<dictcomp>|<setcomp>|<genexpr>|[A-Za-z_][A-Za-z0-9_]{0,79})$/,
  module: /^[A-Za-z_][A-Za-z0-9_.]{0,80}$/,
  exceptionType: /^[A-Z0-9_]{1,40}$/,
  exceptionCodes: /^[A-Za-z0-9 ,x_]{1,80}$/,
  nativeText: /^[A-Za-z0-9 _:,.()x-]{1,100}$/,
  image: /^[A-Za-z0-9_.+ -]{1,80}$/,
  symbol: /^[A-Za-z0-9_:~<>*&(),.\[\] $+'-]{1,200}$/,
  imageUuid: /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/,
};
const PLATFORMS = new Set(['macos', 'windows', 'linux']);
const PACKAGING = new Set(['macos-app', 'portable', 'arch', 'snap', 'flatpak', 'rpm', 'deb',
  'package-manager', 'windows', 'windows-store', 'development']);
const OPERATIONS = new Set(['decode', 'render', 'export', 'unknown']);
const MARKERS = new Set(['invalid-frame', 'no-python-frame', 'truncated']);

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

class Checker {
  constructor() { this.problems = []; }
  fail(path) { if (this.problems.length < 20) this.problems.push(path); return false; }
  object(value, path, keys) {
    if (!isObject(value)) return this.fail(path);
    for (const key of Object.keys(value)) if (!keys.includes(key)) this.fail(`${path}.${key}`);
    return true;
  }
  text(value, path, pattern, { nullable = true } = {}) {
    if (value === null && nullable) return true;
    return (typeof value === 'string' && pattern.test(value)) || this.fail(path);
  }
  integer(value, path, low, high, { nullable = true } = {}) {
    if (value === null && nullable) return true;
    return (Number.isInteger(value) && value >= low && value <= high) || this.fail(path);
  }
  choice(value, path, choices, { nullable = false } = {}) {
    if (value === null && nullable) return true;
    return choices.has(value) || this.fail(path);
  }
  list(value, path, limit, each) {
    if (!Array.isArray(value) || value.length > limit) return this.fail(path);
    value.forEach((item, index) => each(item, `${path}[${index}]`));
    return true;
  }
}

/** Every problem with a report, or [] for a valid one. */
export function validateReport(report) {
  const check = new Checker();
  if (!check.object(report, 'report', ['schema', 'id', 'app', 'system', 'crash'])) return check.problems;
  if (report.schema !== 1) check.fail('schema');
  check.text(report.id, 'id', PATTERNS.uuid, { nullable: false });

  const { app, system, crash } = report;
  if (check.object(app, 'app', ['version', 'build', 'revision', 'modified', 'packaging'])) {
    check.text(app.version, 'app.version', PATTERNS.version, { nullable: false });
    check.text(app.build, 'app.build', PATTERNS.build);
    check.text(app.revision, 'app.revision', PATTERNS.revision);
    if (app.modified !== null && typeof app.modified !== 'boolean') check.fail('app.modified');
    check.choice(app.packaging, 'app.packaging', PACKAGING);
  }
  if (check.object(system, 'system', ['platform', 'osVersion', 'osBuild', 'distribution',
    'arch', 'model', 'memoryGB', 'cpuCount'])) {
    check.choice(system.platform, 'system.platform', PLATFORMS);
    for (const key of ['osVersion', 'osBuild', 'distribution', 'arch', 'model']) {
      check.text(system[key], `system.${key}`, PATTERNS[key]);
    }
    check.integer(system.memoryGB, 'system.memoryGB', 0, 4096);
    check.integer(system.cpuCount, 'system.cpuCount', 1, 1024);
  }
  if (check.object(crash, 'crash', ['component', 'detectedOn', 'uptimeSeconds', 'exitStatus',
    'signal', 'operation', 'fatalError', 'threads', 'extensionModules', 'native'])) {
    check.choice(crash.component, 'crash.component', new Set(['app', 'engine']));
    check.text(crash.detectedOn, 'crash.detectedOn', PATTERNS.day, { nullable: false });
    check.integer(crash.uptimeSeconds, 'crash.uptimeSeconds', 0, 1e8);
    check.integer(crash.exitStatus, 'crash.exitStatus', -1000, 1000);
    check.text(crash.signal, 'crash.signal', PATTERNS.signal);
    check.choice(crash.operation, 'crash.operation', OPERATIONS);
    check.text(crash.fatalError, 'crash.fatalError', PATTERNS.fatalError);
    check.list(crash.threads, 'crash.threads', 32, (thread, path) => {
      if (!check.object(thread, path, ['current', 'frames'])) return;
      if (typeof thread.current !== 'boolean') check.fail(`${path}.current`);
      check.list(thread.frames, `${path}.frames`, 64, (frame, framePath) => {
        if (isObject(frame) && 'marker' in frame) {
          if (check.object(frame, framePath, ['marker'])) {
            check.choice(frame.marker, `${framePath}.marker`, MARKERS);
          }
        } else if (check.object(frame, framePath, ['file', 'line', 'function'])) {
          check.text(frame.file, `${framePath}.file`, PATTERNS.file, { nullable: false });
          check.integer(frame.line, `${framePath}.line`, 0, 1e7, { nullable: false });
          check.text(frame.function, `${framePath}.function`, PATTERNS.function, { nullable: false });
        }
      });
    });
    check.list(crash.extensionModules, 'crash.extensionModules', 128, (name, path) => {
      check.text(name, path, PATTERNS.module, { nullable: false });
    });
    const native = crash.native;
    if (native !== null && check.object(native, 'crash.native', ['exceptionType',
      'exceptionCodes', 'exceptionSubtype', 'termination', 'frames', 'images'])) {
      check.text(native.exceptionType, 'crash.native.exceptionType', PATTERNS.exceptionType);
      check.text(native.exceptionCodes, 'crash.native.exceptionCodes', PATTERNS.exceptionCodes);
      check.text(native.exceptionSubtype, 'crash.native.exceptionSubtype', PATTERNS.nativeText);
      check.text(native.termination, 'crash.native.termination', PATTERNS.nativeText);
      check.list(native.frames, 'crash.native.frames', 64, (frame, path) => {
        if (!check.object(frame, path, ['image', 'symbol', 'offset'])) return;
        check.text(frame.image, `${path}.image`, PATTERNS.image);
        check.text(frame.symbol, `${path}.symbol`, PATTERNS.symbol);
        check.integer(frame.offset, `${path}.offset`, 0, 2 ** 48);
      });
      check.list(native.images, 'crash.native.images', 64, (image, path) => {
        if (!check.object(image, path, ['name', 'uuid', 'arch'])) return;
        check.text(image.name, `${path}.name`, PATTERNS.image, { nullable: false });
        check.text(image.uuid, `${path}.uuid`, PATTERNS.imageUuid);
        check.text(image.arch, `${path}.arch`, PATTERNS.arch);
      });
    }
    const evidence = (crash.threads?.length || 0) + (native ? 1 : 0)
      + (crash.exitStatus === null || crash.exitStatus === undefined ? 0 : 1);
    if (!evidence) check.fail('crash.evidence');
  }
  return check.problems;
}

/** The thread that crashed: faulthandler marks it, otherwise the first one. */
export function crashedThread(crash) {
  const threads = crash.threads || [];
  return threads.find((thread) => thread.current) || threads[0] || { frames: [] };
}

const codeFrames = (crash) => crashedThread(crash).frames.filter((frame) => frame.file);
const topAppFrame = (crash) => codeFrames(crash)
  .find((frame) => /^(app|code):/.test(frame.file) && frame.function !== '<other>');
const topSymbol = (crash) => (crash.native?.frames || []).find((frame) => frame.symbol);

/** Group reports of the same failure regardless of version or line numbers. */
export async function reportSignature(report) {
  const { crash, system } = report;
  const parts = [crash.component, crash.signal || crash.native?.exceptionType
    || crash.fatalError || `exit ${crash.exitStatus}`];
  const frames = codeFrames(crash).slice(0, 6).map((frame) => `${frame.file}:${frame.function}`);
  const symbols = (crash.native?.frames || []).filter((frame) => frame.symbol)
    .slice(0, 6).map((frame) => `${frame.image}:${frame.symbol}`);
  if (frames.length || symbols.length) parts.push(...frames, ...symbols);
  else parts.push(system.platform, crash.operation);
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(parts.join('\n')));
  return [...new Uint8Array(digest)].slice(0, 6).map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

const shortFunction = (symbol) => symbol.replace(/\(.*$/, '').slice(0, 60);

export function issueTitle(report, signature) {
  const { crash } = report;
  const what = crash.signal || crash.native?.exceptionType || crash.fatalError
    || (crash.exitStatus === null ? 'crash' : `exit ${crash.exitStatus}`);
  const frame = topAppFrame(crash);
  const symbol = topSymbol(crash);
  let where = frame ? ` in ${frame.file.replace(/^[a-z]+:/, '').replace(/\.py[cw]?$/, '')}.${frame.function}` : '';
  if (symbol) where += `${where ? ' via' : ' in'} ${shortFunction(symbol.symbol)}`;
  return `[${crash.component}] ${what}${where} (${signature})`.slice(0, 200);
}

const code = (value) => (value === null || value === undefined || value === '' ? '—' : `\`${value}\``);

function environmentLine(report) {
  const { app, system, crash } = report;
  const os = [system.platform, system.distribution, system.osVersion,
    system.osBuild && `(${system.osBuild})`].filter(Boolean).join(' ');
  const hardware = [system.arch, system.model, system.memoryGB !== null && `${system.memoryGB} GB`,
    system.cpuCount && `${system.cpuCount} CPUs`].filter(Boolean).join(', ');
  const build = [app.version, app.build && `build ${app.build}`,
    app.revision && app.revision.slice(0, 10), app.modified && 'modified'].filter(Boolean).join(' · ');
  return { os, hardware, build, uptime: crash.uptimeSeconds === null ? null : `${crash.uptimeSeconds} s` };
}

export function pythonStack(report) {
  const thread = crashedThread(report.crash);
  return thread.frames.map((frame) => (frame.marker
    ? `<${frame.marker}>` : `${frame.file}:${frame.line} in ${frame.function}`)).join('\n');
}

export function nativeStack(report) {
  return (report.crash.native?.frames || []).map((frame, index) =>
    `${String(index).padStart(2)}  ${(frame.image || '???').padEnd(28)} ${frame.symbol || '?'}`
    + (frame.offset === null ? '' : ` + ${frame.offset}`)).join('\n');
}

export function issueBody(report, signature) {
  const { app, crash } = report;
  const env = environmentLine(report);
  const lines = [
    'Automatic crash report from LightTable, filed by the crash report relay.',
    'Values come from an allowlisted report; they contain no photos, metadata, paths or personal information.',
    '',
    '| | |', '|---|---|',
    `| Signature | ${code(signature)} |`,
    `| First build | ${code(env.build)} (${code(app.packaging)}) |`,
    `| System | ${code(env.os)}, ${code(env.hardware)} |`,
    `| Crash | ${code(crash.component)}, exit ${code(crash.exitStatus)}, ${code(crash.signal)}, `
      + `during ${code(crash.operation)}, after ${code(env.uptime)} |`,
    `| Fatal error | ${code(crash.fatalError)} |`,
    `| Native exception | ${code(crash.native?.exceptionType)} ${code(crash.native?.exceptionSubtype)} |`,
    `| Seen | ${code(crash.detectedOn)} |`,
    '',
  ];
  const python = pythonStack(report);
  if (python) lines.push('### Python stack (crashed thread)', '', '```text', python, '```', '');
  const native = nativeStack(report);
  if (native) lines.push('### Native stack (faulting thread)', '', '```text', native, '```', '');
  const full = JSON.stringify(report, null, 2);
  const details = ['<details><summary>Full report</summary>', '', '```json', full, '```', '', '</details>'];
  const body = lines.join('\n');
  return body.length + full.length < 60000 ? `${body}\n${details.join('\n')}` : body;
}

export function occurrenceComment(report, total) {
  const env = environmentLine(report);
  const lines = [
    `Occurrence ${total}: ${code(env.build)} on ${code(env.os)}, ${code(env.hardware)}; `
      + `${code(report.crash.component)} during ${code(report.crash.operation)} after ${code(env.uptime)} `
      + `(${code(report.crash.detectedOn)}).`,
  ];
  const python = pythonStack(report);
  if (python) lines.push('', '```text', python.split('\n').slice(0, 12).join('\n'), '```');
  return lines.join('\n');
}
