// SPDX-License-Identifier: GPL-3.0-only
// LightTable crash report relay (Cloudflare Worker).
//
// POST /v1/crash accepts one schema-1 report from the app, which already
// removed everything personal; the relay validates it again, never stores the
// sender's address, and queues it in one Durable Object. The object files one
// private GitHub issue per crash signature and adds a comment for each later
// occurrence, within daily and hourly limits. Reports wait in storage while
// GitHub is unavailable or the token is missing, then expire.
import {
  MAX_BODY_BYTES, issueBody, issueTitle, occurrenceComment, reportSignature, validateReport,
} from './report.js';

export const LIMITS = {
  perClientPerHour: 12,      // in memory only; resets when the object restarts
  acceptedPerHour: 240,
  queued: 500,
  issuesPerHour: 30,         // GitHub allows 80 content writes a minute, 500 an hour
  writesPerSignaturePerDay: 5,   // the issue, then comments
  batch: 20,
  attempts: 8,
  maxAgeMs: 14 * 24 * 3600 * 1000,
};
const RETRY_MS = [60e3, 10 * 60e3, 3600e3, 6 * 3600e3];
const LABELS = ['crash'];

function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store', ...headers },
  });
}

async function readLimited(request, limit) {
  if (!request.body) return '';
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder().decode(bytes);
}

/** A per-day, per-deployment pseudonym for rate limiting; never stored. */
async function clientKey(request, env, now) {
  const address = request.headers.get('cf-connecting-ip') || 'unknown';
  const day = new Date(now).toISOString().slice(0, 10);
  const data = new TextEncoder().encode(`${env.CLIENT_KEY_SALT || 'lighttable'}|${day}|${address}`);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)].slice(0, 8).map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/v1/health') return json({ ok: true });
    if (url.pathname !== '/v1/crash') return json({ error: 'not found' }, 404);
    if (request.method !== 'POST') return json({ error: 'method not allowed' }, 405, { allow: 'POST' });
    if (!(request.headers.get('content-type') || '').toLowerCase().startsWith('application/json')) {
      return json({ error: 'unsupported media type' }, 415);
    }
    if (Number(request.headers.get('content-length') || 0) > MAX_BODY_BYTES) {
      return json({ error: 'report too large' }, 413);
    }
    const text = await readLimited(request, MAX_BODY_BYTES);
    if (text === null) return json({ error: 'report too large' }, 413);
    let report;
    try { report = JSON.parse(text); } catch (_) { return json({ error: 'invalid JSON' }, 400); }
    const problems = validateReport(report);
    if (problems.length) return json({ error: 'invalid report', problems: problems.slice(0, 5) }, 422);
    const desk = env.DESK.get(env.DESK.idFromName('crash-desk'));
    return desk.fetch('https://desk.internal/report', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ report, client: await clientKey(request, env, Date.now()) }),
    });
  },
};

const hourKey = (now) => new Date(now).toISOString().slice(0, 13);
const dayKey = (now) => new Date(now).toISOString().slice(0, 10);

export class CrashDesk {
  constructor(ctx, env, { fetcher = (...args) => fetch(...args), now = () => Date.now() } = {}) {
    this.ctx = ctx;
    this.env = env;
    this.fetcher = fetcher;
    this.now = now;
    this.clients = new Map();
    this.recentIds = new Map();
  }

  get storage() { return this.ctx.storage; }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname !== '/report' || request.method !== 'POST') return json({ error: 'not found' }, 404);
    const { report, client } = await request.json();
    return this.accept(report, client);
  }

  async count(key, window) {
    const record = await this.storage.get(key);
    return record?.window === window ? record.count : 0;
  }

  async bump(key, window) {
    const count = await this.count(key, window) + 1;
    await this.storage.put(key, { window, count });
    return count;
  }

  async accept(report, client) {
    const now = this.now();
    if (validateReport(report).length) return json({ error: 'invalid report' }, 422);
    const hour = hourKey(now);
    const seen = this.clients.get(client);
    const used = seen?.hour === hour ? seen.count : 0;
    if (used >= LIMITS.perClientPerHour) return json({ error: 'too many reports' }, 429, { 'retry-after': '3600' });
    if (this.recentIds.has(report.id)) return json({ ok: true, duplicate: true }, 202);
    if (await this.count('limit:accepted', hour) >= LIMITS.acceptedPerHour) {
      return json({ error: 'busy' }, 429, { 'retry-after': '3600' });
    }
    const queued = (await this.storage.get('meta:queued')) || 0;
    if (queued >= LIMITS.queued) return json({ error: 'busy' }, 503, { 'retry-after': '3600' });

    if (this.clients.size > 10000) this.clients.clear();
    this.clients.set(client, { hour, count: used + 1 });
    this.recentIds.set(report.id, now);
    if (this.recentIds.size > 5000) this.recentIds.delete(this.recentIds.keys().next().value);
    await this.bump('limit:accepted', hour);
    const key = `queue:${String(now).padStart(15, '0')}:${report.id}`;
    // One atomic write keeps the counter and the queue in step.
    await this.storage.put({ [key]: { report, received: now, attempts: 0, next: 0 },
      'meta:queued': queued + 1 });
    await this.schedule(now);
    return json({ ok: true, id: report.id }, 202);
  }

  async schedule(at) {
    const current = await this.storage.getAlarm();
    if (current === null || current > at) await this.storage.setAlarm(at);
  }

  async github(path, body) {
    const response = await this.fetcher(`https://api.github.com/repos/${this.env.GITHUB_REPO}${path}`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${this.env.GITHUB_TOKEN}`,
        accept: 'application/vnd.github+json',
        'content-type': 'application/json',
        'user-agent': 'lighttable-crash-reports',
        'x-github-api-version': '2022-11-28',
      },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (_) { /* an empty error body */ }
    return { status: response.status, data, retryAfter: Number(response.headers.get('retry-after')) || 0 };
  }

  /** File or annotate one report. Returns the retry delay in ms, or 0 when done. */
  async deliver(report, now) {
    const signature = await reportSignature(report);
    const key = `sig:${signature}`;
    const known = await this.storage.get(key);
    const day = dayKey(now);
    if (known?.issue) {
      const today = known.day === day ? known.dayCount : 0;
      const total = (known.total || 1) + 1;
      if (today < LIMITS.writesPerSignaturePerDay) {
        const result = await this.github(`/issues/${known.issue}/comments`,
          { body: occurrenceComment(report, total) });
        if (result.status !== 201) return this.failure(result);
      }
      await this.storage.put(key, { ...known, total, day, dayCount: today + 1, lastSeen: now });
      return 0;
    }
    const hour = hourKey(now);
    if (await this.count('limit:issues', hour) >= LIMITS.issuesPerHour) return 3600e3;
    const result = await this.github('/issues', {
      title: issueTitle(report, signature), body: issueBody(report, signature), labels: LABELS,
    });
    if (result.status !== 201 || !Number.isInteger(result.data?.number)) return this.failure(result);
    await this.bump('limit:issues', hour);
    await this.storage.put(key, { issue: result.data.number, total: 1, day, dayCount: 1,
      firstSeen: now, lastSeen: now });
    return 0;
  }

  failure({ status, retryAfter }) {
    if (retryAfter) return retryAfter * 1000;
    if (status === 403 || status === 429) return 3600e3;  // GitHub rate limits
    if (status === 401 || status === 404) return 6 * 3600e3;  // token or repository setup
    return -1;  // any other failure: use the report's own backoff
  }

  async dequeue(key) {
    const queued = (await this.storage.get('meta:queued')) || 1;
    await this.storage.delete(key);
    await this.storage.put('meta:queued', Math.max(0, queued - 1));
  }

  async alarm() {
    const now = this.now();
    if (!this.env.GITHUB_TOKEN || !this.env.GITHUB_REPO) {
      await this.storage.setAlarm(now + 6 * 3600e3);
      return;
    }
    const window = LIMITS.batch * 5;
    const entries = await this.storage.list({ prefix: 'queue:', limit: window });
    if (entries.size < window) await this.storage.put('meta:queued', entries.size);
    let delivered = 0, wait = 0;
    for (const [key, entry] of entries) {
      if (delivered >= LIMITS.batch) { wait = 60e3; break; }
      if (now - entry.received > LIMITS.maxAgeMs || entry.attempts >= LIMITS.attempts) {
        await this.dequeue(key);
        continue;
      }
      if (entry.next > now) {
        wait = wait ? Math.min(wait, entry.next - now) : entry.next - now;
        continue;
      }
      let delay;
      try {
        delay = await this.deliver(entry.report, now);
      } catch (_) {
        delay = -1;
      }
      delivered += 1;
      if (delay === 0) {
        await this.dequeue(key);
        continue;
      }
      const attempts = entry.attempts + 1;
      const backoff = delay > 0 ? delay : RETRY_MS[Math.min(attempts, RETRY_MS.length) - 1];
      await this.storage.put(key, { ...entry, attempts, next: now + backoff });
      if (delay > 0) {  // GitHub asked everyone to wait
        wait = backoff;
        break;
      }
      wait = wait ? Math.min(wait, backoff) : backoff;
    }
    const remaining = await this.storage.list({ prefix: 'queue:', limit: 1 });
    if (remaining.size) await this.storage.setAlarm(now + Math.max(wait, 1000));
  }
}
