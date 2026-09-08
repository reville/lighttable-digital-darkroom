#!/usr/bin/env python3
"""Validate, review, and bundle source-linked help using only the stdlib.

Each article carries real source anchors. Per-article source and content hashes
record the version reviewed by a maintainer; this is a drift alarm, not a claim
that hashing can prove prose correct. See docs/help/README.md.
"""
import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ('Getting started', 'Library', 'Editing', 'Film', 'Export',
              'Settings', 'Troubleshooting')
LOCK = 'docs/help/review-lock.json'
BUNDLE = 'web/help-content.json'
LOCALIZATION_SOURCE = 'docs/localization/source.json'
LOCALE_MANIFEST = 'web/locales/manifest.json'
LOCALE_PATTERN = re.compile(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*')
NAMED_TOKENS = re.compile(r'\{\w+\}')
# These are literal file names/extensions, not words to translate. Keep the
# leading dot in hidden portable-state files and extension-only chooser labels.
FILE_TOKENS = re.compile(
    r'(?<![A-Za-z0-9_.])\.?[A-Za-z0-9_{}-]*(?:\.[A-Za-z0-9_{}-]+)*\.'
    r'(?:json|xmp|ltpreset|lrtemplate|costylepack|costyle|zip|tif|tiff|jpe?g|'
    r'png|heif|heic|dng|arw|cr[23]|nef|orf|raf|rw2|pdf|csv|sqlite3|db|mov|mp4|m4v)'
    r'(?![A-Za-z0-9_])', re.IGNORECASE)


class HelpError(ValueError):
    pass


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise HelpError(f'{path.name}: {error}') from error


def require_text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise HelpError(f'{where}: expected nonempty text')


def text_list(value, where, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise HelpError(f'{where}: expected a list of text')
    for item in value:
        require_text(item, where)


def load_articles(root=ROOT):
    articles = []
    for path in sorted((root / 'docs/help').glob('*.json')):
        if path.name == 'review-lock.json':
            continue
        content = read_json(path)
        if not isinstance(content, list):
            raise HelpError(f'{path.name}: expected an article array')
        articles.extend(content)
    if not articles:
        raise HelpError('No help articles found')
    ids = set()
    for article in articles:
        if not isinstance(article, dict):
            raise HelpError('Each article must be an object')
        aid = article.get('id', '')
        if not isinstance(aid, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', aid):
            raise HelpError(f'Invalid article ID: {aid!r}')
        if aid in ids:
            raise HelpError(f'Duplicate article ID: {aid}')
        ids.add(aid)
        for key in ('title', 'summary'):
            require_text(article.get(key), f'{aid}.{key}')
        if article.get('category') not in CATEGORIES:
            raise HelpError(f'{aid}: unknown category')
        text_list(article.get('keywords'), f'{aid}.keywords', nonempty=True)
        text_list(article.get('related', []), f'{aid}.related')
        if not isinstance(article.get('sections'), list) or not article['sections']:
            raise HelpError(f'{aid}: needs sections')
        for section in article['sections']:
            if not isinstance(section, dict):
                raise HelpError(f'{aid}: invalid section')
            require_text(section.get('title'), f'{aid}.section.title')
            for key in ('paragraphs', 'steps', 'tips'):
                text_list(section.get(key, []), f'{aid}.section.{key}')
            section.setdefault('paragraphs', [])
            if not any(section.get(key) for key in ('paragraphs', 'steps', 'tips')):
                raise HelpError(f'{aid}: empty section')
        if not isinstance(article.get('sources'), list) or not article['sources']:
            raise HelpError(f'{aid}: needs source evidence')
        for source in article['sources']:
            if not isinstance(source, dict):
                raise HelpError(f'{aid}: invalid source')
            relative = source.get('path')
            require_text(relative, f'{aid}.source.path')
            path = (root / relative).resolve()
            if Path(relative).is_absolute() or '..' in Path(relative).parts or not path.is_relative_to(root.resolve()):
                raise HelpError(f'{aid}: source must stay inside repository: {relative}')
            if not path.is_file() or relative.startswith('docs/help/') or relative == BUNDLE:
                raise HelpError(f'{aid}: missing implementation source: {relative}')
            require_text(source.get('anchor'), f'{aid}.source.anchor')
            if source['anchor'] not in path.read_text():
                raise HelpError(f'{aid}: source anchor no longer exists in {relative}: {source["anchor"]!r}')
    for article in articles:
        for related in article.get('related', []):
            if related not in ids or related == article['id']:
                raise HelpError(f'{article["id"]}: invalid related article {related}')
    return sorted(articles, key=lambda item: (CATEGORIES.index(item['category']), item['title']))


def current_records(articles, root=ROOT):
    source_hashes = {}
    for article in articles:
        for source in article['sources']:
            path = source['path']
            if path not in source_hashes:
                source_hashes[path] = digest((root / path).read_bytes())
    return {article['id']: {
        'content': digest(article),
        'sources': {source['path']: source_hashes[source['path']] for source in article['sources']},
    } for article in articles}


def read_lock(root=ROOT):
    path = root / LOCK
    if not path.exists():
        return {}
    lock = read_json(path)
    if not isinstance(lock, dict) or lock.get('version') != 1 or not isinstance(lock.get('articles'), dict):
        raise HelpError('Invalid help review lock')
    return lock['articles']


def drift(current, reviewed):
    issues = {}
    for aid, record in current.items():
        prior = reviewed.get(aid)
        if not isinstance(prior, dict):
            issues[aid] = ['new article; review required']
            continue
        reasons = []
        if record['content'] != prior.get('content'):
            reasons.append('article content or source links changed')
        old_sources = prior.get('sources', {})
        for path, source_hash in record['sources'].items():
            if source_hash != old_sources.get(path):
                reasons.append(f'source changed: {path}')
        if reasons:
            issues[aid] = reasons
    for aid in reviewed.keys() - current.keys():
        issues[aid] = ['article removed; rebuild the review lock']
    return issues


def bundle_text(articles):
    # Evidence belongs in the authoring system, not the reader's instructions.
    public = [{key: article[key] for key in
               ('id', 'title', 'category', 'summary', 'keywords', 'sections', 'related') if key in article}
              for article in articles]
    return json.dumps({'version': 1, 'articles': public}, ensure_ascii=False, indent=2) + '\n'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def localization_source_digest(messages):
    """Shared UI/help message identity; compact, sorted, unique UTF-8 JSON."""
    canonical = json.dumps(sorted(set(messages)), ensure_ascii=False,
                           separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def load_help_bundle(root=ROOT):
    """Read the English public bundle without consulting or changing reviews."""
    bundle = read_json(root / BUNDLE)
    if not isinstance(bundle, dict) or bundle.get('version') != 1:
        raise HelpError('Invalid English help bundle version')
    articles = bundle.get('articles')
    if not isinstance(articles, list) or not articles:
        raise HelpError('English help bundle needs articles')
    ids = set()
    article_fields = {'id', 'title', 'category', 'summary', 'keywords', 'sections', 'related'}
    for article in articles:
        if not isinstance(article, dict) or set(article) - article_fields:
            raise HelpError('English help article contains unsupported fields')
        aid = article.get('id', '')
        if not isinstance(aid, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', aid):
            raise HelpError('English help article has an invalid ID')
        if aid in ids:
            raise HelpError(f'Duplicate English help article ID: {aid}')
        ids.add(aid)
        if article.get('category') not in CATEGORIES:
            raise HelpError(f'{aid}: unknown English help category')
        for key in ('title', 'summary'):
            require_text(article.get(key), f'{aid}.{key}')
        text_list(article.get('keywords'), f'{aid}.keywords', nonempty=True)
        text_list(article.get('related', []), f'{aid}.related')
        sections = article.get('sections')
        if not isinstance(sections, list) or not sections:
            raise HelpError(f'{aid}: English help needs sections')
        for section in sections:
            if not isinstance(section, dict) or set(section) - {'title', 'paragraphs', 'steps', 'tips'}:
                raise HelpError(f'{aid}: unsupported English help section fields')
            require_text(section.get('title'), f'{aid}.section.title')
            for key in ('paragraphs', 'steps', 'tips'):
                text_list(section.get(key, []), f'{aid}.section.{key}')
            if not any(section.get(key) for key in ('paragraphs', 'steps', 'tips')):
                raise HelpError(f'{aid}: English help section is empty')
    for article in articles:
        if any(aid not in ids or aid == article['id'] for aid in article.get('related', [])):
            raise HelpError(f'{article["id"]}: invalid English help related links')
    return bundle


def help_messages(bundle):
    """Return every translatable help string for the shared source manifest.

    English category values stay as stable filter keys; their translated labels
    are included as messages and written into categoryLabel in localized help.
    """
    messages = set(CATEGORIES)
    for article in bundle['articles']:
        messages.update(article[key] for key in ('title', 'category', 'summary'))
        messages.update(article['keywords'])
        for section in article['sections']:
            messages.add(section['title'])
            for key in ('paragraphs', 'steps', 'tips'):
                messages.update(section.get(key, []))
    return sorted(messages)


def read_localization_source(root=ROOT):
    source = read_json(root / LOCALIZATION_SOURCE)
    if not isinstance(source, dict) or source.get('version') != 1:
        raise HelpError('Invalid localization source manifest')
    messages = source.get('messages')
    text_list(messages, 'localization source messages', nonempty=True)
    if messages != sorted(set(messages)):
        raise HelpError('Localization source messages must be sorted and unique')
    current_digest = localization_source_digest(messages)
    if source.get('sourceDigest') != current_digest:
        raise HelpError('Localization source manifest digest is stale')
    return messages, current_digest


def declared_locales(root=ROOT):
    manifest = read_json(root / LOCALE_MANIFEST)
    if not isinstance(manifest, dict) or manifest.get('version') != 1:
        raise HelpError('Invalid locale manifest')
    entries = manifest.get('locales')
    if not isinstance(entries, list) or not entries:
        raise HelpError('Locale manifest needs declared locales')
    locales = []
    for entry in entries:
        locale = entry.get('code') if isinstance(entry, dict) else entry
        if not isinstance(locale, str) or not LOCALE_PATTERN.fullmatch(locale):
            raise HelpError(f'Invalid locale code: {locale!r}')
        if locale in locales:
            raise HelpError(f'Duplicate locale code: {locale}')
        locales.append(locale)
    if 'en' not in locales:
        raise HelpError('Locale manifest must declare English as en')
    return locales


def literal_inputs(source):
    # This word is an input command accepted by capture_time.py, not prose.
    return ('remove',) if 'enter remove to clear it.' in source else ()


def locale_script_issue(source, translated, locale):
    """Catch accidental foreign-script fragments, not translation quality.

    Latin product names and technical terms are valid in every locale. Keep
    characters already present in the source (for example, a quoted filename),
    shared combining accents, and the scripts normally used by the target.
    """
    scripts = {
        'ru': ('CYRILLIC',),
        'zh': ('CJK', 'IDEOGRAPHIC'),
        'ja': ('CJK', 'IDEOGRAPHIC', 'HIRAGANA', 'KATAKANA'),
        'ko': ('CJK', 'IDEOGRAPHIC', 'HANGUL'),
        'ar': ('ARABIC',), 'hi': ('DEVANAGARI',),
        'bn': ('BENGALI',), 'th': ('THAI',),
    }.get(locale.split('-')[0], ())
    allowed = ('LATIN', 'COMBINING', *scripts)
    unexpected = set()
    for character in translated:
        if character in source or not unicodedata.category(character).startswith(('L', 'M')):
            continue
        name = unicodedata.name(character, '')
        # Unicode classifies the ordinary Spanish/Portuguese ordinal indicators
        # as letters, but their names do not contain LATIN.
        if character in 'ªº' or any(script in name for script in allowed):
            continue
        unexpected.add(character)
    if unexpected:
        names = ', '.join(unicodedata.name(character, f'U+{ord(character):04X}')
                          for character in sorted(unexpected)[:4])
        return f'unexpected script for {locale}: {names}'
    return None


def validate_translation(source, translated, locale):
    require_text(translated, f'{locale} translation of {source[:70]!r}')
    script_issue = locale_script_issue(source, translated, locale)
    if script_issue:
        raise HelpError(script_issue)
    if Counter(NAMED_TOKENS.findall(source)) != Counter(NAMED_TOKENS.findall(translated)):
        raise HelpError(f'{locale}: named placeholders changed in {source[:90]!r}')
    if Counter(FILE_TOKENS.findall(source)) != Counter(FILE_TOKENS.findall(translated)):
        raise HelpError(f'{locale}: literal filenames or extensions changed in {source[:90]!r}')
    for token in literal_inputs(source):
        if not re.search(r'(?<!\w)' + re.escape(token) + r'(?!\w)', translated):
            raise HelpError(f'{locale}: literal input {token!r} must remain unchanged')


def localized_help_text(bundle, catalog, locale, source_messages, source_digest):
    """Validate a complete shared catalog and compile help with stable IDs.

    There is deliberately no get(msgid, msgid) fallback. An explicit identity
    translation is valid for technical names, but a missing entry is an error.
    """
    if not isinstance(catalog, dict) or catalog.get('version') != 1:
        raise HelpError(f'{locale}: invalid translation catalog version')
    if catalog.get('locale') != locale:
        raise HelpError(f'{locale}: translation catalog locale does not match its filename')
    if catalog.get('sourceDigest') != source_digest:
        raise HelpError(f'{locale}: stale translation sourceDigest')
    translations = catalog.get('messages')
    if not isinstance(translations, dict):
        raise HelpError(f'{locale}: translation messages must be an object')
    expected = set(source_messages)
    missing = expected - translations.keys()
    extra = translations.keys() - expected
    if missing:
        raise HelpError(f'{locale}: {len(missing)} missing translations; first: {sorted(missing)[0]!r}')
    if extra:
        raise HelpError(f'{locale}: {len(extra)} obsolete translations; refresh the catalog')
    missing_help = set(help_messages(bundle)) - expected
    if missing_help:
        raise HelpError(f'Localization source manifest is missing {len(missing_help)} help messages; '
                        f'first: {sorted(missing_help)[0]!r}')
    for source in source_messages:
        validate_translation(source, translations[source], locale)
    localized = copy.deepcopy(bundle)
    localized.update(locale=locale, sourceDigest=source_digest)
    for article in localized['articles']:
        article['categoryLabel'] = translations[article['category']]
        for key in ('title', 'summary'):
            article[key] = translations[article[key]]
        article['keywords'] = [translations[value] for value in article['keywords']]
        for section in article['sections']:
            section['title'] = translations[section['title']]
            for key in ('paragraphs', 'steps', 'tips'):
                if key in section:
                    section[key] = [translations[value] for value in section[key]]
    return json.dumps(localized, ensure_ascii=False, indent=2) + '\n'


def run_localization(command, locales=None, root=ROOT):
    if command not in ('localization-build', 'localization-check'):
        raise HelpError('Unknown help localization command')
    bundle = load_help_bundle(root)
    source_messages, source_digest = read_localization_source(root)
    declared = declared_locales(root)
    selected = list(dict.fromkeys(locales)) if locales else [code for code in declared if code != 'en']
    unknown = set(selected) - set(declared)
    if unknown:
        raise HelpError(f'Locales are not declared: {", ".join(sorted(unknown))}')
    if 'en' in selected:
        raise HelpError('English uses web/help-content.json; run build/check for English')
    if not selected:
        raise HelpError('No translated help locales declared')
    # Validate all selected locales before writing any bundle. A missing final
    # language must not leave a partially refreshed set that looks complete.
    outputs = []
    for locale in selected:
        catalog = read_json(root / 'web/locales' / f'{locale}.json')
        expected = localized_help_text(bundle, catalog, locale, source_messages, source_digest)
        target = root / 'web/locales/help' / f'{locale}.json'
        if command == 'localization-check' and (not target.is_file() or target.read_text() != expected):
            raise HelpError(f'{locale}: localized help is missing or stale; run localization-build')
        outputs.append((target, expected))
    if command == 'localization-build':
        for target, expected in outputs:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected, encoding='utf-8')
    print(f'Help {command}: {len(selected)} locales, {len(bundle["articles"])} articles each; '
          'complete translations and source versions current.')
    return 0


def run(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'status', 'build', 'review',
                                          'localization-build', 'localization-check'))
    parser.add_argument('articles', nargs='*', help='Reviewed article IDs (review only)')
    parser.add_argument('--all', action='store_true', help='Acknowledge review of every article')
    parser.add_argument('--locale', action='append', help='Limit localization commands to a declared locale; repeatable')
    args = parser.parse_args(argv)
    if args.command != 'review' and (args.articles or args.all):
        parser.error('Article IDs and --all are valid only with review')
    if args.locale and not args.command.startswith('localization-'):
        parser.error('--locale is valid only with localization-build/localization-check')
    try:
        if args.command.startswith('localization-'):
            return run_localization(args.command, args.locale, root)
        articles = load_articles(root)
        current = current_records(articles, root)
        reviewed = read_lock(root)
        if args.command == 'review':
            if not args.all and not args.articles:
                raise HelpError('Name the articles you reviewed, or use --all after reviewing all content')
            selected = set(current) if args.all else set(args.articles)
            unknown = selected - set(current) - set(reviewed)
            if unknown:
                raise HelpError(f'Unknown article IDs: {", ".join(sorted(unknown))}')
            for aid in selected:
                if aid in current:
                    reviewed[aid] = current[aid]
                else:
                    reviewed.pop(aid, None)
            if args.all:
                reviewed = current
            write_json(root / LOCK, {'version': 1, 'articles': reviewed})
            print(f'Recorded review of {len(selected)} articles. Run build to refresh the bundled help.')
            return 0
        issues = drift(current, reviewed)
        if issues:
            for aid, reasons in sorted(issues.items()):
                print(f'{aid}: {"; ".join(reasons)}')
            print('Review these articles against the changed code, then acknowledge their IDs with the review command.')
            return 1
        expected = bundle_text(articles)
        target = root / BUNDLE
        if args.command == 'build':
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected)
        elif args.command == 'check' and (not target.exists() or target.read_text() != expected):
            raise HelpError('Bundled help is stale. Run python3 scripts/help_content.py build')
        print(f'Help {args.command}: {len(articles)} articles, source anchors and reviewed versions current.')
        return 0
    except (HelpError, OSError, UnicodeError) as error:
        print(f'Help check failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(run())
