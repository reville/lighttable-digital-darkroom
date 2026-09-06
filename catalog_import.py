"""Reading a Lightroom Classic catalog into ours.

A `.lrcat` file is a SQLite database, which makes this an import by query
rather than by parser. Three rules shape everything in this module:

* **The user's catalog is never opened.** Lightroom holds a lock on the file
  while it runs, and a partially written journal is easy to create and
  impossible to apologise for. Every read here happens against a throwaway
  copy of the catalog and its `-wal` journal, opened `mode=ro` and deleted
  when the import ends. The original is not touched, even by SQLite.
* **Columns are checked, never assumed.** The schema drifts between Lightroom
  versions, and a catalog written by a release this build has never seen must
  still import what it can. Every table and column goes through `_has()`
  first, so drift becomes a recorded warning and a skipped category instead of
  a traceback halfway through a long job.
* **Nothing is created on the user's behalf.** The importer matches files a
  scan has already added to the catalog. A root folder that lies outside every
  registered source is counted and reported so the caller can offer to add it;
  this module never adds a source itself, and never writes beside the photos.

What is skipped is named rather than approximated, in the voice
`preset_io` uses for preset conversion: the `.lrcat-data` folder holding AI
masks and imported LUT payloads, local masks, camera profiles, and any
smart-collection rule the catalog cannot express.
"""

from __future__ import annotations

import contextlib
import uuid
import json
import re
import shutil
import sqlite3
import tempfile
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Callable, Iterator

import catalog as catalog_module

try:  # another module owns per-image XMP; the importer works without it
    import xmp_sidecar
except ImportError:  # pragma: no cover - exercised only without the module
    xmp_sidecar = None  # type: ignore[assignment]


class UnsupportedCatalog(Exception):
    """The file is not a Lightroom catalog, or cannot be opened at all."""


DEFAULT_OPTIONS: dict[str, Any] = {
    "metadata": True,
    "keywords": True,
    "collections": True,
    "stacks": True,
    "develop": True,
    # History is the one category that is off by default: it multiplies the
    # row count by up to two hundred and most people want the edit, not the
    # path it took.
    "history": False,
    "conflict": "skip",
    # Imported develop settings turn the Film pipeline off, exactly as an
    # imported preset does, because they were authored against Adobe's
    # rendering and not against a film stock.
    "filmOff": True,
    "trial": False,
    "referenceRoot": "",
}

CONFLICT_POLICIES = ("skip", "overwrite", "merge")

LABEL_NAMES = {"red", "yellow", "green", "blue", "purple"}

# A straighten angle outside the app's range cannot be applied, so it is
# reported rather than clipped into a different photograph.
MAX_ROTATE = 15.0
MAX_HISTORY_STEPS = 200
MAX_SETTINGS_BYTES = 400_000
MAX_XMP_BYTES = 1_000_000
KEYWORD_DEPTH = 8

_CROP_KEYS = (
    "HasCrop", "CropTop", "CropLeft", "CropBottom", "CropRight", "CropAngle",
    "CropConstrainToWarp", "CropConstrainAspectRatio",
)

_FALSE_TEXT = {"", "false", "0", "no", "off", "none"}


# ------------------------------------------------------------------ plumbing

def _has(conn: sqlite3.Connection, table: str, *columns: str) -> bool:
    """True when `table` exists and carries every named column.

    This is the single guard against schema drift. Every statement in this
    module is preceded by one of these calls, so a Lightroom release that
    renames or drops a column costs a category rather than the whole import.
    """
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.DatabaseError:
        return False
    if not rows:
        return False
    present = {str(row[1]).casefold() for row in rows}
    return all(str(column).casefold() in present for column in columns)


def _count(conn: sqlite3.Connection, table: str, *columns: str) -> int:
    if not _has(conn, table, *columns):
        return 0
    try:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0])
    except sqlite3.DatabaseError:
        return 0


def _number(value, default: float | None = None) -> float | None:
    try:
        return float(str(value).strip().lstrip("+"))
    except (TypeError, ValueError):
        return default


def _truthy(value) -> bool:
    return str(value or "").strip().casefold() not in _FALSE_TEXT


def _norm(path_text) -> str:
    """One spelling for a path: forward slashes, no trailing separator.

    Lightroom stores roots with a trailing slash and Windows roots with
    backslashes, so every comparison in this module runs through here.
    """
    text = str(path_text or "").replace("\\", "/").strip()
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _local_identity_path(value) -> str:
    text = _norm(value)
    path = Path(text)
    # Resolve local aliases without interpreting a foreign platform's drive.
    return _norm(path.resolve()) if path.is_absolute() else text


def _remap(path: str, root_map) -> str:
    """Re-point a root that has moved since the catalog was written."""
    if not root_map:
        return path
    table = {_norm(old).casefold(): _norm(new)
             for old, new in dict(root_map).items() if old and new}
    lowered = path.casefold()
    for old in sorted(table, key=len, reverse=True):
        if lowered == old:
            return table[old]
        if lowered.startswith(old + "/"):
            return table[old] + path[len(old):]
    return path


def _uri(path: Path) -> str:
    text = str(path).replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text
    return "file:" + urllib.parse.quote(text) + "?mode=ro"


@contextlib.contextmanager
def _open_readonly(
        path: Path) -> Iterator[tuple[sqlite3.Connection, list[str]]]:
    """Open a private copy of the catalog read-only, then throw the copy away.

    The copy is what makes this safe to run while Lightroom is open. The
    write-ahead log has to come with it or the newest edits are invisible; if
    the log needs replaying before it can be read, that happens against the
    copy and is reported, never against the user's file.
    """
    path = Path(path).expanduser()
    if not path.is_file():
        raise UnsupportedCatalog(f"no catalog file at {path}")
    warnings: list[str] = []
    temporary = Path(tempfile.mkdtemp(prefix="lighttable-lrcat-"))
    try:
        # A fixed copy name keeps the SQLite URI free of whatever punctuation
        # the user's catalog name happens to carry.
        copy = temporary / "catalog.lrcat"
        try:
            shutil.copy2(path, copy)
            for suffix in ("-wal", "-shm", "-journal"):
                sibling = path.with_name(path.name + suffix)
                if sibling.is_file():
                    shutil.copy2(sibling, copy.with_name(copy.name + suffix))
        except OSError as error:
            raise UnsupportedCatalog(f"could not copy the catalog: {error}")

        try:
            conn = sqlite3.connect(_uri(copy), uri=True)
        except sqlite3.OperationalError:
            _checkpoint(copy)
            warnings.append("the catalog's write-ahead log was replayed into "
                            "the working copy before reading")
            try:
                conn = sqlite3.connect(_uri(copy), uri=True)
            except sqlite3.Error as error:
                raise UnsupportedCatalog(
                    f"could not open the catalog: {error}")
        conn.row_factory = sqlite3.Row
        try:
            if not _has(conn, "Adobe_images", "id_local"):
                raise UnsupportedCatalog(
                    f"{path.name} is not a Lightroom catalog this build reads")
            yield conn, warnings
        finally:
            conn.close()
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _checkpoint(copy: Path) -> None:
    """Fold a copied write-ahead log into the copied database file."""
    try:
        writable = sqlite3.connect(copy)
        try:
            writable.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            writable.close()
    except sqlite3.Error:
        pass


def _warn(result: dict, text: str) -> None:
    if text not in result["warnings"]:
        result["warnings"].append(text)


def _skip(result: dict, category: str, count: int = 1) -> None:
    result["skipped"][category] = result["skipped"].get(category, 0) + count


def _report(progress, stage: str, done: int, total: int) -> None:
    if progress:
        progress({"stage": stage, "done": int(done), "total": int(total)})


# -------------------------------------------------------------- description

def _version(conn: sqlite3.Connection) -> str:
    if _has(conn, "Adobe_variablesTable", "name", "value"):
        try:
            rows = conn.execute('SELECT "name", "value"'
                                ' FROM "Adobe_variablesTable"').fetchall()
        except sqlite3.DatabaseError:
            rows = []
        values = {str(row["name"]): str(row["value"]) for row in rows}
        for key in ("Adobe_DBVersion", "Adobe_dbVersion", "AgLibraryVersion",
                    "Adobe_schemaVersion"):
            if values.get(key):
                return values[key]
        for name, value in values.items():
            if "version" in name.casefold() and value:
                return value
    return "unknown"


def _root_rows(conn: sqlite3.Connection, root_map=None) -> list[dict]:
    """Every catalogued root folder, with the number of files beneath it."""
    if not _has(conn, "AgLibraryRootFolder", "id_local", "absolutePath"):
        return []
    counts: dict[Any, int] = {}
    if _has(conn, "AgLibraryFolder", "id_local", "rootFolder") \
            and _has(conn, "AgLibraryFile", "id_local", "folder"):
        for row in conn.execute(
                'SELECT fo."rootFolder" AS root, COUNT(fi."id_local") AS n'
                ' FROM "AgLibraryFolder" fo'
                ' LEFT JOIN "AgLibraryFile" fi ON fi."folder" = fo."id_local"'
                ' GROUP BY fo."rootFolder"').fetchall():
            counts[row["root"]] = int(row["n"] or 0)
    out = []
    for row in conn.execute(
            'SELECT "id_local", "absolutePath" FROM "AgLibraryRootFolder"'
            ' ORDER BY "absolutePath"').fetchall():
        original = _norm(row["absolutePath"])
        mapped = _remap(original, root_map)
        out.append({
            "id": int(row["id_local"]),
            "path": mapped,
            "originalPath": original,
            "exists": bool(mapped) and Path(mapped).is_dir(),
            "files": counts.get(row["id_local"], 0),
        })
    return out


def _data_folder(path: Path) -> Path | None:
    """The `.lrcat-data` sidecar folder, when the catalog has one."""
    for candidate in (path.with_name(path.name + "-data"),
                      path.with_suffix(".lrcat-data")):
        if candidate.is_dir():
            return candidate
    return None


def inspect(path: Path | str) -> dict:
    """Open a catalog read-only and summarise it without importing anything.

    This is what the first step of the import dialog shows: how much is in
    there, which roots are reachable from this computer, and what will be
    skipped before any of it is written.
    """
    path = Path(path).expanduser()
    result: dict[str, Any] = {"warnings": []}
    with _open_readonly(path) as (conn, warnings):
        result["warnings"].extend(warnings)
        keywords = 0
        if _has(conn, "AgLibraryKeyword", "id_local", "name"):
            keywords = int(conn.execute(
                'SELECT COUNT(*) FROM "AgLibraryKeyword"'
                " WHERE \"name\" IS NOT NULL AND TRIM(\"name\") != ''"
            ).fetchone()[0])
        summary = {
            "version": _version(conn),
            "images": _count(conn, "Adobe_images", "id_local"),
            "roots": _root_rows(conn),
            "keywords": keywords,
            "collections": _count(conn, "AgLibraryCollection", "id_local"),
            "stacks": _count(conn, "AgLibraryFolderStack", "id_local"),
            "hasDevelopSettings": _count(
                conn, "Adobe_imageDevelopSettings", "image", "text") > 0,
            "hasHistory": _count(
                conn, "Adobe_libraryImageDevelopHistoryStep", "image",
                "name") > 0,
        }
        for table, category in (("AgLibraryKeyword", "keywords"),
                                ("AgLibraryCollection", "collections"),
                                ("AgLibraryFolderStack", "stacks")):
            if not _has(conn, table, "id_local"):
                _warn(result, f"this catalog has no {category} table, so "
                              f"{category} are skipped")
    summary["warnings"] = result["warnings"]
    if _data_folder(path):
        summary["warnings"].append(
            "masks and AI data live beside the catalog and are skipped")
    for root in summary["roots"]:
        if not root["exists"]:
            summary["warnings"].append(
                f"the root folder {root['path']} is not on this computer; "
                "its photos can be re-pointed or skipped")
    return summary


# ----------------------------------------------------------- develop settings

def _map_settings(xmp: str, attrs: dict, curves: dict,
                  cropped: bool) -> tuple[dict, list[str]]:
    """Hand crs settings to the same converter the preset importer uses.

    `preset_io.map_crs_settings` is the shared entry point; the older
    `import_lightroom` path is kept as a fallback so this module works against
    either shape of that file.
    """
    import preset_io

    mapper = getattr(preset_io, "map_crs_settings", None)
    if callable(mapper):
        converted = mapper(attrs, curves)
        return (dict(converted.get("grade") or {}),
                list(converted.get("ignored") or []))
    preset = preset_io.import_lightroom(xmp, "develop.xmp")
    ignored = list((preset.get("conversion") or {}).get("ignored") or [])
    if cropped and "geometry or crop" in ignored:
        # The crop is applied below, so it is not a skipped operation.
        ignored.remove("geometry or crop")
    return dict(preset.get("grade") or {}), ignored


def _crop_rect(attrs: dict) -> dict | None:
    """Camera Raw crop fractions as the catalog's normalised `{x,y,w,h}`."""
    if "HasCrop" in attrs and not _truthy(attrs["HasCrop"]):
        return None
    left = _number(attrs.get("CropLeft"), 0.0) or 0.0
    top = _number(attrs.get("CropTop"), 0.0) or 0.0
    right = _number(attrs.get("CropRight"), 1.0)
    bottom = _number(attrs.get("CropBottom"), 1.0)
    right = 1.0 if right is None else right
    bottom = 1.0 if bottom is None else bottom
    x = max(0.0, min(1.0, left))
    y = max(0.0, min(1.0, top))
    width = max(0.0, min(1.0 - x, right - x))
    height = max(0.0, min(1.0 - y, bottom - y))
    if width < 0.01 or height < 0.01:
        return None
    rect = {"x": round(x, 5), "y": round(y, 5),
            "w": round(width, 5), "h": round(height, 5)}
    if rect == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}:
        return None
    return rect


def _develop_from_lua(text) -> dict | None:
    """Convert one Lua develop table into grade, crop, and optics.

    Lightroom stores per-image settings in the same declarative Lua table a
    `.lrtemplate` preset uses, so the conversion goes through `preset_io`
    rather than growing a second, divergent mapping here.
    """
    import preset_io

    body = str(text or "")
    if not body.strip() or len(body) > MAX_SETTINGS_BYTES:
        return None
    try:
        xmp = preset_io._legacy_template_as_xmp(body, "develop.lrtemplate")
        attrs = preset_io._xmp_attrs(xmp)
        curves = {key: preset_io._xmp_curve(xmp, key)
                  for key in preset_io.CRS_CURVE_KEYS}
    except (AttributeError, ValueError, TypeError):
        return None
    curves = {key: value for key, value in curves.items() if value}
    if not attrs and not curves:
        return None

    crop = _crop_rect(attrs)
    angle = _number(attrs.get("CropAngle"))
    settings = {key: value for key, value in attrs.items()
                if key not in _CROP_KEYS}
    try:
        grade, ignored = _map_settings(xmp, settings, curves, crop is not None)
    except (ValueError, TypeError):
        return None

    optics: dict[str, float] = {}
    if angle is not None and abs(angle) > 1e-6:
        if abs(angle) > MAX_ROTATE:
            ignored.append("crop angle beyond supported range")
        else:
            optics["rotate"] = round(float(angle), 4)
    return {"grade": grade, "crop": crop, "optics": optics,
            "cropKeys": any(key in attrs for key in _CROP_KEYS),
            "ignored": sorted(set(ignored))}


# --------------------------------------------------------- smart collections

_CRITERIA_SPLIT = re.compile(r'criteria\s*=\s*"([^"]+)"')
_OPERATION = re.compile(r'operation\s*=\s*"([^"]+)"')
_VALUE_TEXT = re.compile(
    r'(?<![A-Za-z0-9_])value\s*=\s*"((?:\\.|[^"\\])*)"')
_VALUE_NUMBER = re.compile(
    r'(?<![A-Za-z0-9_])value\s*=\s*([-+]?[0-9]+(?:\.[0-9]*)?)')

_PICK_STATUS = {1: "approved", -1: "skipped", 0: "pending"}
_TEXT_CRITERIA = {"all", "any", "anysearchable", "searchable", "filename",
                  "keywords", "caption", "title", "text", "allsearchable"}


def _smart_rules(content) -> tuple[dict, list[str]]:
    """Recreate the smart-collection rules the catalog can express.

    Returns the rules plus the names of the criteria that were dropped. Only
    rating, flag, colour label, and free text have an equivalent here; a rule
    the catalog cannot express is named rather than approximated, because a
    smart collection that silently means something else is worse than one that
    is honestly empty.
    """
    body = str(content or "")
    if not body.strip():
        return {}, []
    rules: dict[str, Any] = {}
    dropped: list[str] = []
    parts = _CRITERIA_SPLIT.split(body)
    for index in range(1, len(parts) - 1, 2):
        criteria = parts[index].strip()
        block = parts[index + 1][:2000]
        lowered = criteria.casefold()
        found = _OPERATION.search(block)
        operation = found.group(1) if found else ""
        text_value = _VALUE_TEXT.search(block)
        number_value = _VALUE_NUMBER.search(block)
        number = _number(number_value.group(1)) if number_value else None
        text = text_value.group(1) if text_value else ""

        if lowered == "rating" and number is not None:
            if operation in ("", ">=", ">", "==", "=", "is", "isGreaterThan",
                             "isGreaterThanOrEqualTo"):
                step = 1 if operation in (">", "isGreaterThan") else 0
                rules["ratingMin"] = max(0, min(5, int(number) + step))
            else:
                dropped.append(criteria)
        elif lowered in ("pick", "flag", "pickstatus") and number is not None:
            status = _PICK_STATUS.get(int(number))
            if status:
                rules["status"] = status
            else:
                dropped.append(criteria)
        elif lowered in ("labelcolor", "colorlabel", "label", "labeltext"):
            name = (text or "").strip().casefold()
            if name in LABEL_NAMES:
                rules["label"] = name
            else:
                dropped.append(criteria)
        elif lowered in _TEXT_CRITERIA and text:
            rules["query"] = text[:200]
        else:
            dropped.append(criteria)
    return rules, sorted(set(dropped))


# ------------------------------------------------------------------ matching

def _options(options) -> dict:
    given = dict(options or {})
    out = dict(DEFAULT_OPTIONS)
    for key in ("metadata", "keywords", "collections", "stacks", "develop",
                "history", "filmOff", "trial"):
        if key in given:
            out[key] = bool(given[key])
    conflict = str(given.get("conflict", out["conflict"]))
    out["conflict"] = conflict if conflict in CONFLICT_POLICIES else "skip"
    out["referenceRoot"] = str(given.get("referenceRoot", "")).strip()
    return out


def _catalog_index(cat) -> dict[tuple[int, str], list[tuple[int, str]]]:
    """Preserve every candidate when distinct paths collide after case-folding."""
    index: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for row in cat.connection.execute(
            "SELECT i.id AS image_id, f.source_id, f.relpath FROM images i"
            " JOIN files f ON f.id=i.file_id"
            " WHERE i.copy_ident IS NULL").fetchall():
        relpath = row["relpath"]
        index.setdefault((int(row["source_id"]), relpath.casefold()), []).append((
            int(row["image_id"]), relpath))
    return index


def _match_files(conn, cat, root_map, result, progress) -> dict[int, dict]:
    """Resolve every catalogued file to a source, a relative path, an image.

    Matching is by absolute path, which is the only identity the two catalogs
    share. A file the scan has not seen is counted rather than inserted: the
    catalog's own scanner owns file rows, hashes them, and reads their
    metadata, and an importer that guessed at those columns would produce rows
    a rescan then had to repair.
    """
    roots = _root_rows(conn, root_map)
    tallies = {root["id"]: {"matched": 0, "unmatched": 0} for root in roots}
    result["roots"] = roots
    if not roots:
        _warn(result, "this catalog has no root folder table, so no files "
                      "could be matched")
        _skip(result, "files")
        return {}
    if not _has(conn, "AgLibraryFolder", "id_local", "rootFolder",
                "pathFromRoot") \
            or not _has(conn, "AgLibraryFile", "id_local", "folder",
                        "baseName"):
        _warn(result, "this catalog does not carry the folder and file tables "
                      "the importer reads, so no files could be matched")
        _skip(result, "files")
        return {}

    by_root = {root["id"]: root for root in roots}
    folders: dict[Any, tuple[Any, str]] = {}
    for row in conn.execute(
            'SELECT "id_local", "rootFolder", "pathFromRoot"'
            ' FROM "AgLibraryFolder"').fetchall():
        folders[row["id_local"]] = (row["rootFolder"],
                                    _norm(row["pathFromRoot"]).strip("/"))

    sources = sorted(
        ((_local_identity_path(source["path"]).casefold(), _local_identity_path(source["path"]),
          int(source["id"])) for source in cat.sources()),
        key=lambda item: len(item[0]), reverse=True)
    index = _catalog_index(cat)

    has_idx = _has(conn, "AgLibraryFile", "idx_filename")
    has_ext = _has(conn, "AgLibraryFile", "extension")
    columns = ['"id_local"', '"folder"', '"baseName"']
    if has_ext:
        columns.append('"extension"')
    if has_idx:
        columns.append('"idx_filename"')
    rows = conn.execute(
        f'SELECT {", ".join(columns)} FROM "AgLibraryFile"').fetchall()

    files: dict[int, dict] = {}
    total = len(rows)
    for done, row in enumerate(rows, 1):
        folder = folders.get(row["folder"])
        if not folder:
            continue
        root = by_root.get(folder[0])
        if not root:
            continue
        filename = str(row["idx_filename"] or "") if has_idx else ""
        if not filename:
            extension = str(row["extension"] or "") if has_ext else ""
            filename = f'{row["baseName"]}.{extension}' if extension \
                else str(row["baseName"] or "")
        if not filename:
            continue
        absolute = "/".join(part for part in (root["path"], folder[1],
                                              filename) if part)
        entry = {"path": absolute, "root": root["id"], "sourceId": None,
                 "relpath": "", "imageId": None}
        match_path = "/".join(part for part in (_local_identity_path(root["path"]), folder[1], filename) if part)
        lowered = match_path.casefold()
        for folded_source, source_path, source_id in sources:
            if lowered.startswith(folded_source + "/"):
                relpath = match_path[len(source_path) + 1:]
                candidates = index.get((source_id, relpath.casefold()), [])
                exact = [candidate for candidate in candidates if candidate[1] == relpath]
                found = exact[0] if len(exact) == 1 else candidates[0] if len(candidates) == 1 else None
                if len(candidates) > 1 and not exact:
                    entry["matchWarning"] = "Ambiguous case-insensitive path; no photo was chosen"
                entry["sourceId"] = source_id
                entry["relpath"] = found[1] if found else relpath
                entry["imageId"] = found[0] if found else None
                break
        files[int(row["id_local"])] = entry
        tally = tallies.get(root["id"])
        if tally is not None:
            tally["matched" if entry["imageId"] else "unmatched"] += 1
        if done % 2000 == 0:
            _report(progress, "files", done, total)
    _report(progress, "files", total, total)

    for root in roots:
        tally = tallies.get(root["id"], {"matched": 0, "unmatched": 0})
        root.update(tally)
        folded_root = _local_identity_path(root["path"]).casefold()
        root["sourceId"] = next(
            (source_id for folded, _path, source_id in sources
             if folded_root == folded or folded_root.startswith(folded + "/")),
            None)
        if tally["unmatched"] and root["sourceId"] is None:
            _warn(result, f"{root['path']} is not one of this catalog's "
                          "sources, so its photos were not imported")
    return files


# -------------------------------------------------------------- catalog side

def _load(blob) -> dict:
    try:
        value = json.loads(blob)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _existing_state(
        cat: catalog_module.Catalog) -> tuple[set[int], dict, dict, dict]:
    """What the catalog already holds, read once instead of per photo.

    The conflict policy needs to know which images carry edits, and merging
    `profile_enabled` or a straighten angle needs the blobs they go into.
    """
    edited: set[int] = set()
    params: dict[int, dict] = {}
    optics: dict[int, dict] = {}
    keywords: dict[int, list[str]] = {}
    columns = ("params_json", "grade_json", "crop_json", "masks_json",
               "heals_json", "optics_json")
    for row in cat.connection.execute(
            "SELECT image_id, params_json, grade_json, crop_json, masks_json,"
            " heals_json, optics_json FROM image_state").fetchall():
        image_id = int(row["image_id"])
        if any(row[column] for column in columns):
            edited.add(image_id)
        if row["params_json"]:
            params[image_id] = _load(row["params_json"])
        if row["optics_json"]:
            optics[image_id] = _load(row["optics_json"])
    for row in cat.connection.execute(
            "SELECT ik.image_id, k.path FROM image_keywords ik"
            " JOIN keywords k ON k.id=ik.keyword_id").fetchall():
        keywords.setdefault(int(row["image_id"]), []).append(row["path"])
    return edited, params, optics, keywords


def _keyword_paths(conn, result) -> dict[Any, list[str]]:
    """`Parent > Child` paths per catalogued image."""
    if not _has(conn, "AgLibraryKeyword", "id_local", "name") \
            or not _has(conn, "AgLibraryKeywordImage", "image", "tag"):
        _skip(result, "keywords")
        _warn(result, "this catalog does not carry the keyword tables the "
                      "importer reads, so keywords were skipped")
        return {}
    has_parent = _has(conn, "AgLibraryKeyword", "parent")
    columns = '"id_local", "name"' + (', "parent"' if has_parent else "")
    names: dict[Any, str] = {}
    parents: dict[Any, Any] = {}
    for row in conn.execute(
            f'SELECT {columns} FROM "AgLibraryKeyword"').fetchall():
        # A name carrying the path separator would invent a hierarchy the
        # catalog never had, so it is flattened instead.
        name = " ".join(str(row["name"] or "").replace(">", "-")
                        .replace("|", "-").split()).strip()
        names[row["id_local"]] = name[:60]
        parents[row["id_local"]] = row["parent"] if has_parent else None

    cache: dict[Any, str] = {}

    def path_for(key) -> str:
        if key in cache:
            return cache[key]
        parts: list[str] = []
        seen: set[Any] = set()
        cursor = key
        while cursor is not None and cursor not in seen \
                and len(parts) < KEYWORD_DEPTH:
            seen.add(cursor)
            name = names.get(cursor)
            if name is None:
                break
            if name:
                parts.append(name)
            cursor = parents.get(cursor)
        cache[key] = " > ".join(reversed(parts))
        return cache[key]

    per_image: dict[Any, list[str]] = {}
    for row in conn.execute(
            'SELECT "image", "tag" FROM "AgLibraryKeywordImage"').fetchall():
        path = path_for(row["tag"])
        if path:
            per_image.setdefault(row["image"], []).append(path)
    return per_image


def _label_for(value, result) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() in ("none", "0"):
        return "none"
    lowered = text.casefold()
    if lowered in LABEL_NAMES:
        return lowered
    _skip(result, "custom colour labels")
    _warn(result, f'the colour label "{text}" has no equivalent here, so '
                  "those photos arrived unlabelled")
    return "none"


def _capture_time(value) -> str | None:
    text = str(value or "").strip().replace(" ", "T")[:19]
    return text if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


def _iptc_from_xmp(blob) -> dict:
    if xmp_sidecar is None or not blob:
        return {}
    if isinstance(blob, (bytes, bytearray, memoryview)):
        raw = bytes(blob)
        if len(raw) > MAX_XMP_BYTES:
            return {}
        # Lightroom Classic stores Adobe_AdditionalMetadata.xmp as either
        # plain XML or a four-byte big-endian size followed by zlib data.
        # The synthetic fixture used to cover only the plain form, which hid
        # the title/creator/location fields in catalogs written by Lightroom.
        if len(raw) > 6:
            expected = int.from_bytes(raw[:4], "big")
            if 0 < expected <= MAX_XMP_BYTES:
                try:
                    inflater = zlib.decompressobj()
                    unpacked = inflater.decompress(
                        raw[4:], MAX_XMP_BYTES + 1)
                    if inflater.eof and len(unpacked) == expected:
                        raw = unpacked
                except zlib.error:
                    pass
        text = raw.decode("utf-8", "replace")
    else:
        text = str(blob)
    if len(text) > MAX_XMP_BYTES:
        return {}
    try:
        parsed = xmp_sidecar.parse(text)
    except Exception:
        return {}
    if not parsed:
        return {}
    fields = {}
    for key in ("title", "caption", "creator", "copyright", "credit",
                "headline", "city", "state", "country"):
        value = parsed.get(key)
        if value:
            fields[key] = value
    gps = parsed.get("gps")
    if gps:
        fields["gps_lat"] = gps.get("lat")
        fields["gps_lon"] = gps.get("lon")
        if gps.get("alt") is not None:
            fields["gps_alt"] = gps["alt"]
    return fields


def _photo_report(result, row):
    if len(result["photos"]) < 100:
        result["photos"].append(row)
    result["reportedPhotos"] += 1
    if result.get("_report_photo"):
        result["_report_photo"](row)


def _rendered_reference(cat, record, root):
    if not root or not record.get("relpath"):
        return None
    base = Path(root).expanduser().resolve()
    relative = Path(record["relpath"])
    candidates = [base / relative.with_suffix(ext) for ext in (".tif", ".tiff", ".jpg", ".jpeg")]
    matches = [path for path in candidates if path.is_file() and base in path.resolve().parents
               and path.resolve() != Path(record["path"]).resolve()]
    if len(matches) != 1:
        return {"note": "Multiple rendered references match; choose one explicitly"} if matches else None
    path = matches[0].resolve()
    reference = {"path": str(path), "note": "Add this reference folder to the library to group its photos"}
    for source in cat.sources():
        try:
            relpath = path.relative_to(Path(source["path"]).resolve()).as_posix()
        except ValueError:
            continue
        image_id = cat.image_id_for(source["id"], relpath)
        if image_id is not None:
            reference = {"path": str(path), "imageId": image_id,
                         "name": catalog_module.qualified_name(source["id"], relpath)}
            break
    return reference


def _import_images(conn, cat, opts, files, keyword_paths, result, progress,
                   cancel) -> dict[Any, int]:
    """Ratings, flags, labels, keywords, metadata, and edits, photo by photo.

    Collection and stack membership is deliberately applied even to images the
    conflict policy leaves alone: a half-populated collection is a worse lie
    than an unchanged photograph, and membership is organisation rather than
    an edit.
    """
    targets: dict[Any, int] = {}
    if not _has(conn, "Adobe_images", "id_local", "rootFile"):
        _warn(result, "this catalog has no image table the importer reads")
        _skip(result, "images")
        return targets

    present = {name for name in ("rating", "pick", "colorLabels",
                                 "captureTime", "fileFormat", "masterImage",
                                 "copyName")
               if _has(conn, "Adobe_images", name)}
    columns = ", ".join(f'"{name}"'
                        for name in ["id_local", "rootFile", *sorted(present)])
    order = ' ORDER BY ("masterImage" IS NULL) DESC, "id_local"' \
        if "masterImage" in present else ' ORDER BY "id_local"'
    rows = conn.execute(f'SELECT {columns} FROM "Adobe_images"{order}'
                        ).fetchall()
    result["images"] = len(rows)
    if opts["trial"]:
        rows = rows[:20]
        result["trial"] = True
        result["trialPhotos"] = len(rows)
    trial_key = uuid.uuid4().hex[:12]

    iptc_rows: dict[Any, dict] = {}
    if opts["metadata"] and _has(conn, "AgLibraryIPTC", "image"):
        picks = [name for name in ("caption", "copyright")
                 if _has(conn, "AgLibraryIPTC", name)]
        if picks:
            select = ", ".join(f'"{name}"' for name in ["image", *picks])
            for row in conn.execute(
                    f'SELECT {select} FROM "AgLibraryIPTC"').fetchall():
                iptc_rows[row["image"]] = {
                    name: row[name] for name in picks if row[name]}

    gps_rows: dict[Any, tuple] = {}
    if opts["metadata"] and _has(conn, "AgHarvestedExifMetadata", "image",
                                 "gpsLatitude", "gpsLongitude"):
        for row in conn.execute(
                'SELECT "image", "gpsLatitude", "gpsLongitude"'
                ' FROM "AgHarvestedExifMetadata"').fetchall():
            if row["gpsLatitude"] is None or row["gpsLongitude"] is None:
                continue
            gps_rows[row["image"]] = (row["gpsLatitude"], row["gpsLongitude"])

    xmp_sql = None
    if opts["metadata"] and xmp_sidecar is not None \
            and _has(conn, "Adobe_AdditionalMetadata", "image", "xmp"):
        xmp_sql = ('SELECT "xmp" FROM "Adobe_AdditionalMetadata"'
                   ' WHERE "image"=?')

    develop_sql = None
    has_cropped_size = False
    if opts["develop"]:
        if _has(conn, "Adobe_imageDevelopSettings", "image", "text"):
            picks = ['"text"']
            if _has(conn, "Adobe_imageDevelopSettings", "croppedWidth",
                    "croppedHeight"):
                picks += ['"croppedWidth"', '"croppedHeight"']
                has_cropped_size = True
            develop_sql = (f'SELECT {", ".join(picks)} FROM'
                           ' "Adobe_imageDevelopSettings" WHERE "image"=?')
        else:
            _skip(result, "develop settings")
            _warn(result, "this catalog does not carry develop settings the "
                          "importer reads, so edits were skipped")

    history_sql = None
    history_has_text = False
    if opts["history"]:
        table = "Adobe_libraryImageDevelopHistoryStep"
        if _has(conn, table, "image", "name"):
            picks = ['"name"']
            history_has_text = _has(conn, table, "text")
            if history_has_text:
                picks.append('"text"')
            order_by = '"dateCreated"' if _has(conn, table, "dateCreated") \
                else '"id_local"'
            history_sql = (f'SELECT {", ".join(picks)} FROM "{table}"'
                           f' WHERE "image"=? ORDER BY {order_by} ASC')
        else:
            _skip(result, "history")
            _warn(result, "this catalog does not carry the develop history "
                          "table, so history was skipped")

    edited, params_by_image, optics_by_image, keywords_by_image = \
        _existing_state(cat)
    conflict = opts["conflict"]
    applied_keywords: set[str] = set()
    capture_updates: list[tuple[str, int]] = []
    total = len(rows)

    for done, row in enumerate(rows, 1):
        if cancel():
            result["cancelled"] = True
            _warn(result, "the import was stopped before it finished")
            break
        lr_id = row["id_local"]
        record = files.get(row["rootFile"])
        coverage = {"sourceId": lr_id, "sourcePath": record["path"] if record else "",
                    "targetName": None, "outcome": "unmatched", "mapped": [], "skipped": []}
        if record and record.get("matchWarning"):
            coverage["skipped"].append(record["matchWarning"])
        if record is None or record["imageId"] is None:
            result["unmatched"] += 1
            _photo_report(result, coverage)
            _skip(result, "files outside every source"
                  if record is None or record["sourceId"] is None
                  else "files not yet scanned")
            continue

        master = row["masterImage"] if "masterImage" in present else None
        if opts["trial"]:
            ident = f"trial-{trial_key}-{lr_id}"
            image_id = cat.add_virtual_copy(record["imageId"], ident, f"Import trial {lr_id}")
        elif master:
            parent = targets.get(master)
            if parent is None:
                result["unmatched"] += 1
                _skip(result, "virtual copies without a master")
                coverage["skipped"].append("Virtual copy has no matched master")
                _photo_report(result, coverage)
                continue
            ident = f"lr-{lr_id}"
            display = str(row["copyName"] or "").strip() \
                if "copyName" in present else ""
            image_id = cat.image_id_for(record["sourceId"], record["relpath"],
                                        ident)
            if image_id is None:
                image_id = cat.add_virtual_copy(
                    parent, ident, display or f"Copy {lr_id}")
        else:
            image_id = record["imageId"]
        targets[lr_id] = image_id
        result["matched"] += 1
        target = cat.image_row(image_id)
        coverage["targetName"] = catalog_module.qualified_name(record["sourceId"], record["relpath"], target["copy_ident"])
        coverage["outcome"] = "trial variant" if opts["trial"] else "imported"

        if image_id in edited and conflict == "skip":
            _skip(result, "images already edited")
            coverage["outcome"] = "kept existing edits"
            _photo_report(result, coverage)
            if done % 50 == 0:
                _report(progress, "images", done, total)
            continue
        keep_edits = image_id in edited and conflict == "merge"
        # Lightroom keeps basic adjustments for video; nothing here renders
        # them, so they are named as skipped rather than half-applied.
        video = "fileFormat" in present and str(
            row["fileFormat"] or "").strip().casefold() == "video"

        entry: dict[str, Any] = {}
        if opts["metadata"]:
            if "rating" in present:
                entry["rating"] = max(0, min(
                    5, int(_number(row["rating"], 0) or 0)))
            if "pick" in present:
                entry["status"] = _PICK_STATUS.get(
                    int(_number(row["pick"], 0) or 0), "pending")
            if "colorLabels" in present:
                entry["label"] = _label_for(row["colorLabels"], result)
        if opts["keywords"]:
            imported = keyword_paths.get(lr_id) or []
            if imported:
                entry["keywords"] = list(dict.fromkeys(
                    [*keywords_by_image.get(image_id, []), *imported]))
                applied_keywords.update(imported)

        if develop_sql and not keep_edits:
            settings = conn.execute(develop_sql, (lr_id,)).fetchone()
            if settings is not None and video:
                if str(settings["text"] or "").strip():
                    _skip(result, "video edits")
            elif settings is not None:
                patch = _develop_from_lua(settings["text"])
                if patch is None:
                    if str(settings["text"] or "").strip():
                        _skip(result, "develop settings")
                else:
                    if patch["grade"]:
                        entry["grade"] = patch["grade"]
                    if patch["crop"]:
                        entry["crop"] = patch["crop"]
                    if patch["optics"]:
                        entry["optics"] = dict(
                            optics_by_image.get(image_id) or {},
                            **patch["optics"])
                    if opts["filmOff"]:
                        entry["params"] = dict(
                            params_by_image.get(image_id) or {},
                            profile_enabled=False)
                    coverage["skipped"].extend(patch["ignored"])
                    for name in patch["ignored"]:
                        _skip(result, name)
                    if has_cropped_size and patch["crop"] is None \
                            and not patch["cropKeys"] \
                            and settings["croppedWidth"]:
                        _skip(result, "crop rectangle")
        coverage["mapped"] = sorted(entry)
        coverage["appearance"] = "Approximate conversion; compare with an exported reference"
        if entry:
            cat.save_state(image_id, entry)
        reference = _rendered_reference(cat, record, opts.get("referenceRoot"))
        if reference:
            coverage["reference"] = reference
            if reference.get("imageId") and reference["imageId"] != image_id:
                occupied = cat.connection.execute("SELECT image_id FROM stack_images WHERE image_id IN (?,?)",
                                                  (image_id, reference["imageId"])).fetchall()
                if not occupied:
                    cat.add_stack("Original and rendered reference", [image_id, reference["imageId"]])
                    coverage["reference"]["grouped"] = True
                else:
                    coverage["reference"]["note"] = "Existing stacks were preserved"
        _photo_report(result, coverage)

        if opts["metadata"]:
            fields = {}
            if xmp_sql:
                blob = conn.execute(xmp_sql, (lr_id,)).fetchone()
                fields = _iptc_from_xmp(blob["xmp"] if blob else None)
            for key, value in (iptc_rows.get(lr_id) or {}).items():
                fields[key] = value
            coordinates = gps_rows.get(lr_id)
            if coordinates:
                fields["gps_lat"], fields["gps_lon"] = coordinates
            if fields:
                cat.save_iptc(image_id, fields)
            if "captureTime" in present and not opts["trial"]:
                stamp = _capture_time(row["captureTime"])
                if stamp:
                    capture_updates.append((stamp, image_id))

        if history_sql:
            steps = conn.execute(history_sql, (lr_id,)).fetchall()
            for step in steps[-MAX_HISTORY_STEPS:]:
                text = step["text"] if history_has_text else ""
                snapshot: dict[str, Any] = {"imported": True}
                patch = _develop_from_lua(text) if text else None
                if patch:
                    snapshot.update({"grade": patch["grade"],
                                     "crop": patch["crop"],
                                     "optics": patch["optics"]})
                elif text:
                    snapshot["note"] = str(text)[:400]
                cat.add_history(image_id, str(step["name"] or "Imported step"),
                                snapshot, origin="import")
                result["history"] += 1

        if done % 50 == 0:
            _report(progress, "images", done, total)

    if capture_updates:
        with cat.write() as writer:
            writer.executemany(
                "UPDATE files SET capture_time=?"
                " WHERE id=(SELECT file_id FROM images WHERE id=?)"
                "   AND (capture_time IS NULL OR capture_time='')",
                capture_updates)
    result["keywords"] = len(applied_keywords)
    _report(progress, "images", total, total)
    return targets


def _import_collections(conn, cat, targets, result, progress) -> None:
    """Collections, collection sets, and the smart rules that survive."""
    if not _has(conn, "AgLibraryCollection", "id_local", "name"):
        _skip(result, "collections")
        _warn(result, "this catalog has no collection table the importer "
                      "reads, so collections were skipped")
        return
    has_parent = _has(conn, "AgLibraryCollection", "parent")
    has_creation = _has(conn, "AgLibraryCollection", "creationId")
    has_content = _has(conn, "AgLibraryCollection", "content")
    picks = ['"id_local"', '"name"']
    if has_parent:
        picks.append('"parent"')
    if has_creation:
        picks.append('"creationId"')
    if has_content:
        picks.append('"content"')
    rows = {row["id_local"]: row for row in conn.execute(
        f'SELECT {", ".join(picks)} FROM "AgLibraryCollection"').fetchall()}

    contents: dict[Any, Any] = {}
    if not has_content and _has(conn, "AgLibraryCollectionContent",
                                "collection", "content"):
        for row in conn.execute(
                'SELECT "collection", "content"'
                ' FROM "AgLibraryCollectionContent"').fetchall():
            contents[row["collection"]] = row["content"]

    members: dict[Any, list] = {}
    if _has(conn, "AgLibraryCollectionImage", "collection", "image"):
        has_position = _has(conn, "AgLibraryCollectionImage",
                            "positionInCollection")
        select = 'SELECT "collection", "image" FROM "AgLibraryCollectionImage"'
        if has_position:
            select += ' ORDER BY "positionInCollection"'
        for row in conn.execute(select).fetchall():
            members.setdefault(row["collection"], []).append(row["image"])
    else:
        _skip(result, "collection membership")

    existing = {(item["parentId"], item["name"].casefold()): item["id"]
                for item in cat.collections()}
    created: dict[Any, int] = {}
    smart: set[Any] = set()

    def ensure(key, depth: int = 0) -> int | None:
        if key in created:
            return created[key]
        row = rows.get(key)
        if row is None or depth > 8:
            return None
        parent = ensure(row["parent"], depth + 1) \
            if has_parent and row["parent"] else None
        name = " ".join(str(row["name"] or "").split())[:120] or "Collection"
        creation = str(row["creationId"] or "").casefold() if has_creation \
            else ""
        kind = "smart" if "smart" in creation else "regular"
        seen = (parent, name.casefold())
        if seen in existing:
            created[key] = existing[seen]
            if kind == "smart":
                smart.add(key)
            return created[key]
        rules: dict = {}
        dropped: list[str] = []
        if kind == "smart":
            smart.add(key)
            rules, dropped = _smart_rules(
                row["content"] if has_content else contents.get(key))
        if kind == "smart" and not rules:
            # An empty rule set would match the whole library, which is a
            # different collection than the one being imported.
            collection_id = cat.add_collection(name, kind="regular",
                                               parent_id=parent)
            _skip(result, "smart collection rules")
            _warn(result, f'the smart collection "{name}" uses rules this '
                          "catalog cannot express, so it arrived empty")
        else:
            collection_id = cat.add_collection(
                name, kind=kind, rules=rules or None, parent_id=parent)
            if dropped:
                _skip(result, "smart collection rules", len(dropped))
                _warn(result, f'the smart collection "{name}" dropped the '
                              f'rules {", ".join(dropped)}')
        existing[seen] = collection_id
        created[key] = collection_id
        result["collections"] += 1
        return collection_id

    total = len(rows)
    for done, key in enumerate(rows, 1):
        collection_id = ensure(key)
        if collection_id is None or key in smart:
            continue
        ours = [targets[image] for image in members.get(key, [])
                if image in targets]
        ours = list(dict.fromkeys(ours))
        if ours:
            cat.add_to_collection(collection_id, ours)
        if done % 50 == 0:
            _report(progress, "collections", done, total)
    _report(progress, "collections", total, total)


def _import_stacks(conn, cat, targets, result, progress) -> None:
    """Folder stacks, for the members this catalog knows about."""
    if not _has(conn, "AgLibraryFolderStack", "id_local") \
            or not _has(conn, "AgLibraryFolderStackImage", "stack", "image"):
        _skip(result, "stacks")
        _warn(result, "this catalog does not carry the stack tables the "
                      "importer reads, so stacks were skipped")
        return
    has_position = _has(conn, "AgLibraryFolderStackImage", "position")
    select = 'SELECT "stack", "image" FROM "AgLibraryFolderStackImage"'
    if has_position:
        select += ' ORDER BY "position"'
    groups: dict[Any, list] = {}
    for row in conn.execute(select).fetchall():
        groups.setdefault(row["stack"], []).append(row["image"])

    has_text = _has(conn, "AgLibraryFolderStack", "text")
    has_collapsed = _has(conn, "AgLibraryFolderStack", "collapsed")
    picks = ['"id_local"']
    if has_text:
        picks.append('"text"')
    if has_collapsed:
        picks.append('"collapsed"')
    stacks = {row["id_local"]: row for row in conn.execute(
        f'SELECT {", ".join(picks)} FROM "AgLibraryFolderStack"').fetchall()}

    total = len(groups)
    with cat.write() as writer:
        occupied = {int(row["image_id"]) for row in writer.execute(
            "SELECT image_id FROM stack_images").fetchall()}
        for done, (key, images) in enumerate(groups.items(), 1):
            ours = [targets[image] for image in images if image in targets]
            ours = [image for image in dict.fromkeys(ours)
                    if image not in occupied]
            if len(ours) < 2:
                if ours:
                    _skip(result, "stacks with one member here")
                continue
            row = stacks.get(key)
            name = str((row["text"] if row is not None and has_text else "")
                       or "Photo stack")[:80]
            collapsed = 1
            if row is not None and has_collapsed:
                collapsed = 1 if _truthy(row["collapsed"]) else 0
            cursor = writer.execute(
                "INSERT INTO stacks(name, collapsed) VALUES(?,?)",
                (name, collapsed))
            stack_id = int(cursor.lastrowid)
            for position, image_id in enumerate(ours):
                writer.execute(
                    "INSERT OR IGNORE INTO stack_images(stack_id, image_id,"
                    " position) VALUES(?,?,?)", (stack_id, image_id, position))
            occupied.update(ours)
            result["stacks"] += 1
            if done % 50 == 0:
                _report(progress, "stacks", done, total)
    _report(progress, "stacks", total, total)


# ------------------------------------------------------------------- the job

def import_catalog(cat: catalog_module.Catalog, path: Path | str, *,
                   options: dict | None = None,
                   progress: Callable[[dict], None] | None = None,
                   root_map: dict | None = None,
                   should_cancel: Callable[[], bool] | None = None,
                   report_photo: Callable[[dict], None] | None = None) -> dict:
    """Import one Lightroom catalog into `cat`, reporting what did not fit.

    `options` selects the categories: `metadata`, `keywords`, `collections`,
    `stacks`, `develop` (all on by default) and `history` (off). `conflict`
    is one of `skip`, `overwrite`, or `merge`, and decides what happens to an
    image that already carries edits here: leave it alone, replace what the
    imported catalog carries, or keep its edits while still taking its
    metadata, keywords, and memberships. Collection and stack membership is
    applied in every case, because it is organisation and not an edit.
    `filmOff`
    turns the Film pipeline off wherever develop settings are imported, the
    same default an imported preset gets.

    `root_map` re-points roots that have moved, as `{old path: new path}`.
    `progress` receives `{"stage", "done", "total"}`, and `should_cancel` is
    consulted between photographs so a long import can be stopped.

    Returns counts by category, the roots the catalog referred to with how
    many of their files were matched here, the skipped categories, and the
    warnings worth showing.
    """
    opts = _options(options)
    path = Path(path).expanduser()
    result: dict[str, Any] = {
        "images": 0, "matched": 0, "unmatched": 0, "keywords": 0,
        "collections": 0, "stacks": 0, "history": 0,
        "skipped": {}, "warnings": [], "roots": [], "cancelled": False,
        "options": opts, "photos": [], "reportedPhotos": 0, "_report_photo": report_photo,
    }
    cancel = should_cancel if callable(should_cancel) else (lambda: False)
    with _open_readonly(path) as (conn, warnings):
        result["warnings"].extend(warnings)
        data = _data_folder(path)
        if data:
            _skip(result, "masks and AI data")
            _warn(result, f"masks and AI data live in {data.name} and are "
                          "skipped, as they are for presets")
        _report(progress, "opening", 0, 1)
        files = _match_files(conn, cat, root_map, result, progress)
        keywords = _keyword_paths(conn, result) if opts["keywords"] else {}
        targets = _import_images(conn, cat, opts, files, keywords, result,
                                 progress, cancel)
        if opts["trial"] and targets:
            collection = cat.add_collection("Catalog import trial")
            cat.add_to_collection(collection, list(targets.values()))
            result["trialCollectionId"] = collection
        if opts["collections"] and not opts["trial"] and not result["cancelled"]:
            _import_collections(conn, cat, targets, result, progress)
        if opts["stacks"] and not opts["trial"] and not result["cancelled"]:
            _import_stacks(conn, cat, targets, result, progress)
    result.pop("_report_photo", None)
    _report(progress, "done", 1, 1)
    return result


def preview_import(cat, path, **kwargs):
    """Compute real import coverage against a disposable, consistent catalog copy."""
    with tempfile.TemporaryDirectory(prefix="lighttable-import-preview-") as folder:
        target = Path(folder) / "library.sqlite3"
        destination = sqlite3.connect(target)
        try:
            cat.connection.backup(destination)
        finally:
            destination.close()
        preview = catalog_module.Catalog(target)
        try:
            result = import_catalog(preview, path, **kwargs)
            result["previewOnly"] = True
            return result
        finally:
            preview.close()
