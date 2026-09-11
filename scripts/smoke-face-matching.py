#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Exercise real INT8 inference with a CC0 photo; downloads require --download."""
from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageEnhance
from film_lab_ai.face_models import FaceAnalyzer, MODELS, valid_model
from film_lab_ai.face_store import FaceStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    if not args.download and not all(valid_model(args.model_dir / Path(p).name, size, digest)
                                     for p, size, digest in MODELS):
        parser.error('Verified model files are required; use --download to fetch them.')
    analyzer = FaceAnalyzer(args.model_dir)
    analyzer.prepare(lambda: False, lambda _: None)
    try:
        original = (ROOT / 'tests/fixtures/photos/portrait.jpg').read_bytes()
        with Image.open(io.BytesIO(original)) as photo:
            photo.thumbnail((960, 960))
            changed = ImageEnhance.Brightness(photo.convert('RGB')).enhance(.85)
            output = io.BytesIO()
            changed.save(output, format='JPEG', quality=80)
        first, second = analyzer.analyze(original), analyzer.analyze(output.getvalue())
        assert len(first) == len(second) == 1, 'Expected one face in each portrait preview'
        score = float(first[0]['vector'] @ second[0]['vector'])
        assert score >= .55, f'Resized/recompressed portrait failed the matching threshold: {score}'
        blank = io.BytesIO()
        Image.new('RGB', (640, 480), (128, 128, 128)).save(blank, format='JPEG')
        assert not analyzer.analyze(blank.getvalue()), 'A blank image must not create a face'
        with tempfile.TemporaryDirectory() as temp:
            store = FaceStore(Path(temp) / 'faces.sqlite3')
            store.add_photo('original.jpg', 'v1', 'smoke', first)
            group = store.gallery()[0]['id']
            store.edit('rename', {'group': group, 'name': 'Portrait fixture'})
            store.add_photo('transformed.jpg', 'v1', 'smoke', second)
            assert store.stats()['groups'] == 1, 'Strong real-image match must join the named group'
            assert store.labels(['transformed.jpg']) == {'transformed.jpg': ['Portrait fixture']}
            assert not store.suggestions(), 'Automatically matched portrait should not need review'
        print(json.dumps(dict(model=analyzer.capabilities()['model'], faces=1,
                              embeddingDimensions=len(first[0]['vector']),
                              transformedPortraitSimilarity=round(score, 4), blankFaces=0,
                              namedGroupAutoMatch=True)))
    finally:
        analyzer.shutdown()


if __name__ == '__main__':
    main()
