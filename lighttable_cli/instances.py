"""Discover and safely clean LightTable instance registrations."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import platform_paths


def default_instance_directory() -> Path:
    return platform_paths.instance_directory()


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # EPERM means the process exists but this caller may not signal it.
        return True
    except (OSError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class Instance:
    url: str
    port: int
    pid: int
    token: str
    catalog: str | None
    folder: str
    headless: bool
    path: Path

    @classmethod
    def from_record(cls, record: dict, path: Path) -> "Instance":
        if not isinstance(record, dict):
            raise ValueError("instance registration must be an object")
        host = str(record.get("host") or "127.0.0.1")
        port = int(record["port"])
        pid = int(record["pid"])
        if not 1 <= port <= 65535 or path.name != f"{port}.json" or pid <= 0:
            raise ValueError("invalid instance registration identity")
        return cls(
            url=f"http://{host}:{port}", port=port, pid=pid,
            token=str(record.get("token") or ""),
            catalog=str(record["catalog"]) if record.get("catalog") else None,
            folder=str(record.get("folder") or ""),
            headless=bool(record.get("headless")), path=path,
        )


def discover(directory: Path | None = None, *, clean_stale: bool = True) -> list[Instance]:
    root = directory or default_instance_directory()
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.glob("*.json")):
        # The server writes <port>.json. Native startup reports share this
        # directory and may also contain a live pid and port; they are neither
        # registrations nor ours to remove, even when malformed or stale.
        stem = path.stem
        if (not stem.isascii() or not stem.isdecimal()
                or not 1 <= int(stem) <= 65535 or stem != str(int(stem))):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            instance = Instance.from_record(record, path)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            if clean_stale:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        if not process_is_alive(instance.pid):
            if clean_stale:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        found.append(instance)
    return found


def select(instances: list[Instance], *, port: int | None = None,
           catalog: str | None = None) -> Instance | None:
    choices = instances
    if port is not None:
        choices = [item for item in choices if item.port == port]
    if catalog:
        wanted = str(Path(catalog).expanduser().resolve())
        choices = [item for item in choices
                   if item.catalog and str(Path(item.catalog).resolve()) == wanted]
    if len(choices) == 1:
        return choices[0]
    if not choices:
        return None
    raise RuntimeError("several LightTable instances are running; use --port or --catalog")
