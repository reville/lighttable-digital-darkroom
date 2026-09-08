from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import platform_paths

from .client import Client, ClientError, query
from .instances import Instance, default_instance_directory, discover, select
from .manifest import (
    DESTRUCTIVE_ROUTES,
    DOMAIN_ACTIONS,
    INTERNAL_ROUTES,
    ROUTE_COVERAGE,
    TOOLS,
)
from .manifest import schema as manifest_schema
from .output import emit, progress
from .selectors import parse_where, resolve_many, resolve_reference


APP = Path(__file__).resolve().parents[1]
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_SERVER = 3
EXIT_VALIDATION = 4
EXIT_JOB_FAILED = 5
EXIT_CONFIRMATION = 6

def value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def assignments(values: list[str] | None) -> dict:
    result = {}
    for item in values or []:
        key, separator, raw = item.partition("=")
        if not separator or not key:
            raise ValueError(f"expected key=value, got {item!r}")
        result[key] = value(raw)
    return result


def curve_assignments(values: list[str] | None) -> dict:
    """Parse normalized curve control points for the server-side LUT helper."""
    channels = {"L": "curveL", "R": "curveR", "G": "curveG", "B": "curveB"}
    result = {}
    for item in values or []:
        channel, separator, raw_points = item.partition("=")
        key = channels.get(channel.upper())
        if not separator or key is None:
            raise ValueError(f"expected L|R|G|B=x,y;x,y, got {item!r}")
        points = []
        for raw_point in raw_points.split(";"):
            pair = raw_point.split(",")
            if len(pair) != 2:
                raise ValueError(f"invalid curve point {raw_point!r}")
            x, y = (float(part) for part in pair)
            if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
                raise ValueError("curve coordinates must be between 0 and 1")
            points.append([round(x * 255.0, 6), round(y * 255.0, 6)])
        if len(points) < 2:
            raise ValueError("a curve needs at least two points")
        result[key] = points
    return result


def nested_assignments(values: list[str] | None) -> dict:
    """Turn dotted key=value assignments into nested dictionaries."""
    result = {}
    for dotted, item_value in assignments(values).items():
        cursor = result
        parts = dotted.split(".")
        if any(not part for part in parts):
            raise ValueError(f"invalid dotted field {dotted!r}")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):
                raise ValueError(f"conflicting dotted field {dotted!r}")
        cursor[parts[-1]] = item_value
    return result


def merge_mapping(base: dict, patch: dict) -> dict:
    """Recursively merge nested edit objects without losing sibling controls."""
    result = dict(base)
    for key, incoming in patch.items():
        if isinstance(incoming, dict) and isinstance(result.get(key), dict):
            result[key] = merge_mapping(result[key], incoming)
        else:
            result[key] = incoming
    return result


# The window's lens defaults. A preset carrying exactly these values has no
# lens edit to apply, so they are not layered onto a photo.
OPTICS_DEFAULTS = {
    "profileEnabled": False, "profileOverride": None,
    "profileDistortion": True, "profileVignette": True,
    "flipHorizontal": False, "flipVertical": False, "distortion": 0.0,
    "vignette": 0.0, "vertical": 0.0, "horizontal": 0.0, "rotate": 0.0,
    "scale": 1.0,
}
PRESET_LAYERED = ("masks", "heals")


def preset_state(preset: dict) -> dict:
    """The groups a preset carries, following the window's layering rules.

    A saved preset stores every group, so a grade-only preset still holds an
    empty film recipe, empty mask and heal lists, and default lens values.
    Sending those would reset the photo's own film, local corrections, and
    geometry; only the groups the preset actually contains are applied.
    """
    if preset.get("scope") == "look":
        from preset_library import look_patch
        return look_patch(preset)
    entry: dict = {}
    grade = preset.get("grade") if isinstance(preset.get("grade"), dict) else {}
    included = preset.get("includedGrade")
    if not isinstance(included, list):
        included = list(grade)
    chosen = {key: grade[key] for key in included if key in grade}
    if chosen:
        entry["grade"] = chosen
    params = preset.get("params")
    if preset.get("includeFilm", True) and isinstance(params, dict) and params:
        entry["params"] = params
    for key in PRESET_LAYERED:
        if isinstance(preset.get(key), list) and preset[key]:
            entry[key] = preset[key]
    optics = preset.get("optics") if isinstance(preset.get("optics"), dict) else {}
    changed = {key: value for key, value in optics.items()
               if key not in OPTICS_DEFAULTS or value != OPTICS_DEFAULTS[key]}
    if changed:
        entry["optics"] = changed
    return entry


def fresh_identities(items: list, group: str) -> list:
    """Copy layered masks or heals with identities that cannot collide."""
    prefix = group[:-1]
    return [{**item, "id": f"{prefix}-{uuid.uuid4().hex}"}
            if isinstance(item, dict) else item for item in items]


def add_selector_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("refs", nargs="*", help="qualified names, paths, #ids, @current, or @selection")
    parser.add_argument("--where", action="append", default=[], metavar="FIELD=VALUE")
    parser.add_argument("--names-from")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--sort", default="capture:desc")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lighttable",
        description="Control the local LightTable catalog and editor")
    parser.add_argument("--url", help="explicit server URL")
    parser.add_argument("--port", type=int, help="select a running instance")
    parser.add_argument("--catalog", help="select an instance by catalog path")
    parser.add_argument("--profile", choices=["default", "review"])
    parser.add_argument("--folder", help="photo folder for an explicitly started server")
    parser.add_argument("--origin", default="cli")
    parser.add_argument("--lenient", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--jsonl", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="print a destructive operation's plan without applying it")
    parser.add_argument("--timeout", type=float, default=300.0)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status")
    commands.add_parser("instances")
    commands.add_parser("doctor")
    commands.add_parser("open")
    serve = commands.add_parser("serve")
    serve.add_argument("--daemon", action="store_true")
    commands.add_parser("stop")

    photos = commands.add_parser("photos").add_subparsers(dest="action", required=True)
    listing = photos.add_parser("list")
    listing.add_argument("--where", action="append", default=[])
    listing.add_argument("--limit", type=int, default=500)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--sort", default="capture:desc")
    find = photos.add_parser("find")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=500)
    find.add_argument("--offset", type=int, default=0)
    find.add_argument("--sort", default="capture:desc")
    show = photos.add_parser("show")
    show.add_argument("ref")
    path = photos.add_parser("path")
    path.add_argument("ref")
    for action in ("thumb", "orig"):
        item = photos.add_parser(action)
        item.add_argument("ref")
        item.add_argument("-o", "--output", required=True)
        item.add_argument("--width", type=int, default=1400)

    rate = commands.add_parser("rate")
    rate.add_argument("rating", type=int, choices=range(0, 6))
    add_selector_arguments(rate)
    flag = commands.add_parser("flag")
    flag.add_argument("flag", choices=["pick", "reject", "clear"])
    add_selector_arguments(flag)
    label = commands.add_parser("label")
    label.add_argument("colour", choices=["none", "red", "yellow", "green", "blue", "purple"])
    add_selector_arguments(label)

    edit = commands.add_parser("edit")
    edit.add_argument("action", choices=["get", "set", "reset"])
    add_selector_arguments(edit)
    edit.add_argument("--grade", action="append", default=[])
    edit.add_argument("--film", action="append", default=[])
    edit.add_argument("--rotate", type=float)
    edit.add_argument("--curve", action="append", default=[],
                      metavar="L=0,0;0.5,0.4;1,1")
    edit.add_argument("--hsl", action="append", default=[],
                      metavar="BAND.FIELD=VALUE")
    edit.add_argument("--crop", help="x,y,w,h or null")
    edit.add_argument("--patch", help="JSON merge object or filename")
    edit.add_argument("--replace", help="complete edit-state JSON object or filename")
    edit.add_argument("--from", dest="copy_from", help="copy settings from another photo")
    edit.add_argument("--include", default="film,grade,crop,masks,heals,optics")
    edit.add_argument("--preset", help="apply a named preset")
    edit.add_argument("--layer", action="store_true",
                      help="accepted for compatibility; presets always layer"
                           " over existing settings")
    edit.add_argument("--group", choices=["film", "grade", "crop", "masks", "heals", "optics", "all"], default="all")
    edit.add_argument("--history-label", "--label", default="Command-line edit")

    render = commands.add_parser("render")
    render.add_argument("ref")
    render.add_argument("-o", "--output", required=True)
    render.add_argument("--width", type=int, default=1400)
    render.add_argument("--before", action="store_true")
    render.add_argument("--state")
    render.add_argument("--format", choices=["png", "jpeg"])
    analyze = commands.add_parser("analyze")
    analyze.add_argument("ref")
    analyze.add_argument("--width", type=int, default=1400)
    analyze.add_argument("--reference")
    analyze.add_argument("--region", action="append", default=[])
    compare = commands.add_parser("compare")
    compare.add_argument("ref")
    compare.add_argument("-o", "--output", required=True)
    compare.add_argument("--against")
    compare.add_argument("--width", type=int, default=1400)
    compare.add_argument("--wipe", type=float)

    sources = commands.add_parser("sources").add_subparsers(dest="action", required=True)
    sources.add_parser("list")
    add_source = sources.add_parser("add"); add_source.add_argument("path")
    for action in ("remove", "rescan"):
        item = sources.add_parser(action); item.add_argument("id", type=int)
        if action == "remove": item.add_argument("--yes", action="store_true")
    rename = sources.add_parser("rename"); rename.add_argument("id", type=int); rename.add_argument("name")
    favorite = sources.add_parser("favorite"); favorite.add_argument("id", type=int); favorite.add_argument("value", choices=["on", "off"])

    folders = commands.add_parser("folders").add_subparsers(dest="action", required=True)
    create = folders.add_parser("create"); create.add_argument("name"); create.add_argument("--parent", default="")
    rename_folder = folders.add_parser("rename"); rename_folder.add_argument("path"); rename_folder.add_argument("name")

    metadata = commands.add_parser("metadata").add_subparsers(dest="action", required=True)
    metadata_get = metadata.add_parser("get"); metadata_get.add_argument("ref")
    metadata_set = metadata.add_parser("set"); metadata_set.add_argument("ref"); metadata_set.add_argument("fields", nargs="+")
    metadata_bulk = metadata.add_parser("bulk"); add_selector_arguments(metadata_bulk)
    metadata_bulk.add_argument("--field", action="append", required=True)

    history = commands.add_parser("history").add_subparsers(dest="action", required=True)
    history_list = history.add_parser("list"); history_list.add_argument("ref")
    history_show = history.add_parser("show"); history_show.add_argument("id", type=int)
    history_restore = history.add_parser("restore"); history_restore.add_argument("ref"); history_restore.add_argument("id", type=int)
    history_clear = history.add_parser("clear"); history_clear.add_argument("ref"); history_clear.add_argument("--yes", action="store_true")

    film = commands.add_parser("film").add_subparsers(dest="action", required=True)
    for action in ("stocks", "papers", "profiles", "recipes", "defaults"):
        film.add_parser(action)

    presets = commands.add_parser("presets").add_subparsers(dest="action", required=True)
    presets.add_parser("list")
    preset_show = presets.add_parser("show"); preset_show.add_argument("name")
    preset_delete = presets.add_parser("delete"); preset_delete.add_argument("name")
    preset_apply = presets.add_parser("apply"); preset_apply.add_argument("name"); add_selector_arguments(preset_apply)
    preset_save = presets.add_parser("save"); preset_save.add_argument("name"); preset_save.add_argument("ref")
    preset_import = presets.add_parser("import"); preset_import.add_argument("files", nargs="+")
    preset_export = presets.add_parser("export"); preset_export.add_argument("name")
    preset_export.add_argument("-o", "--output", required=True)
    preset_export.add_argument("--format", default="lighttable")

    versions = commands.add_parser("versions").add_subparsers(dest="action", required=True)
    for action in ("list", "save", "restore", "delete"):
        item = versions.add_parser(action); item.add_argument("ref")
        if action == "save": item.add_argument("--name", required=True)
        if action in {"restore", "delete"}: item.add_argument("id")
        if action == "delete": item.add_argument("--yes", action="store_true")

    export = commands.add_parser("export").add_subparsers(dest="action", required=True)
    run_export = export.add_parser("run"); add_selector_arguments(run_export)
    run_export.add_argument("--which", choices=["approved", "rated", "all"], default="approved")
    run_export.add_argument("--destination", default="film-exports")
    run_export.add_argument("--destination-mode", choices=["fixed", "original-folder-relative", "preserve-source-hierarchy"], default="fixed")
    run_export.add_argument("--preserve-capture-time", action="store_true")
    run_export.add_argument("--capture-time-policy", choices=["require-offset", "local"], default="require-offset")
    run_export.add_argument("--metadata", choices=["all", "all-except-location", "copyright", "none"], default="all-except-location")
    run_export.add_argument("--no-sidecar", action="store_true")
    run_export.add_argument(
        "--format", choices=["jpeg", "heif", "png", "tif"], default="jpeg")
    run_export.add_argument("--quality", type=int, default=92)
    run_export.add_argument("--long-edge", type=int)
    run_export.add_argument("--no-wait", action="store_true")
    export.add_parser("status")
    export.add_parser("recipes")

    jobs = commands.add_parser("jobs").add_subparsers(dest="action", required=True)
    jobs.add_parser("list")
    for action in ("show", "wait", "cancel"):
        item = jobs.add_parser(action); item.add_argument("id")

    ui = commands.add_parser("ui").add_subparsers(dest="action", required=True)
    ui.add_parser("state")
    ui_command = ui.add_parser("command"); ui_command.add_argument("verb"); ui_command.add_argument("args", nargs="*")

    for domain, actions in DOMAIN_ACTIONS.items():
        item = commands.add_parser(domain)
        item.add_argument("action", choices=actions)
        item.add_argument("refs", nargs="*")
        item.add_argument("--body", default="{}", help="JSON object or JSON file")
        item.add_argument("--yes", action="store_true")

    route = commands.add_parser("route")
    route.add_argument("method", choices=["GET", "POST", "get", "post"])
    route.add_argument("path")
    route.add_argument("--body", default="{}")
    route.add_argument("--yes", action="store_true")

    commands.add_parser("schema")
    completion = commands.add_parser("completion")
    completion.add_argument("shell", choices=["bash", "zsh", "fish"])
    commands.add_parser("mcp")
    return parser


def normalize_global_arguments(argv: list[str]) -> list[str]:
    """Allow universal output/connection flags before or after subcommands."""
    flags = {"--lenient", "--json", "--jsonl", "--quiet", "--dry-run"}
    valued = {"--url", "--port", "--catalog", "--profile", "--folder",
              "--origin", "--timeout"}
    global_part, command_part = [], []
    index = 0
    while index < len(argv):
        item = argv[index]
        key = item.split("=", 1)[0]
        if item in flags or ("=" in item and key in flags):
            global_part.append(item)
        elif key in valued:
            global_part.append(item)
            if "=" not in item and index + 1 < len(argv):
                index += 1
                global_part.append(argv[index])
        else:
            command_part.append(item)
        index += 1
    return global_part + command_part


def profile_environment(args, *, parent: bool) -> tuple[dict, Path]:
    env = dict(os.environ)
    if args.profile == "review":
        support = platform_paths.profile_root() / "review"
        folder = Path(args.folder).expanduser() if args.folder else APP / "demo-assets/cc0-raw/files"
        env.update({
            "LIGHTTABLE_DIR": str(folder),
            "LIGHTTABLE_CATALOG_FILE": str(support / "Catalog/library.sqlite3"),
            "LIGHTTABLE_PREFS_FILE": str(support / "prefs.json"),
            "LIGHTTABLE_PRESETS_FILE": str(support / "presets.json"),
            "LIGHTTABLE_CACHE_DIR": str(support / "Cache"),
            "LIGHTTABLE_INSTANCE_DIR": str(support / "instances"),
            "LIGHTTABLE_HEADLESS": "1",
        })
        if platform_paths.is_linux():
            env.update({
                "LIGHTTABLE_AI_DIR": str(support / "AI Index"),
                "LIGHTTABLE_MODEL_DIR": str(support / "Models"),
                "LIGHTTABLE_SERVER_LOG": str(support / "logs/server.log"),
                "LIGHTTABLE_LOG_FILE": str(support / "logs/server.log"),
            })
    else:
        folder = Path(args.folder).expanduser() if args.folder else Path.home() / "Pictures"
        env["LIGHTTABLE_DIR"] = str(folder)
        env["LIGHTTABLE_HEADLESS"] = "1"
    env["LIGHTTABLE_PORT"] = str(args.port or 0)
    if parent:
        env["LIGHTTABLE_PARENT_PID"] = str(os.getpid())
        env["LIGHTTABLE_WATCH_PARENT"] = "1"
    return env, folder


def start_server(args, *, parent: bool) -> tuple[subprocess.Popen, Path]:
    env, _ = profile_environment(args, parent=parent)
    if platform_paths.is_linux() and (parent or getattr(args, "daemon", False)):
        env.setdefault("LIGHTTABLE_SERVER_LOG", str(platform_paths.server_log_file()))
        env.setdefault("LIGHTTABLE_LOG_FILE", env["LIGHTTABLE_SERVER_LOG"])
    python = APP / ".venv/bin/python"
    executable = str(python if python.is_file() else Path(sys.executable))
    process = subprocess.Popen(
        [executable, str(APP / "server.py")], cwd=APP, env=env,
        stdout=subprocess.DEVNULL if parent or getattr(args, "daemon", False) else None,
        stderr=subprocess.DEVNULL if parent or getattr(args, "daemon", False) else None,
        start_new_session=bool(getattr(args, "daemon", False)),
    )
    directory = Path(env.get("LIGHTTABLE_INSTANCE_DIR") or default_instance_directory())
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("LightTable server exited during startup")
        candidates = [item for item in discover(directory)
                      if item.pid == process.pid]
        if candidates:
            return process, candidates[0].path
        time.sleep(0.05)
    process.terminate()
    raise RuntimeError("LightTable server did not register within five seconds")


def discovery_directory(args) -> Path:
    if args.profile:
        env, _ = profile_environment(args, parent=False)
        return Path(env.get("LIGHTTABLE_INSTANCE_DIR")
                    or default_instance_directory())
    return default_instance_directory()


def client_for(args, *, allow_start: bool = True) -> tuple[Client, subprocess.Popen | None]:
    instances = discover(discovery_directory(args))
    chosen = None
    explicit_url = args.url or os.environ.get("LIGHTTABLE_URL")
    if explicit_url:
        parsed = urllib.parse.urlparse(explicit_url)
        port = parsed.port
        chosen = next((item for item in instances if item.port == port), None)
        return Client(explicit_url, token=chosen.token if chosen else "",
                      strict=not args.lenient, origin=args.origin,
                      timeout=args.timeout), None
    chosen = select(instances, port=args.port, catalog=args.catalog)
    process = None
    if chosen is None and allow_start and args.profile:
        process, path = start_server(args, parent=True)
        chosen = next(item for item in discover(path.parent)
                      if item.pid == process.pid)
    if chosen is None:
        raise ClientError(0, {"error": "no LightTable server is running",
                              "code": "no-server"})
    return Client(chosen.url, token=chosen.token, strict=not args.lenient,
                  origin=args.origin, timeout=args.timeout), process


def selected_names(client: Client, args) -> list[str]:
    names = resolve_many(client, getattr(args, "refs", []),
        where=getattr(args, "where", []), names_from=getattr(args, "names_from", None),
        limit=getattr(args, "limit", 5000), sort=getattr(args, "sort", "capture:desc"))
    if not names:
        raise ValueError("no photos selected")
    return names


def state_update(client: Client, names: list[str], entry: dict, args,
                 history_label: str) -> dict:
    body = {"names": names, "entry": entry, "origin": args.origin,
            "historyLabel": history_label}
    return client.post("/api/state/bulk", body)


def state_merge_update(client: Client, names: list[str], entry: dict, args,
                       history_label: str, *,
                       append: tuple[str, ...] = ()) -> dict:
    """Apply a patch without replacing each photo's unsupplied nested fields.

    Groups named in ``append`` are lists layered after the photo's own items
    with fresh identities, the way the window applies a preset's masks.
    """
    results = []
    for name in names:
        current = client.get(query("/api/state", name=name))
        merged = {}
        for key, incoming in entry.items():
            if key in {"grade", "params", "optics"} and isinstance(incoming, dict):
                merged[key] = merge_mapping(current.get(key) or {}, incoming)
            elif key in append and isinstance(incoming, list):
                merged[key] = [*(current.get(key) or []),
                               *fresh_identities(incoming, key)]
            else:
                merged[key] = incoming
        results.append(client.post("/api/state", {
            "name": name, **merged, "origin": args.origin,
            "historyLabel": history_label,
        }))
    warnings = [warning for result in results
                for warning in result.get("warnings", [])]
    return {"ok": True, "count": len(results), "warnings": warnings}


def read_json_argument(argument: str):
    path = Path(argument).expanduser()
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    text = path.read_text(encoding="utf-8") if is_file else argument
    return json.loads(text)


def wait_export(client: Client, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        latest = client.get("/api/export/status")
        if not latest.get("running"):
            if latest.get("errors"):
                raise ClientError(500, {"error": "; ".join(latest["errors"]),
                                        "code": "job-failed"})
            return latest
        progress(f"export {latest.get('done', 0)} / {latest.get('total', 0)}")
        time.sleep(0.25)
    raise ClientError(500, {"error": "export timed out", "code": "job-failed"})


def wait_job(client: Client, ident: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/api/jobs/{ident}")
        if record.get("state") in {"done", "failed", "cancelled"}:
            return record
        progress(f"{record.get('kind')} {record.get('progress', 0)} / {record.get('total', 0)}")
        time.sleep(0.25)
    raise ClientError(500, {"error": "job timed out", "code": "job-failed"})


def dispatch(client: Client, args):
    command = args.command
    if command == "status":
        return client.get("/api/health")
    if command == "doctor":
        health = client.get("/api/health")
        catalog = client.get("/api/catalog")
        try:
            library = client.get("/api/recovery?backups=0")
        except ClientError:
            library = None
        return {"ok": bool(health.get("ok")), "health": health,
                "catalog": catalog.get("stats"),
                "catalogRecovery": catalog.get("recovery"),
                "library": library and {
                    "damaged": (library.get("catalog") or {}).get("damaged"),
                    "verify": library.get("verify"),
                    "session": library.get("session"),
                    "quarantine": library.get("quarantine"),
                    "disk": library.get("disk"),
                    "documents": library.get("documents"),
                    "safeMode": library.get("safeMode"),
                },
                "checks": {
                    "server": bool(health.get("ok")),
                    "catalog": bool(catalog.get("enabled")),
                    "rust": bool(health.get("rust")),
                    "models": bool((health.get("models") or {}).get("available")),
                    "library": bool(
                        (health.get("library") or {}).get("status")
                        in ("ok", "folder-mode")),
                }}
    if command == "open":
        import webbrowser
        return {"ok": bool(webbrowser.open(client.url)), "url": client.url}
    if command == "photos":
        if args.action in {"list", "find"}:
            where = list(getattr(args, "where", []))
            if args.action == "find": where.append(f"q={args.query}")
            return client.post("/api/catalog/query", parse_where(
                where, limit=args.limit, offset=args.offset, sort=args.sort))["items"]
        names = resolve_reference(client, args.ref)
        if not names:
            raise ValueError("photo was not found")
        name = names[0]
        if args.action == "show":
            return {"name": name, **client.get(query("/api/state", name=name))}
        if args.action == "path":
            return client.post("/api/photos/reveal", {"name": name})
        path = query("/api/thumb" if args.action == "thumb" else "/api/orig",
                     name=name, w=args.width)
        payload, _ = client.bytes(path)
        Path(args.output).expanduser().write_bytes(payload)
        return {"ok": True, "output": str(Path(args.output).expanduser()),
                "bytes": len(payload)}
    if command in {"rate", "flag", "label"}:
        names = selected_names(client, args)
        if command == "rate": entry, label = {"rating": args.rating}, f"Rating {args.rating}"
        elif command == "flag":
            status = {"pick": "approved", "reject": "skipped", "clear": "pending"}[args.flag]
            entry, label = {"status": status}, f"Flag {args.flag}"
        else: entry, label = {"label": args.colour}, f"Label {args.colour}"
        return state_update(client, names, entry, args, label)
    if command == "edit":
        names = selected_names(client, args)
        if args.action == "get":
            return [{"name": name, **client.get(query("/api/state", name=name))}
                    for name in names]
        if args.action == "reset":
            options = client.get("/api/options")
            groups = [args.group] if args.group != "all" else [
                "film", "grade", "crop", "masks", "heals", "optics"]
            reset = {}
            for group in groups:
                reset[group if group != "film" else "params"] = {
                    "grade": options["grade"]["defaults"],
                    "film": options["params"]["defaults"], "crop": None,
                    "masks": [], "heals": [], "optics": {},
                }[group]
            return state_update(client, names, reset, args, "Reset edits")
        entry = {}
        if args.replace:
            if any((args.patch, args.copy_from, args.preset, args.grade,
                    args.film, args.curve, args.hsl, args.crop,
                    args.rotate is not None)):
                raise ValueError("--replace cannot be combined with edit patches")
            replacement = read_json_argument(args.replace)
            if not isinstance(replacement, dict):
                raise ValueError("replacement state must be an object")
            editable = {
                key: replacement[key] for key in (
                    "status", "rating", "label", "params", "grade", "crop",
                    "masks", "heals", "optics", "keywords", "versions")
                if key in replacement
            }
            if not editable:
                raise ValueError("replacement contains no editable state")
            return state_update(client, names, editable, args,
                                args.history_label)
        if args.copy_from:
            source_names = resolve_reference(client, args.copy_from)
            if not source_names:
                raise ValueError("copy source was not found")
            source = client.get(query("/api/state", name=source_names[0]))
            aliases = {"film": "params", "grade": "grade", "crop": "crop",
                       "masks": "masks", "heals": "heals", "optics": "optics"}
            groups = [item.strip() for item in args.include.split(",") if item.strip()]
            unknown = sorted(set(groups) - set(aliases))
            if unknown:
                raise ValueError(f"unknown include group: {', '.join(unknown)}")
            entry.update({aliases[group]: source.get(aliases[group])
                          for group in groups if aliases[group] in source})
        if args.preset:
            presets = client.get("/api/presets")
            preset = next((item for item in presets
                           if item.get("name") == args.preset), None)
            if preset is None:
                raise ValueError("preset not found")
            entry = merge_mapping(entry, preset_state(preset))
        grade_patch = assignments(args.grade)
        grade_patch.update(curve_assignments(args.curve))
        if args.hsl:
            grade_patch = merge_mapping(
                grade_patch, {"hsl": nested_assignments(args.hsl)})
        if grade_patch:
            entry["grade"] = merge_mapping(entry.get("grade") or {}, grade_patch)
        film_patch = assignments(args.film)
        if args.rotate is not None:
            film_patch["rotate"] = args.rotate
        if film_patch:
            entry["params"] = merge_mapping(entry.get("params") or {}, film_patch)
        if args.crop:
            entry["crop"] = None if args.crop == "null" else dict(zip(
                ("x", "y", "w", "h"), (float(v) for v in args.crop.split(","))))
        if args.patch:
            patch = read_json_argument(args.patch)
            if not isinstance(patch, dict): raise ValueError("patch must be an object")
            entry = merge_mapping(entry, patch)
        if not entry: raise ValueError("no edit fields were supplied")
        return state_merge_update(client, names, entry, args,
                                  args.history_label,
                                  append=PRESET_LAYERED if args.preset else ())
    if command in {"render", "analyze", "compare"}:
        names = resolve_reference(client, args.ref)
        if not names: raise ValueError("photo was not found")
        body = {"name": names[0], "w": args.width, "client": client.client_id}
        if command == "render":
            body.update(before=args.before,
                        format=args.format or ("jpeg" if str(args.output).lower().endswith((".jpg", ".jpeg")) else "png"))
            if args.state: body["state"] = read_json_argument(args.state)
            payload, ctype = client.bytes("/api/render/file", body)
            Path(args.output).expanduser().write_bytes(payload)
            return {"ok": True, "output": str(Path(args.output).expanduser()),
                    "contentType": ctype, "bytes": len(payload)}
        if command == "analyze":
            if args.reference:
                refs = resolve_reference(client, args.reference)
                body["reference"] = refs[0] if refs else args.reference
            body["regions"] = [[float(v) for v in item.split(",")]
                               for item in args.region]
            return client.post("/api/analyze", body)
        if args.against:
            refs = resolve_reference(client, args.against)
            body["against"] = refs[0] if refs else args.against
        if args.wipe is not None: body["wipe"] = args.wipe
        payload, ctype = client.bytes("/api/render/compare", body)
        Path(args.output).expanduser().write_bytes(payload)
        return {"ok": True, "output": str(Path(args.output).expanduser()),
                "contentType": ctype, "bytes": len(payload)}
    if command == "sources":
        if args.action == "list": return client.get("/api/catalog")["sources"]
        if args.action == "rescan": return client.post("/api/catalog/scan", {"sourceId": args.id})
        if args.action == "remove" and args.dry_run:
            return {"dryRun": True, "action": "remove-source", "id": args.id}
        if args.action == "remove" and not args.yes:
            raise PermissionError("source removal requires --yes")
        body = {"action": args.action}
        if args.action == "add": body["path"] = args.path
        else: body["id"] = args.id
        if args.action == "rename": body["name"] = args.name
        if args.action == "favorite": body.update(action="favorite", favorite=args.value == "on")
        return client.post("/api/catalog/sources", body)
    if command == "folders":
        body = {"action": args.action, "name": args.name}
        body["parent" if args.action == "create" else "path"] = args.parent if args.action == "create" else args.path
        return client.post("/api/folders", body)
    if command == "metadata":
        if args.action == "bulk":
            names = selected_names(client, args)
            return client.post("/api/metadata/bulk", {
                "names": names, "fields": assignments(args.field)})
        names = resolve_reference(client, args.ref); name = names[0]
        if args.action == "get": return client.get(query("/api/metadata", name=name))
        return client.post("/api/metadata", {"name": name, "fields": assignments(args.fields)})
    if command == "history":
        if args.action == "show": return client.get(query("/api/history/state", id=args.id))
        names = resolve_reference(client, args.ref); name = names[0]
        if args.action == "list": return client.get(query("/api/history", name=name))["steps"]
        if args.action == "clear":
            if args.dry_run:
                return {"dryRun": True, "action": "clear-history",
                        "name": name}
            if not args.yes: raise PermissionError("history clear requires --yes")
            return client.post("/api/history/clear", {"name": name})
        state = client.get(query("/api/history/state", id=args.id))
        editable = {key: state[key] for key in (
            "status", "rating", "label", "params", "grade", "crop",
            "masks", "heals", "optics", "keywords", "versions")
            if key in state}
        return client.post("/api/state", {"name": name, **editable,
            "origin": args.origin, "historyLabel": "Restore history"})
    if command == "film":
        options = client.get("/api/options")
        return {"stocks": options["stocks"]["negatives"] + options["stocks"]["positives"],
                "papers": options["stocks"]["papers"], "profiles": options["profiles"],
                "recipes": options["outputRecipes"], "defaults": options["params"]["defaults"]}[args.action]
    if command == "presets":
        items = client.get("/api/presets")
        if args.action == "list": return items
        if args.action == "import":
            uploads = []
            for raw in args.files:
                path = Path(raw).expanduser()
                uploads.append({"name": path.name,
                    "base64": base64.b64encode(path.read_bytes()).decode()})
            return client.post("/api/presets/import", {"files": uploads})
        if args.action == "export":
            result = client.post("/api/presets/export", {
                "name": args.name, "format": args.format})
            output = Path(args.output).expanduser()
            output.write_text(str(result["content"]), encoding="utf-8")
            return {"ok": True, "output": str(output),
                    "contentType": result.get("contentType"),
                    "bytes": output.stat().st_size}
        preset = next((item for item in items if item.get("name") == args.name), None)
        if args.action == "show":
            if not preset: raise ValueError("preset not found")
            return preset
        if args.action == "delete": return client.post("/api/presets", {"action": "delete", "name": args.name})
        if args.action == "save":
            names = resolve_reference(client, args.ref); state = client.get(query("/api/state", name=names[0]))
            return client.post("/api/presets", {"action": "save", "name": args.name, **state})
        if not preset: raise ValueError("preset not found")
        names = selected_names(client, args)
        return state_merge_update(client, names, preset_state(preset), args,
                                  f"Preset {args.name}", append=PRESET_LAYERED)
    if command == "versions":
        names = resolve_reference(client, args.ref)
        if not names: raise ValueError("photo was not found")
        name = names[0]
        state = client.get(query("/api/state", name=name))
        versions = list(state.get("versions") or [])
        if args.action == "list": return versions
        if args.action == "save":
            version = {"id": uuid.uuid4().hex, "name": args.name,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **{key: state.get(key) for key in
                   ("params", "grade", "crop", "masks", "heals", "optics")}}
            return client.post("/api/state", {"name": name,
                "versions": [version, *versions][:50], "origin": args.origin,
                "historyLabel": f"Save version {args.name}"})
        chosen = next((item for item in versions if item.get("id") == args.id), None)
        if not chosen: raise ValueError("version not found")
        if args.action == "delete":
            if args.dry_run:
                return {"dryRun": True, "action": "delete-version",
                        "name": name, "id": args.id}
            if not args.yes: raise PermissionError("version delete requires --yes")
            return client.post("/api/state", {"name": name,
                "versions": [item for item in versions if item.get("id") != args.id],
                "origin": args.origin, "historyLabel": "Delete version"})
        entry = {key: chosen[key] for key in
                 ("params", "grade", "crop", "masks", "heals", "optics")
                 if key in chosen}
        return state_merge_update(client, [name], entry, args,
                                  f"Restore version {chosen.get('name', '')}")
    if command == "export":
        if args.action == "status": return client.get("/api/export/status")
        if args.action == "recipes": return client.get("/api/export-recipes")
        names = resolve_many(client, args.refs, where=args.where,
            names_from=args.names_from, limit=args.limit, sort=args.sort)
        body = {"which": args.which, "destination": args.destination,
                "format": args.format, "quality": args.quality,
                "longEdge": args.long_edge, "destinationMode": args.destination_mode,
                "preserveCaptureTime": args.preserve_capture_time,
                "captureTimePolicy": args.capture_time_policy,
                "metadata": args.metadata, "sidecar": not args.no_sidecar}
        if names: body["names"] = names
        result = client.post("/api/export", body)
        if not args.no_wait and result.get("queued"): result["status"] = wait_export(client, args.timeout)
        return result
    if command == "jobs":
        if args.action == "list": return client.get("/api/jobs")["jobs"]
        if args.action == "cancel": return client.post(f"/api/jobs/{args.id}/cancel", {})
        if args.action == "wait": return wait_job(client, args.id, args.timeout)
        return client.get(f"/api/jobs/{args.id}")
    if command == "ui":
        if args.action == "state": return client.get("/api/ui/state")
        payload = assignments(args.args)
        return client.post("/api/ui/command", {"command": args.verb,
            "args": payload, "origin": args.origin, "timeout": min(args.timeout, 30)})
    if command in DOMAIN_ACTIONS:
        return dispatch_domain(client, args)
    if command == "route":
        method, path = args.method.upper(), args.path
        if not path.startswith("/api/"): raise ValueError("route path must start with /api/")
        if method == "POST" and path in DESTRUCTIVE_ROUTES and args.dry_run:
            return {"dryRun": True, "method": method, "path": path,
                    "body": read_json_argument(args.body)}
        if method == "POST" and path in DESTRUCTIVE_ROUTES and not args.yes:
            raise PermissionError("destructive route requires --yes")
        return client.get(path) if method == "GET" else client.post(path, read_json_argument(args.body))
    if command == "schema":
        return manifest_schema(client.get("/api/options"))
    if command == "completion":
        return completion_script(args.shell)
    if command == "mcp":
        run_mcp(client); return None
    raise ValueError(f"unsupported command: {command}")


def dispatch_domain(client: Client, args):
    """Route the long-tail command families through their named API surface."""
    body = read_json_argument(args.body)
    if not isinstance(body, dict):
        raise ValueError("--body must be a JSON object")
    body = dict(body)
    action = args.action
    names = resolve_many(client, args.refs) if args.refs else []

    destructive = {
        ("raw-default", "delete"), ("collections", "delete"),
        ("stacks", "unstack"), ("virtual-copy", "delete"),
        ("watch", "remove"), ("files", "move"),
        ("files", "rename"), ("files", "trash"),
        ("ai-index", "clear"),
    }
    if (args.command, action) in destructive and not args.yes \
            and not args.dry_run:
        raise PermissionError(f"{args.command} {action} requires --yes")
    if (args.command, action) in destructive and args.dry_run \
            and (args.command, action) != ("files", "trash"):
        return {"dryRun": True, "command": args.command,
                "action": action, "references": names, "body": body}

    if args.command == "keywords":
        if action == "list":
            return client.get("/api/catalog/keywords")["keywords"]
        if action == "rename":
            return client.post("/api/catalog/keywords",
                               {"action": "rename", **body})
        if not names:
            raise ValueError("keyword edits need one or more photo references")
        incoming = body.get("keywords") or body.get("values") or []
        if isinstance(incoming, str):
            incoming = [incoming]
        results = []
        for name in names:
            current = client.get(query("/api/state", name=name))
            existing = list(current.get("keywords") or [])
            if action == "set": updated = list(incoming)
            elif action == "add": updated = list(dict.fromkeys([*existing, *incoming]))
            else: updated = [item for item in existing if item not in set(incoming)]
            results.append(client.post("/api/state", {
                "name": name, "keywords": updated, "origin": args.origin,
                "historyLabel": f"Keywords {action}",
            }))
        return {"ok": True, "count": len(results)}

    if args.command == "raw-default":
        if not names:
            raise ValueError("raw-default needs a photo reference")
        if action == "get":
            return client.get(query("/api/raw-default", name=names[0]))
        return client.post("/api/raw-default", {
            "name": names[0], "action": action, **body})

    if args.command == "collections":
        if action == "list":
            return client.get("/api/catalog")["collections"]
        payload = {"action": action.replace("-", "_"), **body}
        if names:
            page = client.post("/api/catalog/query", {"limit": 20000})
            ids = {str(item["name"]): int(item["id"])
                   for item in page.get("items", [])}
            payload["imageIds"] = [ids[name] for name in names if name in ids]
        return client.post("/api/catalog/collections", payload)

    if args.command == "stacks":
        mapped = {"create": "create_stack", "toggle": "toggle_stack",
                  "unstack": "unstack"}[action]
        return client.post("/api/library", {"action": mapped,
            **body, **({"members": names} if names else {})})

    if args.command == "virtual-copy":
        if not names:
            raise ValueError("virtual-copy needs a photo reference")
        return client.post("/api/library", {
            "action": "create_virtual" if action == "create" else "delete_virtual",
            "name": names[0], **body})

    if args.command == "import":
        if action == "status": return client.get("/api/import/status")
        if names: body.setdefault("names", names)
        if action == "sidecars": return client.post("/api/import/sidecars", body)
        if action == "write-sidecars": return client.post("/api/sidecars/write", body)
        body["inspectOnly"] = action == "catalog-inspect"
        return client.post("/api/import/catalog", body)

    if args.command == "ingest":
        if action == "sources": return client.get("/api/ingest/sources")
        if action == "status": return client.get("/api/ingest/status")
        route = {"scan": "/api/ingest/scan", "run": "/api/ingest",
                 "cancel": "/api/ingest/cancel"}[action]
        return client.post(route, body)

    if args.command == "watch":
        if action == "list": return client.get("/api/watch")
        if action == "status": return client.get("/api/watch/status")
        body["action"] = "save" if action == "add" else "delete"
        return client.post("/api/watch", body)

    if args.command == "merge":
        if action == "status": return client.get("/api/merge/status")
        supplied = body.pop("names", [])
        return client.post("/api/merge", {"mode": action,
            "names": names or supplied, **body})

    if args.command == "denoise":
        if action == "status": return client.get("/api/denoise/status")
        if action == "cancel": return client.post("/api/denoise/cancel", {})
        if names: body.setdefault("name", names[0])
        return client.post("/api/denoise", body)

    if args.command == "enhance":
        if action == "capabilities": return client.get("/api/enhance/capabilities")
        if names: body.setdefault("name", names[0])
        return client.post("/api/enhance", body)

    if args.command == "external-edit":
        if action == "status": return client.get("/api/edit-external/status")
        if names: body.setdefault("names", names)
        return client.post("/api/edit-external", body)

    if args.command == "files":
        if action == "duplicates": return client.get("/api/catalog/duplicates")
        if action == "reveal":
            if not names: raise ValueError("files reveal needs a photo reference")
            return client.post("/api/photos/reveal", {"name": names[0]})
        if names: body.setdefault("names", names)
        route = {"move": "/api/photos/move", "rename": "/api/photos/rename",
                 "trash": "/api/photos/trash"}[action]
        if args.dry_run:
            if action == "trash":
                plan = client.post(route, body)
                return {"dryRun": True, "action": "trash",
                        "paths": plan.get("paths", []),
                        "count": plan.get("count", 0)}
            return {"dryRun": True, "action": action,
                    "names": body.get("names", []),
                    **{key: value for key, value in body.items()
                       if key != "names"}}
        result = client.post(route, body)
        if action == "trash":
            relay = client.post("/api/ui/command", {
                "command": "trash", "args": {"paths": result.get("paths", [])},
                "origin": args.origin, "timeout": min(args.timeout, 30)})
            return {**result, "relay": relay}
        return result

    if args.command == "catalog":
        if action == "backup": return client.post("/api/catalog/backup", body)
        if action == "folders":
            return client.get(query("/api/catalog/folders", **body))
        catalog = client.get("/api/catalog")
        return catalog["stats"] if action == "stats" else catalog["scan"]

    if args.command == "cache":
        if action == "status": return client.get("/api/cache/pregenerate/status")
        if names: body.setdefault("names", names)
        route = "/api/cache/pregenerate/cancel" if action == "cancel" else "/api/cache/pregenerate"
        return client.post(route, body)

    if args.command == "masks":
        if action == "status": return client.get("/api/batch/semantic-masks/status")
        if names: body.setdefault("names", names)
        route = "/api/batch/semantic-masks/cancel" if action == "cancel" else "/api/batch/semantic-masks"
        return client.post(route, body)

    if args.command == "ai-index":
        if action == "status": return client.get("/api/ai-index/status")
        if action == "results": return client.get("/api/ai-index/results")
        return client.post("/api/ai-index", {"action": action, **body})

    if args.command == "soft-proof":
        if action == "profiles": return client.get("/api/soft-proof/profiles")
        return client.post("/api/soft-proof", body)

    if args.command == "prefs":
        if action == "get": return client.get("/api/prefs")
        return client.post("/api/prefs", body)

    if args.command == "match-exposure":
        if names:
            body.setdefault("reference", names[0])
            body.setdefault("targets", names[1:])
        return client.post("/api/match-exposure", body)
    raise ValueError(f"unsupported command family: {args.command}")


def completion_script(shell: str) -> str:
    commands = "status instances doctor open serve stop photos rate flag label edit render analyze compare sources folders metadata history versions film presets export jobs ui keywords raw-default collections stacks virtual-copy import ingest watch merge denoise enhance external-edit files catalog cache masks ai-index soft-proof prefs match-exposure route schema completion mcp"
    if shell == "fish": return f"complete -c lighttable -f -a '{commands}'"
    if shell == "zsh": return f"#compdef lighttable\n_arguments '1:command:({commands})'"
    return f"complete -W '{commands}' lighttable"


def mcp_call(client: Client, tool: dict, arguments: dict) -> dict:
    name = tool["name"]
    if name == "lighttable_api_request":
        method = str(arguments.get("method", "GET")).upper()
        path = str(arguments.get("path", ""))
        if path in DESTRUCTIVE_ROUTES and not arguments.get("confirm"):
            return {"isError": True, "content": [{"type": "text",
                "text": "This destructive call needs confirm: true."}]}
        result = client.get(path) if method == "GET" else client.post(path, arguments.get("body") or {})
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    route = tool["route"]
    if tool["method"] == "GET":
        if arguments:
            route = query(route, **arguments)
        result = client.get(route)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    if tool.get("image"):
        raw, content_type = client.bytes(route, arguments)
        return {"content": [{"type": "image", "mimeType": content_type,
                             "data": base64.b64encode(raw).decode()}]}
    if name == "lighttable_state_update":
        arguments.setdefault("origin", "agent:mcp")
        arguments.setdefault("historyLabel", "Agent edit")
    result = client.post(route, arguments)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


def run_mcp(client: Client) -> None:
    by_name = {tool["name"]: tool for tool in TOOLS}
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18",
                          "capabilities": {"tools": {}, "resources": {}},
                          "serverInfo": {"name": "lighttable", "version": "1.0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": tool["name"],
                    "description": tool["summary"], "inputSchema": tool["schema"],
                    "annotations": {"readOnlyHint": tool["readOnly"],
                                    "destructiveHint": tool.get("route") in DESTRUCTIVE_ROUTES}}
                    for tool in TOOLS]}
            elif method == "tools/call":
                params = request.get("params") or {}; tool = by_name[params["name"]]
                result = mcp_call(client, tool, params.get("arguments") or {})
            elif method == "resources/list":
                result = {"resources": [
                    {"uri": "lighttable://schema", "name": "LightTable schema", "mimeType": "application/json"},
                    {"uri": "lighttable://agents", "name": "LightTable agent guide", "mimeType": "text/markdown"},
                    {"uri": "lighttable://ui", "name": "Current LightTable window", "mimeType": "application/json"}]}
            elif method == "resources/read":
                uri = (request.get("params") or {}).get("uri")
                if uri == "lighttable://schema": text = json.dumps(manifest_schema(client.get("/api/options")))
                elif uri == "lighttable://agents": text = (APP / "AGENTS.md").read_text(encoding="utf-8")
                elif uri == "lighttable://ui": text = json.dumps(client.get("/api/ui/state"))
                else: raise ValueError("unknown resource")
                result = {"contents": [{"uri": uri, "text": text}]}
            elif method == "notifications/initialized":
                continue
            else:
                raise ValueError(f"unsupported method: {method}")
            response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        except Exception as error:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32000, "message": str(error)}}
        print(json.dumps(response, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalize_global_arguments(raw_argv))
    if args.command == "instances":
        emit([{key: value for key, value in item.__dict__.items()
               if key not in {"token", "path"}}
              | {"path": str(item.path), "authenticated": bool(item.token)}
              for item in discover(discovery_directory(args))],
             json_mode=args.json, jsonl=args.jsonl, quiet=args.quiet)
        return 0
    if args.command == "serve":
        try:
            process, path = start_server(args, parent=not args.daemon)
            instance = next(item for item in discover(path.parent)
                            if item.pid == process.pid)
            emit({"ok": True, "pid": process.pid, "url": instance.url,
                  "instance": str(path)}, json_mode=True, quiet=args.quiet)
            if not args.daemon:
                try: return process.wait()
                except KeyboardInterrupt:
                    process.terminate(); process.wait(timeout=5); return 0
            return 0
        except (OSError, RuntimeError) as error:
            print(f"lighttable: {error}", file=sys.stderr)
            return EXIT_ERROR
    auto_process = None
    try:
        client, auto_process = client_for(args, allow_start=args.command not in {"status", "stop"})
        if args.command == "stop":
            instance = select(discover(discovery_directory(args)),
                              port=args.port, catalog=args.catalog)
            if not instance: raise ClientError(0, {"error": "no matching server", "code": "no-server"})
            os.kill(instance.pid, signal.SIGTERM)
            for _ in range(60):
                if not any(item.pid == instance.pid
                           for item in discover(instance.path.parent)):
                    break
                time.sleep(0.05)
            result = {"ok": True, "pid": instance.pid,
                      "stopped": not instance.path.exists()}
        else:
            result = dispatch(client, args)
        if result is not None:
            emit(result, json_mode=args.json, jsonl=args.jsonl, quiet=args.quiet)
        return 0
    except PermissionError as error:
        print(f"lighttable: {error}", file=sys.stderr); return EXIT_CONFIRMATION
    except ClientError as error:
        prefix = (f"{error.field}: " if error.field and
                  not str(error).startswith(f"{error.field}:") else "")
        print(f"lighttable: {prefix}{error}", file=sys.stderr)
        if error.code == "no-server" or error.status == 0: return EXIT_NO_SERVER
        if error.code == "validation": return EXIT_VALIDATION
        if error.code == "job-failed": return EXIT_JOB_FAILED
        return EXIT_ERROR
    except (ValueError, KeyError, OSError, RuntimeError,
            json.JSONDecodeError) as error:
        print(f"lighttable: {error}", file=sys.stderr); return EXIT_USAGE
    finally:
        # The headless child watches this process and exits just after us. An
        # explicit terminate makes test and short-command cleanup immediate.
        if auto_process is not None and auto_process.poll() is None:
            auto_process.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
