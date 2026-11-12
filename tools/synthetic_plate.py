"""Render synthetic NZ-style plate scenes for smoke tests (not a substitute for real camera images).

Usage: python tools/synthetic_plate.py OUT_DIR TEXT [WIDTH_PX ...]
Writes one 3840x2160 JPEG per plate width, simulating a vehicle approaching the camera.
"""

import pathlib
import sys

import cv2
import numpy as np


def scene(text: str, plate_w: int, cx: int = 1920, cy: int = 1500) -> np.ndarray:
    rng = np.random.default_rng(plate_w)
    img = rng.normal(110, 18, (2160, 3840, 3)).clip(0, 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (7, 7), 0)
    plate_h = int(plate_w * 0.27)  # NZ plates are 372 x 134 mm
    car_w, car_h = plate_w * 5, plate_w * 3
    cv2.rectangle(img, (cx - car_w // 2, cy - car_h // 2), (cx + car_w // 2, cy + car_h // 2), (60, 50, 140), -1)
    x1, y1 = cx - plate_w // 2, cy - plate_h // 2
    cv2.rectangle(img, (x1, y1), (x1 + plate_w, y1 + plate_h), (245, 245, 245), -1)
    cv2.rectangle(img, (x1, y1), (x1 + plate_w, y1 + plate_h), (20, 20, 20), max(2, plate_w // 100))
    scale = plate_w / 230
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, max(2, plate_w // 40))
    cv2.putText(img, text, (cx - tw // 2, cy + th // 2), cv2.FONT_HERSHEY_DUPLEX, scale, (15, 15, 15),
                max(2, plate_w // 40), cv2.LINE_AA)
    return img


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for w in [int(v) for v in sys.argv[3:]] or [180, 220, 260]:
        cv2.imwrite(str(out / f"{sys.argv[2]}_{w}.jpg"), scene(sys.argv[2], w), [cv2.IMWRITE_JPEG_QUALITY, 92])
