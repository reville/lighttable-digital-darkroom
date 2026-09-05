#!/usr/bin/env python3
"""Publish the supported-camera page from the LibRaw the app actually links.

LightTable decodes RAW files with LibRaw, through rawpy.  rawpy exposes no
camera list of its own, but the LibRaw shared library inside its wheel does:
`libraw_cameraCount()` and `libraw_cameraList()` are plain C entry points that
ctypes can call without a build step.  Reading the list out of the linked
library, rather than off a version tag in LibRaw's source tree, means the
published page can never claim support the shipped binary does not have.

    .venv/bin/python scripts/camera-list.py \
        --output /path/to/lighttable-site/cameras.html
    .venv/bin/python scripts/camera-list.py --check \
        --output /path/to/lighttable-site/cameras.html
    .venv/bin/python scripts/camera-list.py --json out.json

Set ``LIGHTTABLE_SITE_ROOT`` to make the canonical website checkout the
default target. The app repository deliberately carries no duplicate site.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import datetime
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
_SITE_ROOT = os.environ.get("LIGHTTABLE_SITE_ROOT")
DEFAULT_OUTPUT = (
    Path(_SITE_ROOT).expanduser() / "cameras.html" if _SITE_ROOT else None
)
VERSION_META = "libraw-version"
COUNT_META = "camera-count"
GENERATED_META = "generated"

# LibRaw hands back one flat string per camera and the maker is simply its
# first word, which is right for "Canon EOS R5" and wrong in two ways: a few
# spellings are not the ones the makers use, and a few makers are two or three
# words that a first-word split cuts in half ("Digital Bolex D16" would file
# under "Digital").  Keys are matched against the start of the LibRaw string,
# longest first; values are the heading printed on the page.  Anything absent
# keeps its first word, which is correct for the other eighty-odd makers.
MANUFACTURER_NAMES = {
    "AutelRobotics": "Autel Robotics",
    "BlackMagic": "Blackmagic",
    "DXO": "DxO",
    "Digital Bolex": "Digital Bolex",
    "FujiFilm": "Fujifilm",
    "GITUP": "GitUp",
    "JaiPulnix": "JAI Pulnix",
    "OM Digital Solutions": "OM System",  # the brand on the bodies themselves
    "PARROT": "Parrot",
    "PhaseOne": "Phase One",
    "PtGrey": "Point Grey",
    "RaspberryPi": "Raspberry Pi",
}
# Longest first so "Digital Bolex" wins over a bare "Digital".
MANUFACTURER_PREFIXES = tuple(
    sorted(MANUFACTURER_NAMES, key=lambda prefix: (-len(prefix), prefix)))


class LibRawUnavailable(RuntimeError):
    """No LibRaw could be loaded, so there is no camera list to publish."""


@dataclass(frozen=True)
class CameraList:
    """What one LibRaw binary reports about itself."""

    version: str
    version_number: int
    cameras: tuple[str, ...]
    library: Path

    @property
    def count(self) -> int:
        return len(self.cameras)


@dataclass(frozen=True)
class Manufacturer:
    """One heading on the page and the models filed under it."""

    name: str
    models: tuple[str, ...]


# --------------------------------------------------------------------------
# Finding and reading LibRaw
# --------------------------------------------------------------------------

def candidate_libraries() -> list[Path]:
    """Bundled LibRaw copies, most trustworthy first.

    Only wheel-internal copies count.  A system LibRaw is usually a different
    build from the one rawpy is linked against — Homebrew's 0.21.4 knows 94
    fewer cameras than the 0.22.0 in the wheel — and publishing its list would
    describe an app nobody is running.
    """
    try:
        import rawpy
    except ImportError:
        return []
    package = Path(rawpy.__file__).resolve().parent
    # macOS wheels delocate into `.dylibs`, manylinux wheels put the auditwheel
    # copy in a sibling `rawpy.libs`, and the Windows wheel ships the DLL right
    # next to the extension module.
    searches = (
        (package / ".dylibs", ("libraw*.dylib",)),
        (package.parent / "rawpy.libs", ("libraw*.so*",)),
        (package, ("libraw*.dll", "raw*.dll")),
    )
    found: list[Path] = []
    for directory, patterns in searches:
        if not directory.is_dir():
            continue
        for pattern in patterns:
            found.extend(sorted(directory.glob(pattern)))
    return found


def read_library(path: Path) -> CameraList | None:
    """Read the camera list out of one shared library, or None if it cannot."""
    try:
        library = ctypes.CDLL(str(path))
    except OSError:
        return None
    try:
        library.libraw_version.restype = ctypes.c_char_p
        library.libraw_versionNumber.restype = ctypes.c_int
        library.libraw_cameraCount.restype = ctypes.c_int
        library.libraw_cameraList.restype = ctypes.POINTER(ctypes.c_char_p)
    except AttributeError:
        return None  # a library called libraw*, but not LibRaw
    count = library.libraw_cameraCount()
    names = library.libraw_cameraList()
    if count <= 0 or not names:
        return None
    cameras = tuple(
        " ".join(names[index].decode("utf-8", "replace").split())
        for index in range(count))
    return CameraList(
        version=library.libraw_version().decode("utf-8", "replace"),
        version_number=library.libraw_versionNumber(),
        cameras=cameras,
        library=path,
    )


def load_libraw(library: Path | None = None) -> CameraList:
    """Load the bundled LibRaw, or raise LibRawUnavailable."""
    if library is not None:
        found = read_library(Path(library))
        if found is None:
            raise LibRawUnavailable(f"{library} is not a usable LibRaw")
        return found
    for candidate in candidate_libraries():
        found = read_library(candidate)
        if found is not None:
            return found
    # Last resort only: whatever the loader finds on the system path.
    system = ctypes.util.find_library("raw")
    if system:
        found = read_library(Path(system))
        if found is not None:
            return found
    raise LibRawUnavailable(
        "no LibRaw could be loaded; install rawpy in this interpreter")


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------

def split_camera(name: str) -> tuple[str, str]:
    """Return (manufacturer heading, model) for one LibRaw camera string."""
    cleaned = " ".join(name.split())
    for prefix in MANUFACTURER_PREFIXES:
        if cleaned == prefix:
            return MANUFACTURER_NAMES[prefix], cleaned
        if cleaned.startswith(prefix + " "):
            return MANUFACTURER_NAMES[prefix], cleaned[len(prefix) + 1:]
    head, _, tail = cleaned.partition(" ")
    return head, tail or cleaned


def natural_key(text: str) -> tuple:
    """Sort "EOS 5D" before "EOS 50D" the way a reader expects.

    re.split on a captured group always alternates text, number, text, so the
    two element types never meet at the same index of two keys. The raw text
    is carried along as a tiebreak because "K-1" and "K-01" reduce to the same
    natural key, and without a total order the page would come out in a
    different order from one run to the next.
    """
    return ([part if index % 2 == 0 else int(part)
             for index, part in enumerate(re.split(r"(\d+)", text.lower()))],
            text)


def group_cameras(names) -> list[Manufacturer]:
    """Group LibRaw camera strings under sorted manufacturer headings."""
    grouped: dict[str, list[str]] = {}
    for name in names:
        maker, model = split_camera(name)
        grouped.setdefault(maker, []).append(model)
    return [
        # dict.fromkeys rather than set(): deduplicating must not reintroduce
        # the run-to-run ordering the tiebreak above exists to remove.
        Manufacturer(maker, tuple(sorted(dict.fromkeys(models),
                                         key=natural_key)))
        for maker, models in sorted(grouped.items(),
                                    key=lambda item: natural_key(item[0]))
    ]


def slug(text: str) -> str:
    return "m-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def accepted_extensions(app_root: Path = APP_ROOT) -> tuple[str, ...]:
    """The RAW suffixes the app opens, read from the shared manifest."""
    source = app_root / "media-formats.json"
    if not source.is_file():
        return ()
    try:
        manifest = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError):
        return ()
    return tuple(sorted(set(str(value).lower()
                            for value in manifest.get("raw", []))))


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

# Lifted from docs/styles.css so the page is one file with no fetches: same
# tokens, same faces, same header and footer rules.  Only the pieces this page
# uses are copied, plus the rules for the list and the filter.
PAGE_CSS = """
      :root {
        color-scheme: dark;
        --ink: #f3efe2;
        --muted: #aaa696;
        --dim: #777467;
        --paper: #15140f;
        --panel: #1d1c16;
        --line: rgba(243, 239, 226, 0.14);
        --acid: #e8ef84;
        --warm: #d7a76e;
        --max: 1240px;
        --radius: 10px;
        font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
          "Segoe UI", sans-serif;
        font-synthesis: none;
      }

      * {
        box-sizing: border-box;
      }

      html {
        scroll-behavior: smooth;
      }

      body {
        margin: 0;
        color: var(--ink);
        background:
          radial-gradient(circle at 75% 2%, rgba(232, 239, 132, 0.08), transparent 28rem),
          var(--paper);
        font-size: 16px;
        line-height: 1.55;
      }

      a {
        color: inherit;
        text-decoration: none;
      }

      [hidden] {
        display: none !important;
      }

      .skip-link {
        position: fixed;
        left: 1rem;
        top: -5rem;
        z-index: 10;
        padding: 0.75rem 1rem;
        color: #11120b;
        background: var(--acid);
        border-radius: 6px;
      }

      .skip-link:focus {
        top: 1rem;
      }

      .site-header {
        width: min(calc(100% - 48px), var(--max));
        min-height: 92px;
        margin: 0 auto;
        display: flex;
        align-items: center;
        justify-content: space-between;
        border-bottom: 1px solid var(--line);
      }

      .wordmark {
        display: inline-flex;
        align-items: center;
        gap: 0.7rem;
        font-size: 1.06rem;
        font-weight: 640;
        letter-spacing: -0.025em;
      }

      .wordmark img {
        border-radius: 8px;
      }

      .site-header nav {
        display: flex;
        align-items: center;
        gap: 2rem;
        font-size: 0.78rem;
        font-weight: 600;
        letter-spacing: 0.07em;
        text-transform: uppercase;
      }

      .site-header nav a {
        color: var(--muted);
        transition: color 150ms ease;
      }

      .site-header nav a:hover,
      .site-header nav a:focus-visible {
        color: var(--ink);
      }

      .site-header nav a[aria-current="page"] {
        color: var(--ink);
      }

      .nav-download {
        padding: 0.62rem 0.9rem;
        color: var(--ink);
        border: 1px solid var(--line);
        border-radius: 999px;
      }

      main,
      footer {
        width: min(calc(100% - 48px), var(--max));
        margin-inline: auto;
      }

      h1,
      h2,
      h3,
      p {
        margin-top: 0;
      }

      .eyebrow {
        display: flex;
        align-items: center;
        gap: 0.65rem;
        margin: 0 0 1.7rem;
        color: var(--muted);
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.13em;
        text-transform: uppercase;
      }

      .eyebrow span {
        width: 7px;
        height: 7px;
        display: inline-block;
        background: var(--acid);
        border-radius: 50%;
        box-shadow: 0 0 16px rgba(232, 239, 132, 0.48);
      }

      h1 {
        max-width: 760px;
        margin-bottom: 1.55rem;
        font-family: Georgia, "Times New Roman", serif;
        font-size: clamp(3rem, 6vw, 4.8rem);
        font-weight: 400;
        line-height: 0.95;
        letter-spacing: -0.05em;
      }

      h1 em {
        color: var(--acid);
        font-weight: 400;
      }

      .lede {
        max-width: 700px;
        margin-bottom: 2.4rem;
        color: var(--muted);
        font-size: clamp(1.04rem, 2vw, 1.2rem);
        line-height: 1.6;
      }

      .page-head {
        padding: 4.5rem 0 1rem;
      }

      .facts {
        margin: 0 0 2.6rem;
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 1px;
        overflow: hidden;
        background: var(--line);
        border: 1px solid var(--line);
        border-radius: var(--radius);
      }

      .facts > div {
        padding: 1.05rem 1.2rem 1.15rem;
        background: #1a1913;
      }

      .facts dt {
        margin-bottom: 0.4rem;
        color: var(--dim);
        font-size: 0.66rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }

      .facts dd {
        margin: 0;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.98rem;
      }

      .notes {
        display: grid;
        gap: 1.15rem;
        max-width: 780px;
      }

      .notes p {
        margin: 0;
        padding-left: 1.05rem;
        color: var(--muted);
        font-size: 0.94rem;
        border-left: 1px solid rgba(232, 239, 132, 0.45);
      }

      .notes strong {
        color: var(--ink);
        font-weight: 650;
      }

      .notes code {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.85em;
      }

      .finder {
        position: sticky;
        top: 0;
        z-index: 5;
        margin-top: 2.6rem;
        padding: 1.1rem 0 1rem;
        background: var(--paper);
        border-bottom: 1px solid var(--line);
      }

      .finder label {
        display: block;
        margin-bottom: 0.55rem;
        color: var(--dim);
        font-size: 0.66rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }

      .finder input {
        width: min(100%, 460px);
        padding: 0.7rem 0.9rem;
        color: var(--ink);
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 7px;
        font-family: inherit;
        font-size: 0.95rem;
      }

      .finder input:focus-visible {
        outline: 2px solid var(--acid);
        outline-offset: 1px;
      }

      .filter-status {
        margin: 0.6rem 0 0;
        color: var(--dim);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.72rem;
      }

      .brand-index {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        padding: 2.2rem 0 0;
      }

      .brand-index a {
        padding: 0.3rem 0.62rem;
        color: var(--muted);
        font-size: 0.72rem;
        font-weight: 600;
        border: 1px solid var(--line);
        border-radius: 999px;
        transition: color 150ms ease, background 150ms ease;
      }

      .brand-index a:hover,
      .brand-index a:focus-visible {
        color: #11120b;
        background: var(--acid);
        border-color: var(--acid);
      }

      .camera-list {
        padding: 2.4rem 0 1rem;
      }

      .brand {
        padding: 2.1rem 0;
        border-top: 1px solid var(--line);
      }

      .brand h2 {
        display: flex;
        align-items: baseline;
        gap: 0.75rem;
        margin-bottom: 1.15rem;
        font-family: Georgia, "Times New Roman", serif;
        font-size: 1.85rem;
        font-weight: 400;
        letter-spacing: -0.035em;
      }

      .brand-count {
        color: var(--dim);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.68rem;
        letter-spacing: 0.08em;
      }

      /* Columns rather than a grid so a sorted list reads down each column
         instead of across the rows. */
      .brand ul {
        margin: 0;
        padding: 0;
        list-style: none;
        columns: 4 232px;
        column-gap: 1.7rem;
      }

      .camera {
        padding: 0.12rem 0;
        color: var(--muted);
        font-size: 0.88rem;
        line-height: 1.5;
        break-inside: avoid;
      }

      .no-matches {
        padding: 3.5rem 0 4rem;
        color: var(--muted);
        border-top: 1px solid var(--line);
      }

      footer {
        min-height: 150px;
        display: grid;
        grid-template-columns: 1fr 1fr auto;
        gap: 1rem;
        align-items: center;
        color: var(--dim);
        font-size: 0.74rem;
        border-top: 1px solid var(--line);
      }

      footer > span:first-child {
        color: var(--ink);
        font-weight: 650;
      }

      footer a {
        color: var(--muted);
      }

      @media (max-width: 900px) {
        .facts {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }
      }

      @media (max-width: 620px) {
        .site-header,
        main,
        footer {
          width: min(calc(100% - 30px), var(--max));
        }

        .site-header {
          min-height: 76px;
        }

        .site-header nav a:not(.nav-download) {
          display: none;
        }

        .page-head {
          padding-top: 3rem;
        }

        .facts {
          grid-template-columns: 1fr;
        }

        .brand ul {
          columns: 1;
        }

        footer {
          min-height: 180px;
          grid-template-columns: 1fr;
          align-content: center;
          gap: 0.35rem;
        }
      }

      @media (prefers-reduced-motion: reduce) {
        html {
          scroll-behavior: auto;
        }

        *,
        *::before,
        *::after {
          transition-duration: 0.01ms !important;
        }
      }
"""

# Plain DOM work, inline, so the page keeps its promise of no fetches.  Every
# token of the query has to appear somewhere in "manufacturer model", which is
# what makes "canon r5" find "Canon EOS R5".
PAGE_JS = """
      (function () {
        var input = document.getElementById("camera-filter");
        var status = document.getElementById("filter-status");
        var empty = document.getElementById("no-matches");
        var chips = {};
        var total = 0;
        var groups = [];

        Array.prototype.forEach.call(
          document.querySelectorAll(".brand-index a"),
          function (chip) { chips[chip.getAttribute("data-jump")] = chip; });

        Array.prototype.forEach.call(
          document.querySelectorAll(".brand"),
          function (section) {
            var maker = section.getAttribute("data-manufacturer");
            var items = Array.prototype.map.call(
              section.querySelectorAll(".camera"),
              function (node) {
                total += 1;
                return {
                  node: node,
                  text: (maker + " " + node.textContent).toLowerCase()
                };
              });
            groups.push({ section: section, chip: chips[maker], items: items });
          });

        function apply() {
          var query = input.value.trim().toLowerCase();
          var terms = query ? query.split(/\\s+/) : [];
          var shown = 0;
          groups.forEach(function (group) {
            var visible = 0;
            group.items.forEach(function (item) {
              var match = terms.every(function (term) {
                return item.text.indexOf(term) !== -1;
              });
              item.node.hidden = !match;
              if (match) { visible += 1; }
            });
            group.section.hidden = visible === 0;
            if (group.chip) { group.chip.hidden = visible === 0; }
            shown += visible;
          });
          empty.hidden = shown !== 0;
          status.textContent = terms.length
            ? shown + " of " + total + " cameras match \\"" + input.value.trim() + "\\""
            : total + " camera models, grouped by manufacturer.";
        }

        input.addEventListener("input", apply);
        apply();
      })();
"""


def _long_date(day: datetime.date) -> str:
    return f"{day.day} {day.strftime('%B %Y')}"


def render_page(info: CameraList, generated: datetime.date,
                extensions: tuple[str, ...] = ()) -> str:
    """Return the complete supported-camera page as self-contained HTML."""
    groups = group_cameras(info.cameras)
    version = html.escape(info.version, quote=False)
    version_attr = html.escape(info.version, quote=True)
    count = f"{info.count:,}"
    made = _long_date(generated)

    if extensions:
        suffixes = " ".join(f".{value}" for value in extensions)
        extension_note = (
            f"LightTable opens files whose suffix it already recognises "
            f"(<code>{html.escape(suffixes, quote=False)}</code>), so a camera "
            f"listed here that writes some other suffix is passed over when a "
            f"folder is scanned until that suffix is added to the app.")
    else:
        extension_note = (
            "LightTable opens files whose suffix it already recognises, so a "
            "camera listed here that writes some other suffix is passed over "
            "when a folder is scanned until that suffix is added to the app.")

    out: list[str] = []
    add = out.append
    add('<!doctype html>')
    add('<html lang="en">')
    add('  <head>')
    add('    <meta charset="utf-8" />')
    add('    <meta name="viewport" content="width=device-width, initial-scale=1" />')
    add('    <meta')
    add('      name="description"')
    add(f'      content="The {count} camera models LightTable can decode, read '
        f'from LibRaw {version_attr}."')
    add('    />')
    add('    <meta name="theme-color" content="#15140f" />')
    add(f'    <meta name="{VERSION_META}" content="{version_attr}" />')
    add(f'    <meta name="{COUNT_META}" content="{info.count}" />')
    add(f'    <meta name="{GENERATED_META}" content="{generated.isoformat()}" />')
    add('    <title>Supported cameras — LightTable</title>')
    add('    <link rel="icon" href="icon.png" />')
    add('    <style>' + PAGE_CSS.rstrip() + '\n    </style>')
    add('  </head>')
    add('  <body>')
    add('    <a class="skip-link" href="#main">Skip to content</a>')
    add('')
    add('    <header class="site-header">')
    add('      <a class="wordmark" href="index.html" aria-label="LightTable home">')
    add('        <img src="icon.png" alt="" width="36" height="36" />')
    add('        <span>LightTable</span>')
    add('      </a>')
    add('      <nav aria-label="Main navigation">')
    add('        <a href="index.html#process">Process</a>')
    add('        <a href="cameras.html" aria-current="page">Cameras</a>')
    add('        <a class="nav-download" href="index.html#download">Download</a>')
    add('      </nav>')
    add('    </header>')
    add('')
    add('    <main id="main">')
    add('      <section class="page-head">')
    add(f'        <p class="eyebrow"><span></span> Camera support · LibRaw {version}</p>')
    add('        <h1>Every camera the decoder <em>knows.</em></h1>')
    add('        <p class="lede">')
    add('          LightTable reads RAW files with LibRaw. These are the')
    add(f'          {count} camera models that LibRaw {version} recognises,')
    add('          exactly as the library itself reports them.')
    add('        </p>')
    add('        <dl class="facts">')
    add(f'          <div><dt>LibRaw</dt><dd>{version}</dd></div>')
    add(f'          <div><dt>Camera models</dt><dd>{count}</dd></div>')
    add(f'          <div><dt>Manufacturers</dt><dd>{len(groups)}</dd></div>')
    add(f'          <div><dt>Generated</dt><dd>{made}</dd></div>')
    add('        </dl>')
    add('        <div class="notes">')
    add('          <p>')
    add('            <strong>Being listed here is necessary, not sufficient.</strong>')
    add(f'            {extension_note}')
    add('          </p>')
    add('          <p>')
    add('            <strong>Not on the list? Convert to DNG.</strong> DNG is an')
    add('            openly documented RAW container that LibRaw reads whichever')
    add('            camera produced it. Files from a camera this build does not')
    add('            know can usually be brought in by converting them to DNG')
    add('            first, with the camera maker\'s own utility or with Adobe\'s')
    add('            free DNG converter, and LightTable then treats the result as')
    add('            an ordinary RAW file.')
    add('          </p>')
    add('          <p>')
    add('            <strong>This page is generated, not maintained.</strong>')
    add('            <code>scripts/camera-list.py</code> asks the LibRaw binary')
    add('            the app is linked against for its own list, so the page')
    add('            cannot claim support the shipped build does not have.')
    add('          </p>')
    add('        </div>')
    add('        <nav class="brand-index" aria-label="Jump to a manufacturer">')
    for group in groups:
        name = html.escape(group.name, quote=False)
        add(f'          <a href="#{slug(group.name)}" '
            f'data-jump="{html.escape(group.name, quote=True)}">{name}</a>')
    add('        </nav>')
    add('      </section>')
    add('')
    add('      <section class="finder" aria-label="Filter the camera list">')
    add('        <label for="camera-filter">Filter</label>')
    add('        <input')
    add('          id="camera-filter"')
    add('          type="search"')
    add('          autocomplete="off"')
    add('          spellcheck="false"')
    add('          placeholder="Camera or maker — try “X-T5”, “canon r5”, “Hasselblad”"')
    add('        />')
    add('        <p class="filter-status" id="filter-status" role="status">')
    add(f'          {info.count} camera models, grouped by manufacturer.')
    add('        </p>')
    add('      </section>')
    add('')
    add('      <div class="camera-list">')
    for group in groups:
        anchor = slug(group.name)
        name = html.escape(group.name, quote=False)
        add(f'        <section class="brand" id="{anchor}"')
        add(f'          data-manufacturer="{html.escape(group.name, quote=True)}"')
        add(f'          aria-labelledby="{anchor}-title">')
        add(f'          <h2 id="{anchor}-title">{name}')
        add(f'            <span class="brand-count">{len(group.models)}</span>')
        add('          </h2>')
        add('          <ul>')
        for model in group.models:
            add(f'            <li class="camera">'
                f'{html.escape(model, quote=False)}</li>')
        add('          </ul>')
        add('        </section>')
    add('      </div>')
    add('')
    add('      <p class="no-matches" id="no-matches" hidden>')
    add('        No camera matches that filter. Try a shorter one — the list')
    add('        holds the maker and the model exactly as LibRaw spells them.')
    add('      </p>')
    add('    </main>')
    add('')
    add('    <footer>')
    add('      <span>LightTable</span>')
    add(f'      <span>Camera support from LibRaw {version} · generated {made}</span>')
    add('      <a href="index.html">Back to the front page</a>')
    add('    </footer>')
    add('    <script>' + PAGE_JS.rstrip() + '\n    </script>')
    add('  </body>')
    add('</html>')
    return "\n".join(out) + "\n"


class PageReader(HTMLParser):
    """Read back what a published cameras.html claims, for --check."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.version: str | None = None
        self.count: str | None = None
        self.generated: str | None = None
        self.entries: list[str] = []
        self._maker: str | None = None
        self._model: list[str] | None = None

    def handle_starttag(self, tag, attrs) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "meta":
            name = values.get("name")
            if name == VERSION_META:
                self.version = values.get("content")
            elif name == COUNT_META:
                self.count = values.get("content")
            elif name == GENERATED_META:
                self.generated = values.get("content")
        elif tag == "section" and "brand" in classes:
            self._maker = values.get("data-manufacturer")
        elif tag == "li" and "camera" in classes:
            self._model = []

    def handle_data(self, data) -> None:
        if self._model is not None:
            self._model.append(data)

    def handle_endtag(self, tag) -> None:
        if tag == "li" and self._model is not None:
            model = " ".join("".join(self._model).split())
            if self._maker and model:
                self.entries.append(f"{self._maker} {model}")
            self._model = None
        elif tag == "section":
            self._maker = None


def published_entries(info: CameraList) -> list[str]:
    """The "Manufacturer Model" strings the page should be listing."""
    return [f"{group.name} {model}"
            for group in group_cameras(info.cameras)
            for model in group.models]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def check_page(info: CameraList, output: Path,
               stream=None, limit: int = 12) -> int:
    """Compare a generated page with the library. 0 if it is current."""
    # Resolved here rather than in the signature so a caller that redirects
    # stdout, a test among them, actually captures what this prints.
    stream = sys.stdout if stream is None else stream
    if not output.is_file():
        print(f"{output} does not exist; run scripts/camera-list.py",
              file=stream)
        return 1
    markup = output.read_text(encoding="utf-8")
    reader = PageReader()
    reader.feed(markup)
    expected = published_entries(info)
    added = sorted(set(expected) - set(reader.entries))
    removed = sorted(set(reader.entries) - set(expected))
    stale_version = reader.version != info.version
    # Re-rendered against the date the page itself claims, so the only
    # differences left are real ones: the accepted suffixes, the wording, or
    # the template. Without this a hand-edited page would pass.
    try:
        generated = datetime.date.fromisoformat(reader.generated or "")
    except (TypeError, ValueError):
        generated = datetime.date.today()
    rewritten = markup != render_page(info, generated, accepted_extensions())

    if not (added or removed or stale_version or rewritten):
        print(f"{output} is current: LibRaw {info.version}, "
              f"{info.count} cameras", file=stream)
        return 0

    print(f"{output} is out of date; run scripts/camera-list.py", file=stream)
    if stale_version:
        print(f"  LibRaw on the page: {reader.version or 'not stated'}",
              file=stream)
        print(f"  LibRaw linked here: {info.version}", file=stream)
    print(f"  {len(added)} camera(s) added, {len(removed)} removed "
          f"({len(reader.entries)} on the page, {len(expected)} in the library)",
          file=stream)
    for symbol, names in (("+", added), ("-", removed)):
        for name in names[:limit]:
            print(f"  {symbol} {name}", file=stream)
        if len(names) > limit:
            print(f"  {symbol} ... and {len(names) - limit} more", file=stream)
    if rewritten and not (added or removed or stale_version):
        print("  the camera list still matches, but the page text does not: "
              "the accepted suffixes, the wording, or the template changed",
              file=stream)
    return 1


def dump_json(info: CameraList, destination: Path) -> None:
    """Write the list as it came out of the library, plus its provenance."""
    payload = {
        "librawVersion": info.version,
        "librawVersionNumber": info.version_number,
        "library": str(info.library),
        "cameraCount": info.count,
        "cameras": list(info.cameras),
        "manufacturers": {group.name: list(group.models)
                          for group in group_cameras(info.cameras)},
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2) + "\n",
                           encoding="utf-8")


def write_page(info: CameraList, output: Path,
               generated: datetime.date | None = None) -> None:
    page = render_page(info, generated or datetime.date.today(),
                       accepted_extensions())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a supported-camera page from the linked LibRaw.")
    parser.add_argument(
        "--check", action="store_true",
        help="do not write; exit non-zero if the generated page is stale")
    parser.add_argument(
        "--json", type=Path, metavar="PATH",
        help="dump the raw camera list as JSON and write no page")
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, metavar="PATH",
        help=("page to write or check (required unless LIGHTTABLE_SITE_ROOT "
              "is set)"))
    parser.add_argument(
        "--library", type=Path, default=None, metavar="PATH",
        help="read a specific LibRaw instead of the one rawpy bundles")
    args = parser.parse_args(argv)

    if args.output is None and not args.json:
        parser.error(
            "--output is required unless LIGHTTABLE_SITE_ROOT is set")

    try:
        info = load_libraw(args.library)
    except LibRawUnavailable as error:
        print(f"camera-list: {error}", file=sys.stderr)
        return 2

    if args.json:
        dump_json(info, args.json)
        print(f"wrote {args.json}: {info.count} cameras from "
              f"LibRaw {info.version}")
    if args.check:
        return check_page(info, args.output)
    if args.json:
        return 0

    write_page(info, args.output)
    print(f"wrote {args.output}: {info.count} cameras, "
          f"{len(group_cameras(info.cameras))} manufacturers, "
          f"LibRaw {info.version} ({info.library})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
