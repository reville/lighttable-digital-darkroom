# Crash report relay

A Cloudflare Worker that receives opt-in crash reports from LightTable and
files them as issues in a **private** repository. `crash_reports.py` builds and
sends the reports; `docs/recovery.md` describes what one contains and how the
app decides to send it.

Nothing here holds user content. A report has already been reduced to an
allowlist before it leaves the computer, `src/report.js` validates that
allowlist again, and the relay never stores the sender's address: rate limiting
uses a salted daily hash held only in memory, and request logs are off.

## Pieces

| File | Role |
|---|---|
| `src/index.js` | HTTP entry point and the `CrashDesk` Durable Object that queues, groups, and files reports |
| `src/report.js` | Schema-1 validation, crash signature, issue title, body, and occurrence comment |
| `wrangler.toml` | Worker name, route, Durable Object binding, target repository |

`POST /v1/crash` takes one report (≤ 64 KB JSON) and answers `202` once it is
queued, `422` with the failing fields when validation rejects it, `429` when a
sender or the relay is over its limit, and `503` when the queue is full.
`GET /v1/health` answers `200`.

One issue is filed per crash signature — the component, the fatal error or
signal, and the crashed thread's frames without line numbers — so the same
crash from different versions stays in one thread. Later occurrences become
comments, at most five writes a day per signature counting the issue itself.
While GitHub is unreachable or the token is missing, reports wait in the
object's storage and are retried after a minute, ten minutes, an hour, then six
hours, and are dropped after eight attempts or two weeks.

## Deploy

The relay needs a private repository for the issues and a token that can only
write issues there.

1. Create the private repository, for example `reville/lighttable-crash-reports`,
   and a `crash` label in it. Set `GITHUB_REPO` in `wrangler.toml` to match.
2. Create a fine-grained personal access token at
   <https://github.com/settings/personal-access-tokens/new>: **Resource owner**
   your account, **Repository access** only that repository, **Repository
   permissions → Issues: Read and write**, and nothing else.
3. Store the token as a Worker secret, pasting it at the prompt so it is never
   written to a file or to a shell history:

   ```sh
   cd services/crash-reports
   CLOUDFLARE_API_TOKEN=… npx wrangler secret put GITHUB_TOKEN
   CLOUDFLARE_API_TOKEN=… npx wrangler deploy
   ```

4. Point `reports.lighttable.app` at the Worker: a proxied DNS record for that
   name plus the route in `wrangler.toml`. `LIGHTTABLE_CRASH_REPORT_URL`
   overrides the address the app uses, which is how a test build reaches a
   `wrangler dev` relay instead.
5. Check it: `curl -s https://reports.lighttable.app/v1/health`.

Rotate the token by repeating step 3; the relay reads it on each request.
Removing the secret stops delivery without losing queued reports.

## Tests

`tests/crash-relay.test.mjs` in the repository root covers validation,
grouping, issue text, the HTTP surface, and the queue against a fake storage
and a fake GitHub. `tests/test_crash_reports.py` checks that a report the app
builds passes this validator, so the two schemas cannot drift apart.

```sh
node --test tests/crash-relay.test.mjs
```
