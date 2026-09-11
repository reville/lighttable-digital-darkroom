# LightTable agent guide

Use the `lighttable` command rather than opening `library.sqlite3`: the server
is the catalog's only writer and owns its render caches and resident GPU engine.

- Start by running `lighttable status --json`. If no window is open, use the
  explicitly isolated review profile for experiments:
  `lighttable --profile review photos list --limit 5 --json`.
- Address a photo by its qualified catalog name, an absolute path, `#imageId`,
  `@current`, or `@selection`. Use `--where` with catalog fields for batches.
- Use `lighttable schema` for exact object fields and ranges. Strict validation
  rejects unknown or clamped edit values.
- Inspect edits with `render`, `analyze`, and `compare`; do not infer pixel
  results from a successful state write.
- External edits are attributed in History, refresh in the open window, and
  provide Undo. Respect the window's automation preference.
- When changing user-facing behavior, run `python3 scripts/help_content.py status`
  and review affected articles in `docs/help/`. Keep help, source evidence, and
  the bundled content in the same change. Follow `docs/help/README.md`; never
  automatically acknowledge reviews to silence a failing help check.
- Plan destructive work first. Generic destructive route calls require `--yes`,
  and originals may only move through the recoverable trash workflow.

Useful sequences:

```sh
lighttable photos list --where status=pending --jsonl
lighttable edit set @selection --grade exposure=0.2 --history-label "Lift exposure"
lighttable compare @current --wipe 0.5 -o /tmp/comparison.png
lighttable analyze @current --reference 1:reference.tif --json
lighttable export run @selection --destination ~/Pictures/Exports
lighttable history list @current --json
```

See `lighttable --help`, `lighttable schema`, and `docs/cli.md` for the generated
command and route reference.

## Recorded journey review

For an on-demand recorded journey review, follow
`scripts/visual-review/README.md`. Start with `--preflight`; the real native
recording requires an unlocked graphical session and explicit foreground-test
authorization. Keep `recorded`, `functional checks passed`, and `visually
reviewed` separate. Inspect clips and transition frames before recording
findings in `review.json`; do not automatically approve a baseline. The first
version covers browse, zoom, basic editing, and bounded exploratory interaction.
