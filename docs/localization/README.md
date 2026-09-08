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

Refresh every affected catalog in `web/locales/`. `translate-locales.py` is an
optional bounded authoring tool requiring an API key in its environment. Normal
builds and the installed app never call a translation service. Missing entries
must be translated; English fallback does not satisfy the completion checks.

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
shared catalogs, and generated Help together. CI repeats these checks. Both
macOS build routes package translated Photos permission text from these same
catalogs. See [server messages](SERVER.md) for error classification and workers,
and [Help maintenance](../help/README.md) for article identity and review rules.
