"""On-device analyzers behind one deliberately small interface."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import platform
import select
import subprocess
import threading
from pathlib import Path


class VisionProvider:
    """Use Apple's purpose-built Vision framework through a tiny Swift helper."""

    def __init__(self, helper: Path):
        self.helper = helper
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._request_id = 0

    @property
    def available(self) -> bool:
        return (platform.system() == "Darwin"
                and self.helper.is_file() and os.access(self.helper, os.X_OK))

    def analyze(self, image: Path) -> dict:
        if not self.available:
            raise RuntimeError("the local Vision analyzer has not been built")
        result = self._request("analyze", input=str(image))
        if not isinstance(result, dict):
            raise RuntimeError("Vision returned an invalid result")
        result["provider"] = "vision"
        return result

    def foreground_mask(self, image: Path, output: Path) -> bool:
        if not self.available:
            return False
        result = self._request(
            "foreground-mask", input=str(image), output=str(output))
        return bool(result.get("ok")) and output.is_file()

    def depth_map(self, image: Path, output: Path) -> bool:
        """Extract embedded capture depth when the source contains it."""
        if not self.available:
            return False
        try:
            result = self._request(
                "depth-map", input=str(image), output=str(output))
        except RuntimeError:
            return False
        return bool(result.get("available")) and output.is_file()

    def person_parts(self, image: Path, output_dir: Path) -> dict:
        """Generate the complete person-part set in one Vision request."""
        if not self.available:
            raise RuntimeError("People masks need the Vision helper (macOS 14 or later)")
        output_dir.mkdir(parents=True, exist_ok=True)
        result = self._request(
            "person-parts", input=str(image), outputDir=str(output_dir))
        return result

    def _start(self) -> subprocess.Popen:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        self._process = subprocess.Popen(
            [str(self.helper), "--server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        return self._process

    def _request(self, command: str, **payload) -> dict:
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            request = dict(payload, id=request_id, command=command)
            last_error: Exception | None = None
            for attempt in range(2):
                process = self._start()
                try:
                    if process.stdin is None or process.stdout is None:
                        raise RuntimeError("Vision server has no pipes")
                    process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                    process.stdin.flush()
                    ready, _, _ = select.select([process.stdout], [], [], 90)
                    if not ready:
                        raise TimeoutError("Vision analysis timed out")
                    line = process.stdout.readline()
                    if not line:
                        raise RuntimeError("Vision server stopped")
                    result = json.loads(line)
                    if not isinstance(result, dict) or result.get("id") != request_id:
                        raise RuntimeError("Vision returned an invalid result")
                    if not result.get("ok"):
                        raise RuntimeError(str(result.get("error") or
                                               "Vision analysis failed"))
                    result.pop("id", None)
                    return result
                except (BrokenPipeError, OSError, ValueError,
                        json.JSONDecodeError, TimeoutError, RuntimeError) as error:
                    last_error = error
                    self._stop()
                    if attempt:
                        break
            raise RuntimeError(str(last_error or "Vision analysis failed"))

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        finally:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()

    def shutdown(self) -> None:
        with self._lock:
            self._stop()


class FoundationModelsProvider:
    """Optional richer descriptions on systems with image prompting support."""

    def __init__(self):
        self._status: dict | None = None

    def status(self) -> dict:
        if self._status is not None:
            return dict(self._status)
        major = _macos_major()
        if major < 27:
            self._status = {
                "available": False,
                "reason": "Richer descriptions require macOS 27 or later",
            }
            return dict(self._status)
        if importlib.util.find_spec("apple_fm_sdk") is None:
            self._status = {
                "available": False,
                "reason": "The optional Apple Foundation Models bridge is not installed",
            }
            return dict(self._status)
        try:
            import apple_fm_sdk as fm

            available, reason = fm.SystemLanguageModel(
                use_case=fm.SystemLanguageModelUseCase.CONTENT_TAGGING,
            ).is_available()
            self._status = {
                "available": bool(available),
                "reason": "" if available else str(reason or "Apple Intelligence unavailable"),
            }
        except Exception as error:  # optional capability must never break Vision
            self._status = {"available": False, "reason": str(error)[:180]}
        return dict(self._status)

    def analyze(self, image: Path) -> dict:
        if not self.status()["available"]:
            raise RuntimeError(self.status()["reason"])
        return asyncio.run(self._analyze(image))

    async def _analyze(self, image: Path) -> dict:
        import apple_fm_sdk as fm

        @fm.generable("Search metadata for one photograph")
        class PhotoDescription:
            caption: str = fm.guide(
                "One factual sentence describing visible subjects, setting, and activity"
            )
            tags: list[str] = fm.guide(
                "Concrete visible objects, scene types, colors, and activities",
                max_items=14,
            )

        model = fm.SystemLanguageModel(
            use_case=fm.SystemLanguageModelUseCase.CONTENT_TAGGING,
        )
        session = fm.LanguageModelSession(
            model=model,
            instructions=(
                "Create concise photo-search metadata using only visible evidence. "
                "Do not identify people or infer identity, health, ethnicity, religion, "
                "politics, sexuality, or other sensitive traits."
            ),
        )
        response = await session.respond(
            [
                "Describe this photo for a private local photo search index.",
                fm.ImageAttachment(image, label="photo"),
            ],
            generating=PhotoDescription,
        )
        return {"caption": response.caption, "tags": list(response.tags)}

class LocalPhotoAnalyzer:
    """Combine fast Vision metadata with an optional richer model pass."""

    def __init__(self, vision_helper: Path, *, vision_provider=None):
        self.vision = vision_provider or VisionProvider(vision_helper)
        self.foundation = FoundationModelsProvider()

    def capabilities(self) -> dict:
        foundation = self.foundation.status()
        return {
            "vision": {
                "available": self.vision.available,
                "description": "Objects, scenes, visible text, and face detection",
            },
            "foundationModels": foundation,
            "contentMode": (
                "foundation-models" if foundation["available"] else "vision"
            ),
        }

    def analyze(self, image: Path) -> dict:
        result = self.vision.analyze(image)
        if self.foundation.status()["available"]:
            try:
                richer = self.foundation.analyze(image)
                result["caption"] = richer.get("caption", "")
                result["tags"] = _merge_tags(
                    richer.get("tags", []), result.get("tags", []),
                )
                result["provider"] = "vision+foundation-models"
            except Exception:
                # Image prompting is additive. Vision data remains useful if the
                # optional model is temporarily unavailable or rejects a photo.
                pass
        return result

    def shutdown(self) -> None:
        self.vision.shutdown()


def _macos_major() -> int:
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (TypeError, ValueError):
        return 0


def _merge_tags(primary, secondary) -> list[str]:
    output = []
    seen = set()
    for value in [*primary, *secondary]:
        text = " ".join(str(value).split()).strip()[:100]
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output[:20]
