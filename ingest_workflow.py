"""Card ingest: scan a card, plan a verified copy, and run it file by file."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import durable_io

import media_formats


# The same set server.py browses (its `EXTS`: processed plus every RAW format
# the bundled LibRaw decodes). A card holds what the library can open, so the
# two lists have to stay identical; the test suite compares them.
PHOTO_EXTENSIONS = media_formats.PHOTO_EXTENSIONS

DATE_TOKENS = {"yyyy", "yy", "mm", "dd", "hh", "min", "ss"}
TOKENS = {"filename", "camera", "sequence", "custom", *DATE_TOKENS}

FOLDER_TEMPLATE = "{yyyy}/{yyyy}-{mm}-{dd}"
FILENAME_TEMPLATE = "{filename}"
SEQUENCE_DIGITS = 4
VERIFY_MODES = ("none", "size", "hash")
DUPLICATE_POLICIES = ("skip", "copy")
HEADER_CHUNK = 65536
COPY_CHUNK = 1 << 20
MOMENT_FORMAT = "%Y-%m-%dT%H:%M:%S"

_MOMENT_RE = re.compile(
    r"(\d{4})\D(\d{1,2})\D(\d{1,2})(?:\D+(\d{1,2})\D(\d{1,2})\D(\d{1,2}))?")


# --- small shared helpers ---------------------------------------------------

def _text(value, default: str, limit: int) -> str:
    result = " ".join(str(value or default).split()).strip()
    return (result or default)[:limit]


def _safe_piece(value, default="untitled") -> str:
    """Sanitise one path SEGMENT, as `export_workflow._safe_piece` does.

    Separators, device characters and leading or trailing dots are removed, so
    a segment can never reach a parent folder or name a Windows device.
    """
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return (value or default)[:120]


def _reason(error: OSError) -> str:
    return str(getattr(error, "strerror", "") or error)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _size(path: Path) -> int:
    return Path(path).stat().st_size


def _parse_moment(value) -> datetime | None:
    """Read a capture time out of EXIF or ISO text, tolerating camera junk."""
    match = _MOMENT_RE.search(str(value or ""))
    if not match:
        return None
    parts = [int(piece or 0) for piece in match.groups()]
    try:
        return datetime(*parts)
    except ValueError:  # "0000:00:00 00:00:00" and other empty EXIF stamps
        return None


def _format_moment(moment: datetime) -> str:
    return moment.strftime(MOMENT_FORMAT)


# --- identity ---------------------------------------------------------------

def header_hash(path: Path, *, chunk: int = HEADER_CHUNK) -> str:
    """Identity for one file: BLAKE2b-128 over its size and first `chunk` bytes.

    The digest is `hashlib.blake2b(digest_size=16)` fed the byte length in
    decimal ASCII, a newline, then the first `chunk` bytes of the file, and is
    returned as 32 hex characters. It is cheap enough to run over a whole card
    and is the identity the catalog stores for a photo, so the recipe is fixed:
    changing the size prefix, the chunk size or the digest size would stop new
    hashes matching the ones already written.
    """
    path = Path(path)
    digest = hashlib.blake2b(digest_size=16)
    digest.update(f"{path.stat().st_size}\n".encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(max(1, int(chunk))))
    return digest.hexdigest()


def _file_hash(path: Path) -> str:
    """Full-content BLAKE2b-128, used when a copy is verified in "hash" mode."""
    digest = hashlib.blake2b(digest_size=16)
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(COPY_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


# --- scanning ---------------------------------------------------------------

def _exif_text(data, *keys: str) -> str:
    """First value present among `keys`, as `platform_image` reads exiv2 data."""
    import exiv2

    for key in keys:
        item = data.findKey(exiv2.ExifKey(key))
        if item != data.end():
            return item.toString()
    return ""


def _capture_and_camera(path: Path) -> tuple[str, str]:
    """Capture time and camera name read straight from the file header.

    exiv2 reads only the metadata blocks, so this stays cheap on a card full of
    RAW files. Anything unreadable comes back empty and the caller falls back to
    the file's modification time.
    """
    try:
        import exiv2

        image = exiv2.ImageFactory.open(str(path))
        image.readMetadata()
        data = image.exifData()
        capture = _exif_text(data, "Exif.Photo.DateTimeOriginal",
                             "Exif.Image.DateTimeOriginal", "Exif.Image.DateTime")
        make = " ".join(_exif_text(data, "Exif.Image.Make").split())
        model = " ".join(_exif_text(data, "Exif.Image.Model").split())
    except Exception:  # noqa: BLE001 - a card of odd files must still scan
        return "", ""
    if model.casefold().startswith(make.casefold()) and make:
        camera = model  # Canon writes "Canon" in both fields
    else:
        camera = " ".join(part for part in (make, model) if part)
    moment = _parse_moment(capture)
    return (_format_moment(moment) if moment else ""), camera[:80]


def _scan_root(root: Path) -> Path:
    """Cards keep photos under DCIM; any other folder is scanned whole."""
    try:
        for child in sorted(root.iterdir()):
            if child.name.lower() == "dcim" and child.is_dir():
                return child
    except OSError:
        pass
    return root


def _walk_photos(start: Path):
    for directory, folders, names in os.walk(start):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in PHOTO_EXTENSIONS:
                yield Path(directory) / name


def _describe(path: Path) -> dict:
    stat = path.stat()
    capture, camera = _capture_and_camera(path)
    fallback = _format_moment(datetime.fromtimestamp(stat.st_mtime))
    return {
        "path": str(path), "name": path.name, "ext": path.suffix.lower(),
        "size": int(stat.st_size), "mtime": float(stat.st_mtime),
        "captureTime": capture or fallback, "camera": camera,
        "hash": header_hash(path),
    }


def describe_file(path: Path | str) -> dict:
    """Public one-file counterpart to :func:`scan_source` for hot folders."""
    candidate = Path(path).expanduser()
    if candidate.suffix.lower() not in PHOTO_EXTENSIONS or not candidate.is_file():
        raise ValueError("the watched arrival is not a supported photo")
    return _describe(candidate)


def scan_source(root: Path, *, limit: int = 20000) -> list[dict]:
    """List up to `limit` photos on a card, with capture time, camera and hash."""
    root = Path(root).expanduser()
    limit = max(1, min(200000, int(limit or 0) or 20000))
    items = []
    for path in _walk_photos(_scan_root(root)):
        try:
            items.append(_describe(path))
        except OSError:
            continue  # a card unplugged mid-scan simply yields fewer photos
        if len(items) >= limit:
            break
    return items


# --- request validation -----------------------------------------------------

def clean_plan_request(raw: dict | None) -> dict:
    """Clamp one ingest request into the exact shape `build_plan` expects."""
    raw = raw if isinstance(raw, dict) else {}
    folder = str(raw.get("folderTemplate") or FOLDER_TEMPLATE)[:160]
    if not folder.strip():
        folder = FOLDER_TEMPLATE
    filename = str(raw.get("filenameTemplate") or FILENAME_TEMPLATE)[:160]
    if not filename.strip():
        filename = FILENAME_TEMPLATE
    try:
        start = max(1, min(1000000, int(raw.get("startNumber", 1) or 1)))
    except (TypeError, ValueError):
        start = 1
    # `or` rather than a default argument: str(None) is "none", a real mode.
    verify = str(raw.get("verify") or "hash").lower()
    if verify not in VERIFY_MODES:
        verify = "hash"
    duplicate = str(raw.get("onDuplicate") or "skip").lower()
    if duplicate not in DUPLICATE_POLICIES:
        duplicate = "skip"
    backup = str(raw.get("backupDestination") or "").strip()[:1000]
    preset = str(raw.get("preset") or "").strip()[:200]
    selected = raw.get("selected")
    if isinstance(selected, list):
        seen = set()
        chosen = []
        for value in selected[:200000]:
            path = str(value)[:1000]
            if path and path not in seen:
                seen.add(path); chosen.append(path)
        selected = chosen
    else:
        selected = None
    metadata = raw.get("metadataPreset")
    return {
        "destination": str(raw.get("destination") or "").strip()[:1000],
        "folderTemplate": folder, "filenameTemplate": filename,
        "custom": _text(raw.get("custom"), "", 60),
        "startNumber": start, "backupDestination": backup or None,
        "eject": bool(raw.get("eject", False)), "verify": verify,
        "onDuplicate": duplicate, "selected": selected,
        "preset": preset or None,
        # The metadata preset schema lands with Phase 3.3; carry it through.
        "metadataPreset": metadata if isinstance(metadata, dict) else None,
    }


# --- templates --------------------------------------------------------------

def render_path(template: str, context: dict) -> str:
    """Render one folder or filename template to a safe relative path.

    Every token value is sanitised per path segment before substitution, so a
    value carrying `/`, `\\` or `..` can never introduce a separator or reach a
    parent folder; only separators written in the template itself divide
    folders. A known token with no value drops out, and an unknown token renders
    as its own name, matching `export_workflow.render_filename`.
    """
    def replace(match):
        token = match.group(1)
        return _safe_piece(context.get(token, ""), "" if token in TOKENS else token)

    rendered = re.sub(r"\{([a-zA-Z]+)\}", replace, str(template or ""))
    segments = [_safe_piece(piece) for piece in re.split(r"[\\/]+", rendered)
                if piece.strip(" .")]
    return "/".join(segments)


def template_context(item: dict, sequence: int, custom: str) -> dict:
    """Token values for one described photo: name, camera, sequence, dates."""
    moment = _parse_moment(item.get("captureTime"))
    if moment is None:
        try:
            moment = datetime.fromtimestamp(float(item.get("mtime") or 0))
        except (TypeError, ValueError, OSError, OverflowError):
            moment = datetime.fromtimestamp(0)
    return {
        "filename": Path(str(item.get("name") or "")).stem,
        "camera": str(item.get("camera") or ""),
        "sequence": f"{int(sequence):0{SEQUENCE_DIGITS}d}",
        "custom": custom,
        "yyyy": f"{moment.year:04d}", "yy": f"{moment.year % 100:02d}",
        "mm": f"{moment.month:02d}", "dd": f"{moment.day:02d}",
        "hh": f"{moment.hour:02d}", "min": f"{moment.minute:02d}",
        "ss": f"{moment.second:02d}",
    }


# --- planning ---------------------------------------------------------------

def _destination_root(value) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("choose an ingest destination")
    root = Path(raw).expanduser().resolve()
    if root == Path(root.anchor):
        raise ValueError("choose a specific ingest folder")
    return root


def _same_photo(path: Path, item: dict) -> bool:
    """True when a file already on disk is this very photo, from an earlier run."""
    try:  # the header hash already covers the byte length
        return header_hash(path) == str(item.get("hash") or "")
    except OSError:
        return False


def _unique_path(candidate: Path, taken: set[str], item: dict) -> Path:
    """First free name for one photo, appending -2, -3 … on a collision.

    A path is free when no earlier item in this plan claimed it and nothing else
    occupies it on disk. A file that is already an identical copy of this photo
    counts as free, so an interrupted ingest resumes onto the same names instead
    of writing a second numbered set.
    """
    for index in range(1, 10000):
        path = candidate if index == 1 else candidate.with_name(
            f"{candidate.stem}-{index}{candidate.suffix}")
        key = str(path).casefold()  # macOS and Windows fold case
        if key in taken:
            continue
        if path.exists() and not _same_photo(path, item):
            continue
        taken.add(key)
        return path
    raise ValueError(f"could not find a free name for {candidate.name}")


def build_plan(items, request, *, existing_hashes=None) -> dict:
    """Turn scanned items and one request into an exact, collision-free copy list."""
    request = clean_plan_request(request)
    root = _destination_root(request["destination"])
    backup_root = (_destination_root(request["backupDestination"])
                   if request["backupDestination"] else None)
    known = {str(value).casefold() for value in (existing_hashes or ())}
    selected = None if request["selected"] is None else set(request["selected"])

    chosen = [item for item in items or () if isinstance(item, dict)
              and (selected is None or str(item.get("path", "")) in selected)]
    chosen.sort(key=lambda item: (str(item.get("captureTime") or ""),
                                  str(item.get("name") or ""),
                                  str(item.get("path") or "")))

    planned: list[dict] = []
    skipped: list[dict] = []
    taken: set[str] = set()
    sequence = request["startNumber"]
    duplicates = 0
    for item in chosen:
        digest = str(item.get("hash") or "")
        duplicate = bool(digest) and digest.casefold() in known
        if duplicate:
            duplicates += 1
            if request["onDuplicate"] == "skip":
                skipped.append({"source": str(item.get("path", "")),
                                "name": str(item.get("name", "")),
                                "hash": digest, "reason": "duplicate"})
                continue
        context = template_context(item, sequence, request["custom"])
        folder = render_path(request["folderTemplate"], context)
        name = str(item.get("name", ""))
        stem = render_path(request["filenameTemplate"], context) \
            or _safe_piece(Path(name).stem)
        # The card's own extension case is kept: RAW+JPEG pairs stay recognisable.
        relative = f"{folder}/{stem}" if folder else stem
        destination = _unique_path(
            Path(f"{root / relative}{Path(name).suffix}"), taken, item)
        backup = (backup_root / destination.relative_to(root)) if backup_root else None
        planned.append({
            "source": str(item.get("path", "")), "name": name,
            "destination": str(destination),
            "backup": str(backup) if backup else None,
            "sequence": sequence, "hash": digest,
            "size": int(item.get("size", 0) or 0),
            "captureTime": str(item.get("captureTime", "")),
            "camera": str(item.get("camera", "")), "duplicate": duplicate,
        })
        sequence += 1
    return {
        "items": planned, "skipped": skipped, "total": len(planned),
        "bytes": sum(entry["size"] for entry in planned),
        "duplicates": duplicates,
    }


# --- copying ----------------------------------------------------------------

def _copy_bytes(source: Path, temporary: Path) -> int:
    """Stream one file to a temporary beside its destination and flush it to disk."""
    total = 0
    with open(source, "rb") as reader, open(temporary, "wb") as writer:
        for block in iter(lambda: reader.read(COPY_CHUNK), b""):
            writer.write(block)
            total += len(block)
        writer.flush()
        os.fsync(writer.fileno())
    try:
        shutil.copystat(source, temporary)
    except OSError:
        pass  # timestamps are a nicety; the bytes are what must arrive
    return total


def _mismatch(source: Path, destination: Path, mode: str) -> str | None:
    """Say how a copy differs from its source, or None when the two agree."""
    try:
        source_size, copied_size = _size(source), _size(destination)
        if source_size != copied_size:
            return f"copied {copied_size} of {source_size} bytes"
        if mode == "hash" and _file_hash(source) != _file_hash(destination):
            return "copied bytes do not match the source"
        return None
    except OSError as error:
        return _reason(error)


def _copy_verified(source: Path, destination: Path, mode: str) -> str | None:
    """Copy one file into place and check it, returning an error or None.

    The copy lands in a unique temporary file in the destination folder, is
    flushed and fsynced, then published without replacement, so a half-written
    file is never mistaken for a finished one and simultaneous imports cannot
    clobber each other. A destination that already matches its source is left
    alone, which is what makes a rerun resume. Nothing is written to the source.
    """
    temporary = durable_io.temporary_path(destination, "ingest")
    # Even "none" compares size here, so a rerun cannot adopt a stray file.
    if destination.exists():
        mismatch = _mismatch(
            source, destination, "size" if mode == "none" else mode)
        if not mismatch:
            return None
        return f"{source.name}: destination appeared or changed ({mismatch})"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_bytes(source, temporary)
        # Verify the private staging file before making it visible. This also
        # means a third party changing the destination after publication can
        # never cause us to delete a file we no longer own.
        problem = _mismatch(
            source, temporary, "size" if mode == "none" else mode)
        if problem:
            _remove(temporary)
            return f"{source.name}: {problem}"
        durable_io.publish_file_no_replace(temporary, destination)
        _remove(temporary)
    except OSError as error:
        # The rename is atomic, so an unreadable card or a full disk costs only
        # the partial file; a copy an earlier run verified stays where it is.
        _remove(temporary)
        return f"{source.name}: {_reason(error)}"
    return None


def _sidecar_pairs(source: Path, destination: Path) -> list[tuple[Path, Path]]:
    """Both sidecar forms: `photo.xmp` beside `photo.CR2`, and `photo.CR2.xmp`."""
    pairs = []
    seen = set()
    for candidate, target in ((source.with_suffix(".xmp"),
                               destination.with_suffix(".xmp")),
                              (Path(f"{source}.xmp"), Path(f"{destination}.xmp"))):
        if str(target) in seen or not candidate.is_file():
            continue
        seen.add(str(target))
        pairs.append((candidate, target))
    return pairs


def copy_item(item: dict, *, verify: str = "hash") -> dict:
    """Copy one planned item, with its sidecars and optional backup, and verify it.

    Never raises and never writes to the card. Any file that fails verification
    is removed from the destination, the source is left exactly as it was, and
    the result carries `ok: False` with the first error; a photo that verified
    stays even when one of its sidecars did not. `bytes` counts the photo
    itself, so a caller can sum it for progress.
    """
    item = item if isinstance(item, dict) else {}
    mode = verify if verify in VERIFY_MODES else "hash"
    source_value = str(item.get("source") or "")
    destination_value = str(item.get("destination") or "")
    backup_value = str(item.get("backup") or "")
    result = {"ok": False, "destination": destination_value,
              "backup": backup_value or None, "bytes": 0, "error": None}
    if not source_value or not destination_value:
        result["error"] = "ingest item is missing a source or a destination"
        return result

    source, destination = Path(source_value), Path(destination_value)
    error = _copy_verified(source, destination, mode)
    if error:
        result["error"] = error
        return result
    try:
        result["bytes"] = _size(destination)
    except OSError:
        result["bytes"] = 0

    errors = [_copy_verified(sidecar, target, mode)
              for sidecar, target in _sidecar_pairs(source, destination)]
    if backup_value:
        backup = Path(backup_value)
        errors.append(_copy_verified(source, backup, mode))
        errors += [_copy_verified(sidecar, target, mode)
                   for sidecar, target in _sidecar_pairs(source, backup)]
    errors = [message for message in errors if message]
    result["ok"] = not errors
    result["error"] = errors[0] if errors else None
    return result
