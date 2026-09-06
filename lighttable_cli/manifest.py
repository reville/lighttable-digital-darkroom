"""The shared command, route, schema, and agent-tool manifest.

Keep this module standard-library-only: `lighttable --help`, schema discovery,
and MCP startup must not import the image engine.
"""
from __future__ import annotations


# These families share the manifest-driven long-tail parser.  Bespoke command
# families below them still use custom argument builders where that makes the
# help materially clearer, but their routes and agent surfaces live here too.
DOMAIN_ACTIONS = {
    "keywords": ["list", "add", "remove", "set", "rename"],
    "raw-default": ["get", "save", "delete"],
    "collections": ["list", "create", "create-smart", "add", "set", "delete"],
    "stacks": ["create", "toggle", "unstack"],
    "virtual-copy": ["create", "delete"],
    "import": ["catalog-inspect", "catalog-run", "sidecars", "write-sidecars", "status"],
    "ingest": ["sources", "scan", "run", "cancel", "status"],
    "watch": ["list", "add", "remove", "status"],
    "merge": ["hdr", "panorama", "focus", "status"],
    "denoise": ["run", "cancel", "status"],
    "enhance": ["capabilities", "run"],
    "external-edit": ["start", "status"],
    "files": ["move", "rename", "trash", "reveal", "duplicates"],
    "catalog": ["backup", "stats", "folders", "scan-status"],
    "cache": ["pregenerate", "cancel", "status"],
    "masks": ["batch", "cancel", "status"],
    "ai-index": ["status", "enable", "disable", "rebuild", "clear", "results"],
    "soft-proof": ["profiles", "render"],
    "prefs": ["get", "set"],
    "match-exposure": ["run"],
}


ROUTE_COVERAGE = {
    "status / doctor / open / serve / instances / stop": ["/api/health"],
    "photos list / show / path / thumb / orig": [
        "/api/images", "/api/catalog/query", "/api/state", "/api/resolve",
        "/api/thumb", "/api/orig", "/api/exif"],
    "rate / flag / label / edit": ["/api/state", "/api/state/bulk"],
    "render / analyze / compare": [
        "/api/render", "/api/render/file", "/api/analyze",
        "/api/render/compare", "/api/neutral"],
    "sources / folders": [
        "/api/catalog/sources", "/api/catalog/scan",
        "/api/catalog/folders", "/api/folders"],
    "collections / stacks / virtual-copy": [
        "/api/catalog/collections", "/api/library"],
    "keywords": ["/api/catalog/keywords", "/api/state"],
    "metadata": ["/api/metadata", "/api/metadata/bulk", "/api/exif"],
    "history": ["/api/history", "/api/history/state", "/api/history/clear"],
    "versions": ["/api/state"],
    "film / schema": ["/api/options"],
    "raw-default": ["/api/raw-default"],
    "presets": ["/api/presets", "/api/presets/import", "/api/presets/export"],
    "export": ["/api/export", "/api/export/status", "/api/export-recipes"],
    "jobs": ["/api/jobs", "/api/jobs/<id>", "/api/jobs/<id>/cancel"],
    "import": ["/api/import/catalog", "/api/import/status",
               "/api/import/sidecars", "/api/sidecars/write", "/api/sidecars/status"],
    "ingest": ["/api/ingest", "/api/ingest/scan", "/api/ingest/status",
               "/api/ingest/sources", "/api/ingest/cancel"],
    "watch": ["/api/watch", "/api/watch/status"],
    "merge": ["/api/merge", "/api/merge/status"],
    "denoise / enhance": [
        "/api/denoise", "/api/denoise/status", "/api/denoise/cancel",
        "/api/enhance", "/api/enhance/capabilities"],
    "external-edit": ["/api/edit-external", "/api/edit-external/status"],
    "files": ["/api/photos/move", "/api/photos/rename",
              "/api/photos/trash", "/api/photos/reveal",
              "/api/catalog/duplicates"],
    "catalog": ["/api/catalog", "/api/catalog/backup",
                "/api/catalog/folders", "/api/catalog/scan"],
    "doctor / recovery": ["/api/recovery", "/api/recovery/log"],
    "cache / masks": [
        "/api/cache/status", "/api/cache/purge",
        "/api/cache/pregenerate", "/api/cache/pregenerate/status",
        "/api/cache/pregenerate/cancel", "/api/batch/semantic-masks",
        "/api/batch/semantic-masks/status",
        "/api/batch/semantic-masks/cancel", "/api/mask/semantic"],
    "ai-index": ["/api/ai-index", "/api/ai-index/status",
                 "/api/ai-index/results"],
    "soft-proof": ["/api/soft-proof", "/api/soft-proof/profiles"],
    "prefs": ["/api/prefs"],
    "match-exposure": ["/api/match-exposure"],
    "lens / geometry": ["/api/lens-profile", "/api/geometry/auto"],
    "ui": ["/api/ui/state", "/api/ui/command", "/api/ui/result",
           "/api/events"],
    "route": ["/api/*"],
}

INTERNAL_ROUTES = {
    "/api/export/preview": "read-only export dialog delivery example",
    "/api/thumb/rendered": "edit-aware browser thumbnail replacement",
    "/api/render/native": "native surface transport",
    "/api/render/helper": "browser helper generated from a native surface",
    "/api/render/image": "cached base-render bytes",
    "/api/edit/image": "cached base-edit bytes",
    "/api/refine": "progressive browser preview plumbing",
    "/api/perf/export-one": "benchmark-only export path",
    "/api/video": "range streaming used by the window",
    "/api/calibration/target.png": "calibration UI asset",
}

TOOLS = [
    {
        "name": "lighttable_status", "summary": "Inspect the running app",
        "route": "/api/health", "method": "GET", "readOnly": True,
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "lighttable_photos_list",
        "summary": "List photos matching catalog query fields",
        "route": "/api/catalog/query", "method": "POST", "readOnly": True,
        "schema": {"type": "object", "additionalProperties": True},
    },
    {
        "name": "lighttable_photo_show", "summary": "Read one edit record",
        "route": "/api/state", "method": "GET", "readOnly": True,
        "schema": {"type": "object", "required": ["name"],
                   "properties": {"name": {"type": "string"}}},
    },
    {
        "name": "lighttable_state_update",
        "summary": "Strictly update one photo and record its origin",
        "route": "/api/state", "method": "POST", "readOnly": False,
        "schema": {"type": "object", "required": ["name"],
                   "additionalProperties": True,
                   "properties": {"name": {"type": "string"},
                                  "origin": {"type": "string"},
                                  "historyLabel": {"type": "string"}}},
    },
    {
        "name": "lighttable_render", "summary": "Render an edited photo",
        "route": "/api/render/file", "method": "POST", "readOnly": True,
        "image": True,
        "schema": {"type": "object", "required": ["name"],
                   "properties": {"name": {"type": "string"},
                                  "w": {"type": "integer", "minimum": 64},
                                  "before": {"type": "boolean"}}},
    },
    {
        "name": "lighttable_analyze", "summary": "Measure a rendered photo",
        "route": "/api/analyze", "method": "POST", "readOnly": True,
        "schema": {"type": "object", "required": ["name"],
                   "additionalProperties": True,
                   "properties": {"name": {"type": "string"}}},
    },
    {
        "name": "lighttable_compare", "summary": "Render a visual comparison",
        "route": "/api/render/compare", "method": "POST", "readOnly": True,
        "image": True,
        "schema": {"type": "object", "required": ["name"],
                   "additionalProperties": True,
                   "properties": {"name": {"type": "string"}}},
    },
    {
        "name": "lighttable_ui_command", "summary": "Drive the visible window",
        "route": "/api/ui/command", "method": "POST", "readOnly": False,
        "schema": {"type": "object", "required": ["command"],
                   "properties": {"command": {"type": "string"},
                                  "args": {"type": "object"}}},
    },
    {
        "name": "lighttable_api_request",
        "summary": "Call any manifest-covered local API route",
        "route": None, "method": None, "readOnly": False,
        "schema": {"type": "object", "required": ["method", "path"],
                   "properties": {
                       "method": {"enum": ["GET", "POST"]},
                       "path": {"type": "string", "pattern": "^/api/"},
                       "body": {"type": "object"},
                       "confirm": {"type": "boolean"}}},
    },
]


DESTRUCTIVE_ROUTES = {
    "/api/photos/trash", "/api/history/clear", "/api/ai-index",
    "/api/catalog/sources", "/api/photos/move", "/api/photos/rename",
    "/api/recovery",
}


def schema(options: dict | None = None) -> dict:
    options = options or {}
    grade = options.get("grade") or {}
    params = options.get("params") or {}
    labels = options.get("labels") or ["none", "red", "yellow", "green",
                                        "blue", "purple"]
    statuses = options.get("statuses") or ["pending", "approved", "skipped"]
    param_properties = {}
    for key, default in (params.get("defaults") or {}).items():
        if key in (params.get("ranges") or {}):
            minimum, maximum = params["ranges"][key]
            param_properties[key] = {
                "type": "number", "minimum": minimum, "maximum": maximum}
        elif isinstance(default, bool):
            param_properties[key] = {"type": "boolean"}
        elif isinstance(default, str):
            param_properties[key] = {"type": "string"}
        else:
            param_properties[key] = {"type": "number"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://lighttable.local/schema/cli.json",
        "title": "LightTable command and agent API",
        "type": "object",
        "properties": {
            "tools": {"type": "array", "default": TOOLS},
            "routes": {"type": "object", "default": ROUTE_COVERAGE},
        },
        "$defs": {
            "exportRecipe": {
                "type": "object",
                "properties": {
                    "destination": {"type": "string", "default": "film-exports"},
                    "destinationMode": {"enum": ["fixed", "original-folder-relative", "preserve-source-hierarchy"], "default": "fixed"},
                    "preserveCaptureTime": {"type": "boolean", "default": False},
                    "captureTimePolicy": {"enum": ["require-offset", "local"], "default": "require-offset"},
                    "metadata": {"enum": ["all", "all-except-location", "copyright", "none"]},
                    "sidecar": {"type": "boolean"},
                    "filenameTemplate": {"type": "string"},
                    "collision": {"enum": ["rename", "skip", "overwrite"]},
                },
            },
            "stateUpdate": {
                "type": "object", "required": ["name"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "origin": {"type": "string"},
                    "historyLabel": {"type": "string"},
                    "status": {"enum": statuses},
                    "rating": {"type": "integer", "minimum": 0, "maximum": 5},
                    "label": {"enum": labels},
                    "params": {"type": "object",
                               "properties": param_properties},
                    "grade": {"type": "object", "properties": {
                        key: {"type": "number",
                              "minimum": (grade.get("ranges") or {}).get(
                                  key, [-1, 1])[0],
                              "maximum": (grade.get("ranges") or {}).get(
                                  key, [-1, 1])[1]}
                        for key in (grade.get("defaults") or {})}},
                    "crop": {"type": ["object", "null"]},
                    "masks": {"type": "array"},
                    "heals": {"type": "array"},
                    "optics": {"type": "object"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "versions": {"type": "array"},
                },
            },
            "job": {
                "type": "object", "required": ["id", "kind", "state"],
                "properties": {
                    "id": {"type": "string"}, "kind": {"type": "string"},
                    "state": {"enum": ["queued", "running", "done",
                                        "failed", "cancelled"]},
                    "progress": {"type": "integer"},
                    "total": {"type": "integer"},
                    "errors": {"type": "array"}, "result": {},
                },
            },
        },
    }
