#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Extract explicit web translation calls without evaluating application code.

Run with --write to refresh the two source inventories, or --check in CI.
The lexer skips comments, regex literals, and template text, but visits code
inside ${...}; a translation hidden in generated HTML is therefore included.
No npm packages or browser runtime are required.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
REGEX_PREFIX = {"(", "[", "{", "=", ":", ",", ";", "!", "?", "&", "|", "+", "-", "*", "return", "case", "throw", "=>"}


def read_escape(source: str, index: int) -> tuple[str, int]:
    char = source[index]
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    if char in simple:
        return simple[char], index + 1
    if char in "\r\n":
        return "", index + (2 if source[index:index + 2] == "\r\n" else 1)
    if char == "x":
        return chr(int(source[index + 1:index + 3], 16)), index + 3
    if char == "u":
        if source[index + 1:index + 2] == "{":
            end = source.index("}", index + 2)
            return chr(int(source[index + 2:end], 16)), end + 1
        return chr(int(source[index + 1:index + 5], 16)), index + 5
    return char, index + 1


def tokens(source: str) -> list[tuple[str, str, int]]:
    result: list[tuple[str, str, int]] = []

    def scan(index: int, nested: bool = False) -> int:
        depth = 0
        previous = "="
        while index < len(source):
            char = source[index]
            if char.isspace():
                index += 1
                continue
            if source.startswith("//", index):
                end = source.find("\n", index + 2)
                index = len(source) if end < 0 else end + 1
                continue
            if source.startswith("/*", index):
                index = source.index("*/", index + 2) + 2
                continue
            if char in "\"'`":
                start, quote = index, char
                index += 1
                value = ""
                substituted = False
                while index < len(source):
                    if source[index] == "\\":
                        escaped, index = read_escape(source, index + 1)
                        value += escaped
                    elif source[index] == quote:
                        index += 1
                        break
                    elif quote == "`" and source.startswith("${", index):
                        substituted = True
                        index = scan(index + 2, True)
                    else:
                        value += source[index]
                        index += 1
                else:
                    raise ValueError(f"Unclosed string at offset {start}")
                if not substituted:
                    result.append(("string", value, start))
                previous = "string"
                continue
            if char == "/" and previous in REGEX_PREFIX:
                index += 1
                character_class = False
                while index < len(source):
                    if source[index] == "\\":
                        index += 2
                        continue
                    if source[index] == "[":
                        character_class = True
                    elif source[index] == "]":
                        character_class = False
                    elif source[index] == "/" and not character_class:
                        index += 1
                        while index < len(source) and source[index].isalpha():
                            index += 1
                        break
                    index += 1
                previous = "regex"
                continue
            if char.isalpha() or char in "_$":
                end = index + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                previous = source[index:end]
                result.append(("identifier", previous, index))
                index = end
                continue
            if char == "}":
                if nested and depth == 0:
                    return index + 1
                depth -= 1
            elif char == "{":
                depth += 1
            previous = source[index:index + 2] if source.startswith("=>", index) else char
            result.append(("punctuation", previous, index))
            index += len(previous)
        if nested:
            raise ValueError("Unclosed template expression")
        return index

    scan(0)
    return result


def extract(root: Path = ROOT) -> tuple[list[str], dict]:
    messages: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for path in sorted((root / "web").glob("*.js")):
        source = path.read_text()
        lexemes = tokens(source)
        # Only imported translation bindings are eligible; local identifiers
        # called t in rendering/math code are not translation calls.
        aliases: dict[str, str] = {"t": "t", "tn": "tn"} if path.name == 'i18n.js' else {}
        for index, (kind, value, _) in enumerate(lexemes):
            if kind != 'identifier' or value != 'import' or lexemes[index + 1][1] != '{':
                continue
            end = index + 2
            while end < len(lexemes) and lexemes[end][1] != '}':
                end += 1
            if end + 2 >= len(lexemes) or lexemes[end + 1][1] != 'from' or lexemes[end + 2][1] not in {'./i18n.js', '/web/i18n.js'}:
                continue
            cursor = index + 2
            while cursor < end:
                original = local = lexemes[cursor][1]
                cursor += 1
                if cursor < end and lexemes[cursor][1] == 'as':
                    local = lexemes[cursor + 1][1]
                    cursor += 2
                if original in {'t', 'tn'}:
                    aliases[local] = original
                if cursor < end and lexemes[cursor][1] == ',':
                    cursor += 1
        for index, (kind, value, offset) in enumerate(lexemes):
            if kind != "identifier" or value not in aliases:
                continue
            if index and lexemes[index - 1][1] in {".", "?.", "function"}:
                continue
            if index + 1 >= len(lexemes) or lexemes[index + 1][1] != "(":
                continue
            needed = 2 if aliases[value] == "tn" else 1
            strings = []
            cursor = index + 2
            # Static DOM marker lookup is the one runtime-owned exception;
            # its English source is extracted from HTML by localization.py.
            if cursor >= len(lexemes) or lexemes[cursor][0] != "string":
                if path.name == 'i18n.js' and value == 't' and lexemes[cursor][1] == 'source':
                    continue
                line = source.count("\n", 0, offset) + 1
                raise ValueError(f"{path.relative_to(root)}:{line}: {value} requires literal English source strings")
            for argument in range(needed):
                if cursor >= len(lexemes) or lexemes[cursor][0] != "string":
                    line = source.count("\n", 0, offset) + 1
                    raise ValueError(f"{path.relative_to(root)}:{line}: {value} requires literal English source strings")
                strings.append(lexemes[cursor][1])
                cursor += 1
                if argument + 1 < needed:
                    if lexemes[cursor][1] != ",":
                        raise ValueError(f"{path}: plural sources must be separate literals")
                    cursor += 1
            if lexemes[cursor][1] not in {',', ')'}:
                raise ValueError(f"{path}: translation source must be one complete literal, not concatenated fragments")
            messages.update(strings)
            if needed == 2:
                pairs.add((strings[0], strings[1]))
    return sorted(messages), {"pairs": [{"one": one, "other": other} for one, other in sorted(pairs)]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    messages, plurals = extract()
    if args.stdout:
        print(json.dumps({"messages": messages, **plurals}, ensure_ascii=False, indent=2))
        return 0
    stale = []
    for name, data in [("web-dynamic-source.json", messages), ("web-plural-source.json", plurals)]:
        destination = ROOT / "docs" / "localization" / name
        expected = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if args.write:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(expected)
        elif not destination.exists() or destination.read_text() != expected:
            stale.append(str(destination.relative_to(ROOT)))
    if stale:
        print("Translation inventories are stale: " + ", ".join(stale), file=sys.stderr)
        print("Run python3 scripts/extract-web-i18n.py --write", file=sys.stderr)
        return 1
    print(f"{'Wrote' if args.write else 'Verified'} {len(messages)} dynamic sources and {len(plurals['pairs'])} plural pairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
