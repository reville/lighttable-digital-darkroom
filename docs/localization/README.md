# Localizing LightTable

LightTable uses 20 locales, including English. **Settings → General → Language**
changes the interface, native menus, application messages, and searchable Help.
Applying a language saves pending edits and reloads the window. Photo metadata,
filenames, preset content, identifiers, and stored history remain their original
data. Generated photo descriptions and third-party diagnostics are not translated.

The catalogs are machine-authored, with source, token, plural, and coverage
validation. These checks do not replace linguistic review. Inspect long labels,
Arabic reading direction, language switching, contextual Help, and save/reload
behavior in the running app before shipping text changes.

## update-text

Finish documentation, in-app Help, and website feature copy first. Mark new
JavaScript messages with literal `t`/`tn` calls, Python messages with `T`, and
native strings with `L`/`tr`. Keep complete sentences and named placeholders;
never translate user data or build sentences from translated fragments.

```sh
python3 scripts/localization.py annotate
python3 scripts/extract-web-i18n.py --write
python3 server_localization.py extract
python3 scripts/native_localization_sources.py --write
```

HTML annotation preserves IDs, input/option values, and text-node structure.
Review affected Help source anchors and articles after annotation, then record
those specific IDs and regenerate the English reader:

```sh
python3 scripts/help_content.py status
python3 scripts/help_content.py review reviewed-article-id
python3 scripts/help_content.py build
python3 scripts/localization.py extract
```

Between releases a feature may merge with pending translations. Run
`python3 scripts/localization.py check --allow-pending` and
`python3 scripts/help_content.py localization-check --allow-pending`, which is
what CI runs on pull requests and `main`: they validate the translations that
exist, report the missing messages per locale, and keep each lagging locale's
last complete Help bundle. The interface shows English for a missing message
until its catalog catches up. `python3 scripts/localization.py pending --json`
lists what is missing.

Before a release, complete every catalog in `web/locales/` with
`translate-locales.py`, a bounded authoring tool that needs an API key in its
environment and keeps existing translations, then run the strict checks below.
Normal builds and the installed app never call a translation service. The
release build's `translations` job refuses a tag whose catalogs are incomplete;
English fallback does not satisfy the strict checks.

```sh
python3 scripts/localization.py check
python3 scripts/help_content.py check
python3 scripts/help_content.py localization-build
python3 scripts/help_content.py localization-check
python3 scripts/extract-web-i18n.py --check
python3 server_localization.py check
python3 scripts/native_localization_sources.py
```

Commit the implementation, English sources, review lock, extracted inventories,
shared catalogs, and generated Help together. CI repeats these checks, allowing
pending translations on pull requests and `main` and requiring complete
catalogs for a release. Both
macOS build routes package translated Photos permission text from these same
catalogs. See [server messages](SERVER.md) for error classification and workers,
and [Help maintenance](../help/README.md) for article identity and review rules.
