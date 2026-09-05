"""Stable stdout formatting for people and programs."""
from __future__ import annotations

import json
import sys


def emit(value, *, json_mode: bool = False, jsonl: bool = False,
         quiet: bool = False) -> None:
    if quiet:
        return
    if jsonl:
        items = value if isinstance(value, list) else value.get("items", [])
        for item in items:
            print(json.dumps(item, separators=(",", ":"), ensure_ascii=False))
        return
    if json_mode or isinstance(value, dict):
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print(item.get("name") or item.get("id") or json.dumps(item))
            else:
                print(item)
        return
    print(value)


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
