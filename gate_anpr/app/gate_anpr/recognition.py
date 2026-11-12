"""FastALPR running in a separate low-priority process so OCR never stalls the event loop."""

import asyncio
import logging
import os
import platform
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from .decision import PlateRead
from .plates import normalise

_LOG = logging.getLogger(__name__)
_ALPR = None  # per worker process


@dataclass(frozen=True)
class Region:
    """Detection region as fractions of the frame (0..1)."""

    left: float = 0.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        x1, x2 = sorted((self.left, self.right))
        y1, y2 = sorted((self.top, self.bottom))
        box = (int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height))
        if box[2] - box[0] < 32 or box[3] - box[1] < 32:
            raise ValueError("detection region is too small")
        return box


def _init_worker(detector_model: str, ocr_model: str, threads: int) -> None:
    global _ALPR
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    import onnxruntime as ort
    from fast_alpr import ALPR

    def options():
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        return so

    _ALPR = ALPR(
        detector_model=detector_model,
        detector_providers=["CPUExecutionProvider"],
        detector_sess_options=options(),
        ocr_model=ocr_model,
        ocr_device="cpu",
        ocr_providers=["CPUExecutionProvider"],
        ocr_sess_options=options(),
    )


def _decode(jpeg):
    """A JPEG snapshot (bytes) or an already-decoded BGR video frame."""
    import cv2
    import numpy as np

    if not isinstance(jpeg, (bytes, bytearray)):
        return jpeg
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("camera image could not be decoded")
    return img


def _recognise(jpeg: bytes, region: Region) -> dict:
    img = _decode(jpeg)
    height, width = img.shape[:2]
    x1, y1, x2, y2 = region.pixels(width, height)
    started = time.perf_counter()
    results = _ALPR.predict(img[y1:y2, x1:x2])
    reads = []
    for res in results:
        if res.ocr is None:
            continue
        conf = res.ocr.confidence
        # The weakest character decides: one doubtful character means a doubtful plate.
        conf = min(conf) if isinstance(conf, list) else conf
        b = res.detection.bounding_box
        reads.append(
            {
                "text": normalise(res.ocr.text),
                "raw": res.ocr.text,
                "ocr_conf": round(float(conf or 0.0), 4),
                "det_conf": round(float(res.detection.confidence), 4),
                "box": (b.x1 + x1, b.y1 + y1, b.x2 + x1, b.y2 + y1),
            }
        )
    return {
        "width": width,
        "height": height,
        "reads": reads,
        "ms": round((time.perf_counter() - started) * 1000),
    }


def _self_test() -> int:
    """One full-HD inference, so start-up proves the models run on this CPU and times them."""
    import numpy as np

    started = time.perf_counter()
    _ALPR.predict(np.zeros((1080, 1920, 3), np.uint8))
    return round((time.perf_counter() - started) * 1000)


def _annotate(jpeg: bytes, box, label: str, region: Region, max_width: int,
              exclusions=()) -> tuple[bytes, bytes | None]:
    import cv2

    img = _decode(jpeg)
    height, width = img.shape[:2]
    plate_jpeg = None
    if box:
        x1, y1, x2, y2 = box
        pad = max(4, (x2 - x1) // 6)
        crop = img[max(0, y1 - pad): y2 + pad, max(0, x1 - pad): x2 + pad]
        plate_jpeg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
    rx1, ry1, rx2, ry2 = region.pixels(width, height)
    thick = max(2, width // 640)
    cv2.rectangle(img, (rx1, ry1), (rx2, ry2), (255, 200, 0), thick)
    for l, t, r, b in exclusions:
        cv2.rectangle(img, (int(l * width), int(t * height)), (int(r * width), int(b * height)), (0, 0, 255), thick)
    if box:
        cv2.rectangle(img, box[:2], box[2:], (0, 255, 0), thick * 2)
        scale = max(0.8, width / 1400)
        cv2.putText(img, label, (box[0], max(30, box[1] - 15)), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, (0, 0, 0), thick * 4, cv2.LINE_AA)
        cv2.putText(img, label, (box[0], max(30, box[1] - 15)), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, (0, 255, 0), thick, cv2.LINE_AA)
    if width > max_width:
        img = cv2.resize(img, (max_width, int(height * max_width / width)), interpolation=cv2.INTER_AREA)
    frame_jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    return frame_jpeg, plate_jpeg


def _cpu() -> dict:
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return {"model": platform.processor()}
    model = next((ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("model name")), "")
    flags = next((ln.split(":", 1)[1].split() for ln in text.splitlines() if ln.startswith(("flags", "Features"))), [])
    return {"model": model, "avx2": "avx2" in flags, "avx512f": "avx512f" in flags, "neon": "asimd" in flags}


_CPU = _cpu()


class RecognitionEngine:
    def __init__(self, detector_model: str, ocr_model: str, threads: int):
        self.detector_model = detector_model
        self.ocr_model = ocr_model
        self.threads = threads
        self._pool: ProcessPoolExecutor | None = None
        self.ready = False
        self.warmup_ms: int | None = None
        self.last_error: str | None = None

    def info(self) -> dict:
        return {
            "detector_model": self.detector_model,
            "ocr_model": self.ocr_model,
            "threads": self.threads,
            "machine": platform.machine(),
            "cpu": _CPU,
            "cpu_count": os.cpu_count(),
            "ready": self.ready,
            "warmup_ms": self.warmup_ms,
            "last_error": self.last_error,
        }

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=1,
            initializer=_init_worker,
            initargs=(self.detector_model, self.ocr_model, self.threads),
        )

    async def _run(self, fn, *args):
        if self._pool is None:
            self._pool = self._new_pool()
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, fn, *args)
        except BrokenProcessPool:
            # Worker died (e.g. out of memory). Start a fresh one next time; this call fails closed.
            self.ready = False
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
            self.last_error = "recognition worker crashed and was restarted"
            raise

    async def start(self) -> None:
        """Load the models and time one inference so the hardware is proven before use."""
        try:
            await self._run(_self_test)  # first call also loads the models
            self.warmup_ms = await self._run(_self_test)
            self.ready = True
            self.last_error = None
            _LOG.info("Recognition engine ready in %s ms (%s)", self.warmup_ms, self.info())
        except Exception as err:  # noqa: BLE001 - reported, engine stays not-ready
            self.ready = False
            self.last_error = f"engine start failed: {err}"
            _LOG.exception("Recognition engine failed to start")

    async def recognise(self, jpeg: bytes, region: Region) -> tuple[int, int, list[PlateRead], int]:
        out = await self._run(_recognise, jpeg, region)
        self.ready = True
        reads = [PlateRead(r["text"], r["raw"], r["ocr_conf"], r["det_conf"], tuple(r["box"]))
                 for r in out["reads"]]
        return out["width"], out["height"], reads, out["ms"]

    async def annotate(self, jpeg: bytes, box, label: str, region: Region,
                       max_width: int = 1280, exclusions=()) -> tuple[bytes, bytes | None]:
        return await self._run(_annotate, jpeg, box, label, region, max_width, tuple(exclusions))

    def close(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
