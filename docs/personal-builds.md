# Personal builds and receipts

Run application development tasks from the public checkout,
`~/CODING/lighttable-dev/lighttable-digital-darkroom`, or an isolated worktree
of it. The parent `lighttable-dev` repository contains private operational
notes; the old `Film Lab` symlink is a compatibility alias, not a suitable Codex
sandbox root. Configure the saved LightTable project to use the real public
checkout. Existing tasks keep the workspace with which they were started.

## Fixed snapshot personal installation

```sh
bash scripts/mnb.sh
```

The runner fetches `origin` once and pins `origin/main`. It builds using only
that revision's updater, without overlaying files from the caller's checkout.
It does not merge branches or wait for other tasks. A per-app and per-worktree
process lock prevents two personal builds from installing or changing the same
source concurrently.

The dedicated `../mnb-managed-worktree` is created detached and locked in Git.
Its ownership record lives in the repository's Git common directory. Reuse
requires matching ownership, a detached HEAD, this runner's lock, and clean
tracked and untracked source. Dirty, branched, unowned, or differently locked
worktrees are preserved; the runner never uses `reset --hard` or `git clean`.
Older `mnb-worktree` and dated worktrees are left alone. Set
`LIGHTTABLE_MNB_WORKTREE` to an unused real path to choose a different managed
worktree. Do not assign the path of another task.

The existing installed app remains open while its replacement builds and runs
the isolated package journey. After verifying the staged bundle, the runner
requests a graceful quit of the exact native PID. It never force-kills the
native app. Installation uses a candidate staged on the destination volume,
preserves the previous bundle beside the installed app, and restores it if the
replacement move fails. The receipt records the rollback path. A later launch
or health failure is reported as a failed installation verification; its
rollback copy remains available.

Success requires the pinned revision in both plist and manifest, clean source,
matching source fingerprints, a valid strict signature, live processes from
that exact bundle, HTTP 200 for its explicitly selected catalog, matching
version, and a connected native window. Packaged macOS servers may report a
null Git revision; their signed plist and manifest provide source identity,
while kernel executable paths bind the health response to the installed app.
A connected window and automated journey are distinct from a human or agent
visual review.

## Cache validity

The quick updater reuses the verified Python environment and unchanged native
components. Fingerprints include the vendored Rust dependencies, compiler
identity, target and build options. Missing required inputs or unavailable
toolchain identity cannot produce a reusable fingerprint. Changes to the
fingerprint format invalidate compiled components; packaging and runtime
compatibility checks still apply. A changed Python runtime, pinned external
engine, Sparkle package, or incompatible Xcode packaging requires a full
release build to establish a new reusable base.

## Compact evidence

Each invocation writes a JSON receipt and a separate build log under the
calling checkout's `.build/mnb/`. The console prints phase names and the final
receipt path. Receipts contain the cutoff, package identity, checks, phase
status/duration, artifact paths, installation proof when performed, and a
failure reason. Raw CLI responses and instance tokens are not included.

```sh
# Builds and runs the native package journey; does not install or close the old app.
bash scripts/mnb.sh --build-only

# Select a receipt destination.
bash scripts/mnb.sh --receipt /tmp/lighttable-build-receipt.json

# Explicit cleanup of this runner's clean, idle, owned worktree only.
bash scripts/mnb.sh --retire-worktree
```

`build_verified` proves a package build. `installed_verified` additionally
proves installation and runtime health. `visual_review: not_run` must not be
reported as visual proof. A failed invocation still writes its phase receipt.
Native package journeys and reopening the installed app require an authorized
foreground test session; `--build-only` is not a headless test option.

Tests for the orchestration use temporary Git repositories and fake process /
installation boundaries, so they do not touch the installed app:

```sh
python3 -B -m unittest discover -s tests -p 'test_mnb.py'
python3 -B -m unittest discover -s tests -p 'test_build_fingerprint.py'
```

PR-only concurrency cancels superseded Python/help runs. Push, manual, and
release validation keep unique run groups and are not cancelled by those PRs.
