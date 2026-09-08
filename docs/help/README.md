# Maintaining in-app help

## update-text

`update-text` means completing this sequence after the implementation is ready:

1. Update the README and relevant workflow, compatibility, and command
   documentation against the implemented behavior.
2. Update in-app Help using the source review, build, and check steps below.
3. Update the separate `reville/lighttable-site` feature content. Preserve
   existing user-written copy and distinguish implemented features from
   deferred work and unverified compatibility claims.
4. Once English is settled, refresh the affected UI and Help source strings,
   every declared translation catalog, and generated localized Help bundles.
   Preserve placeholders, file formats, paths, keyboard shortcuts, and user
   data. Run the localization checks provided by that checkout and inspect
   translated controls and Help in the running interface.

Keep app, website, and translation changes reviewable together. Report their
actual source, publication, and installation states separately. If localization
is being developed in another checkout, identify that location and its remaining
integration work; English fallback is not evidence of a completed translation.

The [localization maintenance guide](../localization/README.md) covers the
shared catalogs, generated Help, and native permission text used by the app.

## Help source

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

## Localized help

English stays in `web/help-content.json`. Localized help is generated from that
reviewed English bundle and the same message catalogs used by the interface:
`web/locales/{locale}.json`. Article IDs, related-article links, and canonical
English `category` values never change across languages. A translated
`categoryLabel` supplies the readable category name. Titles, summaries,
keywords, section titles, paragraphs, steps, and tips are all translated.

The shared source manifest is `docs/localization/source.json`:

```json
{"version": 1, "sourceDigest": "sha256", "messages": ["Editing", "Open a photo."]}
```

`messages` must contain the sorted, unique English UI and help strings,
including every help category label. `help_messages(bundle)` in
`scripts/help_content.py` returns the help contribution for an extractor.
`localization_source_digest(messages)` calculates SHA-256 over sorted, unique
messages serialized as compact UTF-8 JSON, with Unicode characters unescaped.
This digest is distinct from the help source-review fingerprints.

Each translation catalog uses the English string as its message key:

```json
{"version": 1, "locale": "fr", "sourceDigest": "sha256", "messages": {"Editing": "Retouche", "Open a photo.": "Ouvrez une photo."}}
```

Optional catalog metadata, such as generation model and human-review status,
does not change the message format. The declared languages come from
`web/locales/manifest.json`, whose `locales` entries have `code`, `name`,
`nativeName`, and `dir` (`ltr` or `rtl`). The English code is `en`.

After changing English content:

1. Finish the English review and `build`/`check` workflow above.
2. Refresh the shared source manifest, then update each declared language
   catalog. Every catalog must contain every shared source message, including
   interface messages outside help, and match the current source digest.
3. Run `python3 scripts/help_content.py localization-build`, then
   `python3 scripts/help_content.py localization-check`.
4. Review translated wording and exercise search, related links, category
   filters, and layout in the chosen languages. These checks prove coverage and
   consistency, not translation quality or whether the English instructions
   match the current app.

The localized reader bundles are `web/locales/help/{locale}.json`. The commands
process every declared locale except English by default. Add `--locale fr`
(repeatable) while working on specific languages. A filtered check verifies
only those languages; use an unfiltered check before delivery. English still
uses the ordinary `build` and `check` commands.

Localization fails for missing, blank, obsolete, or stale translation entries,
untranslated help strings absent from the source manifest, changed placeholder
names/counts such as `{filename}` or `{0}`, changed literal filenames and known
extensions, and a missing or stale generated reader bundle. All selected
catalogs must validate before any output is written. There is no implicit
English fallback: unchanged technical names require explicit catalog entries.
Explicit identical strings alone are not proof that prose has been translated;
translation review still matters. Localization commands never update the help
review lock or acknowledge source changes.
