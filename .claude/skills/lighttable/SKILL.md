---
name: lighttable
description: Use the LightTable photo catalog and editor CLI for culling, rating, editing, presets, rendering, analysis, catalog work, and export.
---

# LightTable

1. Run `lighttable status --json`; do not open the SQLite catalog directly.
2. Use `--profile review` only when an isolated headless experiment is needed.
3. Resolve exact photo names with `photos list`, paths, or `@current` and
   `@selection` when a window is connected.
4. Read `lighttable schema` before setting unfamiliar state fields. Keep strict
   validation enabled.
5. Verify edits with `render`, `analyze`, or `compare`, then inspect History.
6. Require a dry-run or explicit confirmation before destructive work.

The server is the only writer. External state changes are attributed, streamed
to the live window, and undoable. See `docs/cli.md` for recipes and route coverage.
