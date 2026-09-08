# DAM translation update

This directory keeps the completed DAM English text and its translations with
the feature branch. It contains 110 UI and Help source strings, three plural
pairs, and generated translations of the current 55-article Help bundle for all
20 declared locales, including English.

The translations are machine-authored and structurally checked. They are not a
claim of human linguistic review or a localized installed app. The application
localization runtime remains separate, unfinished work on
`codex/localization-20-languages-20260906`. This handoff preserves that work and
keeps the DAM text ready for its eventual integration.

## Refresh and check

Finish the documentation, Help, and website English copy first. Refresh
`source.json` after changing one of its recorded source files, then update the
translation catalogs in the localization checkout. That checkout consumes the
DAM source under `docs/localization/feature-sources/dam-workflows.json` alongside
its existing UI, native, server, and Help vocabulary.

Once every catalog and plural form is complete:

```sh
python3 scripts/dam_localization.py build --catalogs /path/to/localization-checkout/web/locales
python3 scripts/dam_localization.py check
```

The check requires no API access. It verifies source file hashes, locale and
message coverage, named placeholders, literal filenames/extensions, plural
categories, and Help article identities, steps, and related links. It does not
judge translation quality. Inspect translated controls and Help after merging
the localization runtime, including long labels and right-to-left layouts.

`messages/{locale}.json` contains the DAM additions for the shared catalog.
`help/{locale}.json` contains the complete current Help translation. Merge the
message additions with the application's catalogs, refresh the shared source
inventory and generated bundles, and wire the new dynamic messages through the
runtime's literal translation and plural calls. Do not replace an entire
application catalog with this smaller message file.
