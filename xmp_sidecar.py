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
        return float(str(value).strip().lstrip("+"))
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
            if not text:
                continue
            if item.get(f"{{{XML_NS}}}lang") == "x-default" and default is None:
                default = text
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
            if text:
                scalars.setdefault((prefix, local), text)
    return scalars, arrays, kinds


def _first(arrays: dict, scalars: dict, prefix: str, local: str) -> str | None:
    items = arrays.get((prefix, local))
    if items:
        return items[0]
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
    if number is None:
        return None, False
    if number < 0:
        # Lightroom writes -1 for a rejected photo; LightTable keeps the
        # rejection as a status, never as a star count.
        return None, True
    return int(max(0, min(5, round(number)))), False


def parse(text: str) -> dict | None:
    """Read one XMP document into LightTable's normalised metadata dict.

    Returns None for anything that is not parseable XML, or is too large to
    be a sidecar.
    """
    if not text or len(text) > MAX_XMP_BYTES:
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
    orientation = _number(scalars.get(("tiff", "Orientation")))
    angle = _number(crs.get("CropAngle"))
    parsed = {
        "rating": rating,
        "captureTime": scalars.get(("exif", "DateTimeOriginal")) or scalars.get(("photoshop", "DateCreated")),
        "rejected": rejected,
        "label": _label(scalars.get(("xmp", "Label"))),
        "keywords": list(arrays.get(("dc", "subject")) or []),
        "keywordPaths": list(arrays.get(("lr", "hierarchicalSubject")) or []),
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
    return parsed


def sidecar_paths(source: Path) -> list[Path]:
    """Both conventional sidecar names: photo.xmp and photo.CR2.xmp."""
    source = Path(source)
    replaced = source.with_name(f"{source.stem}{SIDECAR_SUFFIXES[0]}")
    appended = source.with_name(f"{source.name}{SIDECAR_SUFFIXES[0]}")
    return [replaced] if replaced == appended else [replaced, appended]


def find_sidecar(source: Path) -> Path | None:
    """The first sidecar on disk for one image, in Adobe's naming order."""
    for candidate in sidecar_paths(source):
        for suffix in SIDECAR_SUFFIXES:
            path = candidate.with_suffix(suffix)
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
    return None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def read_sidecar(source: Path) -> dict | None:
    """Parse the sidecar beside one image, if there is one."""
    path = find_sidecar(source)
    if path is None:
        return None
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
    # Pasted captions can contain form feeds and other characters XML 1.0
    # cannot represent, even as character references. Never publish a sidecar
    # that reports success but cannot be read or merged on the next save.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]",
                  "\ufffd", str(value))
    return (text.replace("&", "&amp;").replace("<", "&lt;")
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
    import json as _json

    attributes: list[str] = []
    elements: list[str] = []

    rating = -1 if record.get("status") == "skipped" else record.get("rating")
    if rating:
        attributes.append(f'   xmp:Rating="{int(rating)}"')
    label = record.get("label")
    if label and label != "none":
        attributes.append(f'   xmp:Label="{_escape(label.title())}"')
    attributes.append('   xmp:CreatorTool="LightTable"')
    if record.get("captureTimeOverride"):
        import capture_time
        try:
            stamp = capture_time.normalized_timestamp(record["captureTimeOverride"])
            attributes.append(f'   exif:DateTimeOriginal="{_escape(stamp)}"')
            attributes.append(f'   photoshop:DateCreated="{_escape(stamp)}"')
            elements.append('   <lighttable:captureTimeOriginal rdf:parseType="Resource"/>')
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
    if keywords:
        flat = [path.rsplit(" > ", 1)[-1] for path in keywords]
        elements.append(_bag("dc", "subject", dict.fromkeys(flat)))
        elements.append(_bag("lr", "hierarchicalSubject",
                             [path.replace(" > ", "|") for path in keywords]))

    iptc = record.get("iptc") or {}
    for key, (prefix, local, kind) in {
        "title": ("dc", "title", "alt"),
        "caption": ("dc", "description", "alt"),
        "copyright": ("dc", "rights", "alt"),
        "creator": ("dc", "creator", "seq"),
        "headline": ("photoshop", "Headline", "attr"),
        "credit": ("photoshop", "Credit", "attr"),
        "city": ("photoshop", "City", "attr"),
        "state": ("photoshop", "State", "attr"),
        "country": ("photoshop", "Country", "attr"),
    }.items():
        value = iptc.get(key)
        if not value:
            continue
        prefix_name, local_name, kind_name = prefix, local, kind
        if kind_name == "alt":
            elements.append(_alt(prefix_name, local_name, value))
        elif kind_name == "seq":
            elements.append(_seq(prefix_name, local_name, [value]))
        else:
            attributes.append(
                f'   {prefix_name}:{local_name}="{_escape(value)}"')

    native = {key: record.get(key) for key in
              ("params", "grade", "crop", "masks", "heals", "optics", "status")
              if record.get(key) not in (None, [], {})}
    if native:
        payload = _escape(_json.dumps(native, separators=(",", ":")))
        attributes.append(f'   lighttable:edit="{payload}"')
        attributes.append('   lighttable:note="crs values are approximate; '
                          'the lighttable:edit payload is authoritative"')

    return _XMP_TEMPLATE.format(
        attributes="\n".join(attributes),
        elements="\n".join(elements) if elements else "")


def _import_scoped_element(document, node):
    """Copy an element together with the namespace scope it inherited."""
    saved = document.importNode(node, deep=True)
    ancestor, declarations = node, {}
    while ancestor is not None:
        if ancestor.nodeType == Node.ELEMENT_NODE:
            for attribute in ancestor.attributes.values():
                if attribute.name == "xmlns" or attribute.prefix == "xmlns":
                    declarations.setdefault(attribute.name, attribute.value)
        ancestor = ancestor.parentNode
    for name, value in declarations.items():
        saved.setAttribute(name, value)
    return saved


def merge_sidecar(existing: str, record: dict) -> str:
    """Update our properties, retaining the rest of an editor's RDF document.

    Work with namespace-aware DOM nodes so foreign prefixes, declarations,
    structured masks, qualifiers and unrelated RDF subjects survive intact.
    Refuse malformed files instead of replacing work we cannot understand.
    """
    if len(existing.encode("utf-8")) > MAX_XMP_BYTES:
        raise ValueError("existing XMP is too large to update safely")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", existing, re.I):
        raise ValueError("XMP declarations cannot be updated safely")
    document = minidom.parseString(existing)
    generated = minidom.parseString(build_sidecar(record))
    rdf_nodes = document.getElementsByTagNameNS(RDF_NS, "RDF")
    if not rdf_nodes:
        raise ValueError("existing XMP has no RDF metadata document")
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
    owned = {(NAMESPACES["xmp"], "CreatorTool")}
    for key, properties in {
        "rating": [("xmp", "Rating")], "status": [("xmp", "Rating")],
        "label": [("xmp", "Label")],
        "keywords": [("dc", "subject"), ("lr", "hierarchicalSubject")],
        "grade": [("crs", name) for name, _ in _CRS_EXPORT.values()],
        "crop": [("crs", name) for name in
                 ("HasCrop", "CropLeft", "CropTop", "CropRight", "CropBottom")],
        "iptc": [("dc", name) for name in
                 ("title", "description", "rights", "creator")]
                + [("photoshop", name) for name in
                   ("Headline", "Credit", "City", "State", "Country")],
    }.items():
        if key in record:
            owned.update((NAMESPACES[prefix], name) for prefix, name in properties)
    native_uri = "https://lighttable.photo/ns/1.0/"
    capture_properties = {(NAMESPACES["exif"], "DateTimeOriginal"),
                          (NAMESPACES["photoshop"], "DateCreated")}
    capture_marker = (native_uri, "captureTimeOriginal")
    previous_capture = next((child for holder in holders for child in holder.childNodes
                             if (child.namespaceURI, child.localName) == capture_marker), None)
    if "captureTimeOverride" in record:
        changing = fresh.hasAttributeNS(NAMESPACES["exif"], "DateTimeOriginal")
        if changing or (record["captureTimeOverride"] is None and previous_capture is not None):
            owned.update(capture_properties | {capture_marker})
            if changing:
                replacement = fresh.getElementsByTagNameNS(*capture_marker)[0]
                if previous_capture is not None:
                    fresh.replaceChild(_import_scoped_element(generated, previous_capture), replacement)
                else:
                    # Keep the pre-correction dates inside an XMP structure.
                    # A later reset restores them, while ordinary full-state
                    # mirrors with no override leave foreign camera dates alone.
                    for holder in holders:
                        for attribute in holder.attributes.values():
                            if (attribute.namespaceURI, attribute.localName) in capture_properties:
                                saved = generated.createElementNS(attribute.namespaceURI, attribute.name)
                                saved.setAttribute(f"xmlns:{attribute.prefix}", attribute.namespaceURI)
                                saved.appendChild(generated.createTextNode(attribute.value))
                                replacement.appendChild(saved)
                        for child in holder.childNodes:
                            if (child.namespaceURI, child.localName) in capture_properties:
                                replacement.appendChild(_import_scoped_element(generated, child))
            else:
                for child in previous_capture.childNodes:
                    if (child.namespaceURI, child.localName) in capture_properties:
                        fresh.appendChild(_import_scoped_element(generated, child))
    if any(key in record for key in
           ("params", "grade", "crop", "masks", "heals", "optics", "status")):
        owned.update((native_uri, name) for name in ("edit", "note"))
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


def write_sidecar(source: Path, record: dict,
                  errors: list[str] | None = None) -> bool:
    """Write `photo.xmp` beside an original. Best effort, never raises.

    A read-only volume or a permission error returns False rather than
    surfacing an error: the catalog is the real store and this file is a
    convenience for other software.
    """
    source = Path(source)
    target = source.with_suffix(".xmp")
    existing = find_sidecar(source)
    if existing is not None:
        target = existing
    try:
        original = target.read_bytes() if target.exists() else None
        document = (merge_sidecar(original.decode("utf-8"), record)
                    if original is not None else build_sidecar(record))
        if ((target.read_bytes() if target.exists() else None) != original):
            raise OSError("Another application changed the XMP file. Retry to merge its latest changes.")
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
