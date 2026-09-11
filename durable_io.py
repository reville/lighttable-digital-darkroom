# SPDX-License-Identifier: GPL-3.0-only
"""Small crash-safe filesystem primitives used by LightTable's durable data.

The photo originals are deliberately outside this module.  These helpers are
for catalog-adjacent JSON, sidecars, and generated outputs: write beside the
destination, flush the bytes, and only then make the new file visible with one
atomic rename.  A last-known-good backup can be retained for documents that a
user may need to recover after an external or interrupted write.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from server_localization import T


def backup_path(path: Path | str) -> Path:
    path = Path(path)
    name = path.name + ".backup"
    if len(name.encode("utf-8")) > 255:
        # Some filesystems accept longer Unicode names. Keep an already saved
        # backup at the old spelling reachable after upgrading this helper.
        legacy = path.with_name(name)
        try:
            if legacy.is_file():
                return legacy
        except OSError:
            pass
        # Retain a stable association with the complete name when appending
        # .backup would exceed NAME_MAX. A digest separates equal long prefixes.
        tail = "." + hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16] + ".backup"
        prefix = path.name.encode("utf-8")[:255 - len(tail)].decode("utf-8", errors="ignore")
        name = prefix + tail
    return path.with_name(name)


def temporary_path(path: Path | str, label: str = "tmp") -> Path:
    """A unique path beside ``path`` that retains its real file extension."""
    path = Path(path)
    token = f"{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    tail = f".{label}.{token}{path.suffix}"
    # A valid destination can already be close to NAME_MAX. Appending the
    # staging token to its full stem would make an otherwise writable file
    # impossible to save. A UTF-8 byte bound also works on byte-limited volumes.
    budget = 255 - len(tail.encode("utf-8")) - 1
    if budget < 1:
        raise ValueError(T("The file extension is too long for a staging filename"))
    stem = path.stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return path.with_name(f".{stem}{tail}")


def _flush_directory(directory: Path) -> None:
    """Persist directory-entry changes where the platform supports it."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_file(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _copy_file(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("wb") as writer:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())


def atomic_write_bytes(
    path: Path | str,
    payload: bytes,
    *,
    keep_backup: bool = False,
    backup_once: bool = False,
    valid_existing: Callable[[bytes], bool] | None = None,
) -> Path:
    """Durably replace a file without exposing partially-written bytes.

    When ``keep_backup`` is set, only a valid prior file is copied to
    the sibling returned by :func:`backup_path`. ``backup_once`` is useful for
    XMP: the first pre-app sidecar is retained rather than gradually replaced
    by app-authored copies.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_path(path)
    backup_temporary: Path | None = None
    try:
        _write_file(temporary, bytes(payload))
        if keep_backup and path.is_file():
            previous = path.read_bytes()
            valid = valid_existing(previous) if valid_existing else True
            saved = backup_path(path)
            if valid and not (backup_once and saved.exists()):
                backup_temporary = temporary_path(saved, "backup")
                # Use the exact bytes that passed validation. Reopening the
                # live path here would let an external writer swap in damaged
                # content between validation and backup publication.
                _write_file(backup_temporary, previous)
                os.replace(backup_temporary, saved)
                backup_temporary = None
        os.replace(temporary, path)
        _flush_directory(path.parent)
        return path
    finally:
        temporary.unlink(missing_ok=True)
        if backup_temporary is not None:
            backup_temporary.unlink(missing_ok=True)


def _valid_json(payload: bytes) -> bool:
    try:
        json.loads(payload.decode("utf-8"))
        return True
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    keep_backup: bool = False,
    backup_once: bool = False,
) -> Path:
    return atomic_write_bytes(
        path, text.encode(encoding), keep_backup=keep_backup,
        backup_once=backup_once,
    )


def atomic_write_json(
    path: Path | str,
    value: Any,
    *,
    indent: int | None = 1,
    keep_backup: bool = True,
) -> Path:
    payload = json.dumps(value, indent=indent).encode("utf-8")
    return atomic_write_bytes(
        path, payload, keep_backup=keep_backup, valid_existing=_valid_json,
    )


def load_json(path: Path | str, default: Any) -> Any:
    """Read JSON, falling back to its last valid backup without rewriting."""
    path = Path(path)
    for candidate in (path, backup_path(path)):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return default


def atomic_create_json(
    path: Path | str,
    value: Any,
    *,
    indent: int | None = 1,
) -> Path:
    """Durably create JSON while refusing to replace an existing file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = temporary_path(path, "create")
    try:
        _write_file(staged, json.dumps(value, indent=indent).encode("utf-8"))
        publish_file_no_replace(staged, path)
        staged.unlink(missing_ok=True)
        return path
    finally:
        staged.unlink(missing_ok=True)


def publish_file(staged: Path | str, destination: Path | str) -> Path:
    """Publish a completed staged output, preserving the old one on failure."""
    staged, destination = Path(staged), Path(destination)
    # Windows _commit (os.fsync) requires a writable file handle.
    with staged.open("r+b" if os.name == "nt" else "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(staged, destination)
    _flush_directory(destination.parent)
    return destination


def publish_file_no_replace(staged: Path | str, destination: Path | str) -> Path:
    """Publish completed bytes without replacement; may consume the stage."""
    staged, destination = Path(staged), Path(destination)
    # Windows _commit (os.fsync) requires a writable file handle.
    with staged.open("r+b" if os.name == "nt" else "rb") as handle:
        os.fsync(handle.fileno())
    try:
        os.link(staged, destination)
    except OSError as error:
        if not _link_unsupported(error):
            raise
        _rename_no_replace(staged, destination)
    _flush_directory(destination.parent)
    return destination


def _link_unsupported(error: OSError) -> bool:
    # FAT/exFAT and some network filesystems have no hard links. Do not turn
    # collisions, missing sources, or a disconnected/full volume into retries.
    return error.errno in {errno.EPERM, errno.ENOTSUP, errno.ENOSYS} or \
        getattr(error, "winerror", None) in (1, 50)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomic no-clobber rename for filesystems without hard links.

    Never emulate this with exists()+replace() or an exposed partial copy.
    Unsupported platforms/filesystems fail while keeping the source intact.
    """
    if sys.platform == "win32":
        # Unlike POSIX rename, Windows os.rename refuses an existing target.
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(destination), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        # AT_FDCWD, RENAME_NOREPLACE; supported by modern Linux filesystems.
    else:
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP), str(source))
    rename.restype = ctypes.c_int
    if rename(*args) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def copy_file_no_replace(source: Path | str, destination: Path | str) -> Path:
    """Atomically copy a file while refusing to overwrite a raced-in target."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = temporary_path(destination, "copy")
    try:
        _copy_file(source, staged)
        try:
            shutil.copystat(source, staged)
        except OSError:
            pass
        # Publish a completed sibling with the filesystem's no-clobber primitive.
        # Unlike exists()+replace(), another process winning the name is an
        # error rather than permission to overwrite its file.
        publish_file_no_replace(staged, destination)
        return destination
    finally:
        staged.unlink(missing_ok=True)


def move_file_no_replace(source: Path | str, destination: Path | str) -> Path:
    """Move a file without ever replacing a raced-in target.

    A hard link makes the complete source visible at the destination before
    the old name is removed. If interruption lands between those operations,
    both names reference the same bytes, which is recoverable and lossless.
    Across volumes (EXDEV), copies atomically to a staged destination first,
    durably publishes without replacing, and only then removes the source.
    """
    source, destination = Path(source), Path(destination)
    try:
        os.link(source, destination)
    except OSError as error:
        if error.errno == errno.EXDEV or getattr(error, "winerror", None) == 17:
            copy_file_no_replace(source, destination)
            try:
                source.unlink()
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            _flush_directory(source.parent)
            return destination
        if not _link_unsupported(error):
            raise
        try:
            _rename_no_replace(source, destination)
        except OSError as ren_error:
            if ren_error.errno == errno.EXDEV or getattr(ren_error, "winerror", None) == 17:
                copy_file_no_replace(source, destination)
                try:
                    source.unlink()
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                _flush_directory(source.parent)
                return destination
            raise
        _flush_directory(destination.parent)
        _flush_directory(source.parent)
        return destination
    _flush_directory(destination.parent)
    try:
        source.unlink()
    except Exception:
        # The source still exists, so remove only the link this call created.
        destination.unlink(missing_ok=True)
        raise
    _flush_directory(source.parent)
    return destination


def publish_cache(staged: Path | str, destination: Path | str) -> Path:
    """Atomically publish disposable, validated cache data without fsync.

    Only reproducible cache entries may use this path. Catalog state, originals,
    user exports and sidecars must continue using the durable publishers.
    """
    destination = Path(destination)
    os.replace(staged, destination)
    return destination


def cache_write_bytes(path: Path | str, payload: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = temporary_path(path, "cache")
    try:
        staged.write_bytes(payload)
        return publish_cache(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def cache_write_json(path: Path | str, value: Any, *,
                     indent: int | None = None) -> Path:
    return cache_write_bytes(path, json.dumps(value, indent=indent).encode("utf-8"))
