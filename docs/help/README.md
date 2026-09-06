# Maintaining in-app help

Help is authored alongside the implementation in the JSON arrays in this
directory. `web/help-content.json` is the generated, offline reader bundle.
The app loads it only when Help opens. No network search or external service
is used. The top-bar Help button, F1, `?`, the macOS Help menu, and inspector
section help buttons all open the same reader.

Each article includes stable `id`, `title`, `category`, `summary`, search
`keywords`, `sections` (plain-text paragraphs, steps, and optional tips), related
article IDs, and `sources`. A source names a repository-relative implementation
file and an exact `anchor` from that file. Use actual user-visible labels and
behavior. Link both the UI and backend where the backend determines the result.
Do not use roadmaps as evidence of shipped functionality.

## When code changes

1. Run `python3 scripts/help_content.py status`. It lists affected articles and
   the source files that changed. Removed anchors fail validation immediately.
2. Read the implementation diff, follow the workflow, and revise affected
   articles if behavior, labels, availability, or limitations changed.
3. Acknowledge only the articles reviewed:
   `python3 scripts/help_content.py review article-id another-article-id`.
   An article can remain verbatim when the source change does not affect its
   explanation. The acknowledgement records that decision against current code.
4. Run `python3 scripts/help_content.py build`, then
   `python3 scripts/help_content.py check`. Commit the articles, review lock,
   generated bundle, and code together.

`review --all` is for an intentional review of the entire help collection,
including the initial content import. Never run it automatically to silence a
failing check. `check`, tests, CI, and `build` do not acknowledge reviews.

The validator rejects duplicate IDs, empty articles, missing source files or
anchors, invalid related links, unreviewed source/content versions, and a stale
reader bundle. `tests/test_help_content.py` runs the same check in the normal
test suite. A lightweight help workflow also runs it before runtime installation.

## What synchronization means

`review-lock.json` records SHA-256 fingerprints of each article and its complete
source files. Whole-file tracking is deliberately conservative: a change to a
large shared file can require checking several articles even if only one
feature changed. Prefer smaller feature modules for evidence when possible.
Anchors keep the dependency explicit and catch renamed or removed controls.
The first version does not infer semantic changes or automatically rewrite prose.
It also cannot detect a new feature that nobody has documented or a dependency
that an author failed to link. During feature review, add or update the matching
help article and its sources; verify the reader using the shipped UI.

## Search and UI

Search ranks titles, keywords, summaries, and full article text, supports partial
words, and ignores common question words. Categories narrow the results. Add
common user vocabulary to `keywords`, including names people might know from
other photo editors when useful. Keep code paths and evidence out of product
prose; the builder strips them from the reader bundle. Content is rendered with
text nodes, never raw HTML.

UI logic lives in `web/help-panel.js`, styling in `web/help.css`, and pure search
logic in `web/help-search.js`. The panel contains focus, returns it on dismissal,
and suspends photo keyboard/menu actions while open. At narrow widths it switches
between the article list and reader using Back to articles.
