"""Per-image XMP reading for LightTable.

Lightroom, Camera Raw, Bridge, and the open-source raw editors record what
they know about a photo either in a sidecar next to it (``photo.xmp`` or
``photo.CR2.xmp``) or in an XMP packet inside the file.  Both carry the same
RDF payload in one of two shapes: properties as attributes of an
``rdf:Description``, or properties as child elements of it.  This module reads
both with a real XML parser and returns one normalised dict, so the importer
never has to guess at a regular expression.

Namespace prefixes are writer-defined, so every lookup here matches on the
namespace URI instead.  Develop settings are handed to
:func:`preset_io.map_crs_settings`, the same conversion the preset importer
uses, so a sidecar and a preset carrying identical ``crs`` values produce
identical grades.
"""
from __future__ import annotations

from server_localization import T

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom, Node
from pathlib import Path

import durable_io

# Namespaces this module understands, keyed by the prefix used in the parsed
# output.  The prefix in the document itself is irrelevant.
NAMESPACES = {
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "digiKam": "http://www.digikam.org/ns/1.0/",
    "photomechanic": "http://ns.camerabits.com/photomechanic/1.0/",
    "lighttable": "https://lighttable.photo/ns/1.0/",
}
_PREFIX_BY_URI = {uri: prefix for prefix, uri in NAMESPACES.items()}

RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# A sidecar is a small text file; anything larger is not one and is refused
# rather than parsed, so a stray archive cannot be expanded in memory.
MAX_XMP_BYTES = 8 * 1024 * 1024

# The two conventional sidecar spellings differ only in case on the disks
# that care about it.
SIDECAR_SUFFIXES = (".xmp", ".XMP")

# Formats whose XMP lives inside the file rather than beside it.
EMBEDDED_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".heic", ".heif", ".dng",
})

# Lightroom's five colour labels.  Anything else is a user-named label and is
# kept as written.
LABEL_COLORS = ("red", "yellow", "green", "blue", "purple")
# digiKam's enum is distinct from Photo Mechanic's user-customizable classes.
_DIGIKAM_COLORS = ("none", "red", "orange", "yellow", "green", "blue",
                   "purple", "gray", "black", "white")
_DIGIKAM_STATUS = {0: "pending", 1: "skipped", 2: "pending", 3: "approved"}

_IPTC_PROPERTIES = {
    "title": ("dc", "title", "alt"),
    "caption": ("dc", "description", "alt"),
    "copyright": ("dc", "rights", "alt"),
    "creator": ("dc", "creator", "seq"),
    "headline": ("photoshop", "Headline", "attr"),
    "credit": ("photoshop", "Credit", "attr"),
    "city": ("photoshop", "City", "attr"),
    "state": ("photoshop", "State", "attr"),
    "country": ("photoshop", "Country", "attr"),
}
_NATIVE_FIELDS = ("params", "grade", "crop", "masks", "heals", "optics", "status", "rating")

# Placeholder for a nested crs structure (a look, a mask group) that has no
# scalar value.  It reads as "present but unsupported" to the crs mapper.
STRUCTURED = "(structured)"

MAX_CROP_ANGLE = 15.0
CROP_ANGLE_SKIPPED = "crop angle beyond supported range"

# Crop keys this module applies itself, so the develop mapper is not asked to
# report them as skipped geometry.
_CROP_KEYS = (
    "HasCrop", "CropTop", "CropLeft", "CropBottom", "CropRight", "CropAngle",
    "CropConstrainToWarp",
)


def _split(tag: str) -> tuple[str, str]:
    """Split an ElementTree ``{uri}local`` name into its parts."""
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        return uri, local
    return "", tag


def _number(value, default: float | None = None) -> float | None:
    try:
        number = float(str(value).strip().lstrip("+"))
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _flag(value) -> bool:
    return str(value or "").strip().casefold() in {"true", "1", "yes"}


def _container(element) -> tuple[str, list[str]] | None:
    """Return ``(kind, items)`` for an rdf:Bag/Seq/Alt child, else None."""
    for child in element:
        uri, kind = _split(child.tag)
        if uri != RDF_NS or kind not in ("Bag", "Seq", "Alt"):
            continue
        items: list[str] = []
        default = None
        for item in child:
            item_uri, item_local = _split(item.tag)
            if item_uri != RDF_NS or item_local != "li":
                continue
            text = (item.text or "").strip()
            if not text and len(item):
                text = STRUCTURED
            if item.get(f"{{{XML_NS}}}lang") == "x-default" and default is None:
                default = text
            if not text and item.get(f"{{{XML_NS}}}lang") != "x-default":
                continue
            items.append(text)
        if default is not None and items and items[0] != default:
            items.remove(default)
            items.insert(0, default)
        return kind, items
    return None


def _collect(root) -> tuple[dict, dict, dict]:
    """Read every known property from attribute and element form alike."""
    scalars: dict[tuple[str, str], str] = {}
    arrays: dict[tuple[str, str], list[str]] = {}
    kinds: dict[tuple[str, str], str] = {}

    holders = [element for element in root.iter()
               if _split(element.tag) == (RDF_NS, "Description")]
    primary = [element for element in holders
               if not element.get(f"{{{RDF_NS}}}about")]
    if primary:
        holders = primary
    if not holders:
        # Fragments and hand-written XMP sometimes omit rdf:Description; fall
        # back to treating every element as a possible property holder.
        holders = list(root.iter())

    for holder in holders:
        for name, value in holder.attrib.items():
            uri, local = _split(name)
            prefix = _PREFIX_BY_URI.get(uri)
            if prefix:
                scalars.setdefault((prefix, local), value)
        for child in holder:
            uri, local = _split(child.tag)
            prefix = _PREFIX_BY_URI.get(uri)
            if not prefix:
                continue
            container = _container(child)
            if container is not None:
                kind, items = container
                arrays.setdefault((prefix, local), items)
                kinds.setdefault((prefix, local), kind)
                continue
            if len(child):
                scalars.setdefault((prefix, local), STRUCTURED)
                continue
            text = (child.text or "").strip()
            scalars.setdefault((prefix, local), text)
    return scalars, arrays, kinds


def _first(arrays: dict, scalars: dict, prefix: str, local: str) -> str | None:
    items = arrays.get((prefix, local))
    if items:
        return items[0] or None
    value = scalars.get((prefix, local))
    return value.strip() or None if value else None


def _curve_points(items) -> list[tuple[float, float]]:
    """Read ``"128, 142"`` tone-curve entries into point pairs."""
    points = []
    for item in items or []:
        parts = [part for part in str(item).replace(",", " ").split() if part]
        if len(parts) < 2:
            continue
        x, y = _number(parts[0]), _number(parts[1])
        if x is not None and y is not None:
            points.append((x, y))
    return points


def _coordinate(value, reference=None, limit: float = 90.0) -> float | None:
    """Read a decimal or ``DDD,MM.mmk`` / ``DDD,MM,SSk`` GPS coordinate."""
    text = str(value or "").strip()
    if not text:
        return None
    hemisphere = ""
    if text[-1:].upper() in ("N", "S", "E", "W"):
        hemisphere, text = text[-1].upper(), text[:-1].strip()
    elif reference:
        hemisphere = str(reference).strip()[:1].upper()
    parts = [part for part in text.replace(";", ",").split(",") if part.strip()]
    numbers = [_number(part) for part in parts]
    if not numbers or any(number is None for number in numbers):
        return None
    degrees = abs(numbers[0])
    if len(numbers) > 1:
        degrees += numbers[1] / 60.0
    if len(numbers) > 2:
        degrees += numbers[2] / 3600.0
    if numbers[0] < 0 or hemisphere in ("S", "W"):
        degrees = -degrees
    if abs(degrees) > limit:
        return None
    return round(degrees, 7)


def _rational(value) -> float | None:
    text = str(value or "").strip()
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        top, bottom = _number(numerator), _number(denominator)
        if top is None or not bottom:
            return None
        return top / bottom
    return _number(text)


def _gps(scalars: dict) -> dict | None:
    latitude = _coordinate(scalars.get(("exif", "GPSLatitude")),
                           scalars.get(("exif", "GPSLatitudeRef")), 90.0)
    longitude = _coordinate(scalars.get(("exif", "GPSLongitude")),
                            scalars.get(("exif", "GPSLongitudeRef")), 180.0)
    if latitude is None or longitude is None:
        return None
    altitude = _rational(scalars.get(("exif", "GPSAltitude")))
    if altitude is not None and str(
            scalars.get(("exif", "GPSAltitudeRef")) or "").strip() == "1":
        altitude = -abs(altitude)
    return {"lat": latitude, "lon": longitude,
            "alt": None if altitude is None else round(altitude, 3)}


def _crop(crs: dict) -> dict | None:
    """Camera Raw crop fractions as the app's ``{x,y,w,h}`` rect."""
    if not _flag(crs.get("HasCrop")):
        return None
    left = _number(crs.get("CropLeft"), 0.0) or 0.0
    top = _number(crs.get("CropTop"), 0.0) or 0.0
    right = _number(crs.get("CropRight"), 1.0)
    bottom = _number(crs.get("CropBottom"), 1.0)
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


def _label(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    return lowered if lowered in LABEL_COLORS else text


def _rating(value) -> tuple[int | None, bool]:
    number = _number(value)
    if number is None or not math.isfinite(number):
        return None, False
    if number < 0:
        # Lightroom writes -1 for a rejected photo; LightTable keeps the
        # rejection as a status, never as a star count.
        return None, True
    return int(max(0, min(5, round(number)))), False


def _enum(value, choices):
    number = _number(value)
    if number is None or not math.isfinite(number) or number != int(number):
        return None
    return int(number) if int(number) in choices else None


def metadata_keywords(parsed: dict) -> list[str]:
    """Retain full hierarchy plus flat terms not already represented by it."""
    paths = list(dict.fromkeys(parsed.get("keywordPaths") or []))
    represented = {part.casefold() for path in paths for part in path.split("|")}
    represented.update(path.casefold() for path in paths)
    for keyword in parsed.get("keywords") or []:
        if keyword.casefold() not in represented:
            paths.append(keyword)
            represented.add(keyword.casefold())
    return [path.replace("|", " > ") for path in paths]


def parse(text: str) -> dict | None:
    """Read one XMP document into LightTable's normalised metadata dict.

    Returns None for anything that is not parseable XML, or is too large to
    be a sidecar.
    """
    if (not text or len(text.encode("utf-8")) > MAX_XMP_BYTES
            or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I)):
        return None
    body = text.lstrip("\ufeff \t\r\n")
    if body.startswith("<?xml"):
        # The text is already decoded, so an encoding declaration would only
        # make ElementTree refuse it.
        end = body.find("?>")
        if end < 0:
            return None
        body = body[end + 2:]
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError, TypeError):
        return None

    scalars, arrays, kinds = _collect(root)
    present = set(scalars) | set(arrays)

    crs: dict[str, str] = {}
    curves: dict[str, list[tuple[float, float]]] = {}
    for (prefix, local), value in scalars.items():
        if prefix == "crs":
            crs[local] = value
    for (prefix, local), items in arrays.items():
        if prefix != "crs":
            continue
        points = _curve_points(items) if local.startswith("ToneCurve") else []
        if len(points) >= 2:
            curves[local] = points
        elif items:
            crs[local] = (items[0] if kinds.get((prefix, local)) == "Alt"
                          else "; ".join(items))

    rating, rejected = _rating(scalars.get(("xmp", "Rating")))
    pick = _enum(scalars.get(("digiKam", "PickLabel")), _DIGIKAM_STATUS)
    status = _DIGIKAM_STATUS.get(pick)
    tagged = scalars.get(("photomechanic", "Tagged"))
    if status is None and tagged is not None:
        status = "approved" if _flag(tagged) else "pending"
    if rejected:
        status = "skipped"
        # Standard XMP uses the rating slot for rejection. Our own payload
        # retains the independent star count when both have been assigned.
        try:
            native = json.loads(scalars.get(("lighttable", "edit")) or "{}")
            if isinstance(native, dict) and "rating" in native:
                rating, _ = _rating(native["rating"])
        except (TypeError, ValueError):
            pass
    color = _enum(scalars.get(("digiKam", "ColorLabel")), range(10))
    label = _label(scalars.get(("xmp", "Label")))
    if label is None and color is not None:
        label = _DIGIKAM_COLORS[color]
    # digiKam's default mapping prefers its slash-separated path list.
    paths = arrays.get(("digiKam", "TagsList"))
    paths = ([path.replace("/", "|") for path in paths] if paths else
             list(arrays.get(("lr", "hierarchicalSubject")) or []))
    orientation = _number(scalars.get(("tiff", "Orientation")))
    angle = _number(crs.get("CropAngle"))
    parsed = {
        "rating": rating,
        "captureTime": scalars.get(("exif", "DateTimeOriginal")) or scalars.get(("photoshop", "DateCreated")),
        "rejected": status == "skipped",
        "status": status,
        "label": label,
        "keywords": list(arrays.get(("dc", "subject")) or []),
        "keywordPaths": paths,
        "title": _first(arrays, scalars, "dc", "title"),
        "caption": _first(arrays, scalars, "dc", "description"),
        "creator": _first(arrays, scalars, "dc", "creator"),
        "copyright": _first(arrays, scalars, "dc", "rights"),
        "gps": _gps(scalars),
        "orientation": None if orientation is None else int(orientation),
        "crop": _crop(crs),
        "cropAngle": angle,
        "crs": crs,
        "crsCurves": curves,
    }
    for field, tag in (("headline", "Headline"), ("credit", "Credit"),
                       ("city", "City"), ("state", "State"),
                       ("country", "Country")):
        value = scalars.get(("photoshop", tag))
        parsed[field] = value.strip() or None if value else None
    parsed["metadataKeywords"] = metadata_keywords(parsed)
    fields = []
    for field, properties in {
        "rating": [("xmp", "Rating")],
        "label": [("xmp", "Label"), ("digiKam", "ColorLabel")],
        "keywords": [("dc", "subject"), ("lr", "hierarchicalSubject"), ("digiKam", "TagsList")],
        "captureTimeOverride": [("exif", "DateTimeOriginal"), ("photoshop", "DateCreated")],
        **{key: [(prefix, name)] for key, (prefix, name, _) in _IPTC_PROPERTIES.items()},
    }.items():
        if any(prop in present for prop in properties):
            fields.append(field)
    if status is not None:
        fields.append("status")
    parsed["metadataPresent"] = fields
    return parsed


def sidecar_paths(source: Path) -> list[Path]:
    """Both conventional sidecar names: photo.xmp and photo.CR2.xmp."""
    source = Path(source)
    replaced = source.with_name(f"{source.stem}{SIDECAR_SUFFIXES[0]}")
    appended = source.with_name(f"{source.name}{SIDECAR_SUFFIXES[0]}")
    return [replaced] if replaced == appended else [replaced, appended]


def find_sidecars(source: Path) -> list[Path]:
    """Existing conventional names, deduplicated on case-insensitive disks."""
    found = []
    identities = set()
    for candidate in sidecar_paths(source):
        for suffix in SIDECAR_SUFFIXES:
            path = candidate.with_suffix(suffix)
            try:
                if path.is_file():
                    stat = path.stat()
                    identity = (stat.st_dev, stat.st_ino)
                    if identity not in identities:
                        found.append(path)
                        identities.add(identity)
            except OSError:
                continue
    return found


def find_sidecar(source: Path) -> Path | None:
    """The first sidecar on disk for one image, in Adobe's naming order."""
    return next(iter(find_sidecars(source)), None)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def read_sidecar(source: Path) -> dict | None:
    """Parse the sidecar beside one image, if there is one."""
    paths = find_sidecars(source)
    if not paths:
        return None
    path = paths[0]
    try:
        if path.stat().st_size > MAX_XMP_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    parsed = parse(_decode(data))
    if parsed is not None:
        parsed["origin"] = "sidecar"
        parsed["path"] = str(path)
        if len(paths) > 1:
            parsed["sidecarConflicts"] = [str(candidate) for candidate in paths]
    return parsed


def _document_from_datums(data) -> str:
    """Rebuild an XMP document from exiv2 datums when no packet is stored."""
    rdf = ET.Element(f"{{{RDF_NS}}}RDF")
    description = ET.SubElement(rdf, f"{{{RDF_NS}}}Description")
    for datum in data:
        uri = NAMESPACES.get(datum.groupName())
        if not uri:
            continue
        node = ET.SubElement(description, f"{{{uri}}}{datum.tagName()}")
        type_name = datum.typeName() or ""
        text = datum.toString() or ""
        if type_name in ("XmpBag", "XmpSeq"):
            kind = "Bag" if type_name == "XmpBag" else "Seq"
            container = ET.SubElement(node, f"{{{RDF_NS}}}{kind}")
            for item in text.split(","):
                if item.strip():
                    ET.SubElement(container,
                                  f"{{{RDF_NS}}}li").text = item.strip()
        elif type_name == "LangAlt":
            alt = ET.SubElement(node, f"{{{RDF_NS}}}Alt")
            item = ET.SubElement(alt, f"{{{RDF_NS}}}li")
            item.set(f"{{{XML_NS}}}lang", "x-default")
            item.text = re.sub(r'^lang="[^"]*"\s*', "", text)
        else:
            node.text = text
    return ET.tostring(rdf, encoding="unicode")


def read_embedded(source: Path) -> dict | None:
    """Parse the XMP packet inside a JPEG, TIFF, HEIC, or DNG."""
    source = Path(source)
    if source.suffix.casefold() not in EMBEDDED_SUFFIXES:
        return None
    try:
        import exiv2

        image = exiv2.ImageFactory.open(str(source))
        image.readMetadata()
        data = image.xmpData()
        packet = image.xmpPacket() or ""
        if not packet and data.count():
            packet = data.xmpPacket() or _document_from_datums(data)
    except Exception:  # noqa: BLE001 - unreadable metadata is not an error
        return None
    if not packet:
        return None
    parsed = parse(packet)
    if parsed is not None:
        parsed["origin"] = "embedded"
        parsed["path"] = str(source)
    return parsed


def read_for(source: Path) -> dict | None:
    """Metadata for one image: its sidecar wins, then its embedded packet."""
    parsed = read_sidecar(source)
    return parsed if parsed is not None else read_embedded(source)


def as_edit_patch(parsed: dict) -> dict:
    """Convert parsed XMP into the edit fields LightTable stores.

    Crop fractions map straight through; a straighten angle inside the app's
    range becomes ``optics.rotate`` and is reported as skipped outside it.
    """
    # Imported here so reading metadata does not pull in numpy.
    import preset_io

    parsed = parsed or {}
    crs = dict(parsed.get("crs") or {})
    crop = parsed.get("crop")
    angle = parsed.get("cropAngle")
    # Crop and straightening are applied below, so the develop mapper should
    # not also report them as skipped geometry.
    for key in _CROP_KEYS:
        crs.pop(key, None)
    curves = {
        key: preset_io._curve_lut(points)
        for key, points in (parsed.get("crsCurves") or {}).items()
    }
    converted = preset_io.map_crs_settings(crs, curves)

    optics: dict[str, float] = {}
    ignored = list(converted["ignored"])
    if angle is not None:
        if abs(angle) > MAX_CROP_ANGLE:
            ignored.append(CROP_ANGLE_SKIPPED)
        elif abs(angle) > 1e-6:
            optics["rotate"] = round(float(angle), 4)
    mapped = converted["mapped"] + (1 if crop else 0) + len(optics)
    return {
        "grade": converted["grade"],
        "crop": crop,
        "optics": optics,
        "ignored": sorted(set(ignored)),
        "mapped": mapped,
    }


# --------------------------------------------------------------- writing --

_XMP_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="LightTable">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
   xmlns:xmp="http://ns.adobe.com/xap/1.0/"
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:lr="http://ns.adobe.com/lightroom/1.0/"
   xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
   xmlns:exif="http://ns.adobe.com/exif/1.0/"
   xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   xmlns:digiKam="http://www.digikam.org/ns/1.0/"
   xmlns:photomechanic="http://ns.camerabits.com/photomechanic/1.0/"
   xmlns:lighttable="https://lighttable.photo/ns/1.0/"
{attributes}>
{elements}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""

# Grade keys that have a defensible Camera Raw counterpart, with the scale the
# importer already uses in reverse. Anything absent here is written only into
# the LightTable namespace, never approximated into a `crs:` key it does not
# really match.
_CRS_EXPORT = {
    "exposure": ("Exposure2012", 1.0),
    "contrast": ("Contrast2012", 100.0),
    "highlights": ("Highlights2012", 100.0),
    "shadows": ("Shadows2012", 100.0),
    "whites": ("Whites2012", 100.0),
    "blacks": ("Blacks2012", 100.0),
    "texture": ("Texture", 100.0),
    "clarity": ("Clarity2012", 100.0),
    "dehaze": ("Dehaze", 100.0),
    "vibrance": ("Vibrance", 100.0),
    "saturation": ("Saturation", 100.0),
}


def _escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _bag(prefix: str, local: str, values) -> str:
    items = "".join(
        f"     <rdf:li>{_escape(item)}</rdf:li>\n" for item in values)
    return (f"   <{prefix}:{local}>\n    <rdf:Bag>\n{items}"
            f"    </rdf:Bag>\n   </{prefix}:{local}>")


def _alt(prefix: str, local: str, value: str) -> str:
    return (f"   <{prefix}:{local}>\n    <rdf:Alt>\n"
            f'     <rdf:li xml:lang="x-default">{_escape(value)}</rdf:li>\n'
            f"    </rdf:Alt>\n   </{prefix}:{local}>")


def _seq(prefix: str, local: str, values) -> str:
    items = "".join(
        f"     <rdf:li>{_escape(item)}</rdf:li>\n" for item in values)
    return (f"   <{prefix}:{local}>\n    <rdf:Seq>\n{items}"
            f"    </rdf:Seq>\n   </{prefix}:{local}>")


def build_sidecar(record: dict) -> str:
    """Render one photo's catalog record as an XMP document.

    Two audiences share the file. `xmp:`, `dc:`, `lr:`, and `photoshop:` carry
    rating, label, keywords, and rights, which every other editor reads. A
    small `crs:` block carries the grade values that have a real counterpart,
    marked in the notes as approximate. Everything LightTable owns outright —
    the film pipeline, masks, healing, lens geometry — goes into its own
    namespace as JSON rather than being bent into a `crs:` key that means
    something else somewhere else.
    """
    attributes: list[str] = []
    elements: list[str] = []

    rating = -1 if record.get("status") == "skipped" else record.get("rating")
    if rating or "rating" in record:
        attributes.append(f'   xmp:Rating="{int(rating or 0)}"')
    label = record.get("label")
    if label and label != "none":
        text = label.title() if label in LABEL_COLORS else label
        attributes.append(f'   xmp:Label="{_escape(text)}"')
    elif "label" in record:
        attributes.append('   xmp:Label=""')
    if label in _DIGIKAM_COLORS:
        attributes.append(f'   digiKam:ColorLabel="{_DIGIKAM_COLORS.index(label)}"')
    status = record.get("status")
    if status in ("pending", "approved", "skipped"):
        pick = {"pending": 0, "skipped": 1, "approved": 3}[status]
        attributes.append(f'   digiKam:PickLabel="{pick}"')
        attributes.append(f'   photomechanic:Tagged="{"True" if status == "approved" else "False"}"')
    attributes.append('   xmp:CreatorTool="LightTable"')
    if record.get("captureTimeOverride"):
        import capture_time
        try:
            stamp = capture_time.normalized_timestamp(record["captureTimeOverride"])
            attributes.append(f'   exif:DateTimeOriginal="{_escape(stamp)}"')
            attributes.append(f'   photoshop:DateCreated="{_escape(stamp)}"')
        except ValueError:
            pass

    grade = record.get("grade") or {}
    for key, (crs_name, scale) in _CRS_EXPORT.items():
        value = grade.get(key)
        if value is None or abs(float(value)) < 1e-6:
            continue
        scaled = float(value) * scale
        text = (f"{scaled:+.2f}" if crs_name.startswith("Exposure")
                else f"{scaled:+.0f}")
        attributes.append(f'   crs:{crs_name}="{text}"')

    crop = record.get("crop")
    if crop:
        attributes.append('   crs:HasCrop="True"')
        attributes.append(f'   crs:CropLeft="{crop["x"]:.6f}"')
        attributes.append(f'   crs:CropTop="{crop["y"]:.6f}"')
        attributes.append(f'   crs:CropRight="{crop["x"] + crop["w"]:.6f}"')
        attributes.append(f'   crs:CropBottom="{crop["y"] + crop["h"]:.6f}"')

    keywords = record.get("keywords") or []
    if "keywords" in record:
        paths = list(dict.fromkeys(path.replace(" > ", "|") for path in keywords))
        flat = [path.rsplit("|", 1)[-1] for path in paths]
        elements.append(_bag("dc", "subject", dict.fromkeys(flat)))
        elements.append(_bag("lr", "hierarchicalSubject", paths))
        elements.append(_seq("digiKam", "TagsList",
                             [path.replace("|", "/") for path in paths]))

    iptc = record.get("iptc") or {}
    for key, (prefix, local, kind) in _IPTC_PROPERTIES.items():
        value = iptc.get(key)
        if key not in iptc:
            continue
        value = value or ""
        prefix_name, local_name, kind_name = prefix, local, kind
        if kind_name == "alt":
            elements.append(_alt(prefix_name, local_name, value))
        elif kind_name == "seq":
            elements.append(_seq(prefix_name, local_name, [value]))
        else:
            attributes.append(
                f'   {prefix_name}:{local_name}="{_escape(value)}"')

    native = {key: record.get(key) for key in _NATIVE_FIELDS
              if record.get(key) not in (None, [], {})}
    if native:
        payload = _escape(json.dumps(native, separators=(",", ":")))
        attributes.append(f'   lighttable:edit="{payload}"')
        attributes.append('   lighttable:note="crs values are approximate; '
                          'the lighttable:edit payload is authoritative"')

    return _XMP_TEMPLATE.format(
        attributes="\n".join(attributes),
        elements="\n".join(elements) if elements else "")


def _owned_properties(record: dict) -> set[tuple[str, str]]:
    """Only explicitly supplied fields are ours to update."""
    owned = {(NAMESPACES["xmp"], "CreatorTool")}
    for key, properties in {
        "rating": [("xmp", "Rating"), ("photomechanic", "Prefs")],
        "status": [("xmp", "Rating"), ("digiKam", "PickLabel"),
                   ("photomechanic", "Tagged"), ("photomechanic", "Prefs")],
        "label": [("xmp", "Label"), ("digiKam", "ColorLabel")],
        "keywords": [("dc", "subject"), ("lr", "hierarchicalSubject"),
                     ("digiKam", "TagsList")],
        "grade": [("crs", name) for name, _ in _CRS_EXPORT.values()],
        "crop": [("crs", name) for name in
                 ("HasCrop", "CropLeft", "CropTop", "CropRight", "CropBottom")],
        "captureTimeOverride": [("exif", "DateTimeOriginal"),
                                ("photoshop", "DateCreated")],
    }.items():
        if key in record:
            owned.update((NAMESPACES[prefix], name) for prefix, name in properties)
    for key in record.get("iptc") or {}:
        if key in _IPTC_PROPERTIES:
            prefix, name, _ = _IPTC_PROPERTIES[key]
            owned.add((NAMESPACES[prefix], name))
    native_uri = "https://lighttable.photo/ns/1.0/"
    if any(key in record for key in _NATIVE_FIELDS):
        owned.update((native_uri, name) for name in ("edit", "note"))
    return owned


def _preserve_alternatives(document, holders, fresh, record):
    """Editing x-default must leave translations of that field recoverable."""
    for key in record.get("iptc") or {}:
        field = _IPTC_PROPERTIES.get(key)
        if not field or field[2] != "alt":
            continue
        prefix, name, _ = field
        uri = NAMESPACES[prefix]
        translations = {}
        for holder in holders:
            for node in holder.childNodes:
                if (node.namespaceURI, node.localName) != (uri, name):
                    continue
                for alt in node.childNodes:
                    if (alt.namespaceURI, alt.localName) != (RDF_NS, "Alt"):
                        continue
                    for item in alt.childNodes:
                        if (item.namespaceURI, item.localName) != (RDF_NS, "li"):
                            continue
                        language = item.getAttributeNS(XML_NS, "lang")
                        if language and language != "x-default":
                            translations.setdefault(language, item)
        if not translations:
            continue
        properties = fresh.getElementsByTagNameNS(uri, name)
        if properties:
            prop = properties[0]
            alt = prop.getElementsByTagNameNS(RDF_NS, "Alt")[0]
        else:
            prop = document.createElementNS(uri, f"{prefix}:{name}")
            alt = document.createElementNS(RDF_NS, "rdf:Alt")
            prop.appendChild(alt)
            fresh.appendChild(prop)
            # An explicit empty default distinguishes a clear from a missing
            # default, which otherwise falls back to a retained translation.
            item = document.createElementNS(RDF_NS, "rdf:li")
            item.setAttributeNS(XML_NS, "xml:lang", "x-default")
            alt.appendChild(item)
        for item in translations.values():
            clone = document.importNode(item, deep=True)
            # A qualifier may use a prefix declared on an old ancestor that
            # disappears when the owned property is replaced.
            ancestor = item
            while ancestor is not None and ancestor.nodeType == Node.ELEMENT_NODE:
                for attr in ancestor.attributes.values():
                    if attr.namespaceURI == "http://www.w3.org/2000/xmlns/" and not clone.hasAttribute(attr.name):
                        clone.setAttribute(attr.name, attr.value)
                ancestor = ancestor.parentNode
            alt.appendChild(clone)


def merge_sidecar(existing: str, record: dict) -> str:
    """Update our properties, retaining the rest of an editor's RDF document.

    Work with namespace-aware DOM nodes so foreign prefixes, declarations,
    structured masks, qualifiers and unrelated RDF subjects survive intact.
    Refuse malformed files instead of replacing work we cannot understand.
    """
    if len(existing.encode("utf-8")) > MAX_XMP_BYTES:
        raise ValueError(T("existing XMP is too large to update safely"))
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", existing, re.I):
        raise ValueError(T("XMP declarations cannot be updated safely"))
    document = minidom.parseString(existing)
    scalars, _, _ = _collect(ET.fromstring(existing))
    supplied = record
    if "rating" in record and "status" not in record:
        # Stars and rejection are independent in LightTable. A star edit
        # must not silently turn a rejected photo into an unflagged photo.
        if (parse(existing) or {}).get("status") == "skipped":
            record = dict(record, status="skipped")
    if "status" in record and "rating" not in record:
        rating = (parse(existing) or {}).get("rating")
        if rating is not None:
            record = dict(record, rating=rating)
    generated = minidom.parseString(build_sidecar(record))
    rdf_nodes = document.getElementsByTagNameNS(RDF_NS, "RDF")
    if not rdf_nodes:
        raise ValueError(T("existing XMP has no RDF metadata document"))
    rdf = rdf_nodes[0]
    holders = [node for node in rdf.childNodes
               if node.nodeType == Node.ELEMENT_NODE
               and node.namespaceURI == RDF_NS
               and node.localName == "Description"
               and not node.getAttributeNS(RDF_NS, "about")]
    fresh = generated.getElementsByTagNameNS(RDF_NS, "Description")[0]
    if not holders:
        holder = document.createElementNS(RDF_NS, "rdf:Description")
        holder.setAttribute("xmlns:rdf", RDF_NS)
        holder.setAttributeNS(RDF_NS, "rdf:about", "")
        rdf.appendChild(holder)
        holders.append(holder)
    owned = _owned_properties(record)
    if any(key in supplied for key in _NATIVE_FIELDS):
        payload = scalars.get(("lighttable", "edit")) or "{}"
        try:
            native = json.loads(payload)
        except (TypeError, ValueError) as error:
            raise ValueError(T("Existing LightTable edits could not be merged safely")) from error
        if not isinstance(native, dict):
            raise ValueError(T("Existing LightTable edits could not be merged safely"))
        # Keep unknown future keys and explicit clears. Do not feed these
        # inherited fields to build_sidecar, which would also export them to
        # Camera Raw properties the user did not request to change.
        native.update({key: record[key] for key in _NATIVE_FIELDS if key in record})
        fresh.setAttributeNS(NAMESPACES["lighttable"], "lighttable:edit",
                             json.dumps(native, separators=(",", ":")))
    _preserve_alternatives(generated, holders, fresh, record)
    # Photo Mechanic also caches tag and rating in a colon-separated field.
    # Preserve its arbitrary color class and frame number while keeping the
    # culling values consistent with the standard properties we change.
    prefs = scalars.get(("photomechanic", "Prefs"))
    if prefs and ("rating" in record or "status" in record):
        values = prefs.split(":", 3)
        if len(values) != 4 or not all(value.strip().isdigit() for value in values[:3]):
            raise ValueError(T("Photo Mechanic preferences could not be updated safely"))
        if "status" in record:
            values[0] = "1" if record["status"] == "approved" else "0"
        if "rating" in record:
            values[2] = str(int(record["rating"] or 0))
        fresh.setAttributeNS(NAMESPACES["photomechanic"], "photomechanic:Prefs", ":".join(values))
    for holder in holders:
        for attribute in list(holder.attributes.values()):
            if (attribute.namespaceURI, attribute.localName) in owned:
                holder.removeAttributeNode(attribute)
        for child in list(holder.childNodes):
            if (child.namespaceURI, child.localName) in owned:
                holder.removeChild(child)
        has_properties = any(
            attribute.namespaceURI != "http://www.w3.org/2000/xmlns/"
            and (attribute.namespaceURI, attribute.localName) != (RDF_NS, "about")
            for attribute in holder.attributes.values())
        has_content = any(node.nodeType not in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE)
                          or bool(node.data.strip()) for node in holder.childNodes)
        if not has_properties and not has_content:
            rdf.removeChild(holder)
    # Give the generated properties a separate scope. A foreign writer may
    # bind the same prefix to another URI; never change its namespace binding.
    update = document.importNode(fresh, deep=True)
    update.setAttribute("xmlns:rdf", RDF_NS)
    rdf.insertBefore(update, rdf.firstChild)
    return document.toxml()


def _property_signatures(text: str) -> dict[str, str]:
    """Fingerprint RDF values without depending on the writer's prefixes."""
    if (len(text.encode("utf-8")) > MAX_XMP_BYTES
            or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I)):
        raise ValueError(T("XMP cannot be checked safely"))
    root = ET.fromstring(text)
    rdfs = list(root.iter(f"{{{RDF_NS}}}RDF"))
    if not rdfs:
        raise ValueError(T("existing XMP has no RDF metadata document"))

    def value(node):
        # Attribute and simple element property forms have equal values.
        if not len(node) and not node.attrib:
            return (node.text or "").strip()
        return (tuple(sorted(node.attrib.items())), (node.text or "").strip(),
                tuple((child.tag, value(child)) for child in node))

    properties = {}
    for rdf in rdfs:
        for holder in rdf:
            if (holder.tag != f"{{{RDF_NS}}}Description"
                    or holder.get(f"{{{RDF_NS}}}about")):
                continue
            for name, item in holder.attrib.items():
                if _split(name)[0] not in ("", RDF_NS):
                    properties.setdefault(name, []).append(item.strip())
            for child in holder:
                properties.setdefault(child.tag, []).append(value(child))
    return {name: hashlib.sha256(repr(items).encode("utf-8")).hexdigest()
            for name, items in properties.items()}


def sidecar_snapshot(source: Path) -> dict:
    """Persist this with an outbox entry to detect later external changes.

    No metadata values or image bytes are stored in the snapshot. Failure to
    read a present sidecar must prevent a write, not count as an absent file.
    """
    paths = find_sidecars(source)
    if len(paths) > 1:
        raise ValueError(T("Multiple XMP sidecars exist for this photo. Keep one naming convention before syncing."))
    if not paths:
        return {"path": None, "properties": {}}
    target = paths[0]
    if target.stat().st_size > MAX_XMP_BYTES:
        raise ValueError(T("existing XMP is too large to update safely"))
    return {"path": str(target),
            "properties": _property_signatures(_decode(target.read_bytes()))}


def _check_snapshot(target, original, document, record, expected):
    if expected.get("path") != (str(target) if original is not None else None):
        raise OSError(T("The XMP sidecar was created, removed, or renamed by another application. Read its metadata before syncing again."))
    before = expected.get("properties") or {}
    current = _property_signatures(_decode(original)) if original is not None else {}
    proposed = _property_signatures(document)
    conflicts = []
    for uri, name in _owned_properties(record):
        if (uri, name) == (NAMESPACES["xmp"], "CreatorTool"):
            continue
        key = f"{{{uri}}}{name}"
        if current.get(key) != before.get(key) and current.get(key) != proposed.get(key):
            conflicts.append(f"{_PREFIX_BY_URI.get(uri, 'lighttable')}:{name}")
    if conflicts:
        raise OSError(T("External XMP changes conflict with pending edits ({properties}). Read the sidecar metadata before syncing again.",
                        properties=", ".join(sorted(conflicts))))


def write_sidecar(source: Path, record: dict,
                  errors: list[str] | None = None, *,
                  expected_snapshot: dict | None = None) -> bool:
    """Write `photo.xmp` beside an original. Best effort, never raises.

    Errors are returned for the durable outbox to show and retry. A caller
    with an earlier snapshot also refuses conflicting external edits.
    """
    source = Path(source)
    target = source.with_suffix(".xmp")
    try:
        existing = find_sidecars(source)
        if len(existing) > 1:
            raise ValueError(T("Multiple XMP sidecars exist for this photo. Keep one naming convention before syncing."))
        if existing:
            target = existing[0]
        if target.exists() and target.stat().st_size > MAX_XMP_BYTES:
            raise ValueError(T("existing XMP is too large to update safely"))
        original = target.read_bytes() if target.exists() else None
        document = (merge_sidecar(_decode(original), record)
                    if original is not None else build_sidecar(record))
        if expected_snapshot is not None:
            _check_snapshot(target, original, document, record, expected_snapshot)
        if ((target.read_bytes() if target.exists() else None) != original):
            raise OSError(T("Another application changed the XMP file. Retry to merge its latest changes."))
        # Retain the first pre-LightTable sidecar. Other editors may carry
        # settings we cannot round-trip, and an opt-in mirror must never make
        # those bytes unrecoverable.
        durable_io.atomic_write_text(
            target, document, encoding="utf-8",
            keep_backup=True, backup_once=True,
        )
        return True
    except Exception as error:  # an unreadable foreign document must survive
        if errors is not None:
            errors.append(str(error))
        return False
