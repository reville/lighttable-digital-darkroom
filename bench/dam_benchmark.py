#!/usr/bin/env python3
"""Benchmark 50k/100k metadata-only catalogs without reading anyone's photos.

Each size runs in a separate bounded subprocess and temporary directory. The
fixture contains catalog rows, never image files. All measured queries/writes
use the production Catalog API. This measures SQLite/Python costs, not server
startup, HTTP, thumbnail decoding, browser scrolling, RAW rendering, or XMP I/O.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest import mock

try:
    import resource
except ImportError:  # Windows has no standard-library process RSS reader.
    resource = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import catalog  # noqa: E402

CATALOG_SHA256 = hashlib.sha256((ROOT / "catalog.py").read_bytes()).hexdigest()


def summary(samples):
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(ordered[max(0, math.ceil(len(samples) * .95) - 1)], 3),
        "max_ms": round(max(samples), 3),
    }


def peak_rss_mib():
    if resource is None:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 ** 2 if sys.platform == "darwin" else 1024), 2)


def fixture_fields(index):
    """Independent repeating dimensions keep compound filters nonempty."""
    return {
        "rating": (index // 3) % 6,
        "camera": ("X-T5", "Z8")[(index // 7) % 2],
        "lens": ("35mm F2", "50mm F1.8", "85mm F1.8")[index % 3],
        "year": 2020 + (index // 18) % 7,
        "snow": index % 17 == 0,
        "source": 1 + index % 2,
        "keyword": 1 + index % 3,
        "iso": (100, 400, 800, 1600, 3200)[(index // 5) % 5],
        "focal_length": (35, 50, 85)[index % 3],
        "aperture": (1.8, 2.8, 4, 8)[(index // 9) % 4],
        "shutter_seconds": (1 / 30, 1 / 125, 1 / 250, 1 / 1000)[(index // 13) % 4],
    }


def seed_catalog(path, count):
    """Direct fixture insertion is confined to this new disposable catalog."""
    cat = catalog.Catalog(path)
    local = path.parent / "empty-online-source"
    local.mkdir()
    offline = path.parent / "disconnected-drive"
    source_ids = [cat.add_source(local), cat.add_source(offline)]
    folder_count = min(400, max(2, count // 250))
    folder_count += folder_count % 2
    with cat.write() as conn:
        conn.execute("UPDATE sources SET available=0 WHERE id=?", (source_ids[1],))
        conn.executemany(
            "INSERT INTO folders(id,source_id,relpath,name) VALUES(?,?,?,?)",
            ((i + 1, source_ids[i % 2], f"shoot-{i:03}", f"shoot-{i:03}")
             for i in range(folder_count)),
        )
        conn.executemany("INSERT INTO keywords(id,name,path) VALUES(?,?,?)", (
            (1, "Snow", "Places > Snow"), (2, "Coast", "Places > Coast"),
            (3, "Portrait", "People > Portrait"),
        ))
    # At most one 1,000-record fixture batch is held in Python memory.
    for start in range(0, count, 1000):
        files, images, states, keywords, search, overrides = [], [], [], [], [], []
        for i in range(start, min(start + 1000, count)):
            f = fixture_fields(i)
            image_id = i + 1
            folder = i % folder_count
            filename = f"frame-{i:06}.CR3"
            captured = f"{f['year']}-06-{1 + i % 28:02}T12:30:{i % 60:02}"
            files.append((image_id, source_ids[i % 2], folder + 1,
                f"shoot-{folder:03}/{filename}", filename, ".cr3", "raw",
                48_000_000, 1_700_000_000_000_000_000 + i,
                captured, f"{i:040x}", captured,
                "Fujifilm" if f["camera"] == "X-T5" else "Nikon",
                f["camera"], f["lens"], f["iso"], f["focal_length"],
                f["aperture"], f["shutter_seconds"], 8000, 6000, 1,
                "offline" if i % 2 else "local", 1_700_000_000 + i))
            images.append((image_id, image_id, filename, 1_700_000_000 + i))
            states.append((image_id, f["rating"],
                ("pending", "approved", "skipped")[(i // 11) % 3],
                ("none", "red", "yellow", "green", "blue", "purple")[(i // 13) % 6],
                '{"exposure":0.25,"contrast":0.1}' if i % 7 == 0 else None))
            keywords.append((image_id, f["keyword"]))
            keyword = ("Places > Snow", "Places > Coast", "People > Portrait")[i % 3]
            caption = "A dog running in snow" if f["snow"] else "Travel photograph"
            search.append((image_id, image_id, filename, keyword, "", caption,
                f["camera"], f["lens"]))
            if i % 101 == 0:
                # Overrides exercise the real effective-capture sort path.
                overrides.append((image_id, captured.replace("T12:", "T13:"), 1_700_000_000))
        with cat.write() as conn:
            conn.executemany(
                "INSERT INTO files(id,source_id,folder_id,relpath,filename,ext,kind,"
                "size,mtime_ns,mtime_iso,header_hash,capture_time,camera_make,"
                "camera_model,lens,iso,focal_length,aperture,shutter_seconds,"
                "width,height,orientation,availability,added_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", files)
            conn.executemany("INSERT INTO images(id,file_id,display_name,created_at) VALUES(?,?,?,?)", images)
            conn.executemany("INSERT INTO image_state(image_id,rating,status,label,grade_json) VALUES(?,?,?,?,?)", states)
            conn.executemany("INSERT INTO image_keywords(image_id,keyword_id) VALUES(?,?)", keywords)
            conn.executemany("INSERT INTO image_search(rowid,image_id,filename,keywords,title,caption,camera,lens) VALUES(?,?,?,?,?,?,?,?)", search)
            conn.executemany("INSERT INTO iptc(image_id,caption) VALUES(?,?)",
                ((row[0], row[5]) for row in search))
            conn.executemany("INSERT INTO capture_overrides(file_id,capture_time,updated_at) VALUES(?,?,?)", overrides)
    rules = {"ratingMin": 4, "lens": "50mm", "dateFrom": "2026-01-01", "dateTo": "2026-12-31"}
    smart = cat.add_collection("Four-star 50mm photographs from 2026", kind="smart", rules=rules)
    regular = cat.add_collection("Selected shoot")
    cat.set_collection_members(regular, list(range(1, min(count, 1000) + 1)))
    cat.checkpoint(truncate=True)
    assert cat.connection.execute("PRAGMA foreign_key_check").fetchone() is None
    cat.close()
    return {"sources": source_ids, "smart": smart, "regular": regular,
            "folder_count": folder_count, "smart_rules": rules}


def query_cases(count, fixture, page_size):
    cases = [
        ("capture_first_page", {}, lambda i, f: True),
        ("capture_deep_page", {"offset": max(0, count - page_size)}, lambda i, f: True),
        ("name_first_page", {"sort": {"field": "name"}}, lambda i, f: True),
        ("rating_filter", {"filter": {"ratingMin": 4}}, lambda i, f: f["rating"] >= 4),
        ("camera_lens_filter", {"filter": {"camera": "X-T5", "lens": "50mm"}}, lambda i, f: f["camera"] == "X-T5" and f["lens"] == "50mm F1.8"),
        ("numeric_exif_filter", {"filter": {"isoMin": 800, "focalLengthMin": 50, "focalLengthMax": 50, "apertureMax": 4, "shutterMax": "1/125"}}, lambda i, f: f["iso"] >= 800 and f["focal_length"] == 50 and f["aperture"] <= 4 and f["shutter_seconds"] <= 1 / 125),
        ("capture_year_filter", {"filter": {"dateFrom": "2026-01-01", "dateTo": "2026-12-31"}}, lambda i, f: f["year"] == 2026),
        ("fts_caption_search", {"filter": {"query": "dog snow"}}, lambda i, f: f["snow"]),
        ("hierarchical_keyword", {"filter": {"keyword": "Places"}}, lambda i, f: f["keyword"] in (1, 2)),
        ("offline_source", {"scope": "source", "sourceId": fixture["sources"][1]}, lambda i, f: f["source"] == 2),
        ("smart_collection", {"scope": "collection", "collectionId": fixture["smart"]}, lambda i, f: f["rating"] >= 4 and f["lens"] == "50mm F1.8" and f["year"] == 2026),
        ("regular_collection", {"scope": "collection", "collectionId": fixture["regular"]}, lambda i, f: i < 1000),
    ]
    return [(name, {"limit": page_size, **spec},
             sum(predicate(i, fixture_fields(i)) for i in range(count)))
            for name, spec, predicate in cases]


def query_plan(cat, spec):
    statements = []
    cat.connection.set_trace_callback(statements.append)
    try:
        cat.query(spec, include_state=True)
    finally:
        cat.connection.set_trace_callback(None)
    return [{"sql": sql, "plan": [row[3] for row in cat.connection.execute("EXPLAIN QUERY PLAN " + sql)]}
        for sql in statements if sql.startswith(("SELECT COUNT(*)", "SELECT i.id,", "WITH page AS ("))]


def run_worker(count, iterations, page_size):
    with tempfile.TemporaryDirectory(prefix="lighttable-dam-bench-") as temp:
        path = Path(temp) / "synthetic.sqlite3"
        started = time.perf_counter()
        fixture = seed_catalog(path, count)
        seed_ms = (time.perf_counter() - started) * 1000
        seeded_rss = peak_rss_mib()
        started = time.perf_counter()
        cat = catalog.Catalog(path)
        open_ms = (time.perf_counter() - started) * 1000
        result = {"images": count, "page_size": page_size, "fixture_seed_ms": round(seed_ms, 2),
            "catalog_sha256": CATALOG_SHA256, "catalog_schema": catalog.SCHEMA_VERSION,
            "catalog_reopen_ms": round(open_ms, 3), "database_mib": round(path.stat().st_size / 1024 ** 2, 2),
            "source_image_files": 0, "fixture": fixture, "queries": {}}
        try:
            # Catalog reads/writes remain usable with unavailable originals and
            # must not invoke a filesystem walk. Source availability may stat
            # two directories; no source image exists to read or render.
            with mock.patch("os.scandir", side_effect=AssertionError("catalog operation attempted a directory scan")):
                assert [s["available"] for s in sorted(cat.sources(), key=lambda s: s["id"])] == [True, False]
                for name, spec, expected in query_cases(count, fixture, page_size):
                    samples = []
                    for _ in range(iterations):
                        started = time.perf_counter()
                        page = cat.query(spec, include_state=True)
                        samples.append((time.perf_counter() - started) * 1000)
                        assert page["total"] == expected, (name, page["total"], expected)
                        assert len(page["items"]) == min(page_size, max(0, expected - spec.get("offset", 0)))
                    started = time.perf_counter()
                    encoded = json.dumps(page, separators=(",", ":")).encode()
                    result["queries"][name] = {**summary(samples), "first_ms": round(samples[0], 3),
                        "total": page["total"], "returned": len(page["items"]),
                        "json_kib": round(len(encoded) / 1024, 2),
                        "json_encode_ms": round((time.perf_counter() - started) * 1000, 3)}
                result["plans"] = {name: query_plan(cat, spec)
                    for name, spec in (("capture_first_page", {"limit": page_size}),
                        ("fts_caption_search", {"limit": page_size, "filter": {"query": "dog snow"}}),
                        ("offline_source", {"limit": page_size, "scope": "source", "sourceId": fixture["sources"][1]}))}
                result["fts_lookup_plans"] = {
                    key: [row[3] for row in cat.connection.execute(
                        "EXPLAIN QUERY PLAN SELECT rowid FROM image_search WHERE " + clause)]
                    for key, clause in (("legacy_unindexed_image_id", "image_id=1"),
                                        ("keyed_rowid", "rowid=1"))}
                # Tracing is separate so Python allocation accounting does not
                # contaminate query timings. SQLite memory is reflected in RSS.
                tracemalloc.start()
                cat.query({"limit": page_size}, include_state=True)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                result["page_python_peak_mib"] = round(peak / 1024 ** 2, 3)
                samples = []
                for n in range(iterations):
                    image_id = 1 + (n * 7919) % count
                    rating = (cat.state_for(image_id)["rating"] + 1) % 6
                    started = time.perf_counter()
                    cat.save_state(image_id, {"rating": rating})
                    samples.append((time.perf_counter() - started) * 1000)
                    assert cat.state_for(image_id)["rating"] == rating
                result["rating_save"] = summary(samples)
                batch = {i: {"rating": 5} for i in range(1, min(count, 100) + 1)}
                started = time.perf_counter()
                cat.save_states(batch)
                result["rating_batch_100_ms"] = round((time.perf_counter() - started) * 1000, 3)
                samples, tagged = [], set()
                for n in range(iterations):
                    image_id = 1 + (n * 7919) % count
                    keywords = cat.keywords_for(image_id) + ["Benchmark > Selection"]
                    started = time.perf_counter()
                    cat.save_state(image_id, {"keywords": keywords})
                    samples.append((time.perf_counter() - started) * 1000)
                    tagged.add(image_id)
                assert cat.query({"filter": {"query": "Benchmark"}})["total"] == len(tagged)
                result["keyword_save_with_fts_update"] = summary(samples)
                cat.close()
                cat = catalog.Catalog(path)
                assert all(cat.state_for(i)["rating"] == 5 for i in batch)
                result["offline_queries_without_scan"] = True
                result["rating_saved_after_reopen"] = True
            result["peak_rss_after_seed_mib"] = seeded_rss
            result["peak_rss_process_mib"] = peak_rss_mib()
            final_rss = result["peak_rss_process_mib"]
            result["peak_rss_growth_after_seed_mib"] = (round(final_rss - seeded_rss, 2)
                if final_rss is not None and seeded_rss is not None else None)
            return result
        finally:
            cat.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[50000, 100000])
    parser.add_argument("--iterations", type=int, default=7)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--timeout", type=int, default=240, help="Hard subprocess limit, seconds per size")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    sizes = [args.worker] if args.worker is not None else args.sizes
    if not all(1 <= n <= 1000000 for n in sizes) or not 1 <= args.iterations <= 100 or not 1 <= args.page_size <= 5000 or args.timeout < 1:
        parser.error("sizes: 1..1000000; iterations: 1..100; page size: 1..5000; timeout must be positive")
    if args.worker is not None:
        print(json.dumps(run_worker(args.worker, args.iterations, args.page_size)))
        return
    output = {"schema": 1, "scope": "Synthetic catalog API only; no server/browser/render/XMP timings",
        "platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "catalog_sha256": CATALOG_SHA256, "runs": []}
    for count in args.sizes:
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(count),
            "--iterations", str(args.iterations), "--page-size", str(args.page_size)],
            cwd=ROOT, capture_output=True, text=True, timeout=args.timeout, check=True)
        output["runs"].append(json.loads(completed.stdout))
        print(f"Measured {count:,} catalog records", file=sys.stderr, flush=True)
    encoded = json.dumps(output, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
