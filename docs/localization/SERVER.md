# Server messages

`server_localization.py` translates explicitly marked, server-owned UI prose.
It reads the current `locale` preference from the server's `PREFS_FILE` (or
`LIGHTTABLE_PREFS_FILE` outside the server) and shares the browser catalogs in
`web/locales/{locale}.json`. Reads are cached by file identity, size, and
modification time. A language change takes effect on the next message call.
The saved application preference is shared by HTTP handlers and background
workers; workers read it when producing a message, including workers created
before the preference changed. There is no independent request-header locale
that could put the native shell, browser, and jobs in different languages.

Use literal message keys with named placeholders:

```python
from server_localization import T

return {"error": T("Photo {name} is unavailable", name=photo_name),
        "code": "unavailable", "name": photo_name}
```

Only the message is localized. Keep IDs, codes, states, names, paths, preset
content, and photo metadata untouched. Do not apply translation recursively to
JSON responses, and do not use arbitrary exception strings as translation keys.
Format numbers and values before passing them to placeholders when needed.
Inserted values are never parsed as another template or translated themselves.

`LocalizedText` preserves the original English message for existing exception
classification. Use `source_message(error)` for compatibility logic that still
depends on wording; prefer stable error codes for new behavior. For explicitly
marked capability text kept in a cache, `refresh(value)` evaluates the same
message under the latest language. It leaves ordinary strings/data unchanged.
Persisted or completed job messages keep the language in which they were made.
The same applies to durable XMP outbox errors. A later retry produces messages
in the current language. Keyword and XMP validation errors are explicit messages;
XMP property identifiers in conflict details and imported metadata remain intact.
History labels and other stored user content are not rewritten when switching
languages.

Run `python3 server_localization.py extract` after updating markers. This
creates `server-source.json`, with sorted unique `messages`, their digest, and
per-module message membership. The shared UI/help source extractor must merge
those `messages` before translation catalogs are regenerated. Add newly marked
modules to `SOURCE_MODULES`. `python3 server_localization.py check` fails if
the checked-in extraction is stale or a marker uses a nonliteral key or
mismatched placeholder arguments.

Runtime error reporting falls back to the English message when a catalog is
missing, unreadable, or has invalid/missing tokens. Complete-catalog checks are
therefore required before shipping; this fallback is resilience, not a claim
of completed localization. Third-party/native diagnostics and generated photo
descriptions remain their original content rather than being guessed at or
sent to an external translation service.
