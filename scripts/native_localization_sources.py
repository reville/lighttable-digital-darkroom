#!/usr/bin/env python3
"""Extract/check native English templates; reject unwrapped native UI literals.

Run with --write after intentional native copy changes, then regenerate catalogs.
The default check never acknowledges or changes the inventory.
"""
import argparse
import json
from pathlib import Path
import re
import plistlib

ROOT = Path(__file__).resolve().parents[1]
FILES = ('app/main.swift', 'app/DiagnosticReports.swift', 'windows-shell/src/main.rs', 'windows-shell/src/localization.rs', 'app/Info.plist')
PLIST_KEYS = ('NSPhotoLibraryUsageDescription',)
LITERAL = r'"(?:[^"\\]|\\.)*"'
CALL = re.compile(r'\b(?:L|tr|tr_args)\(\s*(' + LITERAL + r')')
# These interfaces present app-authored text. Localized variables and host/system
# errors are allowed; newly added plain English literals must use L/tr explicitly.
RAW = re.compile(r'(?:\btitle:\s*|\bwithTitle:\s*|\.(?:messageText|informativeText|prompt|title|message)\s*=\s*|\b(?:showSplash|showFatal)\(\s*|\.(?:set_title|set_description|add_filter)\(\s*)(' + LITERAL + r')')

def extract_native_sources(root=ROOT):
    messages = set()
    errors = []
    for relative in FILES:
        text = (root / relative).read_text()
        for match in CALL.finditer(text):
            literal = match.group(1)
            if r'\(' in literal or literal.startswith('""'):
                errors.append(f'{relative}:{text.count(chr(10), 0, match.start()) + 1}: use a single English string with named placeholders')
                continue
            try:
                value = json.loads(literal)
            except json.JSONDecodeError as error:
                errors.append(f'{relative}: unsupported string escape: {error}')
                continue
            if value and value != 'LightTable':
                messages.add(value)
        for match in RAW.finditer(text):
            value = match.group(1)
            if value == '"LightTable"':
                continue  # Product name remains unchanged in every language.
            errors.append(f'{relative}:{text.count(chr(10), 0, match.start()) + 1}: native UI string needs L/tr: {value[:100]}')
    plist = plistlib.loads((root / 'app/Info.plist').read_bytes())
    for key in PLIST_KEYS:
        value = plist.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f'app/Info.plist: {key} must contain its English permission text')
        else:
            messages.add(value)
    if errors:
        raise ValueError('\n'.join(errors))
    return sorted(messages)


def bundle_localizations(app, catalogs, root=ROOT):
    """Package OS permission text only after every declared locale validates."""
    plist_path = app / 'Contents/Info.plist'
    target = plistlib.loads(plist_path.read_bytes())
    source = plistlib.loads((root / 'app/Info.plist').read_bytes())
    manifest = json.loads((catalogs / 'manifest.json').read_text())
    if manifest.get('version') != 1 or manifest.get('sourceLocale') != 'en':
        raise ValueError('Unsupported native localization manifest')
    codes = [entry.get('code', '') for entry in manifest.get('locales', [])]
    if (not codes or 'en' not in codes or len(codes) != len(set(codes))
            or any(not isinstance(code, str) or not re.fullmatch(
                r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', code) for code in codes)):
        raise ValueError('Invalid native localization language codes')
    translated_plists = {}
    for code in codes:
        catalog = json.loads((catalogs / f'{code}.json').read_text())
        if catalog.get('version') != 1 or catalog.get('locale') != code:
            raise ValueError(f'{code}: invalid native translation catalog')
        messages = catalog.get('messages', {})
        values = {}
        for key in PLIST_KEYS:
            english = source[key]
            value = english if code == 'en' else messages.get(english)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{code}: missing {key} translation')
            values[key] = value
        translated_plists[code] = plistlib.dumps(values)
    for code, data in translated_plists.items():
        directory = app / 'Contents/Resources' / f'{code}.lproj'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'InfoPlist.strings').write_bytes(data)
    target['CFBundleDevelopmentRegion'] = 'en'
    target['CFBundleLocalizations'] = codes
    for key in PLIST_KEYS:
        target[key] = source[key]
    plist_path.write_bytes(plistlib.dumps(target))
    return len(codes)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--bundle', type=Path, help='Package translated OS permission text into this macOS app')
    parser.add_argument('--catalogs', type=Path, default=ROOT / 'web/locales')
    args = parser.parse_args()
    if args.bundle:
        try:
            count = bundle_localizations(args.bundle, args.catalogs)
        except (ValueError, OSError, KeyError, TypeError) as error:
            parser.exit(1, str(error) + '\n')
        print(f'Packaged native permission text for {count} locales.')
        return
    try:
        messages = extract_native_sources()
    except ValueError as error:
        parser.exit(1, str(error) + '\n')
    path = ROOT / 'docs/localization/native-source.json'
    if args.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(messages, ensure_ascii=False, indent=2) + '\n')
        print(f'Wrote {len(messages)} native messages.')
    else:
        stored = json.loads(path.read_text()) if path.is_file() else None
        if stored != messages:
            parser.exit(1, 'Native message inventory is stale. Run python3 scripts/native_localization_sources.py --write and update translations.\n')
        print(f'Native inventory matches {len(messages)} source templates.')
if __name__ == '__main__':
    main()
