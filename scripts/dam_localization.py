#!/usr/bin/env python3
"""Build/check DAM translation handoff data without enabling an unfinished runtime."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'docs/localization/dam-update'
TOKENS = re.compile(r'\{\w+\}')
FILES = re.compile(r'(?<![A-Za-z0-9_.])\.?[A-Za-z0-9_{}-]*(?:\.[A-Za-z0-9_{}-]+)*\.'
                   r'(?:json|xmp|ltpreset|lrtemplate|costylepack|costyle|zip|tif|tiff|jpe?g|png|heif|heic|dng|arw|cr[23]|nef|orf|raf|rw2|pdf|csv|sqlite3|db|mov|mp4|m4v)(?![A-Za-z0-9_])', re.I)


def read(path):
    return json.loads(path.read_text())


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def validate_text(source, translated):
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError(f'Missing translation: {source[:80]}')
    for pattern in (TOKENS, FILES):
        if Counter(pattern.findall(source)) != Counter(pattern.findall(translated)):
            raise ValueError(f'Changed literal tokens: {source[:80]}')
    if '<' not in source and re.search(r'<\s*/?\w+[^>]*>', translated):
        raise ValueError('Unexpected markup in translated prose')


def translate_help(bundle, messages, code):
    out = copy.deepcopy(bundle)
    out['locale'] = code
    def translate(source):
        result = messages.get(source)
        validate_text(source, result)
        return result
    for article in out['articles']:
        article['categoryLabel'] = translate(article['category'])
        for key in ('title', 'summary'):
            article[key] = translate(article[key])
        article['keywords'] = list(map(translate, article['keywords']))
        for section in article['sections']:
            section['title'] = translate(section['title'])
            for key in ('paragraphs', 'steps', 'tips'):
                if key in section:
                    section[key] = list(map(translate, section[key]))
    return out


def check_help(english, translated):
    if len(english['articles']) != len(translated['articles']):
        raise ValueError('Localized Help article count differs')
    for original, local in zip(english['articles'], translated['articles']):
        for key in ('id', 'category', 'related'):
            if original.get(key) != local.get(key):
                raise ValueError(f'Changed Help identity or related links: {original["id"]}')
        for key in ('title', 'summary'):
            validate_text(original[key], local.get(key))
        validate_text(original['category'], local.get('categoryLabel'))
        if len(original['sections']) != len(local['sections']):
            raise ValueError('Changed Help sections')
        pairs = [(original['keywords'], local['keywords'])]
        for a, b in zip(original['sections'], local['sections']):
            validate_text(a['title'], b.get('title'))
            pairs.extend((a.get(key, []), b.get(key, [])) for key in ('paragraphs', 'steps', 'tips'))
        for originals, locals_ in pairs:
            if len(originals) != len(locals_):
                raise ValueError('Changed Help paragraph, step, or keyword count')
            for a, b in zip(originals, locals_):
                validate_text(a, b)


def run(args):
    source = read(PACK / 'source.json')
    expected = digest(json.dumps(sorted(set(source['messages'])), ensure_ascii=False,
                                 separators=(',', ':')).encode())
    if expected != source['sourceDigest']:
        raise ValueError('DAM source digest is stale')
    for name, checksum in source['sourceFiles'].items():
        if digest((ROOT / name).read_bytes()) != checksum:
            raise ValueError(f'DAM English source changed: {name}; refresh the translation inventory')
    english = read(ROOT / 'web/help-content.json')
    english_hash = digest((ROOT / 'web/help-content.json').read_bytes())
    manifest = read(args.catalogs / 'manifest.json') if args.catalogs else read(PACK / 'manifest.json')
    outputs = {}
    for locale in manifest['locales']:
        code = locale['code']
        if not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', code):
            raise ValueError('Invalid locale code')
        if args.command == 'build':
            catalog = read(args.catalogs / f'{code}.json')
            messages = {key: catalog['messages'].get(key) for key in source['messages']}
            translated = translate_help(english, catalog['messages'], code)
            pack = {'version': 1, 'locale': code, 'sourceDigest': expected,
                    'generation': catalog.get('generation', {'reviewStatus': 'source-language'}),
                    'messages': messages, 'plurals': {
                        pair['one']: catalog.get('plurals', {}).get(pair['one'], {})
                        for pair in source['pluralPairs']}}
        else:
            pack = read(PACK / 'messages' / f'{code}.json')
            translated = read(PACK / 'help' / f'{code}.json')
        if pack.get('locale') != code or pack.get('sourceDigest') != expected or set(pack['messages']) != set(source['messages']):
            raise ValueError(f'{code}: DAM catalog is stale or incomplete')
        for key, value in pack['messages'].items():
            validate_text(key, value)
        categories = json.loads(subprocess.check_output(['node', '-e',
            'process.stdout.write(JSON.stringify(new Intl.PluralRules(process.argv[1]).resolvedOptions().pluralCategories))', code], text=True))
        for pair in source['pluralPairs']:
            for category in categories:
                validate_text(pair['one'], pack['plurals'].get(pair['one'], {}).get(category))
        if translated.get('locale') != code:
            raise ValueError('Localized Help locale is incorrect')
        check_help(english, translated)
        outputs[PACK / 'messages' / f'{code}.json'] = encoded(pack)
        outputs[PACK / 'help' / f'{code}.json'] = encoded(translated)
    if args.command == 'build':
        manifest.update(sourceDigest=expected, helpSourceDigest=english_hash,
                        runtimeIntegration='pending')
        outputs[PACK / 'manifest.json'] = encoded(manifest)
        # Validate every language before replacing any handoff output.
        for path, content in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    elif manifest.get('sourceDigest') != expected or manifest.get('helpSourceDigest') != english_hash:
        raise ValueError('DAM handoff manifest is stale')
    print(f'DAM localization {args.command}: {len(manifest["locales"])} locales, '
          f'{len(source["messages"])} messages, {len(english["articles"])} Help articles per locale.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'check'])
    parser.add_argument('--catalogs', type=Path)
    args = parser.parse_args()
    if args.command == 'build' and not args.catalogs:
        parser.error('build requires --catalogs pointing to complete translation catalogs')
    try:
        run(args)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f'DAM localization: {error}\n')
