// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import relay, { CrashDesk, LIMITS } from '../services/crash-reports/src/index.js';
import {
  issueBody, issueTitle, occurrenceComment, reportSignature, validateReport,
} from '../services/crash-reports/src/report.js';

const HOUR = 3600e3;
const NOW = Date.parse('2026-09-17T12:00:00Z');

function sample(overrides = {}) {
  const report = {
    schema: 1,
    id: '2b1f6d0e-6a2c-4c55-9d64-1f0a4c1b9e21',
    app: { version: '0.7.6', build: '12', revision: 'b89a5c9c594111a65286ab761857f1fb372fe829',
      modified: false, packaging: 'macos-app' },
    system: { platform: 'macos', osVersion: '26.6.2', osBuild: '25G83', distribution: null,
      arch: 'arm64', model: 'Mac16,10', memoryGB: 16, cpuCount: 10 },
    crash: {
      component: 'engine', detectedOn: '2026-09-17', uptimeSeconds: 2, exitStatus: 11,
      signal: 'SIGSEGV', operation: 'unknown', fatalError: 'Segmentation fault',
      threads: [
        { current: false, frames: [{ file: 'code:server.py', line: 7480, function: 'do_GET' }] },
        { current: true, frames: [
          { file: 'code:platform_image.py', line: 287, function: 'metadata' },
          { file: 'code:server.py', line: 2992, function: 'exif_for' },
          { marker: 'invalid-frame' },
        ] },
      ],
      extensionModules: ['exiv2._image', 'numpy._core._multiarray_umath'],
      native: {
        exceptionType: 'EXC_BAD_ACCESS', exceptionCodes: '0x0000000000000001, 0x0000000000000010',
        exceptionSubtype: 'KERN_INVALID_ADDRESS at 0x0000000000000010',
        termination: 'Segmentation fault: 11',
        frames: [{ image: 'libexiv2.28.dylib', offset: 123456,
          symbol: 'Exiv2::XmpParser::initialize(void (*)(void*, bool), void*)' }],
        images: [{ name: 'libexiv2.28.dylib', uuid: 'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE', arch: 'arm64' }],
      },
    },
  };
  return structuredClone({ ...report, ...overrides });
}

test('validation accepts the app schema and rejects anything else', () => {
  assert.deepEqual(validateReport(sample()), []);
  const cases = [
    [(r) => { r.extra = 1; }, 'report.extra'],
    [(r) => { r.schema = 2; }, 'schema'],
    [(r) => { r.app.version = '0.7.6`'; }, 'app.version'],
    [(r) => { delete r.app.build; }, 'app.build'],
    [(r) => { r.system.model = '@alice'; }, 'system.model'],
    [(r) => { r.system.platform = 'amiga'; }, 'system.platform'],
    [(r) => { r.crash.fatalError = 'see /Users/alice'; }, 'crash.fatalError'],
    [(r) => { r.crash.threads[1].frames[0].file = '/Users/alice/x.py'; }, 'crash.threads[1].frames[0].file'],
    [(r) => { r.crash.threads[1].frames[0].function = 'run #12'; }, 'crash.threads[1].frames[0].function'],
    [(r) => { r.crash.threads[1].frames[0].path = 'x'; }, 'crash.threads[1].frames[0].path'],
    [(r) => { r.crash.threads = Array.from({ length: 33 }, () => ({ current: false, frames: [] })); }, 'crash.threads'],
    [(r) => { r.crash.native.frames[0].symbol = 'f`echo`'; }, 'crash.native.frames[0].symbol'],
    [(r) => { r.crash.native.asi = ['private']; }, 'crash.native.asi'],
    [(r) => { r.crash.threads = []; r.crash.native = null; r.crash.exitStatus = null; }, 'crash.evidence'],
  ];
  for (const [mutate, problem] of cases) {
    const report = sample();
    mutate(report);
    assert.ok(validateReport(report).includes(problem), `${problem}: ${validateReport(report)}`);
  }
  assert.deepEqual(validateReport(null), ['report']);
  assert.deepEqual(validateReport([]), ['report']);
});

test('signatures group a crash across versions and line numbers only', async () => {
  const first = await reportSignature(sample());
  assert.match(first, /^[0-9a-f]{12}$/);
  const later = sample();
  later.app.version = '0.7.9';
  later.crash.threads[1].frames[0].line = 300;
  later.crash.uptimeSeconds = 900;
  assert.equal(await reportSignature(later), first);
  const other = sample();
  other.crash.threads[1].frames[0].function = 'orientation_degrees';
  assert.notEqual(await reportSignature(other), first);
  const bare = sample();
  bare.crash.threads = [];
  bare.crash.native = null;
  const bareOther = structuredClone(bare);
  bareOther.system.platform = 'linux';
  assert.notEqual(await reportSignature(bare), await reportSignature(bareOther));
});

test('issue text names the crash, keeps values in code, and stays within limits', async () => {
  const report = sample();
  const signature = await reportSignature(report);
  assert.equal(issueTitle(report, signature),
    `[engine] SIGSEGV in platform_image.metadata via Exiv2::XmpParser::initialize (${signature})`);
  const body = issueBody(report, signature);
  assert.match(body, /code:platform_image\.py:287 in metadata/);
  assert.match(body, /Exiv2::XmpParser::initialize/);
  assert.match(body, /<details><summary>Full report<\/summary>/);
  assert.match(body, /`macos 26\.6\.2 \(25G83\)`/);
  assert.doesNotMatch(body, /@|#\d/);
  const comment = occurrenceComment(report, 3);
  assert.match(comment, /^Occurrence 3: `0\.7\.6 · build 12 · b89a5c9c59`/);
  const huge = sample();
  huge.crash.threads = Array.from({ length: 32 }, () => ({ current: false,
    frames: Array.from({ length: 64 }, () => ({ file: `app:${'x'.repeat(190)}.py`, line: 1, function: 'f' })) }));
  const bounded = issueBody(huge, signature);
  assert.doesNotMatch(bounded, /Full report/);
  assert.ok(bounded.length < 65536);
});

class FakeStorage {
  constructor() { this.map = new Map(); this.alarm = null; }
  async get(key) { return structuredClone(this.map.get(key)); }
  async put(key, value) {
    const entries = typeof key === 'string' ? { [key]: value } : key;
    for (const [name, item] of Object.entries(entries)) this.map.set(name, structuredClone(item));
  }
  async delete(key) { return this.map.delete(key); }
  async list({ prefix = '', limit = Infinity } = {}) {
    const keys = [...this.map.keys()].filter((key) => key.startsWith(prefix)).sort().slice(0, limit);
    return new Map(keys.map((key) => [key, structuredClone(this.map.get(key))]));
  }
  async getAlarm() { return this.alarm; }
  async setAlarm(at) { this.alarm = at; }
}

function desk({ env = { GITHUB_TOKEN: 'fixture-token', GITHUB_REPO: 'reville/crashes' }, responses = [] } = {}) {
  const storage = new FakeStorage();
  const calls = [];
  const clock = { now: NOW };
  let issue = 40;
  const fetcher = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    const next = responses.shift() ?? (url.endsWith('/issues') ? { status: 201, body: { number: ++issue } }
      : { status: 201, body: { id: 1 } });
    return new Response(JSON.stringify(next.body ?? {}), { status: next.status, headers: next.headers });
  };
  const object = new CrashDesk({ storage }, env, { fetcher, now: () => clock.now });
  return { object, storage, calls, clock };
}

const queued = (storage) => [...storage.map.keys()].filter((key) => key.startsWith('queue:')).sort();
const withId = (index, overrides) => sample({ id: `2b1f6d0e-6a2c-4c55-9d64-${String(index).padStart(12, '0')}`, ...overrides });

test('reports are queued once and filed as one issue plus comments', async () => {
  const { object, storage, calls, clock } = desk();
  const response = await object.accept(sample(), 'client-a');
  assert.equal(response.status, 202);
  assert.deepEqual(await response.json(), { ok: true, id: sample().id });
  assert.equal((await object.accept(sample(), 'client-a')).status, 202, 'a retried report is not queued twice');
  assert.equal(queued(storage).length, 1);
  assert.equal(storage.alarm, NOW);
  assert.equal(await storage.get('meta:queued'), 1);

  await object.alarm();
  assert.equal(calls.length, 1);
  const [created] = calls;
  assert.equal(created.url, 'https://api.github.com/repos/reville/crashes/issues');
  assert.equal(created.init.headers.authorization, 'Bearer fixture-token');
  assert.deepEqual(created.body.labels, ['crash']);
  assert.match(created.body.title, /^\[engine\] SIGSEGV in platform_image\.metadata/);
  assert.equal(queued(storage).length, 0);
  assert.equal(await storage.get('meta:queued'), 0);

  for (let index = 1; index <= LIMITS.writesPerSignaturePerDay + 2; index += 1) {
    clock.now += 1000;
    await object.accept(withId(index), `client-${index}`);
  }
  clock.now += 1000;
  await object.alarm();
  const comments = calls.slice(1);
  assert.equal(comments.length, LIMITS.writesPerSignaturePerDay - 1,
    'the issue counts towards the daily cap; further occurrences are only counted');
  assert.ok(comments.every((call) => call.url.endsWith('/issues/41/comments')));
  const [signature] = [...storage.map.keys()].filter((key) => key.startsWith('sig:'));
  const record = await storage.get(signature);
  assert.equal(record.total, LIMITS.writesPerSignaturePerDay + 3);
  assert.equal(record.issue, 41);
});

test('senders and the relay are rate limited without storing addresses', async () => {
  const { object, storage } = desk();
  for (let index = 0; index < LIMITS.perClientPerHour; index += 1) {
    assert.equal((await object.accept(withId(index), 'one-sender')).status, 202);
  }
  assert.equal((await object.accept(withId(99), 'one-sender')).status, 429);
  assert.equal((await object.accept(withId(98), 'another-sender')).status, 202);
  assert.ok(![...storage.map.keys()].some((key) => key.includes('sender')), 'client keys stay in memory');
  await storage.put('meta:queued', LIMITS.queued);
  assert.equal((await object.accept(withId(97), 'third-sender')).status, 503);
  await storage.put('limit:accepted', { window: new Date(NOW).toISOString().slice(0, 13), count: LIMITS.acceptedPerHour });
  assert.equal((await object.accept(withId(96), 'fourth-sender')).status, 429);
  assert.equal((await object.accept({ schema: 1 }, 'fifth-sender')).status, 422);
});

test('reports wait without a token and back off when GitHub refuses', async () => {
  const waiting = desk({ env: { GITHUB_REPO: 'reville/crashes' } });
  await waiting.object.accept(sample(), 'client');
  await waiting.object.alarm();
  assert.equal(waiting.calls.length, 0);
  assert.equal(queued(waiting.storage).length, 1);
  assert.equal(waiting.storage.alarm, NOW + 6 * HOUR);

  const limited = desk({ responses: [{ status: 403, body: { message: 'rate limit' } }] });
  await limited.object.accept(sample(), 'client');
  await limited.object.accept(withId(2, { crash: { ...sample().crash, component: 'app' } }), 'client');
  await limited.object.alarm();
  assert.equal(limited.calls.length, 1, 'a rate limit stops the batch');
  const [first] = queued(limited.storage);
  assert.equal((await limited.storage.get(first)).next, NOW + HOUR);
  assert.equal(limited.storage.alarm, NOW + HOUR);

  const failing = desk({ responses: [{ status: 502 }, { status: 201, body: { number: 7 } }] });
  await failing.object.accept(sample(), 'client');
  await failing.object.alarm();
  const [key] = queued(failing.storage);
  assert.deepEqual([(await failing.storage.get(key)).attempts, (await failing.storage.get(key)).next],
    [1, NOW + 60e3]);
  failing.clock.now += 60e3;
  await failing.object.alarm();
  assert.equal(queued(failing.storage).length, 0);

  const stale = desk();
  await stale.object.accept(sample(), 'client');
  stale.clock.now += LIMITS.maxAgeMs + 1;
  await stale.object.alarm();
  assert.equal(stale.calls.length, 0);
  assert.equal(queued(stale.storage).length, 0);
  assert.equal(await stale.storage.get('meta:queued'), 0);
});

test('the HTTP entry point validates before it queues and forwards no address', async () => {
  const forwarded = [];
  const env = {
    DESK: {
      idFromName: (name) => name,
      get: (id) => ({ fetch: async (url, init) => {
        forwarded.push({ id, url, body: JSON.parse(init.body) });
        return new Response('{"ok":true}', { status: 202 });
      } }),
    },
  };
  const post = (body, headers = {}) => relay.fetch(new Request('https://reports.lighttable.app/v1/crash', {
    method: 'POST', body, headers: { 'content-type': 'application/json', 'cf-connecting-ip': '203.0.113.9', ...headers },
  }), env);
  assert.equal((await relay.fetch(new Request('https://reports.lighttable.app/v1/health'), env)).status, 200);
  assert.equal((await relay.fetch(new Request('https://reports.lighttable.app/other'), env)).status, 404);
  assert.equal((await relay.fetch(new Request('https://reports.lighttable.app/v1/crash'), env)).status, 405);
  assert.equal((await post('{}', { 'content-type': 'text/plain' })).status, 415);
  assert.equal((await post('x'.repeat(64 * 1024 + 1))).status, 413);
  assert.equal((await post('{nope')).status, 400);
  const rejected = await post(JSON.stringify({ ...sample(), extra: true }));
  assert.equal(rejected.status, 422);
  assert.deepEqual((await rejected.json()).problems, ['report.extra']);
  assert.equal(forwarded.length, 0);
  assert.equal((await post(JSON.stringify(sample()))).status, 202);
  const [{ id, body }] = forwarded;
  assert.equal(id, 'crash-desk');
  assert.deepEqual(body.report, sample());
  assert.match(body.client, /^[0-9a-f]{16}$/);
  assert.doesNotMatch(JSON.stringify(body), /203\.0\.113\.9/);
});
