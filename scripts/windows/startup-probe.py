"""Temporary bounded diagnosis of the native launcher's cold Python command."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener

bundle, evidence = map(lambda value: Path(value).resolve(), sys.argv[1:])
evidence.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location("desktop_smoke", Path(__file__).with_name("desktop-smoke.py"))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
with tempfile.TemporaryDirectory(prefix="lighttable-cold-startup-") as temporary:
    root = Path(temporary)
    environment = smoke.smoke_environment(root)
    (root / "photos").mkdir()
    (root / "support").mkdir()
    (root / "support/prefs.json").write_text(json.dumps({"allowAutomation": True,
        "locale": "en", "localeChosen": True, "writeSidecars": False}), encoding="utf-8")
    import numpy as np
    import tifffile
    tifffile.imwrite(root / "photos/smoke.tif", np.tile(
        np.linspace(0, 65535, 1024, dtype=np.uint16)[None, :, None], (128, 1, 3)),
        photometric="rgb", metadata=None)
    environment.update(LIGHTTABLE_PORT="0", LIGHTTABLE_WATCH_PARENT="1",
        LIGHTTABLE_PARENT_PID=str(os.getpid()), LIGHTTABLE_WATCH_STDIN="1",
        OMP_NUM_THREADS="2", NUMBA_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2")
    command = [str(bundle / "Python/python.exe"), "-u", "-X", "importtime", "-X",
        f"pycache_prefix={root / 'cache/python-bytecode'}",
        str(bundle / "Resources/LightTable/server.py")]
    report = {"ok": False, "cold_bytecode": True, "startup_limit_seconds": 150,
        "native_launcher_limit_seconds": 45, "phases": []}
    opener = build_opener(ProxyHandler({}))
    with (evidence / "imports.log").open("wb") as log:
        started = time.monotonic()
        process = subprocess.Popen(command, env=environment,
            cwd=bundle / "Resources/LightTable", stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            previous = None
            while time.monotonic() - started < 150:
                if process.poll() is not None:
                    report["exit_code"] = process.returncode
                    break
                try:
                    state = json.loads((root / "startup.json").read_text(encoding="utf-8"))
                    phase = state.get("phase")
                    if phase != previous:
                        report["phases"].append({"phase": phase, "seconds": time.monotonic() - started})
                        previous = phase
                    if phase == "ready":
                        with opener.open(f"http://127.0.0.1:{int(state['port'])}/api/health", timeout=2) as response:
                            health = json.load(response)
                        if health.get("ok") is True and health.get("pid") == process.pid:
                            report["ready_seconds"] = time.monotonic() - started
                            report["ok"] = True
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            report["elapsed_seconds"] = time.monotonic() - started
            (evidence / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report), flush=True)
