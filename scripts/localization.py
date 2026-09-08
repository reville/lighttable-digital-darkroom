#!/usr/bin/env python3
"""Extract and validate the versioned UI/help translation catalogs."""
import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import importlib.util
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP_TAGS = {'script', 'style', 'svg', 'path', 'code', 'kbd'}
ATTRIBUTES = ('title', 'aria-label', 'placeholder', 'alt')


def source_digest(messages):
    return hashlib.sha256(json.dumps(sorted(set(messages)), ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def prose(value):
    return bool(re.search(r'[A-Za-z]', value)) and not re.fullmatch(r'[\d\s.,%+−–/:×-]+', value)


class HTMLMessages(HTMLParser):
    """Track explicit text-node positions without inserting layout-changing spans."""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.stack = []
        self.nodes = []
        self.messages = set()
        self.void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def handle_starttag(self, tag, attrs):
        if self.stack:
            self.stack[-1]['children'] += 1
        line, col = self.getpos()
        offset = sum(len(row) for row in self.lines[:line - 1]) + col
        attributes = dict(attrs)
        skipped = tag in SKIP_TAGS or any(node['skip'] for node in self.stack) or 'data-no-i18n' in attributes
        node = {'tag': tag, 'offset': offset, 'raw': self.get_starttag_text(),
                'attrs': attributes, 'text': {}, 'children': 0, 'skip': skipped}
        if not skipped:
            for name in ATTRIBUTES:
                value = attributes.get(name)
                if value and prose(value):
                    self.messages.add(value)
        self.nodes.append(node)
        if tag not in self.void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1]['tag'] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                break

    def handle_comment(self, text):
        if self.stack:
            self.stack[-1]['children'] += 1

    def handle_data(self, data):
        if not self.stack:
            return
        node = self.stack[-1]
        index = node['children']
        node['children'] += 1
        value = ' '.join(data.split())
        if not node['skip'] and value and prose(value):
            node['text'][str(index)] = value
            self.messages.add(value)

    def annotated(self):
        updated = self.source
        for node in reversed(self.nodes):
            if node['skip']:
                continue
            raw = re.sub(r'\sdata-i18n-(?:text|attrs)=(?:"[^"]*"|\x27[^\x27]*\x27)', '', node['raw'])
            additions = {}
            if node['text']:
                additions['data-i18n-text'] = json.dumps(node['text'], ensure_ascii=False, separators=(',', ':'))
            attrs = {name: node['attrs'][name] for name in ATTRIBUTES
                     if node['attrs'].get(name) and prose(node['attrs'][name])}
            if attrs:
                additions['data-i18n-attrs'] = json.dumps(attrs, ensure_ascii=False, separators=(',', ':'))
            # Option text is its implicit machine value unless explicitly set.
            if node['tag'] == 'option' and 'value' not in node['attrs'] and node['text']:
                additions['value'] = next(iter(node['text'].values()))
            suffix = '/>' if raw.endswith('/>') else '>'
            decorated = raw[:-len(suffix)] + ''.join(f' {key}="{html.escape(value, quote=True)}"'
                for key, value in additions.items()) + suffix
            start = node['offset']
            updated = updated[:start] + decorated + updated[start + len(node['raw']):]
        return updated


def help_messages():
    # Authoring strings must enter translation before the reviewed English
    # reader bundle is rebuilt. Keep source-anchor validation, but do not use
    # the review lock as an extraction dependency or acknowledge reviews here.
    help_content = load_extractor('help_content.py')
    articles = help_content.load_articles(ROOT)
    return set(help_content.help_messages({'version': 1, 'articles': articles}))


@lru_cache(maxsize=8)
def load_extractor(filename):
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plural_pairs():
    pairs = load_extractor('extract-web-i18n.py').extract(ROOT)[1]['pairs']
    return [{'one': one, 'other': other} for one, other in sorted(
        {(pair['one'], pair['other']) for pair in pairs})]


@lru_cache(maxsize=24)
def plural_categories(code):
    # Intl is the runtime source of truth; no separate hand-maintained CLDR table.
    value = subprocess.check_output(['node', '-e',
        'process.stdout.write(JSON.stringify(new Intl.PluralRules(process.argv[1]).resolvedOptions().pluralCategories))', code], text=True)
    return json.loads(value)


def plural_digest(pairs):
    return hashlib.sha256(json.dumps(pairs, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def collect(annotate=False):
    messages = help_messages()
    for path in [ROOT / 'web/index.html', ROOT / 'web/loupe.html']:
        parser = HTMLMessages(path.read_text())
        parser.feed(parser.source)
        messages.update(parser.messages)
        if annotate:
            path.write_text(parser.annotated())
    # Extract from actual source, never trust a stale intermediate inventory.
    web_messages, _ = load_extractor('extract-web-i18n.py').extract(ROOT)
    messages.update(web_messages)
    messages.update(load_extractor('native_localization_sources.py').extract_native_sources(ROOT))
    if (ROOT / 'server_localization.py').exists():
        messages.update(load_extractor('../server_localization.py').extract_source(ROOT)['messages'])
    return sorted(messages)


def write_source(messages):
    pairs = plural_pairs()
    payload = {'version': 1, 'sourceDigest': source_digest(messages), 'pluralsDigest': plural_digest(pairs),
               'pluralPairs': pairs, 'messages': messages}
    path = ROOT / 'docs/localization/source.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    english = {'version': 1, 'locale': 'en', 'sourceDigest': payload['sourceDigest'],
               'pluralsDigest': payload['pluralsDigest'],
               'plurals': {pair['one']: {'one': pair['one'], 'other': pair['other']} for pair in pairs},
               'messages': {message: message for message in messages}}
    (ROOT / 'web/locales/en.json').write_text(json.dumps(english, ensure_ascii=False, indent=2) + '\n')


def validate_translation(source, translation, locale=None):
    if not isinstance(translation, str) or not translation.strip():
        return 'empty translation'
    tokens = lambda text: Counter(re.findall(r'\{\w+\}', text))
    if tokens(source) != tokens(translation):
        return 'interpolation or filename tokens changed'
    help_content = load_extractor('help_content.py')
    if locale:
        script_issue = help_content.locale_script_issue(source, translation, locale)
        if script_issue:
            return script_issue
    file_tokens = help_content.FILE_TOKENS
    if Counter(file_tokens.findall(source)) != Counter(file_tokens.findall(translation)):
        return 'literal filenames or extensions changed'
    for token in help_content.literal_inputs(source):
        if not re.search(r'(?<!\w)' + re.escape(token) + r'(?!\w)', translation):
            return f'literal input {token!r} must remain unchanged'
    if '<' not in source and re.search(r'<\s*/?\w+[^>]*>', translation):
        return 'unexpected HTML in translation'
    return None


def check(messages):
    source = json.loads((ROOT / 'docs/localization/source.json').read_text())
    if source.get('sourceDigest') != source_digest(messages) or source.get('messages') != messages:
        raise ValueError('Source manifest is stale; run python3 scripts/localization.py extract')
    pairs = plural_pairs()
    if source.get('pluralPairs') != pairs or source.get('pluralsDigest') != plural_digest(pairs):
        raise ValueError('Plural source manifest is stale; run extract')
    manifest = json.loads((ROOT / 'web/locales/manifest.json').read_text())
    for locale in manifest['locales']:
        code = locale['code']
        catalog = json.loads((ROOT / f'web/locales/{code}.json').read_text())
        if catalog.get('locale') != code or catalog.get('sourceDigest') != source['sourceDigest']:
            raise ValueError(f'{code}: translation source version is stale')
        if catalog.get('pluralsDigest') != source['pluralsDigest']:
            raise ValueError(f'{code}: plural forms are stale')
        for pair in pairs:
            forms = catalog.get('plurals', {}).get(pair['one'], {})
            for category in plural_categories(code):
                error = validate_translation(pair['one'], forms.get(category), code)
                if error:
                    raise ValueError(f'{code}: plural {category}: {error}: {pair["one"][:80]}')
        for message in messages:
            error = validate_translation(message, catalog['messages'].get(message), code)
            if error:
                raise ValueError(f'{code}: {error}: {message[:100]}')
    print(f'Localization current: {len(manifest["locales"])} locales, {len(messages)} messages.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('extract', 'annotate', 'check'))
    args = parser.parse_args()
    try:
        messages = collect(annotate=args.command == 'annotate')
        if args.command == 'check':
            check(messages)
        else:
            write_source(messages)
            print(f'Extracted {len(messages)} English messages.')
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f'Localization: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
