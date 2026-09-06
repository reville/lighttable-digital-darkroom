#!/usr/bin/env python3
"""Validate, review, and bundle source-linked help using only the stdlib.

Each article carries real source anchors. Per-article source and content hashes
record the version reviewed by a maintainer; this is a drift alarm, not a claim
that hashing can prove prose correct. See docs/help/README.md.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ('Getting started', 'Library', 'Editing', 'Film', 'Export',
              'Settings', 'Troubleshooting')
LOCK = 'docs/help/review-lock.json'
BUNDLE = 'web/help-content.json'


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


def run(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'status', 'build', 'review'))
    parser.add_argument('articles', nargs='*', help='Reviewed article IDs (review only)')
    parser.add_argument('--all', action='store_true', help='Acknowledge review of every article')
    args = parser.parse_args(argv)
    if args.command != 'review' and (args.articles or args.all):
        parser.error('Article IDs and --all are valid only with review')
    try:
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
