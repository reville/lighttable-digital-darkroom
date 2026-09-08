"""Bundled looks and a bounded, static community catalog. Presets are data only."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import durable_io

ROOT = Path(__file__).resolve().parent
ORIGIN = "https://lighttable.app"
CATALOG_PATH = "/presets/catalog-v1.json"
NATIVE_VERSION = 3
CAPABILITIES = {"look-v1"}
MAX_RECIPE_BYTES = 512 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}/[a-z0-9][a-z0-9-]{0,59}\Z")
VERSION_PATTERN = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}\Z")
CREATIVE_GRADE_KEYS = frozenset({
    "contrast", "highlights", "shadows", "whites", "blacks", "vibrance",
    "saturation", "texture", "clarity", "dehaze", "vignette", "curveL",
    "curveR", "curveG", "curveB", "hsl", "pointColor", "colorGrading",
})
CREATIVE_FILM_KEYS = frozenset({
    "stock", "paper", "workflow_mode", "paper_locked", "output_recipe",
    "development_time", "print_development_time", "exposure_ev",
    "print_exposure", "gamma", "auto_exposure", "scan_sharpen",
    "couplers_on", "couplers_amount", "halation_on", "halation_amount",
    "grain_on", "grain_amount", "glare_on", "glare_amount",
    "camera_diffusion_family", "camera_diffusion_strength", "print_preflash",
    "print_y_filter_shift", "print_m_filter_shift", "scan_softness", "scan_sharpness",
})


def valid_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("Invalid community preset ID")
    return value


def version(value):
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise ValueError("Invalid preset version")
    return value


def text(value, maximum=240):
    return " ".join(str(value or "").split())[:maximum]


def public_link(value):
    value = str(value or "")[:1000]
    u = urlsplit(value)
    return value if u.scheme == "https" and u.netloc and not u.username else ""


def metadata(raw):
    """Preserve portable attribution separately from library ownership."""
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    result = {
        "version": version(raw.get("version", "1.0.0")),
        "description": text(raw.get("description"), 600),
        "tags": [text(t, 32) for t in raw.get("tags", [])[:12]
                 if isinstance(t, str)] if isinstance(raw.get("tags"), list) else [],
        "author": {"name": text(author.get("name"), 100),
                   "url": public_link(author.get("url")),
                   **({"id": text(author["id"], 60)} if author.get("id") else {})},
        "license": text(raw.get("license"), 80),
    }
    if raw.get("parentId"):
        result["parentId"] = text(raw["parentId"], 120)
    origin = raw.get("community")
    if isinstance(origin, dict):
        result["community"] = {"id": valid_id(origin.get("id")),
                               "version": version(origin.get("version"))}
    return result


def _finite(value, depth=0):
    if depth > 12:
        raise ValueError("Preset settings are nested too deeply")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Preset settings must be finite numbers")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item, depth + 1)
    if isinstance(value, list):
        if len(value) > 256:
            raise ValueError("Preset setting contains too many values")
        for item in value:
            _finite(item, depth + 1)


def validate_look(raw):
    """Reject unsupported v3 behavior, rather than silently changing the look."""
    import film_pipeline as fp
    import grade

    if not isinstance(raw, dict) or raw.get("scope") != "look":
        raise ValueError("This preset requires an unsupported preset scope")
    if raw.get("filmMode") not in {"on", "off", "preserve"}:
        raise ValueError("This preset requires an unsupported Film mode")
    _finite(raw)
    g, p = raw.get("grade", {}), raw.get("params", {})
    if not isinstance(g, dict) or not isinstance(p, dict):
        raise ValueError("Invalid preset adjustments")
    included_g = raw.get("includedGrade", list(g))
    included_p = raw.get("includedFilm", list(p))
    if not isinstance(included_g, list) or not isinstance(included_p, list):
        raise ValueError("Invalid preset setting scope")
    if (not all(isinstance(k, str) for k in included_g + included_p)
            or not set(included_g) <= CREATIVE_GRADE_KEYS
            or not set(g) <= CREATIVE_GRADE_KEYS
            or not set(included_p) <= CREATIVE_FILM_KEYS
            or not set(p) <= CREATIVE_FILM_KEYS):
        raise ValueError("A shared look cannot replace photo corrections")
    if set(included_g) != set(g) or set(included_p) != set(p):
        raise ValueError("Preset scope does not match its adjustments")
    if any(raw.get(k) for k in ("crop", "masks", "heals", "optics")):
        raise ValueError("A shared look cannot carry crop, retouching, or lens corrections")
    if raw["filmMode"] == "on" and "stock" not in p:
        raise ValueError("A Film look must identify its film stock")
    if "stock" in p and p["stock"] not in fp.PROFILE_BY_ID:
        raise ValueError("This look needs a film stock unavailable in this app")
    if "paper" in p and p["paper"] not in {v["id"] for v in fp.PAPER_PROFILES}:
        raise ValueError("This look needs a print profile unavailable in this app")
    cg, cp = grade.clean(g), fp.clean_params(p)
    # Values stripped or clamped by the normalizers would change the recipe.
    for key, value in g.items():
        if key not in cg or cg[key] != value:
            raise ValueError(f"Unsupported or out-of-range preset adjustment: {key}")
    for key, value in p.items():
        if cp.get(key) != value:
            raise ValueError(f"Unsupported or out-of-range Film adjustment: {key}")
    metadata(raw)
    return raw


def prepare_look(raw):
    """Explicitly export creative settings from a local photo/preset snapshot."""
    import film_pipeline as fp
    import grade

    result = copy.deepcopy(raw)
    g = grade.clean(raw.get("grade") or {})
    included = raw.get("includedGrade")
    chosen = set(included) if isinstance(included, list) else set(g)
    result["grade"] = {k: v for k, v in g.items() if k in CREATIVE_GRADE_KEYS and k in chosen}
    params = raw.get("params") or {}
    mode = raw.get("filmMode")
    if mode not in {"on", "off", "preserve"}:
        mode = "on" if raw.get("includeFilm") and params.get("profile_enabled", True) else "off"
    cleaned = fp.clean_params(params)
    included_film = raw.get("includedFilm")
    chosen_film = set(included_film) if isinstance(included_film, list) else set(cleaned)
    result["params"] = {k: v for k, v in cleaned.items()
                        if k in CREATIVE_FILM_KEYS and k in chosen_film} if mode == "on" else {}
    result.update(scope="look", filmMode=mode, includeFilm=mode == "on",
                  includedGrade=list(result["grade"]), includedFilm=list(result["params"]),
                  presetType="style", masks=[], heals=[], optics={})
    result.pop("crop", None)
    result.pop("collection", None)
    # Community identity is a local install record, never a redistributor identity.
    if result.get("community"):
        result["parentId"] = result["community"]["id"]
        result.pop("community", None)
    return result


def builtin_presets():
    value = json.loads((ROOT / "presets" / "builtin.json").read_text())
    if value.get("version") != NATIVE_VERSION:
        raise ValueError("Unsupported bundled preset version")
    return [dict(validate_look(p), collection="builtin") for p in value["presets"]]


def look_patch(preset):
    """Sparse patch shared by non-UI apply paths; photo corrections stay local."""
    g = preset.get("grade") or {}
    p = preset.get("params") or {}
    result = {"grade": {k: g[k] for k in preset.get("includedGrade", g)
                        if k in g and k in CREATIVE_GRADE_KEYS}}
    params = {k: p[k] for k in preset.get("includedFilm", p)
              if k in p and k in CREATIVE_FILM_KEYS}
    if preset.get("filmMode") in {"on", "off"}:
        params["profile_enabled"] = preset["filmMode"] == "on"
    if params:
        result["params"] = params
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("The preset host redirected this request")


class CommunityCatalog:
    def __init__(self, cache, *, origin=ORIGIN, fetch=None):
        self.cache = Path(cache)
        self.origin = origin.rstrip("/")
        self.fetch = fetch or self._fetch
        self.lock = threading.RLock()

    def url(self, value):
        if not isinstance(value, str) or len(value) > 1200:
            raise ValueError("Invalid community asset URL")
        parsed = urlsplit(urljoin(self.origin + "/", value))
        origin = urlsplit(self.origin)
        if (parsed.scheme != "https" or parsed.netloc != origin.netloc
                or not parsed.path.startswith("/presets/")
                or parsed.query or parsed.fragment or "%" in parsed.path
                or "\\" in parsed.path or any(p in {".", ".."} for p in parsed.path.split("/"))):
            raise ValueError("Community assets must be hosted in the LightTable preset gallery")
        return parsed.geturl()

    def _fetch(self, url, limit, etag=""):
        headers = {"User-Agent": "LightTable-Presets/1", "Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=12) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > limit:
                    raise ValueError("Community download is too large")
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise ValueError("Community download is too large")
                return payload, response.headers.get("ETag", "")[:200]
        except HTTPError as error:
            if error.code == 304:
                return None, etag
            raise

    def validate(self, value):
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise ValueError("Update LightTable to use this community catalog")
        entries = value.get("presets")
        if not isinstance(entries, list) or len(entries) > 2000:
            raise ValueError("Invalid community catalog size")
        result, seen = [], set()
        for record in entries:
            if not isinstance(record, dict):
                raise ValueError("Invalid community listing")
            ident, rev = valid_id(record.get("id")), version(record.get("version"))
            if ident in seen:
                raise ValueError("Duplicate community preset ID")
            seen.add(ident)
            capabilities = record.get("capabilities", [])
            compatible = (record.get("schemaVersion") == NATIVE_VERSION
                          and isinstance(capabilities, list)
                          and "look-v1" in capabilities
                          and all(isinstance(c, str) and c in CAPABILITIES for c in capabilities))
            file = record.get("file") or {}
            if not isinstance(file, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(file.get("sha256", ""))):
                raise ValueError("Invalid preset checksum")
            size = file.get("bytes")
            if type(size) is not int or not 0 < size <= MAX_RECIPE_BYTES:
                raise ValueError("Invalid preset download size")
            file_url = self.url(file.get("url"))
            expected = f"/presets/files/{ident}/{rev}.ltpreset"
            if urlsplit(file_url).path != expected:
                raise ValueError("Preset download path does not match its identity")
            previews = record.get("previews", [])
            if not isinstance(previews, list) or len(previews) > 6:
                raise ValueError("Invalid preview collection")
            cleaned_previews = []
            for preview in previews:
                if not isinstance(preview, dict):
                    raise ValueError("Invalid preset preview")
                credit = preview.get("credit") or {}
                if not isinstance(credit, dict):
                    raise ValueError("Invalid preview credit")
                cleaned_previews.append({"label": text(preview.get("label"), 80),
                    "before": self.url(preview.get("before")),
                    "after": self.url(preview.get("after")),
                    "credit": {"name": text(credit.get("name"), 120),
                               "url": public_link(credit.get("url")),
                               "license": text(credit.get("license"), 80)}})
            mode = record.get("filmMode")
            if mode not in {"on", "off", "preserve"}:
                compatible = False
            result.append({**metadata(record), "id": ident, "name": text(record.get("name"), 120),
                "schemaVersion": record.get("schemaVersion"), "capabilities": capabilities,
                "scope": "look", "filmMode": mode, "compatible": compatible,
                "file": {"url": file_url, "sha256": file["sha256"], "bytes": size},
                "previews": cleaned_previews, "pageUrl": self.url(record.get("pageUrl") or f"/presets/{ident}/"),
                "featured": record.get("featured") is True,
                "publishedAt": text(record.get("publishedAt"), 40)})
        return {"schemaVersion": 1, "updatedAt": text(value.get("updatedAt"), 40), "presets": result}

    def _cached(self):
        try:
            raw = json.loads((self.cache / "catalog.json").read_text())
            self.validate(raw["catalog"])
            return raw
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def catalog(self, refresh=False):
        with self.lock:
            cached = self._cached()
            if cached and not refresh and time.time() - cached.get("checkedAt", 0) < 86400:
                return {**cached["catalog"], "cached": True, "offline": False}
            try:
                data, etag = self.fetch(self.url(CATALOG_PATH), MAX_CATALOG_BYTES,
                                        cached.get("etag", "") if cached else "")
                if data is None and not cached:
                    raise ValueError("No cached catalog is available")
                catalog = self.validate(json.loads(data)) if data is not None else cached["catalog"]
                self.cache.mkdir(parents=True, exist_ok=True)
                durable_io.atomic_write_json(self.cache / "catalog.json", {
                    "catalog": catalog, "etag": etag, "checkedAt": time.time()})
                return {**catalog, "cached": data is None, "offline": False}
            except (OSError, ValueError, TypeError, KeyError, URLError):
                fallback = cached["catalog"] if cached else {"schemaVersion": 1, "updatedAt": "", "presets": []}
                return {**fallback, "cached": bool(cached), "offline": True,
                        "error": "Community is unavailable. Your saved and built-in presets still work."}

    def recipe(self, ident, expected_version=None):
        valid_id(ident)
        with self.lock:
            catalog = self.catalog()
            item = next((p for p in catalog["presets"] if p["id"] == ident), None)
            if not item:
                raise ValueError("This preset is not in the current community catalog")
            if not item["compatible"]:
                raise ValueError("Update LightTable to use this preset")
            if expected_version and expected_version != item["version"]:
                raise ValueError("This preset has changed. Refresh its details before installing.")
            file = item["file"]
            path = self.cache / "recipes" / (file["sha256"] + ".json")
            try:
                data = path.read_bytes() if path.stat().st_size <= MAX_RECIPE_BYTES else b""
            except OSError:
                data = b""
            if hashlib.sha256(data).hexdigest() != file["sha256"]:
                data, _ = self.fetch(file["url"], MAX_RECIPE_BYTES)
            if (not data or len(data) != file["bytes"]
                    or hashlib.sha256(data).hexdigest() != file["sha256"]):
                raise ValueError("Preset download could not be verified. Try again.")
            payload = json.loads(data)
            if (not isinstance(payload, dict) or payload.get("format") != "LightTable Preset"
                    or payload.get("version") != NATIVE_VERSION
                    or not isinstance(payload.get("presets"), list) or len(payload["presets"]) != 1):
                raise ValueError("Unsupported community preset file")
            preset = validate_look(payload["presets"][0])
            if (preset.get("id") != ident or preset.get("version") != item["version"]
                    or preset.get("name") != item["name"] or preset.get("filmMode") != item["filmMode"]):
                raise ValueError("Preset identity does not match its listing")
            path.parent.mkdir(parents=True, exist_ok=True)
            durable_io.atomic_write_text(path, data.decode("utf-8"))
            # Bound cached downloads; installed presets live separately and are never evicted.
            cached_files = sorted(path.parent.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in cached_files[100:]:
                stale.unlink(missing_ok=True)
            return {**copy.deepcopy(preset), **metadata(item)}


def submission_url(name):
    return ("https://github.com/reville/lighttable-site/issues/new?template=preset-submission.yml"
            "&title=" + quote("Preset submission: " + text(name, 120)))
