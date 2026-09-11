#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Measured synthetic catalog/slow-I/O benchmark; never reads a real library.

Run: .venv/bin/python bench/storage_readiness.py --output /tmp/storage.json
Cloud flags and metadata latency are injected fixtures. This is not a physical
HDD, network drive, 8 GB RAM, macOS cloud download or image-render benchmark.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import catalog
import catalog_scan


def run(count=10000, slow_ms=.25):
    with tempfile.TemporaryDirectory(prefix='lighttable-storage-bench-') as temp:
        root = Path(temp)
        photos = root / 'photos'
        photos.mkdir()
        for index in range(count):
            folder = photos / f'shoot-{index % 100:03}'
            folder.mkdir(exist_ok=True)
            (folder / f'frame-{index:06}.jpg').write_bytes(
                (f'distinct synthetic capture {index:06}\n'.encode() * 32))
        cat = catalog.Catalog(root / 'catalog.sqlite3')
        source = cat.add_source(photos)
        real_walk, real_hash = catalog_scan.walk_source, catalog_scan.header_hash
        counters = {'headerReads': 0, 'metadataReads': 0, 'placeholderContentReads': 0}
        cloud = True

        def placeholder(path):
            return cloud and int(Path(path).stem.split('-')[1]) % 5 == 0

        def walk(*args, **kwargs):
            for record in real_walk(*args, **kwargs):
                record['availability'] = 'cloud-only' if placeholder(record['path']) else 'local'
                yield record

        def read(kind, path):
            if placeholder(path):
                counters['placeholderContentReads'] += 1
                raise AssertionError('scanner attempted to hydrate a placeholder')
            counters[kind] += 1
            time.sleep(slow_ms / 1000)

        def hashed(path):
            read('headerReads', path)
            return real_hash(path)

        def metadata(path):
            read('metadataReads', path)
            return {'metadata_version': catalog_scan.METADATA_VERSION,
                    'capture_time': '2024-01-02T12:30:00',
                    'camera_model': f'fixture-{int(path.stem.split("-")[1]) % 3}',
                    'width': 6000, 'height': 4000}

        def phase():
            counters.update({key: 0 for key in counters})
            result, failures, query_ms = {}, [], []
            started = time.perf_counter()
            first_rows_ms = None

            def worker():
                try:
                    result.update(catalog_scan.scan_source(cat, source))
                except BaseException as error:
                    failures.append(error)
                finally:
                    cat.close()

            worker_thread = threading.Thread(target=worker)
            worker_thread.start()
            # Bounded; normal default runs take seconds, maximum 300 seconds.
            for _ in range(15000):
                query_started = time.perf_counter()
                page = cat.query({'limit': 100, 'sort': {'field': 'capture'}})
                query_ms.append((time.perf_counter() - query_started) * 1000)
                if page['items'] and first_rows_ms is None:
                    first_rows_ms = (time.perf_counter() - started) * 1000
                if not worker_thread.is_alive():
                    break
                time.sleep(.02)
            worker_thread.join(timeout=1)
            if worker_thread.is_alive():
                raise RuntimeError('benchmark exceeded its 300-second bound')
            if failures:
                raise failures[0]
            return {'elapsedMs': round((time.perf_counter() - started) * 1000, 2),
                    'firstCatalogPageMs': round(first_rows_ms, 2) if first_rows_ms is not None else None,
                    'querySamples': len(query_ms), 'queryMedianMs': round(statistics.median(query_ms), 2),
                    'queryP95Ms': round(sorted(query_ms)[int((len(query_ms) - 1) * .95)], 2),
                    'reads': dict(counters), 'scan': result}

        try:
            with mock.patch.object(catalog_scan, 'walk_source', side_effect=walk), \
                 mock.patch.object(catalog_scan, 'header_hash', side_effect=hashed), \
                 mock.patch.object(catalog_scan, 'read_metadata', side_effect=metadata):
                cold = phase()
                warm = phase()
                cloud = False
                hydrated = phase()
            return {'method': 'Synthetic distinct file headers + injected metadata/latency/cloud flags; cold means empty catalog, not flushed OS disk cache.',
                    'limits': 'Not physical HDD/network/cloud-provider behavior; no 8GB memory constraint; no RAW/image decode or renderer measurement.',
                    'platform': platform.platform(), 'python': platform.python_version(),
                    'fixtureCount': count, 'injectedDelayPerContentOperationMs': slow_ms,
                    'cloudFractionInitially': .2, 'coldMixed': cold, 'warmMixed': warm,
                    'hydrationRescan': hydrated}
        finally:
            cat.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=10000)
    parser.add_argument('--slow-ms', type=float, default=.25)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.count <= 20000 or not 0 <= args.slow_ms <= 1:
        parser.error('count must be 1–20000; slow-ms must be 0–1 (bounded benchmark)')
    result = json.dumps(run(args.count, args.slow_ms), indent=2) + '\n'
    if args.output:
        args.output.write_text(result)
    print(result, end='')


if __name__ == '__main__':
    main()
