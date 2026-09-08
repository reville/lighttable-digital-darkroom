"""Explicit localization of server-owned UI prose; never walk response data.

Use T('Photo {name} is unavailable', name=name) at a user-visible message site.
Photo names, metadata, stable status codes, and arbitrary exception strings are
not translation keys. Catalog generation is offline from this runtime helper.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading


ROOT = Path(__file__).resolve().parent
SOURCE_FILE = 'docs/localization/server-source.json'
SOURCE_MODULES = ('server.py', 'export_workflow.py', 'media_availability.py',
                  'jobs.py', 'enhance_workflow.py', 'color_pipeline.py',
                  'film_lab_ai/providers.py', 'film_lab_ai/service.py',
                  'ingest_workflow.py', 'watch_workflow.py',
                  'keyword_workflow.py', 'xmp_sidecar.py', 'platform_image.py',
                  'file_identity.py', 'durable_io.py')
TOKENS = re.compile(r'\{\w+\}')
LOCALE = re.compile(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*')


class LocalizedText(str):
    """Keep English error classification independent from displayed language."""
    def __new__(cls, translated, source=None, template=None, values=None):
        result = super().__new__(cls, translated)
        result.source = str(source if source is not None else translated)
        result.template = template
        result.values = values or {}
        return result

    def __reduce__(self):
        return type(self), (str(self), self.source, self.template, self.values)


def source_message(error):
    """The original message for compatibility logic, never a translated code."""
    value = error.args[0] if isinstance(error, BaseException) and error.args else error
    return getattr(value, 'source', str(value))


class Translator:
    def __init__(self, root=ROOT, prefs_file=None):
        self.root = Path(root)
        self.prefs_file = prefs_file
        self._lock = threading.RLock()
        self._cache = {}

    def _read(self, path):
        # Re-stat on every call so changing language or replacing a catalog is
        # visible without restarting. Files themselves are parsed only on drift.
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            signature = None
        with self._lock:
            prior = self._cache.get(path)
            if prior and prior[0] == signature:
                return prior[1]
            try:
                value = json.loads(path.read_text(encoding='utf-8')) if signature else {}
                if not isinstance(value, dict):
                    value = {}
            except (OSError, UnicodeError, ValueError):
                value = {}
            self._cache[path] = (signature, value)
            return value

    def locale(self):
        configured = self.prefs_file() if callable(self.prefs_file) else self.prefs_file
        path = Path(configured or os.environ.get('LIGHTTABLE_PREFS_FILE') or
                    self.root / 'prefs.json').expanduser()
        locale = self._read(path).get('locale', 'en')
        return locale if isinstance(locale, str) and LOCALE.fullmatch(locale) else 'en'

    def __call__(self, message, **values):
        if not isinstance(message, str):
            raise TypeError('T requires an English message string')
        locale = self.locale()
        translated = message
        if locale != 'en':
            catalog = self._read(self.root / 'web/locales' / f'{locale}.json')
            messages = catalog.get('messages')
            candidate = messages.get(message) if isinstance(messages, dict) else None
            if (catalog.get('version') == 1 and catalog.get('locale') == locale
                    and isinstance(candidate, str) and candidate.strip()
                    and Counter(TOKENS.findall(candidate)) == Counter(TOKENS.findall(message))):
                translated = candidate
        # Substitute only named tokens, without Python attribute/index lookup or
        # interpreting braces contained inside user-provided values.
        def interpolate(template):
            return TOKENS.sub(lambda match: str(values.get(match.group()[1:-1], match.group())), template)
        return LocalizedText(interpolate(translated), interpolate(message), message, values)


_translator = Translator()


def configure(*, prefs_file=None, root=ROOT):
    """The server supplies a callable so tests/profiles can change PREFS_FILE."""
    global _translator
    _translator = Translator(root=root, prefs_file=prefs_file)


def T(message, **values):
    return _translator(message, **values)


def refresh(value):
    """Refresh an already-marked cached message, leaving ordinary data alone."""
    if isinstance(value, LocalizedText) and value.template is not None:
        return _translator(value.template, **value.values)
    return value


def extract_source(root=ROOT, modules=SOURCE_MODULES):
    """Extract only literal T markers. Unsupported marker syntax fails loudly."""
    root = Path(root)
    locations = {}
    for relative in modules:
        tree = ast.parse((root / relative).read_text(encoding='utf-8'), filename=relative)
        marked = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != 'T':
                continue
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise ValueError(f'{relative}:{node.lineno}: T needs one literal English message')
            message = node.args[0].value
            if not message.strip():
                raise ValueError(f'{relative}:{node.lineno}: T message is empty')
            supplied = {keyword.arg for keyword in node.keywords}
            required = {token[1:-1] for token in TOKENS.findall(message)}
            if supplied != required:
                raise ValueError(f'{relative}:{node.lineno}: T placeholders do not match keyword arguments')
            marked.add(message)
        locations[relative] = sorted(marked)
    messages = sorted({message for entries in locations.values() for message in entries})
    digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False,
                                      separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'version': 1, 'messages': messages, 'sourceDigest': digest,
            'sources': locations}


def run(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('extract', 'check'))
    args = parser.parse_args(argv)
    try:
        result = extract_source(root)
        expected = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        target = Path(root) / SOURCE_FILE
        if args.command == 'extract':
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected, encoding='utf-8')
        elif not target.is_file() or target.read_text(encoding='utf-8') != expected:
            raise ValueError('Server message manifest is stale; run python3 server_localization.py extract')
        print(f'Server localization {args.command}: {len(result["messages"])} explicit messages.')
        return 0
    except (OSError, UnicodeError, ValueError, SyntaxError) as error:
        print(f'Server localization failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
