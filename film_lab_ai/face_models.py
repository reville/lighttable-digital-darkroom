"""Pinned, optional face models. Downloads contain weights, never photo data."""

from __future__ import annotations

from server_localization import T

import hashlib
import importlib.util
import os
import tempfile
import urllib.request
from pathlib import Path

REVISION = "47534e27c9851bb1128ccc0102f1145e27f23f98"
MODEL_ID = "sface-int8-2021dec-v1"
MODELS = (
    ("face_recognition_sface/face_recognition_sface_2021dec_int8.onnx",
     9896933, "2b0e941e6f16cc048c20aee0c8e31f569118f65d702914540f7bfdc14048d78a"),
    ("face_detection_yunet/face_detection_yunet_2023mar.onnx",
     232589, "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
)
DOWNLOAD_BYTES = sum(item[1] for item in MODELS)


def valid_model(path: Path, size: int, digest: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() == digest


def ensure_models(root: Path, cancelled=lambda: False, progress=lambda n: None):
    """Verify before loading; publish each complete download atomically."""
    root.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    paths = []
    for remote, size, digest in MODELS:
        target = root / Path(remote).name
        paths.append(target)
        if valid_model(target, size, digest):
            downloaded += size
            progress(downloaded)
            continue
        temporary = None
        try:
            url = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{REVISION}/models/{remote}"
            request = urllib.request.Request(url, headers={"User-Agent": "LightTable-FaceModels/1"})
            with urllib.request.urlopen(request, timeout=20) as response, tempfile.NamedTemporaryFile(
                    dir=root, prefix=".download-", delete=False) as output:
                temporary = Path(output.name)
                received = 0
                while received <= size:
                    if cancelled():
                        raise InterruptedError(T("Face matching paused"))
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    received += len(block)
                    output.write(block)
                    progress(downloaded + min(received, size))
            if not valid_model(temporary, size, digest):
                raise RuntimeError(T("Face model download failed its integrity check. Try again."))
            os.replace(temporary, target)
            downloaded += size
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return paths


class FaceAnalyzer:
    def __init__(self, root: Path):
        self.root = root
        self.detector = self.recognizer = None

    def capabilities(self):
        return {"available": importlib.util.find_spec("cv2") is not None,
                "model": MODEL_ID, "modelName": "SFace · compact (INT8)",
                "downloadBytes": DOWNLOAD_BYTES,
                "installed": all((self.root / Path(p).name).is_file() for p, _, _ in MODELS)}

    def prepare(self, cancelled, progress):
        import cv2
        # Keep background indexing from occupying every CPU core while editing.
        cv2.setNumThreads(2)
        recognizer, detector = ensure_models(self.root, cancelled, progress)
        self.detector = cv2.FaceDetectorYN.create(str(detector), "", (320, 320), 0.9, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer), "")

    def analyze(self, preview: bytes):
        import cv2
        import numpy as np
        image = cv2.imdecode(np.frombuffer(preview, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(T("The photo preview could not be decoded"))
        if max(image.shape[:2]) > 1024:
            scale = 1024 / max(image.shape[:2])
            image = cv2.resize(image, (round(image.shape[1]*scale), round(image.shape[0]*scale)), interpolation=cv2.INTER_AREA)
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, detections = self.detector.detect(image)
        detections = list([] if detections is None else detections)
        # YuNet has a bounded face-size range. A second, smaller view finds
        # close-up faces without sacrificing distant faces in the first pass.
        if max(width, height) > 400:
            scale = 320 / max(width, height)
            small = cv2.resize(image, (round(width*scale), round(height*scale)))
            self.detector.setInputSize((small.shape[1], small.shape[0]))
            _, large_faces = self.detector.detect(small)
            for row in ([] if large_faces is None else large_faces):
                row = row.copy()
                row[:14] /= scale
                detections.append(row)
        if detections:
            keep = cv2.dnn.NMSBoxes([list(map(float, f[:4])) for f in detections],
                                   [float(f[-1]) for f in detections], 0.9, 0.3)
            detections = [detections[int(i)] for i in np.asarray(keep).flatten()]
        results = []
        for face in detections:
            x, y, w, h = map(float, face[:4])
            if min(w, h) < 24:
                continue  # Very small faces do not carry reliable matching detail.
            aligned = self.recognizer.alignCrop(image, face)
            vector = self.recognizer.feature(aligned).flatten().astype("float32")
            norm = float(np.linalg.norm(vector))
            if vector.size != 128 or not np.isfinite(vector).all() or norm < 1e-8:
                continue
            # Show a natural square crop, while matching uses landmark alignment.
            side = max(w, h) * 1.65
            cx, cy = x + w / 2, y + h * 0.45
            crop = image[max(0, int(cy-side/2)):min(height, int(cy+side/2)),
                         max(0, int(cx-side/2)):min(width, int(cx+side/2))]
            if not crop.size:
                continue
            crop = cv2.resize(crop, (160, 160), interpolation=cv2.INTER_AREA)
            ok, jpeg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                results.append({"box": [x/width, y/height, w/width, h/height],
                                "vector": vector / norm, "thumbnail": jpeg.tobytes(),
                                "quality": float(face[-1]) * min(w, h)})
        return results

    def shutdown(self):
        self.detector = self.recognizer = None
