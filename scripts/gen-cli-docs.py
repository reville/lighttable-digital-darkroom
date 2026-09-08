#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lighttable_cli.manifest import INTERNAL_ROUTES, ROUTE_COVERAGE, TOOLS


lines = [
    "# LightTable command line", "",
    "`lighttable` is a standard-library client for the local app server. Its",
    "stdout is data, progress is written to stderr, and all agent-originated",
    "state changes are strict, attributed, visible in the window, and undoable.",
    "", "## Start", "", "```sh",
    "scripts/install-cli.sh",
    "lighttable status --json",
    "lighttable --profile review photos list --limit 5 --jsonl",
    "```", "",
    "A running window is discovered from its protected instance file. Use",
    "`--port` or `--catalog` if several instances exist. Supplying the",
    "`--profile review` option may start an isolated headless server for that command.",
    "", "## Common recipes", "", "```sh",
    "lighttable photos list --where status=pending --jsonl",
    "lighttable photos list --where 'rating>=4' --where focal-length=50 --where from=2026-01-01 --where to=2026-12-31 --jsonl",
    "lighttable rate 5 @current",
    "lighttable flag reject --where 'rating>=1' --where status=pending",
    "lighttable edit set @selection --grade exposure=0.25 --curve 'L=0,0;0.5,0.4;1,1' --label 'Lift exposure'",
    "lighttable render @current -o /tmp/after.png --width 1400",
    "lighttable analyze @current --region 0.2,0.1,0.4,0.3 --json",
    "lighttable export run @selection --destination ~/Pictures/Exports",
    "lighttable ui command nextPhoto",
    "```", "", "## Export delivery", "",
    "`--destination-mode fixed` writes into one destination folder (the default).",
    "`original-folder-relative` treats `--destination` as a subfolder beside each",
    "original. `preserve-source-hierarchy` retains folders beneath stable source",
    "namespaces, so two sources named Photos cannot collide. Single and batch",
    "exports follow the same rules; `/api/export/preview` returns sample paths.",
    "", "`--preserve-capture-time` sets file times from EXIF capture time. An embedded",
    "timezone offset wins. Without one, the default preserves export time and",
    "reports a warning; `--capture-time-policy local` explicitly uses this",
    "computer's timezone at the capture date, including daylight saving time.",
    "`--metadata` selects all, all-except-location, copyright, or none;",
    "`--no-sidecar` omits the delivery recipe sidecar.",
    "", "Use `--no-wait` to get a job ID, then `lighttable jobs cancel ID` to stop it.",
    "Cancellation stops queueing and waits for active work to clean up. Completed",
    "outputs survive. The job becomes terminal only after cleanup; its result",
    "records completed, skipped, cancelledCount and per-file warnings.",
    "", "## Photo availability and lens profiles", "",
    "Catalog photos expose `availability`: local or cloud-only. Cloud-only means",
    "macOS reports a dataless placeholder; download it in Finder, then rescan.",
    "Content hashing, metadata extraction and rendering refuse placeholders.",
    "", "`/api/lens-profile?name=...` returns found, profile, reason and candidates.",
    "Set `optics.profileOverride` to null for automatic selection or an object",
    "with cameraMaker, cameraModel, lensMaker and lensModel from a candidate.",
    "The choice is validated against the camera and recorded focal length.",
    "Ambiguous automatic matches and unavailable overrides stay uncorrected.",
    "", "## Route coverage", "",
    "| Command family | API routes |", "| --- | --- |",
]
for command, routes in ROUTE_COVERAGE.items():
    lines.append(f"| `{command}` | " + ", ".join(f"`{route}`" for route in routes) + " |")
lines += ["", "Internal browser/native routes are declared rather than hidden:", ""]
for route, reason in INTERNAL_ROUTES.items():
    lines.append(f"- `{route}` — {reason}.")
lines += ["", "## MCP tools", ""]
for tool in TOOLS:
    lines.append(f"- `{tool['name']}` — {tool['summary']}.")
lines += ["", "Run `lighttable mcp` as a stdio MCP server. It supports `initialize`,",
          "`tools/list`, `tools/call`, `resources/list`, and `resources/read`.",
          "Resources expose this schema, the agent guide, and current window state.",
          "", "## Safety", "",
          "Mutating requests require the token held in the discovered instance file.",
          "The CLI never prints it. Cross-origin requests and incorrect Host headers",
          "are rejected. Destructive generic route calls require `--yes`; photo trash",
          "remains a recoverable native-host operation.", ""]

target = ROOT / "CLI.md"
content = "\n".join(lines)
if "--check" in sys.argv:
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        print("CLI.md is stale; run scripts/gen-cli-docs.py", file=sys.stderr)
        raise SystemExit(1)
else:
    target.write_text(content, encoding="utf-8")
